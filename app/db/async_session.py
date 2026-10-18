"""Async sessions for API requests and the durable household run queue."""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


@lru_cache
def get_async_engine() -> AsyncEngine:
    """Psycopg 3 selects its async driver when used with create_async_engine."""
    return create_async_engine(str(get_settings().database_url), pool_pre_ping=True)


@lru_cache
def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_async_engine(), expire_on_commit=False)


async def get_async_db() -> AsyncIterator[AsyncSession]:
    """One session per request; mutations explicitly commit before returning."""
    async with get_async_session_factory()() as session:
        yield session
