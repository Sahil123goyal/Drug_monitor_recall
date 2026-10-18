"""Durable household queue; sync graph calls run in threads with their own sessions."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents.runtime import open_agent
from app.models import Run

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphStep:
    state: dict[str, Any]
    pending_interrupt: dict[str, Any] | None
    error: str | None = None


class GraphRunner:
    """Reopen the graph for every step; only PostgreSQL retains its state."""

    def __init__(
        self, factory: Callable[[], AbstractContextManager[CompiledStateGraph]] | None = None,
    ) -> None:
        self.factory = factory or (lambda: open_agent(setup=False))

    def step(
        self, run_id: UUID, medicine_id: str, destination: str,
        reply: dict[str, Any] | None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> GraphStep:
        thread_id = f"{run_id}:{medicine_id}"
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "run_name": "household-recall-monitor",
            "tags": ["synthetic-household", "mock-alerts"],
            "metadata": {"run_id": str(run_id), "medicine_id": medicine_id},
        }
        with self.factory() as graph:
            def advance(value: dict[str, Any] | Command | None) -> None:
                # Publish real node completions, never a timer-generated progress bar.
                for state in graph.stream(value, config, stream_mode="values"):
                    if on_progress is not None:
                        on_progress({key: item for key, item in state.items() if key != "__interrupt__"})

            snapshot = graph.get_state(config)
            error = None
            try:
                pending = snapshot.interrupts
                if not snapshot.values:
                    advance({"medicine_id": medicine_id, "destination": destination})
                elif pending:
                    # Recovery must not replay a consumed reply into a NEW question.
                    if reply and reply["thread_id"] == thread_id:
                        identifier = reply["interrupt_id"]
                        if any(item.id == identifier for item in pending):
                            advance(Command(resume={identifier: reply["value"]}))
                elif snapshot.next:
                    advance(None)
            except Exception as exc:
                error = f"Agent execution failed ({type(exc).__name__})."
                logger.error("Agent run %s failed: %s", run_id, type(exc).__name__)
            snapshot = graph.get_state(config)
            pending = snapshot.interrupts
            question = None
            if pending:
                question = {"id": pending[0].id, "thread_id": thread_id,
                            "medicine_id": medicine_id, "value": pending[0].value}
            return GraphStep(dict(snapshot.values), question, error)


class RunWorker:
    """Recover committed jobs, including jobs left RUNNING after a process crash.

    A PostgreSQL session advisory lock prevents two API processes from driving
    one run concurrently. No transaction remains open across HTTP/graph calls.
    """

    def __init__(self, engine: AsyncEngine, runner: GraphRunner | None = None) -> None:
        self.engine = engine
        self.runner = runner or GraphRunner()
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.tasks: dict[UUID, asyncio.Task[None]] = {}
        self.stopping = asyncio.Event()

    async def poll(self) -> None:
        while not self.stopping.is_set():
            try:
                async with self.sessions() as session:
                    ids = (await session.scalars(
                        select(Run.id).where(Run.status.in_(["QUEUED", "RUNNING"]))
                        .order_by(Run.created_at, Run.id).limit(100)
                    )).all()
                for identifier in ids:
                    if identifier not in self.tasks and len(self.tasks) < 4:
                        task = asyncio.create_task(self.process(identifier))
                        self.tasks[identifier] = task
                        task.add_done_callback(lambda done, key=identifier: self._finished(key, done))
            except SQLAlchemyError as exc:
                logger.error("Run queue unavailable: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=1.0)
            except TimeoutError:
                pass

    def _finished(self, identifier: UUID, task: asyncio.Task[None]) -> None:
        self.tasks.pop(identifier, None)
        if not task.cancelled() and task.exception():
            logger.error("Run worker %s failed: %s", identifier, type(task.exception()).__name__)

    async def stop(self) -> None:
        """Drain graph threads before releasing their database locks."""
        self.stopping.set()
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def process(self, identifier: UUID) -> None:
        lock_key = int.from_bytes(identifier.bytes[:8], "big", signed=True)
        async with self.engine.connect() as connection:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
            )
            await connection.commit()
            if not acquired:
                return
            sessions = async_sessionmaker(connection, expire_on_commit=False)
            try:
                await self._process_locked(identifier, sessions)
            finally:
                await connection.rollback()
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                await connection.commit()

    async def _process_locked(
        self, identifier: UUID, sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        while True:
            async with sessions() as session, session.begin():
                run = await session.get(Run, identifier, with_for_update=True)
                if run is None or run.status not in {"QUEUED", "RUNNING"}:
                    return
                if run.cursor >= len(run.medicine_ids):
                    failed = any(s.get("outcome") == "FAILED" for s in run.state.values())
                    run.status = "FAILED" if failed else "COMPLETED"
                    run.pending_interrupt = None
                    return
                medicine_id = run.medicine_ids[run.cursor]
                destination, reply = run.destination, run.pending_resume
                run.status = "RUNNING"

            loop = asyncio.get_running_loop()

            async def save_progress(state: dict[str, Any]) -> None:
                async with sessions() as session, session.begin():
                    current = await session.get(Run, identifier, with_for_update=True)
                    assert current is not None
                    current.state = {**current.state, medicine_id: state}

            def publish(state: dict[str, Any]) -> None:
                # The graph thread never shares an AsyncSession with the event loop.
                asyncio.run_coroutine_threadsafe(save_progress(state), loop).result()

            execution = asyncio.create_task(asyncio.to_thread(
                self.runner.step, identifier, medicine_id, destination, reply, publish,
            ))
            try:
                result = await asyncio.shield(execution)
            except asyncio.CancelledError:
                # A thread cannot be cancelled: keep its lock until it finishes.
                await execution
                raise
            except Exception as exc:
                result = GraphStep({}, None, f"Agent infrastructure failed ({type(exc).__name__}).")

            async with sessions() as session, session.begin():
                run = await session.get(Run, identifier, with_for_update=True)
                assert run is not None
                run.state = {**run.state, medicine_id: result.state}
                run.pending_resume = None
                run.pending_interrupt = result.pending_interrupt
                run.error = result.error
                if result.error:
                    run.status = "FAILED"
                    return
                if result.pending_interrupt:
                    run.status = "WAITING"
                    return
                run.cursor += 1
