"""FastAPI application and lifecycle of the durable household queue worker."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agents.runtime import open_agent
from app.api.routes import router
from app.db.async_session import get_async_engine
from app.services.run_worker import RunWorker
from app.services.scheduler import DailyMonitor


def initialize_checkpoints() -> None:
    with open_agent():
        pass


def create_app(*, enable_worker: bool = True) -> FastAPI:
    """Disable the worker only in tests that explicitly drive their fake runner."""
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if not enable_worker:
            yield
            return
        await asyncio.to_thread(initialize_checkpoints)
        engine = get_async_engine()
        monitor = DailyMonitor(engine)
        worker = RunWorker(engine)
        poller = asyncio.create_task(worker.poll())
        application.state.run_worker = worker
        application.state.daily_monitor = monitor
        try:
            monitor.start()
            yield
        finally:
            await monitor.stop()
            await worker.stop()
            await poller
            await engine.dispose()

    application = FastAPI(title="Drug Recall Monitor", version="0.2.0", lifespan=lifespan)
    application.include_router(router)

    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Liveness only; queue/database errors remain visible in logs and runs."""
        return {"status": "ok"}

    return application


app = create_app()

