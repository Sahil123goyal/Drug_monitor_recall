"""Minimal durable CLI: start, resume with a JSON reply, inspect, or retry a run."""

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from app.agents.runtime import open_agent
from app.db.session import get_session_factory
from app.models import Medicine


def _print_state(graph: Any, config: RunnableConfig) -> int:
    snapshot = graph.get_state(config)
    interrupts = [{"id": item.id, "value": item.value}
                  for task in snapshot.tasks for item in task.interrupts]
    errors = [str(task.error) for task in snapshot.tasks if task.error]
    print(json.dumps({"thread_id": config["configurable"]["thread_id"],
                      "next": list(snapshot.next), "interrupts": interrupts,
                      "errors": errors, "state": snapshot.values}, indent=2, default=str))
    return 1 if errors or snapshot.values.get("outcome") in {"FAILED", "NEEDS_REVIEW"} else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the synthetic-household recall workflow.")
    parser.add_argument("command", choices=["start", "resume", "status", "retry"])
    parser.add_argument("--thread-id", required=True, help="Reuse exactly this ID when resuming")
    parser.add_argument("--name", default="ibuprofen")
    parser.add_argument("--strength", default="200 mg")
    parser.add_argument("--batch", default=None)
    parser.add_argument("--destination", default="mock://synthetic-household")
    parser.add_argument("--reply-file", type=Path, help="JSON object answering the displayed interrupt")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config: RunnableConfig = {
        "configurable": {"thread_id": args.thread_id},
        "run_name": "drug-recall-monitor", "tags": ["hackathon", "synthetic-household"],
        "metadata": {"workflow": "drug-recall-monitor", "thread_id": args.thread_id},
    }
    try:
        with open_agent() as graph:
            previous = graph.get_state(config)
            if args.command == "start":
                if previous.values:
                    parser.error("This thread already exists; use status, resume, or a new --thread-id.")
                identifier = uuid5(NAMESPACE_URL, "drug-monitor-synthetic-medicine:" + args.thread_id)
                with get_session_factory().begin() as session:
                    existing = session.get(Medicine, identifier)
                    if existing is None:
                        session.add(Medicine(
                            id=identifier, household_id=UUID("00000000-0000-4000-8000-000000000001"),
                            name=args.name, strength=args.strength, batch_no=args.batch,
                        ))
                graph.invoke({"medicine_id": str(identifier), "destination": args.destination}, config)
            elif not previous.values:
                parser.error("Unknown thread; start it first.")
            elif args.command == "resume":
                if not args.reply_file or not any(task.interrupts for task in previous.tasks):
                    parser.error("resume requires a pending interrupt and --reply-file.")
                reply = json.loads(args.reply_file.read_text(encoding="utf-8-sig"))
                graph.invoke(Command(resume=reply), config)
            elif args.command == "retry":
                if any(task.interrupts for task in previous.tasks):
                    parser.error("Human input is pending; use resume with --reply-file.")
                if not previous.next:
                    parser.error("The graph has completed; inspect failures or start a new assessment.")
                graph.invoke(None, config)
            return _print_state(graph, config)
    except Exception as exc:
        # Config/database errors can contain credentials. Task details remain in
        # checkpoints; show only the exception type here and never erase failures.
        print(f"Workflow failed ({type(exc).__name__}). Check configuration/Postgres; "
              "use status with the same thread ID for recorded task failures.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
