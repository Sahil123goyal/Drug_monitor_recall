"""Deterministic candidate matching; no LLM, database writes, or alerts.

Supported evidence: one distinct strength and explicit batch IDs. Unsupported
formats fail closed as UNCERTAIN rather than inferring missing recall evidence.
"""

import math
import re
import unicodedata
from datetime import date
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from pydantic import HttpUrl
from rapidfuzz.fuzz import ratio

from app.models.enums import Verdict
from app.schemas.decision import Decision
from app.schemas.extraction import ExtractedNotice

DEFAULT_BRAND_THRESHOLD = 90.0


class MedicineLike(Protocol):
    """Both the Pydantic input and existing SQLAlchemy medicine satisfy this."""

    @property
    def name(self) -> str: ...

    @property
    def strength(self) -> str: ...

    @property
    def batch_no(self) -> str | None: ...


class NoticeLike(Protocol):
    """Fields shared by the source payload and stored notice."""

    @property
    def product_description(self) -> str | None: ...

    @property
    def code_info(self) -> str | None: ...

    @property
    def url(self) -> str | HttpUrl: ...

    @property
    def report_date(self) -> date | None: ...


def normalize_name(value: str) -> str:
    """Lowercase Unicode text, replace punctuation with spaces, collapse spaces."""
    value = unicodedata.normalize("NFKC", value).lower()
    return " ".join(
        "".join(char if char.isalnum() or char.isspace() else " " for char in value).split()
    )


_NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
_UNIT = r"(?:mcg|mg|ug|g|ml|l|iu|units?|%)"
_STRENGTH = re.compile(
    rf"(?<![\w.,+-])(?P<amount>{_NUMBER})\s*(?P<unit>{_UNIT})(?!\w)"
    rf"(?:\s*(?:/|per\b)\s*(?P<denominator>{_NUMBER})?\s*"
    rf"(?P<denominator_unit>{_UNIT})(?!\w))?",
    re.IGNORECASE,
)


def _strength_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).lower().replace("μ", "u").replace("µ", "u")


def _quantity(value: str) -> str:
    # Decimal avoids binary floating-point rounding in exact strength comparisons.
    number = format(Decimal(value), "f")
    return number.rstrip("0").rstrip(".") if "." in number else number


def _unit(value: str) -> str:
    return {"ug": "mcg", "unit": "units"}.get(value, value)


def _canonical_strength(found: re.Match[str]) -> str:
    result = _quantity(found["amount"]) + _unit(found["unit"])
    if found["denominator_unit"]:
        result += "/" + _quantity(found["denominator"] or "1")
        result += _unit(found["denominator_unit"])
    return result


def normalize_strength(value: str) -> str | None:
    """Normalize a whole dose/concentration; do not convert mass or volume units."""
    found = _STRENGTH.fullmatch(_strength_text(value).strip())
    return _canonical_strength(found) if found else None


def _notice_strengths(description: str) -> tuple[list[str], str]:
    text = _strength_text(description)
    found = list(_STRENGTH.finditer(text))
    # Do not read an unsupported concentration as its numerator alone.
    if any(re.match(r"\s*/", text[item.end():]) for item in found):
        return [], ""
    return sorted({_canonical_strength(item) for item in found}), _STRENGTH.sub(" ", text)


def _brand_candidate(name: str, description: str) -> tuple[str, float]:
    """Score complete word windows, avoiding substring-only matches like AB/ABC."""
    words = normalize_name(description).split()
    width = len(name.split())
    best, score = "", 0.0
    # Adjacent window sizes accommodate spaces introduced by punctuation.
    for size in sorted({max(1, width - 1), width, width + 1}):
        for start in range(len(words) - size + 1):
            candidate = " ".join(words[start:start + size])
            current = ratio(name, candidate)
            if current > score:
                best, score = candidate, current
    return best, score


_LOT_LABEL = re.compile(
    r"\b(?:lots?|batches|batch)\b\s*(?:(?:numbers?|nos?\.?)\b\.?\s*)?[:#\s]*",
    re.IGNORECASE,
)
_AMBIGUOUS_CODES = re.compile(
    r"\b(?:all\s+(?:lots?|batches)|unknown|unavailable|various|"
    r"see|refer|attached|through|between|range|to|not\s+(?:available|provided|reported|specified))\b"
    r"|\bN\s*/\s*A\b|\*|\b[A-Za-z]*\d+\s*-\s*[A-Za-z]*\d+\b",
    re.IGNORECASE,
)
_METADATA = re.compile(
    r"\b(?:exp(?:iry|iration)?(?:\s+dates?)?|best\s+before|use\s+by|ndc|upc)\b"
    r".*?(?=\b(?:lots?|batches|batch)\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_BATCH_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*")


def _batch_tokens(code_info: str | None) -> list[str] | None:
    """Read explicit code lists, preserving case, punctuation and leading zeros.

    Bare lists require a digit in each ID. Labeled lists also accept uppercase
    alphabetic IDs. Prose, ranges and wildcard/all-lot statements are ambiguous.
    """
    if not code_info or not code_info.strip():
        return None
    # Expiry/NDC/UPC identifiers are not batch evidence.
    text = _METADATA.sub(" ", code_info)
    if _AMBIGUOUS_CODES.search(text):
        return None
    label = _LOT_LABEL.search(text)
    if label:
        text = text[label.start():]
    text = _LOT_LABEL.sub(" ", text)
    text = re.sub(r"\band\b", " ", text, flags=re.IGNORECASE)
    tokens = _BATCH_TOKEN.findall(text)
    remainder = _BATCH_TOKEN.sub("", text)
    if not tokens or re.search(r"[^\s,;:()\[\]#.]", remainder):
        return None
    if any(
        not any(char.isdigit() for char in token)
        and not (label and token.isalpha() and token.isupper())
        for token in tokens
    ):
        return None
    return list(dict.fromkeys(tokens))


def match(
    medicine: MedicineLike,
    notice: NoticeLike,
    *,
    brand_threshold: float = DEFAULT_BRAND_THRESHOLD,
) -> Decision:
    """Return MATCH, NO_MATCH, or UNCERTAIN with reproducible evidence.

    A missing household batch always takes precedence and requests user input.
    Unparseable notice evidence also takes precedence over negative comparisons.
    The caller must handle the pause/approval/verification workflow separately.
    """
    if not math.isfinite(brand_threshold) or not 0 < brand_threshold <= 100:
        raise ValueError("brand_threshold must be finite and greater than 0, up to 100.")
    batch = medicine.batch_no.strip() if medicine.batch_no else None
    name = normalize_name(medicine.name)
    strength = normalize_strength(medicine.strength)
    evidence: dict[str, Any] = {
        "medicine": {"name": medicine.name, "strength": medicine.strength, "batch_no": batch},
        "notice": {
            "url": str(notice.url),
            "report_date": notice.report_date.isoformat() if notice.report_date else None,
            "product_description": notice.product_description,
            "code_info": notice.code_info,
        },
        "brand": {"normalized": name, "candidate": None, "score": None,
                  "threshold": brand_threshold, "matched": None},
        "strength": {"normalized": strength, "found": [], "matched": None},
        "batch": {"tokens": [], "matched": None},
        "requires_user_input": False,
    }

    def decision(verdict: Verdict, reason: str) -> Decision:
        return Decision(verdict=verdict, reason=reason, evidence=evidence)

    if not batch:
        evidence["requires_user_input"] = True
        evidence["missing_fields"] = ["batch_no"]
        evidence["question"] = "What batch number is printed on your medicine package?"
        return decision(Verdict.UNCERTAIN, "Medicine batch number is missing; pause and ask the user.")
    if not _BATCH_TOKEN.fullmatch(batch):
        return decision(Verdict.UNCERTAIN, "Medicine batch number has an unsupported format.")
    if not name or not any(char.isalpha() for char in name) or strength is None:
        return decision(Verdict.UNCERTAIN, "Medicine brand or strength is missing or unparseable.")
    description = notice.product_description
    if not description or not any(char.isalpha() for char in description):
        return decision(Verdict.UNCERTAIN, "Notice product description is missing or unparseable.")
    strengths, brand_text = _notice_strengths(description)
    evidence["strength"]["found"] = strengths
    if len(strengths) != 1:
        return decision(Verdict.UNCERTAIN, "Notice needs one unambiguous, parseable strength.")
    candidate, score = _brand_candidate(name, brand_text)
    if not candidate:
        return decision(Verdict.UNCERTAIN, "Notice brand text is unparseable.")
    brand_matches = score >= brand_threshold
    evidence["brand"].update(candidate=candidate, score=score, matched=brand_matches)
    evidence["strength"]["matched"] = strength == strengths[0]
    tokens = _batch_tokens(notice.code_info)
    if tokens is None:
        return decision(Verdict.UNCERTAIN, "Notice batch evidence is missing or unparseable.")
    evidence["batch"].update(tokens=tokens, matched=batch in tokens)
    if not brand_matches:
        return decision(Verdict.NO_MATCH, "Brand similarity is below the configured threshold.")
    if batch not in tokens:
        return decision(Verdict.NO_MATCH, "Brand matches, but the exact batch is not in the notice.")
    if strength != strengths[0]:
        return decision(Verdict.NO_MATCH, "Brand and batch match, but strength differs.")
    return decision(Verdict.MATCH, "Brand, normalized strength, and exact batch all match.")


@dataclass(frozen=True)
class _StructuredNotice:
    """Internal adapter lets extracted fields use the same deterministic rules."""

    product_description: str
    code_info: str
    url: str | HttpUrl
    report_date: date | None


def match_extracted(
    medicine: MedicineLike,
    notice: NoticeLike,
    extraction: ExtractedNotice | None,
    *,
    brand_threshold: float = DEFAULT_BRAND_THRESHOLD,
    min_confidence: float = 0.9,
) -> Decision:
    """Match accepted extraction; failed extraction always yields UNCERTAIN.

    This function makes no LLM calls. The original match() remains a pure parser.
    Raw evidence is preserved, and fields are rechecked against this notice so an
    extraction from another source cannot be silently reused.
    """
    from app.services.extraction import contains_literal, extraction_is_usable

    original = match(medicine, notice, brand_threshold=brand_threshold)
    if original.evidence["requires_user_input"]:
        return original
    evidence = original.evidence.copy()
    evidence["extraction"] = extraction.model_dump(mode="json") if extraction else None
    evidence["extraction_min_confidence"] = min_confidence

    def uncertain(reason: str) -> Decision:
        return Decision(verdict=Verdict.UNCERTAIN, reason=reason, evidence=evidence)

    if extraction is None:
        return uncertain("Notice extraction failed or had low confidence.")
    description, codes = notice.product_description or "", notice.code_info or ""
    text = description + "\n" + codes
    if not extraction_is_usable(extraction, text, min_confidence=min_confidence):
        return uncertain("Extracted fields are incomplete, low confidence, or absent from this notice.")
    assert extraction.brand is not None and extraction.strength is not None
    # Brand/strength must come from the product description, batches from code_info.
    batch_text = _METADATA.sub(" ", codes)
    if (
        not contains_literal(description, extraction.brand)
        or not contains_literal(description, extraction.strength)
        or any(batch not in _BATCH_TOKEN.findall(batch_text) for batch in extraction.batch_numbers)
        or _AMBIGUOUS_CODES.search(batch_text)
    ):
        return uncertain("Extracted fields are not supported by the required notice fields.")
    strengths, _ = _notice_strengths(description)
    parsed_batches = _batch_tokens(codes)
    if strengths != [normalize_strength(extraction.strength)] or (
        parsed_batches is not None and set(parsed_batches) != set(extraction.batch_numbers)
    ):
        return uncertain("Extraction conflicts with or omits parseable source evidence.")
    structured = _StructuredNotice(
        product_description=f"{extraction.brand} {extraction.strength}",
        code_info="Lots: " + ", ".join(extraction.batch_numbers),
        url=notice.url,
        report_date=notice.report_date,
    )
    result = match(medicine, structured, brand_threshold=brand_threshold)
    result.evidence["notice"] = original.evidence["notice"]
    result.evidence["extraction"] = evidence["extraction"]
    result.evidence["extraction_min_confidence"] = min_confidence
    return result
