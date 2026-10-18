"""Persist source records without replacing IDs referenced by decisions."""

from collections.abc import Sequence
import logging
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.models import Notice as NoticeRecord
from app.schemas.notice import Notice
from app.services.notice_changes import notice_content

logger = logging.getLogger(__name__)


class NoticeStore(Protocol):
    """Persistence boundary, replaceable by an in-memory store in unit tests."""

    def save(self, notices: Sequence[Notice]) -> None:
        """Commit the fetched records or propagate the database error."""
        ...


class SQLAlchemyNoticeStore:
    """Upsert one fetched page atomically using the source's external ID."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._session_factory = session_factory

    def save(self, notices: Sequence[Notice]) -> None:
        if not notices:
            return
        # A page can repeat an ID; PostgreSQL forbids updating a row twice in one
        # INSERT. Keep its newest observation before constructing the batch.
        latest: dict[tuple[str, str], Notice] = {}
        for notice in notices:
            key = (notice.source, notice.recall_number)
            if key not in latest or notice.fetched_at >= latest[key].fetched_at:
                latest[key] = notice
        rows = [
            {
                "source": notice.source,
                "notice_id": notice.recall_number,
                "url": str(notice.url),
                "fetched_at": notice.fetched_at,
                "status": notice.status,
                "report_date": notice.report_date,
                "product_description": notice.product_description,
                "code_info": notice.code_info,
                "raw_json": notice.raw_json,
            }
            for notice in latest.values()
        ]
        factory = self._session_factory or get_session_factory()
        changes = []
        with factory.begin() as session:
            # Stable lock order avoids deadlocks when overlapping pages arrive.
            for values in sorted(rows, key=lambda row: (row["source"], row["notice_id"])):
                inserted = session.scalar(insert(NoticeRecord).values(**values)
                    .on_conflict_do_nothing(constraint="uq_notices_source_notice_id")
                    .returning(NoticeRecord.id))
                if inserted is not None:
                    continue  # First observation establishes a baseline.
                current = session.scalar(select(NoticeRecord).where(
                    NoticeRecord.source == values["source"],
                    NoticeRecord.notice_id == values["notice_id"],
                ).with_for_update())
                assert current is not None
                if values["fetched_at"] < current.fetched_at:
                    continue
                before = notice_content(current.status, current.code_info)
                after = notice_content(values["status"], values["code_info"])
                if before != after:
                    changes.append({"source": current.source, "notice_id": current.notice_id,
                                    "before": before, "after": after})
                for name, value in values.items():
                    setattr(current, name, value)
        # Emit only committed changes. A failed HTTP request never reaches here.
        for change in changes:
            logger.info("NOTICE_CHANGED source=%s notice_id=%s before=%s after=%s",
                        change["source"], change["notice_id"], change["before"], change["after"],
                        extra={"event": "NOTICE_CHANGED", "notice_change": change})

