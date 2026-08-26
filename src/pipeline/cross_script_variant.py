# ============================================================
# Cross-Script Query Variant — Retrieval-Only Translation
#
# PURPOSE (RETRIEVAL_CROSS_LINGUAL_FIX_PROMPT.md, Fix 3):
# Asking the same question in Urdu-script vs. English still surfaces
# different chunks/cases, even after Fix 1 (BM25 full-corpus scope) and
# Fix 2 (cross-case diversity capping) — because those two fixes only
# addressed retrieval DIVERSITY, not cross-lingual CONSISTENCY. The
# corpus is a genuine mix of Urdu-script and English documents, and:
#   1. BM25 is lexical/script-blind — an English query has zero token
#      overlap with Urdu-script chunk text, so it gets essentially no
#      BM25 signal against them (and vice versa).
#   2. multilingual-e5-large-instruct doesn't guarantee identical
#      nearest-neighbors across languages for the same meaning.
#
# This module generates ONE additional query variant, translated into
# "the other" script relative to the input, used ONLY to widen the
# retrieval candidate pool (embedded + BM25'd alongside the original and
# expand_query()'s paraphrases in orchestrator.py's RAG route). It is
# NEVER shown to the user and NEVER affects the final answer's language
# — prompts/query_rewriter.txt's "never translate the user-facing query"
# rule is about a different, user-visible string entirely.
#
# FAILURE MODE:
# On LLM error or empty response, returns None so the caller can fold it
# in only when present — mirrors query_expander.py's []-on-failure
# contract. No degradation: the pipeline behaves exactly as it did
# before this module existed if this call fails.
# ============================================================

import logging
import re
from pathlib import Path

from src.llm.client import call_llm

logger = logging.getLogger(__name__)

_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "cross_script_query.txt"
)
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")

# Urdu is written in the Arabic script. Same ranges as
# src/ingestion/tokenizer.py's _URDU_LETTERS, collapsed to plain Unicode
# block bounds since this is detection, not tokenization — a single
# match anywhere in the query is enough to call it Urdu-script.
_ARABIC_SCRIPT = re.compile(
    "[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]"
)


def _detect_script(text: str) -> str:
    """Return "urdu" if the query contains any Arabic-script character, else "latin"."""
    return "urdu" if _ARABIC_SCRIPT.search(text) else "latin"


async def generate_cross_script_variant(query: str) -> str | None:
    """
    Generate one translated/transliterated variant of `query` in "the
    other" script, for retrieval use only.

    Urdu-script input -> English variant. Latin-script input (English or
    Roman Urdu) -> Urdu-script variant.

    Returns the one-line variant string, or None on empty input, LLM
    failure, or an empty/whitespace-only response — the caller (the RAG
    route in orchestrator.py) treats None exactly like expand_query()
    returning [] for its variants: fold it in when present, otherwise
    proceed with what's already there.
    """
    if not query or not query.strip():
        return None

    script = _detect_script(query)
    target_script = "English" if script == "urdu" else "Urdu script"

    system_prompt = _PROMPT_TEMPLATE.replace("{target_script}", target_script).replace(
        "{query}", query
    )

    try:
        raw = await call_llm(
            system_prompt=system_prompt,
            user_message=f"Target script: {target_script}\nQuery: {query}",
            temperature=0.0,
            # Same Qwen3-14B thinking-trace consideration as every other
            # local call site in this codebase (see query_expander.py,
            # query_rewriter.py) — 800 turned out not to be enough on live
            # re-measurement, raised to 2000 for the LOCAL budget.
            # cloud_max_tokens pinned at the old 800 so the cloud fallback
            # is unaffected.
            max_tokens=2000,
            cloud_max_tokens=800,
        )
    except Exception as exc:
        logger.warning("Cross-script variant LLM call failed: %s — skipping", exc)
        return None

    variant = (raw or "").strip()
    if not variant:
        logger.warning("Cross-script variant returned empty response — skipping")
        return None

    logger.debug(
        "Cross-script variant: '%s' (%s) -> '%s' (%s)",
        query[:50], script, variant[:50], target_script,
    )
    return variant
