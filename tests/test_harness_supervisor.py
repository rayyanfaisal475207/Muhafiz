"""
Tests for src/pipeline/harness/supervisor.py (Phase 1).

Covers:
  (a) correct classification -> dispatch using mock sub-agents satisfying
      the SubAgent Protocol (not real sub-agents — none exist yet);
  (b) CallerContext passed through byte-for-byte unchanged (object
      identity preserved, not merely equal);
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
    INVESTIGATIVE_ANALYSIS,
    LARGE_SCALE_AGGREGATE,
    REPORT_DRAFTING,
    SEMANTIC_SEARCH,
    Supervisor,
    classify_to_subagent,
    register,
    unregister,
)
from src.pipeline.harness.types import (
    CallerContext,
    PipelineEvent,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)


def _caller(role=Role.INVESTIGATOR, **kw):
    return CallerContext(user_id="u1", role=role, active_case_id="CASE-001", **kw)


def _agent_input(caller=None, query_text="what happened in this case?", **kw):
    return SubAgentInput(query_text=query_text, caller=caller or _caller(), **kw)


def _mock_sub_agent(name: str, result: SubAgentResult):
    """A minimal stand-in satisfying the SubAgent Protocol — captures the
    exact `agent_input` it received for later inspection."""
    calls = []

    async def _handler(agent_input: SubAgentInput) -> SubAgentResult:
        calls.append(agent_input)
        return result

    _handler.name = name
    _handler.calls = calls
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
        ({"route": "XGRAPH", "output_format": "chat"}, CROSS_CASE_LINKAGE),
        ({"route": "XNETWORK", "output_format": "chat"}, CROSS_CASE_LINKAGE),
        ({"route": "XAGG", "output_format": "chat"}, LARGE_SCALE_AGGREGATE),
        ({"route": "DIRECT", "output_format": "chat"}, SEMANTIC_SEARCH),
        ({"route": "WEB", "output_format": "chat"}, SEMANTIC_SEARCH),
        # File output_format overrides the route entirely.
        ({"route": "RAG", "output_format": "file_pdf"}, REPORT_DRAFTING),
        ({"route": "XAGG", "output_format": "file_xlsx"}, REPORT_DRAFTING),
    ],
)
def test_classify_to_subagent(route_result, expected_name):
    assert classify_to_subagent(route_result) == expected_name


@pytest.mark.asyncio
async def test_dispatch_routes_to_correct_registered_mock(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "XAGG", "output_format": "chat"})

    expected = SubAgentResult(status=SubAgentStatus.OK, answer_text="42 cases")
    mock = _mock_sub_agent(LARGE_SCALE_AGGREGATE, expected)
    isolated_registry[LARGE_SCALE_AGGREGATE] = mock

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(_agent_input(query_text="how many cases in total"))

    assert result is expected
    assert len(mock.calls) == 1


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
# (b) CallerContext threaded through completely unchanged
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
    assert received.caller is caller
    assert received.caller.role is Role.PLATFORM_ADMIN
    assert received.caller.preferred_language == "ur"
    assert received.caller.active_case_id == "CASE-001"


@pytest.mark.asyncio
async def test_caller_role_never_defaulted_for_investigator(monkeypatch, isolated_registry):
    # Regression guard for the historical bug documented throughout the
    # design/interfaces docs: role must never be silently defaulted to
    # "investigator" (or anything else) on the way through the Supervisor.
    _stub_route_query(monkeypatch, {"route": "XGRAPH", "output_format": "chat"})

    caller = _caller(role=Role.STATION_ADMIN)
    mock = _mock_sub_agent(CROSS_CASE_LINKAGE, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[CROSS_CASE_LINKAGE] = mock

    sup = Supervisor(registry=isolated_registry)
    await sup.handle(_agent_input(caller=caller))

    assert mock.calls[0].caller.role is Role.STATION_ADMIN


# ═══════════════════════════════════════════════════════════════════════
# (c) unregistered route
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_unregistered_route_returns_typed_not_available_result(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "XAGG", "output_format": "chat"})

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
    _stub_route_query(monkeypatch, {"route": "XNETWORK", "output_format": "chat"})

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
