"""Typed PostgreSQL models for household inventory and the recall audit trail."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ActionStatus, Verdict, VerificationResult


class UUIDPrimaryKey:
    """Generate UUIDs both for ORM inserts and direct SQL inserts."""

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )


class Medicine(UUIDPrimaryKey, Base):
    """A synthetic household's medicine. Missing batch requires user input."""

    __tablename__ = "medicines"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
        CheckConstraint("btrim(strength) <> ''", name="strength_not_blank"),
        CheckConstraint(
            "batch_no IS NULL OR btrim(batch_no) <> ''", name="batch_not_blank"
        ),
        Index("ix_medicines_exact_identity", "name", "strength", "batch_no"),
    )

    name: Mapped[str] = mapped_column(Text)  # Brand name, not generic ingredient.
    strength: Mapped[str] = mapped_column(Text)
    # NULL means unknown, never a wildcard. Preserve leading zeros and punctuation.
    batch_no: Mapped[str | None] = mapped_column(Text)
    # Household authentication is outside this local hackathon implementation.
    household_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), index=True)

    decisions: Mapped[list[Decision]] = relationship(
        back_populates="medicine", passive_deletes="all"
    )


class Notice(UUIDPrimaryKey, Base):
    """Official source record, including its unmodified response payload."""

    __tablename__ = "notices"
    __table_args__ = (
        UniqueConstraint("source", "notice_id", name="uq_notices_source_notice_id"),
    )

    source: Mapped[str] = mapped_column(
        String(80), default="openFDA", server_default=text("'openFDA'")
    )
    # External source identifier (e.g. recall_number), distinct from our UUID id.
    notice_id: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Source fields may be absent; NULL must lead to UNCERTAIN where required.
    status: Mapped[str | None] = mapped_column(Text)
    report_date: Mapped[date | None] = mapped_column(Date, index=True)
    product_description: Mapped[str | None] = mapped_column(Text)
    code_info: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB(none_as_null=True))

    decisions: Mapped[list[Decision]] = relationship(
        back_populates="notice", passive_deletes="all"
    )


class Decision(UUIDPrimaryKey, Base):
    """One evaluation of a medicine against one saved notice."""

    __tablename__ = "decisions"

    medicine_id: Mapped[UUID] = mapped_column(
        ForeignKey("medicines.id", ondelete="RESTRICT"), index=True
    )
    # References notices.id, not the external notices.notice_id string.
    notice_id: Mapped[UUID] = mapped_column(
        ForeignKey("notices.id", ondelete="RESTRICT"), index=True
    )
    verdict: Mapped[Verdict] = mapped_column(
        Enum(Verdict, name="decision_verdict", validate_strings=True)
    )
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    medicine: Mapped[Medicine] = relationship(back_populates="decisions")
    notice: Mapped[Notice] = relationship(back_populates="decisions")
    actions: Mapped[list[Action]] = relationship(
        back_populates="decision", passive_deletes="all"
    )


class Action(UUIDPrimaryKey, Base):
    """Proposed mock alert: approval precedes delivery, then verification audits it."""

    __tablename__ = "actions"
    __table_args__ = (
        CheckConstraint(
            "status NOT IN ('APPROVED', 'SENT') OR "
            "(approved_message IS NOT NULL AND btrim(approved_message) <> '')",
            name="approval_message_required",
        ),
        CheckConstraint("btrim(idempotency_key) <> ''", name="key_not_blank"),
    )

    decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("decisions.id", ondelete="RESTRICT"), index=True
    )
    proposed_message: Mapped[str] = mapped_column(Text)
    approved_message: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ActionStatus] = mapped_column(
        Enum(ActionStatus, name="action_status", validate_strings=True),
        default=ActionStatus.PENDING_APPROVAL,
        server_default=text("'PENDING_APPROVAL'"),
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    decision: Mapped[Decision] = relationship(back_populates="actions")
    verifications: Mapped[list[Verification]] = relationship(
        back_populates="action", passive_deletes="all"
    )


class Verification(UUIDPrimaryKey, Base):
    """A separate reopening of an official notice, retaining each attempt."""

    __tablename__ = "verifications"
    __table_args__ = (
        CheckConstraint(
            "result <> 'CONFIRMED' OR "
            "(batch_found IS TRUE AND notice_date IS NOT NULL "
            "AND url_opened IS NOT NULL AND btrim(url_opened) <> '')",
            name="confirmed_evidence_required",
        ),
    )

    action_id: Mapped[UUID] = mapped_column(
        ForeignKey("actions.id", ondelete="RESTRICT"), index=True
    )
    # NULL records an unsuccessful open; batch_found NULL means unknown.
    url_opened: Mapped[str | None] = mapped_column(Text)
    batch_found: Mapped[bool | None] = mapped_column(Boolean)
    notice_date: Mapped[date | None] = mapped_column(Date)
    result: Mapped[VerificationResult] = mapped_column(
        Enum(VerificationResult, name="verification_result", validate_strings=True)
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    action: Mapped[Action] = relationship(back_populates="verifications")


class TestRun(UUIDPrimaryKey, Base):
    """Synthetic evaluation inputs, expected outputs, and observed evidence."""

    __tablename__ = "test_runs"
    __test__ = False  # This ORM class is not a pytest test suite.

    tc_name: Mapped[str] = mapped_column(Text, index=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB(none_as_null=True))
    expected: Mapped[dict[str, Any]] = mapped_column(JSONB(none_as_null=True))
    actual: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    passed: Mapped[bool | None] = mapped_column(Boolean)  # NULL = not evaluated.


class Run(UUIDPrimaryKey, Base):
    """Durable household queue and API view of per-medicine graph checkpoints."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'WAITING', 'COMPLETED', 'FAILED')",
            name="valid_status",
        ),
        CheckConstraint("cursor >= 0", name="nonnegative_cursor"),
    )

    household_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), index=True)
    destination: Mapped[str] = mapped_column(String(320))
    # Freeze membership at start: later inventory additions belong to a new run.
    medicine_ids: Mapped[list[str]] = mapped_column(JSONB)
    cursor: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    pending_interrupt: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # Persist the reply before scheduling; a process restart must not lose approval.
    pending_resume: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

