"""Contract implemented by public recall sources."""

from typing import Protocol

from app.schemas.notice import Notice


class SourceError(Exception):
    """The source failed or returned incomplete/unusable data; not NO_MATCH."""

    def __init__(self, message: str, *, category: str = "source_error") -> None:
        super().__init__(message)
        self.category = category


class SourceAdapter(Protocol):
    """Fetch and persist candidates, or raise on an incomplete search."""

    def fetch_notices(self, query: str) -> list[Notice]:
        """Search product descriptions. A successful empty search returns []."""
        ...

