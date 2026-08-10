"""
Supervisor routing classifier — maps a router decision onto a sub-agent.

NOT a parallel classifier. `route_query()` already does the classification work,
across three tiers that exist because each one was earned:

  tier 0  greeting/short-message fast-path (orchestrator.py) -> DIRECT, no LLM
  tier 1  `_deterministic_route_override()` -> SQL/XNETWORK/XAGG/XGRAPH, no LLM
  tier 2  the LLM call, for everything else

Those tier-1 patterns are a live-testing log, not speculation: the router
prompt's OWN few-shot examples misrouted repeatedly (a PPC-section lookup
falling to RAG across independent runs; XNETWORK's "overall picture" phrasing
being swallowed by XGRAPH's `across...cases` pattern). Re-deriving that
calibration would re-earn those bugs, so this module consumes `route_query()`'s
output rather than replacing it.

What is genuinely new here is small:
  * a route -> sub-agent mapping table,
  * one additional deterministic tier for timeline-shaped phrasing, which has
    no route of its own (Timeline Building and Case Summarization both consume
    GRAPH, and only phrasing separates them),
  * the `output_format` -> Report Drafting rule.

Nothing here is wired into `supervisor._route()` yet.
"""
from __future__ import annotations

import re
from typing import Optional

from src.pipeline.harness.agents import (
    aggregate_analysis,
    case_summary,
    cross_case_linkage,
    investigative_analysis,
    report_draft,
    semantic_search,
    timeline,
)

# Returned when no sub-agent should handle the query at all — DIRECT answers
# from general knowledge with no retrieval, and the Verifier deliberately does
# not gate it (design §1). Every sub-agent runs a grounding gate, so routing
# DIRECT to any of them would abstain on a query that should simply be
# answered. The caller handles this case; it is not a failure.
NO_SUB_AGENT = "__direct__"


# ── The new tier: timeline-shaped phrasing ───────────────────────────────
#
# Deliberately NARROW, for the same reason router.py excluded
# "network ... across cases" from its XNETWORK triggers: a pattern that
# overlaps a neighbouring route's vocabulary causes misroutes that are harder
# to diagnose than the gap it closed.
#
# Specifically NOT matched, and why:
#   * bare `events?`   — "what events led to the arrest" is an investigative
#                        question (GRAPH_HYBRID), not a chronology request.
#   * bare `when`      — "when was the FIR filed" is a single-fact lookup.
#   * bare `history`   — "criminal history" means prior record (cross-case
#                        recurrence), not a timeline.
#   * bare "what happened" — that is a summarization request; it only counts
#                        here when an ordering word co-occurs within a short
#                        span.
_TIMELINE_OVERRIDE_PATTERNS = [
    re.compile(r"\btime\s?line\b", re.IGNORECASE),
    re.compile(r"\bchronolog(y|ical)\b", re.IGNORECASE),
    re.compile(r"\bsequence of events\b", re.IGNORECASE),
    re.compile(r"\border of events\b", re.IGNORECASE),
    re.compile(r"\bwhat happened\b.{0,20}\b(when|first|next|order)\b", re.IGNORECASE),
    re.compile(r"ٹائم\s*لائن"),                                   # Urdu: timeline
    re.compile(r"واقعات\s*کی\s*ترتیب"),                            # Urdu: sequence of events
    re.compile(r"\bwaqiat\b.{0,15}\btarteeb\b", re.IGNORECASE),    # Roman-Urdu
]

# Timeline Building is deliberately case-scoped (`_TIMELINE_MAX_HOPS = 1`,
# `cross_case=False`), so the timeline tier may only override a WITHIN-CASE
# route. Letting "timeline of this suspect's cases" win over XGRAPH would
# silently narrow a cross-case request to a single case — a real correctness
# bug, not a routing preference.
_TIMELINE_ELIGIBLE_ROUTES = frozenset({"GRAPH", "GRAPH_HYBRID", "RAG"})

# Routes whose evidence spans cases by definition. Used for the `case_scope`
# guard below.
_CROSS_CASE_ROUTES = frozenset({"XGRAPH", "XNETWORK", "XAGG"})

# ── Route -> sub-agent ───────────────────────────────────────────────────
#
# Nine routes, seven sub-agents, so this is not 1:1. Each mapping follows the
# §2.1 contract's "Composes" column:
#
#   XGRAPH/XNETWORK -> Cross-Case Linkage      (composes exactly those two)
#   XAGG            -> Large-Scale Aggregate   (composes exactly XAGG)
#   GRAPH_HYBRID    -> Investigative Analysis  (RAG+GRAPH+SQL; the router's own
#                                               prompt calls GRAPH_HYBRID a
#                                               "broad investigative question")
#   GRAPH           -> Case Summarization      (RAG+GRAPH, case-scoped)
#   RAG             -> Semantic Search         (RAG only)
#   SQL/WEB         -> Semantic Search         (see the caveat below)
#   DIRECT          -> NO_SUB_AGENT
#
# SQL and WEB are a DOCUMENTED LIMITATION, not a clean fit (logged in
# AGENT_HARNESS_DESIGN.md §7). No sub-agent composes either tool alone —
# Investigative Analysis uses SQL only alongside RAG+GRAPH. Semantic Search is
# the closest available home because the RAG tool is already the declared
# fallback target for both (§2.6, §2.7), so a SQL- or WEB-routed query degrades
# here exactly as it would on the legacy path. It is still a coverage gap.
_ROUTE_TO_SUB_AGENT: dict[str, str] = {
    "DIRECT": NO_SUB_AGENT,
    "RAG": semantic_search.NAME,
    "SQL": semantic_search.NAME,
    "WEB": semantic_search.NAME,
    "GRAPH": case_summary.NAME,
    "GRAPH_HYBRID": investigative_analysis.NAME,
    "XGRAPH": cross_case_linkage.NAME,
    "XNETWORK": cross_case_linkage.NAME,
    "XAGG": aggregate_analysis.NAME,
}

# Sub-agents whose tools carry cross-case role gates. Guarded by `case_scope`
# below.
_CROSS_CASE_SUB_AGENTS = frozenset(
    {cross_case_linkage.NAME, aggregate_analysis.NAME}
)

_FILE_FORMATS = frozenset({"file_pdf", "file_xlsx", "file_docx"})


def wants_timeline(query_text: str) -> bool:
    """True when the query asks for a chronology rather than a summary."""
    return any(p.search(query_text or "") for p in _TIMELINE_OVERRIDE_PATTERNS)


def classify(route_result: dict, query_text: str) -> str:
    """
    Map a `route_query()` result onto a sub-agent name.

    Takes the router's decision rather than re-deriving it — `route_result` is
    exactly what `route_query()` returns, so the caller makes one classification
    call and this adds no latency or cost of its own.

    Returns a sub-agent NAME, or `NO_SUB_AGENT` for DIRECT.

    Precedence, highest first:
      0. DIRECT -> NO_SUB_AGENT, ahead of everything (including a file format)
      1. a file `output_format` -> Report Drafting, whatever the route
      2. timeline phrasing on a within-case route -> Timeline Building
      3. the route mapping
      4. the `case_scope` guard, which can demote a cross-case mapping
    """
    route = str(route_result.get("route") or "RAG").upper()
    output_format = str(route_result.get("output_format") or "chat").lower()
    case_scope = str(route_result.get("case_scope") or "within_case").lower()

    # ── 0. DIRECT wins outright, even over a file request ──
    # DIRECT can legitimately carry a file output_format — the router's own
    # few-shots include {"route": "DIRECT", "output_format": "file_docx"} for
    # "write me a document about something unrelated to police facts". Letting
    # the format check below claim that would send it to Report Drafting, which
    # consumes Case Summarization and therefore performs case-scoped retrieval
    # — retrieval this query was explicitly routed AWAY from, and which would
    # most likely abstain.
    #
    # The legacy path already handles this correctly: its DIRECT branch
    # generates the answer and then falls through to _generate_file(). So
    # returning NO_SUB_AGENT here preserves existing behaviour exactly rather
    # than reimplementing it.
    if route == "DIRECT":
        return NO_SUB_AGENT

    # ── 1. Output format decides, ahead of content ──
    # Report Drafting is orthogonal to route: it consumes Case Summarization's
    # output and produces a document. Reusing the router's existing
    # `output_format` (already specified in prompts/router.txt with worked
    # examples) means no new inference and no clarification step — the router
    # prompt explicitly forbids clarifying questions, so asking the user which
    # file type would contradict it.
    #
    # This also resolves what would otherwise be a landmine: Report Drafting
    # REJECTS `output_format == "chat"`. Keying on the format rather than the
    # route makes that rejection an invariant this function maintains, instead
    # of a failure mode a content-based route could stumble into.
    if output_format in _FILE_FORMATS:
        return report_draft.NAME

    # ── 2. Timeline phrasing, within-case routes only ──
    if route in _TIMELINE_ELIGIBLE_ROUTES and wants_timeline(query_text):
        return timeline.NAME

    # ── 3. The route mapping ──
    sub_agent = _ROUTE_TO_SUB_AGENT.get(route, semantic_search.NAME)

    # ── 4. case_scope guard ──
    # Redundant today: the router forces every route except XGRAPH/XAGG/XNETWORK
    # back to `within_case` unconditionally, so a cross-case sub-agent paired
    # with a within-case scope should be unreachable. Kept anyway, matching the
    # redundant-check discipline used elsewhere in this harness (the per-tool
    # role gates, the type-pinned `fallback_to_rag`): cheap now, and it means a
    # future router.py change cannot silently route a within-case query into a
    # cross-case sub-agent — where the tools' own role gates would then deny it,
    # producing a confusing DENIED on a query that never asked to cross cases.
    if sub_agent in _CROSS_CASE_SUB_AGENTS and case_scope != "cross_case":
        return semantic_search.NAME

    # Inverse of the same guard: a cross-case scope on a route that does not
    # map to a cross-case sub-agent. The router's own guard should prevent it;
    # if it ever occurs, the safe reading is that scope is wrong rather than
    # that a case-scoped sub-agent should quietly widen.
    if route not in _CROSS_CASE_ROUTES and case_scope == "cross_case":
        return sub_agent

    return sub_agent


def describe(route_result: dict, query_text: str) -> dict:
    """
    `classify()` plus the reasoning, for trace/debugging.

    The supervisor emits its dispatch decision as a `PipelineEvent`; this gives
    that event something more useful than a bare name, without making
    `classify()` itself return a structure every caller would have to unpack.
    """
    route = str(route_result.get("route") or "RAG").upper()
    output_format = str(route_result.get("output_format") or "chat").lower()
    sub_agent = classify(route_result, query_text)

    if output_format in _FILE_FORMATS:
        basis = f"output_format={output_format} — document requested"
    elif sub_agent == timeline.NAME:
        basis = f"timeline phrasing on a within-case route ({route})"
    elif sub_agent == NO_SUB_AGENT:
        basis = "DIRECT — answered without retrieval, no sub-agent"
    elif _ROUTE_TO_SUB_AGENT.get(route) != sub_agent:
        basis = f"route={route} demoted by case_scope guard"
    else:
        basis = f"route={route}"

    return {"sub_agent": sub_agent, "route": route, "basis": basis}
