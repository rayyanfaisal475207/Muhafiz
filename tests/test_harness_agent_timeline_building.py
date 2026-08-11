"""
Tests for src/pipeline/harness/agents/timeline_building.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 4, "Phase 5" per this
session's brief).

Covers:
  (a) successful timeline with mixed conflict states (CONFLICT/NONE) ->
      status=OK, events ordered chronologically, deterministic answer_text;
  (b) no date-bearing edges -> status=EMPTY, not an error;
  (c) conflict-detection fetch failing while date edges resolve fine ->
      status=PARTIAL, every event conflict_state=UNKNOWN (never NONE),
      degraded_from=[] (this session's resolved Deviation 2 -- see the
      module's own docstring), a caveat naming conflict detection;
  (d) date-edge fetch itself raising -> ABSTAINED, error propagated;
  (e) no active case -> EMPTY (not an error, not an exception);
  (f) module self-registration and a Supervisor.handle() integration test
      that bypasses route_query()'s real classification (per this
      session's explicit "no classification-trigger guessing" scope) by
      monkeypatching classify_to_subagent() directly.

`scoped_cypher` is monkeypatched at the module level (`tb_mod.*`) in every
test that needs it -- no live AGE/Postgres, per this session's scope.
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.agents.timeline_building as tb_mod
from src.pipeline.harness.agents.timeline_building import timeline_building
from src.pipeline.harness.supervisor import (
    Supervisor,
    TIMELINE_BUILDING,
    get_registered,
)
from src.pipeline.harness.types import (
    CallerContext,
    ConflictState,
    ExecutionContext,
    Role,
    SubAgentInput,
    SubAgentStatus,
)


def _caller(role=Role.INVESTIGATOR, active_case_id="CASE-001", **kw):
    return CallerContext(user_id="u1", role=role, active_case_id=active_case_id, **kw)


def _execution(caller=None):
    return ExecutionContext(caller=caller or _caller())


def _agent_input(caller=None, query_text="build a timeline for this case", **kw):
    return SubAgentInput(query_text=query_text, execution=_execution(caller=caller), **kw)


def _dated_row(entity_id, description, occurred_on, locked=False):
    return {
        "entity_id": entity_id,
        "description": description,
        "occurred_on": occurred_on,
        "locked": locked,
    }


def _stub_scoped_cypher(monkeypatch, dated_rows=None, conflict_rows=None, conflict_exc=None, dated_exc=None):
    """
    Routes on which query the call site is running -- distinguished by the
    Cypher text itself (contains "OCCURRED_ON" for the date fetch,
    "CONFLICTS_WITH" for the conflict fetch), the same shape
    scoped_cypher() is actually called with in the module under test.
    """

    async def _fake(cypher_query, case_id, params=None, columns=("result",), **kw):
        assert case_id  # scoped_cypher() itself would refuse an empty one
        if "OCCURRED_ON" in cypher_query:
            if dated_exc is not None:
                raise dated_exc
            return dated_rows or []
        if "CONFLICTS_WITH" in cypher_query:
            if conflict_exc is not None:
                raise conflict_exc
            return conflict_rows or []
        raise AssertionError(f"Unexpected Cypher query in test stub: {cypher_query!r}")

    monkeypatch.setattr(tb_mod, "scoped_cypher", _fake)


# ═══════════════════════════════════════════════════════════════════════
# (a) successful timeline, mixed conflict states, chronological order
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_successful_timeline_mixed_conflict_states(monkeypatch):
    _stub_scoped_cypher(
        monkeypatch,
        dated_rows=[
            _dated_row("INCIDENT-002", "Second incident", "2026-02-01"),
            _dated_row("INCIDENT-001", "First incident", "2026-01-01", locked=True),
            _dated_row("INCIDENT-003", "Third incident", "2026-03-01"),
        ],
        conflict_rows=[
            {"a_id": "INCIDENT-001", "b_id": "INCIDENT-002", "basis": "Conflicting dates reported."},
        ],
    )

    result = await timeline_building(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert result.tools_used == ["GRAPH"]
    assert result.degraded_from == []
    assert len(result.events) == 3
    # Chronological order, not insertion order.
    assert [e.event_id for e in result.events] == ["INCIDENT-001", "INCIDENT-002", "INCIDENT-003"]

    e1, e2, e3 = result.events
    assert e1.conflict_state == ConflictState.CONFLICT
    assert e1.conflict_basis == "Conflicting dates reported."
    assert e1.locked is True
    assert e2.conflict_state == ConflictState.CONFLICT
    assert e3.conflict_state == ConflictState.NONE  # checked, no conflict found
    assert e3.conflict_basis is None

    # Deterministic, non-null answer_text summarizing count/conflicts.
    assert result.answer_text is not None
    assert "3 dated event" in result.answer_text
    assert "2026-01-01" in result.answer_text and "2026-03-01" in result.answer_text

    # Bounded payload -- never raw graph rows.
    assert not hasattr(result, "chunks")


# ═══════════════════════════════════════════════════════════════════════
# (b) no date-bearing edges -> EMPTY, not an error
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_dated_edges_returns_empty_not_error(monkeypatch):
    _stub_scoped_cypher(monkeypatch, dated_rows=[])

    result = await timeline_building(_agent_input())

    assert result.status == SubAgentStatus.EMPTY
    assert result.answer_text is None
    assert result.events == []
    assert result.error is None
    assert result.caveats


# ═══════════════════════════════════════════════════════════════════════
# (c) RESOLVED-5's exact scenario: conflict detection fails, dates resolve
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_conflict_detection_failure_yields_all_unknown_and_partial(monkeypatch):
    _stub_scoped_cypher(
        monkeypatch,
        dated_rows=[
            _dated_row("INCIDENT-001", "First incident", "2026-01-01"),
            _dated_row("INCIDENT-002", "Second incident", "2026-02-01"),
        ],
        conflict_exc=RuntimeError("AGE query timed out"),
    )

    result = await timeline_building(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert len(result.events) == 2
    # [RESOLVED-5] Every event UNKNOWN -- never NONE, which would assert
    # an all-clear the check never actually verified.
    assert all(e.conflict_state == ConflictState.UNKNOWN for e in result.events)
    assert all(e.conflict_basis is None for e in result.events)
    # [This session's resolved Deviation 2] degraded_from stays [] --
    # GRAPH's date-edge fetch genuinely succeeded and belongs in
    # tools_used only; the conflict-check failure is carried by
    # status=PARTIAL + the caveat below instead.
    assert result.degraded_from == []
    assert result.tools_used == ["GRAPH"]
    assert any("conflict" in c.lower() for c in result.caveats)
    assert result.answer_text is not None
    assert "unknown" in result.answer_text.lower()


# ═══════════════════════════════════════════════════════════════════════
# (d) date-edge fetch itself raises -> ABSTAINED, error propagated
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_date_edge_fetch_failure_abstains(monkeypatch):
    _stub_scoped_cypher(monkeypatch, dated_exc=RuntimeError("postgres unreachable"))

    result = await timeline_building(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.events == []
    assert result.error is not None
    assert result.error.kind == "upstream_failure"


# ═══════════════════════════════════════════════════════════════════════
# (e) no active case -> EMPTY, not an exception
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_active_case_returns_empty():
    result = await timeline_building(_agent_input(caller=_caller(active_case_id=None)))

    assert result.status == SubAgentStatus.EMPTY
    assert result.answer_text is None
    assert result.caveats


# ═══════════════════════════════════════════════════════════════════════
# (f) registration + Supervisor integration, classification bypassed
# ═══════════════════════════════════════════════════════════════════════

def test_timeline_building_is_registered_under_its_own_name():
    # Import-time self-registration (module docstring) -- importing the
    # module above already registered it into the live, module-level
    # registry; this just asserts that actually happened.
    assert get_registered(TIMELINE_BUILDING) is timeline_building


@pytest.mark.asyncio
async def test_supervisor_dispatches_to_real_timeline_building_via_direct_dispatch(monkeypatch):
    """
    Supervisor.handle() -> real Timeline Building -> real scoped_cypher()
    call sites (stubbed to deterministic test data, not live infra).

    [Classification-reachability caveat, stated per this session's scope]
    route_query() has no real signal for Timeline Building today
    (AGENT_HARNESS_IMPLEMENTATION_PLAN.md §9/§11) -- this test does not
    exercise real end-user classification. It forces the dispatch by
    monkeypatching classify_to_subagent() directly, proving the Supervisor
    <-> sub-agent wiring itself works, while leaving real classification
    reachability as the documented, separately-tracked gap it is.
    """
    import src.pipeline.harness.supervisor as supervisor_mod

    async def _fake_route_query(query_text: str) -> dict:
        return {"route": "GRAPH", "output_format": "chat"}

    monkeypatch.setattr(supervisor_mod, "route_query", _fake_route_query)
    monkeypatch.setattr(
        supervisor_mod, "classify_to_subagent", lambda route_result, query_text="": TIMELINE_BUILDING
    )

    _stub_scoped_cypher(
        monkeypatch,
        dated_rows=[_dated_row("INCIDENT-001", "Only incident", "2026-01-01")],
        conflict_rows=[],
    )

    sup = Supervisor()  # no override -> real module-level registry
    result = await sup.handle(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert len(result.events) == 1
    assert result.events[0].conflict_state == ConflictState.NONE
    assert result.tools_used == ["GRAPH"]
