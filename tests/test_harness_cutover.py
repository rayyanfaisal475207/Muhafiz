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
    ConflictState,
    CrossCaseLink,
    DataQualityMetric,
    DataQualityReadiness,
    GeneratedFileRef,
    SubAgentResult,
    SubAgentStatus,
    TimelineEvent,
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

    async def _fake_save(session_id, user_message, response, user_id, project_id=None, degradation_trace=None):
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
async def test_degradation_trace_saved_with_history(monkeypatch):
    """[Reconciliation merge — durable per-message trace, migration 019]
    An investigator's own "what I checked" panel is sourced from
    messages.degradation_trace, written in the SAME save_history() call
    that persists the answer -- not a separate write."""
    traces: list = []

    async def _fake_load(session_id, user_id=None):
        return []

    async def _fake_save(session_id, user_message, response, user_id, project_id=None, degradation_trace=None):
        traces.append(degradation_trace)

    monkeypatch.setattr(cutover_mod, "async_load_history", _fake_load)
    monkeypatch.setattr(cutover_mod, "async_save_history", _fake_save)

    result = SubAgentResult(
        status=SubAgentStatus.PARTIAL,
        answer_text="Partial finding [Document 1].",
        citations=[Citation(document_index=1, source_tool="RAG")],
        tools_used=["RAG"],
        degraded_from=["GRAPH"],
        caveats=["Case graph data was unavailable."],
    )
    _stub_supervisor(monkeypatch, result)

    await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id=None, case_id="CASE-001",
            user_id="u1", user_role="investigator", preferred_language=None, gateway=_FakeGateway(),
        )
    )

    assert len(traces) == 1
    trace = traces[0]
    assert trace["sub_agent_status"] == "partial"
    assert trace["tools_used"] == ["RAG"]
    assert trace["degraded_from"] == ["GRAPH"]
    assert trace["contributed_only"] == ["RAG"]
    assert trace["degraded_only"] == ["GRAPH"]
    assert trace["labels"]["degraded_only"] == ["case-graph search"]
    assert trace["caveats"] == ["Case graph data was unavailable."]


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


# ── Full-result-field translation — the gap this session closes: the SSE
# generator previously read only result.status/answer_text, silently
# dropping .generated_file/.links/.events/.metrics. ─────────────────────────

@pytest.mark.asyncio
async def test_generated_file_yields_file_generation_event_matching_orchestrator_shape(monkeypatch):
    """Mirrors orchestrator.py::_generate_file()'s own
    event("file_generation", "done", ..., sources=[{"filename","type","file_id"}])
    — the exact shape MessageBubble.tsx filters on to render the download link."""
    _stub_history(monkeypatch, saved=(saved := []))
    result = SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text="Report drafted.",
        generated_file=GeneratedFileRef(
            file_id="file-1", file_name="Case Summary.pdf", storage_path="/tmp/x.pdf",
            disclosure_rendered=False,
        ),
    )
    _stub_supervisor(monkeypatch, result)
    gateway = _FakeGateway()

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="draft a report", project_id=None, case_id="CASE-001",
            user_id="u1", user_role="investigator", preferred_language=None, gateway=gateway,
        )
    )

    file_events = [e for e in events if e["step"] == "file_generation"]
    assert len(file_events) == 1
    assert file_events[0]["status"] == "done"
    assert file_events[0]["sources"] == [
        {"filename": "Case Summary.pdf", "type": "pdf", "file_id": "file-1"}
    ]
    # Ordering parity with orchestrator.py: file_generation comes after memory.
    steps = [e["step"] for e in events]
    assert steps.index("memory") < steps.index("file_generation")
    assert saved == [("s1", "draft a report", "Report drafted.")]


@pytest.mark.asyncio
async def test_no_generated_file_means_no_file_generation_event(monkeypatch):
    _stub_history(monkeypatch)
    result = SubAgentResult(status=SubAgentStatus.OK, answer_text="ok")
    _stub_supervisor(monkeypatch, result)

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id=None, case_id=None,
            user_id="u1", user_role="investigator", preferred_language=None, gateway=_FakeGateway(),
        )
    )
    assert not any(e["step"] == "file_generation" for e in events)


@pytest.mark.asyncio
async def test_cross_case_links_yield_cross_case_finding_event(monkeypatch):
    """Mirrors orchestrator.py's XGRAPH-route event("cross_case_finding",
    "done", ..., case_scope=..., unconfirmed_links=...) as closely as the
    bounded CrossCaseLink payload allows (no raw-chunk `sources` available
    at this boundary — see the inline comment at the yield site)."""
    _stub_history(monkeypatch)
    result = SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text="Two cases connect via a shared vehicle plate.",
        links=[
            CrossCaseLink(
                description="Shared plate ABC-123", case_ids=["CASE-001", "CASE-002"],
                confidence=0.82, source_tool="XGRAPH", is_unconfirmed=False,
            ),
            CrossCaseLink(
                description="Possible shared alias", case_ids=["CASE-001", "CASE-003"],
                confidence=0.31, source_tool="XGRAPH", is_unconfirmed=True,
            ),
        ],
    )
    _stub_supervisor(monkeypatch, result)

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id=None, case_id=None,
            user_id="u1", user_role="investigator", preferred_language=None, gateway=_FakeGateway(),
        )
    )

    xc = [e for e in events if e["step"] == "cross_case_finding"]
    assert len(xc) == 1
    assert xc[0]["status"] == "done"
    assert xc[0]["case_scope"] == "cross_case"
    assert len(xc[0]["unconfirmed_links"]) == 1
    assert xc[0]["unconfirmed_links"][0]["description"] == "Possible shared alias"
    # Ordering parity with orchestrator.py: cross_case_finding precedes response.
    steps = [e["step"] for e in events]
    assert steps.index("cross_case_finding") < steps.index("response")


@pytest.mark.asyncio
async def test_cross_case_links_rendered_into_delivered_answer_text(monkeypatch):
    """[Reconciliation fix — harness-reconciliation Unit 12] The
    cross_case_finding SSE event alone is not enough — the frontend's
    consumption of that step is shallow (a numeric badge only), so the
    actual substance of a finding (which cases connect, and which possible
    identity matches are unconfirmed) must also reach the answer text
    itself, the one surface guaranteed to reach the user."""
    _stub_history(monkeypatch)
    result = SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text="Two cases connect via a shared vehicle plate.",
        links=[
            CrossCaseLink(
                description="Shared plate ABC-123", case_ids=["CASE-001", "CASE-002"],
                confidence=0.82, source_tool="XGRAPH", is_unconfirmed=False,
            ),
            CrossCaseLink(
                description="Possible shared alias", case_ids=["CASE-001", "CASE-003"],
                confidence=0.31, source_tool="XGRAPH", is_unconfirmed=True,
            ),
        ],
    )
    _stub_supervisor(monkeypatch, result)

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id=None, case_id=None,
            user_id="u1", user_role="investigator", preferred_language=None, gateway=_FakeGateway(),
        )
    )

    streamed = [e for e in events if e["step"] == "response" and e["status"] == "streaming"]
    assert len(streamed) == 1
    delivered = streamed[0]["detail"]
    assert "Shared plate ABC-123" in delivered
    assert "Possible shared alias" in delivered
    assert "Confirmed connections" in delivered
    assert "unverified leads" in delivered.lower()


@pytest.mark.asyncio
async def test_caveats_rendered_into_delivered_answer_text_without_duplication(monkeypatch):
    """[Reconciliation fix — Unit 12] SubAgentResult.caveats' own
    [PRESERVE — design §3] rule: qualifications MUST survive to the final
    response. Previously never read on the success path at all -- every
    degradation qualification a sub-agent attached was silently dropped."""
    _stub_history(monkeypatch)
    result = SubAgentResult(
        status=SubAgentStatus.PARTIAL,
        answer_text="Case graph data was unavailable; this summary is based on documents only.",
        caveats=[
            # Already present in answer_text verbatim -- must NOT be printed twice.
            "Case graph data was unavailable; this summary is based on documents only.",
            # Not present in answer_text -- must be appended.
            "A secondary claim-verification check could not be completed for this report.",
        ],
    )
    _stub_supervisor(monkeypatch, result)

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id=None, case_id=None,
            user_id="u1", user_role="investigator", preferred_language=None, gateway=_FakeGateway(),
        )
    )

    streamed = [e for e in events if e["step"] == "response" and e["status"] == "streaming"]
    delivered = streamed[0]["detail"]
    assert delivered.count("Case graph data was unavailable; this summary is based on documents only.") == 1
    assert "A secondary claim-verification check could not be completed for this report." in delivered


@pytest.mark.asyncio
async def test_no_links_means_no_cross_case_finding_event(monkeypatch):
    _stub_history(monkeypatch)
    result = SubAgentResult(status=SubAgentStatus.OK, answer_text="ok")
    _stub_supervisor(monkeypatch, result)

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id=None, case_id=None,
            user_id="u1", user_role="investigator", preferred_language=None, gateway=_FakeGateway(),
        )
    )
    assert not any(e["step"] == "cross_case_finding" for e in events)


@pytest.mark.asyncio
async def test_timeline_events_yield_new_timeline_building_step(monkeypatch):
    """No pre-harness precedent — AskUserQuestion-resolved this session to a
    new, additive SSE step carrying the full TimelineEvent payload."""
    _stub_history(monkeypatch)
    result = SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text="Three events reconstructed.",
        events=[
            TimelineEvent(
                event_id="evt-1", description="Suspect seen at scene",
                occurred_on="2026-01-05T10:00:00Z", conflict_state=ConflictState.CONFLICT,
                conflict_basis="Two witness statements disagree on time.",
            ),
        ],
    )
    _stub_supervisor(monkeypatch, result)

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id=None, case_id=None,
            user_id="u1", user_role="investigator", preferred_language=None, gateway=_FakeGateway(),
        )
    )

    tl = [e for e in events if e["step"] == "timeline_building"]
    assert len(tl) == 1
    assert tl[0]["status"] == "done"
    assert tl[0]["events"][0]["event_id"] == "evt-1"
    assert tl[0]["events"][0]["conflict_state"] == "conflict"


@pytest.mark.asyncio
async def test_data_quality_metrics_yield_new_data_quality_step(monkeypatch):
    """No pre-harness precedent — same AskUserQuestion resolution as
    timeline_building above."""
    _stub_history(monkeypatch)
    result = SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text="Coverage assessed.",
        metrics=[
            DataQualityMetric(
                name="document_coverage", label="Document coverage",
                readiness=DataQualityReadiness.READY, counts={"documents": 42},
                explains="How much of the case file has been ingested.",
            ),
        ],
    )
    _stub_supervisor(monkeypatch, result)

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id=None, case_id=None,
            user_id="u1", user_role="investigator", preferred_language=None, gateway=_FakeGateway(),
        )
    )

    dq = [e for e in events if e["step"] == "data_quality"]
    assert len(dq) == 1
    assert dq[0]["status"] == "done"
    assert dq[0]["metrics"][0]["name"] == "document_coverage"
    assert dq[0]["metrics"][0]["readiness"] == "ready"


@pytest.mark.asyncio
async def test_no_events_or_metrics_means_no_new_steps(monkeypatch):
    _stub_history(monkeypatch)
    result = SubAgentResult(status=SubAgentStatus.OK, answer_text="ok")
    _stub_supervisor(monkeypatch, result)

    events = await _collect(
        run_cutover_query(
            session_id="s1", user_message="q", project_id=None, case_id=None,
            user_id="u1", user_role="investigator", preferred_language=None, gateway=_FakeGateway(),
        )
    )
    steps = {e["step"] for e in events}
    assert "timeline_building" not in steps
    assert "data_quality" not in steps
