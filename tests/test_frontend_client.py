"""HTTP mapping and failure handling for the UI boundary."""

import json
from uuid import uuid4

import httpx
import pytest

from frontend.api_client import APIClient, APIError


def test_edit_preserves_batch_tokens_and_does_not_change_household():
    medicine_id, household_id = uuid4(), uuid4()
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        return httpx.Response(200, json={**payload, "id": str(medicine_id), "household_id": str(household_id)})

    client = APIClient("http://test", transport=httpx.MockTransport(handler))
    medicine = client.save_medicine(household_id, " DemoRelief ", "500 mg", " 00-A001 ", medicine_id)
    assert medicine.batch_no == "00-A001"
    assert requests[0].method == "PUT" and requests[0].url.path == f"/medicines/{medicine_id}"
    assert "household_id" not in json.loads(requests[0].content)
    assert client.save_medicine(household_id, "DemoRelief", "500 mg", " ", medicine_id).batch_no is None


@pytest.mark.parametrize("status", [409, 422, 500])
def test_failure_is_not_retried_or_converted_to_empty_success(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"detail": "synthetic detail"})

    client = APIClient("http://test", transport=httpx.MockTransport(handler))
    with pytest.raises(APIError) as error:
        client.approve(uuid4(), "current", {str(uuid4()): False})
    assert len(requests) == 1
    assert "synthetic detail" not in str(error.value) if status == 500 else "synthetic detail" in str(error.value)


def test_bad_schema_is_visible_instead_of_trusting_response():
    client = APIClient("http://test", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "ok"})))
    with pytest.raises(APIError, match="unexpected response"):
        client.run(uuid4())
