"""
Supervisor routing and the sub-agent handoff boundary.

These tests assert STRUCTURAL properties, not example values: the point is that
a future refactor cannot quietly leak evidence upward or drop trace events and
still pass.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness import supervisor
from src.pipeline.harness.agents import semantic_search
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
from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER


def _input(query: str = "What happened at the G-8/4 premises?", **kwargs) -> SubAgentInput:
    return SubAgentInput(
        query_text=query,
        caller=CallerContext(
            user_id="user-1", role=Role.INVESTIGATOR, active_case_id="CASE-A1B2C3D4"
        ),
        **kwargs,
    )


# ── Routing ──────────────────────────────────────────────────────────────

async def test_routing_reaches_semantic_search():
    state = await supervisor.invoke(_input())

    assert state.selected_agent == semantic_search.NAME
    assert state.result is not None
    assert state.result.status is SubAgentStatus.OK
    assert state.result.answer_text


async def test_supervisor_returns_terminal_state_with_events():
    state = await supervisor.invoke(_input())

    assert state.events, "supervisor must return the accumulated trace"
    assert state.agent_input.query_text == _input().query_text


# ── The handoff boundary — asserted structurally ─────────────────────────

def test_subagent_result_has_no_field_that_can_hold_evidence():
    """
    The strongest form of the §3 guarantee: `SubAgentResult` must have no field
    capable of carrying an EvidenceChunk, so leaking one is a type error rather
    than a code-review catch.

    Walks the model's declared annotations rather than one instance, so adding
    a chunk-typed field later fails here even if no test data exercises it.
    """
    offenders = []
    for field_name, field in SubAgentResult.model_fields.items():
        annotation = repr(field.annotation)
        if "EvidenceChunk" in annotation:
            offenders.append(field_name)

    assert not offenders, (
        f"SubAgentResult fields {offenders} can hold EvidenceChunk objects. "
        "Design §3: the bounded payload must never carry raw evidence upward."
    )


async def test_handoff_payload_carries_citations_not_chunks():
    state = await supervisor.invoke(_input())
    result = state.result

    assert result.citations, "an OK result must carry citations"
    assert all(isinstance(c, Citation) for c in result.citations)
    assert not any(isinstance(c, EvidenceChunk) for c in result.citations)


async def test_citations_carry_no_chunk_text():
    """
    Citations are provenance only. If chunk text ever rides along on a
    Citation, the bounding is defeated even though the type is nominally right.
    """
    state = await supervisor.invoke(_input())

    from src.pipeline.harness.tools import _fixtures

    bodies = [c.text for c in _fixtures.rag_chunks()]
    for citation in state.result.citations:
        serialized = citation.model_dump_json()
        for body in bodies:
            assert body[:80] not in serialized, (
                "Citation leaked chunk text into the handoff payload."
            )


async def test_answer_text_is_bounded_despite_verbose_retrieval():
    """
    The stub RAG tool returns deliberately long, near-duplicate passages. The
    answer handed upward must not grow with them — a naive concatenating
    generator would fail this.
    """
    from src.pipeline.harness.tools import _fixtures

    retrieved_size = sum(len(c.text) for c in _fixtures.rag_chunks())
    state = await supervisor.invoke(_input())

    assert len(state.result.answer_text) < retrieved_size / 2, (
        "answer_text scales with retrieval size — summarization is not bounding."
    )


async def test_citation_count_is_capped_below_retrieved_set():
    from src.pipeline.harness.tools import _fixtures

    state = await supervisor.invoke(_input())

    assert 0 < len(state.result.citations) < len(_fixtures.rag_chunks()), (
        "citations must be a bounded top-N, not the full reranked set"
    )


# ── Verifier gate ────────────────────────────────────────────────────────

async def test_failing_verifier_triggers_abstention():
    """
    A rejected answer is never served — the ungrounded draft must not appear in
    the payload in any form.
    """
    state = await supervisor.invoke(_input(query=f"tell me {UNGROUNDED_TRIGGER} things"))
    result = state.result

    assert result.status is SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.citations == []
    assert result.tools_used == []


async def test_abstention_records_the_attempted_tool_in_degraded_from():
    """[RESOLVED-4] tools_used is post-fallback contributors; a failed attempt goes to degraded_from."""
    state = await supervisor.invoke(_input(query=f"tell me {UNGROUNDED_TRIGGER} things"))

    assert state.result.tools_used == []
    assert state.result.degraded_from == ["RAG"]


async def test_empty_retrieval_abstains():
    state = await supervisor.invoke(_input(query="__empty__ nothing here"))

    assert state.result.status is SubAgentStatus.ABSTAINED
    assert state.result.answer_text is None


async def test_verifier_fails_closed_on_empty_chunk_list():
    from src.pipeline.harness.verifier_gate import verify_grounding

    verdict = await verify_grounding(answer="anything", cited_chunks=[], case_id="CASE-X")

    assert verdict["grounded"] is False


async def test_verifier_catches_cross_case_leakage():
    from src.pipeline.harness.verifier_gate import verify_grounding

    verdict = await verify_grounding(
        answer="An answer [Document 1]",
        cited_chunks=[{"id": "c1", "text": "t", "metadata": {"case_id": "CASE-OTHER"}}],
        case_id="CASE-A1B2C3D4",
    )

    assert verdict["grounded"] is False
    assert verdict["leaked_case_id"] == "CASE-OTHER"


# ── Event emission ───────────────────────────────────────────────────────

async def test_events_emitted_at_each_expected_transition():
    recorder = EventRecorder()
    await supervisor.invoke(_input(), events=recorder)

    steps = [e.step for e in recorder.events]

    assert "supervisor:dispatch" in steps
    assert f"subagent:{semantic_search.NAME}" in steps
    assert "tool:rag" in steps
    assert "supervisor:complete" in steps


async def test_subagent_and_tool_emit_both_active_and_terminal_events():
    """
    §2.2 granularity: one event per meaningful transition, not one summary
    event. Both the sub-agent and the tool must show a start and an end.
    """
    recorder = EventRecorder()
    await supervisor.invoke(_input(), events=recorder)

    for step in (f"subagent:{semantic_search.NAME}", "tool:rag"):
        statuses = [e.status for e in recorder.events if e.step == step]
        assert "active" in statuses, f"{step} never emitted an 'active' event"
        assert any(s in ("done", "error", "retry") for s in statuses), (
            f"{step} never emitted a terminal event"
        )


async def test_events_use_only_the_sse_status_vocabulary():
    recorder = EventRecorder()
    await supervisor.invoke(_input(), events=recorder)

    allowed = {"active", "done", "error", "retry", "skipped"}
    assert {e.status for e in recorder.events} <= allowed


async def test_failure_path_still_emits_terminal_events():
    recorder = EventRecorder()
    await supervisor.invoke(_input(query=f"{UNGROUNDED_TRIGGER} x"), events=recorder)

    steps = [e.step for e in recorder.events]
    assert "supervisor:dispatch" in steps
    assert "supervisor:complete" in steps


# ── Gateway threading, and the seam it protects ──────────────────────────

async def test_gateway_is_forwarded_to_nodes_that_declare_it(gateway):
    """
    `invoke()` accepted a gateway for its EventRecorder but never passed it to
    the node, so a sub-agent needing one silently fell back to get_gateway().
    Correct in production, but it made `invoke()` undrivable against a fake.
    """
    seen: dict = {}

    async def _node(agent_input, events=None, gateway=None):
        seen["gateway"] = gateway
        return SubAgentResult(status=SubAgentStatus.OK, answer_text="x")

    original = supervisor._route
    supervisor._NODES["gw_probe"] = _node
    try:
        supervisor._route = lambda _i: "gw_probe"
        await supervisor.invoke(_input(), gateway=gateway)
    finally:
        supervisor._route = original
        supervisor._NODES.pop("gw_probe", None)

    assert seen["gateway"] is gateway


async def test_nodes_without_a_gateway_parameter_still_work(gateway):
    """
    Semantic Search and Case Summarization take only (agent_input, events).
    Passing a gateway unconditionally would TypeError — the same
    signature-divergence class as the stub/real sql_tool crash.
    """
    async def _node(agent_input, events=None):
        return SubAgentResult(status=SubAgentStatus.OK, answer_text="x")

    original = supervisor._route
    supervisor._NODES["no_gw"] = _node
    try:
        supervisor._route = lambda _i: "no_gw"
        state = await supervisor.invoke(_input(), gateway=gateway)
    finally:
        supervisor._route = original
        supervisor._NODES.pop("no_gw", None)

    assert state.result.status is SubAgentStatus.OK


async def test_every_registered_sub_agent_survives_invocation(gateway):
    """
    THE SEAM TEST. Each sub-agent is individually correct and individually
    tested; this checks they all survive the ONE path that will reach them
    once routing is real. An audit found a TypeError here (SQL's stub/real
    signature divergence) that no sub-agent's own tests caught, because those
    run against the real tools.

    Asserts only that dispatch does not raise — outcomes vary with stub data
    and are covered by each sub-agent's own suite.
    """
    from src.pipeline.harness.agents import (
        aggregate_analysis, case_summary, cross_case_linkage,
        investigative_analysis, report_draft, semantic_search, timeline,
    )
    from src.pipeline.harness.tools import registry

    gateway.cases["CASE-A"] = {"case_id": "CASE-A"}
    agents = {
        m.NAME: m.run for m in (
            semantic_search, case_summary, report_draft, investigative_analysis,
            timeline, cross_case_linkage, aggregate_analysis,
        )
    }

    registry.use_stubs()
    original = supervisor._route
    try:
        for name, run in agents.items():
            supervisor._NODES[name] = run
            supervisor._route = lambda _i, n=name: n
            try:
                state = await supervisor.invoke(_input(), gateway=gateway)
                assert state.result is not None, f"{name} returned no result"
            finally:
                supervisor._NODES.pop(name, None)
    finally:
        supervisor._route = original
        registry.use_real()


async def test_events_mirror_to_durable_step_log(gateway):
    """
    §2.2: every event except `active` mirrors to log_step, which is the admin
    Run History page's only source.
    """
    recorder = EventRecorder(run_id="run-1", gateway=gateway)
    await supervisor.invoke(_input(), events=recorder)

    assert gateway.steps, "no steps persisted"
    persisted = {s["status"] for s in gateway.steps}
    assert persisted <= {"success", "skipped", "retry", "failed"}, (
        "log_step status must stay within the Postgres CHECK vocabulary"
    )
    assert not any(s["status"] == "active" for s in gateway.steps)
