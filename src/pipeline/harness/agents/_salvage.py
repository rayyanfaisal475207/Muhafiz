"""
Sub-answer salvage slot — src/pipeline/harness/agents/_salvage.py
(Gold-QA Wave 2, Module 53).

WHY THIS EXISTS. `meta_analysis.py::_dispatch_one()` wraps each sub-query in
`asyncio.wait_for(..., timeout=META_ANALYSIS_SUBQUERY_TIMEOUT)` and
`asyncio.gather()`s all N at once, so **every sub-query in a fan-out shares
one wall-clock deadline that starts when the fan-out starts**. The shared
model server does not execute them in parallel — Module 50 measured it
serialising them into a ~10 s-per-sub-query staircase (G1 at N=5: sub-answers
back at +25.0, +33.4, +44.3, +50.9 and +56.7 s). The deadline therefore
measures QUEUE POSITION, not the cost of the sub-query: the last slot is
killed for being served last, not for being slow.

WHAT WAS BEING THROWN AWAY. Module 50 also measured that the `XAGG <kind>`
log line is present for *every* timed-out sub-query — the deterministic
aggregate had already been computed by the time the deadline fired. The only
thing the timeout destroys is its LLM paraphrase. The sub-question's finding
is then reported to the user as "Could not answer sub-question (timed out)",
and the wired-in aggregate that the whole of Modules 31–36 and 50 exists to
reach goes missing from the synthesis.

THE MECHANISM, and why it is a ContextVar holding a MUTABLE box rather than
a return value. `asyncio.wait_for()` CANCELS the coroutine it is waiting on,
so there is no return value to read: the sub-agent's stack frame — and the
`XAggToolResult` in it — is unwound. A `ContextVar` set to a dict *before*
the awaited task is created is inherited by that task's copied context as
the SAME object, so anything the sub-agent writes into it survives the
cancellation. `_dispatch_one()` opens one box per sub-query (each `gather`
child is its own Task with its own context copy, so boxes never collide),
and `large_scale_aggregate.py` deposits `raw_summary_text` into it at the
one moment the data is known to be correct and complete: immediately after
`xagg_tool()` returns `status=OK`, BEFORE the paraphrase LLM call.

WHY SERVING THE RAW AGGREGATE IS NOT A RELAXATION. This is the same
judgement `large_scale_aggregate.py` already made, and the reasoning there
transfers verbatim (see its module docstring's "VERIFIER-REJECTION STATUS
DECISION"): `raw_summary_text` is a machine-rendered string over a real
SQL/Cypher result, it is not the unverified generation, and it has no claims
beyond "this is what the aggregate computed". Module 53 reuses that existing
pattern rather than inventing a second one — the trigger is different (a
deadline rather than a verifier verdict) but the served text, and the reason
it is safe to serve, are identical.

DELIBERATELY NARROW. Only a sub-agent that holds a deterministic, already-
computed result offers salvage. Nothing here can salvage a RAG or GRAPH
sub-answer: those have no correct-by-construction rendering to fall back to,
and inventing one would be exactly the "serve an unverified answer" failure
this codebase refuses elsewhere. A sub-query with no offer in its box still
times out into its caveat, unchanged.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

__all__ = ["SalvagedSubAnswer", "open_slot", "offer", "take"]


@dataclass(frozen=True)
class SalvagedSubAnswer:
    """One deterministic, already-computed rendering offered by a sub-agent
    before it began the generation step that later timed out."""

    text: str
    tool: str  # A SourceTool literal, e.g. "XAGG".
    kind: Optional[str] = None  # The aggregate family, for logging only.


# `default=None` means "no slot open" — i.e. this sub-agent is running for a
# caller that is not Meta-Analysis, and `offer()` is a no-op. That is the
# normal case for every direct route.
_SLOT: ContextVar[Optional[list]] = ContextVar("muhafiz_subanswer_salvage", default=None)


def open_slot() -> list:
    """Open a fresh salvage box in the CURRENT context and return it. Must be
    called before creating the task whose result may need salvaging — the
    task inherits a copy of this context, and the copy shares this exact
    list object."""
    box: list = []
    _SLOT.set(box)
    return box


def offer(text: Optional[str], *, tool: str, kind: Optional[str] = None) -> None:
    """Deposit a deterministic rendering that may be served if this dispatch
    is later cancelled. Cheap and side-effect-free when no slot is open."""
    box = _SLOT.get()
    if box is None or not text or not text.strip():
        return
    box.append(SalvagedSubAnswer(text=text, tool=tool, kind=kind))


def take(box: list) -> Optional[SalvagedSubAnswer]:
    """The most recent offer in `box`, or None. Last wins: a sub-agent that
    offered twice has computed something more specific the second time."""
    return box[-1] if box else None
