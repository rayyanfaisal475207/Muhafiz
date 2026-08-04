# ============================================================
# Domain-entity extraction — vehicle / weapon / gang-alias / informal role
# (Phase 4.6)
#
# Few-shot Qwen3-14B, fixed structured-output schema. Unlike NER (4.5),
# this is LLM-only — there is no cheap statistical/gazetteer signal for
# "is this a weapon description" the way there is for "is this preceded by
# ولد". The one exception is vehicle plates: a plate is a fixed-format
# structured identifier (like CNIC/phone/FIR#), so once the LLM names a
# vehicle mention, its plate is cross-validated against 4.3's regex
# extractor rather than trusted as LLM output — same rationale as CNIC
# throughout this phase: a transcribed-wrong plate silently breaks
# vehicle-based entity resolution (the near-miss-plate test cases, e.g.
# ICT-FL-273 vs ICT-FL-274, exist specifically to stress this).
#
# The LLM is asked for exact verbatim surface text, not character offsets
# — LLMs are unreliable at counting characters. This module locates each
# returned string in the source text by substring search instead (exact
# first, normalized-text fallback); a mention that can't be located gets
# `char_span=None`, not discarded — it's still a usable mention for
# resolution/graph-write even without a provenance span, and it's better
# to say "we don't know where" honestly than to guess.
# ============================================================

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.extraction import structured_fields as sf
from src.ingestion.text_normalizer import normalize_urdu
from src.llm.client import call_llm
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "domain_entities.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

ENTITY_TYPES = ("vehicle", "weapon", "organization", "person", "incident")

# Passage length cap per call — domain-entity mentions in this corpus
# cluster in FIR narratives and recovery memos, both well under this.
_MAX_CHARS = 3000


@dataclass
class DomainMention:
    text: str
    type: str                          # "vehicle" | "weapon" | "organization" | "person" | "incident"
    start: Optional[int]
    end: Optional[int]
    confidence: float
    source_chunk_id: Optional[str] = None
    attributes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "type": self.type,
            "char_span": [self.start, self.end] if self.start is not None else None,
            "source_chunk_id": self.source_chunk_id,
            "confidence": self.confidence,
            "attributes": self.attributes,
        }


def _locate(text: str, needle: str) -> Optional[tuple[int, int]]:
    """
    Find `needle` in `text`, exact first, then normalized-form fallback
    (the LLM sometimes echoes text with a different Arabic/Urdu script
    variant or digit form than the source — normalize_urdu() folds both to
    the same canonical form the same way every other extractor in this
    phase does). Returns None, not a guess, if it truly can't be found.
    """
    if not needle:
        return None
    idx = text.find(needle)
    if idx != -1:
        return idx, idx + len(needle)

    norm_text = normalize_urdu(text)
    norm_needle = normalize_urdu(needle)
    idx = norm_text.find(norm_needle)
    if idx != -1:
        # Offsets are only valid in the normalized text's coordinate space
        # (normalization isn't length-preserving) — callers that need
        # offsets into the original text must normalize it first, the same
        # discipline ingestion/service.py already follows.
        return idx, idx + len(norm_needle)
    return None


def _cross_validate_plate(mention_text: str, context: str, llm_plate: Optional[str]) -> Optional[str]:
    """Prefer a regex-verified plate over the LLM's own transcription."""
    regex_matches = sf.extract_plates(mention_text) or sf.extract_plates(context)
    if regex_matches:
        return regex_matches[0].normalized
    return llm_plate


async def extract_domain_entities(
    text: str, source_chunk_id: Optional[str] = None
) -> list[dict]:
    """
    Run the few-shot Qwen3-14B extraction over `text` and return the
    uniform mention shape shared with ner.py (4.5): {text, type, char_span,
    source_chunk_id, confidence, attributes}.

    Returns [] on any LLM/parse failure (logged, never raised) — matches
    4.10's per-document resilience requirement.

    2026-08-04: switched from a single call_llm() + parse_json_response()
    attempt to call_llm_json()'s retry-with-correction loop (the same
    mechanism router.py uses). Confirmed live: this was the actual root
    cause of the graph having zero Vehicle nodes despite this module
    correctly supporting "vehicle" as an entity type end-to-end — the local
    model (Qwen3-14B) frequently answers this prompt conversationally
    instead of with the required JSON array (e.g. "Here is a more formal
    and structured version of the information you provided..." for a
    passage plainly describing a stolen motorcycle with make/model/plate),
    and the old single-attempt path had no retry at all: one bad reply
    silently discarded every entity in that chunk, indistinguishable from
    "no entities present." This is the identical failure class router.py
    already had a fix for; domain_entities.py just never got it.
    """
    if not text or not text.strip():
        return []

    snippet = text[:_MAX_CHARS]

    try:
        parsed, response = await call_llm_json(
            system_prompt=_SYSTEM_PROMPT,
            user_message=snippet,
            temperature=0.0,
            max_tokens=800,
            validate=lambda r: isinstance(r, list),
            schema_hint='a JSON array of {"text", "type", "attributes", "confidence"} objects (or [] if none found)',
            _call_llm=call_llm,
        )
    except Exception as exc:
        logger.error("domain_entities LLM call failed: %s", exc)
        return []

    if not isinstance(parsed, list):
        logger.error("domain_entities: no valid JSON array after retries. Raw: %s", (response or "")[:500])
        return []

    out: list[DomainMention] = []
    for item in parsed:
        entity_text = (item.get("text") or "").strip()
        entity_type = item.get("type")
        if not entity_text or entity_type not in ENTITY_TYPES:
            continue

        attributes = item.get("attributes") or {}
        if entity_type == "vehicle":
            attributes["plate"] = _cross_validate_plate(
                entity_text, snippet, attributes.get("plate")
            )

        span = _locate(snippet, entity_text)
        start, end = span if span else (None, None)

        out.append(DomainMention(
            text=entity_text,
            type=entity_type,
            start=start,
            end=end,
            confidence=float(item.get("confidence", 0.5) or 0.5),
            source_chunk_id=source_chunk_id,
            attributes=attributes,
        ))

    return [m.to_dict() for m in out]
