"""Deterministic HTTP adapter tests: no API keys, network calls, or real households."""

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import OpenFDASettings
from app.schemas.notice import Notice
from app.sources import OpenFDAAdapter, SourceError


class RecordingStore:
    def __init__(self) -> None:
        self.pages: list[list[Notice]] = []

    def save(self, notices: Sequence[Notice]) -> None:
        self.pages.append(list(notices))


def record(number: str = "SYN-0001") -> dict[str, Any]:
    return {
        "recall_number": number, "product_description": "Synthetic ibuprofen 200 mg",
        "code_info": "Batch 00042", "status": "Ongoing", "report_date": "20260115",
    }


def page(records: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {"meta": {"results": {"total": len(records) if total is None else total}},
            "results": records}


def settings(**overrides: Any) -> OpenFDASettings:
    return OpenFDASettings(_env_file=None, **{
        "openfda_api_key": SecretStr(""), "openfda_max_retries": 2,
        "openfda_page_size": 100, "openfda_timeout_seconds": 5, **overrides,
    })


def test_mapping_paging_persistence_and_secret_free_notice_link() -> None:
    requests: list[httpx.Request] = []
    store = RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["search"] == 'product_description:"ibuprofen"'
        assert request.url.params["api_key"] == "synthetic-key"
        assert request.extensions["timeout"]["read"] == 5
        skip = int(request.url.params["skip"])
        return httpx.Response(200, json=page([record(f"SYN-000{skip + 1}")], total=2))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenFDAAdapter(client=client, store=store, settings=settings(
            openfda_api_key=SecretStr("synthetic-key"), openfda_page_size=1
        ))
        notices = adapter.fetch_notices(" ibuprofen ")
    assert len(notices) == len(store.pages) == len(requests) == 2
    notice = notices[0]
    assert isinstance(notice, Notice)
    assert notice.product_description == "Synthetic ibuprofen 200 mg"
    assert notice.code_info == "Batch 00042"
    assert notice.status == "Ongoing"
    assert notice.report_date == date(2026, 1, 15)
    assert notice.fetched_at.tzinfo is not None
    assert notice.raw_json == record()
    url = httpx.URL(str(notice.url))
    assert url.host == "api.fda.gov"
    assert "api_key" not in url.params
    assert "synthetic-key" not in str(url)
    assert url.params["search"] == 'recall_number:"SYN\\-0001"'


@pytest.mark.parametrize("empty_response", [
    httpx.Response(200, json=page([])),
    httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "No matches found!"}}),
])
def test_empty_results_are_not_errors(empty_response: httpx.Response) -> None:
    store = RecordingStore()
    with httpx.Client(transport=httpx.MockTransport(lambda _: empty_response)) as client:
        assert OpenFDAAdapter(client=client, store=store, settings=settings()).fetch_notices("x") == []
    assert store.pages == []


def test_transient_failures_retry_then_save_once() -> None:
    sleeps: list[float] = []
    store = RecordingStore()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if attempts == 2:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json=page([record()]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        notices = OpenFDAAdapter(
            client=client, store=store, settings=settings(), sleep=sleeps.append
        ).fetch_notices("ibuprofen")
    assert len(notices) == len(store.pages) == 1
    assert attempts == 3
    assert sleeps == [1.0, 3.0]


@pytest.mark.parametrize("status, expected_attempts", [(503, 3), (401, 1), (400, 1), (403, 1)])
def test_failures_raise_instead_of_becoming_empty_results(status: int, expected_attempts: int) -> None:
    attempts = 0
    store = RecordingStore()

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, text="Never print this body: synthetic-secret")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceError) as error:
            OpenFDAAdapter(client=client, store=store, settings=settings(), sleep=lambda _: None).fetch_notices("x")
    assert attempts == expected_attempts
    assert store.pages == []
    assert "synthetic-secret" not in str(error.value)


def test_timeouts_exhaust_retries() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("synthetic", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceError, match="Retry limit"):
            OpenFDAAdapter(client=client, store=RecordingStore(), settings=settings(), sleep=lambda _: None).fetch_notices("x")
    assert attempts == 3


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"null"),
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, json={"results": []}),
    httpx.Response(200, json=page([], total=2)),
    httpx.Response(404, text="wrong endpoint"),
    httpx.Response(200, json=page([{"product_description": "No recall number"}])),
])
def test_malformed_responses_fail_closed(response: httpx.Response) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(SourceError):
            OpenFDAAdapter(client=client, store=RecordingStore(), settings=settings()).fetch_notices("x")


@pytest.mark.parametrize("value", [None, "", "20260230", "bad-date", "202611", 20260115])
def test_missing_or_invalid_dates_remain_unknown(value: Any) -> None:
    data = {"recall_number": "SYN-0001", "report_date": value}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page([data])))) as client:
        notice = OpenFDAAdapter(client=client, store=RecordingStore(), settings=settings()).fetch_notices("x")[0]
    assert notice.report_date is None
    assert notice.code_info is None
    assert notice.product_description is None
    assert notice.raw_json == data


def test_blank_query_does_not_make_a_request() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Blank query must be rejected before HTTP")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="nonblank"):
            OpenFDAAdapter(client=client, store=RecordingStore(), settings=settings()).fetch_notices("  ")


def test_query_is_escaped_as_a_product_description_phrase() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["search"] == 'product_description:"ibuprofen\\" OR status\\:ongoing"'
        return httpx.Response(200, json=page([]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        OpenFDAAdapter(client=client, store=RecordingStore(), settings=settings()).fetch_notices('ibuprofen" OR status:ongoing')


def test_retry_after_http_date_and_long_delay() -> None:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    response = httpx.Response(429, headers={"Retry-After": format_datetime(deadline)})
    assert 28 <= OpenFDAAdapter._retry_delay(response, 0) <= 30
    with pytest.raises(SourceError, match="try again later"):
        OpenFDAAdapter._retry_delay(httpx.Response(429, headers={"Retry-After": "120"}), 0)


def test_later_page_failure_keeps_saved_page_but_raises() -> None:
    store = RecordingStore()
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["skip"] == "0":
            return httpx.Response(200, json=page([record()], total=2))
        return httpx.Response(403)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceError):
            OpenFDAAdapter(client=client, store=store, settings=settings(openfda_page_size=1)).fetch_notices("x")
    assert len(store.pages) == 1


def test_database_failure_is_not_retried_or_reported_as_success() -> None:
    from sqlalchemy.exc import SQLAlchemyError

    class BrokenStore:
        def save(self, notices: Sequence[Notice]) -> None:
            raise SQLAlchemyError("Synthetic write failure")

    attempts = 0
    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json=page([record()]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SQLAlchemyError):
            OpenFDAAdapter(client=client, store=BrokenStore(), settings=settings()).fetch_notices("x")
    assert attempts == 1


def test_paging_limit_raises_instead_of_returning_partial_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.sources.openfda.MAX_SKIP", 0)
    store = RecordingStore()
    response = httpx.Response(200, json=page([record()], total=2))
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(SourceError, match="paging limit"):
            OpenFDAAdapter(client=client, store=store, settings=settings()).fetch_notices("x")
    assert len(store.pages) == 1

