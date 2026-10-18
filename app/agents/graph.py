"""collect -> plan -> retrieve -> match -> confirm -> approve -> act -> verify."""

import json
import logging
from collections.abc import Mapping
from uuid import NAMESPACE_URL, uuid5

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from app.agents.contracts import AgentState, ApprovalReply, BatchReply, DecisionConfirmationReply
from app.agents.repository import WorkflowRepository
from app.models.enums import ActionStatus, Verdict, VerificationResult
from app.schemas.medicine import Medicine
from app.schemas.notice import Notice
from app.services.matcher import match
from app.services.verification import OpenFDAVerifier
from app.sources.base import SourceAdapter

logger = logging.getLogger(__name__)
NODE_ORDER = ("collect", "plan", "retrieve", "match", "confirm", "approve", "act", "verify")


def build_graph(
    repository: WorkflowRepository,
    sources: Mapping[str, SourceAdapter],
    verifier: OpenFDAVerifier,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:
    """Compile with injected dependencies; production supplies PostgresSaver."""
    if not sources:
        raise ValueError("At least one source is required.")

    def completed(state: AgentState, node: str) -> list[str]:
        return [*state.get("completed_nodes", []), node]

    def collect(state: AgentState) -> AgentState:
        medicine = repository.medicine(state["medicine_id"])
        error = None
        while not medicine.get("batch_no") or not medicine["batch_no"].strip():
            # No database transaction is held open while waiting for a human.
            reply = interrupt({
                "kind": "collect", "medicine_id": state["medicine_id"],
                "question": "What batch number is printed on your medicine package?",
                "response_example": {"batch_no": "SYN001"}, "error": error,
            })
            try:
                batch = BatchReply.model_validate(reply)
            except ValidationError:
                error = "Reply with a nonblank batch_no string."
                continue
            repository.collect_batch(state["medicine_id"], batch.batch_no)
            medicine = repository.medicine(state["medicine_id"])
        return {"medicine": medicine, "completed_nodes": completed(state, "collect")}

    def plan(state: AgentState) -> AgentState:
        return {
            "plan": {"sources": sorted(sources), "query": state["medicine"]["name"],
                     "match_scope": ["brand", "strength", "batch"],
                     "policy": "All three identifiers must match; missing evidence is UNCERTAIN."},
            "completed_nodes": completed(state, "plan"),
        }

    def retrieve(state: AgentState) -> AgentState:
        notices = []
        for source_name in state["plan"]["sources"]:
            # The default adapter saves pages itself; this also supports adapters
            # whose persistence is delegated to the graph. Upserts are replay-safe.
            fetched = sources[source_name].fetch_notices(state["plan"]["query"])
            notices.extend(repository.save_notices(fetched))
        unique = {notice["id"]: notice for notice in notices}
        return {"notices": list(unique.values()), "completed_nodes": completed(state, "retrieve")}

    def match_node(state: AgentState, config: RunnableConfig) -> AgentState:
        decision_ids, uncertain = [], 0
        medicine = Medicine.model_validate(state["medicine"])
        thread_id = str(config["configurable"]["thread_id"])
        for snapshot in state["notices"]:
            notice = Notice.model_validate(snapshot["data"])
            result = match(medicine, notice)  # The LLM cannot supply a verdict.
            stable_id = uuid5(NAMESPACE_URL, json.dumps(
                ["drug-monitor-decision", thread_id, state["medicine_id"], snapshot["id"]]
            ))
            saved = repository.save_decision(
                stable_id, state["medicine_id"], snapshot["id"], result,
                state["medicine"], snapshot["data"],
            )
            decision_ids.append(saved.id)
            uncertain += saved.verdict == Verdict.UNCERTAIN
        return {"decision_ids": decision_ids, "all_decision_ids": decision_ids,
                "uncertain_count": uncertain, "completed_nodes": completed(state, "match")}

    def confirm(state: AgentState) -> AgentState:
        decision_ids = list(state.get("decision_ids", []))
        if not decision_ids:
            return {"completed_nodes": completed(state, "confirm")}
        pending = []
        for decision_id in decision_ids:
            decision = repository.decision(decision_id)
            notice = decision.evidence.get("notice_snapshot") or {}
            pending.append({
                "decision_id": decision.id,
                "verdict": decision.verdict.value,
                "reason": decision.reason,
                "notice_url": notice.get("url"),
                "notice_date": notice.get("report_date"),
            })
        error = None
        while True:
            reply = interrupt({
                "kind": "confirm",
                "question": "Accept or reject each matcher decision before alerts are proposed.",
                "decisions": pending,
                "response_example": {"confirmations": [
                    {"decision_id": item["decision_id"], "accepted": True} for item in pending
                ]},
                "error": error,
            })
            try:
                answer = DecisionConfirmationReply.model_validate(reply)
                ids = [item.decision_id for item in answer.confirmations]
                expected = {item["decision_id"] for item in pending}
                if len(ids) != len(set(ids)) or set(ids) != expected:
                    raise ValueError("Accept or reject each displayed decision exactly once.")
                accepted = [item.decision_id for item in answer.confirmations if item.accepted]
                uncertain = sum(
                    1 for decision_id in accepted
                    if repository.decision(decision_id).verdict == Verdict.UNCERTAIN
                )
                return {
                    "decision_ids": accepted,
                    "all_decision_ids": decision_ids,
                    "uncertain_count": uncertain,
                    "completed_nodes": completed(state, "confirm"),
                }
            except (ValueError, ValidationError):
                error = "Invalid reply. Include every decision with a true/false accepted flag."

    def approve(state: AgentState) -> AgentState:
        actions, failures = {}, list(state.get("failures", []))
        for decision_id in state["decision_ids"]:
            decision = repository.decision(decision_id)
            if decision.verdict != Verdict.MATCH:
                continue
            try:
                action = repository.propose(decision, state["destination"])
                actions[action.id] = action
            except ValueError as exc:
                failures.append({"node": "approve", "decision_id": decision_id, "reason": str(exc)})
        pending = [action for action in actions.values() if action.status == ActionStatus.PENDING_APPROVAL]
        if pending:
            error = None
            while True:
                reply = interrupt({
                    "kind": "approve", "question": "Approve or reject each proposed mock alert.",
                    "alerts": [{"action_id": action.id, "idempotency_key": action.idempotency_key,
                                **action.proposed.model_dump()} for action in pending],
                    "response_example": {"approvals": [
                        {"action_id": action.id, "approved": True} for action in pending
                    ]},
                    "editing": "Optional destination/message fields replace the proposed version.",
                    "error": error,
                })
                try:
                    answer = ApprovalReply.model_validate(reply)
                    ids = [choice.action_id for choice in answer.approvals]
                    if len(ids) != len(set(ids)) or set(ids) != {action.id for action in pending}:
                        raise ValueError("Approve or reject each displayed action exactly once.")
                    repository.approve(answer.approvals)
                    break
                except (ValueError, ValidationError) as exc:
                    error = (str(exc) if isinstance(exc, ValueError) and not isinstance(exc, ValidationError)
                             else "Invalid reply. Include every action, a true/false approval, and retain the notice URL/date.")
        return {"action_ids": list(actions), "failures": failures,
                "completed_nodes": completed(state, "approve")}

    def act(state: AgentState) -> AgentState:
        sent, failures = [], list(state.get("failures", []))
        for action_id in state["action_ids"]:
            try:
                repository.send_mock(action_id)
                if repository.action(action_id).status == ActionStatus.SENT:
                    sent.append(action_id)
            except ValueError as exc:
                failures.append({"node": "act", "action_id": action_id, "reason": str(exc)})
        return {"sent_action_ids": sent, "failures": failures,
                "completed_nodes": completed(state, "act")}

    def verify(state: AgentState) -> AgentState:
        results, failures = [], list(state.get("failures", []))
        for action_id in state["sent_action_ids"]:
            action = repository.action(action_id)
            decision = repository.decision(action.decision_id)
            outcome = verifier.verify(
                Medicine.model_validate(decision.evidence["medicine_snapshot"]),
                Notice.model_validate(decision.evidence["notice_snapshot"]),
            )
            identifier = repository.save_verification(action_id, outcome)
            results.append({
                "id": identifier, "action_id": action_id, "url_opened": outcome.url_opened,
                "batch_found": outcome.batch_found,
                "notice_date": outcome.notice_date.isoformat() if outcome.notice_date else None,
                "result": outcome.result.value, "reason": outcome.reason,
            })
            if outcome.result != VerificationResult.CONFIRMED:
                failure = {"node": "verify", "action_id": action_id, "reason": outcome.reason}
                failures.append(failure)
                logger.error("VERIFICATION_FAILED %s", json.dumps(failure))
        outcome = "FAILED" if failures else (
            "NEEDS_REVIEW" if state.get("uncertain_count") else (
                "VERIFIED" if results else "NO_ALERTS"
            )
        )
        return {"verification_results": results, "failures": failures, "outcome": outcome,
                "completed_nodes": completed(state, "verify")}

    graph = StateGraph(AgentState)
    for name, node in zip(NODE_ORDER, (collect, plan, retrieve, match_node, confirm, approve, act, verify)):
        graph.add_node(name, node)
    graph.add_edge(START, "collect")
    for before, after in zip(NODE_ORDER, NODE_ORDER[1:]):
        graph.add_edge(before, after)
    graph.add_edge("verify", END)
    return graph.compile(checkpointer=checkpointer)
