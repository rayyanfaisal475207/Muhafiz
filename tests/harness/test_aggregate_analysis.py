"""
Large-Scale Aggregate sub-agent — contract tests.

The test that matters most is the Verifier-rejection path. XAGG's evidence is
machine-computed and correct by construction, so a rejection means the LLM's
PARAPHRASE failed, not the count. Serving a generic abstention would discard a
correct deterministic finding — the same mistake that nearly lost Cross-Case
Linkage's unconfirmed links (789867b), where a non-prose finding was routed
through a prose-only gate.

Production boundaries are mocked; no database, model server, or network.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness import supervisor
from src.pipeline.harness.agents import aggregate_analysis
from src.pipeline.harness.contracts import (
    CallerContext,
    Citation,
    EvidenceChunk,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)
from src.pipeline.harness.events import EventRecorder
from src.pipeline.harness.tools import registry
from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER

STATION_QUERY = "which stations have the most open theft cases"
CATEGORY_QUERY = "how many cases are there by category"
RECURRENCE_QUERY = "top recurring vehicles across all cases"
TIME_QUERY = "how many cases per month across all cases"


@pytest.fixture(autouse=True)
def _real_tools():
    registry.use_real()
    yield
    registry.use_real()


def _supervisor_caller() -> CallerContext:
    return CallerContext(user_id="u2", role=Role.SUPERVISOR, active_case_id="CASE-A")


def _investigator_caller() -> CallerContext:
    return CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id="CASE-A")


def _input(query: str = STATION_QUERY, caller: CallerContext = None) -> SubAgentInput:
    return SubAgentInput(query_text=query, caller=caller or _supervisor_caller())


@pytest.fixture
def cases(gateway):
    """Seed cases so XAGG's relational family has something to group."""
    def _configure(rows=None):
        gateway.cases.clear()
        for row in rows or [
            {"case_id": "C1", "police_station": "Aabpara", "crime_category": "Theft",
             "investigation_status": "Open"},
            {"case_id": "C2", "police_station": "Aabpara", "crime_category": "Theft",
             "investigation_status": "Open"},
            {"case_id": "C3", "police_station": "Margalla", "crime_category": "Fraud",
             "investigation_status": "Open"},
        ]:
            gateway.cases[row["case_id"]] = row
        return gateway

    return _configure


@pytest.fixture
def recurrence(monkeypatch):
    """Drive XAGG's graph-recurrence family."""
    def _configure(rows=None):
        async def _top(entity_type):
            return rows if rows is not None else [
                {"name": "VEH-0091", "case_count": 3, "case_ids": ["C1", "C2", "C3"]},
                {"name": "VEH-0042", "case_count": 2, "case_ids": ["C1", "C2"]},
            ]

        monkeypatch.setattr("src.pipeline.xagg._top_recurring_nodes", _top)

    return _configure


# ── The three grouping shapes XAGG actually supports ─────────────────────

async def test_station_wise_grouping(cases, gateway):
    cases()

    result = await aggregate_analysis.run(_input(STATION_QUERY), gateway=gateway)

    assert result.status is SubAgentStatus.OK
    assert result.tools_used == ["XAGG"]
    assert "Aabpara" in result.answer_text


async def test_category_wise_grouping(cases, gateway):
    cases()

    result = await aggregate_analysis.run(_input(CATEGORY_QUERY), gateway=gateway)

    assert result.status is SubAgentStatus.OK
    assert result.answer_text


async def test_entity_recurrence_grouping(cases, recurrence, gateway):
    cases()
    recurrence()

    result = await aggregate_analysis.run(_input(RECURRENCE_QUERY), gateway=gateway)

    assert result.status is SubAgentStatus.OK
    assert "VEH-0091" in result.answer_text


async def test_time_grouping_is_reported_as_unsupported(cases, gateway):
    """
    XAGG has NO date grouping — `_station_or_category_counts` picks station or
    category and nothing else. Today a "per month" query silently returns
    CATEGORY counts. Serving that as a trend would answer a question nobody
    asked, so the gap is stated instead.
    """
    cases()

    result = await aggregate_analysis.run(_input(TIME_QUERY), gateway=gateway)

    assert result.status is SubAgentStatus.PARTIAL
    assert any("time period is not supported" in c.lower() for c in result.caveats)
    # The figures are still served — they are correct, just not the requested
    # grouping.
    assert result.answer_text


async def test_ordinary_query_carries_no_time_caveat(cases, gateway):
    cases()

    result = await aggregate_analysis.run(_input(STATION_QUERY), gateway=gateway)

    assert not any("time period" in c.lower() for c in result.caveats)


# ── THE VERIFIER-REJECTION PATH — §2.4, and the 789867b lesson ───────────

async def test_verifier_rejection_serves_raw_figures_not_abstention(cases, gateway):
    """
    The single most important behaviour here. A rejected paraphrase must not
    discard a machine-computed count.
    """
    cases()

    result = await aggregate_analysis.run(
        _input(f"{UNGROUNDED_TRIGGER} {STATION_QUERY}"), gateway=gateway
    )

    assert result.status is SubAgentStatus.PARTIAL
    assert result.status is not SubAgentStatus.ABSTAINED
    assert result.answer_text, "the computed figures were discarded"
    assert "Aabpara" in result.answer_text


async def test_verifier_rejection_says_why(cases, gateway):
    """The reader is told the summary was unverified, not left to assume."""
    cases()

    result = await aggregate_analysis.run(
        _input(f"{UNGROUNDED_TRIGGER} {STATION_QUERY}"), gateway=gateway
    )

    assert any("could not be verified" in c.lower() for c in result.caveats)


async def test_verifier_rejection_records_the_degradation(cases, gateway):
    cases()

    result = await aggregate_analysis.run(
        _input(f"{UNGROUNDED_TRIGGER} {STATION_QUERY}"), gateway=gateway
    )

    assert "XAGG" in result.degraded_from


async def test_verifier_rejection_keeps_no_generated_prose(cases, gateway):
    """
    Keep the finding, drop the prose. The rejected synthesized sentence must
    not survive into the served answer.
    """
    cases()

    result = await aggregate_analysis.run(
        _input(f"{UNGROUNDED_TRIGGER} {STATION_QUERY}"), gateway=gateway
    )

    assert "Aggregate computed across the accessible cases" not in result.answer_text


# ── Role gate (no third check here) ──────────────────────────────────────

async def test_denied_is_its_own_status(cases, gateway):
    cases()

    result = await aggregate_analysis.run(
        _input(STATION_QUERY, caller=_investigator_caller()), gateway=gateway
    )

    assert result.status is SubAgentStatus.DENIED
    assert result.status is not SubAgentStatus.ABSTAINED
    assert result.status is not SubAgentStatus.EMPTY
    assert result.answer_text is None


async def test_denial_audited_once_by_the_tool(cases, gateway):
    """One record, from the tool's own gate — no duplicate from a third check."""
    cases()

    await aggregate_analysis.run(
        _input(STATION_QUERY, caller=_investigator_caller()), gateway=gateway
    )

    violations = [e for e in gateway.audit_log if e["event_type"] == "authorization_violation"]
    assert len(violations) == 1
    assert violations[0]["details"]["route"] == "XAGG"


def test_subagent_does_not_check_roles_itself():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(aggregate_analysis))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert "CROSS_CASE_ROLES" not in names


# ── Bounded handoff ──────────────────────────────────────────────────────

async def test_top_n_is_bounded_with_the_total_stated(cases, gateway):
    """
    [PRESERVE §3] Truncation must never read as the complete set — the total
    is what stops a top-10 list being mistaken for all of it.
    """
    cases([
        {"case_id": f"C{i}", "police_station": f"Station-{i}",
         "crime_category": "Theft", "investigation_status": "Open"}
        for i in range(25)
    ])

    result = await aggregate_analysis.run(_input(STATION_QUERY), gateway=gateway)

    body = result.answer_text
    group_lines = [ln for ln in body.splitlines() if ln.strip().startswith("-")]
    assert len(group_lines) <= 10
    assert "in total" in body


async def test_subagent_result_has_no_field_that_can_hold_evidence():
    offenders = [
        name for name, field in SubAgentResult.model_fields.items()
        if "EvidenceChunk" in repr(field.annotation)
    ]
    assert not offenders


async def test_handoff_carries_citations_not_chunks(cases, gateway):
    cases()

    result = await aggregate_analysis.run(_input(STATION_QUERY), gateway=gateway)

    assert all(isinstance(c, Citation) for c in result.citations)
    assert not any(isinstance(c, EvidenceChunk) for c in result.citations)


# ── Empty ────────────────────────────────────────────────────────────────

async def test_no_matching_cases_is_empty_not_an_error(cases, gateway):
    cases([])

    result = await aggregate_analysis.run(_input(STATION_QUERY), gateway=gateway)

    assert result.status in (SubAgentStatus.EMPTY, SubAgentStatus.OK)
    assert result.status is not SubAgentStatus.ABSTAINED


# ── §2.1.4.1: single tool, tool-emitted events suffice ───────────────────

async def test_relies_on_tool_emitted_events(cases, gateway):
    """Trivially the Case Summarization category — one leg, nothing to collapse."""
    cases()

    recorder = EventRecorder()
    await aggregate_analysis.run(_input(STATION_QUERY), events=recorder, gateway=gateway)

    steps = [e.step for e in recorder.events]
    assert "tool:xagg" in steps
    assert not any(s.startswith("aggregate:") for s in steps)


# ── Automatic tracing ────────────────────────────────────────────────────

async def test_traced_automatically_without_extra_wiring(cases, gateway):
    """
    The supervisor invokes nodes generically as `node(agent_input, recorder)`
    and does not thread a gateway, so the node registered here binds the test
    gateway — mirroring how a real deployment would resolve it internally.
    What is being asserted is that registration alone produces a trace.
    """
    cases()

    async def _bound(agent_input, events=None):
        return await aggregate_analysis.run(agent_input, events=events, gateway=gateway)

    supervisor._NODES[aggregate_analysis.NAME] = _bound
    original = supervisor._route
    try:
        supervisor._route = lambda _i: aggregate_analysis.NAME
        recorder = EventRecorder()
        state = await supervisor.invoke(_input(STATION_QUERY), events=recorder)
    finally:
        supervisor._route = original
        supervisor._NODES.pop(aggregate_analysis.NAME, None)

    traced = [e for e in recorder.events if getattr(e, "trace", None)]
    assert len(traced) == 1
    assert "XAGG" in traced[0].trace["tools_used"]
    assert state.result.status in (SubAgentStatus.OK, SubAgentStatus.PARTIAL)
