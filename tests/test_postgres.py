"""Exercise audit constraints against PostgreSQL in an isolated test schema."""

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError
from app.models import (
    Action, ActionStatus, Decision, Medicine, Notice, TestRun,
    Verification, VerificationResult, Verdict,
)

pytestmark = pytest.mark.integration


def test_defaults_json_and_audit_constraints(database: Engine) -> None:
    """Cover deduplication, approval, confirmation evidence, and FK protection."""
    with database.begin() as connection:
        medicine_id = connection.execute(
            insert(Medicine).values(
                name="SyntheticBrand", strength="10 mg", batch_no="0001",
                household_id=uuid4(),
            ).returning(Medicine.id)
        ).scalar_one()
        missing_batch_id = connection.execute(
            insert(Medicine).values(
                name="SyntheticBrand", strength="10 mg", household_id=uuid4()
            ).returning(Medicine.id)
        ).scalar_one()
        assert connection.scalar(
            select(Medicine.batch_no).where(Medicine.id == missing_batch_id)
        ) is None
        notice_id = connection.execute(
            insert(Notice).values(
                notice_id="SYNTHETIC-001",
                url="https://api.fda.gov/drug/enforcement.json?search=recall_number:SYNTHETIC-001",
                report_date=date(2026, 1, 1),
                raw_json={"synthetic": True, "code_info": "Batch 0001"},
            ).returning(Notice.id)
        ).scalar_one()
        decision_id = connection.execute(
            insert(Decision).values(
                medicine_id=medicine_id, notice_id=notice_id, verdict=Verdict.MATCH,
                reason="Synthetic exact match", evidence={"batch_no": "0001"},
            ).returning(Decision.id)
        ).scalar_one()
        action_values = {
            "decision_id": decision_id,
            "proposed_message": "Synthetic proposed alert; no delivery.",
            "idempotency_key": "synthetic-unique-action",
        }
        action = connection.execute(
            insert(Action).values(**action_values).returning(
                Action.id, Action.status, Action.created_at
            )
        ).one()
        assert action.status == ActionStatus.PENDING_APPROVAL
        assert action.created_at.tzinfo is not None
        assert connection.scalar(select(Notice.raw_json).where(Notice.id == notice_id)) == {
            "synthetic": True, "code_info": "Batch 0001"
        }

        # Savepoints let each rejected write leave the enclosing transaction usable.
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(insert(Action).values(**action_values))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                Action.__table__.update().where(Action.id == action.id).values(
                    status=ActionStatus.APPROVED
                )
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(insert(Verification).values(
                action_id=action.id, result=VerificationResult.CONFIRMED,
                batch_found=False,
            ))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(Medicine.__table__.delete().where(Medicine.id == medicine_id))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(insert(Decision).values(
                medicine_id=uuid4(), notice_id=notice_id, verdict=Verdict.UNCERTAIN,
                reason="Synthetic missing medicine", evidence={},
            ))

        connection.execute(Action.__table__.update().where(Action.id == action.id).values(
            status=ActionStatus.APPROVED, approved_message="Human-approved synthetic text"
        ))
        connection.execute(insert(Verification).values(
            action_id=action.id, result=VerificationResult.CONFIRMED,
            batch_found=True, notice_date=date(2026, 1, 1),
            url_opened="https://api.fda.gov/drug/enforcement.json",
        ))
        connection.execute(insert(TestRun).values(
            tc_name="synthetic_fixture", input={"batch_no": "0001"},
            expected={"verdict": "MATCH"}, actual={"verdict": "MATCH"},
            evidence={"synthetic": True}, passed=True,
        ))


def test_notice_upsert_preserves_id_and_rejects_stale_overwrites(database: Engine) -> None:
    """Repeated fetches refresh evidence and fetched_at without duplicate rows."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy.orm import sessionmaker
    from app.schemas.notice import Notice as NoticePayload
    from app.services.notice_store import SQLAlchemyNoticeStore

    factory = sessionmaker(database)
    store = SQLAlchemyNoticeStore(factory)
    first = NoticePayload(
        recall_number="SYNTHETIC-UPSERT", product_description="Synthetic ibuprofen",
        code_info="Batch 0001", status="Ongoing", report_date=date(2026, 1, 15),
        url="https://api.fda.gov/drug/enforcement.json?search=recall_number:SYNTHETIC-UPSERT",
        fetched_at=datetime.now(timezone.utc), raw_json={"synthetic": True},
    )
    store.save([first, first])
    with factory() as session:
        original_id = session.scalar(select(Notice.id).where(Notice.notice_id == first.recall_number))
    refreshed = first.model_copy(update={
        "fetched_at": first.fetched_at + timedelta(seconds=1),
        "status": "Terminated", "raw_json": {"synthetic": True, "updated": True},
    })
    store.save([refreshed])
    store.save([first])
    with factory() as session:
        saved = session.scalars(select(Notice).where(Notice.notice_id == first.recall_number)).one()
        assert saved.id == original_id
        assert saved.status == "Terminated"
        assert saved.fetched_at == refreshed.fetched_at
        assert saved.raw_json == refreshed.raw_json
        assert saved.code_info == "Batch 0001"


