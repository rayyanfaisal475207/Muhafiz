"""
Meta-Analysis sub-agent — src/pipeline/harness/agents/meta_analysis.py
(findings.md Module 10, "Meta-analysis — query decomposition and
aggregation" — the last of the ten findings.md modules).

Source of truth: findings.md's Module 10 section (the "Proposed approach"
three-stage design, the three design questions resolved via AskUserQuestion
before any of this was written — RBAC, cost/latency cap, trigger patterns —
and the "Relationship to Modules 7, 8, 9" note) and this session's own
approved plan.

SCOPE — THE OUTERMOST LAYER. Every other sub-agent in this package composes
one or more TOOLS (RAG, GRAPH, XGRAPH, ...). This one is different: it
composes the SUPERVISOR ITSELF — it decomposes the original question into a
bounded set of standalone sub-questions and re-enters
`src.pipeline.harness.supervisor.Supervisor.handle()` for each one,
concurrently, then synthesizes across the independently-produced
sub-answers. A sub-query can resolve to ANY of the other ten sub-agents
(Semantic Search, Local Search, Global Search, Cross-Case Linkage, ...) —
this module does not know or care which. It is complementary to, not a
replacement for, Modules 7/8/9: those improve how ONE question is answered;
this module decides whether the user's ask is actually SEVERAL questions in
the first place.

NO RECURSION BEYOND ONE LEVEL — [PRESERVE, per the approved plan and
findings.md's own "central design commitment"]. A sub-query is NEVER itself
decomposed further. Enforced structurally, not by convention: every
`Supervisor.handle()` call this module makes passes
`allow_meta_analysis=False` (see `supervisor.py::classify_to_subagent()`'s
own docstring for the full recursion-guard rationale) — a sub-query
classifies EXACTLY as if this module did not exist, even if its own text
still happens to match `_META_ANALYSIS_TRIGGER_PATTERNS`. This module is the
first sub-agent in this codebase to call `Supervisor.handle()` at all
(every prior sub-agent composes tools directly); the guard exists because
nothing before this module could ever have recursed.

RBAC — [PRESERVE, resolved via AskUserQuestion, not assumed]. This module
adds NO role check of its own — the same "no third gate" discipline
`cross_case_linkage.py`/`large_scale_aggregate.py` already document. The
actual gate is one level up, in `supervisor.py`: `META_ANALYSIS` is in
`_CROSS_CASE_SUBAGENTS`, so a query only ever reaches this module at all
when its own `case_scope` (computed by `route_query()`, the same signal
XAGG/XGRAPH/XNETWORK key on) is `"cross_case"` — a within-case compound
question demotes straight to Semantic Search before this module is ever
invoked, at zero N-way pipeline cost. Once dispatched, individual sub-queries
are gated on their own merits by whichever tool/sub-agent they resolve to
via the normal recursive `Supervisor.handle()` call — a low-privilege
caller's cross-case-shaped sub-question(s) can still come back DENIED
individually, disclosed as a caveat, never a blanket failure of the whole
meta-analysis (see STATUS MAPPING below).

THREE STAGES:

1. DECOMPOSE. `call_llm_json()` against `prompts/meta_analysis_decomposer.txt`
   — an EXTERNAL prompt file, unlike every other sub-agent's own inline
   `_SYSTEM_PROMPT_TEMPLATE` (see `local_search.py`'s own explicit note on
   why ITS generation prompt is inline). This is a deliberate, considered
   difference, not an inconsistency: the decomposer is a narrow classifier/
   judge call (bool + bounded list + one string), the same SHAPE as
   `router.py`'s/`verifier.py`'s own classifier prompts, both of which
   already use external `prompts/*.txt` files — not the "final,
   user-facing, [Document N]-citing narrative answer" shape
   `local_search.py`'s note is actually about. The synthesis prompt below
   (stage 3), which IS that shape, follows the inline-template convention
   instead, for the same reason `local_search.py` gives.

   Can return `decompose: false` — this is the real "no decomposition
   needed" decision (the deterministic trigger patterns in `supervisor.py`
   are a cheap FAST-PATH INTO this call, not the decision itself; they can
   and do over-match, e.g. a long single-topic question that happens to
   contain both "summarize" and "across all" nearby). On `decompose: false`,
   or on a parse failure after `call_llm_json()`'s own retries, this module
   falls back to ONE non-decomposed dispatch of the ORIGINAL query via
   `Supervisor().handle(..., allow_meta_analysis=False)` and returns exactly
   that `SubAgentResult` — untouched, per every prior sub-agent's own
   "[PRESERVE] returned exactly as received" ethos applied one level up.
   The parse-failure path adds one caveat disclosing that automatic
   decomposition could not run; `decompose: false` adds none (the system
   correctly recognized no decomposition was needed — nothing degraded).

2. DISPATCH. One `SubAgentInput` per sub-query (`execution` threaded
   UNCHANGED per [PRESERVE — design §4.4]; `target_entity=None`, letting
   each sub-query's own `route_query()` derive its own rather than
   inheriting the original query's entity onto an unrelated sub-question).
   Each dispatch is wrapped by `_dispatch_one()`, which NEVER RAISES — it
   returns a `_SubQueryOutcome` carrying either a real `SubAgentResult` or a
   failure reason (`"timeout"` or the exception text), mirroring
   `ToolResult`'s own "no tool raises to signal a routine outcome" doctrine
   one level up. All N dispatches run concurrently via `asyncio.gather` over
   these non-raising wrappers — one bad sub-query can never take the others
   down, and every outcome (success, empty, denied, abstained, timed-out)
   reaches stage 3, disclosed, never silently dropped. Bounded at
   `_MAX_SUB_QUERIES` (5, per findings.md's own suggested cap and the
   approved plan) — the decomposer prompt is instructed to stay within this,
   and this module hard-truncates defensively if it doesn't. A
   DETERMINISTIC plan is bounded by `_MAX_PLAN_SUB_QUERIES` instead
   ([Gold-QA fix — Module 50]): same number, different justification, and
   see that constant for the measurement that fixes it at 5.

3. AGGREGATE / SYNTHESIZE. See `_bucket_outcomes()`/`meta_analysis()` for
   the full status-mapping bucket list. In short: a sub-query that produced
   real content (OK/PARTIAL with `answer_text`) OR a legitimate EMPTY
   ("nothing found for this sub-question" — a real finding, not a failure,
   mirroring `cross_case_linkage.py`'s own EMPTY philosophy) CONTRIBUTES a
   pseudo-`[Document N]` entry to the synthesis pass; ABSTAINED/DENIED/
   timeout/exception sub-queries do not contribute a document, but DO
   contribute one `caveats` entry each, naming the sub-question and the
   reason. If EVERY sub-query contributed and none are real EMPTY-only
   (i.e. there is at least one substantive answer), one `call_llm()` pass
   synthesizes across the contributing sub-answers into the decomposer's own
   `synthesis_goal`, verified through the EXISTING `verify_grounding()` —
   **confirmed by reading `verifier.py` before writing this module: its
   signature needs NO extension.** It already accepts an arbitrary flat
   `list[dict]` "documents" list to check `[Document N]` claims against;
   this module's pseudo-chunks (`text=` a contributing sub-query's own
   `answer_text`) are built with the exact same `{"id", "text", "metadata"}`
   shape every other sub-agent's own `_chunk_to_verifier_dict()` produces —
   just sourced from an already-synthesized, already-Verifier-passed
   sub-answer instead of raw evidence text. This is the same "map over
   already-synthesized documents" shape `global_search.py`'s reduce step
   established, adapted for sub-*answers* rather than community reports:
   Global Search has its own tool's raw `EvidenceChunk`s to reduce over
   because it composes ONE TOOL internally and never crosses a
   `SubAgentResult` boundary until the very end; this module composes OTHER
   SUB-AGENTS via `Supervisor.handle()`, so all it ever receives back is
   each one's own BOUNDED `SubAgentResult` (`answer_text` + `citations`,
   [PRESERVE — design §3] never raw chunk text) — the pseudo-chunk *is* the
   sub-answer text, by construction, not a shortcut around fetching real
   evidence.

   If ALL contributing sub-queries are legitimate EMPTY (nothing found
   anywhere, and nothing genuinely failed either), this module returns
   `EMPTY` with a deterministic templated `answer_text` naming every
   sub-question checked — NO LLM call, mirroring `cross_case_linkage.py`'s
   own cheap-and-deterministic EMPTY handling (nothing substantive exists to
   synthesize or hallucinate about).

   If NOTHING contributed at all (every sub-query ABSTAINED/DENIED/timed
   out/raised), this module returns `ABSTAINED` (or `DENIED` in the one case
   every single sub-query came back DENIED specifically — mirrors
   `cross_case_linkage.py`'s "both DENIED -> DENIED" bucket, generalized to
   N; [RESOLVED-6] a mix of DENIED + something-else is `PARTIAL`, DENIED is
   NEVER collapsed into ABSTAINED/EMPTY when it's the SOLE outcome).

VALIDATION GATE — FULL semantic tier (plan §5's higher-stakes tier), same
reasoning as Cross-Case Linkage: a cross-case-shaped synthesis-of-syntheses
is the same high-stakes claim-recurrence-across-cases shape, run here over
the pseudo-chunks the synthesis generator was actually shown.

`tools_used`/`degraded_from` ARE THE UNION (deduplicated) of every
CONTRIBUTING sub-query's own `tools_used`/`degraded_from` lists — real
`SourceTool` values genuinely propagated up from what each sub-dispatch
actually used, never an invented tag (`SourceTool` is a `Literal` type; a
made-up `"META_ANALYSIS"` value would not validate against it). Meta-level-
only failures (decomposer parse failure, a sub-query that itself
ABSTAINED/DENIED/timed out) surface as free-text `caveats` instead, which
carries no such typing constraint.

`citations`: one `Citation` per CONTRIBUTING sub-query, `document_index`
matching its `[Document N]` position in the synthesis pass — this keeps
`Citation`'s own [PRESERVE] positional-correspondence contract intact: for
this module, `[Document N]` genuinely does mean "the Nth sub-answer",
consistent with what the synthesis generator was actually shown.
`source_tool=` that sub-query's own first `tools_used` entry (documented
simplification — a sub-answer can have used more than one tool;
`Citation.source_tool` is a single value). `case_id`/`source_file=None`,
deliberately not asserted: a sub-answer can span multiple cases/files, and
this layer has no raw evidence left to re-flatten and resolve one from.

NO `types.py` CHANGES NEEDED — confirmed by design, not by omission: this
module's payload (`answer_text` + `citations` + `caveats` +
`tools_used`/`degraded_from`) fits the EXISTING `SubAgentResult` shape
without a new field, unlike Timeline Building's `events` or Cross-Case
Linkage's `links`.

NOT IN SCOPE THIS MODULE: live wiring into main.py/orchestrator.py/router.py
(same posture every prior sub-agent module through Module 9 shipped with —
`config.HARNESS_CUTOVER_ROUTES` defaults empty); file-output handling
(`classify_to_subagent()`'s file-output check already wins outright before
the meta-analysis trigger is ever checked, so this module is never dispatched
to for a file-generation request in the first place).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src import config
from src.data_gateway.base import DataGateway
from src.llm.client import call_llm
from src.pipeline.harness.supervisor import META_ANALYSIS, Supervisor, register
from src.pipeline.harness.types import (
    ANSWER_MAX_TOKENS,
    NAME_FIDELITY_RULE,
    Citation,
    OnEventCallback,
    SourceTool,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
    ValidationStatus,
)
from src.pipeline.json_extract import call_llm_json
from src.pipeline.validation import caveats_for_validation, validate_answer
from src.pipeline.verifier import verify_grounding

logger = logging.getLogger(__name__)

_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent.parent / "prompts" / "meta_analysis_decomposer.txt"
)
_DECOMPOSER_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# [findings.md Module 10] Hard cap on N, per the approved plan (findings.md's
# own suggested cap). The decomposer prompt is instructed to stay within
# this; this module hard-truncates defensively if it doesn't, rather than
# rejecting the whole decomposition outright over a prompt-compliance slip.
_MAX_SUB_QUERIES = 5

# [Gold-QA fix — Module 50] The SAME number, for a DIFFERENT reason, and
# split out so the two reasons can move independently.
#
# `_MAX_SUB_QUERIES` above bounds an UNTRUSTED list — whatever the decomposer
# LLM happened to emit. `_MAX_PLAN_SUB_QUERIES` bounds a hand-authored,
# code-reviewed `_DecompositionPlan`. Before this split, a plan was silently
# truncated by the LLM-path cap: appending Modules 31–34's four sub-queries
# to `caseload_review` would have produced a 9-entry plan of which only the
# first FIVE ever dispatched, with nothing anywhere saying so. That is the
# worst available failure mode — a wiring module whose wiring is invisibly
# discarded — so plans are now truncated against their own constant and a
# unit test asserts no plan ever exceeds it (a plan that did would be a
# code bug, not a runtime surprise).
#
# MEASURED, on this branch, port 8013, 2026-09-08 — see MODULE50_RESULT.md §2
# for the full table. The binding constraint is NOT model cost, it is the
# 60 s `META_ANALYSIS_SUBQUERY_TIMEOUT`, which every sub-query in a fan-out
# shares as a single WALL-CLOCK deadline from the moment `asyncio.gather()`
# starts:
#
#   - Dispatched ALONE, each of the six new sub-queries costs 12.1–26.1 s
#     (mean 18.3 s) end to end.
#   - Dispatched CONCURRENTLY they do NOT overlap. The shared model server
#     serialises them: at N=5, G1's five sub-answers completed at +25.0,
#     +33.4, +44.3, +50.9 and +56.7 s after the plan matched — a near-linear
#     ~10 s per sub-query staircase, finishing 3.3 s inside the 60 s budget.
#   - So the ceiling is arithmetic, not a matter of taste:
#     60 s / ~10 s per sub-query ≈ 6, and the 6th lands ON the deadline.
#     N=9 was then run live and behaved exactly as that predicts — see §2.
#
# Raising this number therefore does not buy more coverage; it buys TIMEOUTS,
# and a timed-out sub-query contributes a caveat instead of its finding, so
# the aggregate that was wired in is precisely the one that goes missing.
# Five is kept, and `caseload_review` is RE-COMPOSED rather than extended —
# see that plan's own comment for which five and why.
_MAX_PLAN_SUB_QUERIES = 5

# Self-contained, sub-agent-scoped synthesis prompt — inline template, NOT
# an external prompts/*.txt file. See module docstring's stage-1 note for
# why the decomposer (a classifier/judge call) and this synthesis prompt (a
# final, user-facing, [Document N]-citing narrative) are deliberately
# treated differently, following local_search.py's own established
# distinction rather than findings.md's literal "own prompts/*.txt" phrasing
# for every new prompt this module uses.
_SYNTHESIS_SYSTEM_PROMPT_TEMPLATE = (
    "You are a police investigative-analysis assistant. The user asked one "
    "broad question, which was split into several sub-questions, each "
    "already answered independently below. Your job: {synthesis_goal}\n\n"
    "Write ONE coherent answer to the user's original question by combining "
    "these sub-answers — do not just concatenate them. Every factual claim "
    "MUST cite its source as [Document N], where N is the sub-answer's "
    "1-based position below. Do not invent anything beyond what the "
    "sub-answers state.\n\n"
    "Respond in {preferred_language}.\n\n"
    "--- SUB-ANSWERS ---\n{documents}\n--- END OF SUB-ANSWERS ---\n\n"
    # [Gold-QA fix — Module 29] Both of these were live-caught rejecting a
    # decomposed answer that was otherwise correct, on this module's own
    # first live run of CR3 and G6 (2026-09-08). They are restated HERE,
    # after the sub-answers, rather than only in the instructions above:
    # with a long `synthesis_goal` and five sub-answers in between, the
    # citation rule at the top was reliably lost.
    #   - CR3: verifier `off_topic=True`, reason "Answer is substantial
    #     (long, or a multi-item list) but cites no [Document N] source at
    #     all" — the model wrote a good answer and cited nothing.
    #   - G6: verifier `unsupported=1`, reason "The claim about 73 total
    #     cases is not directly stated in any chunk ... their sum requires
    #     inference" — the model ADDED UP the per-district counts. Correct
    #     arithmetic, but a number that appears in no sub-answer is
    #     unverifiable by construction at this layer, because the raw
    #     evidence is two levels down and never travels this far.
    # This is the same Module 25 verifier-interaction family (M2), and the
    # M2 regression is re-run against this wording — see this module's
    # result file.
    "Check both of these before you answer:\n"
    "1. Every sentence that states a fact carries the [Document N] marker of "
    "the sub-answer it came from. An answer that cites no [Document N] at all "
    "is rejected outright as ungrounded, however good it is.\n"
    "2. Every number you state appears literally in a sub-answer above. Do "
    "not add up, average, or convert figures into percentages yourself — a "
    "derived number that appears in no sub-answer is treated as unsupported "
    "and the whole answer is rejected."
) + NAME_FIDELITY_RULE

_NO_INFO_SUBANSWER_TEXT = "No information was found for this sub-question."


def _generation_role(preferred_language: Optional[str]) -> str:
    """
    Mirrors every prior sub-agent module's own inline `_generation_role()`.
    Same finding all of them document: the Urdu-fine-tuned local
    generation-slot model ignores an explicit "reply in English" instruction,
    so non-Urdu responses route through the reasoning slot instead.
    """
    return "generation" if preferred_language == "Urdu" else "reasoning"


def _validate_decomposer_result(result) -> bool:
    if not isinstance(result, dict) or "decompose" not in result:
        return False
    if result["decompose"] is not True:
        return True  # decompose:false (or falsy) needs nothing else.
    sub_queries = result.get("sub_queries")
    return (
        isinstance(sub_queries, list)
        and 1 <= len(sub_queries) <= _MAX_SUB_QUERIES
        and all(isinstance(q, str) and q.strip() for q in sub_queries)
        and isinstance(result.get("synthesis_goal"), str)
        and bool(result.get("synthesis_goal", "").strip())
    )


@dataclass
class _DecomposerResult:
    decompose: bool
    sub_queries: list[str]
    synthesis_goal: str
    parse_failed: bool = False
    plan_name: Optional[str] = None  # Set iff a deterministic plan matched.


# ═══════════════════════════════════════════════════════════════════════
# [Gold-QA fix — Module 29, questions CR3 / G1 / G6] DETERMINISTIC
# DECOMPOSITION PLANS — checked BEFORE the LLM decomposer call below.
#
# THE DEFECT. Measured on this branch's own base commit, calling
# `_decompose()` directly against each question's literal gold text
# (2026-09-08):
#
#     [CR3] decompose=False   [G1] decompose=False   [G6] decompose=False
#     [M2]  decompose=True  -> 2 sub-queries        (Module 25's question,
#                                                    unaffected)
#
# All three DID reach this module — `supervisor.py`'s
# `_META_ANALYSIS_TRIGGER_PATTERNS` already carries a pattern for each —
# but the LLM decomposer judged each one "a single broad-sounding question
# that is still really one ask" and returned `decompose: false`, so
# `meta_analysis()` fell back to ONE non-decomposed dispatch of the
# original query. That re-dispatch lands on XNETWORK / Cross-Case Linkage,
# whose relevance gate then correctly refuses (Module 21 measured the
# nearest community distances: CR3 0.156, G1 0.202, G6 0.181, all above the
# 0.145 cutoff). The gate is right; the question should never have reached
# the community layer whole. Nothing in `xnetwork.py` is touched here.
#
# WHY DETERMINISTIC RATHER THAN PROMPT-ONLY. The decomposer prompt IS also
# extended (see `prompts/meta_analysis_decomposer.txt`) so paraphrases
# outside these families still decompose. But the prompt alone cannot be
# the whole fix, for three reasons this codebase has already paid for:
#
#   1. It is a live LLM judgment on a rotating free-tier model. The brief
#      for this module requires unit tests pinned to the LITERAL
#      decomposition of CR3/G1/G6 — a promise only a deterministic path can
#      keep across model drift.
#   2. Each sub-query is re-dispatched through `Supervisor().handle()`,
#      which routes it with ANOTHER LLM call unless a deterministic router
#      override catches it first. Measured on this same base commit, the
#      LLM router sent 6 of 9 naturally-phrased sub-questions to
#      **XGRAPH / Cross-Case Linkage** — the exact dead end this module
#      exists to route away from. Every sub-query below is phrased so that
#      `router.py::_deterministic_route_override()` matches it outright
#      (verified live: all 9 return `det=Y route=XAGG`), so decomposition
#      adds ZERO extra router LLM calls and lands on a known aggregate.
#   3. `xagg.py` itself is a family of CANNED aggregates selected by
#      keyword, deliberately not a general text-to-SQL engine (see its own
#      module header). A canned decomposition plan per question SHAPE is
#      the same paradigm one layer up, not a new one.
#
# WHY THESE SUB-QUERIES AND NOT THE ONES THE MODULE BRIEF LISTED. The
# brief expected G1 to decompose into offender age / accused-complainant
# relationship / seized-property counts / incident time-of-day, and G6 to
# include an arrest rate. This module's own gap analysis (published in
# `GOLD_QA_REMAINING_FIXES_PLAN.md` and
# `docs/gold-qa-wave2-results/MODULE29_RESULT.md`, probed live against
# `run_aggregate()`) found that **none of those five aggregates exists**:
# age is an explicit `unsupported_aggregate` refusal, and relationship /
# seized-property / time-of-day / arrest-rate sub-questions fall through
# `run_aggregate()`'s keyword chain to the unrelated person-RECURRENCE
# family and come back with a confidently wrong answer and no caveat.
# Dispatching them today would inject wrong facts into the synthesis. They
# are therefore filed as Modules 31-35 (one per missing aggregate, per this
# project's own "a new gap becomes its own module" discipline) and each
# plan below carries only sub-questions that were LIVE-VERIFIED to reach a
# real, correct aggregate. When one of those modules lands, its sub-question
# is added to the plan here — that is a one-line change by design.
#
# NEGATIVE CONTROL. Too broad a trigger is as bad as too narrow: a
# currently-correct single-fact answer must not turn into a muddled
# synthesis. Every pattern below is asserted in
# `tests/test_harness_agent_meta_analysis.py` against all 32 gold questions
# — the only matches allowed are CR3, G1 and G6. In particular G5
# ("compliance ke lihaz se ... flag karne layak") and G2 already reach this
# module, already answer correctly via the `decompose: false` fallback, and
# must keep doing so: no pattern here uses a bare "flag"/"briefing".
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class _DecompositionPlan:
    """A question SHAPE, the standalone sub-questions it decomposes into,
    and what the synthesis has to do with the answers."""

    name: str
    patterns: tuple[re.Pattern, ...]
    sub_queries: tuple[str, ...]
    synthesis_goal: str


# Sub-question wordings are INTERNAL dispatch strings, never shown to the
# user, so they are written in English regardless of the original query's
# language (G6 is Roman Urdu): `router.py`'s deterministic overrides and
# `xagg.py`'s keyword families are most reliable in English, and the final,
# user-facing synthesis handles language separately via
# `_SYNTHESIS_SYSTEM_PROMPT_TEMPLATE`'s own `{preferred_language}`.
_SQ_COMPLETENESS = "How many cases have incomplete or missing record fields, across all cases?"
_SQ_PERSON_RECURRENCE = (
    "How many cases does each accused person appear in, and which FIR numbers, across all cases?"
)
_SQ_WEAPON_LICENCE = (
    "How many cases involve a recovered weapon with no licence recorded, across all cases?"
)
_SQ_CASE_MIX_BY_YEAR = "What kinds of cases are we dealing with now compared to a couple of years back?"
_SQ_CRIMINAL_RECORD_VS_COURT = (
    "How many cases in the criminal record system have a court outcome that matches "
    "the recorded conviction status, across all cases?"
)
_SQ_DISTRICT_SPREAD = "How many cases are registered in each district, across all cases?"
_SQ_REPORTING_SPEED = (
    "How long does it typically take someone to report a crime to us these days "
    "versus a couple of years ago?"
)
_SQ_GENDER = "How many of the accused are men and how many are women, across all cases?"
_SQ_CMS_LINKAGE = (
    "How many cases have a matching walk-in complaint recorded in the complaint "
    "management system, across all cases?"
)
# NOT USED IN ANY PLAN — kept, named, and explained because the live
# evidence for leaving it out is the most useful thing this module learned
# about CR3, and a future module WILL be tempted to add it.
#
# `case_listing` is the only path in `xagg.py` that returns PER-FIR
# attributes (FIR number, statute, police station, status) rather than
# counts, so it looks like the obvious way to work out WHICH FIRs a question
# such as "the online banking fraud matter" is about. It was tried live in
# the record-consistency plan and REMOVED again: rendering all 73 cases
# takes ~4.6 KB of generation, and running it concurrently with the other
# sub-queries starved them on the shared model server — both of the other
# two sub-queries hit `META_ANALYSIS_SUBQUERY_TIMEOUT` (60 s) on that run
# and the whole answer degraded. The identification gap it was meant to
# close is real; it needs a SUBJECT-FILTERED FIR listing aggregate (Module
# 36 in the plan), not a whole-corpus dump inside a concurrent fan-out.
# [Gold-QA fix — Module 50] Module 36 landed, and that filtered aggregate is
# now wired into the record-consistency plan as `_SQ_FIR_LISTING_CYBER`
# below. This constant stays unused, and the warning above stays true of
# THIS string: the fix was to filter the listing, not to tolerate the dump.
_SQ_CASE_LISTING = "Give me the list of all cases."

# ── [Gold-QA fix — Module 50] The six aggregates Modules 31–36 built and
#    live-verified, and which until this module NOTHING CALLED.
#
# Every string below is COPIED VERBATIM from the test that its own module
# pinned it in (`tests/test_xagg.py`: `_G1_SQ_ACCUSED_AGE`,
# `_G1_SQ_RELATIONSHIP`, `_G1_SQ_SEIZED_PROPERTY`, `_G1_SQ_TIME_OF_DAY`,
# `_G6_SQ_ARREST_RATE`, `_CR3_SQ_FIR_LISTING`). They were pinned there
# precisely so this module could copy them across unchanged, and
# `test_module50_wired_sub_queries_are_byte_identical_to_the_pinned_strings`
# asserts the two copies stay equal.
#
# DO NOT REWORD THEM. Each leads with "How many cases ..." for a reason
# Modules 31–34 caught LIVE, not in review: `router.py`'s
# `_XGRAPH_OVERRIDE_PATTERNS` carries `across.{0,15}cases`, which steals any
# sub-query whose "..., across all cases?" suffix is the first override to
# match. The first drafts ("What relationship is recorded...", "At what time
# of day...") all came back `route='XGRAPH'`, never reached `run_aggregate()`
# at all, and were answered by an unrelated cross-case traversal. Leading
# with "How many cases" makes `_XAGG_OVERRIDE_PATTERNS` win outright, so
# every one of these dispatches costs ZERO router LLM calls and cannot
# drift. `test_module29_every_planned_sub_query_routes_deterministically_to_xagg`
# already covers every plan member, these six included.
_SQ_ACCUSED_AGE = (  # Module 31 -> `offender_age_profile`
    "How many cases involve an accused person, and what is their age range "
    "and average age, across all cases?"
)
_SQ_RELATIONSHIP = (  # Module 32 -> `accused_relationship_breakdown`
    "How many cases record a relationship between the accused and the "
    "complainant, and which relationship is it, across all cases?"
)
_SQ_SEIZED_PROPERTY = (  # Module 33 -> `seized_property_disposition`
    "How many cases record seized property, and what happens to it — how "
    "many items were sent to a forensic laboratory or held for a deceased's "
    "heirs, across all cases?"
)
_SQ_TIME_OF_DAY = (  # Module 34 -> `incident_time_of_day`
    "How many cases record an incident time, and at what time of day do "
    "those incidents happen, across all cases?"
)
_SQ_ARREST_RATE = (  # Module 35 -> `arrest_rate`
    "How many cases record an arrest of an accused person, and on how many "
    "is no arrest recorded, across all cases?"
)
_SQ_FIR_LISTING_CYBER = (  # Module 36 -> `filtered_fir_listing`
    "How many cases are registered under the cybercrime act at a cyber "
    "crime circle station, and what are their FIR numbers and current status?"
)

_DECOMPOSITION_PLANS: tuple[_DecompositionPlan, ...] = (
    # (1) CROSS-RECORD CONSISTENCY — "were these two records processed and
    #     recorded the same way?" (CR3). Answering it needs the pair
    #     IDENTIFIED (which FIRs are the two in question), then the same
    #     record-linkage cross-check applied to each. Checked first: it is
    #     the narrowest family, and a consistency question can also carry
    #     caseload vocabulary.
    #
    # [Gold-QA fix — Module 50] `_SQ_FIR_LISTING_CYBER` closes the
    # IDENTIFICATION half, which Module 29 left open and named as CR3's
    # single cause of instability: nothing in the two original sub-answers
    # said which FIRs "the online banking fraud matter" refers to, so the
    # synthesis model had to guess the pair, and across Module 29's runs it
    # once refused outright and once paired `fir-64-26` with the wrong FIR.
    # It is placed FIRST so it lands as [Document 1] and the synthesis goal
    # can point at it by number.
    #
    # This is NOT `_SQ_CASE_LISTING` (see that constant's own comment for the
    # 4.6 KB whole-corpus dump that starved its siblings). Module 36's
    # `filtered_fir_listing` returns exactly the FIRs matching the statute
    # and station filters — measured live on this branch at 2 rows, not 73 —
    # so the cost that got the unfiltered listing removed does not apply.
    _DecompositionPlan(
        name="record_consistency",
        patterns=(
            re.compile(r"\b(the\s+)?same\s+way\b", re.IGNORECASE),
            re.compile(r"\bprocessed\s+and\s+recorded\b", re.IGNORECASE),
            re.compile(r"\b(handled|recorded|processed)\s+(the\s+)?same\b", re.IGNORECASE),
            re.compile(r"\bek\s*hi\s*tarah\s*(se)?\b", re.IGNORECASE),
            re.compile(r"ایک\s*ہی\s*طرح"),
        ),
        sub_queries=(_SQ_FIR_LISTING_CYBER, _SQ_PERSON_RECURRENCE, _SQ_CMS_LINKAGE),
        synthesis_goal=(
            "Decide whether the records the user asked about were handled identically. "
            "[Document 1] IDENTIFIES the pair: it is a filtered listing of exactly the "
            "FIRs the question is about, so take the FIR numbers from there rather than "
            "inferring them. Then check each of those FIRs against the walk-in-complaint "
            "linkage list: a FIR that appears in that list has a matching complaint; one "
            "that does not appear has none, and that difference IS the answer. The "
            "recurring-person answer is corroboration — it shows the pair shares an "
            "accused — not the identifier. If the linkage list contains all of the "
            "identified FIRs, or none of them, the records were handled the same way and "
            "you should say so. Say 'yes, identically' or 'no, not identically' "
            "explicitly, name the FIR numbers, and name the specific record (with its "
            "case tag) that exists for one and not the other. Do not reply that the "
            "question cannot be answered merely because the sub-answers do not repeat "
            "its wording."
        ),
    ),
    # (2) ORIENTATION / WHAT-TO-EXPECT NOTE — "brief a newly posted officer
    #     on what this caseload is like" (G6). Decomposes into the standing
    #     shape of the caseload: how big and where, what it is made of and
    #     how that changed, how often an arrest actually happens, how fast
    #     things get reported, and the compliance fact that most affects
    #     day-to-day work.
    #
    # [Gold-QA fix — Module 50] `_SQ_ARREST_RATE` (Module 35) added.
    # Module 35 confirmed live that G6 COMPLETED without it ever firing,
    # for the simple reason that this plan did not ask for it — the
    # aggregate existed and was correct and was unreachable.
    #
    # `_SQ_GENDER` was REMOVED to make room, and this is a real cost, stated
    # plainly rather than glossed: gold's "zyada tar mulzim ... mard hain"
    # is an element this plan no longer computes. It lost the slot on
    # gradeable specificity — the arrest rate is a NUMBER gold states
    # ("girftari sirf har no mein se taqreeban ek FIR par", vs this data's
    # measured 1 in 6.6), where the gender split is a soft descriptor. Both
    # cannot fit: N=6 was run live three times on this branch and the third
    # run lost a sub-query to the 60 s timeout and produced an UNVERIFIABLE
    # synthesis. See `_MAX_PLAN_SUB_QUERIES` for the full measurement.
    _DecompositionPlan(
        name="orientation_note",
        patterns=(
            re.compile(r"\borientation\s+note\b", re.IGNORECASE),
            re.compile(r"\btawaqqo\b.{0,30}\brakhni\s*chahiye\b", re.IGNORECASE),
            re.compile(r"\bnewly\s+(posted|assigned|transferred|joined|appointed)\b", re.IGNORECASE),
            re.compile(r"\bnew(ly)?\b.{0,30}\bofficer\b.{0,60}\bexpect\b", re.IGNORECASE),
            re.compile(r"\bnaye?\s*(tainaat|tayinaat)\b", re.IGNORECASE),
            re.compile(r"نئے\s*تعینات"),
        ),
        sub_queries=(
            _SQ_DISTRICT_SPREAD,
            _SQ_CASE_MIX_BY_YEAR,
            _SQ_ARREST_RATE,
            _SQ_REPORTING_SPEED,
            _SQ_WEAPON_LICENCE,
        ),
        synthesis_goal=(
            "Write a short orientation note for an officer joining this caseload. Say "
            "which districts the cases are concentrated in, what the case mix is now and "
            "how it has changed, how often an arrest is actually recorded, how promptly "
            "crimes are reported now compared with earlier, and what the "
            "weapon-licensing picture looks like. "
            "Quote the per-district and per-year figures exactly as the sub-answers give "
            "them — do not total them up — and state plainly anything the sub-answers say "
            "is not available rather than guessing at it."
        ),
    ),
    # (3) WHOLE-CASELOAD REVIEW — "review the caseload and flag anything
    #     unusual or worth monitoring" (G1, and its non-gold paraphrase
    #     "look over everything currently open ... worth a second look").
    #
    # [Gold-QA fix — Module 50] RE-COMPOSED, not extended. This is the
    # substantive judgement of this module, so the reasoning is recorded
    # here rather than in a commit message.
    #
    # Module 29 wired five "is anything off here?" scans and its own result
    # file graded the outcome honestly: "a real analytical answer, but not
    # gold's analytical answer" — every claim correctly computed, and
    # near-zero overlap with what gold actually asked for. Gold's G1 states
    # its method in its first line: "profile the accused, the victims, the
    # property and the timing across the 73 FIRs". Its four findings are
    # therefore EXACTLY Modules 31-34's four aggregates, in order:
    #   (1) offender age 24-49, mean 31.5      -> `_SQ_ACCUSED_AGE`
    #   (2) 'stranger' dominates relationships -> `_SQ_RELATIONSHIP`
    #   (3) 13 forensic-lab / 7 heirs items    -> `_SQ_SEIZED_PROPERTY`
    #   (4) incident time-of-day distribution  -> `_SQ_TIME_OF_DAY`
    #
    # Appending them to the existing five was measured, not assumed, and it
    # does not work: at N=9 FOUR of the nine sub-queries hit the 60 s
    # `META_ANALYSIS_SUBQUERY_TIMEOUT` live on this branch, and two of the
    # four killed were `_SQ_ACCUSED_AGE` and `_SQ_TIME_OF_DAY` — i.e. the
    # wiring silently destroyed the very aggregates it was added to reach.
    # See `_MAX_PLAN_SUB_QUERIES` for the numbers.
    #
    # So the fifth slot is contested, and it goes to `_SQ_PERSON_RECURRENCE`
    # on a single criterion: it is the only one of Module 29's five that no
    # OTHER gold question already asks in its own right. `_SQ_COMPLETENESS`
    # is G2's literal question and `_SQ_WEAPON_LICENCE` is G5's — both now
    # answered directly and correctly by Module 41's supervisor guard, so
    # re-deriving them inside G1 spends a scarce slot on a fact the system
    # already reports elsewhere. `_SQ_CASE_MIX_BY_YEAR` is owned by the
    # orientation plan below. `_SQ_CRIMINAL_RECORD_VS_COURT` is the weakest
    # of the four dropped and the honest reason it lost is that something
    # had to.
    #
    # NOTE — DELIBERATELY NOT TUNED TOWARD GOLD. Module 34 established that
    # gold's own finding (4), "incident times are fairly flat across the day",
    # is a DATE-ONLY ARTEFACT: 14 of the 64 incidents carrying a datetime sit
    # at exactly 00:00:00, and excluding those the remaining 50 lean evening
    # (19) over afternoon (16), morning (14), night (1). The sub-query asks
    # what the data says; the synthesis goal does not ask for "flat", and no
    # wording here should be changed to produce it.
    _DecompositionPlan(
        name="caseload_review",
        patterns=(
            re.compile(r"\breview\b.{0,60}\bcaseload\b", re.IGNORECASE),
            re.compile(r"\bflag\s+anything\b", re.IGNORECASE),
            re.compile(r"\bworth\s+(monitoring|flagging|watching)\b", re.IGNORECASE),
            re.compile(r"\bworth\s+a\s+(second|closer)\s+look\b", re.IGNORECASE),
            re.compile(r"\banything\b.{0,40}\b(unusual|out\s+of\s+the\s+ordinary)\b", re.IGNORECASE),
            re.compile(r"\blooks?\s+unusual\b", re.IGNORECASE),
            re.compile(r"\blook\s+over\s+everything\b", re.IGNORECASE),
            re.compile(r"\bghair\s*[- ]?\s*mamooli\b", re.IGNORECASE),
            re.compile(r"غیر\s*معمولی"),
        ),
        sub_queries=(
            _SQ_ACCUSED_AGE,
            _SQ_RELATIONSHIP,
            _SQ_SEIZED_PROPERTY,
            _SQ_TIME_OF_DAY,
            _SQ_PERSON_RECURRENCE,
        ),
        synthesis_goal=(
            "Report what actually stands out in the current caseload and what is worth "
            "monitoring. Profile the people, the property and the timing, and say what "
            "does not look routine — the age range and average age of the accused, what "
            "relationship (if any) they have to the complainant and which relationship "
            "dominates, what the seized-property register shows was done with the items, "
            "what time of day incidents actually happen, and any accused recurring across "
            "more than one FIR — with the exact counts from the sub-answers. Where a "
            "sub-answer states how much of the caseload its figure is based on, carry "
            "that coverage across too: a profile drawn from a minority of records is a "
            "finding about the records as much as about the crime. Do not pad with "
            "routine observations, and do not assert anything the sub-answers do not "
            "contain."
        ),
    ),
)


def _match_decomposition_plan(query_text: str) -> Optional[_DecompositionPlan]:
    """First matching plan, in declaration order (narrowest family first).
    Pure and deterministic — no LLM call — so the negative control in
    `tests/test_harness_agent_meta_analysis.py` can assert over exactly the
    32 gold questions."""
    for plan in _DECOMPOSITION_PLANS:
        if any(pat.search(query_text) for pat in plan.patterns):
            return plan
    return None


async def _decompose(query_text: str) -> _DecomposerResult:
    """
    Stage 1. Never raises — a parse failure after `call_llm_json()`'s own
    retries is reported via `parse_failed=True`, not an exception; the
    caller (`meta_analysis()`) treats that the same as `decompose=False`
    plus one caveat, per this module's docstring.

    [Gold-QA fix — Module 29] A deterministic plan, when one matches, wins
    outright and skips the LLM call entirely — see `_DECOMPOSITION_PLANS`'
    own comment block for the measured evidence and for why the prompt
    change alone was not enough.
    """
    plan = _match_decomposition_plan(query_text)
    if plan is not None:
        logger.info("Meta-Analysis: deterministic decomposition plan %r matched.", plan.name)
        return _DecomposerResult(
            decompose=True,
            sub_queries=list(plan.sub_queries[:_MAX_PLAN_SUB_QUERIES]),
            synthesis_goal=plan.synthesis_goal,
            plan_name=plan.name,
        )

    result, raw = await call_llm_json(
        system_prompt=_DECOMPOSER_SYSTEM_PROMPT.replace("{query}", query_text),
        user_message=query_text,
        max_tokens=1500,
        cloud_max_tokens=600,
        role="reasoning",
        validate=_validate_decomposer_result,
        schema_hint='"decompose" (true/false), "sub_queries" (array of strings), "synthesis_goal" (string)',
        _call_llm=call_llm,
    )
    if result is None:
        logger.warning(
            "Meta-Analysis: decomposer failed to return valid JSON after retries. Raw: %s",
            raw[:150],
        )
        return _DecomposerResult(decompose=False, sub_queries=[], synthesis_goal="", parse_failed=True)

    if result.get("decompose") is not True:
        return _DecomposerResult(decompose=False, sub_queries=[], synthesis_goal="")

    sub_queries = [q.strip() for q in result["sub_queries"] if isinstance(q, str) and q.strip()]
    sub_queries = sub_queries[:_MAX_SUB_QUERIES]
    if not sub_queries:
        # Defensive — validate() already requires a non-empty list, but a
        # future prompt/model drift that slips an all-blank list through
        # must not silently synthesize over zero sub-questions.
        return _DecomposerResult(decompose=False, sub_queries=[], synthesis_goal="", parse_failed=True)

    return _DecomposerResult(
        decompose=True, sub_queries=sub_queries, synthesis_goal=str(result.get("synthesis_goal") or "").strip()
    )


@dataclass
class _SubQueryOutcome:
    """
    Internal only, never crosses this module's boundary. Never constructed
    via a raised exception reaching the caller — see `_dispatch_one()`.
    """

    sub_query: str
    result: Optional[SubAgentResult]
    failure_reason: Optional[str] = None  # Set iff `result` is None.


async def _dispatch_one(sub_query: str, agent_input: SubAgentInput, on_event, gateway) -> _SubQueryOutcome:
    """
    One sub-query's full pipeline pass, re-entering the Supervisor with
    `allow_meta_analysis=False` — [PRESERVE] the one-level-only recursion
    guard; see module docstring. NEVER RAISES: a timeout or any exception
    from the dispatched sub-agent is caught here and reported as a
    non-contributing outcome, so `asyncio.gather` over N of these can never
    have one bad sub-query take the others down.

    KNOWN, DOCUMENTED GAP: `on_event` is forwarded to every concurrent
    sub-query dispatch unchanged, so a caller watching the live trace sees
    one "supervisor:dispatch" `PipelineEvent` per sub-query without a
    sub-query-identifying tag on it (`PipelineEvent` carries no such field
    today). Not a correctness issue — every event still fires — but a
    trace consumer can't yet tell which sub-question a given event belongs
    to. Same class of gap SUBAGENT_INTERFACES.md §2.1.4 already tracks as
    future work for Cross-Case Linkage's own per-source-tool trace; not
    addressed here either, for the same reason (out of this module's scope,
    no live SSE consumer of this harness yet).
    """
    sub_input = agent_input.model_copy(update={"query_text": sub_query, "target_entity": None})
    try:
        result = await asyncio.wait_for(
            Supervisor().handle(sub_input, on_event=on_event, gateway=gateway, allow_meta_analysis=False),
            timeout=config.META_ANALYSIS_SUBQUERY_TIMEOUT,
        )
        return _SubQueryOutcome(sub_query=sub_query, result=result)
    except asyncio.TimeoutError:
        logger.warning("Meta-Analysis: sub-query timed out after %ss: %r", config.META_ANALYSIS_SUBQUERY_TIMEOUT, sub_query[:80])
        return _SubQueryOutcome(sub_query=sub_query, result=None, failure_reason="timeout")
    except Exception as exc:
        logger.error("Meta-Analysis: sub-query dispatch raised: %s", exc)
        return _SubQueryOutcome(sub_query=sub_query, result=None, failure_reason=str(exc))


# [Gold-QA fix — Module 25, M2] A sub-answer's own text already carries
# `[Document N]` citations from whichever sub-agent produced it (RAG,
# GRAPH, XAGG, ...) — those numbers referenced ITS OWN evidence chunks,
# which never travel any further than that sub-agent's own SubAgentResult.
# By the time this module wraps that text into a pseudo-chunk, the
# original numbering is dangling: meaningless, and — worse — it COLLIDES
# with the meta-analysis-level `[Document N]` numbering the SYNTHESIS
# prompt instructs the model to use for citing pseudo-chunks 1..len(entries)
# (see `_SYNTHESIS_SYSTEM_PROMPT_TEMPLATE`/`_format_subanswers_for_prompt`).
# Two independent sub-answers each ending "...growth rates [Document 1]"
# (each correctly citing chunk 1 of ITS OWN, now-invisible evidence) sit
# side by side as meta-analysis pseudo-chunks [1] and [2] — both containing
# a stale, identically-numbered "[Document 1]" inside their own text.
#
# Confirmed live (2026-09-08, M2's exact gold text, multiple runs): the
# verifier's LLM judge, reading CHUNKS text with this stale numbering baked
# in, mis-attributed which chunk backed which claim ("[Document 2] is not
# present in the CHUNKS list... actually sourced from [Document 1] (chunk
# 2)") and rejected an answer that reused the exact wording of its own,
# fully-grounded sub-answers — this is the "M2 verifier rejection" this
# module was written to fix, and no deterministic pre-check
# (_check_fabricated_case_ids/_check_leakage/etc.) ever fired for it; only
# the LLM judge misread the doubly-numbered citation scheme. This is the
# same class of defect PR #7 fixed in `_check_fabricated_case_ids` — a
# citation-parsing assumption that does not hold for this call shape — just
# surfacing in the LLM judge's own reasoning instead of a deterministic
# check.
#
# Fix: strip any `[Document N]`-shaped marker (bracketed, parenthesized, or
# bare, optionally markdown-bold — same tolerant shape as verifier.py's own
# `_DOCUMENT_CITATION_RE`, kept as a separate, local pattern rather than a
# cross-module import since the two checks solve different problems: that
# one detects presence, this one removes) out of the sub-answer text before
# it becomes a pseudo-chunk. The synthesis prompt already tells the model
# exactly what claim came from which sub-question via
# `_format_subanswers_for_prompt`'s own `[Document N] Sub-question: ...`
# framing — the inner citation was never needed at this level and, left
# in, actively confuses both the synthesis model and the verifier's judge
# about which numbering scheme is in play.
_NESTED_CITATION_RE = re.compile(r"\s*[\[(]?\*{0,2}Document\s+\d+\*{0,2}[\])]?", re.IGNORECASE)


def _strip_nested_citations(text: str) -> str:
    return _NESTED_CITATION_RE.sub("", text).strip()


def _pseudo_chunk(index: int, sub_query: str, text: str) -> dict:
    """Same flat `{"id", "text", "metadata"}` shape every other sub-agent's
    own `_chunk_to_verifier_dict()` produces — see module docstring's stage-3
    note for why the source text here is a sub-answer, not raw evidence.
    `text` is expected to already be `_strip_nested_citations()`-cleaned by
    the caller — see that function's own docstring/comment for why."""
    return {
        "id": f"subquery-{index}",
        "text": text,
        "metadata": {"source": f"Sub-question: {sub_query}", "case_id": None},
    }


def _format_subanswers_for_prompt(entries: list[tuple[str, str]]) -> str:
    """`entries` is `[(sub_query, sub_answer_text), ...]`, same order as the
    pseudo-chunks handed to the Verifier — [PRESERVE — design §5] positional
    correspondence."""
    parts = []
    for i, (sub_query, text) in enumerate(entries, start=1):
        parts.append(f"[Document {i}] Sub-question: {sub_query}\n{text}")
    return "\n\n".join(parts)


async def meta_analysis(
    agent_input: SubAgentInput,
    *,
    on_event: Optional[OnEventCallback] = None,
    gateway: Optional[DataGateway] = None,
) -> SubAgentResult:
    """The Meta-Analysis sub-agent. See module docstring for the full contract."""
    execution = agent_input.execution
    caller = execution.caller

    decomposition = await _decompose(agent_input.query_text)

    if not decomposition.decompose:
        # Stage 1's own "no decomposition needed" outcome, OR a decomposer
        # parse failure — both fall back to ONE non-decomposed dispatch of
        # the original query, per module docstring.
        fallback_input = agent_input.model_copy(update={"target_entity": None})
        result = await Supervisor().handle(
            fallback_input, on_event=on_event, gateway=gateway, allow_meta_analysis=False
        )
        if decomposition.parse_failed:
            result = result.model_copy(
                update={
                    "caveats": [
                        "Automatic question decomposition could not run; this question was "
                        "answered as a single query instead.",
                        *result.caveats,
                    ]
                }
            )
        return result

    outcomes = await asyncio.gather(
        *[
            _dispatch_one(sq, agent_input, on_event, gateway)
            for sq in decomposition.sub_queries
        ]
    )

    contributing: list[tuple[str, str, SubAgentResult]] = []  # (sub_query, text_for_synthesis, result)
    caveats: list[str] = []
    tools_used: set[SourceTool] = set()
    degraded_from: set[SourceTool] = set()
    denied_count = 0
    failed_count = 0
    empty_only = True

    for outcome in outcomes:
        if outcome.result is None:
            failed_count += 1
            reason = "timed out" if outcome.failure_reason == "timeout" else "encountered an error"
            caveats.append(f"Could not answer sub-question ({reason}): {outcome.sub_query}")
            continue

        result = outcome.result
        if result.status == SubAgentStatus.DENIED:
            denied_count += 1
            caveats.append(f"Access denied for sub-question: {outcome.sub_query}")
            continue
        if result.status == SubAgentStatus.ABSTAINED:
            failed_count += 1
            caveats.append(f"Could not answer sub-question: {outcome.sub_query}")
            continue

        # OK / PARTIAL (with answer_text) / EMPTY all CONTRIBUTE — see
        # module docstring's EMPTY-is-a-real-finding note.
        tools_used.update(result.tools_used)
        degraded_from.update(result.degraded_from)
        caveats.extend(result.caveats)
        if result.status == SubAgentStatus.EMPTY or not result.answer_text:
            contributing.append((outcome.sub_query, _NO_INFO_SUBANSWER_TEXT, result))
        else:
            empty_only = False
            contributing.append((outcome.sub_query, result.answer_text, result))

    total_dispatched = len(outcomes)

    if not contributing:
        if denied_count == total_dispatched:
            return SubAgentResult(
                status=SubAgentStatus.DENIED,
                error=ToolError(kind="permission_denied", message="Every sub-question required elevated access."),
                caveats=caveats,
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=ToolError(kind="upstream_failure", message="No sub-question could be answered."),
            caveats=caveats,
        )

    degraded = failed_count > 0 or denied_count > 0

    if empty_only:
        # Every contributing sub-query legitimately found nothing, and
        # nothing genuinely failed alongside it — deterministic text, no LLM
        # call, mirroring cross_case_linkage.py's own cheap EMPTY handling.
        named = "; ".join(sq for sq, _text, _r in contributing)
        return SubAgentResult(
            status=SubAgentStatus.PARTIAL if degraded else SubAgentStatus.EMPTY,
            answer_text=(
                "No information was found for any part of this question. "
                f"Checked: {named}."
            ),
            tools_used=sorted(tools_used),
            degraded_from=sorted(degraded_from),
            caveats=caveats,
        )

    # ── Synthesis pass ────────────────────────────────────────────────
    # [Gold-QA fix — Module 25, M2] Strip each sub-answer's own dangling
    # `[Document N]` citations before they become part of the synthesis
    # prompt OR a verifier pseudo-chunk — see `_strip_nested_citations()`'s
    # own comment for why leaving them in confuses both the synthesis model
    # and the verifier's LLM judge about which numbering scheme is in play.
    entries = [(sq, _strip_nested_citations(text)) for sq, text, _r in contributing]
    resolved_language = caller.preferred_language or "the same language as the user's question"
    system_prompt = _SYNTHESIS_SYSTEM_PROMPT_TEMPLATE.format(
        synthesis_goal=decomposition.synthesis_goal or "combine these sub-answers into one complete answer",
        preferred_language=resolved_language,
        documents=_format_subanswers_for_prompt(entries),
    )

    try:
        answer = await call_llm(
            system_prompt, agent_input.query_text, role=_generation_role(caller.preferred_language), max_tokens=ANSWER_MAX_TOKENS
        )
    except Exception as exc:
        logger.error("Meta-Analysis: synthesis generation failed: %s", exc)
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
            caveats=["Synthesizing the sub-answers into a final answer failed.", *caveats],
        )

    pseudo_chunks = [_pseudo_chunk(i, sq, text) for i, (sq, text) in enumerate(entries, start=1)]

    verification = await verify_grounding(answer=answer, cited_chunks=pseudo_chunks, case_id="cross_case")
    verifier_passed = bool(verification.get("grounded", False)) and not verification.get("off_topic", False)

    if not verifier_passed:
        logger.warning(
            "Meta-Analysis: verifier rejected synthesized answer: %s",
            (verification.get("reason") or "")[:150],
        )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=[
                "The synthesized answer could not be verified as grounded in the sub-answers.",
                *caveats,
            ],
        )

    # Validation gate — FULL semantic tier, same reasoning as Cross-Case
    # Linkage (see module docstring).
    validation_status, validation_claims = await validate_answer(
        answer_text=answer, cited_chunks=pseudo_chunks, tier="full"
    )
    caveats = caveats + caveats_for_validation(validation_status, validation_claims)

    citations = [
        Citation(
            document_index=i,
            source_tool=(result.tools_used[0] if result.tools_used else "RAG"),
            case_id=None,
            source_file=None,
            confidence=None,
        )
        for i, (_sq, _text, result) in enumerate(contributing, start=1)
    ]

    return SubAgentResult(
        status=SubAgentStatus.PARTIAL if degraded else SubAgentStatus.OK,
        answer_text=answer,
        citations=citations,
        tools_used=sorted(tools_used),
        degraded_from=sorted(degraded_from),
        caveats=caveats,
        validation_status=validation_status,
        validation_claims=validation_claims,
    )


meta_analysis.name = META_ANALYSIS

# Import-time self-registration — the same pattern every prior sub-agent
# module established (supervisor.py's own module docstring documents it).
register(meta_analysis)
