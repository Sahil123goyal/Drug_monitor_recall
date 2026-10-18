"""Transactional audit writes and mock-delivery deduplication for graph nodes."""

import logging
import hashlib
from collections.abc import Sequence
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.agents.contracts import AlertEnvelope, ApprovalChoice, StoredAction, StoredDecision, VerificationOutcome
from app.models import Action, ActionStatus, Decision, Medicine, Notice, Verification, Verdict
from app.schemas.decision import Decision as MatchDecision
from app.schemas.medicine import Medicine as MedicineInput
from app.schemas.notice import Notice as NoticeInput
from app.services.notice_store import SQLAlchemyNoticeStore
from app.services.notice_changes import notice_fingerprint
from app.services.verification import validate_official_url

logger = logging.getLogger(__name__)


def action_key(
    medicine_id: str, notice_id: str, source_status: str | None, *, fingerprint: str | None = None,
) -> str:
    """Include notice content so batch-only updates require fresh approval."""
    key = f"{medicine_id}:{notice_id}:{source_status or 'UNKNOWN'}"
    if fingerprint:
        key += f":{fingerprint}"
    if len(key) > 255:
        raise ValueError("Source status is too long for the action idempotency key.")
    return key


def _action(row: Action) -> StoredAction:
    return StoredAction(
        str(row.id), str(row.decision_id), row.status, row.idempotency_key,
        AlertEnvelope.model_validate_json(row.proposed_message),
        AlertEnvelope.model_validate_json(row.approved_message) if row.approved_message else None,
    )


def _decision(row: Decision) -> StoredDecision:
    return StoredDecision(str(row.id), str(row.medicine_id), str(row.notice_id),
                          row.verdict, row.reason, row.evidence)


def validate_approved_message(message: AlertEnvelope, decision: StoredDecision) -> None:
    snapshot = NoticeInput.model_validate(decision.evidence["notice_snapshot"])
    if snapshot.report_date is None:
        raise ValueError("An alert requires an official notice date.")
    validate_official_url(str(snapshot.url))
    if str(snapshot.url) not in message.message or snapshot.report_date.isoformat() not in message.message:
        raise ValueError("The approved message must retain the official notice URL and date.")


def require_current_notice(session: Session, decision: StoredDecision) -> None:
    """A previously approved snapshot cannot authorize a newer regulator version."""
    snapshot = NoticeInput.model_validate(decision.evidence["notice_snapshot"])
    current = session.get(Notice, UUID(decision.notice_id), with_for_update=True)
    if current is None or notice_fingerprint(current.status, current.code_info) != notice_fingerprint(
        snapshot.status, snapshot.code_info
    ):
        raise ValueError("Notice changed after matching; reject this stale alert and review the new check.")


class WorkflowRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def medicine(self, medicine_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(Medicine, UUID(medicine_id))
            if row is None:
                raise ValueError("Medicine does not exist.")
            return MedicineInput.model_validate(row).model_dump(mode="json")

    def collect_batch(self, medicine_id: str, batch_no: str) -> None:
        with self.sessions.begin() as session:
            row = session.scalar(select(Medicine).where(Medicine.id == UUID(medicine_id)).with_for_update())
            if row is None:
                raise ValueError("Medicine does not exist.")
            if row.batch_no and row.batch_no.strip() != batch_no:
                raise ValueError("The medicine batch changed; start a new assessment.")
            row.batch_no = batch_no

    def save_notices(self, notices: Sequence[NoticeInput]) -> list[dict[str, Any]]:
        SQLAlchemyNoticeStore(self.sessions).save(notices)
        snapshots = []
        with self.sessions() as session:
            for notice in notices:
                identifier = session.scalar(select(Notice.id).where(
                    Notice.source == notice.source, Notice.notice_id == notice.recall_number
                ))
                if identifier is None:
                    raise RuntimeError("Fetched notice was not persisted.")
                snapshots.append({"id": str(identifier), "data": notice.model_dump(mode="json")})
        return snapshots

    def save_decision(
        self, decision_id: UUID, medicine_id: str, notice_id: str,
        result: MatchDecision, medicine: dict[str, Any], notice: dict[str, Any],
    ) -> StoredDecision:
        evidence = result.evidence | {"medicine_snapshot": medicine, "notice_snapshot": notice}
        with self.sessions.begin() as session:
            session.execute(insert(Decision).values(
                id=decision_id, medicine_id=UUID(medicine_id), notice_id=UUID(notice_id),
                verdict=result.verdict, reason=result.reason, evidence=evidence,
            ).on_conflict_do_nothing(index_elements=[Decision.id]))
            row = session.get(Decision, decision_id)
            assert row is not None
            return _decision(row)

    def decision(self, identifier: str) -> StoredDecision:
        with self.sessions() as session:
            row = session.get(Decision, UUID(identifier))
            if row is None:
                raise ValueError("Decision does not exist.")
            return _decision(row)

    def propose(self, decision: StoredDecision, destination: str) -> StoredAction:
        if decision.verdict != Verdict.MATCH:
            raise ValueError("Only MATCH decisions may propose alerts.")
        notice = NoticeInput.model_validate(decision.evidence["notice_snapshot"])
        medicine = MedicineInput.model_validate(decision.evidence["medicine_snapshot"])
        if notice.report_date is None:
            raise ValueError("Cannot propose an alert without the official notice date.")
        fingerprint = notice_fingerprint(notice.status, notice.code_info)
        with self.sessions.begin() as session:
            require_current_notice(session, decision)
            previous = session.execute(select(Action, Decision).join(Decision, Action.decision_id == Decision.id)
                .where(Decision.medicine_id == UUID(decision.medicine_id),
                       Decision.notice_id == UUID(decision.notice_id))
                .order_by(Action.created_at.desc(), Action.id)).all()
            for old_action, old_decision in previous[:1]:
                old_notice = NoticeInput.model_validate(old_decision.evidence["notice_snapshot"])
                if notice_fingerprint(old_notice.status, old_notice.code_info) == fingerprint:
                    if old_decision.evidence["medicine_snapshot"] != decision.evidence["medicine_snapshot"]:
                        raise ValueError("This notice has an alert for the medicine's previous details. "
                                         "Add a new medicine item for the changed identity before checking again.")
                    # Also reuses legacy medicine:notice:status keys on upgrade.
                    return _action(old_action)
            # Chaining to the preceding proposal distinguishes A -> B -> A
            # from a retry of A, without changing existing audit-table schemas.
            revision = (hashlib.sha256(f"{fingerprint}:{previous[0][0].id}".encode()).hexdigest()[:32]
                        if previous else fingerprint)
            key = action_key(decision.medicine_id, decision.notice_id, notice.status, fingerprint=revision)
            label = "update" if previous else "alert"
            proposed = AlertEnvelope(destination=destination, message=(
                f"MOCK recall {label}: {medicine.name} {medicine.strength}, batch {medicine.batch_no}. "
                f"openFDA notice {notice.recall_number}, status {notice.status or 'UNKNOWN'}. "
                f"Batch evidence: {notice.code_info}. "
                f"Notice date: {notice.report_date.isoformat()}. Official notice: {notice.url}"
            ))
            validate_approved_message(proposed, decision)
            session.execute(insert(Action).values(
                id=uuid5(NAMESPACE_URL, "drug-monitor-action:" + key),
                decision_id=UUID(decision.id), proposed_message=proposed.model_dump_json(),
                idempotency_key=key, status=ActionStatus.PENDING_APPROVAL,
            ).on_conflict_do_nothing())
            row = session.scalar(select(Action).where(Action.idempotency_key == key))
            assert row is not None
            original = session.get(Decision, row.decision_id)
            assert original is not None
            if original.evidence["medicine_snapshot"] != decision.evidence["medicine_snapshot"]:
                # Editing inventory must not relabel an earlier batch's alert as
                # delivery for the new batch under the required unique key.
                raise ValueError("This notice/status has an alert for the medicine's previous details. "
                                 "Add a new medicine item for the changed identity before checking again.")
            return _action(row)

    def action(self, identifier: str) -> StoredAction:
        with self.sessions() as session:
            row = session.get(Action, UUID(identifier))
            if row is None:
                raise ValueError("Action does not exist.")
            return _action(row)

    def approve(self, choices: Sequence[ApprovalChoice]) -> None:
        """Validate the complete reply, then commit all approvals/rejections together."""
        with self.sessions.begin() as session:
            # Consistent lock order prevents deadlocks for concurrent reviewers.
            for choice in sorted(choices, key=lambda item: item.action_id):
                row = session.scalar(select(Action).where(Action.id == UUID(choice.action_id)).with_for_update())
                if row is None:
                    raise ValueError("Approval names a missing action.")
                if row.status != ActionStatus.PENDING_APPROVAL:
                    continue  # A replay cannot change an already recorded human response.
                if not choice.approved:
                    row.status = ActionStatus.REJECTED
                    continue
                proposed = AlertEnvelope.model_validate_json(row.proposed_message)
                approved = AlertEnvelope(
                    destination=choice.destination if choice.destination is not None else proposed.destination,
                    message=choice.message if choice.message is not None else proposed.message,
                )
                decision_row = session.get(Decision, row.decision_id)
                assert decision_row is not None
                if decision_row.verdict != Verdict.MATCH:
                    raise ValueError("Approval requires a MATCH decision.")
                require_current_notice(session, _decision(decision_row))
                validate_approved_message(approved, _decision(decision_row))
                row.approved_message = approved.model_dump_json()
                row.status = ActionStatus.APPROVED

    def send_mock(self, identifier: str) -> bool:
        """Commit one durable mock receipt, then log it at most once on retries.

        The Action row is the mock delivery. A crash between commit and logging
        can omit the console log; replay still never duplicates the DB receipt.
        Real providers would additionally need a transactional outbox/provider key.
        """
        envelope: AlertEnvelope | None = None
        with self.sessions.begin() as session:
            row = session.scalar(select(Action).where(Action.id == UUID(identifier)).with_for_update())
            if row is None:
                raise ValueError("Action does not exist.")
            if row.status in (ActionStatus.SENT, ActionStatus.REJECTED):
                return False
            if row.status != ActionStatus.APPROVED or not row.approved_message:
                raise ValueError("Mock delivery requires stored human approval.")
            decision_row = session.get(Decision, row.decision_id)
            assert decision_row is not None
            decision = _decision(decision_row)
            if decision.verdict != Verdict.MATCH:
                raise ValueError("Mock delivery requires a MATCH decision.")
            require_current_notice(session, decision)
            current = session.get(Medicine, decision_row.medicine_id, with_for_update=True)
            if current is None or MedicineInput.model_validate(current).model_dump(mode="json") != decision.evidence["medicine_snapshot"]:
                raise ValueError("Medicine changed after matching; a new assessment is required.")
            envelope = AlertEnvelope.model_validate_json(row.approved_message)
            validate_approved_message(envelope, decision)
            row.status = ActionStatus.SENT
            key = row.idempotency_key
        logger.info("MOCK_ALERT action_id=%s idempotency_key=%s payload=%s",
                    identifier, key, envelope.model_dump_json())
        return True

    def save_verification(self, action_id: str, outcome: VerificationOutcome) -> str:
        with self.sessions.begin() as session:
            row = Verification(
                action_id=UUID(action_id), url_opened=outcome.url_opened,
                batch_found=outcome.batch_found, notice_date=outcome.notice_date,
                result=outcome.result,
            )
            session.add(row)
            session.flush()
            return str(row.id)
