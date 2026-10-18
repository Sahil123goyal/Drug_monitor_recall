"""CLI output and failure exit codes without network or database dependencies."""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import SQLAlchemyError

import fetch
from app.schemas.notice import Notice
from app.sources import SourceError


class StubAdapter:
    def __init__(self, result: list[Notice] | Exception) -> None:
        self.result = result

    def fetch_notices(self, query: str) -> list[Notice]:
        assert query == "ibuprofen"
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize("empty", [False, True])
def test_cli_outputs_json_after_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], empty: bool
) -> None:
    notices = [] if empty else [Notice(
        recall_number="SYN-CLI", url="https://api.fda.gov/drug/enforcement.json",
        fetched_at=datetime.now(timezone.utc), raw_json={"synthetic": True},
    )]
    monkeypatch.setattr(fetch, "get_settings", lambda: None)
    monkeypatch.setattr(fetch, "OpenFDAAdapter", lambda: StubAdapter(notices))
    assert fetch.main(["ibuprofen"]) == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["count"] == len(notices)
    assert output.err == ""
    if not empty:
        assert payload["notices"][0]["recall_number"] == "SYN-CLI"
        assert "raw_json" not in payload["notices"][0]


@pytest.mark.parametrize("failure", [
    SourceError("Source unavailable"), SQLAlchemyError("private-database-details"),
])
def test_cli_failure_is_nonzero_and_does_not_claim_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    monkeypatch.setattr(fetch, "get_settings", lambda: None)
    monkeypatch.setattr(fetch, "OpenFDAAdapter", lambda: StubAdapter(failure))
    assert fetch.main(["ibuprofen"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err
    assert "private-database-details" not in output.err

