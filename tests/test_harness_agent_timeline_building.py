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


def _dated_row(entity_id, description, occurred_on, locked=False, occ_id=None, event_type=None, detail=None):
    """
    `occ_id` (M10, docs/decisions/0001-muhafiz-api-migration.md) stands
    in for the real `id(occ)` AGE returns per OCCURRED_ON edge — defaults
    to `entity_id` itself, which is unique across every existing
    single-event-per-incident test row and keeps their event_id
    assertions predictable (f"{entity_id}::{entity_id}"). A test that
    wants to model one Incident with SEVERAL live events (M6a's real
    shape) passes distinct `occ_id`s explicitly.
    """
    return {
        "entity_id": entity_id,
        "description": description,
        "occurred_on": occurred_on,
        "locked": locked,
        "occ_id": occ_id if occ_id is not None else entity_id,
        "event_type": event_type,
        "detail": detail,
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


class _FakeGateway:
    """[Reconciliation fix — harness-reconciliation Unit 6] Minimal stand-in
    for DataGateway.get_case(), backing _conflict_detection_confirmed()'s
    read of cases.conflicts_checked_at. `checked=True` simulates a case
    where the background conflict-detection task has already completed;
    `checked=False` simulates one where it hasn't (or the marker column
    genuinely reads NULL)."""

    def __init__(self, checked: bool):
        self._checked = checked

    async def get_case(self, case_id: str):
        return {"case_id": case_id, "conflicts_checked_at": "2026-08-11T00:00:00Z" if self._checked else None}


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

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert result.status == SubAgentStatus.OK
    assert result.tools_used == ["GRAPH"]
    assert result.degraded_from == []
    assert len(result.events) == 3
    # Chronological order, not insertion order. event_id is now
    # f"{entity_id}::{occ_id}" (M10) — see _dated_row's own docstring.
    assert [e.event_id for e in result.events] == [
        "INCIDENT-001::INCIDENT-001", "INCIDENT-002::INCIDENT-002", "INCIDENT-003::INCIDENT-003",
    ]

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
# (a.1) [Reconciliation fix — Unit 6] detection NOT confirmed -> UNKNOWN,
# never NONE, even though the live CONFLICTS_WITH query succeeded cleanly
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_unconfirmed_detection_yields_unknown_not_none(monkeypatch):
    """The exact bug this reconciliation step fixes: a live, successful
    CONFLICTS_WITH query with zero matching edges used to be trusted as
    proof detection ran ("checked, no conflict found"). It is not -- the
    same empty result occurs when detection never ran or is still in
    flight (see cases.conflicts_checked_at's own migration comment). With
    no completion marker, an absent basis must render UNKNOWN, not NONE."""
    _stub_scoped_cypher(
        monkeypatch,
        dated_rows=[_dated_row("INCIDENT-001", "Only incident", "2026-01-01")],
        conflict_rows=[],  # query succeeds, finds nothing
    )

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=False))

    assert result.status == SubAgentStatus.PARTIAL
    assert result.events[0].conflict_state == ConflictState.UNKNOWN
    assert any("not been confirmed" in c.lower() or "unchecked" in c.lower() for c in result.caveats)


# ═══════════════════════════════════════════════════════════════════════
# (a2) M10, Muhafiz Data API migration — one Incident, SEVERAL live
# OCCURRED_ON edges (M6a's real shape), must become SEVERAL events, not
# one arbitrarily-picked row.
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_one_incident_multiple_occurred_on_edges_become_multiple_events(monkeypatch):
    """
    Regression: before M10, this function de-duplicated on entity_id
    alone, so an Incident with a real incident date PLUS several zimni
    entries PLUS a dispatch date (exactly what structured_projection.py
    now writes) would collapse to a single, arbitrarily-chosen timeline
    entry — silently discarding most of a real case's timeline.
    """
    _stub_scoped_cypher(
        monkeypatch,
        dated_rows=[
            _dated_row("INCIDENT-001", "Incident for FIR 100/26", "2026-08-18",
                       occ_id=1, event_type="incident"),
            _dated_row("INCIDENT-001", "Incident for FIR 100/26", "2026-08-19",
                       occ_id=2, event_type="zimni_entry", detail="entry 1"),
            _dated_row("INCIDENT-001", "Incident for FIR 100/26", "2026-08-20",
                       occ_id=3, event_type="chalaan_dispatch"),
        ],
    )

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert result.status == SubAgentStatus.OK
    assert len(result.events) == 3, "one incident with 3 live events must become 3 timeline entries"

    event_ids = {e.event_id for e in result.events}
    assert len(event_ids) == 3, "event_ids must be distinct per event, not collide on the shared entity_id"

    descriptions = {e.description for e in result.events}
    assert any("zimni_entry: entry 1" in d for d in descriptions)
    assert any("chalaan_dispatch" in d for d in descriptions)
    # [findings.md TB-1] The bare `event_type` event no longer carries the
    # shared Incident narrative — that text is now served once via
    # answer_text (see the TB-1 tests below). Previously this asserted
    # `d == "Incident for FIR 100/26 — incident"`; the narrative half of
    # that string was the duplication TB-1 removes.
    assert any(d == "incident" for d in descriptions)
    assert not any("Incident for FIR 100/26" in d for d in descriptions), (
        "the shared narrative must not be repeated inside per-event descriptions"
    )

    # Chronological order still holds across events from the SAME incident.
    assert [e.occurred_on for e in result.events] == ["2026-08-18", "2026-08-19", "2026-08-20"]


@pytest.mark.asyncio
async def test_legacy_row_with_no_event_type_keeps_bare_description(monkeypatch):
    """A pre-M6a, LLM-derived incident (no event_type/detail properties)
    must render exactly as it always did — no dangling '— None' suffix."""
    _stub_scoped_cypher(monkeypatch, dated_rows=[_dated_row("INCIDENT-005", "Legacy incident", "2026-01-01")])

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert result.events[0].description == "Legacy incident"


# ═══════════════════════════════════════════════════════════════════════
# findings.md TB-1 — the Incident narrative is served ONCE, not per event
# ═══════════════════════════════════════════════════════════════════════

# The real shape that exposed TB-1: one Incident, several dated
# OCCURRED_ON edges, all sharing the same (long) Incident.description.
# Measured on the live corpus: avg 5.9 events per incident, narrative avg
# 1,347 chars -> ~5.9x duplication, 17,676 chars in the worst timeline.
_TB1_NARRATIVE = (
    "میں، عثمان خالد ملک، اسلام آباد کا رہائشی ہوں۔ "
    "دو اپریل کو میرے گھر میں چوری ہوئی۔ " * 8
).strip()


def _tb1_rows():
    return [
        _dated_row("INCIDENT-097", _TB1_NARRATIVE, "2026-03-11", occ_id=1, event_type="incident"),
        _dated_row("INCIDENT-097", _TB1_NARRATIVE, "2026-03-12",
                   occ_id=2, event_type="zimni_entry", detail="entry 1"),
        _dated_row("INCIDENT-097", _TB1_NARRATIVE, "2026-03-13",
                   occ_id=3, event_type="zimni_entry", detail="entry 2"),
        # `chalaan_dispatch`, not `position` — TB-2 gives a detail-less
        # `position` an explicit placeholder, so it is no longer a valid
        # example of a bare event_type. `chalaan_dispatch` genuinely
        # carries no detail (15 such events live) and still renders bare.
        _dated_row("INCIDENT-097", _TB1_NARRATIVE, "2026-03-14",
                   occ_id=4, event_type="chalaan_dispatch"),
    ]


@pytest.mark.asyncio
async def test_narrative_is_not_repeated_once_per_event(monkeypatch):
    """(1) The defining TB-1 assertion."""
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb1_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert len(result.events) == 4
    for e in result.events:
        assert _TB1_NARRATIVE not in e.description, (
            "the shared Incident narrative must not appear inside a per-event description"
        )


@pytest.mark.asyncio
async def test_narrative_appears_exactly_once_in_the_answer(monkeypatch):
    """(2) Removed from the events, but NOT lost — served once, in full."""
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb1_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert result.answer_text is not None
    assert result.answer_text.count(_TB1_NARRATIVE) == 1, (
        "the narrative must be served exactly once — not zero times, not per event"
    )


@pytest.mark.asyncio
async def test_narrative_is_served_verbatim_never_truncated(monkeypatch):
    """(2b) TB-1 removes duplication only — it must not shorten the text."""
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb1_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert _TB1_NARRATIVE in result.answer_text
    assert len(_TB1_NARRATIVE) > 500, "fixture must be long enough to make truncation detectable"


@pytest.mark.asyncio
async def test_event_type_and_detail_are_preserved_per_event(monkeypatch):
    """(3) + (4) The distinguishing information survives."""
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb1_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    descriptions = [e.description for e in result.events]
    assert "zimni_entry: entry 1" in descriptions
    assert "zimni_entry: entry 2" in descriptions


@pytest.mark.asyncio
async def test_event_without_detail_still_renders_its_event_type(monkeypatch):
    """(5) A typed event with no detail keeps its type, with no dangling ': '."""
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb1_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    descriptions = [e.description for e in result.events]
    assert "incident" in descriptions
    assert "chalaan_dispatch" in descriptions
    assert not any(d.endswith(":") or d.endswith(": ") for d in descriptions)


@pytest.mark.asyncio
async def test_event_ordering_is_unchanged_by_tb1(monkeypatch):
    """(6) TB-1 is a rendering change; chronology must be untouched."""
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb1_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert [e.occurred_on for e in result.events] == [
        "2026-03-11", "2026-03-12", "2026-03-13", "2026-03-14",
    ]


@pytest.mark.asyncio
async def test_answer_text_still_carries_the_conflict_summary(monkeypatch):
    """
    (7) The narrative preamble must not displace the existing summary —
    particularly the unchecked-conflict disclosure, which is the guard
    that stops a false all-clear (RESOLVED-5 / Unit 6).
    """
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb1_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=False))

    assert "4 dated event(s)" in result.answer_text
    assert "conflict status is unknown for every event" in result.answer_text
    assert result.answer_text.count(_TB1_NARRATIVE) == 1


@pytest.mark.asyncio
async def test_legacy_untyped_row_narrative_is_not_double_served(monkeypatch):
    """
    An untyped legacy row keeps its narrative in its own description (see
    test_legacy_row_with_no_event_type_keeps_bare_description). It must
    therefore NOT also be prepended to answer_text, or the very
    duplication TB-1 removes would reappear by the other route.
    """
    _stub_scoped_cypher(
        monkeypatch, dated_rows=[_dated_row("INCIDENT-005", "Legacy incident", "2026-01-01")],
    )

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert result.events[0].description == "Legacy incident"
    # TB-1's contract is "served exactly once", not "never shown". This
    # previously asserted zero occurrences only because answer_text carried no
    # event list at all — the gap that made Timeline Building answer "your
    # timeline has N events" without ever showing them (verify-log Finding Z).
    # The row is now rendered, so the narrative appears once (in its own row)
    # and is NOT also prepended above it.
    assert result.answer_text.count("Legacy incident") == 1


@pytest.mark.asyncio
async def test_two_incidents_with_identical_narrative_serve_it_once(monkeypatch):
    """De-duplication is on the narrative text, not on entity_id."""
    _stub_scoped_cypher(
        monkeypatch,
        dated_rows=[
            _dated_row("INCIDENT-A", "Shared narrative text", "2026-01-01",
                       occ_id=1, event_type="incident"),
            _dated_row("INCIDENT-B", "Shared narrative text", "2026-01-02",
                       occ_id=2, event_type="incident"),
        ],
    )

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert result.answer_text.count("Shared narrative text") == 1


# ═══════════════════════════════════════════════════════════════════════
# findings.md TB-2 — a blank `position` renders an explicit absence
# ═══════════════════════════════════════════════════════════════════════

# Real shape: 53/73 FIRs carry at least one fir_position row whose
# `position` text is blank while its `status_date` is real. Measured on the
# live corpus, all 65 such rows have null in EVERY other field, so the date
# is the only recorded fact — suppressing them would erase 24 dates that
# exist nowhere else in the timeline.
def _tb2_rows():
    return [
        _dated_row("INCIDENT-1001", _TB1_NARRATIVE, "2024-09-25", occ_id=1, event_type="incident"),
        _dated_row("INCIDENT-1001", _TB1_NARRATIVE, "2024-09-26",
                   occ_id=2, event_type="zimni_entry", detail="entry 1"),
        _dated_row("INCIDENT-1001", _TB1_NARRATIVE, "2025-02-13",
                   occ_id=3, event_type="position", detail=""),
        _dated_row("INCIDENT-1001", _TB1_NARRATIVE, "2026-01-29",
                   occ_id=4, event_type="position", detail=""),
        _dated_row("INCIDENT-1001", _TB1_NARRATIVE, "2026-02-01",
                   occ_id=5, event_type="position", detail="ملزم گرفتار"),
    ]


@pytest.mark.asyncio
async def test_blank_position_event_is_still_present_and_dated(monkeypatch):
    """(1) + (2) The event is never hidden and never loses its date."""
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb2_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert len(result.events) == 5, "a blank position event must not be suppressed"
    dates = [e.occurred_on for e in result.events]
    assert "2025-02-13" in dates and "2026-01-29" in dates, (
        "dates carried only by a blank position row must survive"
    )


@pytest.mark.asyncio
async def test_blank_position_renders_explicit_absence_not_bare_type(monkeypatch):
    """(3) + (4) The defining TB-2 assertion."""
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb2_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    blanks = [e for e in result.events if e.occurred_on in ("2025-02-13", "2026-01-29")]
    assert len(blanks) == 2
    for e in blanks:
        assert e.description == "position: no position recorded"
        assert e.description != "position", "must not render as a bare event type"


@pytest.mark.asyncio
async def test_non_empty_position_detail_is_unchanged(monkeypatch):
    """(5) A recorded position is preserved verbatim — including Urdu (TB-3 untouched)."""
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb2_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    e = next(e for e in result.events if e.occurred_on == "2026-02-01")
    assert e.description == "position: ملزم گرفتار"


@pytest.mark.asyncio
async def test_other_detail_less_event_types_are_unchanged(monkeypatch):
    """
    (6) TB-2 is scoped to `position` ONLY. 341 events already render with no
    detail (incident 64 / arrest 74 / chalaan_dispatch 15 / zimni_entry 188);
    for those, an absent detail is unremarkable and must stay bare.
    """
    _stub_scoped_cypher(
        monkeypatch,
        dated_rows=[
            _dated_row("INCIDENT-2", "N", "2026-01-01", occ_id=1, event_type="incident"),
            _dated_row("INCIDENT-2", "N", "2026-01-02", occ_id=2, event_type="arrest"),
            _dated_row("INCIDENT-2", "N", "2026-01-03", occ_id=3, event_type="chalaan_dispatch"),
            _dated_row("INCIDENT-2", "N", "2026-01-04", occ_id=4, event_type="zimni_entry"),
        ],
    )

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    descriptions = [e.description for e in result.events]
    assert descriptions == ["incident", "arrest", "chalaan_dispatch", "zimni_entry"]
    assert not any("no position recorded" in d for d in descriptions)


@pytest.mark.asyncio
async def test_tb1_behaviour_intact_alongside_tb2(monkeypatch):
    """(7) The narrative is still served once, and still not inside events."""
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb2_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert result.answer_text.count(_TB1_NARRATIVE) == 1
    for e in result.events:
        assert _TB1_NARRATIVE not in e.description


@pytest.mark.asyncio
async def test_tb2_preserves_event_ordering(monkeypatch):
    """(8) Chronology is untouched by the placeholder."""
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb2_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert [e.occurred_on for e in result.events] == [
        "2024-09-25", "2024-09-26", "2025-02-13", "2026-01-29", "2026-02-01",
    ]


@pytest.mark.asyncio
async def test_multiple_blank_position_events_stay_separate(monkeypatch):
    """
    (9) Two blank position events now render identical TEXT, so they must
    not be collapsed — they are distinct dated reviews.
    """
    _stub_scoped_cypher(monkeypatch, dated_rows=_tb2_rows())

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    blanks = [e for e in result.events if e.description == "position: no position recorded"]
    assert len(blanks) == 2, "identical rendered text must not deduplicate distinct dated events"
    assert {e.occurred_on for e in blanks} == {"2025-02-13", "2026-01-29"}
    assert len({e.event_id for e in blanks}) == 2, "event_ids must stay distinct"


@pytest.mark.asyncio
async def test_tb2_does_not_touch_legacy_untyped_rows(monkeypatch):
    """(10) A pre-M6a row with no event_type keeps its bare description."""
    _stub_scoped_cypher(
        monkeypatch, dated_rows=[_dated_row("INCIDENT-005", "Legacy incident", "2026-01-01")],
    )

    result = await timeline_building(_agent_input(), gateway=_FakeGateway(checked=True))

    assert result.events[0].description == "Legacy incident"


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
        supervisor_mod, "classify_to_subagent", lambda route_result, query_text="", **kwargs: TIMELINE_BUILDING
    )

    _stub_scoped_cypher(
        monkeypatch,
        dated_rows=[_dated_row("INCIDENT-001", "Only incident", "2026-01-01")],
        conflict_rows=[],
    )

    sup = Supervisor()  # no override -> real module-level registry
    result = await sup.handle(_agent_input(), gateway=_FakeGateway(checked=True))

    assert result.status == SubAgentStatus.OK
    assert len(result.events) == 1
    assert result.events[0].conflict_state == ConflictState.NONE
    assert result.tools_used == ["GRAPH"]


# ── Finding Z regression: the timeline itself must be rendered ───────────────
# This sub-agent answers "build a chronological timeline", but `answer_text`
# contained only the summary sentence ("This case's timeline has 9 dated
# event(s)..."). The events went out solely in the structured payload, which
# nothing renders, so the deliverable never reached the user (verify-log
# Finding Z). Rendering stays deterministic — plain formatting, no model call.

from src.pipeline.harness.agents.timeline_building import _answer_text as _tb_answer_text
from src.pipeline.harness.types import ConflictState as _CS, TimelineEvent as _TE


def _ev(eid, desc, on=None, state=_CS.NONE, basis=None):
    return _TE(event_id=eid, description=desc, occurred_on=on,
               conflict_state=state, conflict_basis=basis)


def test_events_are_rendered_not_just_counted():
    text = _tb_answer_text(
        [_ev("1", "FIR registered", "2024-09-17"), _ev("2", "Weapon seized", "2024-09-22")],
        conflict_checked=True,
    )
    assert "FIR registered" in text
    assert "Weapon seized" in text
    assert "2024-09-17" in text and "2024-09-22" in text
    # The summary line is still there.
    assert "2 dated event(s)" in text


def test_unknown_conflict_state_is_visually_distinct_from_none():
    """[PRESERVE] An unchecked event must never read as a verified all-clear."""
    text = _tb_answer_text(
        [_ev("1", "Checked event", "2024-09-17", _CS.NONE),
         _ev("2", "Unchecked event", "2024-09-18", _CS.UNKNOWN)],
        conflict_checked=False,
    )
    unchecked_line = next(ln for ln in text.splitlines() if "Unchecked event" in ln)
    checked_line = next(ln for ln in text.splitlines() if "Checked event" in ln)
    assert "not checked" in unchecked_line
    assert "not checked" not in checked_line


def test_conflicting_event_surfaces_its_basis():
    text = _tb_answer_text(
        [_ev("1", "Disputed event", "2024-09-18", _CS.CONFLICT, "two records disagree")],
        conflict_checked=True,
    )
    assert "conflicting record" in text
    assert "two records disagree" in text


def test_undated_event_is_labelled_not_dropped():
    text = _tb_answer_text([_ev("1", "Undated action", None)], conflict_checked=True)
    assert "Undated action" in text
    assert "undated" in text


def test_no_timeline_header_when_there_are_no_events():
    text = _tb_answer_text([], conflict_checked=True)
    assert "**Timeline**" not in text
    assert "0 dated event(s)" in text


def test_prepended_narrative_is_not_repeated_in_its_own_row():
    """
    [findings.md TB-1] When a narrative is prepended to answer_text, its own
    timeline row must point back to it rather than restate it — otherwise
    rendering the event list (verify-log Finding Z) reintroduces exactly the
    duplication TB-1 removed. The event must still be LISTED; dropping it
    would hide an event from the timeline.
    """
    narrative = "A long incident narrative that is served once."
    text = _tb_answer_text(
        [_ev("1", narrative, "2026-01-01")],
        conflict_checked=True,
        narratives=[narrative],
    )
    assert text.count(narrative) == 1
    assert "2026-01-01" in text  # the event is still listed
