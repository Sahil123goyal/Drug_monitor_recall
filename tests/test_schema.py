"""Offline checks for PostgreSQL DDL and complete model registration."""

from sqlalchemy import create_mock_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import configure_mappers
from sqlalchemy.sql.ddl import ExecutableDDLElement

import app.models  # noqa: F401
from app.db.base import Base
from app.models import Action, Medicine, Verification, Verdict


def test_postgres_schema_compiles_without_a_live_database() -> None:
    """Compile native enum, UUID, JSONB, indexes, and foreign-key definitions."""
    statements: list[str] = []

    def capture(sql: ExecutableDDLElement, *args: object, **kwargs: object) -> None:
        statements.append(str(sql.compile(dialect=postgresql.dialect())))

    engine = create_mock_engine("postgresql+psycopg://", capture)
    configure_mappers()
    Base.metadata.create_all(engine)
    ddl = "\n".join(statements)
    assert set(Base.metadata.tables) == {
        "medicines", "notices", "decisions", "actions", "verifications", "test_runs", "runs"
    }
    assert "JSONB" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "UNIQUE (idempotency_key)" in ddl
    assert "ON DELETE RESTRICT" in ddl
    assert "CREATE TYPE decision_verdict" in ddl


def test_unknown_evidence_and_pending_approval_are_representable() -> None:
    """Missing evidence must not be forced into a false match or approved state."""
    assert Medicine.__table__.c.batch_no.nullable
    assert Verification.__table__.c.batch_found.nullable
    assert Action.__table__.c.approved_message.nullable
    assert str(Action.__table__.c.status.server_default.arg) == "'PENDING_APPROVAL'"
    assert {verdict.value for verdict in Verdict} == {"MATCH", "NO_MATCH", "UNCERTAIN"}

