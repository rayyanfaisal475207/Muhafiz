"""
Tests for src/pipeline/harness/cutover.py — the live-traffic cutover
adapter (AGENT_HARNESS_IMPLEMENTATION_PLAN.md §6).

`Supervisor`, `async_load_history`/`async_save_history`/
`format_history_for_prompt`, and the gateway are all monkeypatched or
faked at the module level (`cutover_mod.*`) — none of these hit live
infra.
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.cutover as cutover_mod
from src.pipeline.harness.cutover import run_cutover_query
from src.pipeline.harness.types import (
    Citation,
    SubAgentResult,
    SubAgentStatus,
    ValidationStatus,
)


class _FakeGateway:
    def __init__(self, project_memory=None, run_id="run-1"):
        self._project_memory = project_memory
        self._run_id = run_id
        self.update_run_calls = []
        self.create_run_calls = []

    async def get_project_memory(self, project_id):
        return self._project_memory

    async def create_run(self, session_id, user_message):
        self.create_run_calls.append((session_id, user_message))
        return self._run_id

    async def update_run(self, run_id, **kwargs):
        self.update_run_calls.append((run_id, kwargs))


def _stub_history(monkeypatch, history=None, saved=None):
    async def _fake_load(session_id, user_id=None):
        return history or []

    async def _fake_save(session_id, user_message, response, user_id, project_id=None):
        if saved is not None:
            saved.append((session_id, user_message, response))

    monkeypatch.setattr(cutover_mod, "async_load_history", _fake_load)
    monkeypatch.setattr(cutover_mod, "async_save_history", _fake_save)
    monkeypatch.setattr(cutover_mod, "format_history_for_prompt", lambda h: "" if not h else "prior turn")


def _stub_supervisor(monkeypatch, result: SubAgentResult, captured: dict = None):
    class _FakeSupervisor:
        async def handle(self, agent_input, *, on_event=None, gateway=None):
            if captured is not None:
                captured["agent_input"] = agent_input
                captured["gateway"] = gateway
            if on_event is not None:
                from src.pipeline.harness.types import PipelineEvent

                on_event(PipelineEvent(step="supervisor:dispatch", status="active", detail="classified"))
                on_event(PipelineEvent(step="supervisor:dispatch", status="done", detail="dispatched"))
            return result

    monkeypatch.setattr(cutover_mod, "Supervisor", _FakeSupervisor)


async def _collect(gen):
    return [evt async for evt in gen]


@pytest.mark.asyncio
async def test_successful_query_yields_response_and_saves_history(monkeypatch):
    _stub_history(monkeypatch, saved=(saved := []))
    result = SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text="The suspect fled the scene [Document 1].",
        citations=[Citation(document_index=1, source_tool="RAG")],
        tools_used=["RAG"],
        validation_status=ValidationStatus.PASSED,
    )
    captured = {}
    _stub_supervisor(monkeypatch, result, captured)
    gateway = _FakeGateway()

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="who fled?", project_id=None, case_id="CASE-001",
            user_id="u1", user_role="investigator", preferred_language=None, gateway=gateway,
        )
    )

    steps = [e["step"] for e in events]
    assert "supervisor" in steps
    assert "supervisor:dispatch" in steps
    assert any(e["step"] == "response" and e["status"] == "done" for e in events)
    assert any(e["step"] == "memory" and e["status"] == "done" for e in events)
    assert saved == [("s1", "who fled?", "The suspect fled the scene [Document 1].")]

    # [PRESERVE -- design §4.4] role threaded from user_role, not user_profile.
    assert captured["agent_input"].execution.caller.role.value == "investigator"
    assert captured["agent_input"].execution.caller.active_case_id == "CASE-001"
    assert captured["gateway"] is gateway

    # Postgres run-level parity.
    assert gateway.create_run_calls == [("s1", "who fled?")]
    assert gateway.update_run_calls[0][0] == "run-1"
    assert gateway.update_run_calls[0][1]["final_outcome"] == "ok"


@pytest.mark.asyncio
async def test_abstained_result_yields_error_event_no_history_save(monkeypatch):
    _stub_history(monkeypatch, saved=(saved := []))
    result = SubAgentResult(
        status=SubAgentStatus.ABSTAINED,
        caveats=["No sufficiently relevant documents were found."],
    )
    _stub_supervisor(monkeypatch, result)
    gateway = _FakeGateway()

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id=None, case_id=None,
            user_id="u1", user_role="investigator", preferred_language=None, gateway=gateway,
        )
    )

    response_events = [e for e in events if e["step"] == "response"]
    assert len(response_events) == 1
    assert response_events[0]["status"] == "error"
    assert "No sufficiently relevant" in response_events[0]["detail"]
    assert saved == []  # Nothing to save -- no answer was served.
    assert gateway.update_run_calls[0][1]["final_outcome"] == "abstained"


@pytest.mark.asyncio
async def test_unrecognized_role_fails_this_request_not_silently(monkeypatch):
    _stub_history(monkeypatch)
    gateway = _FakeGateway()

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id=None, case_id=None,
            user_id="u1", user_role="not-a-real-role", preferred_language=None, gateway=gateway,
        )
    )

    assert len(events) == 1
    assert events[0]["step"] == "system"
    assert events[0]["status"] == "error"
    assert "not-a-real-role" in events[0]["detail"]


@pytest.mark.asyncio
async def test_conversation_context_carries_history_and_project_memory(monkeypatch):
    _stub_history(monkeypatch, history=[object()])  # non-empty -> format_history_for_prompt stub returns "prior turn"
    result = SubAgentResult(status=SubAgentStatus.OK, answer_text="ok")
    captured = {}
    _stub_supervisor(monkeypatch, result, captured)
    gateway = _FakeGateway(project_memory={"summary_text": "established fact X"})

    await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id="proj-1", case_id=None,
            user_id="u1", user_role="investigator", preferred_language="Urdu", gateway=gateway,
        )
    )

    conv = captured["agent_input"].conversation_context
    assert conv.summary == "prior turn"
    assert conv.project_memory == "established fact X"
    assert captured["agent_input"].execution.caller.preferred_language == "Urdu"
    assert captured["agent_input"].execution.project_id == "proj-1"


@pytest.mark.asyncio
async def test_create_run_failure_does_not_block_the_query(monkeypatch):
    _stub_history(monkeypatch)
    result = SubAgentResult(status=SubAgentStatus.OK, answer_text="ok")
    _stub_supervisor(monkeypatch, result)

    class _FailingGateway(_FakeGateway):
        async def create_run(self, session_id, user_message):
            raise RuntimeError("db unavailable")

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id=None, case_id=None,
            user_id="u1", user_role="investigator", preferred_language=None, gateway=_FailingGateway(),
        )
    )
    assert any(e["step"] == "response" and e["status"] == "done" for e in events)
