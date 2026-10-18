"""Run TC1–TC4 and persist evidence in test_runs. No human approvals are simulated."""

import argparse
import json
import sys
from pathlib import Path

from app.db.session import get_engine
from app.services.official_runner import run_official_suite


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["live", "snapshot"], default="live",
                        help="live fetches the exact FDA recall; snapshot replays the SHA-256 checked real response")
    parser.add_argument("--report", type=Path, help="Also write the saved results as JSON")
    args = parser.parse_args()
    try:
        records = run_official_suite(get_engine(), source=args.source)
    except Exception as exc:
        print(f"Cannot save the official suite ({type(exc).__name__}). Configure Postgres and run scripts.init_db. "
              "No successful completion is claimed; any previously committed case reports remain available.", file=sys.stderr)
        return 2
    report = json.dumps(records, indent=2, default=str)
    if args.report:
        args.report.write_text(report + "\n", encoding="utf-8")
    for row in records:
        label = "PASS" if row["passed"] else "FAIL"
        fixture = " [FIXTURE ONLY]" if row["evidence"]["fixture"] else ""
        print(f"{row['tc_name']}{fixture}: {label} — {row['evidence']['explanation']}")
    print("Saved four reports to test_runs. Alerts were inspected at the human-approval boundary; none were sent.")
    return 0 if all(row["passed"] for row in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
