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

import json
import logging
import re
from pathlib import Path

from src.llm.client import call_llm

logger = logging.getLogger(__name__)

_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "query_expander.txt"
)
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")


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
        raw = await call_llm(
            system_prompt=system_prompt,
            user_message=f"Generate {n} alternatives for: {rewritten_query}",
            temperature=0.3,   # Some creativity, but grounded
            # Qwen3-14B's thinking trace eats into max_tokens before the JSON
            # array appears, and this server ignores enable_thinking=False —
            # 200 was truncating to an empty response every time.
            max_tokens=800,
        )
    except Exception as exc:
        logger.warning("Query expander LLM call failed: %s — skipping expansion", exc)
        return []

    # Parse the JSON array from the response
    cleaned = raw.strip()

    # Strip markdown fences if present
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    try:
        variants = json.loads(cleaned)
        if not isinstance(variants, list):
            logger.warning(
                "Query expander returned non-list JSON: %s", cleaned[:100]
            )
            return []

        # Filter to non-empty strings only, cap at n
        result = [v for v in variants if isinstance(v, str) and v.strip()][:n]
        logger.debug(
            "Query expansion: '%s' -> %d variants: %s",
            rewritten_query[:50], len(result), result
        )
        return result

    except json.JSONDecodeError as exc:
        logger.warning(
            "Query expander returned invalid JSON: %s — raw: %s", exc, cleaned[:100]
        )
        return []
