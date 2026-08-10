"""
Durable per-message degradation trace (migration 018).

The admin trace in `pipeline_steps` is keyed on `run_id`, which exists before
the query runs. A message-level trace cannot work the same way: at the moment
the supervisor completes, the assistant message DOES NOT EXIST YET. Retrofitting
it afterwards would mean finding the row by `session_id + role + exact content`
— which `update_message_citations` already does and which mis-keys the instant
two answers in one session are byte-identical.

So the payload is CARRIED to the insert that creates the message rather than
written separately. These tests pin that: one build, one write, no lookup.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness import supervisor
from src.pipeline.harness.agents import case_summary, semantic_search
from src.pipeline.harness.contracts import (
    SOURCE_TOOL_DISPLAY_LABELS,
    CallerContext,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)
from src.pipeline.harness.events import EventRecorder, build_degradation_trace

# `invoke()` requires a route_result. These tests override `_route` directly,
# so the router decision only needs to be well-formed, not meaningful.
_ROUTE_RESULT = {"route": "RAG", "output_format": "chat", "case_scope": "within_case"}


def _caller() -> CallerContext:
    return CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id="CASE-A")


def _input(query: str = "what happened") -> SubAgentInput:
    return SubAgentInput(query_text=query, caller=_caller())


# ── Pre-rendered labels: one source of truth ─────────────────────────────

def test_trace_carries_prerendered_labels():
    """
    Clients render `labels` verbatim. The canonical map lives in contracts.py;
    shipping rendered strings is what stops a third copy of it appearing in
    TypeScript (the admin StepTrace already holds a second — see §7).
    """
    trace = build_degradation_trace(SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="x",
        tools_used=["RAG", "GRAPH"], degraded_from=["RAG", "SQL"],
    ))

    assert trace["labels"]["contributed_only"] == [SOURCE_TOOL_DISPLAY_LABELS["GRAPH"]]
    assert trace["labels"]["degraded_and_contributed"] == [SOURCE_TOOL_DISPLAY_LABELS["RAG"]]
    assert trace["labels"]["degraded_only"] == [SOURCE_TOOL_DISPLAY_LABELS["SQL"]]


def test_labels_mirror_the_raw_three_way_split_exactly():
    """Same partition, different vocabulary — never a different set of tools."""
    trace = build_degradation_trace(SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="x",
        tools_used=["RAG"], degraded_from=["GRAPH", "SQL"],
    ))

    for key in ("contributed_only", "degraded_and_contributed", "degraded_only"):
        assert len(trace["labels"][key]) == len(trace[key])


def test_labels_never_leak_raw_tool_identifiers():
    trace = build_degradation_trace(SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="x",
        tools_used=["RAG"], degraded_from=["GRAPH"],
    ))

    rendered = " ".join(
        trace["labels"]["contributed_only"] + trace["labels"]["degraded_only"]
    )

    assert "RAG" not in rendered
    assert "GRAPH" not in rendered


# ── Persistence round-trip ───────────────────────────────────────────────

async def test_trace_persists_on_the_assistant_message(gateway):
    trace = build_degradation_trace(SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="x",
        tools_used=["GRAPH"], degraded_from=["RAG"], caveats=["doc search unavailable"],
    ))

    await gateway.save_message("s1", "user", "question")
    await gateway.save_message("s1", "assistant", "answer", degradation_trace=trace)

    history = await gateway.get_session_history("s1")
    assistant = [m for m in history if m["role"] == "assistant"][0]

    assert assistant["degradation_trace"] is not None
    assert assistant["degradation_trace"]["degraded_only"] == ["RAG"]
    assert assistant["degradation_trace"]["caveats"] == ["doc search unavailable"]


async def test_user_message_carries_no_trace(gateway):
    """The trace describes how an ANSWER was produced. A question has none."""
    trace = build_degradation_trace(
        SubAgentResult(status=SubAgentStatus.OK, answer_text="x", tools_used=["RAG"])
    )

    await gateway.save_message("s1", "user", "question")
    await gateway.save_message("s1", "assistant", "answer", degradation_trace=trace)

    history = await gateway.get_session_history("s1")
    user_msg = [m for m in history if m["role"] == "user"][0]

    assert user_msg["degradation_trace"] is None


async def test_legacy_messages_read_as_no_trace_not_clean_run(gateway):
    """
    NULL means "no trace recorded" — a pre-harness or legacy-path message.
    That is NOT the same fact as a clean run, and a reader that conflates them
    would show an all-clear the system never computed.
    """
    await gateway.save_message("s1", "assistant", "legacy answer")

    history = await gateway.get_session_history("s1")

    assert history[0]["degradation_trace"] is None


async def test_save_message_defaults_the_trace_so_legacy_callers_are_unaffected(gateway):
    """The legacy orchestrator calls save_message with three args and must keep working."""
    await gateway.save_message("s1", "assistant", "answer")

    assert (await gateway.get_session_history("s1"))[0]["content"] == "answer"


# ── The carry path ───────────────────────────────────────────────────────

async def test_async_save_history_threads_the_trace_through(monkeypatch, gateway):
    """
    End-to-end of the carry path: async_save_history -> save_history ->
    save_message, with the trace landing in the SAME insert that creates the
    message. No second write, no lookup-by-content.
    """
    from src.memory import conversation

    async def _get_gateway():
        return gateway

    monkeypatch.setattr(conversation, "get_gateway", _get_gateway, raising=False)
    monkeypatch.setattr("src.data_gateway.get_gateway", _get_gateway, raising=False)

    gateway.sessions["s1"] = {"session_id": "s1", "user_id": "u1", "title": "t"}

    trace = build_degradation_trace(SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="x",
        tools_used=["GRAPH"], degraded_from=["RAG"],
    ))

    await conversation.async_save_history(
        "s1", "question", "answer", user_id="u1", degradation_trace=trace,
    )

    history = await gateway.get_session_history("s1")
    assistant = [m for m in history if m["role"] == "assistant"][0]
    assert assistant["degradation_trace"]["degraded_only"] == ["RAG"]


async def test_carry_path_is_optional_for_legacy_callers(monkeypatch, gateway):
    """Omitting the trace persists NULL rather than erroring."""
    from src.memory import conversation

    async def _get_gateway():
        return gateway

    monkeypatch.setattr(conversation, "get_gateway", _get_gateway, raising=False)
    monkeypatch.setattr("src.data_gateway.get_gateway", _get_gateway, raising=False)

    gateway.sessions["s1"] = {"session_id": "s1", "user_id": "u1", "title": "t"}

    await conversation.async_save_history("s1", "question", "answer", user_id="u1")

    history = await gateway.get_session_history("s1")
    assert [m for m in history if m["role"] == "assistant"][0]["degradation_trace"] is None


# ── Still generic across all seven sub-agents ────────────────────────────

@pytest.mark.parametrize("agent_name", [semantic_search.NAME, case_summary.NAME])
async def test_persisted_trace_is_generic_across_sub_agents(agent_name, monkeypatch, gateway):
    """
    Requirement 6 re-confirmed WITH persistence added: the payload still comes
    from one generic build at one call site, whichever sub-agent ran.
    """
    monkeypatch.setattr(supervisor, "_route", lambda _i, _r: agent_name)

    recorder = EventRecorder()
    state = await supervisor.invoke(_input(), _ROUTE_RESULT, events=recorder)

    trace = build_degradation_trace(state.result)
    await gateway.save_message("s1", "assistant", "answer", degradation_trace=trace)

    stored = (await gateway.get_session_history("s1"))[0]["degradation_trace"]
    assert set(stored) >= {"v", "labels", "contributed_only", "degraded_only", "caveats"}


async def test_a_new_sub_agent_persists_a_trace_without_extra_wiring(gateway):
    """
    A sub-agent registered later gets a persistable trace for free — the same
    guarantee the admin trace has, now covering the durable message copy too.
    """
    async def brand_new_sub_agent(agent_input, events=None):
        return SubAgentResult(
            status=SubAgentStatus.PARTIAL, answer_text="synthesized",
            tools_used=["RAG"], degraded_from=["GRAPH", "SQL"],
        )

    supervisor._NODES["brand_new"] = brand_new_sub_agent
    original = supervisor._route
    try:
        supervisor._route = lambda _i, _r: "brand_new"
        state = await supervisor.invoke(_input(), _ROUTE_RESULT)
    finally:
        supervisor._route = original
        supervisor._NODES.pop("brand_new", None)

    await gateway.save_message(
        "s1", "assistant", "answer", degradation_trace=build_degradation_trace(state.result),
    )

    stored = (await gateway.get_session_history("s1"))[0]["degradation_trace"]
    assert stored["labels"]["degraded_only"] == [
        SOURCE_TOOL_DISPLAY_LABELS["GRAPH"], SOURCE_TOOL_DISPLAY_LABELS["SQL"],
    ]
