# ============================================================
# Verifier Agent — Grounding, Hallucination & Off-topic Gate
#
# PURPOSE:
# A hard gate between generation and delivery. Every route that
# produces an answer from retrieved evidence (RAG, GRAPH,
# GRAPH_HYBRID, SQL, WEB, XGRAPH, XAGG) is verified here before
# the answer reaches the investigator.
#
# DESIGN (mirrors evaluator.py):
#   - call_llm() with role="reasoning" (Qwen3-14B) — same model/
#     cost as evaluator, max_tokens=800 for thinking-trace budget
#   - Deterministic pre-checks run in pure Python first (temporal
#     validity, cross-case leakage, confidence hedging) — these are
#     fast and never need an LLM
#   - LLM judge runs after pre-checks, verifying claim-by-claim
#     grounding and off-topic detection
#   - 2-attempt retry on JSON parse failure, then fail-closed:
#     default = not grounded (never best-effort-guess)
#
# CHECKS:
#   1. Claim-to-citation grounding   (LLM judge)
#   2. Off-topic / generic response  (LLM judge)
#   3. Cross-case leakage            (deterministic Python + LLM)
#   4. Confidence-appropriate hedging (deterministic Python + LLM)
#   5. Temporal validity             (deterministic Python)
#
# OUTPUT:
#   {
#     "grounded": bool,
#     "off_topic": bool,
#     "leaked_case_id": str | None,
#     "unsupported_claims": list[str],
#     "reason": str,
#   }
# ============================================================

import json
import logging
import re
from pathlib import Path
from typing import Optional

from src.llm.client import call_llm

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "verifier.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# Phrases that satisfy the hedging requirement for low-confidence / unconfirmed links.
_HEDGE_PHRASES = (
    "unconfirmed", "possible", "pending", "not yet verified",
    "under review", "flagged", "uncertain", "may be",
)

# Characters to scan around a [Document N] citation when checking hedging.
_HEDGE_WINDOW = 250


def _format_chunks_for_verifier(chunks: list[dict]) -> str:
    """
    Format cited chunks for the verifier prompt.
    Includes graph_confidence and case_id from metadata when present,
    so the LLM judge can apply the hedging rule (check 4) itself.
    """
    lines: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        meta = chunk.get("metadata") or {}
        source = (
            chunk.get("source_file")
            or meta.get("source")
            or "unknown"
        )
        text = chunk.get("chunk_text") or chunk.get("text") or ""
        graph_conf = chunk.get("graph_confidence")
        case_id_val = meta.get("case_id")

        header_parts = [f"[{i}] Source: {source}"]
        if case_id_val:
            header_parts.append(f"case_id: {case_id_val}")
        if graph_conf is not None:
            header_parts.append(f"graph_confidence: {graph_conf:.2f}")
        lines.append(" — ".join(header_parts))
        lines.append(f"Text: {text}")
        lines.append("")

    return "\n".join(lines)


def _check_temporal(chunks: list[dict], target_date: Optional[int]) -> list[str]:
    """
    Deterministic pre-check: flag chunks whose effective date range
    excludes the target_date (query year as int, e.g. 2025).
    Returns a list of issue strings (empty = no temporal problems).
    """
    if not target_date or not chunks:
        return []
    issues: list[str] = []
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        source = meta.get("source", "unknown")
        ef = meta.get("effective_from")
        et = meta.get("effective_to")
        if ef and ef > target_date:
            issues.append(
                f"Document '{source}' (effective_from={ef}) is not yet effective "
                f"for target date {target_date}."
            )
        elif et and et < target_date:
            issues.append(
                f"Document '{source}' (effective_to={et}) has expired "
                f"for target date {target_date}."
            )
    return issues


def _check_leakage(
    answer: str,
    chunks: list[dict],
    active_case_id: Optional[str],
    cross_case_ids: Optional[list[str]],
) -> Optional[str]:
    """
    Deterministic pre-check: scan [Document N] citations in the answer,
    map them back to chunks, and check that every cited chunk's case_id
    belongs to the active case or the explicitly allowed cross-case set.

    Returns the first offending case_id found, or None if clean.
    Logs an ERROR when leakage is found because it indicates a
    retrieval-time filtering bug, not just a generation quality issue.
    """
    if not active_case_id or active_case_id == "cross_case":
        return None

    allowed = set(cross_case_ids or [])
    allowed.add(active_case_id)

    # Match [Document N] tags in the answer (case-insensitive)
    for m in re.finditer(r"\[Document\s+(\d+)\]", answer, re.IGNORECASE):
        n = int(m.group(1))
        if n < 1 or n > len(chunks):
            continue
        chunk = chunks[n - 1]
        meta = chunk.get("metadata") or {}
        chunk_case = meta.get("case_id")
        if chunk_case and chunk_case not in allowed:
            logger.error(
                "SECURITY: Cross-case leakage detected — chunk '%s' (case_id='%s') "
                "cited in answer for case '%s'. This indicates a retrieval-time "
                "filtering bug.",
                chunk.get("id", f"chunk-{n}"),
                chunk_case,
                active_case_id,
            )
            return chunk_case
    return None


def _check_hedging(answer: str, chunks: list[dict]) -> list[str]:
    """
    Deterministic pre-check: for any chunk with graph_confidence < 0.85,
    verify that the answer contains a hedging phrase within _HEDGE_WINDOW
    characters of the [Document N] citation that references that chunk.
    Returns a list of issue strings (empty = hedging is adequate).
    """
    issues: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        gc = chunk.get("graph_confidence")
        if gc is None or gc >= 0.85:
            continue

        # Find all occurrences of [Document N] in the answer
        pattern = rf"\[Document\s+{i}\]"
        for m in re.finditer(pattern, answer, re.IGNORECASE):
            pos = m.start()
            window_start = max(0, pos - _HEDGE_WINDOW)
            window_end = min(len(answer), pos + _HEDGE_WINDOW)
            window = answer[window_start:window_end].lower()
            if not any(phrase in window for phrase in _HEDGE_PHRASES):
                source = (chunk.get("metadata") or {}).get("source", f"chunk {i}")
                issues.append(
                    f"[Document {i}] (source: {source}, graph_confidence={gc:.2f}) "
                    f"is cited without a required hedging phrase nearby."
                )
                break  # one issue per chunk is enough
    return issues


async def verify_grounding(
    answer: str,
    cited_chunks: list[dict],
    case_id: Optional[str],
    cross_case_ids: Optional[list[str]] = None,
    target_date: Optional[int] = None,
) -> dict:
    """
    Verify that a generated answer is grounded in its cited source chunks.

    This is the hard gate between generation and delivery.

    Args:
        answer:           The complete generated answer text.
        cited_chunks:     The chunks that were retrieved and given to the
                          generator (same list used to build the prompt).
        case_id:          The active case for this query, or None / "cross_case"
                          for XGRAPH/XAGG routes.
        cross_case_ids:   Case IDs explicitly allowed for cross-case routes.
        target_date:      Query's target year as int (e.g. 2025) for temporal
                          validity checks. None to skip.

    Returns:
        Dict with keys:
            "grounded"          (bool)
            "off_topic"         (bool)
            "leaked_case_id"    (str | None)
            "unsupported_claims" (list[str])
            "reason"            (str)
    """
    if not cited_chunks:
        logger.warning("Verifier received empty chunk list — returning not grounded.")
        return {
            "grounded": False,
            "off_topic": False,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": "No source chunks were provided; cannot verify grounding.",
        }

    # ── Deterministic pre-checks (fast, no LLM) ──────────────────────────
    temporal_issues = _check_temporal(cited_chunks, target_date)
    leaked_case = _check_leakage(answer, cited_chunks, case_id, cross_case_ids)
    hedging_issues = _check_hedging(answer, cited_chunks)

    pre_check_issues: list[str] = temporal_issues + hedging_issues
    pre_check_failed = bool(pre_check_issues or leaked_case)

    # ── Build LLM judge input ─────────────────────────────────────────────
    chunks_text = _format_chunks_for_verifier(cited_chunks)
    active_case_str = case_id or "cross_case"
    allowed_str = ", ".join(cross_case_ids) if cross_case_ids else "(none)"

    user_input = (
        f"ANSWER:\n{answer}\n\n"
        f"CHUNKS:\n{chunks_text}\n"
        f"ACTIVE_CASE_ID: {active_case_str}\n"
        f"ALLOWED_CASE_IDS: [{allowed_str}]"
    )

    # ── LLM judge (up to 2 attempts) ─────────────────────────────────────
    llm_result: Optional[dict] = None
    for attempt in range(2):
        raw = await call_llm(
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_input,
            temperature=0.0,
            # Qwen3-14B's thinking trace consumes max_tokens before its JSON
            # answer — 800 matches the ceiling used in evaluator.py.
            max_tokens=800,
            role="reasoning",
        )
        try:
            parsed = json.loads(raw.strip())
            if "grounded" in parsed and "reason" in parsed:
                llm_result = parsed
                break
            logger.warning(
                "Verifier JSON missing expected keys (attempt %d): %s",
                attempt + 1, raw[:120],
            )
        except json.JSONDecodeError as exc:
            logger.warning(
                "Verifier returned invalid JSON (attempt %d): %s — %s",
                attempt + 1, exc, raw[:120],
            )

    if llm_result is None:
        logger.error(
            "Verifier failed to return valid JSON after 2 attempts. "
            "Defaulting to not grounded (fail-closed)."
        )
        llm_result = {
            "grounded": False,
            "off_topic": False,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": "Verifier failed to parse — defaulting to not grounded (fail-closed).",
        }

    # ── Merge deterministic findings into LLM result ──────────────────────
    if pre_check_issues:
        existing = llm_result.get("unsupported_claims") or []
        llm_result["unsupported_claims"] = existing + pre_check_issues
        llm_result["grounded"] = False
        if not llm_result.get("reason") or llm_result["reason"].startswith("All claims"):
            llm_result["reason"] = pre_check_issues[0]

    if leaked_case:
        llm_result["leaked_case_id"] = leaked_case
        llm_result["grounded"] = False
        llm_result["reason"] = (
            f"Cross-case evidence leakage detected: chunk from case '{leaked_case}' "
            f"cited in answer for case '{active_case_str}'."
        )

    logger.info(
        "Verifier: grounded=%s off_topic=%s leaked=%s unsupported=%d — %s",
        llm_result.get("grounded"),
        llm_result.get("off_topic"),
        llm_result.get("leaked_case_id"),
        len(llm_result.get("unsupported_claims") or []),
        (llm_result.get("reason") or "")[:80],
    )
    return llm_result
