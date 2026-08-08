"""
Timeline Building sub-agent — contract tests.

The case worth guarding hardest is [RESOLVED-5]'s three-state conflict flag.
This is the first live path to exercise it, and the distinction is real here:
`detect_conflicts()` swallows its own fetch failure, so an absence of
CONFLICTS_WITH edges means either "checked, clean" or "never checked" — and a
bool would render both as an all-clear the system never verified.

Production boundaries are mocked; no database, model server, or network.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness import supervisor
from src.pipeline.harness.agents import timeline as timeline_agent
from src.pipeline.harness.contracts import (
    CallerContext,
    Citation,
    ConflictState,
    EvidenceChunk,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)
from src.pipeline.harness.events import EventRecorder
from src.pipeline.harness.tools import registry
from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER


@pytest.fixture(autouse=True)
def _real_tools():
    registry.use_real()
    yield
    registry.use_real()


def _caller() -> CallerContext:
    return CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id="CASE-A")


def _input(query: str = "Build a timeline") -> SubAgentInput:
    return SubAgentInput(query_text=query, caller=_caller())


def _event(eid: str, date: str, *, conflict: str = None, locked: bool = False) -> dict:
    meta = {"case_id": "CASE-A", "occurred_on": date, "locked": locked}
    if conflict:
        meta["conflict_basis"] = conflict
    return {"id": eid, "text": f"Event {eid} on {date}.", "metadata": meta}


@pytest.fixture
def graph_returns(monkeypatch):
    """Drive the real GRAPH tool to a chosen outcome."""
    def _configure(chunks, fail=False):
        async def _retrieve(*a, **k):
            if fail:
                raise RuntimeError("graph unavailable")
            return {
                "chunks": list(chunks),
                "hop_count": 1 if chunks else 0,
                "compounded_confidence": 0.9 if chunks else 1.0,
                "seed_entities": [{"entity_id": "E1"}] if chunks else [],
                "unconfirmed_links": [],
            }

        monkeypatch.setattr("src.retrieval.graph_retriever.retrieve_graph", _retrieve)

    return _configure


# ── [RESOLVED-5] The three states ────────────────────────────────────────

async def test_conflict_found_is_conflict_state_true(graph_returns):
    graph_returns([
        _event("e1", "2026-03-14", conflict="Two incompatible dates recorded"),
        _event("e2", "2026-03-15"),
    ])

    result = await timeline_agent.run(_input())

    flagged = [e for e in result.timeline if e.conflict_state is ConflictState.CONFLICT]
    assert len(flagged) == 1
    assert flagged[0].event_id == "e1"
    assert flagged[0].conflict_basis == "Two incompatible dates recorded"


async def test_no_conflict_basis_is_unknown_not_none(graph_returns):
    """
    Nothing at read time proves detection ran for this case — three ways it may
    not have (the fire-and-forget race in service.py, events reaching the graph
    without an ingestion trigger, and detect_conflicts()'s early return /
    swallowed failure). So an event with no conflict basis is UNKNOWN, never
    NONE.

    Reporting NONE here would assert a clean check the system never performed,
    on what is the COMMON path for a new case — the exact failure RESOLVED-5
    exists to prevent.
    """
    graph_returns([_event("e1", "2026-03-14"), _event("e2", "2026-03-15")])

    result = await timeline_agent.run(_input())

    assert all(e.conflict_state is ConflictState.UNKNOWN for e in result.timeline)
    assert not any(e.conflict_state is ConflictState.NONE for e in result.timeline)


async def test_unverified_timeline_says_so(graph_returns):
    """An UNKNOWN state must reach the reader, not sit silently in a field."""
    graph_returns([_event("e1", "2026-03-14")])

    result = await timeline_agent.run(_input())

    assert result.status is SubAgentStatus.PARTIAL
    assert any("conflict detection" in c.lower() for c in result.caveats)


async def test_marker_present_makes_none_reachable(graph_returns, gateway):
    """
    [Migration 019] With `cases.conflicts_checked_at` set, an unflagged event
    is NONE — the check demonstrably completed and was clean.
    """
    graph_returns([_event("e1", "2026-03-14"), _event("e2", "2026-03-15")])
    gateway.cases["CASE-A"] = {"case_id": "CASE-A"}
    await gateway.mark_conflicts_checked("CASE-A")

    result = await timeline_agent.run(_input(), gateway=gateway)

    assert all(e.conflict_state is ConflictState.NONE for e in result.timeline)
    assert result.status is SubAgentStatus.OK
    assert result.caveats == []


async def test_marker_absent_keeps_unknown(graph_returns, gateway):
    """The race case: detection scheduled but not yet completed."""
    graph_returns([_event("e1", "2026-03-14")])
    gateway.cases["CASE-A"] = {"case_id": "CASE-A"}  # no marker written

    result = await timeline_agent.run(_input(), gateway=gateway)

    assert all(e.conflict_state is ConflictState.UNKNOWN for e in result.timeline)
    assert result.status is SubAgentStatus.PARTIAL


async def test_conflict_beats_the_marker(graph_returns, gateway):
    """A real conflict is CONFLICT whether or not the marker exists."""
    graph_returns([_event("e1", "2026-03-14", conflict="contradictory dates")])
    gateway.cases["CASE-A"] = {"case_id": "CASE-A"}
    await gateway.mark_conflicts_checked("CASE-A")

    result = await timeline_agent.run(_input(), gateway=gateway)

    assert result.timeline[0].conflict_state is ConflictState.CONFLICT


async def test_unreadable_marker_fails_closed_to_unknown(graph_returns, gateway, monkeypatch):
    """
    A gateway error must not be read as a clean check. Failing closed costs
    precision; failing open would assert something false.
    """
    graph_returns([_event("e1", "2026-03-14")])

    async def _boom(_case_id):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(gateway, "get_case", _boom)

    result = await timeline_agent.run(_input(), gateway=gateway)

    assert all(e.conflict_state is ConflictState.UNKNOWN for e in result.timeline)


async def test_missing_case_row_fails_closed_to_unknown(graph_returns, gateway):
    graph_returns([_event("e1", "2026-03-14")])
    # no case row at all

    result = await timeline_agent.run(_input(), gateway=gateway)

    assert all(e.conflict_state is ConflictState.UNKNOWN for e in result.timeline)


async def test_none_is_reachable_only_with_a_confirmed_check(graph_returns):
    """
    The mechanism is in place for when a per-case completion marker exists —
    NONE is unreachable today by DESIGN, not because the code cannot express it.
    Exercised directly so the eventual marker change is a one-line flip.
    """
    from src.pipeline.harness.agents.timeline import _conflict_state_for
    from src.pipeline.harness.contracts import ChunkMetadata

    clean = EvidenceChunk(
        id="e1", text="Event.", metadata=ChunkMetadata(source_tool="GRAPH", case_id="CASE-A"),
    )

    assert _conflict_state_for(clean, detection_confirmed=False)[0] is ConflictState.UNKNOWN
    assert _conflict_state_for(clean, detection_confirmed=True)[0] is ConflictState.NONE


async def test_detection_failure_is_unknown_never_false(graph_returns):
    """
    THE RESOLVED-5 CASE. The check could not run, so no event may claim a
    clean result. A bool would render this identically to "checked, no
    conflict" — asserting an all-clear the system never performed.
    """
    graph_returns([], fail=True)

    result = await timeline_agent.run(_input())

    # The tool failed outright, so there is no timeline to qualify — this
    # abstains rather than presenting unverified events as fact.
    assert result.status is SubAgentStatus.ABSTAINED
    assert result.timeline == []
    assert all(e.conflict_state is not ConflictState.NONE for e in result.timeline)


async def test_unknown_conflict_state_is_the_model_default():
    """
    Constructing an event without an explicit state must not imply "clean".
    Guards the default itself, independent of this sub-agent's logic.
    """
    from src.pipeline.harness.contracts import TimelineEvent

    assert TimelineEvent(event_id="e", description="d").conflict_state is ConflictState.UNKNOWN


# ── Empty timeline: a legitimate outcome, not an error ───────────────────

async def test_no_events_returns_explicit_empty_timeline(graph_returns):
    """
    "Nothing to show" is an ANSWER, not a failure. Distinct from a tool
    failure, which abstains.
    """
    graph_returns([])

    result = await timeline_agent.run(_input())

    assert result.status is SubAgentStatus.OK
    assert result.timeline == []
    assert result.answer_text
    assert result.tools_used == ["GRAPH"]


async def test_empty_timeline_is_not_abstained(graph_returns):
    graph_returns([])

    result = await timeline_agent.run(_input())

    assert result.status is not SubAgentStatus.ABSTAINED
    assert result.status is not SubAgentStatus.EMPTY


async def test_tool_failure_abstains_unlike_empty(graph_returns):
    """The contrast that makes the empty case meaningful."""
    graph_returns([], fail=True)

    result = await timeline_agent.run(_input())

    assert result.status is SubAgentStatus.ABSTAINED
    assert result.degraded_from == ["GRAPH"]


# ── Ordering ─────────────────────────────────────────────────────────────

async def test_events_are_ordered_chronologically(graph_returns):
    graph_returns([
        _event("e3", "2026-05-01"),
        _event("e1", "2026-03-14"),
        _event("e2", "2026-04-02"),
    ])

    result = await timeline_agent.run(_input())

    assert [e.event_id for e in result.timeline] == ["e1", "e2", "e3"]


async def test_undated_events_sort_last_not_first(graph_returns):
    """
    An undated event at the head would read as though it happened first.
    It is still kept — an event we cannot date is still an event.
    """
    undated = {"id": "u1", "text": "No date recorded.", "metadata": {"case_id": "CASE-A"}}
    graph_returns([undated, _event("e1", "2026-03-14")])

    result = await timeline_agent.run(_input())

    assert [e.event_id for e in result.timeline] == ["e1", "u1"]
    assert result.timeline[-1].occurred_on is None


# ── Locking: surfaced, enforced upstream ─────────────────────────────────

async def test_locked_events_are_surfaced(graph_returns):
    graph_returns([_event("e1", "2026-03-14", locked=True), _event("e2", "2026-03-15")])

    result = await timeline_agent.run(_input())

    by_id = {e.event_id: e for e in result.timeline}
    assert by_id["e1"].locked is True
    assert by_id["e2"].locked is False


async def test_timeline_building_never_writes_to_the_graph(graph_returns, monkeypatch):
    """
    Locking is enforced at versioning.write_edge(), the graph's single write
    chokepoint. This sub-agent is read-only, so that guard protects a path it
    does not have — and re-checking here would guard nothing. Asserted rather
    than assumed: any write attempt fails this test.
    """
    async def _explode(*a, **k):
        raise AssertionError("Timeline Building attempted a graph write")

    monkeypatch.setattr("src.graph.versioning.write_edge", _explode, raising=False)
    monkeypatch.setattr("src.graph.conflict_detection.detect_conflicts", _explode, raising=False)

    graph_returns([_event("e1", "2026-03-14", locked=True)])

    result = await timeline_agent.run(_input())
    # PARTIAL, not OK: conflict state is UNKNOWN until a completion marker
    # exists. The point here is that it succeeded without writing.
    assert result.status is SubAgentStatus.PARTIAL
    assert result.timeline


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


async def test_handoff_carries_timeline_events_not_graph_rows(graph_returns):
    graph_returns([_event("e1", "2026-03-14", conflict="contradiction")])

    result = await timeline_agent.run(_input())

    assert result.timeline
    assert not any(isinstance(e, EvidenceChunk) for e in result.timeline)
    assert all(isinstance(c, Citation) for c in result.citations)
    # TimelineEvent exposes findings, not the rows behind them.
    assert set(result.timeline[0].model_dump()) == {
        "event_id", "description", "occurred_on",
        "conflict_state", "conflict_basis", "locked",
    }


async def test_answer_text_does_not_concatenate_event_descriptions(graph_returns):
    """
    The answer must not grow with the CONTENT of the timeline. Citation markers
    do scale with event count — that is the [Document N] scheme working — so
    this asserts the thing that actually matters: no event body is copied into
    the answer.
    """
    verbose = [
        {
            "id": f"e{i}",
            "text": f"Detailed narrative for event {i}. " + ("filler text. " * 40),
            "metadata": {"case_id": "CASE-A", "occurred_on": f"2026-03-{i:02d}"},
        }
        for i in range(1, 12)
    ]
    graph_returns(verbose)

    result = await timeline_agent.run(_input())

    total = sum(len(c["text"]) for c in verbose)
    assert len(result.answer_text) < total / 10
    for chunk in verbose:
        assert chunk["text"][:40] not in result.answer_text


async def test_timeline_is_capped(graph_returns):
    """The supervisor must never receive an unbounded event list."""
    graph_returns([_event(f"e{i}", f"2026-03-{(i % 28) + 1:02d}") for i in range(40)])

    result = await timeline_agent.run(_input())

    assert len(result.timeline) <= 20


# ── Verifier gate ────────────────────────────────────────────────────────

async def test_failing_verifier_abstains(graph_returns):
    graph_returns([_event("e1", "2026-03-14")])

    result = await timeline_agent.run(_input(query=f"{UNGROUNDED_TRIGGER} timeline"))

    assert result.status is SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.citations == []


# ── §2.1.4.1 trace mechanism: tool-emitted suffices ──────────────────────

async def test_relies_on_tool_emitted_events_not_subagent_interpreted(graph_returns):
    """
    One tool, no legs that can collapse into one another — so `tool:graph`
    maps one-to-one onto the only source. Sub-agent-level `timeline:*` events
    would double-report the same transition (§2.2), so their ABSENCE is the
    correct behaviour here, not an omission.
    """
    graph_returns([_event("e1", "2026-03-14")])

    recorder = EventRecorder()
    await timeline_agent.run(_input(), events=recorder)

    steps = [e.step for e in recorder.events]
    assert "tool:graph" in steps, "the tool's own per-source event is missing"
    assert not any(s.startswith("timeline:") for s in steps), (
        "sub-agent-interpreted events would duplicate the tool's own"
    )
    assert f"subagent:{timeline_agent.NAME}" in steps


# ── Automatic tracing ────────────────────────────────────────────────────

async def test_traced_automatically_without_extra_wiring(graph_returns):
    graph_returns([_event("e1", "2026-03-14")])

    supervisor._NODES[timeline_agent.NAME] = timeline_agent.run
    original = supervisor._route
    try:
        supervisor._route = lambda _i: timeline_agent.NAME
        recorder = EventRecorder()
        state = await supervisor.invoke(_input(), events=recorder)
    finally:
        supervisor._route = original
        supervisor._NODES.pop(timeline_agent.NAME, None)

    traced = [e for e in recorder.events if getattr(e, "trace", None)]
    assert len(traced) == 1
    # GRAPH contributed AND is recorded as degraded — the timeline is
    # unverified for conflicts, so it appears in both lists (the documented
    # overlap case). PARTIAL for the same reason.
    assert "GRAPH" in traced[0].trace["tools_used"]
    assert state.result.status is SubAgentStatus.PARTIAL
