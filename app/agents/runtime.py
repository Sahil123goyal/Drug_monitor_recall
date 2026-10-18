"""Production graph factory: durable PostgreSQL checkpoints and LangSmith tracing."""

from collections.abc import Iterator
from contextlib import contextmanager

from dotenv import load_dotenv
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.engine import make_url

from app.agents.graph import build_graph
from app.agents.repository import WorkflowRepository
from app.core.config import PROJECT_ROOT, get_settings
from app.db.session import get_session_factory
from app.services.notice_store import SQLAlchemyNoticeStore
from app.services.verification import OpenFDAVerifier
from app.sources.openfda import OpenFDAAdapter


def checkpoint_dsn(database_url: str) -> str:
    """Psycopg accepts postgresql://, not SQLAlchemy's postgresql+psycopg://."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def open_agent(*, setup: bool = True) -> Iterator[CompiledStateGraph]:
    """Keep the checkpointer connection open throughout invoke/get_state.

    LangGraph automatically traces all nodes when LANGSMITH_TRACING=true. Loading
    .env into the environment is necessary because BaseSettings alone does not do it.
    """
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    sessions = get_session_factory()
    dsn = checkpoint_dsn(str(get_settings().database_url))
    with PostgresSaver.from_conn_string(dsn) as checkpointer:
        if setup:
            # Serialize schema setup across CLI/API processes during simultaneous starts.
            checkpointer.conn.execute("SELECT pg_advisory_lock(734910284)")
            try:
                checkpointer.setup()
            finally:
                checkpointer.conn.execute("SELECT pg_advisory_unlock(734910284)")
        yield build_graph(
            WorkflowRepository(sessions),
            {"openFDA": OpenFDAAdapter(store=SQLAlchemyNoticeStore(sessions))},
            OpenFDAVerifier(), checkpointer,
        )
