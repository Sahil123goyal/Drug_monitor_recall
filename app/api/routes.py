"""Async API transactions; graph execution is handled by the durable worker."""

import asyncio

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.contracts import AlertEnvelope, StoredDecision
from app.agents.repository import validate_approved_message
from app.db.async_session import get_async_db
from app.db.session import get_engine
from app.models import Action, Decision, Medicine, Run, TestRun, Verdict
from app.schemas.api import (
    ActionResponse, ApproveRequest, ConfirmDecisionsRequest, DecisionResponse, MedicineCreate,
    MedicineEdit, MedicineResponse, ProvideBatchRequest, RunCreate, RunResponse, RunSummary,
    TestRunResponse,
)
from app.services.evaluations import CORE_CASES, evaluate_core_cases
from app.services.official_cases import SUITE, SourceMode
from app.services.official_runner import run_official_suite

router = APIRouter()
Database = Annotated[AsyncSession, Depends(get_async_db)]


async def run_response(run: Run, session: AsyncSession) -> RunResponse:
    """Only expose decisions belonging to this run's checkpoint threads."""
    identifiers = {
        UUID(value)
        for state in run.state.values()
        for value in (*state.get("decision_ids", []), *state.get("all_decision_ids", []))
    }
    if run.pending_interrupt and run.pending_interrupt["value"].get("kind") == "confirm":
        identifiers.update(
            UUID(item["decision_id"]) for item in run.pending_interrupt["value"].get("decisions", [])
        )
    decisions = []
    if identifiers:
        rows = (await session.scalars(
            select(Decision).where(Decision.id.in_(identifiers)).order_by(Decision.created_at, Decision.id)
        )).all()
        decisions = [DecisionResponse.model_validate(row) for row in rows]
    action_ids = {UUID(value) for state in run.state.values() for value in state.get("action_ids", [])}
    if run.pending_interrupt and run.pending_interrupt["value"].get("kind") == "approve":
        action_ids.update(UUID(item["action_id"]) for item in run.pending_interrupt["value"]["alerts"])
    actions = []
    if identifiers or action_ids:
        # An idempotent action can belong to an earlier run's decision.
        rows = (await session.execute(
            select(Action, Decision).join(Decision, Action.decision_id == Decision.id)
            .where(or_(Action.decision_id.in_(identifiers), Action.id.in_(action_ids)))
            .order_by(Action.created_at, Action.id)
        )).all()
        for action, decision in rows:
            notice = decision.evidence.get("notice_snapshot", {})
            actions.append(ActionResponse(
                id=action.id, decision_id=decision.id, medicine_id=decision.medicine_id,
                status=action.status, proposed=AlertEnvelope.model_validate_json(action.proposed_message),
                approved=AlertEnvelope.model_validate_json(action.approved_message) if action.approved_message else None,
                notice_url=notice.get("url"), notice_date=notice.get("report_date"),
            ))
    return RunResponse(
        id=run.id, household_id=run.household_id, destination=run.destination,
        status=run.status, medicine_ids=run.medicine_ids, cursor=run.cursor,
        state=run.state, pending_interrupt=run.pending_interrupt, decisions=decisions,
        error=run.error, created_at=run.created_at, actions=actions,
    )


@router.get("/medicines", response_model=list[MedicineResponse])
async def list_medicines(household_id: UUID, session: Database) -> list[Medicine]:
    return list((await session.scalars(select(Medicine).where(Medicine.household_id == household_id)
                                      .order_by(Medicine.name, Medicine.id))).all())


@router.put("/medicines/{medicine_id}", response_model=MedicineResponse)
async def edit_medicine(medicine_id: UUID, body: MedicineEdit, session: Database) -> Medicine:
    medicine = await session.get(Medicine, medicine_id, with_for_update=True)
    if medicine is None:
        raise HTTPException(404, "Medicine not found.")
    active = await session.scalar(select(Run.id).where(
        Run.status.in_(["QUEUED", "RUNNING", "WAITING"]), Run.medicine_ids.contains([str(medicine_id)]),
    ).limit(1))
    if active is not None:
        raise HTTPException(409, "Finish the active check before editing. Use Approvals to provide a missing batch.")
    for field, value in body.model_dump().items():
        setattr(medicine, field, value)
    await session.commit()
    await session.refresh(medicine)
    return medicine


@router.post("/medicines", response_model=MedicineResponse, status_code=status.HTTP_201_CREATED)
async def add_medicine(body: MedicineCreate, session: Database) -> Medicine:
    medicine = Medicine(**body.model_dump())
    session.add(medicine)
    await session.commit()
    await session.refresh(medicine)
    return medicine


@router.post("/runs", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_run(body: RunCreate, session: Database) -> RunResponse:
    identifiers = (await session.scalars(
        select(Medicine.id).where(Medicine.household_id == body.household_id)
        .order_by(Medicine.id).with_for_update()
    )).all()
    if not identifiers:
        raise HTTPException(404, "No medicines found for this household.")
    run = Run(household_id=body.household_id, destination=body.destination,
              medicine_ids=[str(value) for value in identifiers], state={}, status="QUEUED")
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return await run_response(run, session)


@router.get("/runs", response_model=list[RunSummary])
async def list_runs(
    household_id: UUID, session: Database, limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[Run]:
    return list((await session.scalars(select(Run).where(Run.household_id == household_id)
                                      .order_by(Run.created_at.desc(), Run.id).limit(limit))).all())


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID, session: Database) -> RunResponse:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Run not found.")
    return await run_response(run, session)


async def waiting_run(
    session: AsyncSession, run_id: UUID, interrupt_id: str,
    kind: Literal["collect", "confirm", "approve"],
) -> Run:
    """Serialize replies on the run row; reject stale or wrong-kind answers."""
    run = await session.get(Run, run_id, with_for_update=True)
    if run is None:
        raise HTTPException(404, "Run not found.")
    pending = run.pending_interrupt
    if (run.status != "WAITING" or not pending or pending["id"] != interrupt_id
            or pending["value"].get("kind") != kind):
        raise HTTPException(409, "This run is not waiting for that interrupt and response type.")
    return run


async def queue_reply(run: Run, value: dict[str, Any], session: AsyncSession) -> RunResponse:
    assert run.pending_interrupt is not None
    run.pending_resume = {"thread_id": run.pending_interrupt["thread_id"],
                          "interrupt_id": run.pending_interrupt["id"], "value": value}
    run.pending_interrupt = None
    run.status = "QUEUED"
    await session.commit()
    return await run_response(run, session)


@router.post("/runs/{run_id}/provide-batch", response_model=RunResponse, status_code=202)
async def provide_batch(run_id: UUID, body: ProvideBatchRequest, session: Database) -> RunResponse:
    run = await waiting_run(session, run_id, body.interrupt_id, "collect")
    return await queue_reply(run, {"batch_no": body.batch_no}, session)


@router.post("/runs/{run_id}/confirm-decisions", response_model=RunResponse, status_code=202)
async def confirm_decisions(run_id: UUID, body: ConfirmDecisionsRequest, session: Database) -> RunResponse:
    run = await waiting_run(session, run_id, body.interrupt_id, "confirm")
    assert run.pending_interrupt is not None
    expected = {item["decision_id"] for item in run.pending_interrupt["value"]["decisions"]}
    supplied = [str(item.decision_id) for item in body.confirmations]
    if len(supplied) != len(set(supplied)) or set(supplied) != expected:
        raise HTTPException(422, "Accept or reject every displayed decision exactly once.")
    value = {"confirmations": [item.model_dump(mode="json") for item in body.confirmations]}
    return await queue_reply(run, value, session)


@router.post("/runs/{run_id}/approve", response_model=RunResponse, status_code=202)
async def approve_run(run_id: UUID, body: ApproveRequest, session: Database) -> RunResponse:
    run = await waiting_run(session, run_id, body.interrupt_id, "approve")
    assert run.pending_interrupt is not None
    expected = {item["action_id"] for item in run.pending_interrupt["value"]["alerts"]}
    supplied = [str(item.action_id) for item in body.approvals]
    if len(supplied) != len(set(supplied)) or set(supplied) != expected:
        raise HTTPException(422, "Approve or reject every displayed action exactly once.")
    for item in body.approvals:
        if not item.approved:
            continue
        action = await session.get(Action, item.action_id)
        if action is None:
            raise HTTPException(409, "Proposed action no longer exists.")
        decision = await session.get(Decision, action.decision_id)
        if decision is None:
            raise HTTPException(409, "Decision no longer exists.")
        if decision.verdict != Verdict.MATCH:
            raise HTTPException(409, "Only MATCH decisions can be approved.")
        proposed = AlertEnvelope.model_validate_json(action.proposed_message)
        approved = AlertEnvelope(destination=item.destination or proposed.destination,
                                 message=item.message or proposed.message)
        try:
            validate_approved_message(approved, StoredDecision(
                str(decision.id), str(decision.medicine_id), str(decision.notice_id),
                decision.verdict, decision.reason, decision.evidence,
            ))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    value = {"approvals": [item.model_dump(mode="json", exclude_none=True) for item in body.approvals]}
    return await queue_reply(run, value, session)


@router.get("/test-runs", response_model=list[TestRunResponse])
async def test_runs(
    session: Database, limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    suite: Literal["core", "official"] | None = None,
) -> list[TestRun]:
    query = select(TestRun)
    if suite == "core":
        query = query.where(TestRun.id.in_([case.id for case in CORE_CASES]))
    elif suite == "official":
        # Show one coherent latest attempt, never mix PASS rows across attempts.
        latest = (select(TestRun.evidence["suite_run_id"].astext)
                  .where(TestRun.evidence["suite"].astext == SUITE)
                  .order_by(TestRun.evidence["executed_at"].astext.desc(), TestRun.id.desc())
                  .limit(1).correlate(None).scalar_subquery())
        query = query.where(TestRun.evidence["suite"].astext == SUITE,
                            TestRun.evidence["suite_run_id"].astext == latest)
    return list((await session.scalars(query.order_by(TestRun.tc_name, TestRun.id).limit(limit).offset(offset))).all())


@router.post("/test-runs/evaluate", response_model=list[TestRunResponse])
async def evaluate_tests(
    session: Database, suite: Literal["core", "official"] = "official", source: SourceMode = "snapshot",
) -> list[TestRunResponse]:
    """Official acceptance suite by default; legacy matcher smoke cases remain explicit."""
    if suite == "official":
        rows = await asyncio.to_thread(run_official_suite, get_engine(), source=source)
        return [TestRunResponse.model_validate(row) for row in rows]
    rows = evaluate_core_cases()
    statement = insert(TestRun).values(rows)
    statement = statement.on_conflict_do_update(index_elements=[TestRun.id], set_={
        name: getattr(statement.excluded, name)
        for name in ("tc_name", "input", "expected", "actual", "evidence", "passed")
    })
    await session.execute(statement)
    await session.commit()
    saved = (await session.scalars(select(TestRun).where(TestRun.id.in_([case.id for case in CORE_CASES]))
                                   .order_by(TestRun.tc_name))).all()
    return [TestRunResponse.model_validate(row) for row in saved]
