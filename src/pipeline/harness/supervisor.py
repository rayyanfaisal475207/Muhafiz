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
    Large-Scale Aggregate; DIRECT/WEB -> Semantic Search as the closest
    available fallback).
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

SUB_AGENT_NAMES: tuple[str, ...] = (
    SEMANTIC_SEARCH,
    LARGE_SCALE_AGGREGATE,
    CASE_SUMMARIZATION,
    TIMELINE_BUILDING,
    CROSS_CASE_LINKAGE,
    INVESTIGATIVE_ANALYSIS,
    REPORT_DRAFTING,
    DATA_QUALITY,
)

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
    "DIRECT": SEMANTIC_SEARCH,
    "WEB": SEMANTIC_SEARCH,
}

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


def classify_to_subagent(route_result: dict, query_text: str = "") -> str:
    """
    Translate `router.py::route_query()`'s output dict into one of the 8
    sub-agent names. Does not call `route_query()` itself and does not
    re-derive its classification — see module docstring.

    `query_text` is optional (defaults to `""`, under which neither
    provisional override below can ever match) so every existing direct
    caller — this module's own tests included — keeps working unchanged;
    `Supervisor.handle()` is the one real caller that has a query to pass,
    and does so below.
    """
    output_format = str(route_result.get("output_format") or "chat").lower()
    if output_format in _FILE_OUTPUT_FORMATS:
        return REPORT_DRAFTING

    route = str(route_result.get("route") or "RAG").upper()

    if route not in _CROSS_CASE_ROUTES:
        if any(pat.search(query_text) for pat in _TIMELINE_TRIGGER_PATTERNS):
            return TIMELINE_BUILDING
        if any(pat.search(query_text) for pat in _INVESTIGATIVE_ANALYSIS_TRIGGER_PATTERNS):
            return INVESTIGATIVE_ANALYSIS

    return _ROUTE_TO_SUBAGENT.get(route, SEMANTIC_SEARCH)


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
        """
        emit = on_event if on_event is not None else (lambda _evt: None)

        route_result = await route_query(agent_input.query_text)
        sub_agent_name = classify_to_subagent(route_result, agent_input.query_text)

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

        # [PRESERVE] Returned exactly as received — not touched,
        # reformatted, or unwrapped.
        return result
