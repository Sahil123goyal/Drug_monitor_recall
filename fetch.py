"""Fetch and persist recall candidates: python fetch.py \"ibuprofen\"."""

import argparse
import json
import sys
from collections.abc import Sequence

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.sources import OpenFDAAdapter, SourceError


def main(argv: Sequence[str] | None = None) -> int:
    """Print saved notices as JSON; failures exit nonzero without leaking secrets."""
    parser = argparse.ArgumentParser(description="Fetch openFDA recall candidates into Postgres.")
    parser.add_argument("query", help="Medicine name or product-description phrase")
    args = parser.parse_args(argv)
    try:
        get_settings()  # Fail early if DATABASE_URL is missing or invalid.
        notices = OpenFDAAdapter().fetch_notices(args.query)
    except ValidationError:
        print("Invalid configuration. Check DATABASE_URL and OPENFDA_* in .env.", file=sys.stderr)
        return 1
    except (SourceError, ValueError) as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print(
            "Database write failed. Check Postgres and run python -m scripts.init_db. "
            "Earlier pages may already have been saved.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({
        "count": len(notices),
        "notices": [notice.model_dump(mode="json", exclude={"raw_json"}) for notice in notices],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

