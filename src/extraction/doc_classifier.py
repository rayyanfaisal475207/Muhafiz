# ============================================================
# Document-type classification — Qwen3-14B (Phase 4.4)
#
# Classifies a WHOLE document into one of the 7 known types. Separate from
# structured_fields.py (4.3, identifiers) and ner.py/domain_entities.py
# (4.5/4.6, entities within a document) — this classifies the document
# itself. Date extraction here is regex-validated (4.3), never taken as-is
# from the LLM: the model can name a plausible-looking date that isn't
# actually in the text.
#
# case_id is NEVER inferred here or anywhere in this module — it always
# comes from Phase 1's ingestion-time attachment (ingest_file's case_id
# param). A wrong content-based inference would leak evidence across cases.
# ============================================================

import logging
import re
from pathlib import Path
from typing import Optional

from src.extraction import structured_fields as sf
from src.extraction.llm_json import parse_json_response
from src.llm.client import call_llm

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "doc_classifier.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# Module 4.3: `dates[0]` (the first date-shaped substring anywhere in the
# document) is not necessarily the actual registration date — a document
# often mentions an earlier incident date before its own registration date.
# Prefer a date found near one of these labels instead; only fall back to
# the first-date heuristic when no labeled date is found.
_DATE_LABEL_RE = re.compile(
    r"date[\s_]+registered|date[\s_]+of[\s_]+registration|registration[\s_]+date|\bdated\b|fir[\s_]+date",
    re.IGNORECASE,
)
# How many characters on either side of a date match count as "near" a label.
_LABEL_PROXIMITY_CHARS = 40


def _find_registration_date(text: str, dates: list) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (date_registered, date_registered_confidence).

    confidence is "labeled" when a recognized label appears within
    _LABEL_PROXIMITY_CHARS BEFORE the chosen date match, "unlabeled_fallback"
    when falling back to the first date found with no nearby label, or
    None when the document has no date-shaped text at all.

    Only the window before the date is checked, not after: this corpus's
    field convention is always "Label: value" (e.g. "Date Registered:
    2026-01-20", "Report No.: TEST-2026-001"), and a label found near a
    LATER, unrelated date would otherwise incorrectly attach to an earlier
    date that happens to fall within the same proximity window.
    """
    for d in dates:
        window = text[max(0, d.start - _LABEL_PROXIMITY_CHARS): d.start]
        if _DATE_LABEL_RE.search(window):
            return d.normalized, "labeled"
    if dates:
        return dates[0].normalized, "unlabeled_fallback"
    return None, None

DOC_TYPES = (
    "FIR",
    "Complaint Application",
    "Case Diary",
    "Charge Sheet",
    "Witness Statement",
    "Recovery Memo",
    "Missing Person Report",
)

# Truncated to keep the classification call cheap — doc-type is decided by
# structure/register in the opening portion of a document in this corpus
# (form fields, opening IO-voice framing), not by content buried at the end.
_MAX_CHARS = 2000


async def classify_document(text: str) -> Optional[dict]:
    """
    Classify a document's type via Qwen3-14B, with a regex-validated date
    extracted alongside (4.3's extract_dates, not LLM-produced).

    Returns:
        {"doc_type": str, "confidence": float, "reasoning": str,
         "date_registered": str | None,
         "date_registered_confidence": "labeled" | "unlabeled_fallback" | None}
        or None if the LLM call/parse failed entirely (logged, not raised
        — one document's classification failure must not abort a batch,
        per 4.10's per-document resilience requirement).
    """
    if not text or not text.strip():
        return None

    snippet = text[:_MAX_CHARS]

    try:
        response = await call_llm(
            system_prompt=_SYSTEM_PROMPT,
            user_message=snippet,
            temperature=0.0,
            # Qwen3-14B's thinking trace consumes max_tokens before the
            # JSON answer; 800 is the floor that avoids truncating into
            # the parse-failure path (see sql_extractor.py's identical note).
            max_tokens=800,
        )
    except Exception as exc:
        logger.error("doc_classifier LLM call failed: %s", exc)
        return None

    parsed = parse_json_response(response, context="doc_classifier")
    if not parsed or "doc_type" not in parsed:
        return None

    doc_type = parsed.get("doc_type")
    if doc_type not in DOC_TYPES:
        logger.warning("doc_classifier returned an unknown doc_type %r — discarding", doc_type)
        return None

    dates = sf.extract_dates(text)
    date_registered, date_registered_confidence = _find_registration_date(text, dates)

    return {
        "doc_type": doc_type,
        "confidence": float(parsed.get("confidence", 0.0) or 0.0),
        "reasoning": parsed.get("reasoning", ""),
        "date_registered": date_registered,
        "date_registered_confidence": date_registered_confidence,
    }
