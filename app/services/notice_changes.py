"""Stable notice identity for change detection and approved-alert deduplication."""

import hashlib
import json
from typing import Any

from app.services.matcher import _batch_tokens


def notice_content(status: str | None, code_info: str | None) -> dict[str, Any]:
    """Ignore list order/duplicates, preserving exact lot spelling and punctuation.

    Unparseable evidence is retained as text, never interpreted as an empty list.
    The deterministic matcher still returns UNCERTAIN for that evidence.
    """
    batches = _batch_tokens(code_info)
    return {
        "status": status.strip() if status else None,
        "batches": sorted(set(batches)) if batches is not None else None,
        "unparsed_code_info": " ".join((code_info or "").split()) if batches is None else None,
    }


def notice_fingerprint(status: str | None, code_info: str | None) -> str:
    payload = json.dumps(notice_content(status, code_info), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]
