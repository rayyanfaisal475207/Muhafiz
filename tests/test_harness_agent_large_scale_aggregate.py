"""
Tests for src/pipeline/harness/agents/large_scale_aggregate.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 2, this session's "Phase 3" --
see the module's own docstring for the naming disambiguation).

Covers:
  (a) successful aggregate + verified paraphrase -> status=OK, bounded
      citations (no chunk text), tools_used=["XAGG"], degraded_from=[];
  (b) verifier-rejection -> falls back to XAggToolResult.raw_summary_text
      served as status=OK with an explanatory caveat (this session's
      explicit, documented status decision -- NOT ABSTAINED/PARTIAL);
  (c) XAGG DENIED -> SubAgentStatus.DENIED, never collapsed into
      ABSTAINED/EMPTY (RESOLVED-6);
  (d) XAGG FAILED -> SubAgentStatus.ABSTAINED, tool's error propagated;
  (e) XAGG EMPTY -> SubAgentStatus.EMPTY, not an error (defensive branch --
      xagg_tool never currently emits this status, kept per the tool's
      declared ToolStatus contract regardless);
  (f) generation (call_llm) raising -> ABSTAINED, distinct from a verifier
      rejection since there is no candidate text to fall back to;
  (g) module-level self-registration into the Supervisor's registry, and a
      Supervisor.handle() -> Large-Scale Aggregate -> XAGG tool integration
      path.

`xagg_tool`, `call_llm`, and `verify_grounding` are monkeypatched at the
module level (`lsa_mod.*`) in every test -- none of these hit live infra,
per this session's scope (test/mock data only).
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.agents.large_scale_aggregate as lsa_mod
from src.pipeline.harness.agents.large_scale_aggregate import large_scale_aggregate
from src.pipeline.harness.supervisor import (
    LARGE_SCALE_AGGREGATE,
    Supervisor,
    get_registered,
)
from src.pipeline.harness.tools.xagg import XAggToolResult
from src.pipeline.harness.types import (
    CallerContext,
    ChunkMetadata,
    EvidenceChunk,
    ExecutionContext,
    Role,
    SubAgentInput,
    SubAgentStatus,
    ToolError,
    ToolStatus,
)


def _agg_chunk(text="- ABC-123 (Vehicle): appears in 3 cases — CASE-001, CASE-002, CASE-003"):
    return EvidenceChunk(
        id="xagg-aggregate",
        text=text,
        metadata=ChunkMetadata(source_tool="XAGG", source_file="cross-case aggregate"),
    )


def _caller(role=Role.SUPERVISOR, **kw):
    return CallerContext(user_id="u1", role=role, active_case_id=None, **kw)


def _execution(caller=None):
    return ExecutionContext(caller=caller or _caller())


def _agent_input(caller=None, query_text="top recurring vehicles across cases", **kw):
    return SubAgentInput(query_text=query_text, execution=_execution(caller=caller), **kw)


def _stub_xagg_tool(monkeypatch, result: XAggToolResult):
    async def _fake(tool_input):
        return result

    monkeypatch.setattr(lsa_mod, "xagg_tool", _fake)


def _stub_call_llm(monkeypatch, answer: str = "3 vehicles recurred across cases [Document 1].", exc=None):
    async def _fake(system_prompt, user_message, **kwargs):
        if exc is not None:
            raise exc
        return answer

    monkeypatch.setattr(lsa_mod, "call_llm", _fake)


def _stub_verify_grounding(monkeypatch, grounded: bool, off_topic: bool = False, reason: str = "ok"):
    captured = {}

    async def _fake(answer, cited_chunks, case_id, cross_case_ids=None, target_date=None):
        captured["case_id"] = case_id
        captured["cross_case_ids"] = cross_case_ids
        return {
            "grounded": grounded,
            "off_topic": off_topic,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": reason,
        }

    monkeypatch.setattr(lsa_mod, "verify_grounding", _fake)
    return captured


# ═══════════════════════════════════════════════════════════════════════
# (a) successful aggregate + verified paraphrase
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_successful_aggregate_returns_ok_with_bounded_citations(monkeypatch):
    chunk = _agg_chunk()
    tool_result = XAggToolResult(
        status=ToolStatus.OK,
        chunks=[chunk],
        case_ids_touched=["CASE-001", "CASE-002", "CASE-003"],
        aggregate_kind="graph_recurrence",
        raw_summary_text=chunk.text,
    )
    _stub_xagg_tool(monkeypatch, tool_result)
    _stub_call_llm(monkeypatch, "3 vehicles recurred across cases [Document 1].")
    captured = _stub_verify_grounding(monkeypatch, grounded=True)

    result = await large_scale_aggregate(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert result.answer_text == "3 vehicles recurred across cases [Document 1]."
    assert result.tools_used == ["XAGG"]
    assert result.degraded_from == []
    assert len(result.citations) == 1
    assert result.citations[0].document_index == 1
    assert result.citations[0].source_tool == "XAGG"
    # Bounded payload -- never the underlying chunk text/EvidenceChunk.
    for citation in result.citations:
        assert not hasattr(citation, "text")
    # [PRESERVE — design §4.6] cross_case_ids threaded from case_ids_touched.
    assert captured["case_id"] == "cross_case"
    assert captured["cross_case_ids"] == ["CASE-001", "CASE-002", "CASE-003"]


# ═══════════════════════════════════════════════════════════════════════
# (b) verifier rejection -> raw_summary_text served as status=OK
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_verifier_rejection_falls_back_to_raw_summary_as_ok(monkeypatch):
    chunk = _agg_chunk("Total cases: 42")
    tool_result = XAggToolResult(
        status=ToolStatus.OK,
        chunks=[chunk],
        case_ids_touched=[],
        aggregate_kind="total_count",
        raw_summary_text="Total cases: 42",
    )
    _stub_xagg_tool(monkeypatch, tool_result)
    _stub_call_llm(monkeypatch, "I don't have access to that information.")
    _stub_verify_grounding(monkeypatch, grounded=False, off_topic=True, reason="generic refusal")

    result = await large_scale_aggregate(_agent_input(query_text="how many cases in total"))

    # [PRESERVE — design §2.4] NOT a generic abstention: this session's
    # explicit status decision is OK, serving the raw deterministic text.
    assert result.status == SubAgentStatus.OK
    assert result.answer_text == "Total cases: 42"
    assert result.tools_used == ["XAGG"]
    assert result.degraded_from == []
    assert len(result.citations) == 1
    assert result.caveats  # explains the raw/unparaphrased format was used


# ═══════════════════════════════════════════════════════════════════════
# (c) XAGG DENIED -> SubAgentStatus.DENIED, never collapsed
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_xagg_denied_propagates_as_its_own_status(monkeypatch):
    err = ToolError(kind="permission_denied", message="Cross-case aggregate queries require supervisor role or higher.")
    _stub_xagg_tool(monkeypatch, XAggToolResult(status=ToolStatus.DENIED, error=err))

    result = await large_scale_aggregate(_agent_input(caller=_caller(role=Role.INVESTIGATOR)))

    assert result.status == SubAgentStatus.DENIED
    assert result.status != SubAgentStatus.ABSTAINED
    assert result.status != SubAgentStatus.EMPTY
    assert result.answer_text is None
    assert result.error is err
    assert result.caveats


# ═══════════════════════════════════════════════════════════════════════
# (d) XAGG FAILED -> ABSTAINED, error propagated
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_xagg_failure_abstains_with_error(monkeypatch):
    err = ToolError(kind="upstream_failure", message="postgres unreachable")
    _stub_xagg_tool(monkeypatch, XAggToolResult(status=ToolStatus.FAILED, error=err))

    result = await large_scale_aggregate(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.error is err


# ═══════════════════════════════════════════════════════════════════════
# (e) XAGG EMPTY -> SubAgentStatus.EMPTY, not an error
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_xagg_empty_returns_empty_not_abstained(monkeypatch):
    _stub_xagg_tool(monkeypatch, XAggToolResult(status=ToolStatus.EMPTY, chunks=[]))

    result = await large_scale_aggregate(_agent_input())

    assert result.status == SubAgentStatus.EMPTY
    assert result.answer_text is None
    assert result.caveats


# ═══════════════════════════════════════════════════════════════════════
# (f) generation exception -> ABSTAINED (distinct from verifier rejection)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_generation_failure_abstains(monkeypatch):
    chunk = _agg_chunk()
    tool_result = XAggToolResult(
        status=ToolStatus.OK,
        chunks=[chunk],
        case_ids_touched=["CASE-001"],
        aggregate_kind="graph_recurrence",
        raw_summary_text=chunk.text,
    )
    _stub_xagg_tool(monkeypatch, tool_result)
    _stub_call_llm(monkeypatch, exc=RuntimeError("llm service unreachable"))

    result = await large_scale_aggregate(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None


# ═══════════════════════════════════════════════════════════════════════
# (g) Supervisor integration -- real registration, real dispatch chain
# ═══════════════════════════════════════════════════════════════════════

def test_large_scale_aggregate_is_registered_under_its_own_name():
    # Import-time self-registration (module docstring) -- importing the
    # module above already registered it into the live, module-level
    # registry; this just asserts that actually happened.
    assert get_registered(LARGE_SCALE_AGGREGATE) is large_scale_aggregate


@pytest.mark.asyncio
async def test_supervisor_dispatches_to_real_large_scale_aggregate_and_real_xagg_tool(monkeypatch):
    """
    Supervisor.handle() -> real Large-Scale Aggregate -> real xagg_tool() ->
    SubAgentResult, with router.route_query() and run_aggregate() stubbed to
    deterministic test data (not live infra). Proves the Supervisor and this
    sub-agent actually connect end to end.
    """
    import src.pipeline.harness.supervisor as supervisor_mod
    import src.pipeline.harness.tools.xagg as xagg_tool_mod

    async def _fake_route_query(query_text: str) -> dict:
        return {"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"}

    monkeypatch.setattr(supervisor_mod, "route_query", _fake_route_query)

    async def _fake_get_gateway():
        class _FakeGateway:
            pass

        return _FakeGateway()

    async def _fake_run_aggregate(query_text, target_entity, gateway, user_id=None, user_role="investigator"):
        return {"kind": "total_count", "total_cases": 7}

    monkeypatch.setattr(xagg_tool_mod, "get_gateway", _fake_get_gateway)
    monkeypatch.setattr(xagg_tool_mod, "run_aggregate", _fake_run_aggregate)

    _stub_call_llm(monkeypatch, "There are 7 cases in total [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    sup = Supervisor()  # no override -> real module-level registry
    result = await sup.handle(_agent_input(caller=_caller(role=Role.SUPERVISOR), query_text="how many cases in total"))

    assert result.status == SubAgentStatus.OK
    assert result.answer_text == "There are 7 cases in total [Document 1]."
    assert result.tools_used == ["XAGG"]
    assert len(result.citations) == 1
    assert result.citations[0].source_tool == "XAGG"
