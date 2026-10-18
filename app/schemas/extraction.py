"""Strict JSON contract for field extraction; verdicts are deliberately absent."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonblankText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
BatchNumber = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*$"),
]


class ExtractedNotice(BaseModel):
    """JSON-serializable facts and self-reported confidence, never a decision.

    Null/empty fields allow the LLM to acknowledge missing evidence. The service
    returns None unless all identity fields are present and pass validation.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    brand: NonblankText | None
    strength: NonblankText | None
    batch_numbers: list[BatchNumber]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

