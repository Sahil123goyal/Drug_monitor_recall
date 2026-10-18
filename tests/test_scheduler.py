"""Synthetic tests for daily scheduling, source events and reviewed updates."""

import asyncio
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

from app.agents.contracts import ApprovalChoice
from app.agents.graph import build_graph
from app.agents.repository import WorkflowRepository, action_key
from app.core.config import OpenFDASettings, SchedulerSettings
from app.models import ActionStatus, Medicine, Notice as NoticeRow, Run
from app.schemas.notice import Notice
from app.services.notice_changes import notice_content, notice_fingerprint
from app.services.notice_store import SQLAlchemyNoticeStore
from app.services.scheduler import DailyMonitor, daily_run_id, enqueue_daily_checks
from app.sources import OpenFDAAdapter, SourceError


def run_async(coro):
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(coro)


def notice() -> Notice:
    return Notice(recall_number="SYN-DAILY-" + uuid4().hex,
        product_description="DemoRelief 500mg tablets", code_info="Lots SYN001, SYN002",
        status="Ongoing", report_date=date(2026, 1, 15), fetched_at=datetime.now(timezone.utc),
        url="https://api.fda.gov/drug/enforcement.json?search=recall_number:SYN001",
        raw_json={"synthetic": True})


def test_revision_ignores_batch_order_but_preserves_exact_tokens():
    first = notice_fingerprint("Ongoing", "Lots SYN001, SYN002")
    assert first == notice_fingerprint("Ongoing", "Lots SYN002, SYN001, SYN001")
    assert first != notice_fingerprint("Terminated", "Lots SYN001, SYN002")
    assert first != notice_fingerprint("Ongoing", "Lots SYN001, SYN003")
    assert notice_fingerprint("Ongoing", "Lot 0001") != notice_fingerprint("Ongoing", "Lot 001")
    assert notice_content("Ongoing", "all lots")["batches"] is None
    assert action_key("m", "n", "Ongoing", fingerprint=first) != action_key(
        "m", "n", "Ongoing", fingerprint=notice_fingerprint("Ongoing", "Lot SYN001"))


def test_daily_ids_deduplicate_only_same_day_same_medicine():
    med = uuid4()
    assert daily_run_id(date(2026, 1, 1), med) == daily_run_id(date(2026, 1, 1), med)
    assert daily_run_id(date(2026, 1, 1), med) != daily_run_id(date(2026, 1, 2), med)
    assert daily_run_id(date(2026, 1, 1), med) != daily_run_id(date(2026, 1, 1), uuid4())


def test_timer_configuration_and_disabled_mode():
    async def scenario():
        settings = SchedulerSettings(_env_file=None, scheduler_hour=23, scheduler_minute=59)
        monitor = DailyMonitor(Mock(), settings)
        monitor.start()
        job = monitor.scheduler.get_job("daily-medicine-recheck")
        assert job.max_instances == 1 and job.coalesce
        assert str(job.trigger.timezone) == "Asia/Kolkata"
        assert "hour='23'" in str(job.trigger) and "minute='59'" in str(job.trigger)
        await monitor.stop()
        disabled = DailyMonitor(Mock(), settings.model_copy(update={"scheduler_enabled": False}))
        disabled.start()
        assert not disabled.scheduler.running
        await disabled.stop()
    run_async(scenario())


def test_startup_catchup_executes_through_apscheduler(monkeypatch):
    async def scenario():
        fired = asyncio.Event()
        async def enqueue(engine, settings):
            fired.set()
            return 0
        monkeypatch.setattr("app.services.scheduler.enqueue_daily_checks", enqueue)
        monitor = DailyMonitor(Mock(), SchedulerSettings(_env_file=None, scheduler_hour=0, scheduler_minute=0))
        try:
            monitor.start()
            await asyncio.wait_for(fired.wait(), timeout=3)
        finally:
            await monitor.stop()
    run_async(scenario())


def test_queue_failure_has_its_own_event(monkeypatch, caplog):
    async def fail(engine, settings):
        raise RuntimeError("synthetic sensitive database detail")
    monkeypatch.setattr("app.services.scheduler.enqueue_daily_checks", fail)
    with caplog.at_level(logging.INFO):
        run_async(DailyMonitor(Mock(), SchedulerSettings(_env_file=None))._enqueue())
    assert [record.event for record in caplog.records if hasattr(record, "event")] == ["DAILY_QUEUE_FAILED"]
    assert "sensitive" not in caplog.text


def test_network_failure_logs_separately_without_saving(caplog):
    def timeout(request):
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    store, sleep = Mock(), Mock()
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client, caplog.at_level(logging.INFO):
        adapter = OpenFDAAdapter(client=client, store=store, sleep=sleep,
            settings=OpenFDASettings(_env_file=None, openfda_max_retries=2, openfda_api_key=""))
        with pytest.raises(SourceError) as error:
            adapter.fetch_notices("DemoRelief")
    assert error.value.category == "network_error"
    assert sleep.call_count == 2
    store.save.assert_not_called()
    assert [record.event for record in caplog.records if hasattr(record, "event")] == ["FETCH_FAILED"]


@pytest.mark.integration
def test_daily_queue_all_medicines_once_per_local_day(database):
    with database.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    sessions = sessionmaker(database, expire_on_commit=False)
    with sessions.begin() as session:
        medicines = [Medicine(name="Synthetic", strength="500mg", batch_no=batch, household_id=uuid4())
                     for batch in ("SYN001", None)]
        session.add_all(medicines)
    async def scenario():
        engine = create_async_engine(database.url, connect_args={"options": f"-csearch_path={schema}"})
        settings = SchedulerSettings(_env_file=None)
        # UTC date Jan 1 is local date Jan 2: assert local calendar scheduling.
        now = datetime(2026, 1, 1, 20, tzinfo=timezone.utc)
        try:
            results = await asyncio.gather(*(enqueue_daily_checks(engine, settings, now=now) for _ in range(2)))
            assert sum(results) == 2
            assert await enqueue_daily_checks(engine, settings, now=now) == 0
            assert await enqueue_daily_checks(engine, settings, now=now + timedelta(days=1)) == 2
        finally:
            await engine.dispose()
    run_async(scenario())
    with sessions() as session:
        for med in medicines:
            row = session.get(Run, daily_run_id(date(2026, 1, 2), med.id))
            assert row.status == "QUEUED" and row.pending_resume is None
            assert row.medicine_ids == [str(med.id)] and row.household_id == med.household_id


@pytest.mark.integration
def test_committed_changes_log_once_and_stale_fetches_do_not_change_baseline(database, caplog):
    store = SQLAlchemyNoticeStore(sessionmaker(database))
    first = notice()
    with caplog.at_level(logging.INFO):
        store.save([first])
        store.save([first.model_copy(update={"code_info": "Lots SYN002, SYN001"})])
        assert not [record for record in caplog.records if getattr(record, "event", "") == "NOTICE_CHANGED"]
        second = first.model_copy(update={"code_info": "Lots SYN001, SYN003",
                                         "fetched_at": first.fetched_at + timedelta(seconds=1)})
        store.save([second])
        store.save([second])
        third = second.model_copy(update={"status": "Terminated",
                                          "fetched_at": second.fetched_at + timedelta(seconds=1)})
        store.save([third])
        store.save([first])
    events = [record for record in caplog.records if getattr(record, "event", "") == "NOTICE_CHANGED"]
    assert len(events) == 2
    assert events[0].notice_change["after"]["batches"] == ["SYN001", "SYN003"]
    assert events[1].notice_change["after"]["status"] == "Terminated"
    with sessionmaker(database)() as session:
        saved = session.scalar(select(NoticeRow).where(NoticeRow.notice_id == first.recall_number))
        assert saved.status == "Terminated" and saved.fetched_at == third.fetched_at


@pytest.mark.integration
@pytest.mark.parametrize("field,value", [("status", "Terminated"), ("code_info", "Lots SYN001, SYN003")])
def test_changed_notice_requires_fresh_human_approval(database, field, value):
    sessions = sessionmaker(database, expire_on_commit=False)
    repository = WorkflowRepository(sessions)
    with sessions.begin() as session:
        med = Medicine(name="DemoRelief", strength="500mg", batch_no="SYN001", household_id=uuid4())
        session.add(med)
    source, verifier = Mock(), Mock()
    first = notice()
    source.fetch_notices.return_value = [first]
    graph = build_graph(repository, {"openFDA": source}, verifier, InMemorySaver())

    def proposal():
        config = {"configurable": {"thread_id": str(uuid4())}}
        state = graph.invoke({"medicine_id": str(med.id), "destination": "mock://synthetic"}, config)
        assert state["__interrupt__"][0].value["kind"] == "confirm"
        state = graph.invoke(Command(resume={"confirmations": [
            {"decision_id": identifier, "accepted": True} for identifier in state["decision_ids"]
        ]}), config)  # Simulated human input, only inside this isolated test.
        question = state["__interrupt__"][0].value
        assert question["kind"] == "approve"
        return repository.action(question["alerts"][0]["action_id"])

    old = proposal()
    assert proposal().id == old.id
    # Exercise stale already-approved delivery as well as stale pending approval.
    repository.approve([ApprovalChoice(action_id=old.id, approved=True)])
    changed = first.model_copy(update={field: value, "fetched_at": first.fetched_at + timedelta(seconds=1)})
    source.fetch_notices.return_value = [changed]
    update = proposal()
    assert update.id != old.id and update.status == ActionStatus.PENDING_APPROVAL
    assert "MOCK recall update:" in update.proposed.message
    assert changed.report_date.isoformat() in update.proposed.message
    assert str(changed.url) in update.proposed.message
    assert proposal().id == update.id
    with pytest.raises(ValueError, match="Notice changed"):
        repository.send_mock(old.id)
    with pytest.raises(ValueError, match="human approval"):
        repository.send_mock(update.id)
    verifier.verify.assert_not_called()
    repository.approve([ApprovalChoice(action_id=update.id, approved=True)])
    assert repository.send_mock(update.id)
    assert not repository.send_mock(update.id)
    source.fetch_notices.return_value = [first.model_copy(update={
        "fetched_at": changed.fetched_at + timedelta(seconds=1)})]
    reverted = proposal()
    assert reverted.id not in {old.id, update.id}
    assert reverted.status == ActionStatus.PENDING_APPROVAL
    assert proposal().id == reverted.id


@pytest.mark.integration
def test_stale_pending_approval_rejected_and_legacy_action_reused(database):
    sessions = sessionmaker(database, expire_on_commit=False)
    repository = WorkflowRepository(sessions)
    with sessions.begin() as session:
        med = Medicine(name="DemoRelief", strength="500mg", batch_no="SYN001", household_id=uuid4())
        session.add(med)
    from app.models import Action
    from app.services.matcher import match
    from app.schemas.medicine import Medicine as MedicineInput
    first = notice()
    saved = repository.save_notices([first])[0]
    decision = repository.save_decision(uuid4(), str(med.id), saved["id"],
        match(MedicineInput.model_validate(med), first), repository.medicine(str(med.id)), saved["data"])
    action = repository.propose(decision, "mock://synthetic")
    with sessions.begin() as session:
        row = session.get(Action, action.id)
        row.idempotency_key = action_key(str(med.id), saved["id"], first.status)
    assert repository.propose(decision, "mock://synthetic").id == action.id
    repository.save_notices([first.model_copy(update={"code_info": "Lot SYN002",
                                                     "fetched_at": first.fetched_at + timedelta(seconds=1)})])
    with pytest.raises(ValueError, match="Notice changed"):
        repository.approve([ApprovalChoice(action_id=action.id, approved=True)])
    assert repository.action(action.id).status == ActionStatus.PENDING_APPROVAL
