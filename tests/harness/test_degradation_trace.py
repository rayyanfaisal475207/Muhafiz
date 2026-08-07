"""
Per-query degradation trace — the shared write path.

The requirement this guards: every sub-agent's degradation detail must reach
`pipeline_steps.output_summary` in ONE consistent shape, written from ONE place,
so no sub-agent author has to remember to wire it up and no future sub-agent can
be silently untraced.

The tests below therefore assert the MECHANISM (single write point, uniform
shape, triggered whichever node ran), not just that a payload exists.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness import supervisor
from src.pipeline.harness.agents import case_summary, semantic_search
from src.pipeline.harness.contracts import (
    CallerContext,
    GeneratedFileRef,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)
from src.pipeline.harness.events import (
    TRACE_PAYLOAD_VERSION,
    EventRecorder,
    build_degradation_trace,
)


def _caller() -> CallerContext:
    return CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id="CASE-A")


def _input(query: str = "what happened") -> SubAgentInput:
    return SubAgentInput(query_text=query, caller=_caller())


def _trace_events(recorder: EventRecorder) -> list:
    return [e for e in recorder.events if getattr(e, "trace", None)]


# ── Payload shape ────────────────────────────────────────────────────────

def test_trace_splits_tools_three_ways():
    """
    The overlap case: a tool in BOTH lists contributed data but degraded
    internally. It must appear in `degraded_and_contributed` and in NEITHER of
    the exclusive buckets — folding it into either would misreport the
    evidence base.
    """
    result = SubAgentResult(
        status=SubAgentStatus.PARTIAL,
        answer_text="x",
        tools_used=["RAG", "GRAPH"],
        degraded_from=["RAG", "SQL"],
    )

    trace = build_degradation_trace(result)

    assert trace["degraded_and_contributed"] == ["RAG"]
    assert trace["contributed_only"] == ["GRAPH"]
    assert trace["degraded_only"] == ["SQL"]


def test_trace_preserves_raw_contract_fields():
    """A reader wanting the originals must not have to reconstruct them."""
    result = SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="x",
        tools_used=["RAG"], degraded_from=["GRAPH", "SQL"],
    )

    trace = build_degradation_trace(result)

    assert trace["tools_used"] == ["RAG"]
    assert trace["degraded_from"] == ["GRAPH", "SQL"]


def test_trace_handles_investigative_analysis_collapse_shape():
    """
    [RESOLVED-4] The hardest known case ahead: three tools attempted, GRAPH and
    SQL both fall back to RAG, so one effective source remains. The payload must
    keep "attempted" and "paid off" separable without a shape change when that
    sub-agent lands.
    """
    result = SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="x",
        tools_used=["RAG"], degraded_from=["GRAPH", "SQL"],
    )

    trace = build_degradation_trace(result)

    assert trace["contributed_only"] == ["RAG"]
    assert trace["degraded_only"] == ["GRAPH", "SQL"]
    assert trace["degraded_and_contributed"] == []


def test_trace_carries_caveats_and_status_and_version():
    result = SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="x",
        tools_used=["GRAPH"], degraded_from=["RAG"],
        caveats=["relevance check unavailable"],
    )

    trace = build_degradation_trace(result)

    assert trace["caveats"] == ["relevance check unavailable"]
    assert trace["sub_agent_status"] == "partial"
    assert trace["v"] == TRACE_PAYLOAD_VERSION


def test_disclosure_rendered_is_none_without_a_generated_file():
    """None means "no file produced" — distinct from False, "file produced, undisclosed"."""
    plain = build_degradation_trace(
        SubAgentResult(status=SubAgentStatus.OK, answer_text="x")
    )
    assert plain["disclosure_rendered"] is None

    with_file = build_degradation_trace(
        SubAgentResult(
            status=SubAgentStatus.PARTIAL, answer_text="x",
            generated_file=GeneratedFileRef(
                file_id="f1", file_name="r.pdf", storage_path="/r.pdf",
                disclosure_rendered=True,
            ),
        )
    )
    assert with_file["disclosure_rendered"] is True


# ── Single write point, uniform across sub-agents ────────────────────────

async def test_supervisor_emits_exactly_one_trace_per_run():
    recorder = EventRecorder()
    await supervisor.invoke(_input(), events=recorder)

    traced = _trace_events(recorder)
    assert len(traced) == 1, "the trace must be written once per sub-agent completion"
    assert traced[0].step == "supervisor:complete"


async def test_trace_rides_the_existing_completion_event():
    """
    §2.2: one event per meaningful transition. The trace attaches to the
    existing completion event rather than adding a second one, which would
    double-count the same transition.
    """
    recorder = EventRecorder()
    await supervisor.invoke(_input(), events=recorder)

    completions = [e for e in recorder.events if e.step == "supervisor:complete"]
    assert len(completions) == 1
    assert getattr(completions[0], "trace", None) is not None


@pytest.mark.parametrize("agent_name", [semantic_search.NAME, case_summary.NAME])
async def test_every_registered_sub_agent_is_traced(agent_name, monkeypatch):
    """
    The mechanism, not one instance: whichever node the supervisor dispatches
    to gets traced, because the write reads the generic `SubAgentResult` the
    supervisor already holds. This is what makes a future sub-agent traced by
    construction rather than by its author remembering.
    """
    monkeypatch.setattr(supervisor, "_route", lambda _i: agent_name)

    recorder = EventRecorder()
    await supervisor.invoke(_input(), events=recorder)

    traced = _trace_events(recorder)
    assert len(traced) == 1
    assert set(traced[0].trace) >= {
        "v", "sub_agent_status", "tools_used", "degraded_from",
        "contributed_only", "degraded_and_contributed", "degraded_only",
        "caveats", "disclosure_rendered",
    }


async def test_a_newly_registered_sub_agent_is_traced_without_touching_it():
    """
    Simulates Investigative Analysis landing later: register a node, change
    nothing else, and it is traced. If this ever fails, the write path has
    stopped being generic.
    """
    async def brand_new_sub_agent(agent_input, events=None):
        return SubAgentResult(
            status=SubAgentStatus.PARTIAL, answer_text="synthesized",
            tools_used=["RAG"], degraded_from=["GRAPH", "SQL"],
            caveats=["two sources fell back"],
        )

    supervisor._NODES["brand_new"] = brand_new_sub_agent
    try:
        original = supervisor._route
        supervisor._route = lambda _i: "brand_new"
        recorder = EventRecorder()
        await supervisor.invoke(_input(), events=recorder)
    finally:
        supervisor._route = original
        supervisor._NODES.pop("brand_new", None)

    traced = _trace_events(recorder)
    assert len(traced) == 1
    assert traced[0].trace["contributed_only"] == ["RAG"]
    assert traced[0].trace["degraded_only"] == ["GRAPH", "SQL"]
    assert traced[0].trace["caveats"] == ["two sources fell back"]


async def test_unroutable_query_is_still_traced():
    """"Nothing ran" is an outcome worth recording, not a hole in the history."""
    original = supervisor._route
    supervisor._route = lambda _i: "no_such_agent"
    try:
        recorder = EventRecorder()
        state = await supervisor.invoke(_input(), events=recorder)
    finally:
        supervisor._route = original

    assert state.selected_agent == supervisor.UNROUTABLE
    traced = _trace_events(recorder)
    assert len(traced) == 1
    assert traced[0].trace["sub_agent_status"] == "abstained"


# ── Durable write ────────────────────────────────────────────────────────

async def test_trace_reaches_pipeline_steps_output_summary(gateway):
    recorder = EventRecorder(run_id="run-1", gateway=gateway)
    await supervisor.invoke(_input(), events=recorder)

    assert gateway.steps, "nothing persisted"


async def test_persisted_summary_keeps_detail_alongside_trace(monkeypatch, gateway):
    """
    `detail` is the pre-existing shape. The trace is added ALONGSIDE it, never
    instead — existing readers must keep finding `detail` where it was.
    """
    captured: list[dict] = []

    async def _log_step(run_id, step_name, step_order, status,
                        duration_ms=None, input_summary=None, output_summary=None):
        captured.append({"step_name": step_name, "output_summary": output_summary})

    monkeypatch.setattr(gateway, "log_step", _log_step)

    recorder = EventRecorder(run_id="run-1", gateway=gateway)
    await supervisor.invoke(_input(), events=recorder)

    completion = [c for c in captured if c["step_name"] == "supervisor:complete"]
    assert completion, "completion step was not persisted"
    summary = completion[0]["output_summary"]
    assert "detail" in summary, "the pre-existing detail field was dropped"
    assert "trace" in summary, "the structured trace never reached output_summary"
    assert summary["trace"]["v"] == TRACE_PAYLOAD_VERSION


async def test_steps_without_a_trace_persist_unchanged(monkeypatch, gateway):
    """Tool-level steps carry no trace; their payload shape must not change."""
    captured: list[dict] = []

    async def _log_step(run_id, step_name, step_order, status,
                        duration_ms=None, input_summary=None, output_summary=None):
        captured.append({"step_name": step_name, "output_summary": output_summary})

    monkeypatch.setattr(gateway, "log_step", _log_step)

    recorder = EventRecorder(run_id="run-1", gateway=gateway)
    await supervisor.invoke(_input(), events=recorder)

    tool_steps = [c for c in captured if c["step_name"].startswith("tool:")]
    assert tool_steps, "no tool steps persisted"
    assert all("trace" not in c["output_summary"] for c in tool_steps)
    assert all("detail" in c["output_summary"] for c in tool_steps)
