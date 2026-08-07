"""
Verifier-equivalent gate — STUB.

Matches the real `verify_grounding()` signature from AGENT_HARNESS_DESIGN.md §5
exactly, so the real implementation drops in without touching a single call
site. The grounding logic itself is deliberately absent at this stage: this
stub decides pass/fail deterministically so tests can drive both branches.

[PRESERVE — design §5] The signature is the contract and must not change:

    verify_grounding(answer, cited_chunks, case_id, cross_case_ids=None,
                     target_date=None) -> dict

Returned dict keys mirror the real verifier's:
    grounded, off_topic, leaked_case_id, unsupported_claims, reason,
    refusal_detected

[PRESERVE] FAILS CLOSED. An empty chunk list is not-grounded by definition —
never a pass. The real verifier treats a JSON-parse failure the same way; this
stub has no parse step, but the empty-input rule is preserved because callers
depend on it.
"""
from __future__ import annotations

from typing import Optional

# A query containing this substring fails verification, letting tests exercise
# the abstention path without patching internals.
UNGROUNDED_TRIGGER = "__ungrounded__"


async def verify_grounding(
    answer: str,
    cited_chunks: list[dict],
    case_id: Optional[str],
    cross_case_ids: Optional[list[str]] = None,
    target_date: Optional[int] = None,
) -> dict:
    """
    Stub grounding gate.

    `cited_chunks` MUST be exactly the list the generator was shown, in the same
    order — the real verifier's deterministic checks index positionally into it
    (`[Document N]` → `chunks[n-1]`). Callers must preserve that even while this
    is a stub, or they will break when the real one lands.
    """
    if not cited_chunks:
        # [PRESERVE] Fail closed on no evidence.
        return {
            "grounded": False,
            "off_topic": False,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": "No source chunks were provided; cannot verify grounding.",
            "refusal_detected": False,
        }

    if UNGROUNDED_TRIGGER in answer:
        return {
            "grounded": False,
            "off_topic": False,
            "leaked_case_id": None,
            "unsupported_claims": ["Stub: claim not traceable to any cited chunk."],
            "reason": "Stub verifier: forced rejection.",
            "refusal_detected": False,
        }

    # Leakage backstop, stubbed but structurally real: any cited chunk whose
    # case_id is outside the allowed set is a leak.
    allowed = set(cross_case_ids or [])
    if case_id:
        allowed.add(case_id)
    if allowed:
        for chunk in cited_chunks:
            chunk_case = (chunk.get("metadata") or {}).get("case_id")
            if chunk_case and chunk_case not in allowed:
                return {
                    "grounded": False,
                    "off_topic": False,
                    "leaked_case_id": chunk_case,
                    "unsupported_claims": [],
                    "reason": f"Cited evidence from case {chunk_case}, outside the allowed scope.",
                    "refusal_detected": False,
                }

    return {
        "grounded": True,
        "off_topic": False,
        "leaked_case_id": None,
        "unsupported_claims": [],
        "reason": "Stub verifier: accepted.",
        "refusal_detected": False,
    }
