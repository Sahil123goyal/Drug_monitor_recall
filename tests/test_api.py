"""HTTP contracts and approval validation with isolated async database doubles."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.contracts import AlertEnvelope
from app.db.async_session import get_async_db
from app.main import create_app
from app.models import Action, Decision, Medicine, Run, TestRun, Verdict


@pytest.fixture
def api():
    session = AsyncMock(spec=AsyncSession)
    session.add = Mock()
    session.execute.return_value = Mock(all=Mock(return_value=[]))
    app = create_app(enable_worker=False)

    async def dependency():
        yield session

    app.dependency_overrides[get_async_db] = dependency
    return app, session


def request(api, method, path, **kwargs):
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api[0]), base_url="http://test") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(invoke())


def waiting(kind="collect"):
    medicine_id, run_id = uuid4(), uuid4()
    return Run(
        id=run_id, household_id=uuid4(), destination="mock://synthetic", cursor=0,
        medicine_ids=[str(medicine_id)], status="WAITING", state={},
        pending_interrupt={"id": "question-1", "thread_id": f"{run_id}:{medicine_id}",
                           "medicine_id": str(medicine_id), "value": {"kind": kind, "alerts": []}},
        pending_resume=None, error=None, created_at=datetime.now(timezone.utc),
    )


def test_create_medicine_uses_async_session_and_allows_missing_batch(api):
    async def refresh(row):
        row.id = uuid4()
    api[1].refresh.side_effect = refresh
    response = request(api, "POST", "/medicines", json={
        "household_id": str(uuid4()), "name": " DemoRelief ", "strength": "500 mg",
    })
    assert response.status_code == 201
    assert response.json()["batch_no"] is None
    assert response.json()["name"] == "DemoRelief"
    api[1].commit.assert_awaited_once()
    assert isinstance(api[1].add.call_args.args[0], Medicine)


@pytest.mark.parametrize("field,value", [
    ("name", "  "), ("strength", ""), ("batch_no", " "), ("household_id", "invalid"),
    ("batch_no", 123), ("verdict", "MATCH"),
])
def test_invalid_inventory_never_touches_database(api, field, value):
    body = {"household_id": str(uuid4()), "name": "DemoRelief", "strength": "500mg"}
    body[field] = value
    assert request(api, "POST", "/medicines", json=body).status_code == 422
    api[1].commit.assert_not_awaited()


def test_start_freezes_household_inventory_and_returns_queued(api):
    identifiers = [uuid4(), uuid4()]
    api[1].scalars.return_value = Mock(all=Mock(return_value=identifiers))

    async def refresh(row):
        row.id, row.cursor = uuid4(), 0
        row.created_at = datetime.now(timezone.utc)
    api[1].refresh.side_effect = refresh
    response = request(api, "POST", "/runs", json={"household_id": str(uuid4())})
    assert response.status_code == 202
    assert response.json()["medicine_ids"] == [str(value) for value in identifiers]
    assert response.json()["status"] == "QUEUED"
    api[1].commit.assert_awaited_once()


def test_empty_household_is_not_a_successful_run(api):
    api[1].scalars.return_value = Mock(all=Mock(return_value=[]))
    assert request(api, "POST", "/runs", json={"household_id": str(uuid4())}).status_code == 404
    api[1].commit.assert_not_awaited()


def test_get_run_exposes_only_its_decisions_and_pending_question(api):
    run = waiting()
    decision = Decision(id=uuid4(), medicine_id=UUID(run.medicine_ids[0]), notice_id=uuid4(),
                        verdict=Verdict.UNCERTAIN, reason="Missing evidence", evidence={"synthetic": True},
                        created_at=datetime.now(timezone.utc))
    run.state = {run.medicine_ids[0]: {"decision_ids": [str(decision.id)]}}
    api[1].get.return_value = run
    api[1].scalars.return_value = Mock(all=Mock(return_value=[decision]))
    response = request(api, "GET", f"/runs/{run.id}")
    assert response.status_code == 200
    assert response.json()["pending_interrupt"]["id"] == "question-1"
    assert response.json()["decisions"][0]["verdict"] == "UNCERTAIN"
    query = api[1].scalars.call_args.args[0].compile()
    assert list(query.params.values()) == [[decision.id]]


@pytest.mark.parametrize("method,suffix,body", [
    ("GET", "", None), ("POST", "/provide-batch", {"interrupt_id": "x", "batch_no": "SYN001"}),
    ("POST", "/approve", {"interrupt_id": "x", "approvals": [{"action_id": str(uuid4()), "approved": False}]}),
])
def test_unknown_run(api, method, suffix, body):
    api[1].get.return_value = None
    assert request(api, method, f"/runs/{uuid4()}{suffix}", json=body).status_code == 404


def test_batch_reply_is_saved_before_returning(api):
    run = waiting()
    api[1].get.return_value = run
    response = request(api, "POST", f"/runs/{run.id}/provide-batch",
                       json={"interrupt_id": "question-1", "batch_no": " SYN001 "})
    assert response.status_code == 202
    assert run.status == "QUEUED" and run.pending_interrupt is None
    assert run.pending_resume["value"] == {"batch_no": "SYN001"}
    assert api[1].get.call_args.kwargs["with_for_update"] is True
    api[1].commit.assert_awaited_once()
    # A replay cannot consume a later question or enqueue a second reply.
    assert request(api, "POST", f"/runs/{run.id}/provide-batch",
                   json={"interrupt_id": "question-1", "batch_no": "SYN001"}).status_code == 409


@pytest.mark.parametrize("kind,identifier,status", [
    ("collect", "old", "WAITING"), ("approve", "question-1", "WAITING"),
    ("collect", "question-1", "RUNNING"), ("collect", "question-1", "COMPLETED"),
])
def test_wrong_or_stale_batch_reply(api, kind, identifier, status):
    run = waiting(kind)
    run.status = status
    api[1].get.return_value = run
    response = request(api, "POST", f"/runs/{run.id}/provide-batch",
                       json={"interrupt_id": identifier, "batch_no": "SYN001"})
    assert response.status_code == 409
    api[1].commit.assert_not_awaited()


def approval_fixture(api):
    run = waiting("approve")
    notice = {
        "recall_number": "SYN001", "report_date": "2026-01-15", "raw_json": {},
        "url": "https://api.fda.gov/drug/enforcement.json?search=recall_number:SYN001",
        "fetched_at": "2026-01-15T00:00:00Z",
    }
    decision = Decision(id=uuid4(), medicine_id=uuid4(), notice_id=uuid4(), verdict=Verdict.MATCH,
                        reason="Synthetic match", evidence={"notice_snapshot": notice})
    envelope = AlertEnvelope(destination="mock://synthetic", message=f"Recall 2026-01-15 {notice['url']}")
    action = Action(id=uuid4(), decision_id=decision.id, proposed_message=envelope.model_dump_json())
    run.pending_interrupt["value"]["alerts"] = [{"action_id": str(action.id), **envelope.model_dump()}]
    api[1].get.side_effect = lambda cls, *args, **kwargs: {Run: run, Action: action, Decision: decision}[cls]
    body = {"interrupt_id": "question-1", "approvals": [{"action_id": str(action.id), "approved": True}]}
    return run, body, envelope


def test_approve_preserves_edited_version_in_durable_reply(api):
    run, body, envelope = approval_fixture(api)
    body["approvals"][0].update(destination="mock://edited", message="Reviewed: " + envelope.message)
    assert request(api, "POST", f"/runs/{run.id}/approve", json=body).status_code == 202
    assert run.pending_resume["value"]["approvals"][0]["message"] == "Reviewed: " + envelope.message
    api[1].commit.assert_awaited_once()


def test_rejection_queues_false_without_sending(api):
    run, body, _ = approval_fixture(api)
    body["approvals"][0]["approved"] = False
    assert request(api, "POST", f"/runs/{run.id}/approve", json=body).status_code == 202
    assert run.pending_resume["value"]["approvals"][0]["approved"] is False
    assert api[1].get.await_count == 1  # Only the locked run; no action is approved here.


@pytest.mark.parametrize("mutation", ["text_false", "unknown", "duplicate", "missing_link", "empty", "verdict"])
def test_invalid_approvals_do_not_queue_or_send(api, mutation):
    run, body, _ = approval_fixture(api)
    item = body["approvals"][0]
    if mutation == "text_false":
        item["approved"] = "false"
    elif mutation == "unknown":
        item["action_id"] = str(uuid4())
    elif mutation == "duplicate":
        body["approvals"].append(item.copy())
    elif mutation == "missing_link":
        item["message"] = "Removed mandatory notice evidence"
    elif mutation == "empty":
        body["approvals"] = []
    else:
        item["verdict"] = "MATCH"
    assert request(api, "POST", f"/runs/{run.id}/approve", json=body).status_code == 422
    api[1].commit.assert_not_awaited()
    assert run.status == "WAITING"


def test_test_runs_preserves_unknown_result_and_paginates(api):
    row = TestRun(id=uuid4(), tc_name="synthetic", input={}, expected={"verdict": "MATCH"},
                  actual=None, evidence=None, passed=None)
    api[1].scalars.return_value = Mock(all=Mock(return_value=[row]))
    response = request(api, "GET", "/test-runs?limit=10&offset=20")
    assert response.status_code == 200 and response.json()[0]["passed"] is None
    assert request(api, "GET", "/test-runs?limit=101").status_code == 422
    assert request(api, "GET", "/test-runs?offset=-1").status_code == 422


def test_list_medicines_and_runs_filter_by_household(api):
    household = uuid4()
    api[1].scalars.return_value = Mock(all=Mock(return_value=[]))
    assert request(api, "GET", f"/medicines?household_id={household}").status_code == 200
    assert household in api[1].scalars.call_args.args[0].compile().params.values()
    assert request(api, "GET", f"/runs?household_id={household}").status_code == 200
    assert household in api[1].scalars.call_args.args[0].compile().params.values()


@pytest.mark.parametrize("active", [True, False])
def test_edit_inventory_is_blocked_during_active_checks(api, active):
    medicine = Medicine(id=uuid4(), household_id=uuid4(), name="DemoRelief", strength="500mg", batch_no="SYN001")
    api[1].get.return_value = medicine
    api[1].scalar.return_value = uuid4() if active else None
    response = request(api, "PUT", f"/medicines/{medicine.id}", json={
        "name": "DemoRelief", "strength": "250mg", "batch_no": None,
    })
    assert response.status_code == (409 if active else 200)
    if active:
        api[1].commit.assert_not_awaited()
        assert medicine.strength == "500mg"
    else:
        api[1].commit.assert_awaited_once()
        assert medicine.strength == "250mg" and medicine.batch_no is None


def test_get_run_includes_actual_delivery_status_and_notice_metadata(api):
    from app.models import ActionStatus
    run, _, envelope = approval_fixture(api)
    action = Action(id=uuid4(), decision_id=uuid4(), status=ActionStatus.REJECTED,
                    proposed_message=envelope.model_dump_json(), approved_message=None)
    decision = Decision(id=action.decision_id, medicine_id=UUID(run.medicine_ids[0]), notice_id=uuid4(),
                        evidence={"notice_snapshot": {"url": "https://api.fda.gov/drug/enforcement.json",
                                                     "report_date": "2026-01-15"}})
    api[1].execute.return_value = Mock(all=Mock(return_value=[(action, decision)]))
    response = request(api, "GET", f"/runs/{run.id}")
    assert response.status_code == 200
    assert response.json()["actions"][0]["status"] == "REJECTED"
    assert response.json()["actions"][0]["notice_date"] == "2026-01-15"


def test_evaluate_endpoint_persists_computed_core_cases(api):
    api[1].scalars.return_value = Mock(all=Mock(return_value=[]))
    assert request(api, "POST", "/test-runs/evaluate?suite=core").status_code == 200
    api[1].execute.assert_awaited_once()
    api[1].commit.assert_awaited_once()
    values = api[1].execute.call_args.args[0].compile().params
    assert [values[f"tc_name_m{index}"] for index in range(4)] == ["TC1", "TC2", "TC3", "TC4"]
