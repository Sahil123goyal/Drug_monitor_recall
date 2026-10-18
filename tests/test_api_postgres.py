"""End-to-end HTTP -> async queue -> PostgresSaver -> audit tables, synthetic data only."""

import asyncio
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.agents.graph import build_graph
from app.agents.repository import WorkflowRepository
from app.agents.runtime import checkpoint_dsn
from app.db.async_session import get_async_db
from app.main import create_app
from app.models import Action, ActionStatus, TestRun, Verification
from app.schemas.notice import Notice
from app.services.run_worker import GraphRunner, RunWorker
from app.services.verification import OpenFDAVerifier

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("approved,verification_ok", [(True, True), (False, True), (True, False)])
def test_household_pause_resume_approval_and_verification(database, approved, verification_ok):
    with database.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    notice = Notice(
        recall_number="SYN-API-" + uuid4().hex, product_description="DemoRelief 500 mg tablets",
        code_info="Lot SYN001", status="Ongoing", report_date=date(2026, 1, 15),
        fetched_at=datetime.now(timezone.utc), raw_json={"synthetic": True},
        url="https://api.fda.gov/drug/enforcement.json?search=recall_number:SYN001",
    )
    adapter = Mock()
    adapter.fetch_notices.return_value = [notice]
    opened = []

    def reopen(request):
        opened.append(str(request.url))
        return httpx.Response(200, json={"results": [{
            "recall_number": notice.recall_number, "product_description": notice.product_description,
            "code_info": "Lot SYN001" if verification_ok else "Lot DIFFERENT001",
            "status": "Ongoing", "report_date": "20260115",
        }]})

    repository = WorkflowRepository(sessionmaker(database, expire_on_commit=False))

    @contextmanager
    def graph_factory():
        # Every call uses a fresh connection/checkpointer, proving restart persistence.
        with psycopg.connect(checkpoint_dsn(database.url.render_as_string(hide_password=False)),
                             options=f"-csearch_path={schema}", autocommit=True,
                             prepare_threshold=0, row_factory=dict_row) as connection:
            saver = PostgresSaver(connection)
            saver.setup()
            with httpx.Client(transport=httpx.MockTransport(reopen)) as client:
                yield build_graph(repository, {"openFDA": adapter}, OpenFDAVerifier(client=client), saver)

    async def scenario():
        engine = create_async_engine(database.url, connect_args={"options": f"-csearch_path={schema}"})
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        app = create_app(enable_worker=False)

        async def db():
            async with sessions() as session:
                yield session

        app.dependency_overrides[get_async_db] = db
        household_id = str(uuid4())
        run_id = None
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.post("/runs", json={"household_id": household_id})).status_code == 404
                for batch in (None, "DIFFERENT001"):
                    response = await client.post("/medicines", json={
                        "household_id": household_id, "name": "DemoRelief", "strength": "500mg", "batch_no": batch,
                    })
                    assert response.status_code == 201
                await client.post("/medicines", json={
                    "household_id": str(uuid4()), "name": "Other household", "strength": "5mg",
                })
                response = await client.post("/runs", json={"household_id": household_id})
                assert response.status_code == 202 and len(response.json()["medicine_ids"]) == 2
                run_id = UUID(response.json()["id"])
                path = f"/runs/{run_id}"
                worker = RunWorker(engine, GraphRunner(graph_factory))
                async def confirm_until_next_question(current):
                    # Medicine UUID order is random: the NO_MATCH can come first.
                    while (current["pending_interrupt"] and
                           current["pending_interrupt"]["value"]["kind"] == "confirm"):
                        pending = current["pending_interrupt"]
                        response = await client.post(path + "/confirm-decisions", json={
                            "interrupt_id": pending["id"], "confirmations": [
                                {"decision_id": item["decision_id"], "accepted": True}
                                for item in pending["value"]["decisions"]]})
                        assert response.status_code == 202
                        await RunWorker(engine, GraphRunner(graph_factory)).process(run_id)
                        current = (await client.get(path)).json()
                    return current
                # Concurrent workers share the PostgreSQL advisory lock.
                await asyncio.gather(worker.process(run_id), worker.process(run_id))
                current = (await client.get(path)).json()
                current = await confirm_until_next_question(current)
                assert current["status"] == "WAITING"
                question = current["pending_interrupt"]
                assert question["value"]["kind"] == "collect"
                body = {"interrupt_id": question["id"], "batch_no": "SYN001"}
                replies = await asyncio.gather(client.post(path + "/provide-batch", json=body),
                                               client.post(path + "/provide-batch", json=body))
                assert sorted(response.status_code for response in replies) == [202, 409]

                # New worker instance resumes only the persisted reply/checkpoints.
                await RunWorker(engine, GraphRunner(graph_factory)).process(run_id)
                current = (await client.get(path)).json()
                current = await confirm_until_next_question(current)
                question = current["pending_interrupt"]
                assert question["value"]["kind"] == "approve"
                alert = question["value"]["alerts"][0]
                action_id = UUID(alert["action_id"])
                medicine_id = current["pending_interrupt"]["medicine_id"]
                editing = await client.put(f"/medicines/{medicine_id}", json={
                    "name": "DemoRelief", "strength": "250mg", "batch_no": "SYN001",
                })
                assert editing.status_code == 409
                async with sessions() as session:
                    assert (await session.get(Action, action_id)).status == ActionStatus.PENDING_APPROVAL
                assert not opened  # Verification and mock delivery wait for approval.
                edited = "Human reviewed synthetic alert. " + alert["message"]
                answer = {"interrupt_id": question["id"], "approvals": [{
                    "action_id": str(action_id), "approved": approved,
                    "destination": "mock://approved", "message": edited,
                }]}
                assert (await client.post(path + "/approve", json=answer)).status_code == 202
                assert (await client.post(path + "/approve", json=answer)).status_code == 409
                await RunWorker(engine, GraphRunner(graph_factory)).process(run_id)
                final = (await client.get(path)).json()
                final = await confirm_until_next_question(final)
                assert final["status"] == ("FAILED" if approved and not verification_ok else "COMPLETED")
                assert final["pending_interrupt"] is None and final["cursor"] == 2
                assert sorted(item["verdict"] for item in final["decisions"]) == ["MATCH", "NO_MATCH"]
                assert len(final["state"]) == 2
                await worker.process(run_id)
                async with sessions() as session:
                    action = await session.get(Action, action_id)
                    assert action.status == (ActionStatus.SENT if approved else ActionStatus.REJECTED)
                    if approved:
                        assert edited in action.approved_message
                        assert "mock://approved" in action.approved_message
                    else:
                        assert action.approved_message is None
                    count = await session.scalar(select(func.count()).select_from(Verification)
                                                 .where(Verification.action_id == action_id))
                    assert count == int(approved)
                    if approved and not verification_ok:
                        assert any(state.get("failures") for state in final["state"].values())
                    session.add(TestRun(tc_name="api-synthetic", input={}, expected={}, actual={},
                                        evidence={"synthetic": True}, passed=True))
                    await session.commit()
                assert len(opened) == int(approved)
                assert (await client.get("/test-runs?limit=100")).status_code == 200
                cases = (await client.post("/test-runs/evaluate?suite=core")).json()
                assert [item["tc_name"] for item in cases] == ["TC1", "TC2", "TC3", "TC4"]
                assert all(item["passed"] for item in cases)
                again = (await client.post("/test-runs/evaluate?suite=core")).json()
                assert [item["id"] for item in cases] == [item["id"] for item in again]
                assert len((await client.get("/test-runs?suite=core")).json()) == 4
                assert len((await client.get(f"/medicines?household_id={household_id}")).json()) == 2
        finally:
            await engine.dispose()

    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(scenario())


def test_concurrent_mock_delivery_and_approval_transaction(database):
    from concurrent.futures import ThreadPoolExecutor
    from app.agents.contracts import ApprovalChoice
    from app.models import Medicine, Verdict
    from app.schemas.decision import Decision as DecisionPayload

    sessions = sessionmaker(database, expire_on_commit=False)
    repository = WorkflowRepository(sessions)
    with sessions.begin() as session:
        medicine = Medicine(name="DemoRelief", strength="500mg", batch_no="SYN001", household_id=uuid4())
        session.add(medicine)
        session.flush()
        medicine_id = str(medicine.id)
    notice = Notice(
        recall_number="SYN-CONCURRENCY-" + uuid4().hex, product_description="DemoRelief 500mg",
        code_info="Lot SYN001", report_date=date(2026, 1, 15), status="Ongoing",
        fetched_at=datetime.now(timezone.utc), raw_json={"synthetic": True},
        url="https://api.fda.gov/drug/enforcement.json?search=recall_number:SYN001",
    )
    saved = repository.save_notices([notice])[0]
    decision = repository.save_decision(uuid4(), medicine_id, saved["id"],
        DecisionPayload(verdict=Verdict.MATCH, reason="Synthetic exact match", evidence={}),
        repository.medicine(medicine_id), saved["data"])
    action = repository.propose(decision, "mock://synthetic")
    assert repository.propose(decision, "mock://synthetic").id == action.id
    with pytest.raises(ValueError, match="human approval"):
        repository.send_mock(action.id)
    with pytest.raises(ValueError, match="official notice URL and date"):
        repository.approve([ApprovalChoice(action_id=action.id, approved=True, message="Missing evidence")])
    assert repository.action(action.id).status == ActionStatus.PENDING_APPROVAL
    repository.approve([ApprovalChoice(action_id=action.id, approved=True)])
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(repository.send_mock, [action.id, action.id]))
    assert sorted(results) == [False, True]
    assert repository.send_mock(action.id) is False
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Action)
                              .where(Action.idempotency_key == action.idempotency_key)) == 1
    with sessions.begin() as session:
        medicine = session.get(Medicine, UUID(medicine_id))
        medicine.batch_no = "SYN002"
    changed = repository.save_decision(uuid4(), medicine_id, saved["id"],
        DecisionPayload(verdict=Verdict.MATCH, reason="Synthetic new assessment", evidence={}),
        repository.medicine(medicine_id), saved["data"])
    with pytest.raises(ValueError, match="previous details"):
        repository.propose(changed, "mock://synthetic")
