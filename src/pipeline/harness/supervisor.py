"""
Supervisor — src/pipeline/harness/supervisor.py (Phase 1).

Source of truth: AGENT_HARNESS_IMPLEMENTATION_PLAN.md §1/§3 (what the
Supervisor is/does), SUBAGENT_INTERFACES.md §0/§2 (the exact contracts),
AGENT_HARNESS_DESIGN.md (the *why*). Per the plan, the Supervisor does
exactly three things and nothing else:

  (a) Classify the incoming question against the 8 sub-agent names.
  (b) Thread `ExecutionContext` through to whichever sub-agent it dispatches
      to, COMPLETELY UNCHANGED. [RENAMED — AGENT_HARNESS_IMPLEMENTATION_PLAN.md
      §10.1] Was `CallerContext`, threaded as `SubAgentInput.caller`; now
      `SubAgentInput.execution: ExecutionContext`, wrapping `CallerContext`
      unchanged as `execution.caller`. Same "threaded unchanged" rule applies
      to the whole `ExecutionContext`, not just the nested `caller`.
  (c) Return exactly the `SubAgentResult` it gets back — untouched,
      unreformatted, unwrapped.

It never talks to more than one sub-agent per question, never touches a
tool directly, and never sees raw evidence.

PHASE 1 BUILD NOTE. None of the 8 sub-agents exist as real modules yet
(they are Phase 2+). This module therefore also owns a REGISTRATION
mechanism — a name -> `SubAgent` map that future sub-agent modules
populate via `register()` at their own import time — and defines the
"classified to a sub-agent with no registered handler yet" outcome
explicitly, since that is literally every route's current state. Wiring
the old `orchestrator.py` as a fallback for unregistered routes is
explicitly OUT OF SCOPE this phase (see AGENT_HARNESS_IMPLEMENTATION_PLAN.md
§6, Rollout strategy) — an unregistered route returns a clear, typed
"not yet available" `SubAgentResult`, never a silent fallback and never a
crash. Nothing in this file is wired into `main.py`'s live `chat_endpoint`,
`orchestrator.py`, or `router.py`'s own behavior — this module is built and
tested in isolation, per the same rollout doc.

── CLASSIFICATION: THE ROUTE -> SUB-AGENT BRIDGE (a documented, deliberate
   choice, not a guess) ─────────────────────────────────────────────────

`router.py::route_query()` is REUSED VERBATIM — not reimplemented, not
"improved" — per the plan's explicit instruction, since its deterministic
overrides and few-shot classification are already tuned against real,
documented misclassification failures (see router.py's own extensive
comments; SUBAGENT_INTERFACES.md §1.4's XGRAPH/XNETWORK/XAGG precedence
notes).

However: `route_query()` classifies into 9 TOOL-LEVEL routes (`DIRECT,
RAG, WEB, SQL, GRAPH, GRAPH_HYBRID, XGRAPH, XAGG, XNETWORK`) — it has no
concept of the 8 SUB-AGENT names at all, and never did (confirmed against
`orchestrator.py` too: today's orchestrator also only branches on
`route_str` + `output_format`, with no Case Summarization / Timeline
Building / Investigative Analysis / Cross-Case Linkage / Data-Quality
concept anywhere). Bridging the two requires *some* new mapping — the
question is only how much new judgement that mapping encodes. Resolved,
by explicit user decision during this session (see git log for this
commit), as the minimal-deterministic-map option:

  - `route_query()` is called completely unchanged; its output is never
    second-guessed or re-derived.
  - A single, small, explicitly-named translation table
    (`_ROUTE_TO_SUBAGENT` below) maps each of its 9 routes to one of the
    8 sub-agent names, using the least-ambiguous available reading (RAG ->
    Semantic Search; GRAPH/GRAPH_HYBRID -> Case Summarization; SQL ->
    Investigative Analysis; XGRAPH/XNETWORK -> Cross-Case Linkage; XAGG ->
    Large-Scale Aggregate; WEB -> Semantic Search as the closest available
    fallback. [Reconciliation fix — harness-reconciliation Unit 2] DIRECT ->
    `NO_SUB_AGENT`, not Semantic Search: DIRECT answers from general
    knowledge with no retrieval and the Verifier never gates it, so forcing
    it through Semantic Search's retrieve-generate-verify pipeline would be
    a behavioral regression, not a fallback — see `NO_SUB_AGENT`'s own
    comment below).
  - `output_format in {file_pdf, file_xlsx, file_docx}` always overrides
    to Report Drafting, regardless of route — this mirrors
    `orchestrator.py`'s own existing behavior, where file generation is a
    post-hoc step layered on top of any route, not a route of its own.
  - **Timeline Building and Data-Quality/Extraction-Coverage are NOT
    reachable via classification in this phase.** `router.py` has no
    signal for either — no route was ever built for "give me a dated
    event timeline" or "how much evidence exists for this case" (the
    latter is explicitly a NEW capability per plan §7.3, with no
    predecessor in `orchestrator.py` at all). Inventing new keyword/regex
    triggers for these two was explicitly rejected as out of scope for
    this session (that would be new classification logic layered on top
    of, not reuse of, router.py's tuned classifier) rather than guessed
    into existence. This is a known, documented gap, not a silent one:
    `SUB_AGENT_NAMES` still lists both (matching the plan's 8-sub-agent
    scope), `register()` will happily accept a handler for either, and
    `_ROUTE_TO_SUBAGENT`'s docstring is the single place this gap is
    tracked. Extending classification to reach them is future work for
    whichever session builds `router.py`-level (or supervisor-level)
    triggers for those two query shapes, evaluated the same way XAGG/
    XGRAPH/XNETWORK's own deterministic overrides were: against
    live-confirmed misclassification failures, not guessed patterns.

**Discrepancy flagged, not silently resolved:** SUBAGENT_INTERFACES.md
§2.1 titles its table "The seven sub-agents" and never lists Data-Quality/
Extraction-Coverage at all; AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4/§7.3
adds it as an eighth, describing it as a new capability with no
`SubAgentResult`-shaped contract written for it yet. This module follows
the plan doc's 8-name scope (the plan is this session's explicit brief),
but the interfaces doc has not been updated to match — flagging here per
this session's instruction to surface doc disagreements rather than
silently pick one.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from src.data_gateway.base import DataGateway
from src.pipeline.harness.types import (
    CROSS_CASE_ROLES,
    PipelineEvent,
    SubAgent,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
)
from src.pipeline.router import route_query

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# Canonical sub-agent names (AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4).
# Exactly these 8 strings are the only valid classification targets and
# the only valid `register()` keys.
# ═══════════════════════════════════════════════════════════════════════

SEMANTIC_SEARCH = "Semantic Search"
LARGE_SCALE_AGGREGATE = "Large-Scale Aggregate"
CASE_SUMMARIZATION = "Case Summarization"
TIMELINE_BUILDING = "Timeline Building"
CROSS_CASE_LINKAGE = "Cross-Case Linkage"
INVESTIGATIVE_ANALYSIS = "Investigative Analysis"
REPORT_DRAFTING = "Report Drafting"
DATA_QUALITY = "Data-Quality/Extraction-Coverage"
# [AMENDMENT — findings.md Module 8, "Local Search"] A 9th sub-agent name,
# additive on top of the plan's original 8-name scope (this module's own
# docstring above documents that scope and its own Data-Quality
# discrepancy note — this follows the same "flag the amendment, don't
# rewrite the history" convention rather than silently editing the "8"s
# above). Local Search composes a genuinely new tool
# (tools/local_search.py — semantic entity-access-point matching, fanned
# out through the existing retrieve_graph() traversal plus a new
# community-report join) that has no predecessor in router.py's 9
# tool-level routes at all, the same "new capability, no route to reuse"
# shape Data-Quality/Extraction-Coverage was in — see the dedicated
# trigger-pattern block near `classify_to_subagent()` below for how it
# actually becomes reachable (unlike Data-Quality, it IS classification-
# reachable, via a narrow trigger-vocabulary override on the confirmed
# officer-role-reference failure class, not a route from router.py).
LOCAL_SEARCH = "Local Search"

# [AMENDMENT — findings.md Module 9, "Global Search"] A 10th sub-agent
# name, same additive-amendment discipline as LOCAL_SEARCH above. Global
# Search composes a new tool (tools/global_search.py — fetches EVERY
# community report for a hierarchy level, not a top-k similarity cut) and
# runs a genuinely different algorithm shape on top of it (map-reduce
# across batched, shuffled reports) than any existing sub-agent — see
# agents/global_search.py's own module docstring. Reachable via its own
# narrow classification override (see the dedicated trigger-pattern block
# near `classify_to_subagent()` below), only for the XNETWORK route's
# whole-dataset-aggregation shape — narrower than XNETWORK's existing
# default (a specific network/cluster question), which still falls
# through to CROSS_CASE_LINKAGE unchanged.
GLOBAL_SEARCH = "Global Search"

# [AMENDMENT — findings.md Module 10, "Meta-Analysis"] An 11th sub-agent
# name, same additive-amendment discipline as LOCAL_SEARCH/GLOBAL_SEARCH
# above. Meta-Analysis is the OUTERMOST layer: it does not compose a tool at
# all — it decomposes the QUESTION into bounded sub-questions and re-enters
# this same Supervisor (one level only, never a tool directly) for each one,
# concurrently, then synthesizes across the independently-produced
# sub-answers. See agents/meta_analysis.py's own module docstring for the
# full three-stage design (decompose / dispatch / aggregate) and the
# `allow_meta_analysis` recursion guard `classify_to_subagent()`/
# `Supervisor.handle()` both grow below — the one piece of this amendment
# that has no precedent in LOCAL_SEARCH/GLOBAL_SEARCH, since neither of
# those ever calls back into the Supervisor itself.
META_ANALYSIS = "Meta-Analysis"

SUB_AGENT_NAMES: tuple[str, ...] = (
    SEMANTIC_SEARCH,
    LARGE_SCALE_AGGREGATE,
    CASE_SUMMARIZATION,
    TIMELINE_BUILDING,
    CROSS_CASE_LINKAGE,
    INVESTIGATIVE_ANALYSIS,
    REPORT_DRAFTING,
    DATA_QUALITY,
    LOCAL_SEARCH,
    GLOBAL_SEARCH,
    META_ANALYSIS,
)

# [Reconciliation fix — harness-reconciliation Unit 2] Returned by
# classify_to_subagent() for the DIRECT route. DIRECT answers from general
# knowledge with no retrieval, and the Verifier deliberately never gates it
# (design §1) — it is definitionally outside the harness's
# retrieval-and-verification scope, not a Semantic Search query in disguise.
# `_ROUTE_TO_SUBAGENT` previously mapped "DIRECT" to SEMANTIC_SEARCH, which
# would have forced a DIRECT query through the full RAG retrieve-generate-
# verify pipeline (wrong answer shape, and would break token streaming, the
# moment DIRECT is ever included in a live cutover route set) rather than
# being recognized as "no sub-agent, caller's own concern" the way the
# legacy orchestrator already treats it. Not currently live-traffic-
# reachable (`config.HARNESS_CUTOVER_ROUTES` gates by route BEFORE
# `Supervisor.handle()` is ever called, and DIRECT is not in that set today
# — see main.py's chat_endpoint), but the classification table itself must
# not assert something false regardless of whether today's config happens
# to mask it. See `Supervisor.handle()` for how a NO_SUB_AGENT
# classification is handled if it is ever dispatched.
NO_SUB_AGENT = "__direct__"

_FILE_OUTPUT_FORMATS = frozenset({"file_pdf", "file_xlsx", "file_docx"})

# See the module docstring's "CLASSIFICATION" section for the full
# rationale. Deliberately a flat, inspectable table — not logic — so the
# mapping decision stays visible and auditable in one place.
_ROUTE_TO_SUBAGENT: dict[str, str] = {
    "RAG": SEMANTIC_SEARCH,
    "GRAPH": CASE_SUMMARIZATION,
    "GRAPH_HYBRID": CASE_SUMMARIZATION,
    "SQL": INVESTIGATIVE_ANALYSIS,
    "XGRAPH": CROSS_CASE_LINKAGE,
    "XNETWORK": CROSS_CASE_LINKAGE,
    "XAGG": LARGE_SCALE_AGGREGATE,
    "DIRECT": NO_SUB_AGENT,
    "WEB": SEMANTIC_SEARCH,
}

# [Reconciliation fix — harness-reconciliation Unit 2] Cheap, defense-in-
# depth guard: a cross-case sub-agent must never be dispatched for a
# within-case scope. Redundant today (router.py forces every route except
# XGRAPH/XAGG/XNETWORK back to within_case unconditionally, so this
# combination should already be unreachable), kept anyway so a future
# router.py change cannot silently route a within-case query into a
# cross-case sub-agent — where the tool's own role gate would then produce a
# confusing DENIED on a query that never asked to cross cases, rather than a
# clean same-case answer.
_CROSS_CASE_SUBAGENTS = frozenset(
    {CROSS_CASE_LINKAGE, LARGE_SCALE_AGGREGATE, GLOBAL_SEARCH, META_ANALYSIS}
)
# [AMENDMENT — findings.md Module 10] META_ANALYSIS's inclusion above IS
# this module's RBAC answer (resolved via AskUserQuestion, not guessed):
# aggregating "a larger chunk of data" is inherently cross-case-shaped in
# most real uses, so a compound-question trigger match (below) on a query
# whose own `case_scope` is NOT `"cross_case"` demotes straight to
# SEMANTIC_SEARCH via this exact guard — no N-way decompose+dispatch is ever
# attempted for a within-case compound question. When `case_scope` IS
# `"cross_case"`, Meta-Analysis dispatches, and — mirroring
# `cross_case_linkage.py`'s/`large_scale_aggregate.py`'s explicit "no third
# gate" discipline — `meta_analysis.py` itself adds NO role check of its
# own; each sub-query re-enters this Supervisor and is gated on its own
# merits by whichever tool it actually resolves to.

# ═══════════════════════════════════════════════════════════════════════
# [Contract amendment — classification reachability, pre-cutover-Part-3]
#
# PROVISIONAL classification triggers for Timeline Building and a broader
# Investigative Analysis reach, resolved via AskUserQuestion before any of
# this was written (see AGENT_HARNESS_IMPLEMENTATION_PLAN.md's progress-log
# entry for this branch for the full "Problem A" reasoning): neither
# sub-agent has any "live-confirmed misclassification failure" evidence to
# point to, the standard every prior XAGG/XGRAPH/XNETWORK-style override in
# `router.py` was held to — because this harness has carried real traffic
# on exactly one sub-agent so far (Semantic Search), and that's off by
# default. Explicitly shipped as provisional, derived from each sub-agent's
# own documented SHAPE (SUBAGENT_INTERFACES.md §2.1's rows — neither has a
# dedicated "trigger vocabulary" subsection the way XGRAPH/XAGG/XNETWORK do
# in §1.4/§1.5/§1.6, so there was no existing vocabulary to transcribe),
# not from any observed failure. Revisit once Semantic Search's own cutover
# has carried enough real traffic to define what "evidence" means for this
# harness at all.
#
# Data-Quality/Extraction-Coverage is DELIBERATELY NOT given a trigger here
# — resolved via the same AskUserQuestion to leave it exactly as plan
# §7.3 already, separately, decided ("Routable-only for MVP... parked, not
# rejected") rather than override that existing decision as a side effect
# of this change.
#
# [PRESERVE — the actual safety property this whole block exists for]
# These patterns run ENTIRELY inside this function, AFTER `route_query()`
# has already returned its own unchanged nine-route classification, and
# they only ever REMAP the sub-agent name this function returns — they
# never touch `router.py`, never change what `route_query()` itself
# returns, and are invisible to `orchestrator.py` (which calls
# `route_query()` directly and never calls `classify_to_subagent()` at
# all). `orchestrator.py`'s own `if/elif` chain over `route_str` has no
# case for a route it doesn't already know about — confirmed by reading it
# before writing this — so inventing a NEW route STRING (e.g. a tenth
# `"TIMELINE"` value) was considered and rejected: it would silently
# break real, still-live `orchestrator.py` traffic for every query that
# newly classified that way, for every route not yet added to
# `HARNESS_CUTOVER_ROUTES`. See `tests/test_harness_supervisor.py`'s own
# regression test proving `route_query()`'s nine-route contract is
# unaffected by anything below.
# ═══════════════════════════════════════════════════════════════════════

# Timeline Building — a case wants its dated events laid out in order, not
# a general document/graph lookup. Narrow by design, matching the same
# discipline router.py's own override lists use ("unambiguous trigger
# language", not every phrasing that could plausibly want a timeline).
_TIMELINE_TRIGGER_PATTERNS = [
    re.compile(r"\btime\s*line\b", re.IGNORECASE),
    re.compile(r"\bchronological\s+order\b", re.IGNORECASE),
    re.compile(r"\b(sequence|order)\s+of\s+events\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+happened\s+when\b", re.IGNORECASE),
    re.compile(r"ٹائم\s*لائن"),
    re.compile(r"واقعات\s*کی\s*ترتیب"),
    re.compile(r"کب\s*کیا\s*ہوا"),
    re.compile(r"\bwaqeat\s*ki\s*tarteeb\b", re.IGNORECASE),
    re.compile(r"\bkab\s*kya\s*hua\b", re.IGNORECASE),
]

# Investigative Analysis's broader "deep synthesis across everything"
# shape (plan §4 row 6), not just the SQL-triggered slice it's reachable
# through today. Deliberately does NOT include generic words like
# "analysis" or "investigate" alone — those are common enough in ordinary
# case questions that they'd swallow queries meant for Semantic Search or
# Case Summarization; every pattern here requires an explicit
# comprehensiveness/depth qualifier alongside the analysis word.
_INVESTIGATIVE_ANALYSIS_TRIGGER_PATTERNS = [
    re.compile(r"\bdeep\s*dive\b", re.IGNORECASE),
    re.compile(r"\b(full|complete|comprehensive|in-?depth)\s+analysis\b", re.IGNORECASE),
    re.compile(r"\b(detailed|comprehensive|full)\s+investigation\b", re.IGNORECASE),
    re.compile(r"\bfull\s+picture\s+of\s+(the|this)\s+case\b", re.IGNORECASE),
    re.compile(r"مکمل\s*تحقیقات"),
    re.compile(r"تفصیلی\s*تجزیہ"),
    re.compile(r"گہرائی\s*سے\s*تجزیہ"),
    re.compile(r"\bmukammal\s*tehqiqat\b", re.IGNORECASE),
    re.compile(r"\btafseeli\s*tajzia\b", re.IGNORECASE),
    re.compile(r"\bgehrai\s*se\s*tajzia\b", re.IGNORECASE),
]

# Cross-case routes already have their own well-established, evidence-based
# precedence in router.py — a query matching both a cross-case trigger and
# one of the two provisional patterns above is a genuine ambiguity this
# function does not try to resolve heuristically. Both provisional
# overrides are skipped whenever route_query() already committed to a
# cross-case route, leaving that classification untouched.
_CROSS_CASE_ROUTES = frozenset({"XGRAPH", "XAGG", "XNETWORK"})

# ═══════════════════════════════════════════════════════════════════════
# [AMENDMENT — findings.md Module 8, "Local Search"] Local Search dispatch
# trigger, same slot and same discipline as the two provisional overrides
# above (checked before the `_ROUTE_TO_SUBAGENT` table lookup, only for
# GRAPH-shaped, non-cross-case routes) — narrow, evidence-anchored
# trigger vocabulary, not a general NER/entity-shape classifier.
#
# WHY A REGEX LIST, NOT THE BROADER "no literal identifier match" SIGNAL:
# `classify_to_subagent()` is a pure, synchronous, no-I/O function — it
# never calls the database. Actually checking "would
# graph_retriever._find_seed_nodes() find nothing for this query" would
# require a live DB round-trip at classification time, which no existing
# override does and which would change this function's contract. A
# trigger-vocabulary list is the same discipline
# `_TIMELINE_TRIGGER_PATTERNS`/`_INVESTIGATIVE_ANALYSIS_TRIGGER_PATTERNS`
# above already established for exactly this reason.
#
# SCOPE: the confirmed failure class only — a descriptive/role-based
# officer reference with no literal name, cnic, phone, or belt number in
# the query (findings.md Module 8's own live-reproduced "who is the
# investigating officer in this case" repro; see
# `src/pipeline/harness/agents/local_search.py`'s module docstring for the
# ASSIGNED_TO-role finding this pattern set exists to reach). NOT extended
# to complainant/accused/witness role references — a plausible future
# extension, but with no confirmed-failure evidence backing it yet, same
# "narrow, evidence-based, not maximally general" standard the two
# existing provisional overrides were held to.
_LOCAL_SEARCH_TRIGGER_PATTERNS = [
    re.compile(r"\binvestigating\s+officer\b", re.IGNORECASE),
    re.compile(r"\brecording\s+officer\b", re.IGNORECASE),
    re.compile(r"\bduty\s+officer\b", re.IGNORECASE),
    re.compile(r"\bwho\s+is\s+the\s+\w+\s+officer\b", re.IGNORECASE),
    re.compile(r"\bwhich\s+officer\b", re.IGNORECASE),
    re.compile(r"تفتیشی\s*افسر"),
    re.compile(r"محرر\s*افسر"),
    re.compile(r"\btaftishi\s*afsar\b", re.IGNORECASE),
]

# ═══════════════════════════════════════════════════════════════════════
# [Audit hypothesis #12] Data-Quality/Extraction-Coverage dispatch
# trigger. No route in `_ROUTE_TO_SUBAGENT` maps to DATA_QUALITY — it is
# reachable only through a trigger override, the same "narrow, evidence-
# based" discipline as `_TIMELINE_TRIGGER_PATTERNS`/
# `_LOCAL_SEARCH_TRIGGER_PATTERNS` above, and was verified unreachable
# entirely before this: every query that should land here fell through to
# Semantic Search instead. Scoped like Timeline/Investigative
# Analysis/Local Search (within-case, not one of `_CROSS_CASE_SUBAGENTS`)
# since `data_quality.py`'s own stats functions all require a case_id —
# there is no cross-case shape for "how complete is this case's data".
_DATA_QUALITY_TRIGGER_PATTERNS = [
    re.compile(r"\bdata\s+quality\b", re.IGNORECASE),
    re.compile(r"\bextraction\s+coverage\b", re.IGNORECASE),
    re.compile(r"\bhow\s+complete\s+is\b", re.IGNORECASE),
    re.compile(r"\bmissing\s+(fields?|data)\b", re.IGNORECASE),
    re.compile(r"\bunstructured\b", re.IGNORECASE),
    re.compile(r"\bstructured\s+data\b", re.IGNORECASE),
    re.compile(r"ڈیٹا\s*کوالٹی"),
    re.compile(r"کتنا\s*ڈیٹا\s*نکالا\s*گیا"),
    re.compile(r"\bdata\s*quality\s*kitni\s*hai\b", re.IGNORECASE),
    re.compile(r"\bkitna\s*data\s*nikala\s*gaya\b", re.IGNORECASE),
]

# ═══════════════════════════════════════════════════════════════════════
# [AMENDMENT — findings.md Module 9, "Global Search"] Global Search
# dispatch trigger. PROVISIONAL, same disclosure as
# _TIMELINE_TRIGGER_PATTERNS/_INVESTIGATIVE_ANALYSIS_TRIGGER_PATTERNS
# above — this harness has carried no real Global Search traffic yet, so
# there is no "live-confirmed misclassification failure" to point to the
# way _LOCAL_SEARCH_TRIGGER_PATTERNS' officer-role trigger can. Derived
# instead from GraphRAG's own stated Global Search question shape
# (findings.md Module 9's "Origin" quote: "what are the top 5 themes in
# the data?") and today's XNETWORK docstring's own narrower framing
# ("overall picture/network" — a specific cluster/network question, NOT a
# whole-dataset aggregation one).
#
# Checked ONLY for the XNETWORK route (below, not in the
# `route not in _CROSS_CASE_ROUTES` block the other two provisional
# overrides share) — XNETWORK's existing default (no match here) stays
# CROSS_CASE_LINKAGE, completely unaffected; this only narrows the ONE
# route that already means "cross-case, community-summary-based
# synthesis" into two shapes (a specific network vs. a whole-dataset
# aggregation), the same "narrow override, reuse the default otherwise"
# discipline _LOCAL_SEARCH_TRIGGER_PATTERNS itself already uses for
# GRAPH/GRAPH_HYBRID.
# ═══════════════════════════════════════════════════════════════════════
_GLOBAL_SEARCH_TRIGGER_PATTERNS = [
    re.compile(r"\btop\s+\d+\s+(themes?|patterns?|trends?)\b", re.IGNORECASE),
    re.compile(r"\bmost\s+common\s+(themes?|patterns?|methods?|crimes?)\b", re.IGNORECASE),
    re.compile(r"\brecurring\s+(themes?|patterns?)\s+across\b", re.IGNORECASE),
    re.compile(r"\b(overall|general)\s+(themes?|patterns?|trends?)\s+(in|across)\s+(the\s+)?(data|dataset|cases)\b", re.IGNORECASE),
    re.compile(r"\bacross\s+(all|the\s+entire)\s+(dataset|data|cases)\b.*\b(themes?|patterns?|trends?)\b", re.IGNORECASE),
    re.compile(r"مجموعی\s*رجحانات"),
    re.compile(r"بڑے\s*موضوعات"),
    re.compile(r"\bmajmoi\s*rujhanat\b", re.IGNORECASE),
]


# ═══════════════════════════════════════════════════════════════════════
# [AMENDMENT — findings.md Module 10, "Meta-Analysis"] Decomposition
# trigger. Same provisional disclosure as _GLOBAL_SEARCH_TRIGGER_PATTERNS
# above (no live-confirmed misclassification failure yet — derived from
# findings.md's own Module 10 "Proposed approach" wording, not observed
# traffic) and the same narrow, evidence-anchored discipline every override
# in this file follows: these patterns exist to fast-path INTO the
# decomposer LLM call cheaply, not to decide decomposition themselves — the
# actual "genuinely compound vs. one ordinary question" judgment is made by
# `meta_analysis.py`'s own decomposer step (prompts/meta_analysis_decomposer.txt),
# which can still say "no decomposition needed" even when a pattern here
# fires. Anchored on the two-part connector shapes findings.md's own
# "Proposed approach" section named explicitly ("summarize...across all",
# "aggregate...and flag", "recurring...and cross-reference"), plus one
# general compound-ask shape — never a bare single keyword like "summarize"
# or "aggregate" alone, which would swallow ordinary single-focus queries
# XAGG/Case Summarization already handle correctly (e.g. "aggregate the case
# counts by station" has no "and flag" nearby and correctly does not match).
#
# UNLIKE every other override list in this file, checked FIRST, before the
# route-specific TIMELINE/INVESTIGATIVE_ANALYSIS/LOCAL_SEARCH/GLOBAL_SEARCH
# blocks below and regardless of `route` — findings.md's own framing is
# explicit that Module 10 is "the outermost layer, dispatching down into
# whichever routes/sub-agents exist," so a genuinely compound question must
# not be swallowed by a single-route override first (e.g. an
# INVESTIGATIVE_ANALYSIS "comprehensive analysis" match on half of a
# compound query would otherwise win before this ever gets a chance). Like
# GLOBAL_SEARCH's own block, does NOT return early — the resulting
# `sub_agent = META_ANALYSIS` still flows through the shared case_scope
# demotion guard at the bottom of this function (see `allow_meta_analysis`'s
# own docstring note below and `_CROSS_CASE_SUBAGENTS`'s own comment for
# why: aggregating "a larger chunk of data" is inherently cross-case-shaped
# in most real uses, resolved via AskUserQuestion as this module's RBAC
# answer — no N-way decompose+dispatch is attempted for a within-case
# compound question; it demotes straight to SEMANTIC_SEARCH instead).
# ═══════════════════════════════════════════════════════════════════════
_META_ANALYSIS_TRIGGER_PATTERNS = [
    re.compile(r"\bsummarize\b.{0,80}\bacross all\b", re.IGNORECASE),
    re.compile(r"\baggregate\b.{0,80}\band flag\b", re.IGNORECASE),
    re.compile(r"\brecurring\b.{0,80}\bcross-?reference\b", re.IGNORECASE),
    re.compile(r"\bacross all\b.{0,60}\band\b.{0,40}\b(flag|identify|highlight)\b", re.IGNORECASE),
    re.compile(r"تمام\s*(کیسز|مقدمات)\s*میں.{0,60}(خلاصہ|نشاندہی)"),
    re.compile(r"\bsab\s*cases\b.{0,60}\b(khulasa|nishandehi)\b", re.IGNORECASE),
]


def classify_to_subagent(
    route_result: dict, query_text: str = "", *, allow_meta_analysis: bool = True
) -> str:
    """
    Translate `router.py::route_query()`'s output dict into one of the
    canonical `SUB_AGENT_NAMES`, or `NO_SUB_AGENT` for DIRECT. Does not call
    `route_query()` itself and does not re-derive its classification — see
    module docstring.

    `query_text` is optional (defaults to `""`, under which none of the
    provisional overrides below can ever match) so every existing direct
    caller — this module's own tests included — keeps working unchanged;
    `Supervisor.handle()` is the one real caller that has a query to pass,
    and does so below.

    [AMENDMENT — findings.md Module 10] `allow_meta_analysis` is the
    recursion guard `meta_analysis.py` needs and no prior sub-agent did:
    every sub-agent before it composes TOOLS, never re-enters this
    Supervisor, so nothing before Module 10 could ever recurse. Meta-Analysis
    is the first to call `Supervisor.handle()` from inside a sub-agent (once
    per sub-query, per its own module docstring) — without a guard, a
    sub-query whose text still happens to match `_META_ANALYSIS_TRIGGER_PATTERNS`
    would reclassify to META_ANALYSIS again, decomposing forever. Defaults to
    `True` (every existing/top-level caller, including this module's own
    tests, is unaffected); `meta_analysis.py` is the one caller that passes
    `False` for every one of its own sub-query dispatches, which makes a
    sub-query classify EXACTLY as if Module 10 did not exist — deterministic,
    not dependent on the sub-queries happening to avoid the trigger
    vocabulary textually.
    """
    output_format = str(route_result.get("output_format") or "chat").lower()
    route = str(route_result.get("route") or "RAG").upper()

    # [Reconciliation fix — Unit 2] DIRECT wins outright, even over a file
    # request — the router's own few-shots include a DIRECT route paired
    # with a file output_format (e.g. "write me a document about something
    # unrelated to case facts"), and routing that to Report Drafting would
    # send it through Case Summarization's case-scoped retrieval, which is
    # exactly the retrieval this query was routed away from. The legacy
    # orchestrator already handles this correctly (its DIRECT branch
    # generates the answer, then falls through to file generation) —
    # returning NO_SUB_AGENT here preserves that instead of reimplementing
    # it.
    if route == "DIRECT":
        return NO_SUB_AGENT

    if output_format in _FILE_OUTPUT_FORMATS:
        return REPORT_DRAFTING

    # [AMENDMENT — findings.md Module 10] Checked before every route-specific
    # override below — see _META_ANALYSIS_TRIGGER_PATTERNS' own comment
    # block for why this must win over TIMELINE/INVESTIGATIVE_ANALYSIS/
    # LOCAL_SEARCH/GLOBAL_SEARCH rather than being folded alongside them.
    if allow_meta_analysis and any(
        pat.search(query_text) for pat in _META_ANALYSIS_TRIGGER_PATTERNS
    ):
        sub_agent = META_ANALYSIS
    else:
        if route not in _CROSS_CASE_ROUTES:
            if any(pat.search(query_text) for pat in _TIMELINE_TRIGGER_PATTERNS):
                return TIMELINE_BUILDING
            if any(pat.search(query_text) for pat in _INVESTIGATIVE_ANALYSIS_TRIGGER_PATTERNS):
                return INVESTIGATIVE_ANALYSIS
            # [Audit hypothesis #12] Data-Quality override — no route maps
            # to it in `_ROUTE_TO_SUBAGENT`, so this trigger check is the
            # only way it is ever reachable at all.
            if any(pat.search(query_text) for pat in _DATA_QUALITY_TRIGGER_PATTERNS):
                return DATA_QUALITY
            # [AMENDMENT — findings.md Module 8] Local Search override — only
            # for the GRAPH-shaped routes it actually improves on (semantic
            # entity access-point matching is meaningless for RAG/SQL/WEB,
            # which never seed off an entity at all).
            if route in ("GRAPH", "GRAPH_HYBRID") and any(
                pat.search(query_text) for pat in _LOCAL_SEARCH_TRIGGER_PATTERNS
            ):
                return LOCAL_SEARCH

        # [AMENDMENT — findings.md Module 9] Global Search override — narrows
        # ONLY the XNETWORK route (deliberately outside the
        # `route not in _CROSS_CASE_ROUTES` guard above, since XNETWORK IS a
        # cross-case route): a whole-dataset theme/pattern aggregation
        # question goes to Global Search's map-reduce; XNETWORK's existing
        # default (a specific network/cluster question, no match here) stays
        # CROSS_CASE_LINKAGE, unaffected. See _GLOBAL_SEARCH_TRIGGER_PATTERNS'
        # own comment block for the full rationale.
        if route == "XNETWORK" and any(
            pat.search(query_text) for pat in _GLOBAL_SEARCH_TRIGGER_PATTERNS
        ):
            sub_agent = GLOBAL_SEARCH
        else:
            sub_agent = _ROUTE_TO_SUBAGENT.get(route, SEMANTIC_SEARCH)

    # [Reconciliation fix — Unit 2] case_scope demotion guard — see
    # _CROSS_CASE_SUBAGENTS's own comment for the full rationale. Deliberately
    # does NOT get bypassed by an early `return` above for GLOBAL_SEARCH or
    # META_ANALYSIS — both are cross-case-role-gated (see
    # _CROSS_CASE_SUBAGENTS), so both must still pass through here; an early
    # return would let a within-case-scoped misclassification reach either
    # directly, the exact bug this guard exists to prevent.
    case_scope = str(route_result.get("case_scope") or "within_case").lower()
    if sub_agent in _CROSS_CASE_SUBAGENTS and case_scope != "cross_case":
        return SEMANTIC_SEARCH

    return sub_agent


# ═══════════════════════════════════════════════════════════════════════
# Sub-agent registry.
#
# A future sub-agent module registers itself, typically at import time,
# e.g.:
#
#     from src.pipeline.harness.supervisor import register, SEMANTIC_SEARCH
#     async def semantic_search(agent_input: SubAgentInput) -> SubAgentResult: ...
#     semantic_search.name = SEMANTIC_SEARCH
#     register(semantic_search)
#
# Module-level (not per-Supervisor-instance) so a sub-agent module only
# has to import and call `register()` once, regardless of how many
# `Supervisor` instances exist. `Supervisor` accepts an optional registry
# override for test isolation (see tests/test_harness_supervisor.py) —
# without an override it reads this same live registry, so a sub-agent
# registered anywhere is immediately dispatchable everywhere.
# ═══════════════════════════════════════════════════════════════════════

_REGISTRY: dict[str, SubAgent] = {}


def register(sub_agent: SubAgent) -> None:
    """Register a sub-agent implementation under its own `.name`."""
    if sub_agent.name not in SUB_AGENT_NAMES:
        raise ValueError(
            f"Cannot register sub-agent with unknown name {sub_agent.name!r}. "
            f"Must be one of {SUB_AGENT_NAMES!r}."
        )
    _REGISTRY[sub_agent.name] = sub_agent


def unregister(name: str) -> None:
    """Remove a registered sub-agent, if present. No-op otherwise."""
    _REGISTRY.pop(name, None)


def get_registered(name: str) -> Optional[SubAgent]:
    return _REGISTRY.get(name)


def registered_names() -> list[str]:
    return sorted(_REGISTRY.keys())


# The "not yet available" outcome for a classified-but-unregistered route.
# Not a crash, not a silent fallback to orchestrator.py (explicitly out of
# scope this phase — see module docstring).
def _not_yet_available_result(sub_agent_name: str) -> SubAgentResult:
    return SubAgentResult(
        status=SubAgentStatus.ABSTAINED,
        answer_text=None,
        error=ToolError(
            kind="upstream_failure",
            message=(
                f"Sub-agent '{sub_agent_name}' has no registered handler yet "
                "(Phase 1: Supervisor built and tested in isolation, no "
                "sub-agents wired in)."
            ),
        ),
        caveats=[f"The '{sub_agent_name}' capability is not available yet."],
    )


# [Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 3 follow-up] "All Cases"
# case-scope resolution has three sources, in priority order, each with
# its own distinct safety argument:
#
#   1. The CURRENT query text itself — either a keyword-anchored
#      reference ("summarize case 435/26", `_case_id_from_query_text()`)
#      or a bare number pair that resolves to a real case's own display
#      number ("summarize 435/26" / "435 26", no "case"/"FIR" word at
#      all — `_case_id_from_bare_number()`). Either shape is a
#      deliberate, explicit reference the user is making RIGHT NOW, and
#      NEITHER is automatically trusted the way history is below: both
#      require a real authorization check (`_case_access_authorized()`)
#      before use, because `CallerContext.active_case_id` is documented
#      as pre-authorized by the API boundary and never re-checked below
#      it (see `CallerContext`'s own docstring) — a case number typed in
#      chat has not been through that boundary's normal case-assignment
#      check yet. Denied (not silently ignored) when the check fails, so
#      the caller gets an honest "you don't have access to that case"
#      instead of a misleading generic "no data" or "select a case"
#      message for a case they explicitly named.
#   2. A case/FIR id the ASSISTANT ITSELF mentioned earlier in this same
#      session's history (`_infer_case_id_from_assistant_history()`) —
#      trusted WITHOUT a fresh access check, because it was already
#      retrieved and shown to this exact user under this exact session's
#      own prior authorization; reusing it for a same-session follow-up
#      grants nothing new. Never scans the user's OWN historical text,
#      only "Assistant:"-prefixed lines — same reasoning as source 1: the
#      user's own text, past or present, is never self-authorizing.
#
# Matches "fir-401-26" / "FIR-97-26" / "CASE-009" (hyphenated, router.py's
# own _ACTIVE_CASE_RE vocabulary) AND the human-typed display shape
# "case 435/26" / "FIR 435/26" (space before the number, slash between
# the two halves — how an investigator actually types a case number in
# conversation, not how it's stored). Group 1 is the prefix word, group 2
# the first number, group 3 the optional second number (hyphen OR slash
# OR space separated). _normalize_case_ref() rebuilds a canonical
# "prefix-num1-num2" id from the captured groups rather than using the
# raw matched text verbatim — necessary because "case 435/26" and
# "fir-401-26" need different reconstruction, not just a case-fold.
_CASE_REF_RE = re.compile(r"\b(fir|case)[\s-]+(\d+)(?:[\s/-]+(\d+))?\b", re.IGNORECASE)


def _normalize_case_ref(m: re.Match) -> str:
    prefix = m.group(1).lower()
    num1, num2 = m.group(2), m.group(3)
    return f"{prefix}-{num1}-{num2}" if num2 else f"{prefix}-{num1}"


def _case_id_from_query_text(query_text: str) -> Optional[str]:
    """The LAST case/FIR id named in `query_text` itself, normalized, or
    `None`. Still requires _case_access_authorized() before use — see the
    module-level comment above."""
    last: Optional[str] = None
    for m in _CASE_REF_RE.finditer(query_text or ""):
        last = _normalize_case_ref(m)
    return last


def _infer_case_id_from_assistant_history(conversation_summary: Optional[str]) -> Optional[str]:
    """
    The most recently assistant-mentioned case/FIR id in `conversation_summary`
    (the `"Role: text"`-per-line format `format_history_for_prompt()`
    produces — see `ConversationContext.summary`'s own docstring), or
    `None`. Only scans lines starting with `"Assistant:"` — see this
    function group's own module-level comment for why that boundary is
    the entire safety argument here. Returns the LAST match across all
    assistant lines (most recent turn wins), not the first, since a
    follow-up question is almost always about what was JUST discussed.
    """
    if not conversation_summary:
        return None
    last: Optional[str] = None
    for line in conversation_summary.splitlines():
        if not line.startswith("Assistant:"):
            continue
        for m in _CASE_REF_RE.finditer(line):
            last = _normalize_case_ref(m)
    return last


# [Gold-QA fix follow-up] A THIRD, lowest-priority resolution source: a
# bare number pair with no "case"/"FIR" word at all ("summarize 435 26",
# "what about 435/26"). Deliberately not treated as a case reference
# purely by shape — two small numbers are genuinely ambiguous (a date, a
# section pair, anything) — so this is gated on actually resolving to a
# real case's own display number (Case.fir_number, e.g. "435/26" — see
# src/database/models.py's Case model; NOT a fixed transform of case_id,
# which is why this needs a real DB lookup rather than string-building
# like _normalize_case_ref() does for the keyword-anchored sources
# above). A pair that doesn't resolve is silently not a case reference —
# no false-positive risk to a query that just happens to contain two
# numbers. Second number pinned to exactly 2 digits (every real
# fir_number in this corpus is "NNN/YY", a 2-digit year suffix) to keep
# the candidate shape itself already narrow, ahead of the DB check.
_BARE_CASE_NUMBER_RE = re.compile(r"\b(\d{2,4})[\s/](\d{2})\b")


async def _case_id_from_bare_number(
    query_text: str, gateway: Optional[DataGateway],
) -> Optional[str]:
    if gateway is None:
        return None
    m = _BARE_CASE_NUMBER_RE.search(query_text or "")
    if not m:
        return None
    fir_number = f"{m.group(1)}/{m.group(2)}"
    try:
        case = await gateway.get_case_by_fir_number(fir_number)
    except Exception:
        logger.exception(
            "Supervisor: get_case_by_fir_number failed for fir_number=%s — treating as no match.",
            fir_number,
        )
        return None
    return case.get("case_id") if case else None


async def _case_access_authorized(
    case_id: str, caller, gateway: Optional[DataGateway],
) -> bool:
    """
    Real authorization check for a case named directly in the CURRENT
    query text — the one source above that isn't already
    self-authorizing. `CROSS_CASE_ROLES` (supervisor/station-admin/
    platform-admin) are granted access to any named case without a
    per-case assignment row, matching how every OTHER cross-case
    capability in this codebase already treats those roles (e.g.
    `graph_retriever.CROSS_CASE_ROLES`'s own `all_cases` scoping,
    `xagg.py::_filtered_cases()`'s platform-admin-wide `get_cases()`
    call) — this is not a new, looser standard invented here. Every other
    role needs a real `CaseAssignment` row, checked via the same
    `gateway.check_case_access()` primitive `src/api/cases.py`'s own
    `require_case_access()` dependency uses. Fails CLOSED (False) when no
    gateway or no user_id is available to check against — never
    best-effort-grants on missing plumbing.
    """
    if caller.role in CROSS_CASE_ROLES:
        return True
    if gateway is None or not caller.user_id:
        return False
    try:
        role_str = caller.role.value if hasattr(caller.role, "value") else str(caller.role)
        return await gateway.check_case_access(case_id, caller.user_id, role_str)
    except Exception:
        logger.exception(
            "Supervisor: check_case_access failed for case_id=%s user_id=%s — denying.",
            case_id, caller.user_id,
        )
        return False


class Supervisor:
    """
    The single entry point every question goes through. Classifies,
    threads `CallerContext` unchanged, dispatches, returns the sub-agent's
    result untouched. See module docstring for the full contract.
    """

    def __init__(self, registry: Optional[dict[str, SubAgent]] = None) -> None:
        # Defaults to the module-level, process-wide registry so a
        # sub-agent registered anywhere is dispatchable through any
        # `Supervisor()` instance. Tests pass their own dict to stay
        # isolated from module-global state and from each other.
        self._registry = _REGISTRY if registry is None else registry

    async def handle(
        self,
        agent_input: SubAgentInput,
        *,
        on_event: Optional[Callable[[PipelineEvent], None]] = None,
        gateway: Optional[DataGateway] = None,
        allow_meta_analysis: bool = True,
    ) -> SubAgentResult:
        """
        Classify `agent_input.query_text`, dispatch to the matching
        sub-agent, and return exactly what it returns.

        `on_event`, if given, is called synchronously with each
        `PipelineEvent` emitted for this dispatch (one at classification,
        one at outcome — see SUBAGENT_INTERFACES.md §2.2's granularity
        rule: one event per meaningful transition, never collapsed into a
        single "ran" event). This phase has no live SSE channel or
        `run_id` to attach a `log_step()` call to — wiring `on_event`
        through to the existing `event()`/`log_step()` machinery is a
        later phase's job (see AGENT_HARNESS_IMPLEMENTATION_PLAN.md §6).
        The hook exists now so that wiring is additive, not a signature
        change.

        [AMENDMENT — pre-Phase-8 contract amendment, mirrors `on_event`'s
        own §12 amendment] `gateway`, if given, is forwarded unchanged to
        the dispatched sub-agent. Only Report Drafting currently uses it
        (to persist a generated file record); every other sub-agent
        accepts-and-ignores it, same as `on_event` before Phase 7.

        [AMENDMENT — findings.md Module 10] `allow_meta_analysis`, forwarded
        unchanged to `classify_to_subagent()` — see that function's own
        docstring for the full recursion-guard rationale. Defaults to `True`
        for every existing/top-level caller; `meta_analysis.py` is the one
        caller that passes `False`, once per sub-query it dispatches.
        """
        emit = on_event if on_event is not None else (lambda _evt: None)

        route_result = await route_query(agent_input.query_text)
        sub_agent_name = classify_to_subagent(
            route_result, agent_input.query_text, allow_meta_analysis=allow_meta_analysis
        )

        # [Reconciliation fix — harness-reconciliation Unit 4/7/8] Thread the
        # router's target_entity onto the input — see
        # SubAgentInput.target_entity's own docstring (types.py) for the
        # full rationale. This is the one place that holds both
        # `route_result` and the `SubAgentInput`, so doing it here means a
        # sub-agent cannot silently lose it. An explicit value already on
        # the input WINS: a caller that constructed a SubAgentInput with a
        # specific entity meant it, and the router's guess must not
        # override a deliberate choice.
        if agent_input.target_entity is None:
            routed_entity = (route_result or {}).get("target_entity")
            if routed_entity:
                agent_input = agent_input.model_copy(
                    update={"target_entity": str(routed_entity)}
                )

        emit(
            PipelineEvent(
                step="supervisor:dispatch",
                status="active",
                detail=(
                    f"Classified query as route={route_result.get('route')!r} "
                    f"-> sub-agent={sub_agent_name!r}"
                ),
            )
        )

        # [Reconciliation fix — Unit 2] DIRECT classifies to NO_SUB_AGENT,
        # never to a real sub-agent name. This dispatch path is not
        # currently live-traffic-reachable (see NO_SUB_AGENT's own comment),
        # but must not silently misbehave if it ever is: the honest answer
        # this layer can give is "no sub-agent handles this route", not a
        # generated response pretending to be one. Full delegation back to
        # the legacy DIRECT path (preserving its streaming behavior) belongs
        # at the caller (main.py/cutover.py), which already has the
        # machinery to answer DIRECT queries — see cutover.py.
        if sub_agent_name == NO_SUB_AGENT:
            emit(
                PipelineEvent(
                    step="supervisor:dispatch",
                    status="skipped",
                    detail="DIRECT route — no sub-agent; caller should use the legacy DIRECT path",
                )
            )
            return SubAgentResult(
                status=SubAgentStatus.ABSTAINED,
                answer_text=None,
                # [Merge reconciliation — Unit 12 follow-up] `error.kind ==
                # "invalid_input"` is the caller-checkable signal for "this
                # was DIRECT, not a real abstention" — cutover.py reads it
                # to decide whether to emit `delegate_to_legacy` rather than
                # a generic error, without re-classifying the query itself.
                error=ToolError(
                    kind="invalid_input",
                    message="Route classified as DIRECT; no sub-agent handles it.",
                ),
                caveats=[
                    "This query classified as DIRECT (general knowledge, no "
                    "retrieval), which the harness does not serve itself."
                ],
            )

        # [Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 3] Case Summarization
        # (GRAPH/GRAPH_HYBRID) is structurally within-case: it summarizes ONE
        # active case's own documents/graph. router.py always forces
        # case_scope="within_case" for these two routes (only XGRAPH/XAGG/
        # XNETWORK are ever cross_case — see route_query()'s own comment), so
        # a genuinely cross-case, multi-record question (compare two cases,
        # trace an item across cases, analyze the whole caseload) that the
        # LLM classifier happens to still route to GRAPH/GRAPH_HYBRID lands
        # here with NOTHING to summarize whenever no case is actively
        # selected — live-confirmed (Gold-QA report §2.1, CR3/CR4/G2/G3/G5/G6):
        # this dispatched straight into Case Summarization, which returned
        # its generic EMPTY "No case documents or case graph data were found
        # to summarize" caveat — a message that reads as "no data exists"
        # when the real problem is "wrong sub-agent for a query with no case
        # context." Caught here, before dispatch, rather than inside Case
        # Summarization itself: this is a routing-context defect (no case_id
        # to scope to), not something the sub-agent's own retrieval logic can
        # detect or fix.
        inferred_case_id: Optional[str] = None
        inferred_case_source: Optional[str] = None  # "query" or "history", for the caveat wording below
        if sub_agent_name == CASE_SUMMARIZATION and not agent_input.execution.caller.active_case_id:
            caller = agent_input.execution.caller
            resolved_case_id: Optional[str] = None
            resolution_source: Optional[str] = None

            # Source 1 — a case named directly in THIS query ("summarize
            # case 435/26"), or (1b) a bare number pair that resolves to a
            # real case's display number ("summarize 435/26" / "435 26",
            # no "case"/"FIR" word — see _case_id_from_bare_number()'s own
            # docstring for why that's safe without a keyword anchor).
            # Either way, requires a real authorization check before use —
            # see _case_access_authorized()'s own docstring. Denied
            # outright (not silently ignored) on failure: once a specific
            # case has been identified as what the user meant, silently
            # falling through to a different answer would be more
            # confusing than an honest denial, not less.
            named_case_id = _case_id_from_query_text(agent_input.query_text)
            if named_case_id is None:
                named_case_id = await _case_id_from_bare_number(agent_input.query_text, gateway)
            if named_case_id is not None:
                if await _case_access_authorized(named_case_id, caller, gateway):
                    resolved_case_id, resolution_source = named_case_id, "query"
                else:
                    emit(
                        PipelineEvent(
                            step="supervisor:dispatch",
                            status="skipped",
                            detail=(
                                f"Query named case {named_case_id!r} but caller is not "
                                f"authorized for it — denying rather than falling through."
                            ),
                        )
                    )
                    return SubAgentResult(
                        status=SubAgentStatus.ABSTAINED,
                        answer_text=None,
                        error=ToolError(
                            kind="permission_denied",
                            message=f"Not authorized for case '{named_case_id}'.",
                        ),
                        caveats=[
                            f"You don't have access to case {named_case_id} — this couldn't "
                            "be answered from it."
                        ],
                    )

            # Source 2 — a case the ASSISTANT itself already surfaced
            # earlier in this session. See
            # _infer_case_id_from_assistant_history()'s own docstring for
            # why no fresh authorization check is needed for this source.
            if resolved_case_id is None:
                resolved_case_id = _infer_case_id_from_assistant_history(
                    agent_input.conversation_context.summary if agent_input.conversation_context else None
                )
                resolution_source = "history" if resolved_case_id is not None else None

            if resolved_case_id is not None:
                inferred_case_id = resolved_case_id
                inferred_case_source = resolution_source
                emit(
                    PipelineEvent(
                        step="supervisor:dispatch",
                        status="active",
                        detail=(
                            f"GRAPH/GRAPH_HYBRID classified with no active case_id — "
                            f"resolved case {inferred_case_id!r} from the {resolution_source} "
                            f"instead of dispatching scope-less."
                        ),
                    )
                )
                scoped_caller = caller.model_copy(update={"active_case_id": inferred_case_id})
                scoped_execution = agent_input.execution.model_copy(update={"caller": scoped_caller})
                # sub_agent_name (CASE_SUMMARIZATION) and every other
                # classification output are already correct for a
                # within-case dispatch — only the missing case scope
                # needed filling in. Reassigning agent_input here means
                # the normal dispatch path below (`handler(agent_input,
                # ...)`) picks it up unchanged from this point on — no
                # separate dispatch call is duplicated. The caveat added
                # after that call returns tells the caller which case was
                # implied, so this never looks like a silent, unexplained
                # scope change.
                agent_input = agent_input.model_copy(update={"execution": scoped_execution})
            else:
                emit(
                    PipelineEvent(
                        step="supervisor:dispatch",
                        status="skipped",
                        detail=(
                            "GRAPH/GRAPH_HYBRID classified with no active case_id and no "
                            "case named in this query or referenced earlier in this "
                            "session — Case Summarization has nothing to scope to; "
                            "returning guidance instead of dispatching."
                        ),
                    )
                )
                return SubAgentResult(
                    status=SubAgentStatus.EMPTY,
                    answer_text=None,
                    caveats=[
                        "This question needs either a specific case selected to "
                        "summarize, or rephrasing as a cross-case question (e.g. "
                        "\"...across cases\", \"...in total\", \"which cases...\") "
                        "so it can be answered from the whole caseload instead."
                    ],
                )

        handler = self._registry.get(sub_agent_name)
        if handler is None:
            emit(
                PipelineEvent(
                    step="supervisor:dispatch",
                    status="skipped",
                    detail=f"Sub-agent {sub_agent_name!r} has no registered handler yet",
                )
            )
            return _not_yet_available_result(sub_agent_name)

        # [PRESERVE — SUBAGENT_INTERFACES.md §0, design §4.4, extended by
        # plan §10.1] `agent_input` (and therefore `agent_input.execution`
        # and its nested `.execution.caller`) is passed straight through,
        # unmodified — no reconstruction, no merge with any
        # profile/preferences object, no defaulting of `.execution.caller.role`.
        #
        # [AMENDMENT — pre-Phase-7 contract amendment, mirrors §10/§11's
        # pattern] `on_event` (this method's own parameter, possibly None)
        # is now forwarded to the sub-agent unchanged — closing the gap
        # SUBAGENT_INTERFACES.md §2.1.4/RESOLVED-4a needs: a sub-agent that
        # composes several tools (Investigative Analysis, Phase 7 onward)
        # can emit its own per-source-tool `PipelineEvent`s through this
        # exact sink, reusing the callback this method has accepted since
        # Phase 1 rather than adding any new delivery mechanism. Every
        # sub-agent's `__call__` now accepts `on_event` (see
        # `types.SubAgent`'s own amendment note) — sub-agents with nothing
        # granular to report simply ignore it.
        result = await handler(agent_input, on_event=on_event, gateway=gateway)

        emit(
            PipelineEvent(
                step="supervisor:dispatch",
                status="done",
                detail=f"{sub_agent_name} completed with status={result.status.value}",
            )
        )

        # [AMENDMENT — Gold-QA fix, Module 3 follow-up] The one deliberate
        # exception to "[PRESERVE] returned exactly as received" below:
        # when this dispatch was scoped to a case resolved from the query
        # text or history (see the guard above), the caller must be told
        # which case was implied and how — an answer that looks like it
        # came from "All Cases" but is actually scoped to one case, with
        # no caveat, would be far more misleading than the original
        # "nothing to summarize" gap this whole mechanism exists to fix.
        # Only fires on this one path; every other dispatch keeps the
        # original untouched-passthrough guarantee.
        if inferred_case_id is not None:
            note = (
                f"No case was selected, so this answers about {inferred_case_id}, "
                "as named in your question."
                if inferred_case_source == "query" else
                f"No case was selected, so this answer reuses {inferred_case_id}, "
                "which was discussed earlier in this conversation."
            )
            result = result.model_copy(
                update={"caveats": [note] + list(result.caveats or [])}
            )

        # [PRESERVE] Otherwise returned exactly as received — not touched,
        # reformatted, or unwrapped.
        return result
