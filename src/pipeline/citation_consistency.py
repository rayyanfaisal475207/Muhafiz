"""
Citation-Consistency check — src/pipeline/citation_consistency.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §5's trust-layer table, §8's build
checklist; built as part of Phase 8, per this session's explicit
AskUserQuestion resolution — see report_drafting.py's own module docstring
for the discrepancy that made that necessary).

Source of truth: AGENT_HARNESS_IMPLEMENTATION_PLAN.md §5 ("Citation-
Consistency | citation_consistency.py | Report Drafting, before the
Verifier runs | Did citation numbers stay correctly pointed at the right
evidence when Report Drafting recomposed Case Summarization's output") and
§4 row 7's build note ("Runs a citation-consistency check before
verification"). Lives beside verifier.py (same "trust layer" module
grouping, §5's table), not under harness/ — it is invoked BY the Report
Drafting sub-agent, the same relationship verifier.py already has to every
sub-agent that calls it.

WHY THIS EXISTS, SEPARATELY FROM THE VERIFIER. Report Drafting consumes
Case Summarization's already-Verifier-passed `answer_text` and generates a
NEW report draft from it (see report_drafting.py's own module docstring for
exactly how and why a second generation pass is unavoidable here). An LLM
asked to redraft prose can renumber, invent, or drop `[Document N]` markers
even when the underlying MEANING is preserved — a distinct failure mode
from ungrounded CONTENT (the Verifier's job). This check is deliberately
narrow and deterministic: it says nothing about whether a claim is
supported, only whether the citation markers actually pointing at
something Report Drafting was actually given.

METHOD. Deterministic, not LLM-based — reuses the exact `[Document N]`
citation-marker regex `src/pipeline/verifier.py::_check_leakage()` already
established as this codebase's one canonical pattern for parsing citation
markers out of generated text, so there is exactly one definition of what a
citation marker looks like, not two that could silently drift apart.
Checks that every citation index appearing in the drafted text falls within
the bounds of the citation set Report Drafting actually has available (see
report_drafting.py: exactly one synthetic citation, [Document 1], per this
session's resolution of how Report Drafting can satisfy the Verifier's
flat-chunk-list contract without ever receiving raw EvidenceChunks itself —
design §5's "no implementation types cross a boundary" / SUBAGENT_
INTERFACES.md's Citation type carrying no chunk text). An index outside
that range is an invented or drifted reference — exactly the failure this
check exists to catch.

Uncited text is NOT this check's problem — that is the Verifier's
no-citation check to make (same "one check per concern, no duplicated
logic" principle AGENT_HARNESS_IMPLEMENTATION_PLAN.md §7.1 states for
Validation reusing the Verifier's own citation parse rather than inventing
a second segmentation method).

ORDERING (load-bearing, per §5: "before the Verifier runs"). Report
Drafting calls this BEFORE `verify_grounding()`, not after. Both checks are
deterministic pre-checks over the exact same drafted text; running
citation-consistency first means an inconsistency fails fast without
spending an LLM verification call on text that has already-known citation
drift.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Same pattern as verifier.py's own _check_leakage()/_check_hedging() — kept
# as a single module-level constant here rather than re-derived, so this
# check and the Verifier's own citation parsing can never quietly diverge.
_CITATION_PATTERN = re.compile(r"\[Document\s+(\d+)\]", re.IGNORECASE)


class CitationConsistencyResult(NamedTuple):
    """
    `consistent=False` iff at least one `[Document N]` marker in the
    checked text falls outside `1..valid_citation_count`. `invalid_indices`
    names every offending N (deduplicated, sorted) for the caller's error
    message / logs. `reason` is a ready-to-log operator-facing summary.
    """

    consistent: bool
    invalid_indices: list[int]
    reason: str


def check_citation_consistency(
    drafted_text: str, valid_citation_count: int
) -> CitationConsistencyResult:
    """
    Verify every `[Document N]` marker in `drafted_text` points at a
    citation the caller actually has (`1 <= N <= valid_citation_count`).

    Fails CLOSED, matching the Verifier's own posture (design §5): an
    out-of-range marker is an inconsistency, never something to guess past
    or silently clamp into range. `valid_citation_count == 0` means no
    marker at all is valid — every `[Document N]` in the text would be
    invalid, which is the correct outcome for a caller with nothing to
    cite (see report_drafting.py's own use of this function).
    """
    invalid: set[int] = set()
    for match in _CITATION_PATTERN.finditer(drafted_text):
        n = int(match.group(1))
        if n < 1 or n > valid_citation_count:
            invalid.add(n)

    if invalid:
        sorted_invalid = sorted(invalid)
        return CitationConsistencyResult(
            consistent=False,
            invalid_indices=sorted_invalid,
            reason=(
                f"Drafted report cites document index(es) {sorted_invalid!r}, "
                f"outside the valid range 1..{valid_citation_count} of "
                f"citation(s) actually available to Report Drafting. The "
                f"redraft likely invented or misaligned a citation marker."
            ),
        )
    return CitationConsistencyResult(consistent=True, invalid_indices=[], reason="")
