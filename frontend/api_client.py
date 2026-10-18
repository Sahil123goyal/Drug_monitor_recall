"""Typed HTTP boundary. The UI never writes directly to the database."""

import os
from typing import Any, TypeVar
from uuid import UUID

import httpx
from pydantic import TypeAdapter, ValidationError

from app.schemas.api import MedicineResponse, RunResponse, RunSummary, TestRunResponse

T = TypeVar("T")


class APIError(Exception):
    """A safe, user-facing API failure."""


class APIClient:
    def __init__(self, base_url: str | None = None, *, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = (base_url or os.getenv("DRUG_MONITOR_API_URL", "http://127.0.0.1:8000")).rstrip("/")
        self.transport = transport

    def _request(
        self, method: str, path: str, adapter: TypeAdapter[T], *,
        payload: dict[str, Any] | None = None, params: dict[str, Any] | None = None,
    ) -> T:
        try:
            with httpx.Client(base_url=self.base_url, timeout=10, transport=self.transport) as client:
                response = client.request(method, path, json=payload, params=params)
        except httpx.HTTPError as exc:
            raise APIError("Cannot reach the API. Check that it is running. Refresh before retrying a submission.") from exc
        if response.status_code >= 400:
            message = f"API request failed (HTTP {response.status_code})."
            if response.status_code < 500:
                try:
                    detail = response.json().get("detail")
                    if isinstance(detail, str):
                        message = detail
                    elif isinstance(detail, list):
                        message = "; ".join(str(item.get("msg", "Invalid input")) for item in detail)
                except (ValueError, AttributeError):
                    pass
            raise APIError(message[:1200])
        try:
            return adapter.validate_python(response.json())
        except (ValueError, ValidationError) as exc:
            raise APIError("The API returned an unexpected response. Check that the API and UI versions match.") from exc

    def medicines(self, household_id: UUID) -> list[MedicineResponse]:
        return self._request("GET", "/medicines", TypeAdapter(list[MedicineResponse]),
                             params={"household_id": str(household_id)})

    def save_medicine(
        self, household_id: UUID, name: str, strength: str, batch_no: str,
        medicine_id: UUID | None = None,
    ) -> MedicineResponse:
        payload = {"name": name.strip(), "strength": strength.strip(), "batch_no": batch_no.strip() or None}
        if medicine_id:
            return self._request("PUT", f"/medicines/{medicine_id}", TypeAdapter(MedicineResponse), payload=payload)
        return self._request("POST", "/medicines", TypeAdapter(MedicineResponse),
                             payload={"household_id": str(household_id), **payload})

    def runs(self, household_id: UUID) -> list[RunSummary]:
        return self._request("GET", "/runs", TypeAdapter(list[RunSummary]),
                             params={"household_id": str(household_id), "limit": 100})

    def start_run(self, household_id: UUID, destination: str) -> RunResponse:
        return self._request("POST", "/runs", TypeAdapter(RunResponse),
                             payload={"household_id": str(household_id), "destination": destination.strip()})

    def run(self, run_id: UUID) -> RunResponse:
        return self._request("GET", f"/runs/{run_id}", TypeAdapter(RunResponse))

    def provide_batch(self, run_id: UUID, interrupt_id: str, batch: str) -> RunResponse:
        return self._request("POST", f"/runs/{run_id}/provide-batch", TypeAdapter(RunResponse),
                             payload={"interrupt_id": interrupt_id, "batch_no": batch.strip()})

    def confirm_decisions(self, run_id: UUID, interrupt_id: str, choices: dict[str, bool]) -> RunResponse:
        return self._request("POST", f"/runs/{run_id}/confirm-decisions", TypeAdapter(RunResponse), payload={
            "interrupt_id": interrupt_id,
            "confirmations": [
                {"decision_id": identifier, "accepted": accepted}
                for identifier, accepted in choices.items()
            ],
        })

    def approve(self, run_id: UUID, interrupt_id: str, choices: dict[str, bool]) -> RunResponse:
        return self._request("POST", f"/runs/{run_id}/approve", TypeAdapter(RunResponse), payload={
            "interrupt_id": interrupt_id,
            "approvals": [{"action_id": identifier, "approved": approved} for identifier, approved in choices.items()],
        })

    def test_runs(self) -> list[TestRunResponse]:
        return self._request("GET", "/test-runs", TypeAdapter(list[TestRunResponse]), params={"suite": "official"})

    def evaluate(self) -> list[TestRunResponse]:
        return self._request("POST", "/test-runs/evaluate", TypeAdapter(list[TestRunResponse]),
                             params={"suite": "official", "source": "snapshot"})
