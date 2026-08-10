"""
Cross-Case Linkage sub-agent — contract tests.

Two properties matter most:
  * [RESOLVED-6] DENIED propagates as its own status, never collapsing into
    ABSTAINED or EMPTY — "blocked by permissions" and "found nothing" are
    different facts, and a monitoring surface that cannot tell them apart is
    blind to a security signal.
  * [PRESERVE §3] Unconfirmed identity links are surfaced as caveats and ranked
    below confirmed findings, never presented as established fact.

Production boundaries are mocked; no database, model server, or network.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness import supervisor
from src.pipeline.harness.agents import cross_case_linkage
from src.pipeline.harness.contracts import (
    CallerContext,
    Citation,
    CrossCaseLink,
    EvidenceChunk,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)
from src.pipeline.harness.events import EventRecorder
from src.pipeline.harness.tools import registry
from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER

# `invoke()` requires a route_result. These tests override `_route` directly,
# so the router decision only needs to be well-formed, not meaningful.
_ROUTE_RESULT = {"route": "RAG", "output_format": "chat", "case_scope": "within_case"}

# Queries chosen to hit router.py's real pattern sets.
XGRAPH_QUERY = "has this vehicle appeared in other cases"
XNETWORK_QUERY = "what is the overall picture across these cases"
AMBIGUOUS_QUERY = "tell me about linkages"


@pytest.fixture(autouse=True)
def _real_tools():
    registry.use_real()
    yield
    registry.use_real()


def _supervisor_caller() -> CallerContext:
    return CallerContext(user_id="u2", role=Role.SUPERVISOR, active_case_id="CASE-A")


def _investigator_caller() -> CallerContext:
    return CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id="CASE-A")


def _input(query: str = XGRAPH_QUERY, caller: CallerContext = None) -> SubAgentInput:
    return SubAgentInput(query_text=query, caller=caller or _supervisor_caller())


@pytest.fixture
def tools(monkeypatch):
    """Drive the real XGRAPH/XNETWORK tools to chosen outcomes."""
    def _configure(xgraph="ok", xnetwork="ok", unconfirmed=None):
        async def _retrieve_graph(*a, **k):
            if xgraph == "empty":
                return {"chunks": [], "hop_count": 0, "compounded_confidence": 1.0,
                        "seed_entities": [], "unconfirmed_links": list(unconfirmed or [])}
            return {
                "chunks": [{
                    "id": "x1", "text": "Vehicle VEH-0091 appears in three cases.",
                    "metadata": {"case_id": "CASE-B"}, "graph_confidence": 0.8,
                }],
                "hop_count": 2, "compounded_confidence": 0.8,
                "seed_entities": [{"entity_id": "VEH-0091"}],
                "unconfirmed_links": list(unconfirmed or []),
            }

        async def _communities(query, top_k=5):
            if xnetwork == "empty":
                return []
            return [{
                "community_id": "C-014",
                "summary_text": "A cluster of burglaries shares a consistent entry method.",
                "case_ids": ["CASE-A", "CASE-B"], "member_count": 3, "distance": 0.2,
            }]

        monkeypatch.setattr("src.retrieval.graph_retriever.retrieve_graph", _retrieve_graph)
        # xnetwork.py binds this name at module import, so patching it on the
        # defining module would not affect the already-bound reference.
        monkeypatch.setattr(
            "src.pipeline.xnetwork.query_similar_communities", _communities)

    return _configure


# ── Dispatch (§3.1, reusing router.py's patterns) ────────────────────────

async def test_entity_query_dispatches_to_xgraph_only(tools, gateway):
    tools()

    result = await cross_case_linkage.run(_input(XGRAPH_QUERY), gateway=gateway)

    assert result.tools_used == ["XGRAPH"]
    assert {link.source_tool for link in result.cross_case_links} == {"XGRAPH"}


async def test_thematic_query_dispatches_to_xnetwork_only(tools, gateway):
    tools()

    result = await cross_case_linkage.run(_input(XNETWORK_QUERY), gateway=gateway)

    assert result.tools_used == ["XNETWORK"]
    assert {link.source_tool for link in result.cross_case_links} == {"XNETWORK"}


async def test_ambiguous_query_runs_both(tools, gateway):
    """Independent sources with no shared store — presenting both beats guessing."""
    tools()

    result = await cross_case_linkage.run(_input(AMBIGUOUS_QUERY), gateway=gateway)

    assert sorted(result.tools_used) == ["XGRAPH", "XNETWORK"]


def test_dispatch_reuses_router_patterns_rather_than_new_logic():
    """
    Re-deriving the split would reproduce the misclassifications router.py's
    patterns were tuned to fix.
    """
    import inspect

    source = inspect.getsource(cross_case_linkage._dispatch)
    assert "_XGRAPH_OVERRIDE_PATTERNS" in source
    assert "_XNETWORK_OVERRIDE_PATTERNS" in source


# ── [RESOLVED-6] DENIED is its own status ────────────────────────────────

async def test_both_denied_is_denied_not_abstained(tools, gateway):
    tools()

    result = await cross_case_linkage.run(
        _input(AMBIGUOUS_QUERY, caller=_investigator_caller()), gateway=gateway
    )

    assert result.status is SubAgentStatus.DENIED
    assert result.status is not SubAgentStatus.ABSTAINED
    assert result.status is not SubAgentStatus.EMPTY


async def test_denied_returns_no_evidence(tools, gateway):
    tools()

    result = await cross_case_linkage.run(
        _input(AMBIGUOUS_QUERY, caller=_investigator_caller()), gateway=gateway
    )

    assert result.answer_text is None
    assert result.cross_case_links == []
    assert result.citations == []


async def test_denial_is_audited_by_the_tools_not_this_subagent(tools, gateway):
    """
    §3: the gate is enforced twice already, once per tool. This sub-agent adds
    no third check — so exactly one audit record per tool, no duplicates from
    a redundant sub-agent-level gate.
    """
    tools()

    await cross_case_linkage.run(
        _input(AMBIGUOUS_QUERY, caller=_investigator_caller()), gateway=gateway
    )

    violations = [e for e in gateway.audit_log if e["event_type"] == "authorization_violation"]
    assert len(violations) == 2
    assert {v["details"]["route"] for v in violations} == {"XGRAPH", "XNETWORK"}


def test_subagent_does_not_check_roles_itself():
    """
    A third gate would risk drifting out of sync with the tools' own. Checks
    the executable body rather than the whole file, since the module docstring
    legitimately discusses the role gate it deliberately does not implement.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cross_case_linkage))
    names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "CROSS_CASE_ROLES" not in names, (
        "the sub-agent references the role set — a third gate the contract forbids"
    )


# ── Partial: one succeeds, one empty ─────────────────────────────────────

async def test_xnetwork_empty_still_presents_xgraph(tools, gateway):
    tools(xgraph="ok", xnetwork="empty")

    result = await cross_case_linkage.run(_input(AMBIGUOUS_QUERY), gateway=gateway)

    assert result.status is SubAgentStatus.PARTIAL
    assert result.tools_used == ["XGRAPH"]
    assert result.degraded_from == ["XNETWORK"]
    assert result.cross_case_links


async def test_xgraph_empty_still_presents_xnetwork(tools, gateway):
    tools(xgraph="empty", xnetwork="ok")

    result = await cross_case_linkage.run(_input(AMBIGUOUS_QUERY), gateway=gateway)

    assert result.status is SubAgentStatus.PARTIAL
    assert result.tools_used == ["XNETWORK"]
    assert result.degraded_from == ["XGRAPH"]


async def test_both_empty_is_empty_not_an_error(tools, gateway):
    """
    "No connections exist" is an ANSWER — often the one an investigator most
    needs from a cross-case query.
    """
    tools(xgraph="empty", xnetwork="empty")

    result = await cross_case_linkage.run(_input(AMBIGUOUS_QUERY), gateway=gateway)

    assert result.status is SubAgentStatus.EMPTY
    assert result.answer_text
    assert result.cross_case_links == []
    assert result.status is not SubAgentStatus.ABSTAINED


# ── Unconfirmed links: hedged, never asserted ────────────────────────────

_UNCONFIRMED = [{"from": "VEH-0091", "to": "VEH-0204", "basis": "name match, unverified"}]


async def test_unconfirmed_links_are_flagged(tools, gateway):
    tools(xgraph="ok", unconfirmed=_UNCONFIRMED)

    result = await cross_case_linkage.run(_input(XGRAPH_QUERY), gateway=gateway)

    unconfirmed = [link for link in result.cross_case_links if link.is_unconfirmed]
    assert len(unconfirmed) == 1
    # The basis is carried verbatim so the reader sees WHY it is unconfirmed.
    assert "unverified" in unconfirmed[0].description.lower()
    assert "possible identity link" in unconfirmed[0].description.lower()


async def test_unconfirmed_links_contribute_a_caveat(tools, gateway):
    """
    [PRESERVE §3] The hedge must travel with the payload, not depend on a
    consumer noticing a per-item boolean.
    """
    tools(xgraph="ok", unconfirmed=_UNCONFIRMED)

    result = await cross_case_linkage.run(_input(XGRAPH_QUERY), gateway=gateway)

    assert result.caveats
    assert any("unconfirmed" in c.lower() for c in result.caveats)


async def test_unconfirmed_links_rank_below_confirmed(tools, gateway):
    """
    A high-similarity unconfirmed match is still a lead, not a finding.
    Ranking it first would invert the distinction is_unconfirmed preserves.
    """
    tools(xgraph="ok", unconfirmed=_UNCONFIRMED)

    result = await cross_case_linkage.run(_input(XGRAPH_QUERY), gateway=gateway)

    flags = [link.is_unconfirmed for link in result.cross_case_links]
    assert flags == sorted(flags), "an unconfirmed link outranked a confirmed one"


async def test_unconfirmed_links_survive_an_otherwise_empty_xgraph(tools, gateway):
    """
    "No confirmed connections, but these possible matches" is a real finding —
    reporting nothing would discard it.
    """
    tools(xgraph="empty", xnetwork="empty", unconfirmed=_UNCONFIRMED)

    result = await cross_case_linkage.run(_input(XGRAPH_QUERY), gateway=gateway)

    assert result.cross_case_links
    assert all(link.is_unconfirmed for link in result.cross_case_links)


async def test_no_unconfirmed_links_means_no_hedge_caveat(tools, gateway):
    tools(xgraph="ok")

    result = await cross_case_linkage.run(_input(XGRAPH_QUERY), gateway=gateway)

    assert not any("unconfirmed" in c.lower() for c in result.caveats)


# ── Verifier gate ────────────────────────────────────────────────────────

async def test_failing_verifier_abstains(tools, gateway):
    tools()

    result = await cross_case_linkage.run(
        _input(f"{UNGROUNDED_TRIGGER} {XGRAPH_QUERY}"), gateway=gateway
    )

    assert result.status is SubAgentStatus.ABSTAINED
    assert result.answer_text is None


async def test_verifier_receives_cross_case_sentinel_and_allowed_ids(tools, gateway, monkeypatch):
    """
    Cross-case routes verify against the "cross_case" sentinel with the real
    case IDs passed as cross_case_ids — without them the leakage check would
    reject legitimate cross-case evidence.
    """
    tools()
    seen: dict = {}

    real_verify = cross_case_linkage.verify_grounding

    async def _spy(answer, cited_chunks, case_id, cross_case_ids=None, **kw):
        seen["case_id"] = case_id
        seen["cross_case_ids"] = cross_case_ids
        return await real_verify(answer, cited_chunks, case_id,
                                 cross_case_ids=cross_case_ids, **kw)

    monkeypatch.setattr(cross_case_linkage, "verify_grounding", _spy)

    await cross_case_linkage.run(_input(XGRAPH_QUERY), gateway=gateway)

    assert seen["case_id"] == "cross_case"
    assert seen["cross_case_ids"]


# ── Bounded payload ──────────────────────────────────────────────────────

def test_subagent_result_has_no_field_that_can_hold_evidence():
    offenders = [
        name for name, field in SubAgentResult.model_fields.items()
        if "EvidenceChunk" in repr(field.annotation)
    ]
    assert not offenders, (
        f"SubAgentResult fields {offenders} can hold EvidenceChunk objects. "
        "Design §3: the bounded payload must never carry raw evidence upward."
    )


async def test_handoff_carries_links_not_chunks(tools, gateway):
    tools()

    result = await cross_case_linkage.run(_input(XGRAPH_QUERY), gateway=gateway)

    assert result.cross_case_links
    assert all(isinstance(link, CrossCaseLink) for link in result.cross_case_links)
    assert not any(isinstance(link, EvidenceChunk) for link in result.cross_case_links)
    assert all(isinstance(c, Citation) for c in result.citations)


async def test_links_are_capped(tools, gateway):
    tools(xgraph="ok", unconfirmed=[
        {"from": f"E{i}", "to": f"E{i+1}", "basis": "unverified"} for i in range(30)
    ])

    result = await cross_case_linkage.run(_input(XGRAPH_QUERY), gateway=gateway)

    assert len(result.cross_case_links) <= 10


# ── §2.1.4.1 trace mechanism: tool-emitted suffices (CONFIRMED) ──────────

async def test_relies_on_tool_emitted_events(tools, gateway):
    """
    Confirmed empirically, not inherited: neither tool can declare a fallback,
    neither names a degradation target, they share no backing store, and their
    result shapes are disjoint — so collapse is impossible and tool-level
    events are unambiguous. Sub-agent-level linkage:* events would duplicate
    the same transition.
    """
    tools()

    recorder = EventRecorder()
    await cross_case_linkage.run(_input(AMBIGUOUS_QUERY), events=recorder, gateway=gateway)

    steps = [e.step for e in recorder.events]
    assert "tool:xgraph" in steps
    assert "tool:xnetwork" in steps
    assert not any(s.startswith("linkage:") for s in steps)


def test_the_two_tools_cannot_collapse_into_each_other():
    """
    The empirical basis for the trace decision above, asserted so a future
    change that introduces a shared fallback target fails here rather than
    silently making the tool-level events ambiguous.
    """
    from src.pipeline.harness.contracts import XGraphToolResult, XNetworkToolResult

    for model in (XGraphToolResult, XNetworkToolResult):
        assert model(status="empty").fallback_to_rag is False
        annotation = repr(model.model_fields["fallback_to_rag"].annotation)
        assert "Literal[False]" in annotation


# ── Automatic tracing ────────────────────────────────────────────────────

async def test_traced_automatically_without_extra_wiring(tools, gateway):
    tools()

    supervisor._NODES[cross_case_linkage.NAME] = cross_case_linkage.run
    original = supervisor._route
    try:
        supervisor._route = lambda _i, _r: cross_case_linkage.NAME
        recorder = EventRecorder()
        state = await supervisor.invoke(_input(XGRAPH_QUERY), _ROUTE_RESULT, events=recorder)
    finally:
        supervisor._route = original
        supervisor._NODES.pop(cross_case_linkage.NAME, None)

    traced = [e for e in recorder.events if getattr(e, "trace", None)]
    assert len(traced) == 1
    assert "XGRAPH" in traced[0].trace["tools_used"]
    assert state.result.status in (SubAgentStatus.OK, SubAgentStatus.PARTIAL)
