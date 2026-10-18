"""Shared isolated PostgreSQL fixture; never touches application schemas."""
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import Engine, create_engine
from sqlalchemy.schema import CreateSchema, DropSchema

import app.models  # Registers all tables.
from app.agents.contracts import AlertEnvelope, StoredAction, StoredDecision, VerificationOutcome
from app.agents.graph import build_graph
from app.agents.repository import WorkflowRepository
from app.db.base import Base
from app.models.enums import ActionStatus, Verdict, VerificationResult
from app.schemas.notice import Notice
from app.services.verification import OpenFDAVerifier


@pytest.fixture(scope="module")
def database() -> Iterator[Engine]:
    """Create and remove only a fresh schema, never public application tables."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to enable real PostgreSQL checks")
    schema = "test_drug_monitor_" + uuid4().hex
    admin_engine = create_engine(url)
    # This schema name is generated locally and contains only safe identifiers.
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
            Base.metadata.create_all(connection)  # Repeated bootstrap is harmless.
        yield engine
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin_engine.dispose()


@pytest.fixture
def harness():
    """Mocks isolate graph control flow; PostgreSQL behavior has integration tests."""
    medicine_id, notice_id, action_id = (str(uuid4()) for _ in range(3))
    medicine = {"name": "DemoRelief", "strength": "500mg", "batch_no": "SYN001"}
    notice = Notice(
        recall_number="SYN-AGENT-001", product_description="DemoRelief 500 mg tablets",
        code_info="Lots SYN001", status="Ongoing", report_date=date(2026, 1, 15),
        fetched_at=datetime.now(timezone.utc), raw_json={"synthetic": True},
        url="https://api.fda.gov/drug/enforcement.json?search=recall_number:SYN-AGENT-001",
    )
    repository = Mock(spec=WorkflowRepository)
    repository.medicine.side_effect = lambda _: medicine.copy()
    repository.collect_batch.side_effect = lambda _, batch: medicine.update(batch_no=batch)
    repository.save_notices.return_value = [{"id": notice_id, "data": notice.model_dump(mode="json")}]
    decisions, actions = {}, {}

    def save(identifier, med_id, source_id, result, med_snapshot, notice_snapshot):
        saved = StoredDecision(str(identifier), med_id, source_id, result.verdict, result.reason,
                               result.evidence | {"medicine_snapshot": med_snapshot,
                                                  "notice_snapshot": notice_snapshot})
        decisions[saved.id] = saved
        return saved

    repository.save_decision.side_effect = save
    repository.decision.side_effect = decisions.__getitem__

    def propose(decision, destination):
        actions.setdefault(action_id, StoredAction(
            action_id, decision.id, ActionStatus.PENDING_APPROVAL, "synthetic-key",
            AlertEnvelope(destination=destination, message=f"Notice 2026-01-15: {notice.url}"), None,
        ))
        return actions[action_id]

    def approve(choices):
        for choice in choices:
            current = actions[choice.action_id]
            approved = AlertEnvelope(destination=choice.destination or current.proposed.destination,
                                     message=choice.message or current.proposed.message)
            actions[choice.action_id] = replace(
                current, status=ActionStatus.APPROVED if choice.approved else ActionStatus.REJECTED,
                approved=approved if choice.approved else None,
            )

    def send(identifier):
        current = actions[identifier]
        if current.status in (ActionStatus.SENT, ActionStatus.REJECTED):
            return False
        assert current.status == ActionStatus.APPROVED
        actions[identifier] = replace(current, status=ActionStatus.SENT)
        return True

    repository.propose.side_effect = propose
    repository.approve.side_effect = approve
    repository.send_mock.side_effect = send
    repository.action.side_effect = actions.__getitem__
    repository.save_verification.return_value = str(uuid4())
    adapter = Mock()
    adapter.fetch_notices.return_value = [notice]
    verifier = Mock(spec=OpenFDAVerifier)
    verifier.verify.return_value = VerificationOutcome(
        str(notice.url), True, notice.report_date, VerificationResult.CONFIRMED, "Confirmed synthetic notice",
    )
    saver = InMemorySaver()
    graph = build_graph(repository, {"openFDA": adapter}, verifier, saver)
    return {
        "repository": repository, "adapter": adapter, "verifier": verifier, "saver": saver,
        "graph": graph, "medicine": medicine, "actions": actions, "action_id": action_id,
        "input": {"medicine_id": medicine_id, "destination": "mock://synthetic"},
        "config": {"configurable": {"thread_id": "synthetic-thread"}},
    }

