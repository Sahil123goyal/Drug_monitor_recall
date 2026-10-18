"""Strict API contracts. Human responses cannot set graph state or verdicts."""

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.enums import ActionStatus, Verdict
from app.agents.contracts import AlertEnvelope

Nonblank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RunStatus = Literal["QUEUED", "RUNNING", "WAITING", "COMPLETED", "FAILED"]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MedicineCreate(RequestModel):
    household_id: UUID
    name: Nonblank = Field(max_length=200)
    strength: Nonblank = Field(max_length=100)
    batch_no: Nonblank | None = Field(default=None, max_length=200)


class MedicineResponse(MedicineCreate):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
    id: UUID


class MedicineEdit(RequestModel):
    """Replace the editable inventory fields; household membership is immutable."""
    name: Nonblank = Field(max_length=200)
    strength: Nonblank = Field(max_length=100)
    batch_no: Nonblank | None = Field(default=None, max_length=200)


class RunCreate(RequestModel):
    household_id: UUID
    destination: Nonblank = Field(default="mock://synthetic-household", max_length=320)


class ApprovalItem(RequestModel):
    action_id: UUID
    approved: bool = Field(strict=True)
    destination: Nonblank | None = Field(default=None, max_length=320)
    message: Nonblank | None = Field(default=None, max_length=10000)


class ApproveRequest(RequestModel):
    interrupt_id: Nonblank
    approvals: list[ApprovalItem] = Field(min_length=1)


class DecisionConfirmationItem(RequestModel):
    decision_id: UUID
    accepted: bool = Field(strict=True)


class ConfirmDecisionsRequest(RequestModel):
    interrupt_id: Nonblank
    confirmations: list[DecisionConfirmationItem] = Field(min_length=1)


class ProvideBatchRequest(RequestModel):
    interrupt_id: Nonblank
    batch_no: Nonblank = Field(max_length=200)


class PendingInterrupt(BaseModel):
    id: str
    thread_id: str
    medicine_id: UUID
    value: dict[str, Any]


class DecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    medicine_id: UUID
    notice_id: UUID
    verdict: Verdict
    reason: str
    evidence: dict[str, Any]
    created_at: datetime


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    household_id: UUID
    destination: str
    status: RunStatus
    medicine_ids: list[UUID]
    cursor: int
    error: str | None
    created_at: datetime


class ActionResponse(BaseModel):
    id: UUID
    decision_id: UUID
    medicine_id: UUID
    status: ActionStatus
    proposed: AlertEnvelope
    approved: AlertEnvelope | None
    notice_url: str | None
    notice_date: date | None


class RunResponse(RunSummary):
    state: dict[str, Any]
    pending_interrupt: PendingInterrupt | None
    decisions: list[DecisionResponse]
    actions: list[ActionResponse] = Field(default_factory=list)


class TestRunResponse(BaseModel):
    __test__ = False
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tc_name: str
    input: dict[str, Any]
    expected: dict[str, Any]
    actual: dict[str, Any] | None
    evidence: dict[str, Any] | None
    passed: bool | None
