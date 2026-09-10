"""
Module 71 — forced-hallucination live control.

WHY THIS EXISTS. The defect this module fixes is intermittent: Module 61
measured it on 1 of 3 live G1 runs, and this module's own pre-fix baseline
did not reproduce it. Two contract halves therefore cannot be demonstrated
by simply running G1 and hoping:

  (a) the REAL `verify_grounding()` still refuses a fabricated
      cross-denominator total — the half the brief calls not optional; and
  (b) on that refusal the user now receives the verified sub-answers rather
      than `status=error`.

So the synthesis is forced. This launcher starts the ordinary backend with
ONE boundary replaced: `meta_analysis.call_llm` returns a fixed, fabricated
synthesis instead of asking the model. Everything downstream is untouched
and real — the five XAGG aggregates are really computed, the real verifier
judges the fabricated text, and the real fallback composes what is served.

This is the same shape as Module 53's forced-deadline control: a live
control that makes a rare path certain, run in its own process, never
shipped. NOTHING in `src/` is modified — the patch lives here.

Usage (from the worktree, PYTHONPATH=.):
    M71_CONTROL_PORT=8026 python scripts/module71_forced_hallucination_control.py
"""
from __future__ import annotations

import logging
import os

import uvicorn

import src.pipeline.harness.agents.meta_analysis as ma_mod

logger = logging.getLogger("module71.control")

# The live-captured over-reach, verbatim in substance: 73 is the corpus case
# count stated ONLY by the time-of-day sub-answer, attached here to seized
# property, which computed 45 entries across 28 FIRs. This is the claim
# Module 61's regression guard recorded the verifier rejecting.
_FABRICATED = (
    "Across the caseload, the seized-property register covers a 73-case total, "
    "and that figure does not reconcile with the property entries actually "
    "recorded, which is a data discrepancy worth flagging [Document 3]. "
    "The accused are aged 24-49 [Document 1]."
)


async def _forced_synthesis(system_prompt, user_message, **kwargs):
    logger.warning("Module 71 CONTROL: returning a fabricated synthesis instead of generating one.")
    return _FABRICATED


ma_mod.call_llm = _forced_synthesis  # noqa: E305 — the whole point of this file.


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("M71_CONTROL_PORT", "8026")),
        log_level="info",
    )
