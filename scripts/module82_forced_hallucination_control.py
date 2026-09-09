"""
Modules 82/85/86 — forced-hallucination live control, on the RAG path.

Directly modelled on `scripts/module71_forced_hallucination_control.py`, which
the brief names as the working harness for exactly this, and on Module 53's
forced-deadline control before it. That script forces Meta-Analysis' synthesis;
this one forces the **Semantic Search** sub-agent's generation, because that is
the boundary Modules 82/85/86 change and a control has to exercise the code
under test.

WHY IT EXISTS. The hard constraint on this whole class of work is that a
verifier which passes everything is worse than one that is too strict. This
module's change is a generation-prompt change and touches neither
`verifier.py`, `validation.py` nor `prompts/verifier.txt` — but "we did not
edit it" is an argument, not a measurement. So the fabrication is forced and
the REAL `verify_grounding()` is made to judge it, over the REAL chunks a live
KB4 request retrieves.

ONE boundary is replaced: `semantic_search.call_llm` returns a fixed fabricated
answer instead of generating one. Everything else is real — the retrieval, the
reranker, the relevance evaluator, the verifier, the validation gate, and the
sub-agent's own ABSTAINED contract ("an answer that failed verification is
NEVER served"). NOTHING in `src/` is modified; the patch lives in this file.

The fabrication is built to be plausible rather than absurd, because a
verifier that only catches nonsense proves nothing. It states a specific rule
number the retrieved Punjab Police Rules chunks do not carry, attaches a
retention period to the wrong subject, and invents an FIR id — the same three
shapes Module 17 measured the verifier catching and Modules 25/40/61/71 each
preserved.

Usage (from the worktree, PYTHONPATH=.):
    M82_CONTROL_PORT=8032 python scripts/module82_forced_hallucination_control.py

Then send KB4's gold question at that port and read the log for
`Semantic Search: verifier rejected answer:`; the served payload must contain
none of the fabricated strings.
"""
from __future__ import annotations

import logging
import os

import uvicorn

import src.pipeline.harness.agents.semantic_search as ss_mod

logger = logging.getLogger("module82.control")

# A fabricated case-property answer. Every sentence is a different hallucination
# shape, so a rejection reason names which one the judge caught:
#   * "rule 27.41(3)" and the seven-year clock — a rule number and a retention
#     period that appear in none of the retrieved chunks (the real ones say
#     "over three years" and rule 27.16(1)/27.18(1));
#   * "FIR 512/26" — an invented identifier, the shape `_check_fabricated_case_ids()`
#     exists for;
#   * "fully compliant" over "all 45 entries" — a compliance conclusion no
#     retrieved chunk and no computed summary supports.
_FABRICATED = (
    "Yes. Under rule 27.41(3) of the Punjab Police Rules, 1934, every article of "
    "case property must be entered in the malkhana register within twenty-four "
    "hours of seizure and destroyed exactly seven years after the register is "
    "closed [Document 2]. Our own records are fully compliant with that "
    "requirement: all 45 register entries carry a disposal date, and the audit "
    "of FIR 512/26 confirmed that no entry has ever been retained past the "
    "seven-year limit [Document 7]."
)

_MARKERS = ("27.41", "seven years", "seven-year", "512/26", "fully compliant")


async def _forced_generation(system_prompt, user_message, **kwargs):
    logger.warning(
        "Module 82 CONTROL: returning a fabricated answer instead of generating one. "
        "Markers: %s",
        ", ".join(_MARKERS),
    )
    return _FABRICATED


ss_mod.call_llm = _forced_generation  # noqa: E305 — the whole point of this file.


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("M82_CONTROL_PORT", "8032")),
        log_level="info",
    )
