"""Serializable matching output, independent of persistence and alert delivery."""

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.enums import Verdict


class Decision(BaseModel):
    """Reason and JSON-compatible evidence can be copied into a decision row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Verdict
    reason: str
    evidence: dict[str, Any]

