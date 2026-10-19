"""Groq is an optional fact extractor in the real approval workflow."""

import logging
from unittest.mock import Mock

import pytest
from langgraph.types import Command

from app.core.config import GroqSettings
from app.models.enums import Verdict
from app.schemas.extraction import ExtractedNotice
from app.schemas.medicine import Medicine
from app.services.notice_matching import match_notice


def settings(**overrides) -> GroqSettings:
    return GroqSettings(_env_file=None, **{
        "groq_extraction_enabled": True, "groq_api_key": "synthetic-key",
        "groq_model": "synthetic-model", **overrides,
    })


def messy_notice(harness):
    notice = harness["adapter"].fetch_notices.return_value[0]
    return notice.model_copy(update={
        "code_info": "The affected production runs carry SYN001 or SYN002 on the carton."
    })


def fields(**overrides) -> ExtractedNotice:
    return ExtractedNotice.model_validate({
        "brand": "DemoRelief", "strength": "500 mg", "batch_numbers": ["SYN001", "SYN002"],
        "confidence": 0.99, **overrides,
    })


@pytest.mark.parametrize("overrides,status", [
    ({"groq_extraction_enabled": False}, "disabled"),
    ({"groq_api_key": ""}, "missing_configuration"),
    ({"groq_model": ""}, "missing_configuration"),
])
def test_disabled_or_missing_configuration_cannot_call_groq(harness, overrides, status):
    extractor = Mock()
    result = match_notice(Medicine.model_validate(harness["medicine"]), messy_notice(harness),
                          settings=settings(**overrides), extractor=extractor)
    assert result.verdict == Verdict.UNCERTAIN
    assert result.evidence["extraction_status"] == status
    extractor.assert_not_called()


@pytest.mark.parametrize("batch,expected", [("SYN001", Verdict.MATCH), ("OTHER001", Verdict.NO_MATCH)])
def test_clear_deterministic_results_do_not_use_llm(harness, batch, expected):
    medicine = Medicine.model_validate(harness["medicine"] | {"batch_no": batch})
    extractor = Mock()
    result = match_notice(medicine, harness["adapter"].fetch_notices.return_value[0],
                          settings=settings(), extractor=extractor)
    assert result.verdict == expected and result.evidence["extraction_status"] == "not_needed"
    extractor.assert_not_called()


def test_missing_household_batch_still_requests_input_without_llm(harness):
    medicine = Medicine.model_validate(harness["medicine"] | {"batch_no": None})
    extractor = Mock()
    result = match_notice(medicine, messy_notice(harness), settings=settings(), extractor=extractor)
    assert result.evidence["requires_user_input"] is True
    assert result.verdict == Verdict.UNCERTAIN
    extractor.assert_not_called()


def test_only_public_notice_text_is_sent_and_exact_mismatch_survives(harness, caplog):
    medicine = Medicine.model_validate(harness["medicine"] | {"batch_no": "PRIVATE-HOUSEHOLD-001"})
    notice = messy_notice(harness)
    extractor = Mock(return_value=fields())
    with caplog.at_level(logging.INFO):
        result = match_notice(medicine, notice, settings=settings(), extractor=extractor)
    extractor.assert_called_once_with(notice.product_description + "\n" + notice.code_info)
    assert "PRIVATE-HOUSEHOLD-001" not in extractor.call_args.args[0]
    assert result.verdict == Verdict.NO_MATCH and result.evidence["extraction_status"] == "validated"
    assert "GROQ_EXTRACTION_STARTED" in caplog.text and "GROQ_EXTRACTION_COMPLETED" in caplog.text
    assert "synthetic-key" not in caplog.text and "PRIVATE-HOUSEHOLD-001" not in caplog.text


@pytest.mark.parametrize("extracted", [None, fields(confidence=0.3), fields(brand="InventedBrand"),
                                      fields(batch_numbers=["INVENTED001"])])
def test_failed_or_unsupported_extraction_cannot_upgrade_uncertain(harness, extracted):
    result = match_notice(Medicine.model_validate(harness["medicine"]), messy_notice(harness),
                          settings=settings(), extractor=Mock(return_value=extracted))
    assert result.verdict == Verdict.UNCERTAIN and result.evidence["extraction_status"] == "unusable"


def test_configured_threshold_reaches_the_deterministic_validation(harness):
    result = match_notice(Medicine.model_validate(harness["medicine"]), messy_notice(harness),
                          settings=settings(groq_min_confidence=1), extractor=Mock(return_value=fields()))
    assert result.verdict == Verdict.UNCERTAIN


def test_real_graph_calls_extraction_and_still_pauses_twice_for_human_review(harness, monkeypatch):
    h = harness
    notice = messy_notice(h)
    h["adapter"].fetch_notices.return_value = [notice]
    h["repository"].save_notices.return_value[0]["data"] = notice.model_dump(mode="json")
    config = settings()
    monkeypatch.setattr("app.services.notice_matching.GroqSettings", lambda: config)
    extractor = Mock(return_value=fields())
    monkeypatch.setattr("app.services.notice_matching.extract_notice_fields", extractor)
    state = h["graph"].invoke(h["input"], h["config"])
    extractor.assert_called_once_with(notice.product_description + "\n" + notice.code_info, settings=config)
    question = state["__interrupt__"][0].value
    assert question["kind"] == "confirm"
    assert question["decisions"][0]["extraction_status"] == "validated"
    decision = h["repository"].decision(state["decision_ids"][0])
    assert decision.verdict == Verdict.MATCH
    assert decision.evidence["extraction_model"] == "synthetic-model"
    assert decision.evidence["notice_snapshot"]["code_info"] == notice.code_info
    h["repository"].propose.assert_not_called()
    h["repository"].send_mock.assert_not_called()

    approved_decision = h["graph"].invoke(Command(resume={"confirmations": [
        {"decision_id": decision.id, "accepted": True}
    ]}), h["config"])
    assert approved_decision["__interrupt__"][0].value["kind"] == "approve"
    h["repository"].send_mock.assert_not_called()
    assert extractor.call_count == 1  # Human-review resumes do not rerun matching.
