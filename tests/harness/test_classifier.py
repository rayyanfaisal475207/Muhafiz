"""
Supervisor routing classifier — mapping and pattern tests.

The classifier is a mapping layer over `route_query()`, not a parallel
classifier, so most of what needs testing is the TABLE and the one genuinely
new deterministic tier.

The timeline patterns get the most scrutiny. Unlike the XGRAPH/XNETWORK
patterns they sit beside — which carry real debugging history — these are new,
so their NEAR-MISSES matter as much as their matches. router.py's own comments
record what happens when a pattern overlaps a neighbouring route's vocabulary:
XNETWORK's "overall picture" phrasing was swallowed by XGRAPH's `across...cases`
pattern until the collision was found live.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness import classifier
from src.pipeline.harness.agents import (
    aggregate_analysis, case_summary, cross_case_linkage,
    investigative_analysis, report_draft, semantic_search, timeline,
)


def _rr(route: str, output_format: str = "chat", case_scope: str = None) -> dict:
    """A `route_query()`-shaped result."""
    if case_scope is None:
        case_scope = "cross_case" if route in ("XGRAPH", "XNETWORK", "XAGG") else "within_case"
    return {"route": route, "output_format": output_format, "case_scope": case_scope}


# ── The route -> sub-agent table ─────────────────────────────────────────

@pytest.mark.parametrize("route,expected", [
    ("RAG", semantic_search.NAME),
    ("SQL", semantic_search.NAME),
    ("WEB", semantic_search.NAME),
    ("GRAPH", case_summary.NAME),
    ("GRAPH_HYBRID", investigative_analysis.NAME),
    ("XGRAPH", cross_case_linkage.NAME),
    ("XNETWORK", cross_case_linkage.NAME),
    ("XAGG", aggregate_analysis.NAME),
    ("DIRECT", classifier.NO_SUB_AGENT),
])
def test_route_maps_to_expected_sub_agent(route, expected):
    assert classifier.classify(_rr(route), "a query") == expected


def test_every_valid_route_is_mapped():
    """
    A route with no mapping would silently fall through to the default. Pins
    the table against router.py's own list so a new route cannot be added
    there without a decision here.
    """
    from src.pipeline.router import _VALID_ROUTES

    assert set(_VALID_ROUTES) == set(classifier._ROUTE_TO_SUB_AGENT)


def test_direct_returns_no_sub_agent_rather_than_a_default():
    """
    DIRECT answers from general knowledge with no retrieval, and every
    sub-agent runs a grounding gate — routing it to one would abstain on a
    query that should simply be answered.
    """
    assert classifier.classify(_rr("DIRECT"), "hello") == classifier.NO_SUB_AGENT


def test_unknown_route_falls_back_to_semantic_search():
    """Defensive: a malformed router result must not raise."""
    assert classifier.classify({"route": "NONSENSE"}, "q") == semantic_search.NAME
    assert classifier.classify({}, "q") == semantic_search.NAME


# ── output_format takes precedence over route ────────────────────────────

@pytest.mark.parametrize("fmt", ["file_pdf", "file_xlsx", "file_docx"])
@pytest.mark.parametrize("route", ["RAG", "GRAPH", "GRAPH_HYBRID", "SQL", "XAGG"])
def test_file_output_format_routes_to_report_drafting(fmt, route):
    """
    Report Drafting is orthogonal to route: the format decides. This is also
    what keeps its rejection of `chat` an invariant rather than a landmine.
    """
    assert classifier.classify(_rr(route, output_format=fmt), "write it up") == report_draft.NAME


def test_chat_output_format_routes_by_content():
    assert classifier.classify(_rr("GRAPH", output_format="chat"), "q") == case_summary.NAME


def test_report_drafting_is_never_reached_with_chat_format():
    """
    The invariant Report Drafting itself asserts. If the classifier could route
    a chat query there, every such query would ABSTAIN with invalid_input.
    """
    for route in ("RAG", "SQL", "WEB", "GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG", "XNETWORK"):
        assert classifier.classify(_rr(route, output_format="chat"), "q") != report_draft.NAME


# ── The new timeline tier: intended matches ──────────────────────────────

@pytest.mark.parametrize("query", [
    "give me a timeline of this case",
    "show the time line of events",
    "what is the chronology here",
    "list events in chronological order",
    "what was the sequence of events",
    "tell me the order of events that night",
    "what happened and in what order",
    "what happened first in this case",
    "اس کیس کی ٹائم لائن دکھائیں",
    "واقعات کی ترتیب کیا تھی",
    "is case ke waqiat ki tarteeb batayen",
])
def test_timeline_phrasing_routes_to_timeline_building(query):
    assert classifier.classify(_rr("GRAPH"), query) == timeline.NAME


# ── The new timeline tier: NEAR MISSES that must NOT trigger ─────────────

@pytest.mark.parametrize("query,expected_agent,why", [
    ("what events led to the arrest", case_summary.NAME,
     "bare 'events' is an investigative question, not a chronology request"),
    ("when was the FIR filed", case_summary.NAME,
     "bare 'when' is a single-fact lookup"),
    ("criminal history of the suspect", case_summary.NAME,
     "'history' means prior record, not a timeline"),
    ("what happened in this case", case_summary.NAME,
     "bare 'what happened' is a summarization request — needs an ordering word"),
    ("map the network of associates", case_summary.NAME,
     "network mapping is not chronology"),
    ("summarize the case", case_summary.NAME,
     "plain summarization"),
    ("who was present at the scene", case_summary.NAME,
     "an entity question"),
])
def test_near_miss_phrasing_does_not_trigger_timeline(query, expected_agent, why):
    """
    The patterns must not swallow neighbouring intents. Each case here is a
    genuine query for a DIFFERENT sub-agent that a looser pattern would have
    captured.
    """
    assert classifier.classify(_rr("GRAPH"), query) == expected_agent, why
    assert not classifier.wants_timeline(query), why


# ── Timeline tier is restricted to within-case routes ────────────────────

@pytest.mark.parametrize("route", ["XGRAPH", "XNETWORK", "XAGG"])
def test_timeline_phrasing_never_overrides_a_cross_case_route(route):
    """
    Timeline Building is deliberately case-scoped (_TIMELINE_MAX_HOPS=1,
    cross_case=False). Letting "timeline of this suspect's cases" win over
    XGRAPH would silently narrow a cross-case request to a single case — a
    correctness bug, not a routing preference.
    """
    result = classifier.classify(_rr(route), "timeline of this suspect across cases")

    assert result != timeline.NAME
    assert result in (cross_case_linkage.NAME, aggregate_analysis.NAME)


@pytest.mark.parametrize("route", ["GRAPH", "GRAPH_HYBRID", "RAG"])
def test_timeline_phrasing_applies_to_within_case_routes(route):
    assert classifier.classify(_rr(route), "timeline of this case") == timeline.NAME


def test_file_format_beats_timeline_phrasing():
    """Precedence: a requested document is a document, chronology or not."""
    result = classifier.classify(
        _rr("GRAPH", output_format="file_pdf"), "timeline of this case"
    )
    assert result == report_draft.NAME


# ── case_scope guard ─────────────────────────────────────────────────────

@pytest.mark.parametrize("route", ["XGRAPH", "XNETWORK", "XAGG"])
def test_within_case_scope_blocks_cross_case_sub_agents(route):
    """
    Redundant today — the router forces non-cross-case routes back to
    within_case unconditionally. Kept so a future router.py change cannot
    silently route a within-case query into a cross-case sub-agent, where the
    tools' own role gates would deny it and produce a confusing DENIED on a
    query that never asked to cross cases.
    """
    result = classifier.classify(_rr(route, case_scope="within_case"), "q")

    assert result not in (cross_case_linkage.NAME, aggregate_analysis.NAME)
    assert result == semantic_search.NAME


@pytest.mark.parametrize("route,expected", [
    ("XGRAPH", cross_case_linkage.NAME),
    ("XNETWORK", cross_case_linkage.NAME),
    ("XAGG", aggregate_analysis.NAME),
])
def test_cross_case_scope_allows_cross_case_sub_agents(route, expected):
    assert classifier.classify(_rr(route, case_scope="cross_case"), "q") == expected


def test_missing_case_scope_defaults_to_within_case():
    """An absent scope must not be read as permission to cross cases."""
    result = classifier.classify({"route": "XGRAPH", "output_format": "chat"}, "q")

    assert result == semantic_search.NAME


# ── describe(), for the dispatch trace ───────────────────────────────────

def test_describe_reports_the_basis_for_each_decision():
    file_case = classifier.describe(_rr("RAG", output_format="file_pdf"), "write it up")
    assert file_case["sub_agent"] == report_draft.NAME
    assert "output_format" in file_case["basis"]

    tl = classifier.describe(_rr("GRAPH"), "timeline of this case")
    assert tl["sub_agent"] == timeline.NAME
    assert "timeline" in tl["basis"].lower()

    direct = classifier.describe(_rr("DIRECT"), "hello")
    assert direct["sub_agent"] == classifier.NO_SUB_AGENT
    assert "DIRECT" in direct["basis"]

    guarded = classifier.describe(_rr("XGRAPH", case_scope="within_case"), "q")
    assert "case_scope" in guarded["basis"]


def test_describe_agrees_with_classify():
    """The trace must never disagree with the dispatch it describes."""
    for route in ("DIRECT", "RAG", "GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG", "XNETWORK"):
        rr = _rr(route)
        assert classifier.describe(rr, "q")["sub_agent"] == classifier.classify(rr, "q")


# ── Every sub-agent is reachable ─────────────────────────────────────────

def test_all_seven_sub_agents_are_reachable():
    """
    A sub-agent no route can reach is dead code. Pins that the mapping plus
    the two override tiers cover all seven.
    """
    reachable = {
        classifier.classify(_rr("RAG"), "q"),
        classifier.classify(_rr("GRAPH"), "q"),
        classifier.classify(_rr("GRAPH_HYBRID"), "q"),
        classifier.classify(_rr("XGRAPH"), "q"),
        classifier.classify(_rr("XAGG"), "q"),
        classifier.classify(_rr("GRAPH"), "timeline of this case"),
        classifier.classify(_rr("RAG", output_format="file_pdf"), "write it up"),
    }

    expected = {
        semantic_search.NAME, case_summary.NAME, investigative_analysis.NAME,
        cross_case_linkage.NAME, aggregate_analysis.NAME, timeline.NAME,
        report_draft.NAME,
    }
    assert reachable == expected
