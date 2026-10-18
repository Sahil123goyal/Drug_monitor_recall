"""Run the real graph/repository in rollback-only transactions, retaining reports.

No human approval is fabricated. TC1 and TC3 assert the proposed alert at the
approval interrupt. All action/medicine/fixture writes are rolled back; only
test_runs is committed. Each invocation appends a fresh four-case audit group.
"""

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.graph import build_graph
from app.agents.repository import WorkflowRepository
from app.models import Action, ActionStatus, Decision, Medicine, TestRun
from app.schemas.notice import Notice
from app.services.official_cases import DATA, OFFICIAL_CASES, SUITE, OfficialCase, SourceMode, load_source


class ApprovalBoundaryRepository(WorkflowRepository):
    """The suite cannot approve or send, even if a future graph regresses."""
    def approve(self, choices: Any) -> None:
        raise RuntimeError("Acceptance suite must not fabricate human approval.")

    def send_mock(self, identifier: str) -> bool:
        raise RuntimeError("Acceptance suite must stop before sending alerts.")


class UnusedVerifier:
    def verify(self, medicine: Any, notice: Any) -> Any:
        raise RuntimeError("Pre-approval acceptance cases must not enter delivery verification.")


@dataclass
class ReplaySource:
    notice: Notice
    calls: int = 0

    def fetch_notices(self, query: str) -> list[Notice]:
        self.calls += 1
        return [self.notice]


@contextmanager
def sandbox(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Repository commits only release savepoints, never commit this outer transaction."""
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            yield sessionmaker(connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
        finally:
            outer.rollback()


@dataclass
class Observation:
    state: dict[str, Any]
    question: dict[str, Any] | None
    decisions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    retrieval_calls: int
    completed: bool
    details: dict[str, Any] = field(default_factory=dict)


def observe(
    sessions: sessionmaker[Session], medicine_id: UUID, source: ReplaySource,
) -> Observation:
    graph = build_graph(ApprovalBoundaryRepository(sessions), {"acceptance_source": source},
                        UnusedVerifier(), InMemorySaver())
    config = {"configurable": {"thread_id": "official-test-" + str(uuid4())}}
    graph.invoke({"medicine_id": str(medicine_id), "destination": "mock://official-acceptance-suite"}, config)
    # Accept matcher confirmations so the suite can still stop at the alert-approval boundary.
    # This does not fabricate alert approval or delivery.
    while True:
        snapshot = graph.get_state(config)
        question = snapshot.interrupts[0].value if snapshot.interrupts else None
        if not question or question.get("kind") != "confirm":
            break
        graph.invoke(Command(resume={"confirmations": [
            {"decision_id": item["decision_id"], "accepted": True}
            for item in question.get("decisions", [])
        ]}), config)
    snapshot = graph.get_state(config)
    question = snapshot.interrupts[0].value if snapshot.interrupts else None
    with sessions() as session:
        identifiers = list({
            UUID(value) for value in (
                *snapshot.values.get("all_decision_ids", []),
                *snapshot.values.get("decision_ids", []),
            )
        })
        rows = (
            session.scalars(select(Decision).where(Decision.id.in_(identifiers))).all()
            if identifiers else []
        )
        decisions = [{"id": str(row.id), "verdict": row.verdict.value, "reason": row.reason,
                      "evidence": row.evidence} for row in rows]
        decision_ids = [UUID(value["id"]) for value in decisions]
        rows = (
            session.scalars(select(Action).where(Action.decision_id.in_(decision_ids))).all()
            if decision_ids else []
        )
        actions = []
        for row in rows:
            decision = session.get(Decision, row.decision_id)
            assert decision is not None
            actions.append({"id": str(row.id), "status": row.status.value,
                            "idempotency_key": row.idempotency_key,
                            "proposed": json.loads(row.proposed_message),
                            "notice": decision.evidence["notice_snapshot"]})
    return Observation(dict(snapshot.values), question, decisions, actions, source.calls, not snapshot.next)


def alert_facts(observation: Observation) -> dict[str, Any]:
    question = observation.question
    pending = bool(question and question.get("kind") == "approve")
    alerts = question.get("alerts", []) if pending else []
    current = next((item for item in observation.actions if alerts and item["id"] == alerts[0]["action_id"]), None)
    notice = current["notice"] if current else {}
    message = current["proposed"]["message"] if current else ""
    # Derive fields from the actual stored proposal, not from expected values.
    date = notice.get("report_date")
    link = notice.get("url")
    status = notice.get("status")
    return {
        "proposed_alerts": len(alerts), "approval_required": pending,
        "notice_date": date if date and date in message else None,
        "official_link_present": bool(link and link in message),
        "current_status": status if status and f"status {status}" in message else None,
        "sent_alerts": sum(item["status"] == ActionStatus.SENT.value for item in observation.actions),
        "verdicts": sorted(item["verdict"] for item in observation.decisions),
        "completed": observation.completed,
    }


def execute_case(sessions: sessionmaker[Session], case: OfficialCase, notice: Notice) -> Observation:
    with sessions.begin() as session:
        medicine = Medicine(**case.inputs()["medicine"], household_id=uuid4())
        session.add(medicine)
        session.flush()
        medicine_id = medicine.id
    if case.name != "TC3":
        return observe(sessions, medicine_id, ReplaySource(notice))
    fixture = json.loads((DATA / "terminated_transition.json").read_text(encoding="utf-8"))
    if (fixture.get("kind") != "synthetic_status_transition" or fixture["before"] != {"status": "Ongoing"}
            or fixture["after"] != {"status": "Terminated"}):
        raise ValueError("Invalid TC3 status transition fixture.")
    # The fake status can never overwrite a live openFDA row, even inside the sandbox.
    before = notice.model_copy(update={"source": "fixture:openFDA", "status": "Ongoing",
        "raw_json": {**notice.raw_json, "status": "Ongoing", "_fixture": fixture["label"]}})
    after = before.model_copy(update={"status": "Terminated", "fetched_at": before.fetched_at + timedelta(seconds=1),
        "raw_json": {**before.raw_json, "status": "Terminated"}})
    baseline = observe(sessions, medicine_id, ReplaySource(before))
    result = observe(sessions, medicine_id, ReplaySource(after))
    preserved = False
    if baseline.actions:
        with sessions() as session:
            original = session.get(Action, UUID(baseline.actions[0]["id"]))
            preserved = bool(original and json.loads(original.proposed_message) == baseline.actions[0]["proposed"])
    result.details = {"fixture": fixture, "baseline": baseline.__dict__,
                      "prior_alert_preserved": preserved,
                      "update_semantics": "New pending action for Terminated; earlier alert version retained unchanged."}
    return result


def actual_result(case: OfficialCase, observation: Observation) -> dict[str, Any]:
    facts = alert_facts(observation)
    if case.name == "TC1":
        facts["outcome"] = "DATED_ALERT_PROPOSED" if (facts["proposed_alerts"] and facts["notice_date"]
                                                     and facts["official_link_present"]) else "NO_DATED_ALERT"
    elif case.name == "TC2":
        facts["outcome"] = "NO_ALERT" if not facts["proposed_alerts"] and not facts["sent_alerts"] else "ALERT_PRESENT"
    elif case.name == "TC3":
        baseline = observation.details["baseline"]
        old = baseline["actions"][0] if baseline["actions"] else None
        new = observation.actions[0] if observation.actions else None
        facts.update({"previous_status": old["notice"]["status"] if old else None,
                      "message_changed": bool(old and new and old["proposed"]["message"] != new["proposed"]["message"]),
                      "new_action_key": bool(old and new and old["idempotency_key"] != new["idempotency_key"]),
                      "prior_alert_preserved": observation.details["prior_alert_preserved"]})
        facts["outcome"] = "ALERT_UPDATE_PROPOSED" if (facts["message_changed"] and facts["new_action_key"]
            and facts["current_status"] == "Terminated" and facts["approval_required"]) else "ALERT_NOT_UPDATED"
    else:
        facts["interrupt_kind"] = observation.question.get("kind") if observation.question else None
        facts["retrieval_calls"] = observation.retrieval_calls
        facts["outcome"] = "USER_ASKED" if facts["interrupt_kind"] == "collect" else "USER_NOT_ASKED"
    return facts


def compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Additional observations are allowed; every expected field must match exactly."""
    return [f"{key}: expected {value!r}; actual {actual.get(key, '<missing>')!r}"
            for key, value in expected.items()
            if key not in actual or type(actual[key]) is not type(value) or actual[key] != value]


def run_official_suite(engine: Engine, *, source: SourceMode = "live") -> list[dict[str, Any]]:
    """Append each case separately, preserving earlier reports if a later case fails."""
    run_id, executed_at = str(uuid4()), datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    sources: dict[str, tuple[Notice, dict[str, Any]] | Exception] = {}
    for case in OFFICIAL_CASES:
        expected = case.expected()
        inputs = case.inputs() | {"source_mode": "fixture" if case.name == "TC3" else source,
                                  "alert_stage": "proposed; human approval required"}
        evidence: dict[str, Any] = {"suite": SUITE, "suite_run_id": run_id, "executed_at": executed_at,
                                   "fixture": case.name == "TC3", "approval_simulated": False,
                                   "delivery_scope": "No approvals or sends; acceptance boundary is the human interrupt."}
        try:
            mode = "snapshot" if case.name in {"TC3", "TC4"} else source
            if mode not in sources:
                try:
                    sources[mode] = load_source(mode)
                except Exception as exc:
                    sources[mode] = exc
            loaded = sources[mode]
            if isinstance(loaded, Exception):
                raise loaded
            notice, provenance = loaded
            evidence.update({"source": provenance, "notice": notice.model_dump(mode="json")})
            if case.name == "TC4":
                inputs["source_mode"] = "snapshot (not queried: batch collection must interrupt first)"
            with sandbox(engine) as sessions:
                observation = execute_case(sessions, case, notice)
                actual = actual_result(case, observation)
                evidence["observation"] = observation.__dict__
            differences = compare(expected, actual)
        except Exception as exc:
            actual = {"outcome": "ERROR", "error_type": type(exc).__name__}
            differences = [f"Case could not produce its expected result ({type(exc).__name__}); inspect configuration/source availability."]
        evidence["differences"] = differences
        evidence["explanation"] = "; ".join(differences) if differences else "All expected fields matched the observed graph behavior."
        record = {"id": uuid4(), "tc_name": case.name, "input": inputs, "expected": expected,
                  "actual": actual, "evidence": evidence, "passed": not differences}
        # This independent transaction is the only committed write in the suite.
        with Session(engine) as session, session.begin():
            session.add(TestRun(**record))
        records.append(record)
    return records
