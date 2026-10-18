"""Serializable graph state and typed human replies."""

from dataclasses import dataclass
from datetime import date
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ActionStatus, Verdict, VerificationResult


class AgentState(TypedDict, total=False):
    medicine_id: str
    destination: str
    medicine: dict[str, Any]
    plan: dict[str, Any]
    notices: list[dict[str, Any]]
    decision_ids: list[str]
    all_decision_ids: list[str]
    action_ids: list[str]
    sent_action_ids: list[str]
    verification_results: list[dict[str, Any]]
    failures: list[dict[str, str]]
    completed_nodes: list[str]
    uncertain_count: int
    outcome: str


class AlertEnvelope(BaseModel):
    """Stored as JSON in the existing proposed_message/approved_message TEXT columns."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    destination: str = Field(min_length=1, max_length=320)
    message: str = Field(min_length=1, max_length=10_000)


class BatchReply(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    batch_no: str = Field(min_length=1, max_length=200)


class ApprovalChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str
    approved: bool = Field(strict=True)
    destination: str | None = None
    message: str | None = None


class ApprovalReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approvals: list[ApprovalChoice] = Field(min_length=1)


class DecisionConfirmation(BaseModel):
    """Accept or reject a matcher decision; humans cannot rewrite the verdict."""

    model_config = ConfigDict(extra="forbid")
    decision_id: str
    accepted: bool = Field(strict=True)


class DecisionConfirmationReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmations: list[DecisionConfirmation] = Field(min_length=1)


@dataclass(frozen=True)
class StoredDecision:
    id: str
    medicine_id: str
    notice_id: str
    verdict: Verdict
    reason: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class StoredAction:
    id: str
    decision_id: str
    status: ActionStatus
    idempotency_key: str
    proposed: AlertEnvelope
    approved: AlertEnvelope | None


@dataclass(frozen=True)
class VerificationOutcome:
    url_opened: str | None
    batch_found: bool | None
    notice_date: date | None
    result: VerificationResult
    reason: str
