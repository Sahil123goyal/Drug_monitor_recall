"""Synchronous SQLAlchemy sessions for graph threads and command-line scripts."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """Create a pooled engine on first use; never log the database password."""
    return create_engine(str(get_settings().database_url), pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Return a reusable session factory with explicit transaction ownership."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """Legacy synchronous dependency; API routes use app.db.async_session instead."""
    with get_session_factory()() as session:
        yield session

