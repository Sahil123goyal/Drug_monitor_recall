"""Optional Groq fact extraction followed by the deterministic matching rules."""

import logging
from collections.abc import Callable

from app.core.config import GroqSettings
from app.models.enums import Verdict
from app.schemas.decision import Decision
from app.schemas.extraction import ExtractedNotice
from app.schemas.medicine import Medicine
from app.schemas.notice import Notice
from app.services.extraction import extract_notice_fields
from app.services.matcher import match, match_extracted

logger = logging.getLogger(__name__)
NoticeExtractor = Callable[[str], ExtractedNotice | None]


def match_notice(
    medicine: Medicine, notice: Notice, *, settings: GroqSettings | None = None,
    extractor: NoticeExtractor | None = None,
) -> Decision:
    """Use Groq only for uncertain notice evidence, never to choose a verdict.

    No household information is sent to the extractor. Missing configuration,
    failed extraction, invented fields and ambiguous source evidence cannot
    upgrade an UNCERTAIN decision into an alert. Explicitly parsed decisions
    remain deterministic and do not consume LLM calls.
    """
    result = match(medicine, notice)
    if result.verdict != Verdict.UNCERTAIN or result.evidence.get("requires_user_input"):
        result.evidence["extraction_status"] = "not_needed"
        return result
    config = settings if settings is not None else GroqSettings()
    if not config.groq_extraction_enabled:
        result.evidence["extraction_status"] = "disabled"
        return result
    if not config.groq_api_key.get_secret_value() or not config.groq_model.strip():
        result.evidence["extraction_status"] = "missing_configuration"
        logger.warning("GROQ_EXTRACTION_SKIPPED notice_id=%s reason=missing_configuration", notice.recall_number)
        return result

    # Only the official product description and batch evidence cross this boundary.
    text = (notice.product_description or "") + "\n" + (notice.code_info or "")
    logger.info("GROQ_EXTRACTION_STARTED notice_id=%s", notice.recall_number)
    fields = (extractor(text) if extractor is not None
              else extract_notice_fields(text, settings=config))
    result = match_extracted(medicine, notice, fields, min_confidence=config.groq_min_confidence)
    result.evidence["extraction_status"] = (
        "validated" if result.verdict != Verdict.UNCERTAIN else "unusable"
    )
    result.evidence["extraction_model"] = config.groq_model
    logger.info("GROQ_EXTRACTION_COMPLETED notice_id=%s status=%s",
                notice.recall_number, result.evidence["extraction_status"])
    return result
