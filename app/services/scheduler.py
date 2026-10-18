"""Daily APScheduler trigger feeding the existing durable, human-reviewed queue."""

import asyncio
import logging
from datetime import date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import SchedulerSettings
from app.models import Medicine, Run

logger = logging.getLogger(__name__)


def daily_run_id(day: date, medicine_id: UUID) -> UUID:
    """The database primary key deduplicates cron, restart and multi-worker races."""
    return uuid5(NAMESPACE_URL, f"drug-monitor-daily:{day.isoformat()}:{medicine_id}")


async def enqueue_daily_checks(
    engine: AsyncEngine, settings: SchedulerSettings, *, now: datetime | None = None,
) -> int:
    """Queue every medicine independently so one unanswered batch cannot block others.

    No fetch, approval or delivery happens here. RunWorker executes the unchanged
    graph and its PostgreSQL checkpoints. All inserts commit together.
    """
    if now is not None and now.tzinfo is None:
        raise ValueError("The scheduling clock must include a timezone.")
    local = (now or datetime.now(ZoneInfo(settings.scheduler_timezone))).astimezone(
        ZoneInfo(settings.scheduler_timezone)
    )
    count = 0
    async with async_sessionmaker(engine, expire_on_commit=False).begin() as session:
        medicines = (await session.scalars(select(Medicine).order_by(Medicine.id).with_for_update())).all()
        for medicine in medicines:
            identifier = await session.scalar(insert(Run).values(
                id=daily_run_id(local.date(), medicine.id), household_id=medicine.household_id,
                destination=settings.scheduler_destination, medicine_ids=[str(medicine.id)],
                cursor=0, state={}, status="QUEUED",
            ).on_conflict_do_nothing(index_elements=[Run.id]).returning(Run.id))
            count += identifier is not None
    logger.info("DAILY_CHECKS_QUEUED date=%s count=%s", local.date(), count,
                extra={"event": "DAILY_CHECKS_QUEUED", "queued_count": count})
    return count


class DailyMonitor:
    """Recreate the timer on startup; PostgreSQL owns the durable run queue."""

    def __init__(self, engine: AsyncEngine, settings: SchedulerSettings | None = None) -> None:
        self.engine = engine
        self.settings = settings or SchedulerSettings()
        self.scheduler = AsyncIOScheduler(timezone=self.settings.scheduler_timezone)
        self._running: set[asyncio.Task] = set()

    async def _enqueue(self) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._running.add(task)
        try:
            await enqueue_daily_checks(self.engine, self.settings)
        except Exception as exc:
            # Queue/DB failures are distinct from source/network failures.
            logger.error("DAILY_QUEUE_FAILED error_type=%s", type(exc).__name__,
                         extra={"event": "DAILY_QUEUE_FAILED"})
        finally:
            self._running.discard(task)

    def start(self) -> None:
        if not self.settings.scheduler_enabled:
            return
        self.scheduler.add_job(
            self._enqueue, CronTrigger(hour=self.settings.scheduler_hour,
                                      minute=self.settings.scheduler_minute,
                                      timezone=self.settings.scheduler_timezone),
            id="daily-medicine-recheck", max_instances=1, coalesce=True,
            misfire_grace_time=86400, replace_existing=True,
        )
        now = datetime.now(ZoneInfo(self.settings.scheduler_timezone))
        if (now.hour, now.minute) >= (self.settings.scheduler_hour, self.settings.scheduler_minute):
            # Recover today's missed trigger after restart; never replay old days.
            self.scheduler.add_job(self._enqueue, "date", run_date=now, id="daily-startup-catchup")
        self.scheduler.start()

    async def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.pause()
            if self._running:
                await asyncio.gather(*self._running)
            self.scheduler.shutdown(wait=True)
            await asyncio.sleep(0)  # Let AsyncIOScheduler's shutdown callback run.
