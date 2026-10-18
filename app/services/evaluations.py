"""TC1–TC4: repeatable, network-free cases using the real deterministic matcher."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.schemas.medicine import Medicine
from app.schemas.notice import Notice
from app.services.matcher import match


@dataclass(frozen=True)
class CoreCase:
    name: str
    description: str
    batch: str | None
    code_info: str
    verdict: str
    asks_for_batch: bool = False

    @property
    def id(self) -> UUID:
        return uuid5(NAMESPACE_URL, "drug-monitor/core-suite/v1/" + self.name)

    def inputs(self) -> dict[str, Any]:
        return {"medicine": {"name": "DemoRelief", "strength": "500mg", "batch_no": self.batch},
                "notice": {"product_description": "DemoRelief 500 mg tablets", "code_info": self.code_info}}

    def expected(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "requires_user_input": self.asks_for_batch}


CORE_CASES = (
    CoreCase("TC1", "Exact brand, strength, and batch", "SYN001", "Lots SYN001", "MATCH"),
    CoreCase("TC2", "Same brand and strength, different batch", "SYN002", "Lots SYN001", "NO_MATCH"),
    CoreCase("TC3", "Missing household batch: ask before checking", None, "Lots SYN001", "UNCERTAIN", True),
    CoreCase("TC4", "Unparseable notice batch evidence", "SYN001", "See the damaged package insert", "UNCERTAIN"),
)


def evaluate_core_cases() -> list[dict[str, Any]]:
    results = []
    for case in CORE_CASES:
        inputs = case.inputs()
        notice = Notice(
            recall_number="SYNTHETIC-" + case.name, **inputs["notice"], status="Ongoing",
            report_date=date(2026, 1, 15), fetched_at=datetime.now(timezone.utc),
            url="https://api.fda.gov/drug/enforcement.json?search=recall_number:SYNTHETIC-" + case.name,
            raw_json={"synthetic": True},
        )
        decision = match(Medicine.model_validate(inputs["medicine"]), notice)
        actual = {"verdict": decision.verdict.value,
                  "requires_user_input": bool(decision.evidence.get("requires_user_input", False))}
        results.append({"id": case.id, "tc_name": case.name, "input": inputs,
                        "expected": case.expected(), "actual": actual,
                        "evidence": {"synthetic": True, "reason": decision.reason, **decision.evidence},
                        "passed": actual == case.expected()})
    return results
