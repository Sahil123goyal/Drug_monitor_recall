"""Synchronous openFDA enforcement search with bounded retries and persistence."""

import re
import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.core.config import OpenFDASettings
from app.schemas.notice import Notice
from app.services.notice_store import NoticeStore, SQLAlchemyNoticeStore
from app.sources.base import SourceError

MAX_SKIP = 25_000
MAX_RETRY_DELAY = 60.0
logger = logging.getLogger(__name__)


class _ResultCount(BaseModel):
    total: int = Field(ge=0, strict=True)


class _Meta(BaseModel):
    results: _ResultCount


class _Page(BaseModel):
    meta: _Meta
    results: list[dict[str, Any]]


def _quoted(value: str) -> str:
    """Escape Lucene reserved characters, then search the input as a phrase."""
    escaped = re.sub(r'([+\-=&|><!(){}\[\]^"~*?:\\/])', r"\\\1", value)
    return f'"{escaped}"'


def _report_date(value: Any) -> date | None:
    """An absent or malformed date is missing evidence, never today's date."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


class OpenFDAAdapter:
    """Fetch all accessible search pages and commit each before returning it.

    A supplied HTTP client remains owned by its caller. Default clients are
    closed after each search. Retries apply only to HTTP, never database writes.
    """

    def __init__(
        self,
        *,
        settings: OpenFDASettings | None = None,
        store: NoticeStore | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings if settings is not None else OpenFDASettings()
        self.store = store if store is not None else SQLAlchemyNoticeStore()
        self._client = client
        self._sleep = sleep

    def fetch_notices(self, query: str) -> list[Notice]:
        """Search product_description and persist every successfully mapped page."""
        query = query.strip()
        if not query:
            raise ValueError("Provide a nonblank medicine or product-description query.")
        try:
            if self._client is not None:
                return self._fetch(self._client, query)
            with httpx.Client(follow_redirects=False) as client:
                return self._fetch(client, query)
        except SourceError as exc:
            # Never log authenticated URLs, raw response bodies or household input.
            logger.error("FETCH_FAILED source=openFDA category=%s reason=%s", exc.category, str(exc),
                         extra={"event": "FETCH_FAILED", "category": exc.category})
            raise

    def _fetch(self, client: httpx.Client, query: str) -> list[Notice]:
        notices: dict[str, Notice] = {}
        offset = 0
        while True:
            if offset > MAX_SKIP:
                raise SourceError(
                    "Search exceeds openFDA's paging limit; narrow the query. "
                    "Already fetched pages were saved, but this search is incomplete."
                )
            params = {
                "search": f"product_description:{_quoted(query)}",
                "sort": "report_date:desc",
                "limit": str(self.settings.openfda_page_size),
                "skip": str(offset),
            }
            key = self.settings.openfda_api_key.get_secret_value()
            if key:
                params["api_key"] = key
            payload = self._request(client, params)
            if payload is None:
                if offset:
                    raise SourceError("openFDA results disappeared during pagination; retry.")
                return []
            try:
                page = _Page.model_validate(payload)
            except ValidationError:
                raise SourceError("openFDA returned an invalid results envelope.") from None
            if not page.results:
                if offset == 0 and page.meta.results.total == 0:
                    return []
                raise SourceError("openFDA returned an incomplete results page.")
            fetched_at = datetime.now(timezone.utc)
            mapped = [self._map_record(record, fetched_at) for record in page.results]
            self.store.save(mapped)
            notices.update((notice.recall_number, notice) for notice in mapped)
            offset += len(page.results)
            if offset >= page.meta.results.total:
                return list(notices.values())

    def _request(
        self, client: httpx.Client, params: dict[str, str]
    ) -> dict[str, Any] | None:
        """Retry timeouts, transport errors, 408/429 and 5xx, without hiding failures."""
        for attempt in range(self.settings.openfda_max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = client.get(
                    self.settings.openfda_base_url,
                    params=params,
                    timeout=httpx.Timeout(self.settings.openfda_timeout_seconds),
                )
            except httpx.TransportError:
                error = "openFDA request timed out or could not connect."
            else:
                if response.status_code == 404:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    if (
                        isinstance(payload, dict)
                        and isinstance(payload.get("error"), dict)
                        and payload["error"].get("code") == "NOT_FOUND"
                        and payload["error"].get("message") == "No matches found!"
                    ):
                        return None
                    raise SourceError("openFDA returned an unexpected HTTP 404.")
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        raise SourceError("openFDA returned invalid JSON.") from None
                    if not isinstance(payload, dict):
                        raise SourceError("openFDA returned an invalid results envelope.")
                    return payload
                if response.status_code not in {408, 429} and not (
                    500 <= response.status_code <= 599
                ):
                    # Do not expose response bodies or authenticated request URLs.
                    raise SourceError(f"openFDA request failed (HTTP {response.status_code}).")
                error = f"openFDA unavailable (HTTP {response.status_code})."
            if attempt == self.settings.openfda_max_retries:
                raise SourceError(f"{error} Retry limit reached.", category=(
                    "network_error" if response is None else "http_error"
                )) from None
            self._sleep(self._retry_delay(response, attempt))
        raise AssertionError("Retry loop must return or raise")

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        delay = min(2.0 ** attempt, MAX_RETRY_DELAY)
        if response is not None and (header := response.headers.get("Retry-After")):
            try:
                if header.strip().isdigit():
                    requested = float(header)
                else:
                    deadline = parsedate_to_datetime(header)
                    requested = (deadline - datetime.now(timezone.utc)).total_seconds()
                delay = max(delay, requested)
            except (ValueError, TypeError, OverflowError):
                pass  # Malformed headers fall back to exponential backoff.
        if delay > MAX_RETRY_DELAY:
            raise SourceError("openFDA requested a long Retry-After delay; try again later.")
        return delay

    def _map_record(self, record: dict[str, Any], fetched_at: datetime) -> Notice:
        recall_number = record.get("recall_number")
        if not isinstance(recall_number, str) or not recall_number.strip():
            raise SourceError("openFDA record is missing a usable recall_number.")
        # Official record-specific API URL; deliberately excludes any API key.
        url = httpx.URL(self.settings.openfda_base_url, params={
            "search": f"recall_number:{_quoted(recall_number)}", "limit": "1"
        })
        try:
            return Notice(
                recall_number=recall_number,
                product_description=record.get("product_description"),
                code_info=record.get("code_info"),
                status=record.get("status"),
                report_date=_report_date(record.get("report_date")),
                url=str(url),
                fetched_at=fetched_at,
                raw_json=record,
            )
        except ValidationError:
            raise SourceError("openFDA record has invalid evidence field types.") from None

