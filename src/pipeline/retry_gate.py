# ============================================================
# Retry gate — [Gold-QA fix — Module 143]
#
# PURPOSE:
# After the relevance evaluator rejects the FIRST retrieval pass, decide
# whether re-searching the same corpus with a reworded query could
# plausibly find what the evaluator said was missing — or whether the
# missing thing is a KIND of information no document in the corpus holds
# (a count, rate, average, frequency, ranking or other figure computed
# across cases), so that every retry is wasted.
#
# WHY A SEPARATE CALL, MEASURED NOT ASSUMED (MODULE143_RESULT.md §1):
# The evaluator returns a bare `relevant` bool and a free-text `reason`.
# Nothing retrieval-side separates the two cases: the zero-signal query
# this module was filed from ("How often is the officer who registers an
# FIR also the officer who investigates?") had an attempt-1 top
# cross-encoder score of 0.12, and a plain-path near-miss that the retry
# loop DID rescue on attempt 3 ("the armed robbery in Iqbal Town") scored
# 0.06 — lower. A Roman-Urdu case question answered on attempt 1 scored
# 0.0006. RRF and cosine scores are flatter still. The one place the
# distinction exists is the evaluator's own reason text — "no statistical
# data on conviction rates" versus "none mention Iqbal Town" — and only a
# model can read that reliably; a keyword heuristic misfires on the
# compound-question rejections KB5/KB9's rescues depend on ("data
# tracking", "case records").
#
# SCOPE: consulted ONLY on the plain (non-legal-KB) retrieval path, ONLY
# after attempt 1, ONLY when the evaluator said not-relevant — see
# rag.py::_run_retrieval_loop. The legal-KB path (every gold KB question)
# never calls this; its retries carry Module 30/39/52's rescue machinery
# and are byte-for-byte unchanged.
#
# FAIL-OPEN: any failure (exception, malformed JSON) returns True — the
# loop then retries exactly as it did before this module existed. This
# gate can only ever REMOVE retries, never add or alter one.
# ============================================================

import logging
from pathlib import Path

from src.llm.client import call_llm
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "retry_gate.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")


async def retry_could_help(question: str, evaluator_reason: str) -> bool:
    """
    True if a reworded search of the same corpus could plausibly surface
    what `evaluator_reason` says is missing; False only when the reason
    names a cross-case aggregate no document holds.

    Fail-open: True on any error, empty reason, or unparseable output.
    """
    reason = (evaluator_reason or "").strip()
    if not reason:
        return True

    user_input = (
        f"User's question: {question}\n\n"
        f"Relevance judge's explanation of what was missing: {reason}"
    )
    try:
        result, raw = await call_llm_json(
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_input,
            temperature=0.0,
            # Qwen3-14B's thinking trace precedes the JSON (same budget
            # reasoning as evaluator.py); the cloud fallback needs far less.
            max_tokens=2000,
            cloud_max_tokens=300,
            validate=lambda r: isinstance(r, dict)
            and isinstance(r.get("retry_could_help"), bool)
            and "reason" in r,
            schema_hint='"retry_could_help" (true/false), "reason" (string)',
            _call_llm=call_llm,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        logger.error("Retry gate failed (%s) — retrying as before.", exc)
        return True

    if result is None:
        logger.error(
            "Retry gate returned no valid JSON (raw: %s) — retrying as before.",
            (raw or "")[:150],
        )
        return True

    verdict = bool(result["retry_could_help"])
    logger.info(
        "Retry gate: retry_could_help=%s — %s", verdict, str(result.get("reason", ""))[:120]
    )
    return verdict
