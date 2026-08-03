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
#     cost as evaluator, max_tokens=2000 locally (thinking-trace budget),
#     800 on the cloud fallback (no thinking trace to pad for there)
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

import logging
import re
from pathlib import Path
from typing import Optional

from src.llm.client import call_llm
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "verifier.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# Phrases that satisfy the hedging requirement for low-confidence / unconfirmed links.
# Urdu-script equivalents are required, not optional: cross_case_response.txt
# rule 9 forces the model to answer entirely in the user's preferred language,
# so an English-only list here guarantees every correctly-hedged Urdu-language
# answer fails this check — there is no way for it to ever contain one of the
# English words. Urdu is the majority-corpus language for this platform's
# users, so this was a real (not theoretical) rejection path, not just a gap.
_HEDGE_PHRASES = (
    "unconfirmed", "possible", "pending", "not yet verified",
    "under review", "flagged", "uncertain", "may be",
    "غیر تصدیق شدہ",  # unconfirmed
    "ممکنہ", "ممکن ہے",  # possible / it is possible
    "زیر التواء", "زیر التوا",  # pending
    "تصدیق نہیں ہوئی",  # not yet verified
    "زیر جائزہ", "زیر غور",  # under review
    "نشان زد",  # flagged
    "غیر یقینی",  # uncertain
    "ہو سکتا ہے",  # may be
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


# Confirmed live: the local generation model sometimes ignores the
# retrieved chunks entirely and answers as a generic assistant with "no
# database access" — a privacy/confidentiality refusal roleplay — even
# though the actual record was right there in its prompt (e.g. "I don't
# have access to specific case files, police records, or databases... this
# information is typically confidential... contact the appropriate law
# enforcement agency"). The LLM verifier judge is supposed to catch this
# under its own "off_topic" definition (a generic non-answer when
# case-specific content was available) but unreliably does — it marked
# this exact refusal grounded=true, off_topic=false and let it straight
# through to the user. A deterministic phrase check is a much more
# reliable backstop than relying on the same class of local model to
# correctly judge another local model's refusal.
# Two tiers, not one flat list — confirmed live this distinction matters:
# a genuinely good, cited answer ("Witness Name: Kamran...") got rejected
# because it ALSO included an honest closing caveat mentioning "not
# publicly available" (e.g. "further personal details are not publicly
# available beyond what's summarized here") — a legitimate hedge, not a
# refusal, but a flat phrase list can't tell those apart.
#
# _STRONG_REFUSAL_PHRASES: near-unambiguous even standing alone — an
# answer built around one of these IS the refusal, so these always flag
# regardless of whether a citation also happens to be present.
#
# _WEAK_REFUSAL_PHRASES: plausible as either a full refusal OR an honest
# caveat inside an otherwise-real answer — only flag these when the answer
# has NO [Document N] citation at all (i.e. it isn't using the evidence in
# the first place, so the phrase is load-bearing, not a hedge).
#
# Dropped entirely (confirmed live too broad even weak-tier): "contact the
# appropriate/relevant..." and "through the proper channels" — routine
# closing language on plenty of genuinely good answers, not a refusal
# signal at all.
_STRONG_REFUSAL_PHRASES = (
    "i don't have access to", "i do not have access to",
    "i don't have direct access to", "i do not have direct access to",
    "i'm not able to access", "i am not able to access",
    "i cannot provide information about specific",
    "no access to specific case files", "no access to police records",
)
_WEAK_REFUSAL_PHRASES = (
    "consult official police records", "typically confidential",
    "not publicly available", "closed legal or law enforcement investigation",
    "closed investigation", "part of an ongoing investigation and cannot",
)


def _check_refusal(answer: str) -> Optional[str]:
    """
    Deterministic pre-check: flag an answer that reads as a generic
    "I don't have access" / "consult the proper channels" refusal instead
    of actually using the provided chunks. Only meaningful when chunks were
    provided at all (verify_grounding already returns not-grounded outright
    for an empty chunk list) — a real refusal in the presence of real
    evidence is never correct, since the evaluator already confirmed
    relevance before this function is ever called.

    Returns an issue string, or None if no refusal pattern was found.
    """
    lowered = answer.lower()
    for phrase in _STRONG_REFUSAL_PHRASES:
        if phrase in lowered:
            return (
                f"Answer reads as a generic access/privacy refusal (contains "
                f"{phrase!r}) instead of using the provided evidence, which the "
                f"evaluator already confirmed was relevant."
            )

    # Weak-tier phrases are ambiguous on their own — plausible as either a
    # full refusal or an honest caveat inside an otherwise-real, cited
    # answer. Only load-bearing (and therefore worth flagging) when there's
    # no citation backing the rest of the answer up.
    if not _DOCUMENT_CITATION_RE.search(answer):
        for phrase in _WEAK_REFUSAL_PHRASES:
            if phrase in lowered:
                return (
                    f"Answer reads as a generic access/privacy refusal (contains "
                    f"{phrase!r}, and cites no [Document N] source) instead of "
                    f"using the provided evidence, which the evaluator already "
                    f"confirmed was relevant."
                )
    return None


# Minimum length before a citation-less answer is treated as suspicious —
# a short, honest "the documents don't contain X" (final_response.txt rule
# 3's own explicitly sanctioned wording for genuinely missing evidence) has
# nothing to cite and shouldn't be flagged; a long, substantive-looking
# answer with zero citations is a different, much more suspicious shape.
_SUBSTANTIAL_ANSWER_LEN = 150

# Confirmed live (2026-08-03, query_id 889): the generator sometimes writes
# "**Document 1**" (markdown bold, no brackets) instead of the prompted
# "[Document 1]" — a genuinely grounded, correctly-cited answer ("In
# Document 1, it is mentioned that 'the stolen items were recovered'...")
# then got rejected here as if it cited nothing at all, burning two
# regeneration attempts before falling back to a generic abstention. The
# check's actual purpose is "did this answer engage with a specific
# numbered source," not "did it use exact bracket syntax" — so the pattern
# now also accepts optional brackets/parens and optional markdown bold
# around "Document N", not just the bracketed form.
_DOCUMENT_CITATION_RE = re.compile(r"[\[(]?\*{0,2}Document\s+\d+\*{0,2}[\])]?", re.IGNORECASE)


def _check_no_citation(answer: str) -> Optional[str]:
    """
    Deterministic pre-check, complementary to _check_refusal: catches any
    phrasing of "I won't/can't use the evidence" without relying on a fixed
    phrase list, which is inherently incomplete — confirmed live, the local
    model produces new refusal wordings ("not publicly available", "part of
    a closed... investigation", etc.) faster than a phrase list can be kept
    current. final_response.txt's own rule 2 requires citing every claim as
    [Document N]; a substantive-looking answer with zero such citations,
    for a query the evaluator already confirmed relevant chunks exist for,
    is almost never actually using the evidence — a real grounded answer
    of any length past a one-liner cites something.

    Returns an issue string, or None if the answer looks properly cited (or
    is short enough to plausibly be a legitimate no-citation "not found").
    """
    if len(answer) < _SUBSTANTIAL_ANSWER_LEN:
        return None
    if _DOCUMENT_CITATION_RE.search(answer):
        return None
    return (
        "Answer is substantial in length but cites no [Document N] source at "
        "all, despite the evaluator already confirming relevant chunks exist — "
        "reads as avoiding the provided evidence rather than using it."
    )


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
            "refusal_detected": False,
        }

    # ── Deterministic pre-checks (fast, no LLM) ──────────────────────────
    temporal_issues = _check_temporal(cited_chunks, target_date)
    leaked_case = _check_leakage(answer, cited_chunks, case_id, cross_case_ids)
    hedging_issues = _check_hedging(answer, cited_chunks)
    refusal_issue = _check_refusal(answer) or _check_no_citation(answer)

    pre_check_issues: list[str] = temporal_issues + hedging_issues
    pre_check_failed = bool(pre_check_issues or leaked_case or refusal_issue)

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

    # ── LLM judge ──────────────────────────────────────────────────────
    # call_llm_json retries with an explicit schema-naming correction if
    # Qwen3 answers conversationally instead of with JSON (confirmed live —
    # the identical failure shape already fixed in router.py/evaluator.py),
    # then makes one guaranteed cloud attempt before giving up, rather than
    # fail-closing on the first sign of local-model trouble.
    llm_result, raw = await call_llm_json(
        system_prompt=_SYSTEM_PROMPT,
        user_message=user_input,
        temperature=0.0,
        # Qwen3-14B's thinking trace consumes max_tokens before its JSON
        # answer. 800 was confirmed live to be too tight for a real
        # verification prompt (multiple retrieved chunks, several
        # unsupported_claims): the model's own JSON got cut off
        # mid-string ("Unterminated string...") rather than just
        # running out of room for the thinking trace. 2000 gives real
        # headroom for both on realistically-sized input. Module 6.3:
        # the cloud fallback (Groq/Gemini) has no equivalent
        # thinking-trace tax, so it keeps the original, cheaper
        # 800-token budget — only the local branch needs the extra room.
        max_tokens=2000,
        cloud_max_tokens=800,
        role="reasoning",
        validate=lambda r: isinstance(r, dict) and "grounded" in r and "reason" in r,
        schema_hint='"grounded" (true/false), "off_topic" (true/false), "leaked_case_id" (string or null), "unsupported_claims" (array of strings), "reason" (string)',
        _call_llm=call_llm,
    )

    if llm_result is None:
        logger.error(
            "Verifier failed to return valid JSON after retries. Raw: %s. "
            "Defaulting to not grounded (fail-closed).",
            raw[:150],
        )
        llm_result = {
            "grounded": False,
            "off_topic": False,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": "Verifier failed to parse — defaulting to not grounded (fail-closed).",
            "refusal_detected": False,
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

    # Distinct from off_topic/grounded — callers use this to decide whether
    # regenerating with a corrective prompt is worth trying (a refusal is a
    # different, likely-fixable failure from "the evidence genuinely
    # doesn't answer the question," where regenerating would just produce
    # the same non-answer again).
    llm_result["refusal_detected"] = bool(refusal_issue)

    if refusal_issue:
        llm_result["off_topic"] = True
        llm_result["grounded"] = False
        llm_result["reason"] = refusal_issue

    logger.info(
        "Verifier: grounded=%s off_topic=%s leaked=%s unsupported=%d — %s",
        llm_result.get("grounded"),
        llm_result.get("off_topic"),
        llm_result.get("leaked_case_id"),
        len(llm_result.get("unsupported_claims") or []),
        (llm_result.get("reason") or "")[:80],
    )
    return llm_result
