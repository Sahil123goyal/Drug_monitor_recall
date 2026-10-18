"""Create missing PostgreSQL tables and enums without deleting existing data."""

import app.models  # noqa: F401 -- registers every model with Base.metadata
from app.db.base import Base
from app.db.session import get_engine


def init_db() -> None:
    """Bootstrap the hackathon database; use migrations for later alterations."""
    engine = get_engine()
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(bind=connection, checkfirst=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    init_db()
    print("Database initialized: " + ", ".join(sorted(Base.metadata.tables)))

