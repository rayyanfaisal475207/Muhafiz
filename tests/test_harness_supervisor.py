"""
Tests for src/pipeline/harness/supervisor.py (Phase 1).

Covers:
  (a) correct classification -> dispatch using mock sub-agents satisfying
      the SubAgent Protocol (not real sub-agents — none exist yet);
  (b) ExecutionContext (wrapping CallerContext, per the contract retrofit
      in AGENT_HARNESS_IMPLEMENTATION_PLAN.md §10) passed through
      byte-for-byte unchanged (object identity preserved, not merely
      equal);
  (c) correct behavior on an unregistered route (typed ABSTAINED result,
      not a crash, not a silent fallback);
  (d) PipelineEvent emitted with the right shape (step/status/detail,
      existing five-value SSE vocabulary, one event per meaningful
      transition — never collapsed into one "ran" event).

`route_query` is monkeypatched at the module level (`supervisor.route_query`)
in every test — none of these exercise the real LLM-backed router; that is
router.py's own test suite's job, not this one's (per the plan: reuse
router.py, don't re-test it here).
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.supervisor as supervisor_mod
from src.pipeline.harness.supervisor import (
    CASE_SUMMARIZATION,
    CROSS_CASE_LINKAGE,
    GLOBAL_SEARCH,
    INVESTIGATIVE_ANALYSIS,
    LARGE_SCALE_AGGREGATE,
    NO_SUB_AGENT,
    REPORT_DRAFTING,
    SEMANTIC_SEARCH,
    TIMELINE_BUILDING,
    Supervisor,
    classify_to_subagent,
    register,
    unregister,
)
from src.pipeline.harness.types import (
    CallerContext,
    ExecutionContext,
    PipelineEvent,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)


def _caller(role=Role.INVESTIGATOR, **kw):
    return CallerContext(user_id="u1", role=role, active_case_id="CASE-001", **kw)


def _execution(caller=None, **kw):
    return ExecutionContext(caller=caller or _caller(), **kw)


def _agent_input(caller=None, query_text="what happened in this case?", **kw):
    return SubAgentInput(query_text=query_text, execution=_execution(caller=caller), **kw)


def _mock_sub_agent(name: str, result: SubAgentResult):
    """A minimal stand-in satisfying the SubAgent Protocol — captures the
    exact `agent_input` it received for later inspection.

    [AMENDMENT — pre-Phase-7 contract amendment] Accepts the same
    keyword-only `on_event` every real sub-agent now does (see
    `types.SubAgent`'s amendment note) and records whatever it was given
    (including `None`) so tests can assert Supervisor.handle() actually
    forwards it, rather than only that the call didn't raise.

    [AMENDMENT — pre-Phase-8 contract amendment] Same treatment for the new
    keyword-only `gateway` parameter."""
    calls = []
    on_events_received = []
    gateways_received = []

    async def _handler(agent_input: SubAgentInput, *, on_event=None, gateway=None) -> SubAgentResult:
        calls.append(agent_input)
        on_events_received.append(on_event)
        gateways_received.append(gateway)
        return result

    _handler.name = name
    _handler.calls = calls
    _handler.on_events_received = on_events_received
    _handler.gateways_received = gateways_received
    return _handler


@pytest.fixture(autouse=True)
def isolated_registry():
    """
    Every test gets its own registry dict, never the module-level global
    one — prevents one test's registered mock from leaking into another
    (and from leaking into any other test module that imports supervisor).
    """
    return {}


def _stub_route_query(monkeypatch, route_result: dict):
    async def _fake(query_text: str) -> dict:
        return route_result
    monkeypatch.setattr(supervisor_mod, "route_query", _fake)


# ═══════════════════════════════════════════════════════════════════════
# (a) classification -> dispatch
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "route_result,expected_name",
    [
        ({"route": "RAG", "output_format": "chat"}, SEMANTIC_SEARCH),
        ({"route": "GRAPH", "output_format": "chat"}, CASE_SUMMARIZATION),
        ({"route": "GRAPH_HYBRID", "output_format": "chat"}, CASE_SUMMARIZATION),
        ({"route": "SQL", "output_format": "chat"}, INVESTIGATIVE_ANALYSIS),
        # XGRAPH/XNETWORK/XAGG carry case_scope="cross_case", matching real
        # route_query() output (router.py never forces these three back to
        # within_case) — required for the case_scope demotion guard
        # (reconciliation Unit 2) to dispatch them to their real sub-agent
        # rather than demoting to Semantic Search.
        ({"route": "XGRAPH", "case_scope": "cross_case", "output_format": "chat"}, CROSS_CASE_LINKAGE),
        ({"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"}, CROSS_CASE_LINKAGE),
        ({"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"}, LARGE_SCALE_AGGREGATE),
        # [Reconciliation fix — Unit 2] DIRECT -> NO_SUB_AGENT, not Semantic
        # Search. See NO_SUB_AGENT's own comment in supervisor.py.
        ({"route": "DIRECT", "output_format": "chat"}, NO_SUB_AGENT),
        ({"route": "WEB", "output_format": "chat"}, SEMANTIC_SEARCH),
        # File output_format overrides the route entirely.
        ({"route": "RAG", "output_format": "file_pdf"}, REPORT_DRAFTING),
        ({"route": "XAGG", "case_scope": "cross_case", "output_format": "file_xlsx"}, REPORT_DRAFTING),
        # [Reconciliation fix — Unit 2] DIRECT wins even over a file
        # output_format — see classify_to_subagent()'s own comment.
        ({"route": "DIRECT", "output_format": "file_pdf"}, NO_SUB_AGENT),
        # [Reconciliation fix — Unit 2] case_scope demotion guard: a
        # cross-case route whose case_scope did NOT come back "cross_case"
        # (a genuinely possible LLM-classification outcome, not just a
        # hypothetical) demotes to Semantic Search rather than reaching a
        # cross-case sub-agent under a within-case scope.
        ({"route": "XAGG", "case_scope": "within_case", "output_format": "chat"}, SEMANTIC_SEARCH),
        ({"route": "XGRAPH", "output_format": "chat"}, SEMANTIC_SEARCH),
    ],
)
def test_classify_to_subagent(route_result, expected_name):
    assert classify_to_subagent(route_result) == expected_name


# ── Provisional classification triggers: Timeline Building / broader
# Investigative Analysis reach (resolved via AskUserQuestion -- see the
# progress-log entry for this branch for the full "Problem A" reasoning) ──

@pytest.mark.parametrize(
    "query_text",
    [
        "give me a timeline of events for this case",
        "what is the chronological order of events",
        "show me the sequence of events",
        "what happened when in this investigation",
        "اس کیس کے واقعات کی ترتیب دکھائیں",
        "کب کیا ہوا اس کیس میں",
        "is case ki waqeat ki tarteeb batayen",
        "kab kya hua tha",
    ],
)
def test_timeline_trigger_overrides_graph_classification(query_text):
    route_result = {"route": "GRAPH", "output_format": "chat"}
    assert classify_to_subagent(route_result, query_text) == TIMELINE_BUILDING


@pytest.mark.parametrize(
    "query_text",
    [
        "give me a deep dive on this case",
        "I need a full analysis of this case",
        "give me a comprehensive analysis",
        "run a detailed investigation into this",
        "give me the full picture of this case",
        "اس کیس کی مکمل تحقیقات کریں",
        "تفصیلی تجزیہ درکار ہے",
        "گہرائی سے تجزیہ کریں",
        "mukammal tehqiqat chahiye",
        "tafseeli tajzia karen",
    ],
)
def test_investigative_analysis_trigger_overrides_base_classification(query_text):
    route_result = {"route": "RAG", "output_format": "chat"}
    assert classify_to_subagent(route_result, query_text) == INVESTIGATIVE_ANALYSIS


@pytest.mark.parametrize("route", ["XGRAPH", "XAGG", "XNETWORK"])
@pytest.mark.parametrize(
    "query_text",
    ["give me a timeline of events", "give me a deep dive on this"],
)
def test_provisional_triggers_never_override_a_cross_case_classification(route, query_text):
    """[PRESERVE] A query matching both a cross-case trigger and one of the
    two provisional patterns is a genuine ambiguity these overrides must
    not try to resolve heuristically -- router.py's own already-evidenced
    cross-case precedence wins outright."""
    route_result = {"route": route, "case_scope": "cross_case", "output_format": "chat"}
    result = classify_to_subagent(route_result, query_text)
    assert result == CROSS_CASE_LINKAGE if route in ("XGRAPH", "XNETWORK") else result == LARGE_SCALE_AGGREGATE


# [AMENDMENT — findings.md Module 9, "Global Search"] classify_to_subagent()
# coverage for the new override, per findings.md's own Test plan.
def test_global_search_trigger_overrides_xnetwork_default():
    route_result = {"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"}
    assert classify_to_subagent(route_result, "what are the top 5 themes in the data?") == GLOBAL_SEARCH


def test_global_search_trigger_does_not_fire_on_xnetworks_existing_default_shape():
    """XNETWORK's existing default (a specific network/cluster question,
    findings.md's own repro text) must stay CROSS_CASE_LINKAGE, unaffected
    by the new override."""
    route_result = {"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"}
    assert (
        classify_to_subagent(route_result, "overall picture of associate networks across the robbery cases")
        == CROSS_CASE_LINKAGE
    )


def test_global_search_trigger_only_applies_to_the_xnetwork_route():
    """A 'top 5 themes' phrasing on a non-XNETWORK route must not
    accidentally reroute -- the override is scoped to XNETWORK only."""
    route_result = {"route": "RAG", "output_format": "chat"}
    assert classify_to_subagent(route_result, "what are the top 5 themes in the data?") == SEMANTIC_SEARCH


def test_global_search_is_covered_by_the_case_scope_demotion_guard():
    """GLOBAL_SEARCH is cross-case-role-gated like CROSS_CASE_LINKAGE/
    LARGE_SCALE_AGGREGATE -- it must be included in _CROSS_CASE_SUBAGENTS
    so the same demotion guard applies to it: an XNETWORK route whose
    case_scope did NOT come back "cross_case" must demote to Semantic
    Search, not reach Global Search under a within-case scope."""
    route_result = {"route": "XNETWORK", "case_scope": "within_case", "output_format": "chat"}
    assert classify_to_subagent(route_result, "what are the top 5 themes in the data?") == SEMANTIC_SEARCH


def test_file_output_still_overrides_provisional_triggers():
    route_result = {"route": "GRAPH", "output_format": "file_pdf"}
    assert classify_to_subagent(route_result, "give me a timeline of events") == REPORT_DRAFTING


def test_classify_to_subagent_default_query_text_matches_pre_amendment_behavior():
    """The one-argument call form (every pre-existing direct caller,
    including this file's own parametrized test above) must behave
    identically to before this amendment -- query_text="" can never match
    either provisional trigger."""
    assert classify_to_subagent({"route": "GRAPH", "output_format": "chat"}) == CASE_SUMMARIZATION


def test_provisional_triggers_do_not_fire_on_ordinary_queries():
    """Narrow-by-design check: everyday case questions must not accidentally
    contain trigger language and get silently rerouted."""
    ordinary_queries = [
        "who is the accused in this case",
        "what documents are attached to this case",
        "summarize this case for me",
        "کیس کی تفصیلات بتائیں",
    ]
    for q in ordinary_queries:
        assert classify_to_subagent({"route": "GRAPH", "output_format": "chat"}, q) == CASE_SUMMARIZATION


def test_route_query_contract_is_unaffected_by_the_classification_amendment():
    """[PRESERVE -- non-negotiable] The provisional overrides above live
    entirely inside classify_to_subagent() and must never touch
    router.py's own classification contract, which orchestrator.py still
    depends on for all of its own (still-live) routing. Structural proof:
    router.py's own set of valid routes is exactly the same nine values it
    has always been -- nothing in this amendment added a tenth."""
    from src.pipeline.router import _VALID_ROUTES

    assert set(_VALID_ROUTES) == {
        "DIRECT", "RAG", "WEB", "SQL", "GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG", "XNETWORK",
    }


@pytest.mark.asyncio
async def test_dispatch_routes_to_correct_registered_mock(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"})

    expected = SubAgentResult(status=SubAgentStatus.OK, answer_text="42 cases")
    mock = _mock_sub_agent(LARGE_SCALE_AGGREGATE, expected)
    isolated_registry[LARGE_SCALE_AGGREGATE] = mock

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(_agent_input(query_text="how many cases in total"))

    assert result is expected
    assert len(mock.calls) == 1


@pytest.mark.asyncio
async def test_handle_threads_query_text_into_classify_to_subagent_for_provisional_triggers(
    monkeypatch, isolated_registry
):
    """End-to-end proof (not just the pure-function test above) that
    Supervisor.handle() actually passes agent_input.query_text through to
    classify_to_subagent() -- a query whose base route_query() result is
    an ordinary GRAPH classification still reaches Timeline Building when
    its text matches a provisional trigger."""
    _stub_route_query(monkeypatch, {"route": "GRAPH", "output_format": "chat"})

    expected = SubAgentResult(status=SubAgentStatus.OK, answer_text="3 events")
    mock = _mock_sub_agent(TIMELINE_BUILDING, expected)
    isolated_registry[TIMELINE_BUILDING] = mock
    # A registered Case Summarization mock proves dispatch went to Timeline
    # Building BECAUSE of the query text, not by some other accident (e.g.
    # an unregistered-route fallback landing on the wrong name).
    other_mock = _mock_sub_agent(CASE_SUMMARIZATION, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[CASE_SUMMARIZATION] = other_mock

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(_agent_input(query_text="give me a timeline of events for this case"))

    assert result is expected
    assert len(mock.calls) == 1
    assert len(other_mock.calls) == 0


@pytest.mark.asyncio
async def test_dispatch_does_not_call_unrelated_registered_mocks(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})

    semantic_mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    other_mock = _mock_sub_agent(LARGE_SCALE_AGGREGATE, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = semantic_mock
    isolated_registry[LARGE_SCALE_AGGREGATE] = other_mock

    sup = Supervisor(registry=isolated_registry)
    await sup.handle(_agent_input())

    assert len(semantic_mock.calls) == 1
    assert len(other_mock.calls) == 0


# ═══════════════════════════════════════════════════════════════════════
# (b) ExecutionContext (wrapping CallerContext) threaded through completely
# unchanged — [RENAMED, AGENT_HARNESS_IMPLEMENTATION_PLAN.md §10.1]
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_caller_context_passed_through_unchanged_by_identity(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})

    caller = _caller(role=Role.PLATFORM_ADMIN, preferred_language="ur")
    agent_input = _agent_input(caller=caller)

    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    sup = Supervisor(registry=isolated_registry)
    await sup.handle(agent_input)

    received = mock.calls[0]
    # Identity, not just equality: the Supervisor must not reconstruct,
    # copy, or merge this object with anything else.
    assert received is agent_input
    assert received.execution.caller is caller
    assert received.execution.caller.role is Role.PLATFORM_ADMIN
    assert received.execution.caller.preferred_language == "ur"
    assert received.execution.caller.active_case_id == "CASE-001"


@pytest.mark.asyncio
async def test_caller_role_never_defaulted_for_investigator(monkeypatch, isolated_registry):
    # Regression guard for the historical bug documented throughout the
    # design/interfaces docs: role must never be silently defaulted to
    # "investigator" (or anything else) on the way through the Supervisor.
    _stub_route_query(monkeypatch, {"route": "XGRAPH", "case_scope": "cross_case", "output_format": "chat"})

    caller = _caller(role=Role.STATION_ADMIN)
    mock = _mock_sub_agent(CROSS_CASE_LINKAGE, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[CROSS_CASE_LINKAGE] = mock

    sup = Supervisor(registry=isolated_registry)
    await sup.handle(_agent_input(caller=caller))

    assert mock.calls[0].execution.caller.role is Role.STATION_ADMIN


# ═══════════════════════════════════════════════════════════════════════
# (c) unregistered route
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_unregistered_route_returns_typed_not_available_result(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"})

    sup = Supervisor(registry=isolated_registry)  # empty registry
    result = await sup.handle(_agent_input())

    assert isinstance(result, SubAgentResult)
    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.error is not None
    assert result.error.kind == "upstream_failure"
    assert LARGE_SCALE_AGGREGATE in result.error.message
    assert any(LARGE_SCALE_AGGREGATE in c for c in result.caveats)


@pytest.mark.asyncio
async def test_unregistered_route_does_not_raise(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "GRAPH", "output_format": "chat"})
    sup = Supervisor(registry=isolated_registry)
    # Must not raise — this is expected, not exceptional, behavior for
    # every route right now.
    result = await sup.handle(_agent_input())
    assert result.status == SubAgentStatus.ABSTAINED


@pytest.mark.asyncio
async def test_module_level_register_and_unregister(monkeypatch):
    """Exercises the real module-level registry (register/unregister),
    not an isolated dict — this is the mechanism a future sub-agent module
    actually uses at import time."""
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    try:
        register(mock)
        assert supervisor_mod.get_registered(SEMANTIC_SEARCH) is mock

        _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
        sup = Supervisor()  # no override -> reads the module-level registry
        result = await sup.handle(_agent_input())
        assert result.status == SubAgentStatus.OK
        assert len(mock.calls) == 1
    finally:
        unregister(SEMANTIC_SEARCH)
    assert supervisor_mod.get_registered(SEMANTIC_SEARCH) is None


def test_register_rejects_unknown_sub_agent_name():
    bad = _mock_sub_agent("Not A Real Sub-Agent", SubAgentResult(status=SubAgentStatus.OK))
    with pytest.raises(ValueError):
        register(bad)


# ═══════════════════════════════════════════════════════════════════════
# (d) PipelineEvent shape
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_pipeline_events_emitted_on_successful_dispatch(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    events: list[PipelineEvent] = []
    sup = Supervisor(registry=isolated_registry)
    await sup.handle(_agent_input(), on_event=events.append)

    assert len(events) == 2
    for evt in events:
        assert isinstance(evt, PipelineEvent)
        assert evt.step == "supervisor:dispatch"
        assert evt.status in ("active", "done", "error", "retry", "skipped")
        assert isinstance(evt.detail, str) and evt.detail

    assert events[0].status == "active"
    assert SEMANTIC_SEARCH in events[0].detail
    assert events[1].status == "done"
    assert SEMANTIC_SEARCH in events[1].detail
    assert "ok" in events[1].detail.lower()


@pytest.mark.asyncio
async def test_pipeline_events_emitted_on_unregistered_route(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"})

    events: list[PipelineEvent] = []
    sup = Supervisor(registry=isolated_registry)  # empty
    await sup.handle(_agent_input(), on_event=events.append)

    assert len(events) == 2
    assert events[0].status == "active"
    assert events[1].status == "skipped"
    assert CROSS_CASE_LINKAGE in events[1].detail


@pytest.mark.asyncio
async def test_on_event_is_optional(monkeypatch, isolated_registry):
    # Must not require a sink — dispatch works fine with none given.
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(_agent_input())
    assert result.status == SubAgentStatus.OK


# ═══════════════════════════════════════════════════════════════════════
# (e) on_event threaded down to the sub-agent itself
# [AMENDMENT — pre-Phase-7 contract amendment, mirrors §10/§11's pattern]
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_on_event_forwarded_to_subagent_when_given(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    sup = Supervisor(registry=isolated_registry)
    sink = lambda evt: None
    await sup.handle(_agent_input(), on_event=sink)

    assert mock.on_events_received == [sink]


@pytest.mark.asyncio
async def test_on_event_forwarded_as_none_when_not_given(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    sup = Supervisor(registry=isolated_registry)
    await sup.handle(_agent_input())

    assert mock.on_events_received == [None]


# ═══════════════════════════════════════════════════════════════════════
# (f) gateway threaded down to the sub-agent itself
# [AMENDMENT — pre-Phase-8 contract amendment, mirrors §12/(e) above]
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_gateway_forwarded_to_subagent_when_given(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    sup = Supervisor(registry=isolated_registry)
    fake_gateway = object()
    await sup.handle(_agent_input(), gateway=fake_gateway)

    assert mock.gateways_received == [fake_gateway]


@pytest.mark.asyncio
async def test_gateway_forwarded_as_none_when_not_given(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    sup = Supervisor(registry=isolated_registry)
    await sup.handle(_agent_input())

    assert mock.gateways_received == [None]
