"""Pure presentation rules: unknown, rejected, and failed checks are never 'No match'."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from app.models.enums import ActionStatus, Verdict
from app.schemas.api import ActionResponse, MedicineResponse, RunResponse

STEPS = ("collect", "plan", "retrieve", "match", "confirm", "approve", "act", "verify")
STEP_LABELS = ("Collect", "Plan", "Retrieve", "Match", "Confirm", "Approve", "Act", "Verify")


def official_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname == "api.fda.gov"
                and parsed.path == "/drug/enforcement.json" and parsed.port in (None, 443)
                and not parsed.username and not parsed.password):
            return value
    except ValueError:
        pass
    return None


def medicine_label(medicine: MedicineResponse) -> str:
    return f"{medicine.name} · {medicine.strength} · Batch {medicine.batch_no or 'missing'}"


def snapshot_label(run: RunResponse, medicine_id: UUID, inventory: list[MedicineResponse]) -> str:
    state = run.state.get(str(medicine_id), {})
    snapshot = state.get("medicine")
    if snapshot:
        return f"{snapshot['name']} · {snapshot['strength']} · Batch {snapshot.get('batch_no') or 'missing'}"
    return next((medicine_label(item) for item in inventory if item.id == medicine_id), str(medicine_id))


def step_rows(run: RunResponse, medicine_id: UUID) -> list[dict[str, str]]:
    state = run.state.get(str(medicine_id), {})
    completed = state.get("completed_nodes", [])
    failed = {item.get("node") for item in state.get("failures", [])}
    pending = run.pending_interrupt
    current = run.cursor < len(run.medicine_ids) and run.medicine_ids[run.cursor] == medicine_id
    next_step = next((step for step in STEPS if step not in completed), None)
    rows = []
    for step, label in zip(STEPS, STEP_LABELS):
        status = "Done" if step in completed else "Not started"
        if current and step == next_step and run.status == "RUNNING":
            status = "Running"
        if current and step == next_step and run.status == "QUEUED":
            status = "Queued"
        if pending and pending.medicine_id == medicine_id and pending.value.get("kind") == step:
            if step == "collect":
                status = "Needs batch"
            elif step == "confirm":
                status = "Needs confirmation"
            else:
                status = "Needs approval"
        if step in failed or (current and run.error and step == next_step):
            status = "Failed"
        rows.append({"Step": label, "Status": status})
    return rows


@dataclass(frozen=True)
class MedicineResult:
    label: str
    tone: str
    detail: str
    uncertain: bool
    actions: list[ActionResponse]


def medicine_result(run: RunResponse, medicine_id: UUID) -> MedicineResult:
    state: dict[str, Any] = run.state.get(str(medicine_id), {})
    decisions = [item for item in run.decisions if item.medicine_id == medicine_id]
    actions = [item for item in run.actions if item.medicine_id == medicine_id]
    uncertain = any(item.verdict == Verdict.UNCERTAIN for item in decisions)
    sent = [item for item in actions if item.status == ActionStatus.SENT]
    failed = bool(state.get("failures")) or any(
        item.get("result") != "CONFIRMED" for item in state.get("verification_results", [])
    )
    if sent:
        detail = "Mock alert recorded."
        if failed:
            detail += " Verification failed or remains uncertain; review the evidence below."
        elif "verify" not in state.get("completed_nodes", []):
            detail += " Independent verification is pending."
        else:
            detail += " Official notice verified."
        return MedicineResult("Alert with notice link", "error" if failed else "success", detail, uncertain, sent)
    current = run.cursor < len(run.medicine_ids) and run.medicine_ids[run.cursor] == medicine_id
    if failed or (run.error and current):
        return MedicineResult("Check failed", "error", "The check did not finish reliably. Review the recorded failure.", uncertain, actions)
    if any(item.verdict == Verdict.MATCH for item in decisions):
        rejected = actions and all(item.status == ActionStatus.REJECTED for item in actions)
        return MedicineResult("Alert rejected" if rejected else "Match — awaiting alert",
                              "warning", "No alert was sent." if rejected else "Human approval is required before sending.",
                              uncertain, actions)
    if uncertain:
        return MedicineResult("UNCERTAIN", "warning", "Evidence is missing or unparseable. Review is required.", True, actions)
    if "verify" in state.get("completed_nodes", []):
        detail = "No matching batch in the retrieved notices." if decisions else "No notices returned for this search."
        return MedicineResult("No match", "info", detail, False, actions)
    if run.pending_interrupt and run.pending_interrupt.medicine_id == medicine_id:
        kind = run.pending_interrupt.value.get("kind")
        if kind == "confirm":
            return MedicineResult(
                "Awaiting verdict review", "warning",
                "Accept or reject each matcher decision in Approvals to continue.", uncertain, actions,
            )
        if kind == "approve":
            return MedicineResult(
                "Match — awaiting alert", "warning",
                "Human approval is required before sending.", uncertain, actions,
            )
        return MedicineResult("Awaiting batch", "warning", "Provide the batch number in Approvals to continue.", False, actions)
    return MedicineResult("Not completed", "info", "This medicine has not finished its check.", False, actions)
