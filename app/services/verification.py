"""Fresh official-notice reads, independent of the retrieval cache and Groq."""

from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import ValidationError

from app.agents.contracts import VerificationOutcome
from app.core.config import OpenFDASettings
from app.models.enums import Verdict, VerificationResult
from app.schemas.medicine import Medicine
from app.schemas.notice import Notice
from app.services.matcher import match


def validate_official_url(url: str) -> None:
    """Only reopen the configured public API; never follow redirects or auth pages."""
    parsed = httpx.URL(url)
    if (
        parsed.scheme != "https" or parsed.host != "api.fda.gov"
        or parsed.port not in (None, 443) or parsed.path != "/drug/enforcement.json"
        or parsed.username or parsed.password or "api_key" in parsed.params
    ):
        raise ValueError("Notice URL must be a credential-free official openFDA enforcement URL.")


class OpenFDAVerifier:
    """One fresh request per verification attempt; failures are returned for auditing."""

    def __init__(self, *, client: httpx.Client | None = None, settings: OpenFDASettings | None = None) -> None:
        self.client = client
        self.settings = settings if settings is not None else OpenFDASettings()

    def verify(self, medicine: Medicine, original: Notice) -> VerificationOutcome:
        if self.client is not None:
            return self._verify(self.client, medicine, original)
        with httpx.Client() as client:
            return self._verify(client, medicine, original)

    def _verify(self, client: httpx.Client, medicine: Medicine, original: Notice) -> VerificationOutcome:
        url = str(original.url)
        opened: str | None = None
        try:
            validate_official_url(url)
            key = self.settings.openfda_api_key.get_secret_value()
            # Credentials stay out of the stored URL and logs.
            response = client.get(
                url, timeout=self.settings.openfda_timeout_seconds,
                follow_redirects=False, auth=httpx.BasicAuth(key, "") if key else None,
            )
            opened = url
            if response.status_code != 200:
                return VerificationOutcome(opened, None, None, VerificationResult.UNCERTAIN,
                                           f"Notice reopening failed: HTTP {response.status_code}.")
            payload: Any = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise ValueError("Invalid API envelope")
            records = [record for record in payload["results"] if isinstance(record, dict)
                       and record.get("recall_number") == original.recall_number]
            if len(records) != 1:
                return VerificationOutcome(opened, None, None, VerificationResult.NOT_CONFIRMED,
                                           "The requested recall number was not uniquely found.")
            record = records[0]
            # Strict YYYYMMDD parsing; never substitute fetched_at for report_date.
            raw_date = record.get("report_date")
            report_date = None
            if isinstance(raw_date, str) and len(raw_date) == 8 and raw_date.isdigit():
                report_date = datetime.strptime(raw_date, "%Y%m%d").date()
            current = Notice(
                recall_number=record["recall_number"],
                product_description=record.get("product_description"),
                code_info=record.get("code_info"), status=record.get("status"),
                report_date=report_date, url=url,
                fetched_at=datetime.now(timezone.utc), raw_json=record,
            )
            decision = match(medicine, current)
            batch_found = decision.evidence["batch"]["matched"]
            if current.report_date is None or decision.verdict == Verdict.UNCERTAIN:
                return VerificationOutcome(opened, batch_found, current.report_date,
                                           VerificationResult.UNCERTAIN,
                                           "Fresh notice has missing or unparseable identity/date evidence.")
            if (
                decision.verdict != Verdict.MATCH or current.report_date != original.report_date
                or current.status != original.status
            ):
                return VerificationOutcome(opened, batch_found, current.report_date,
                                           VerificationResult.NOT_CONFIRMED,
                                           "Fresh notice identity, report date, or source status differs.")
            return VerificationOutcome(opened, True, current.report_date,
                                       VerificationResult.CONFIRMED,
                                       "Fresh official notice confirms brand, strength, batch, date and status.")
        except (httpx.HTTPError, ValueError, ValidationError):
            return VerificationOutcome(opened, None, None, VerificationResult.UNCERTAIN,
                                       "Notice reopening failed or the response was unparseable.")
