"""Streamlit widget tests and display rules; API calls are isolated synthetic doubles."""

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from streamlit.testing.v1 import AppTest

from app.agents.contracts import AlertEnvelope
from app.models.enums import ActionStatus, Verdict
from app.schemas.api import ActionResponse, DecisionResponse, MedicineResponse, PendingInterrupt, RunResponse, RunSummary
from app.services.evaluations import CORE_CASES, evaluate_core_cases
from frontend.api_client import APIClient, APIError
from frontend.presentation import medicine_result, official_url, step_rows

APP = Path(__file__).resolve().parents[1] / "frontend" / "app.py"


@pytest.fixture
def ui(monkeypatch):
    household = "00000000-0000-4000-8000-000000000001"
    medicine = MedicineResponse(id=uuid4(), household_id=household, name="DemoRelief", strength="500mg", batch_no="SYN001")
    run = RunResponse(id=uuid4(), household_id=household, destination="mock://synthetic", status="COMPLETED",
                      medicine_ids=[medicine.id], cursor=1, state={str(medicine.id): {
                          "medicine": medicine.model_dump(mode="json", include={"name", "strength", "batch_no"}),
                          "completed_nodes": ["collect", "plan", "retrieve", "match", "approve", "act", "verify"],
                      }}, pending_interrupt=None, decisions=[], actions=[], error=None,
                      created_at=datetime.now(timezone.utc))
    api = Mock(spec=APIClient)
    api.medicines.return_value = [medicine]
    api.runs.return_value = [RunSummary.model_validate(run)]
    api.run.return_value = run
    api.start_run.return_value = run
    api.test_runs.return_value = []
    monkeypatch.setattr("frontend.ui.client", lambda: api)
    return api, run, medicine


def page(path="views/medicines.py"):
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    if path != "views/medicines.py":
        app.switch_page(path).run()
    assert not app.exception
    return app


def button(app, label):
    return next(item for item in app.button if item.label == label)


def field(app, label):
    return next(item for item in app.text_input if item.label == label)


def approval(ui):
    api, run, medicine = ui
    envelope = AlertEnvelope(destination="mock://synthetic", message="Mock recall 2026-01-15 https://api.fda.gov/drug/enforcement.json?search=recall_number:SYN001")
    action = ActionResponse(id=uuid4(), decision_id=uuid4(), medicine_id=medicine.id, status=ActionStatus.PENDING_APPROVAL,
                            proposed=envelope, approved=None, notice_url="https://api.fda.gov/drug/enforcement.json?search=recall_number:SYN001",
                            notice_date=date(2026, 1, 15))
    run.actions = [action]
    run.status, run.cursor = "WAITING", 0
    run.pending_interrupt = PendingInterrupt(id="approve-current", thread_id=f"{run.id}:{medicine.id}", medicine_id=medicine.id,
        value={"kind": "approve", "alerts": [{"action_id": str(action.id), **envelope.model_dump()}]})

    def accepted(*args):
        run.status, run.pending_interrupt = "QUEUED", None
        return run
    api.approve.side_effect = accepted
    return action


def test_add_edit_forms_only_save_on_submission(ui):
    api, _, medicine = ui
    app = page()
    api.save_medicine.assert_not_called()
    field(app, "Brand").set_value("DemoTwo")
    field(app, "Strength").set_value("10 mg")
    field(app, "Batch number (optional)").set_value("SYN002")
    button(app, "Add medicine").click().run()
    assert not app.exception
    api.save_medicine.assert_called_once_with(medicine.household_id, "DemoTwo", "10 mg", "SYN002")
    api.save_medicine.reset_mock()
    app.text_input(key=f"edit_strength:{medicine.id}").set_value("250mg")
    button(app, "Save changes").click().run()
    assert not app.exception
    api.save_medicine.assert_called_once_with(medicine.household_id, "DemoRelief", "250mg", "SYN001", medicine.id)


def test_missing_brand_is_rejected_without_calling_api(ui):
    app = page()
    button(app, "Add medicine").click().run()
    ui[0].save_medicine.assert_not_called()
    assert any("Brand and strength" in item.value for item in app.error)


def test_start_monitor_and_show_saved_steps(ui):
    api, run, medicine = ui
    run.status, run.cursor = "RUNNING", 0
    run.state[str(medicine.id)]["completed_nodes"] = ["collect", "plan"]
    app = page("views/monitor.py")
    assert any(item.value == "Running" for item in app.markdown)
    api.start_run.assert_not_called()
    button(app, "Start check").click().run()
    api.start_run.assert_called_once_with(medicine.household_id, "mock://synthetic-household")
    assert not app.exception


@pytest.mark.parametrize("label,approved", [("Approve", True), ("Reject", False)])
def test_alert_buttons_submit_exact_current_interrupt_only_after_click(ui, label, approved):
    action = approval(ui)
    api, run, _ = ui
    app = page("views/approvals.py")
    api.approve.assert_not_called()
    app.run()  # Refreshing is read-only.
    api.approve.assert_not_called()
    assert any("2026-01-15" in item.value for item in app.markdown)
    button(app, label).click().run()
    assert not app.exception
    api.approve.assert_called_once_with(run.id, "approve-current", {str(action.id): approved})


def test_approval_without_official_link_cannot_be_approved(ui):
    action = approval(ui)
    action.notice_url = "https://example.com/not-official"
    app = page("views/approvals.py")
    assert button(app, "Approve").disabled
    assert not button(app, "Reject").disabled
    ui[0].approve.assert_not_called()


def test_multiple_alerts_require_an_explicit_choice_for_every_item(ui):
    first = approval(ui)
    api, run, _ = ui
    second = first.model_copy(update={"id": uuid4()})
    run.actions.append(second)
    run.pending_interrupt.value["alerts"].append({"action_id": str(second.id), **second.proposed.model_dump()})
    app = page("views/approvals.py")
    app.button(key=f"approve:approve-current:{first.id}").click().run()
    api.approve.assert_not_called()
    app.run()
    api.approve.assert_not_called()
    app.button(key=f"reject:approve-current:{second.id}").click().run()
    api.approve.assert_called_once_with(run.id, "approve-current", {str(first.id): True, str(second.id): False})


def test_missing_batch_form_submits_exact_question(ui):
    api, run, medicine = ui
    run.status, run.cursor = "WAITING", 0
    run.pending_interrupt = PendingInterrupt(id="batch-current", thread_id=f"{run.id}:{medicine.id}", medicine_id=medicine.id,
                                             value={"kind": "collect", "question": "Enter the package batch."})
    app = page("views/approvals.py")
    button(app, "Provide batch").click().run()
    api.provide_batch.assert_not_called()
    field(app, "Batch number").set_value("SYN003")
    button(app, "Provide batch").click().run()
    assert not app.exception
    api.provide_batch.assert_called_once_with(run.id, "batch-current", "SYN003")
    api.approve.assert_not_called()


def test_test_case_page_never_fabricates_passes(ui):
    from app.schemas.api import TestRunResponse
    app = page("views/test_cases.py")
    assert set(app.dataframe[0].value["Result"]) == {"Not run"}
    ui[0].evaluate.assert_not_called()
    ui[0].evaluate.return_value = [TestRunResponse.model_validate(row) for row in evaluate_core_cases()]
    button(app, "Run TC1–TC4").click().run()
    assert not app.exception
    assert set(app.dataframe[0].value["Result"]) == {"PASS"}
    assert list(app.dataframe[0].value["Case"]) == ["TC1", "TC2", "TC3", "TC4"]


def test_api_failure_is_visible_and_not_rendered_as_results(ui):
    ui[0].run.side_effect = APIError("API unavailable")
    app = page("views/results.py")
    assert any("API unavailable" in item.value for item in app.error)
    assert not any("No match" in item.value for item in app.info)


def test_result_rules_distinguish_pending_rejected_uncertain_and_sent(ui):
    action = approval(ui)
    _, run, medicine = ui
    run.decisions = [DecisionResponse(id=action.decision_id, medicine_id=medicine.id, notice_id=uuid4(),
                                      verdict=Verdict.MATCH, reason="Synthetic match", evidence={}, created_at=run.created_at)]
    assert medicine_result(run, medicine.id).label == "Match — awaiting alert"
    action.status = ActionStatus.REJECTED
    assert medicine_result(run, medicine.id).label == "Alert rejected"
    action.status = ActionStatus.SENT
    assert medicine_result(run, medicine.id).label == "Alert with notice link"
    run.decisions.append(run.decisions[0].model_copy(update={"id": uuid4(), "verdict": Verdict.UNCERTAIN}))
    assert medicine_result(run, medicine.id).uncertain
    run.state[str(medicine.id)]["verification_results"] = [{"result": "UNCERTAIN"}]
    assert medicine_result(run, medicine.id).tone == "error"


def test_results_page_shows_no_match_only_for_completed_check(ui):
    app = page("views/results.py")
    assert any("No match" in item.value for item in app.info)
    _, run, medicine = ui
    run.state[str(medicine.id)]["completed_nodes"] = []
    app.run()
    assert any("Not completed" in item.value for item in app.info)
    assert not any("No match" in item.value for item in app.info)


def test_step_rows_report_waiting_and_failed_without_fake_progress(ui):
    _, run, medicine = ui
    run.status, run.cursor = "RUNNING", 0
    run.state[str(medicine.id)]["completed_nodes"] = ["collect", "plan"]
    rows = step_rows(run, medicine.id)
    assert [row["Status"] for row in rows] == ["Done", "Done", "Running", "Not started", "Not started", "Not started", "Not started", "Not started"]
    run.error = "Source unavailable"
    assert step_rows(run, medicine.id)[2]["Status"] == "Failed"


def test_core_suite_has_real_computed_verdicts():
    rows = evaluate_core_cases()
    assert len(rows) == 4 and all(row["passed"] for row in rows)
    assert [row["actual"]["verdict"] for row in rows] == ["MATCH", "NO_MATCH", "UNCERTAIN", "UNCERTAIN"]
    assert len({case.id for case in CORE_CASES}) == 4


@pytest.mark.parametrize("url", ["http://api.fda.gov/drug/enforcement.json", "javascript:alert(1)",
    "https://api.fda.gov.evil.test/drug/enforcement.json", "https://api.fda.gov:bad/drug/enforcement.json", None])
def test_untrusted_notice_links_are_not_rendered(url):
    assert official_url(url) is None
