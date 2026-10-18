"""Fresh HTTP reads must confirm the exact source record and approved snapshot."""

from datetime import date, datetime, timezone

import httpx
import pytest

from app.core.config import OpenFDASettings
from app.models.enums import VerificationResult
from app.schemas.medicine import Medicine
from app.schemas.notice import Notice
from app.services.verification import OpenFDAVerifier


def inputs():
    return Medicine(name="DemoRelief", strength="500mg", batch_no="SYN001"), Notice(
        recall_number="SYN-VERIFY", product_description="DemoRelief 500mg", code_info="Lot SYN001",
        report_date=date(2026, 1, 15), status="Ongoing", raw_json={},
        url="https://api.fda.gov/drug/enforcement.json?search=recall_number:SYN-VERIFY&limit=1",
        fetched_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize("update, expected", [
    ({}, VerificationResult.CONFIRMED),
    ({"code_info": "Lot SYN0019"}, VerificationResult.NOT_CONFIRMED),
    ({"report_date": "20260116"}, VerificationResult.NOT_CONFIRMED),
    ({"status": "Terminated"}, VerificationResult.NOT_CONFIRMED),
    ({"report_date": None}, VerificationResult.UNCERTAIN),
    ({"report_date": "20260230"}, VerificationResult.UNCERTAIN),
    ({"product_description": None}, VerificationResult.UNCERTAIN),
    ({"recall_number": "OTHER-RECALL"}, VerificationResult.NOT_CONFIRMED),
])
def test_verifies_fresh_response_against_approved_snapshot(update, expected):
    medicine, notice = inputs()
    record = {"recall_number": "SYN-VERIFY", "product_description": "DemoRelief 500mg",
              "code_info": "Lot SYN001", "status": "Ongoing", "report_date": "20260115"} | update
    requests = []
    def handler(request):
        requests.append(request)
        assert str(request.url) == str(notice.url)
        return httpx.Response(200, json={"results": [record]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        verifier = OpenFDAVerifier(client=client, settings=OpenFDASettings(_env_file=None, openfda_api_key=""))
        result = verifier.verify(medicine, notice)
    assert result.result == expected
    assert len(requests) == 1
    assert result.url_opened == str(notice.url)
    if expected == VerificationResult.CONFIRMED:
        assert result.batch_found is True
        assert result.notice_date == date(2026, 1, 15)


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": "https://example.com/login"}),
    httpx.Response(200, text="CAPTCHA required"), httpx.Response(503),
])
def test_redirect_auth_pages_and_errors_remain_visible(response):
    medicine, notice = inputs()
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        result = OpenFDAVerifier(client=client).verify(medicine, notice)
    assert result.result == VerificationResult.UNCERTAIN


def test_unofficial_url_is_not_opened():
    medicine, notice = inputs()
    notice = notice.model_copy(update={"url": "http://localhost/private"})
    def forbidden(_):
        pytest.fail("An unofficial URL must not be opened")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        result = OpenFDAVerifier(client=client).verify(medicine, notice)
    assert result.result == VerificationResult.UNCERTAIN
    assert result.url_opened is None
