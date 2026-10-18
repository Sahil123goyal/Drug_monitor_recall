"""Exercise Groq's real SDK with mocked HTTP and three synthetic notice texts."""

import json
from datetime import date, datetime, timezone
from typing import Any

import httpx
import pytest
from groq import Groq

from app.core.config import GroqSettings
from app.models.enums import Verdict
from app.schemas.extraction import ExtractedNotice
from app.schemas.medicine import Medicine
from app.schemas.notice import Notice
from app.services.extraction import extract_notice_fields
from app.services.matcher import match_extracted


def config(**overrides: Any) -> GroqSettings:
    values = {
        "groq_model": "synthetic-test-model", "groq_api_key": "synthetic-test-key",
        "groq_max_retries": 0, "groq_timeout_seconds": 2, "groq_min_confidence": 0.9,
    }
    return GroqSettings(_env_file=None, **(values | overrides))


def source(description: str, codes: str) -> Notice:
    return Notice(
        recall_number="SYN-EXTRACT-001", product_description=description, code_info=codes,
        url="https://api.fda.gov/drug/enforcement.json?search=recall_number:SYN-EXTRACT-001",
        report_date=date(2026, 1, 15), fetched_at=datetime(2026, 1, 16, tzinfo=timezone.utc),
        raw_json={"synthetic": True},
    )


def mock_client(
    content: str, *, finish_reason: str = "stop", status: int = 200,
) -> tuple[Groq, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openai/v1/chat/completions"
        requests.append(json.loads(request.content))
        return httpx.Response(status, json={
            "id": "synthetic-completion", "object": "chat.completion", "created": 0,
            "model": "synthetic-test-model",
            "choices": [{
                "index": 0, "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }],
        })

    client = Groq(
        api_key="synthetic-test-key", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return client, requests


# Three sample notices: confident extraction, a different batch, and damaged text.
SAMPLES = [
    (
        "Brand: DemoRelief®; each tablet contains 500 mg. Retail packs withdrawn.",
        "The affected production runs carry SYN-001 or SYN-002 on the carton.",
        {"brand": "DemoRelief", "strength": "500 mg",
         "batch_numbers": ["SYN-001", "SYN-002"], "confidence": 0.99},
        Medicine(name="DemoRelief", strength="500mg", batch_no="SYN-001"),
        Verdict.MATCH,
    ),
    (
        "CobaltCare\n25 mg tablets — packing text follows the product name.",
        "Following inspection, the affected identifiers were CC002 and CC003 only.",
        {"brand": "CobaltCare", "strength": "25 mg",
         "batch_numbers": ["CC002", "CC003"], "confidence": 0.98},
        Medicine(name="CobaltCare", strength="25mg", batch_no="CC001"),
        Verdict.NO_MATCH,
    ),
    (
        "MistRx product label damaged; strength illegible.",
        "Lot identifiers cannot be read from this copy.",
        {"brand": "MistRx", "strength": None, "batch_numbers": [], "confidence": 0.2},
        Medicine(name="MistRx", strength="10mg", batch_no="M001"),
        Verdict.UNCERTAIN,
    ),
]


@pytest.mark.parametrize("description,codes,payload,medicine,expected", SAMPLES)
def test_three_sample_notice_texts(
    description: str, codes: str, payload: dict[str, Any],
    medicine: Medicine, expected: Verdict,
) -> None:
    notice = source(description, codes)
    text = description + "\n" + codes
    client, requests = mock_client(json.dumps(payload))
    with client:
        fields = extract_notice_fields(text, client=client, settings=config())
    if expected == Verdict.UNCERTAIN:
        assert fields is None
    else:
        assert isinstance(fields, ExtractedNotice)
        assert fields.model_dump(mode="json") == payload
        assert json.loads(fields.model_dump_json()) == payload
    decision = match_extracted(medicine, notice, fields)
    assert decision.verdict == expected
    assert decision.evidence["notice"]["code_info"] == codes
    assert decision.evidence["notice"]["url"] == str(notice.url)
    assert len(requests) == 1
    request = requests[0]
    assert request["response_format"] == {"type": "json_object"}
    assert request["temperature"] == 0
    assert "tools" not in request
    assert json.loads(request["messages"][1]["content"]) == {"notice_text": text}
    assert "never decide" in request["messages"][0]["content"]
    assert "verdict" not in ExtractedNotice.model_json_schema()["properties"]


VALID = {"brand": "DemoRelief", "strength": "500 mg",
         "batch_numbers": ["SYN001"], "confidence": 0.99}
TEXT = "DemoRelief 500 mg tablets. Lot SYN001."


@pytest.mark.parametrize("content", [
    "not JSON", "```json\n{}\n```", "null", "{}",
    json.dumps(VALID | {"confidence": 0.4}),
    json.dumps(VALID | {"confidence": 2}),
    json.dumps(VALID | {"confidence": "0.99"}),
    json.dumps(VALID | {"brand": "InventedBrand"}),
    json.dumps(VALID | {"strength": "50 mg"}),
    json.dumps(VALID | {"batch_numbers": ["SYN002"]}),
    json.dumps(VALID | {"batch_numbers": ["001"]}),
    json.dumps(VALID | {"batch_numbers": ["syn001"]}),
    json.dumps(VALID | {"batch_numbers": ["SYN001", "SYN001"]}),
    json.dumps(VALID | {"batch_numbers": []}),
    json.dumps(VALID | {"verdict": "MATCH"}),
])
def test_invalid_low_confidence_or_invented_output_returns_none(content: str) -> None:
    client, _ = mock_client(content)
    with client:
        assert extract_notice_fields(TEXT, client=client, settings=config()) is None


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "tool_calls"])
def test_incomplete_or_refused_completions_return_none(finish_reason: str) -> None:
    client, _ = mock_client(json.dumps(VALID), finish_reason=finish_reason)
    with client:
        assert extract_notice_fields(TEXT, client=client, settings=config()) is None


@pytest.mark.parametrize("status", [401, 429, 500])
def test_api_errors_return_none(status: int) -> None:
    client, _ = mock_client("", status=status)
    with client:
        assert extract_notice_fields(TEXT, client=client, settings=config()) is None


def test_timeout_returns_none() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with Groq(api_key="synthetic", http_client=httpx.Client(transport=httpx.MockTransport(timeout))) as client:
        assert extract_notice_fields(TEXT, client=client, settings=config()) is None


def test_missing_config_and_blank_input_do_not_call_groq() -> None:
    assert extract_notice_fields(TEXT, settings=config(groq_api_key="")) is None
    assert extract_notice_fields(TEXT, settings=config(groq_model="")) is None
    assert extract_notice_fields("  ", settings=config()) is None
    assert extract_notice_fields("x" * 32_001, settings=config()) is None


def test_configurable_confidence_threshold() -> None:
    client, _ = mock_client(json.dumps(VALID))
    with client:
        assert extract_notice_fields(TEXT, client=client, settings=config(groq_min_confidence=1)) is None


def test_failed_extraction_is_uncertain_even_if_raw_match_would_succeed() -> None:
    notice = source("DemoRelief 500 mg tablets", "Lot SYN001")
    medicine = Medicine(name="DemoRelief", strength="500mg", batch_no="SYN001")
    assert match_extracted(medicine, notice, None).verdict == Verdict.UNCERTAIN
    low_confidence = ExtractedNotice.model_validate(VALID | {"confidence": 0.1})
    assert match_extracted(medicine, notice, low_confidence).verdict == Verdict.UNCERTAIN


def test_extraction_cannot_override_mismatches_or_source_evidence() -> None:
    notice = source("DemoRelief 500 mg tablets", "Lot SYN001")
    fields = ExtractedNotice.model_validate(VALID)
    medicine = Medicine(name="DemoRelief", strength="250mg", batch_no="SYN001")
    assert match_extracted(medicine, notice, fields).verdict == Verdict.NO_MATCH
    changed_notice = notice.model_copy(update={"code_info": "Lot OTHER001"})
    assert match_extracted(medicine, changed_notice, fields).verdict == Verdict.UNCERTAIN


def test_omitted_parseable_batch_list_is_uncertain() -> None:
    notice = source("DemoRelief 500 mg tablets", "Lot SYN001, SYN002")
    medicine = Medicine(name="DemoRelief", strength="500mg", batch_no="SYN002")
    fields = ExtractedNotice.model_validate(VALID)
    assert match_extracted(medicine, notice, fields).verdict == Verdict.UNCERTAIN


def test_missing_household_batch_still_requires_user_input() -> None:
    notice = source("DemoRelief 500 mg tablets", "Lot SYN001")
    medicine = Medicine(name="DemoRelief", strength="500mg")
    decision = match_extracted(medicine, notice, None)
    assert decision.verdict == Verdict.UNCERTAIN
    assert decision.evidence["requires_user_input"] is True


@pytest.mark.parametrize("description", [
    "DemoRelief 500 mg/5ml oral liquid",
    "DemoRelief 500 mg/unknown oral liquid",
    "DemoRelief 500 mg tablets; OtherBrand 250mg tablets",
])
def test_extraction_cannot_shorten_or_select_ambiguous_strengths(description: str) -> None:
    notice = source(description, "Lot SYN001")
    medicine = Medicine(name="DemoRelief", strength="500mg", batch_no="SYN001")
    fields = ExtractedNotice.model_validate(VALID)
    assert match_extracted(medicine, notice, fields).verdict == Verdict.UNCERTAIN


def test_unexpected_api_response_shape_returns_none() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": None})

    with Groq(api_key="synthetic", http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        assert extract_notice_fields(TEXT, client=client, settings=config()) is None


def test_high_confidence_cannot_turn_expiry_date_into_a_batch() -> None:
    notice = source("DemoRelief 500 mg tablets", "Lot SYN001; Exp: 2027")
    medicine = Medicine(name="DemoRelief", strength="500mg", batch_no="2027")
    fields = ExtractedNotice.model_validate(VALID | {"batch_numbers": ["2027"]})
    assert match_extracted(medicine, notice, fields).verdict == Verdict.UNCERTAIN
