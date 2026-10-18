"""Transport model, separate from the SQLAlchemy Notice database entity."""

from datetime import date
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl


class Notice(BaseModel):
    """A recall candidate with source evidence; this is not a matching verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = "openFDA"
    recall_number: str = Field(min_length=1)
    product_description: str | None = None
    code_info: str | None = None
    status: str | None = None
    report_date: date | None = None
    url: HttpUrl
    fetched_at: AwareDatetime
    raw_json: dict[str, Any]

