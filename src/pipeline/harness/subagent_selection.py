# -*- coding: utf-8 -*-
"""
[Gold-QA fix — Modules 158 / 164] Semantic sub-agent selection — the
fallback UNDER `supervisor.classify_to_subagent()`'s trigger list, built the
way Module 145 built the fallback under `xagg.resolve_aggregate_kind()`.

THE DEFECT, at this layer. Meta-Analysis is reachable only through
`_META_ANALYSIS_TRIGGER_PATTERNS`, a literal phrase list that has been
maintained in parallel with `meta_analysis._DECOMPOSITION_PLANS`' own
pattern lists since Module 29. Two consequences, both filed:

  * Module 158 (from Module 79): a question whose text matches a plan — the
    live *"How is the most frequently cited offence pattern distributed
    across districts?"* matches `most_cited_section_by_district` — has the
    one-call XAGG shortcut correctly vetoed by that match, then falls
    through the trigger list (no entry for it) to the XAGG agent, which
    answers the wrong question. The plan is verified in-process and never
    runs live.
  * Module 164 (from Modules 111/116/123/145): six pre-written rewordings of
    CR3/G1/G6 route correctly 6/6 and reach Meta-Analysis 3/6, and the
    split is trigger-pattern membership, not language — G6's Urdu passes
    while its English fails.

THE DESIGN. Two additions in front of the trigger list, both logged with the
same `SUBAGENT-SELECT` line so a live log says which one decided:

  1. A matched decomposition plan IS a Meta-Analysis selection. The plan's
     own patterns are Meta-Analysis's definition of what it decomposes;
     requiring a second, separately-maintained list to agree is the defect.
     Deterministic, free, and independent of the model server — so a dead
     tunnel cannot send a plan-matched question back to the wrong agent.
  2. When neither the trigger list nor a plan matches, the question is
     scored by the cross-encoder against one plain description per plan —
     written from what the plan COMPUTES (its sub-queries and synthesis
     goal), never from its trigger words — alongside ABSORBING descriptions
     of every neighbouring capability: all of Module 145's aggregate table,
     plus the within-case, timeline, report, data-quality, summary and
     global-search shapes. The best description wins; it selects
     Meta-Analysis only if it is a plan AND clears
     `SUBAGENT_SELECTION_THRESHOLD`. A question nearest to *"how many cases
     there are in total"* is not pulled anywhere, however high it scores.

WHAT IS NOT CONSULTED. The gate runs only where Meta-Analysis is reachable
at all (`semantic_selection_applies()`): never for a nested sub-query
(`allow_meta_analysis=False`, the recursion guard), never for DIRECT or a
file request, never for a within-case question (the case_scope demotion
guard would discard the selection anyway), and never when the XAGG
one-call skip, the trigger list or a plan has already decided. On the 32
gold questions that leaves D1 and S2 — the two whose aggregate kind is a
generic catch-all — and both land on an absorbing class.

THE COST OF A FALSE POSITIVE is why the threshold is measured against a
harder hazard set than Module 145's: a decomposition is five to eight
sub-queries and ~130 s on the serial model server. MODULE158_RESULT.md §6
has the distributions and the margin.
"""
from __future__ import annotations

import logging
from typing import Optional

from src import config
from src.pipeline import semantic_dispatch
from src.pipeline.semantic_dispatch import SemanticGate, SemanticMatch

logger = logging.getLogger(__name__)

# ── The plan descriptions: dispatchable ──────────────────────────────────────
#
# One sentence per `_DECOMPOSITION_PLANS` entry, keyed by the plan's name
# (`tests/test_subagent_selection.py` pins the key set equal to the plans,
# so a plan cannot be added without a description). Each is written from
# the plan's sub-queries and synthesis goal — the QUESTION the plan answers
# — and reuses none of the plan's or the trigger list's pattern words as
# load-bearing vocabulary.
PLAN_DESCRIPTIONS: dict[str, str] = {
    "record_consistency": (
        "whether two particular complaints or cases of the same kind — for "
        "instance two victims of the same online banking fraud — were handled "
        "and documented identically by the police: identifying the FIRs "
        "concerned, whether each has a linked walk-in complaint, and whether "
        "they share an accused"
    ),
    "orientation_note": (
        "a briefing for an officer who is new to this caseload on what to expect "
        "from it as a whole: where the cases are concentrated, what the case mix "
        "is and how it has changed, how often an arrest is recorded, how "
        "promptly crimes are reported, the weapon-licensing picture, who the "
        "accused tend to be, and how far cases have got in court"
    ),
    "caseload_review": (
        "an analyst's review of the whole current caseload for whatever stands "
        "out, looks unusual or deserves closer attention — the accused profile, "
        "their relationship to complainants, seized property, the timing of "
        "incidents, and any accused recurring across cases"
    ),
    "most_cited_section_by_district": (
        "a two-step question that needs both answers together: first which "
        "legal section or offence is cited by the most FIRs, and then, for the "
        "cases charged under that one winning section only, how they are "
        "spread out district by district"
    ),
    "busiest_district_by_section": (
        "a two-step question that needs both answers together: first which "
        "district has the most registered cases, and then, for the FIRs in "
        "that one winning district only, which legal sections they are "
        "charged under"
    ),
}

# ── Neighbouring sub-agents: absorbing ───────────────────────────────────────
#
# Described so a question of that shape has a correct nearest neighbour
# instead of the least-wrong plan. Module 145's whole aggregate table is
# absorbed too (below) — every one-call aggregate is a neighbour of some
# plan, and those 40 sentences were measured once already.
NEIGHBOUR_DESCRIPTIONS: dict[str, str] = {
    "within_case_facts": (
        "the facts of one particular case or FIR: what happened, who was "
        "involved, what the statements and documents in that case say"
    ),
    "case_summary": (
        "a summary of a single case: its key facts, parties, evidence and "
        "current status"
    ),
    "timeline_of_a_case": (
        "a chronological timeline of the events in one case, in date order"
    ),
    "investigative_analysis": (
        "an investigative assessment or review of one particular case: whether "
        "anything about that case's investigation looks unusual, its leads, "
        "inconsistencies in the evidence, suspects and next steps for the "
        "investigating officer"
    ),
    "report_file": (
        "a document, report or file to be generated and downloaded, such as a "
        "PDF or Word report"
    ),
    "data_quality_audit": (
        "a data-quality or integrity audit of the records: missing, duplicated, "
        "malformed or contradictory fields"
    ),
    "global_theme_summary": (
        "a general overview or short summary of what the caseload or the case "
        "documents look like overall: the main themes and topics, without a "
        "structured analytical breakdown"
    ),
    # [Round 2 — MODULE158_RESULT.md §6.3] The two HALVES of the chained
    # plans, so a one-call ranking question has a correct nearest neighbour
    # instead of the plan whose first step it resembles.
    "section_ranking_only": (
        "which single legal section is cited by the most FIRs, or a ranking of "
        "sections by how many FIRs cite each, with no further breakdown asked "
        "for"
    ),
    "district_ranking_only": (
        "which single district has the most registered cases, or a ranking of "
        "districts by caseload, with no further breakdown asked for"
    ),
}

CAPABILITY_DESCRIPTIONS: dict[str, str] = {
    **PLAN_DESCRIPTIONS,
    **NEIGHBOUR_DESCRIPTIONS,
    **{f"xagg:{kind}": text for kind, text in semantic_dispatch.CAPABILITY_DESCRIPTIONS.items()},
}

DISPATCHABLE_KINDS: frozenset[str] = frozenset(PLAN_DESCRIPTIONS)

# MEASURED, not chosen — MODULE158_RESULT.md §6.
SUBAGENT_SELECTION_THRESHOLD: float = 0.40

gate = SemanticGate(
    tag="SEMANTIC-SELECT",
    descriptions=CAPABILITY_DESCRIPTIONS,
    dispatchable=DISPATCHABLE_KINDS,
    threshold=SUBAGENT_SELECTION_THRESHOLD,
    enabled=lambda: bool(config.SEMANTIC_DISPATCH_ENABLED),
)


def lookup(query_text: str) -> Optional[SemanticMatch]:
    """Sync, pure: the cached decision if it selects Meta-Analysis."""
    return gate.lookup(query_text)


async def prepare(query_text: str) -> Optional[SemanticMatch]:
    """Async: score once, cache, log; never raises."""
    return await gate.prepare(query_text)


def reset_for_tests() -> None:
    gate.reset_for_tests()


def seed_for_tests(query_text: str, match: Optional[SemanticMatch]) -> None:
    gate.seed_for_tests(query_text, match)
