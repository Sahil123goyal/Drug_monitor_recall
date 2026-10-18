"""Official project acceptance cases and verified provenance for the real FDA record."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from app.core.config import OpenFDASettings, PROJECT_ROOT
from app.schemas.notice import Notice
from app.sources.openfda import OpenFDAAdapter

SUITE = "official-v1"
DATA = PROJECT_ROOT / "data" / "official_tests"
SourceMode = Literal["live", "snapshot"]


class TestDataError(ValueError):
    """Missing, altered, or incompatible evaluation evidence."""


def recorded_source() -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    path = (DATA / manifest["response_file"]).resolve()
    if path.parent != DATA.resolve():
        raise TestDataError("Snapshot path must remain inside the official test data directory.")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
        raise TestDataError("Recorded openFDA response failed its SHA-256 integrity check.")
    payload = json.loads(raw)
    records = payload.get("results", [])
    if len(records) != 1 or records[0].get("recall_number") != manifest["recall_number"]:
        raise TestDataError("Recorded response must contain exactly the selected recall.")
    return manifest, records[0]


class NoopStore:
    """Mapping alone performs no persistence; the graph saves notices in its sandbox."""
    def save(self, notices: list[Notice]) -> None:
        pass


def mapped_notice(record: dict[str, Any], retrieved_at: datetime) -> Notice:
    adapter = OpenFDAAdapter(settings=OpenFDASettings(_env_file=None), store=NoopStore())
    return adapter._map_record(record, retrieved_at)


def load_source(mode: SourceMode, client: httpx.Client | None = None) -> tuple[Notice, dict[str, Any]]:
    manifest, record = recorded_source()
    provenance = {"mode": mode, "official_url": manifest["url"], "snapshot_sha256": manifest["sha256"],
                  "snapshot_retrieved_at": manifest["retrieved_at"], "synthetic_household": True}
    if mode == "snapshot":
        return mapped_notice(record, datetime.fromisoformat(manifest["retrieved_at"])), provenance
    # Public API only, bounded adapter retries; never silently fall back to the snapshot.
    from datetime import timezone
    adapter = OpenFDAAdapter(settings=OpenFDASettings(_env_file=None), store=NoopStore())
    params = {"search": f'recall_number:"{manifest["recall_number"]}"', "limit": "1"}
    if client is None:
        with httpx.Client(follow_redirects=False) as owned:
            payload = adapter._request(owned, params)
    else:
        payload = adapter._request(client, params)
    records = payload.get("results", []) if payload else []
    if len(records) != 1 or records[0].get("recall_number") != manifest["recall_number"]:
        raise TestDataError("The selected recall was not uniquely returned by live openFDA.")
    now = datetime.now(timezone.utc)
    provenance["retrieved_at"] = now.isoformat()
    provenance["response_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return mapped_notice(records[0], now), provenance


@dataclass(frozen=True)
class OfficialCase:
    name: str
    description: str

    def inputs(self) -> dict[str, Any]:
        manifest, record = recorded_source()
        medicine = dict(manifest["medicine"])
        if self.name == "TC2":
            medicine["batch_no"] = manifest["different_batch"]
        elif self.name == "TC4":
            medicine["batch_no"] = None
        return {"medicine": medicine, "recall_number": record["recall_number"],
                "source_url": manifest["url"], "fixture": self.name == "TC3",
                "fixture_label": "FIXTURE: simulated Ongoing -> Terminated" if self.name == "TC3" else None}

    def expected(self) -> dict[str, Any]:
        _, record = recorded_source()
        notice_date = datetime.strptime(record["report_date"], "%Y%m%d").date().isoformat()
        if self.name == "TC1":
            return {"outcome": "DATED_ALERT_PROPOSED", "verdicts": ["MATCH"], "proposed_alerts": 1,
                    "notice_date": notice_date, "official_link_present": True, "approval_required": True,
                    "sent_alerts": 0}
        if self.name == "TC2":
            return {"outcome": "NO_ALERT", "verdicts": ["NO_MATCH"], "proposed_alerts": 0,
                    "completed": True, "sent_alerts": 0}
        if self.name == "TC3":
            return {"outcome": "ALERT_UPDATE_PROPOSED", "previous_status": "Ongoing", "current_status": "Terminated",
                    "message_changed": True, "new_action_key": True, "prior_alert_preserved": True,
                    "notice_date": notice_date, "official_link_present": True,
                    "approval_required": True, "sent_alerts": 0}
        return {"outcome": "USER_ASKED", "interrupt_kind": "collect", "retrieval_calls": 0,
                "proposed_alerts": 0, "sent_alerts": 0}


OFFICIAL_CASES = (
    OfficialCase("TC1", "Real recalled product and exact lot → dated alert"),
    OfficialCase("TC2", "Same brand and strength, different batch → no alert"),
    OfficialCase("TC3", "FIXTURE: status changes to Terminated → updated alert"),
    OfficialCase("TC4", "Missing household batch → ask the user"),
)
