"""Household input used by the pure matcher; identifiers live in the ORM model."""

from pydantic import BaseModel, ConfigDict


class Medicine(BaseModel):
    """An incomplete batch is allowed so the workflow can request user input."""

    model_config = ConfigDict(from_attributes=True, frozen=True, extra="forbid")

    name: str
    strength: str
    batch_no: str | None = None

