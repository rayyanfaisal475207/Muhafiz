# ============================================================
# Query Expander — LLM-based Query Paraphrasing
#
# PURPOSE:
# Addresses semantic drift failures: when a user's query phrasing
# doesn't overlap well with the vocabulary used in the source documents,
# semantic search misses relevant chunks. This module generates N
# paraphrase variants so the orchestrator can run parallel retrievals
# and RRF-merge all result sets, dramatically improving recall.
#
# WHEN IT HELPS:
# - "documents needed for a lost CNIC report" drifted away from chunks
#   phrased as "required documents for lost report procedure". A variant
#   using the document's own phrasing would have surfaced it directly.
# - "PPC section for cyber harassment" drifted from chunks phrased around
#   "PECA 2016". A variant naming both statutes closes that gap.
#
# FAILURE MODE:
# On LLM error the function returns an empty list. The orchestrator
# then falls back to single-query retrieval — no degradation in quality,
# just no expansion benefit. This is the correct behaviour.
#
# LATENCY:
# One small LLM call (~100 token output). With Groq/LLaMA-3.3-70b this
# runs in ~200-400ms concurrently with the embedding call.
# ============================================================

import logging
import re
from pathlib import Path

from src.ingestion.script_detector import _ARABIC_SCRIPT
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "query_expander.txt"
)
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")

# Devanagari block (U+0900-U+097F) — this system's corpus and users never
# use it (English / Urdu-script / Roman-Urdu only). Live-caught (2026-08-04,
# D-2 fix verification): a Roman-Urdu query ("cyber harassment ke liye
# kaunsi section lagti hai") produced Devanagari/Hindi-script variants
# ("कैसे साइबर हरसमेंत के लिए...") despite an explicit prompt instruction
# not to translate — the prompt-only fix from Fix 6/7 wasn't reliable
# enough on its own, so this is a structural post-hoc filter, same pattern
# as query_rewriter.py's guards.
_DEVANAGARI_RE = re.compile("[ऀ-ॿ]")


def _script_class(text: str) -> str:
    if _DEVANAGARI_RE.search(text):
        return "devanagari"
    if _ARABIC_SCRIPT.search(text):
        return "arabic"
    return "latin"


async def expand_query(rewritten_query: str, n: int = 2) -> list[str]:
    """
    Generate N alternative phrasings of the query using an LLM.

    Returns a list of alternative query strings. On any error, returns
    an empty list so the caller can gracefully fall back to single-query
    retrieval.

    Args:
        rewritten_query: The standalone query from the query rewriter.
        n:               Number of paraphrase variants to generate (default 2).

    Returns:
        List of n alternative query strings, or [] on failure.
    """
    if not rewritten_query or not rewritten_query.strip():
        return []

    system_prompt = _PROMPT_TEMPLATE.replace("{n}", str(n)).replace(
        "{query}", rewritten_query
    )

    try:
        # call_llm_json retries with an explicit correction if Qwen3 answers
        # conversationally instead of with a JSON array — confirmed live:
        # "Sure! Here are two alternative phrasings for ...: 1. **...** 2.
        # **...**" instead of ["...", "..."]. A same-prompt retry reliably
        # repeats that; the correction forbids it.
        variants, raw = await call_llm_json(
            system_prompt=system_prompt,
            user_message=f"Generate {n} alternatives for: {rewritten_query}",
            temperature=0.3,   # Some creativity, but grounded
            # Qwen3-14B's thinking trace eats into max_tokens before the JSON
            # array appears, and this server ignores enable_thinking=False —
            # 200 was truncating to an empty response every time; 800 later
            # turned out not to be enough either on live re-measurement.
            # Raised to 2000 for the LOCAL budget; cloud_max_tokens pinned
            # at the old 800 so the cloud fallback is unaffected.
            max_tokens=2000,
            cloud_max_tokens=800,
            validate=lambda r: isinstance(r, list),
            schema_hint="a bare JSON array of strings, e.g. [\"alt phrasing 1\", \"alt phrasing 2\"]",
        )
    except Exception as exc:
        logger.warning("Query expander LLM call failed: %s — skipping expansion", exc)
        return []

    if variants is None:
        logger.warning("Query expander returned no valid JSON after retries: %s", raw[:100])
        return []

    # Filter to non-empty strings, then drop any variant that switched
    # script from the input query — a real, live-observed failure mode
    # (see _DEVANAGARI_RE above), not a hypothetical one. Devanagari is
    # rejected unconditionally regardless of input script since this
    # system never legitimately produces it; otherwise the variant must
    # match the input's own script class.
    input_class = _script_class(rewritten_query)
    result = []
    for v in variants:
        if not isinstance(v, str) or not v.strip():
            continue
        v_class = _script_class(v)
        if v_class == "devanagari" or v_class != input_class:
            logger.warning(
                "Query expander dropped a script-switched variant (%s -> %s): %r",
                input_class, v_class, v[:80],
            )
            continue
        result.append(v)
    result = result[:n]
    logger.debug(
        "Query expansion: '%s' -> %d variants: %s",
        rewritten_query[:50], len(result), result
    )
    return result
