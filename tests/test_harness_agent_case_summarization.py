"""
Tests for src/pipeline/harness/agents/case_summarization.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 3, "Phase 4").

Covers:
  (a) full success -- both RAG and GRAPH contribute -> status=OK, combined
      RAG-then-GRAPH flattened citation order, tools_used=["RAG","GRAPH"],
      degraded_from=[];
  (b) RAG-only degradation (GRAPH empty) -> status=PARTIAL,
      degraded_from=["GRAPH"], tools_used=["RAG"], NO disclosure text
      prepended to answer_text;
  (c) GRAPH-only degradation (RAG empty) -> status=PARTIAL,
      degraded_from=["RAG"], tools_used=["GRAPH"], GRAPH_ONLY_SUMMARY_
      DISCLOSURE correctly PREPENDED post-verification;
  (d) both empty -> status=EMPTY, not an error;
  (e) Verifier rejection on the GRAPH-only path -> ABSTAINED, no disclosure
      produced, answer_text stays None;
  (f) module-level self-registration into the Supervisor's registry, and a
      Supervisor.handle() -> Case Summarization -> real rag_tool()/
      graph_tool() integration path.

`rag_tool`, `graph_tool`, `call_llm`, and `verify_grounding` are
monkeypatched at the module level (`cs_mod.*`) in every test -- none of
these hit live infra, per this session's scope (test/mock data only).
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.agents.case_summarization as cs_mod
from src.pipeline.harness.agents.case_summarization import case_summarization
from src.pipeline.harness.supervisor import (
    CASE_SUMMARIZATION,
    Supervisor,
    get_registered,
)
from src.pipeline.harness.tools.graph import GraphToolResult
from src.pipeline.harness.tools.rag import RagToolResult
from src.pipeline.harness.types import (
    CallerContext,
    ChunkMetadata,
    EvidenceChunk,
    ExecutionContext,
    GRAPH_ONLY_SUMMARY_DISCLOSURE,
    Role,
    SubAgentInput,
    SubAgentStatus,
    ToolError,
    ToolStatus,
)


def _rag_chunk(id_="r1", text="the suspect fled the scene", case_id="CASE-001", source="doc.pdf"):
    return EvidenceChunk(
        id=id_,
        text=text,
        metadata=ChunkMetadata(source_tool="RAG", case_id=case_id, source_file=source),
    )


def _graph_chunk(id_="g1", text="Person P-1 is linked to Vehicle V-1", case_id="CASE-001"):
    return EvidenceChunk(
        id=id_,
        text=text,
        metadata=ChunkMetadata(source_tool="GRAPH", case_id=case_id),
    )


def _caller(case_id="CASE-001", role=Role.INVESTIGATOR, **kw):
    return CallerContext(user_id="u1", role=role, active_case_id=case_id, **kw)


def _execution(caller=None):
    return ExecutionContext(caller=caller or _caller())


def _agent_input(caller=None, query_text="summarize this case", **kw):
    return SubAgentInput(query_text=query_text, execution=_execution(caller=caller), **kw)


def _stub_rag_tool(monkeypatch, result=None, exc=None):
    async def _fake(tool_input):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(cs_mod, "rag_tool", _fake)


def _stub_graph_tool(monkeypatch, result=None, exc=None):
    async def _fake(tool_input):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(cs_mod, "graph_tool", _fake)


def _stub_call_llm(monkeypatch, answer="Status: open [Document 1].", exc=None):
    async def _fake(system_prompt, user_message, **kwargs):
        if exc is not None:
            raise exc
        return answer

    monkeypatch.setattr(cs_mod, "call_llm", _fake)


def _stub_verify_grounding(monkeypatch, grounded: bool, off_topic: bool = False, reason: str = "ok"):
    captured = {}

    async def _fake(answer, cited_chunks, case_id, cross_case_ids=None, target_date=None):
        captured["answer"] = answer
        captured["cited_chunks"] = cited_chunks
        captured["case_id"] = case_id
        return {
            "grounded": grounded,
            "off_topic": off_topic,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": reason,
        }

    monkeypatch.setattr(cs_mod, "verify_grounding", _fake)
    return captured


_RAG_OK = lambda chunks: RagToolResult(status=ToolStatus.OK, chunks=chunks)
_RAG_EMPTY = RagToolResult(status=ToolStatus.EMPTY)
_GRAPH_OK = lambda chunks: GraphToolResult(status=ToolStatus.OK, chunks=chunks)
_GRAPH_EMPTY = GraphToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True)


# ═══════════════════════════════════════════════════════════════════════
# (a) full success -- both tools contribute
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_full_success_flattens_rag_then_graph_and_returns_ok(monkeypatch):
    rag_chunk = _rag_chunk()
    graph_chunk = _graph_chunk()
    _stub_rag_tool(monkeypatch, _RAG_OK([rag_chunk]))
    _stub_graph_tool(monkeypatch, _GRAPH_OK([graph_chunk]))
    _stub_call_llm(monkeypatch, "Status: open [Document 1]. P-1 linked to V-1 [Document 2].")
    captured = _stub_verify_grounding(monkeypatch, grounded=True)

    result = await case_summarization(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert result.answer_text == "Status: open [Document 1]. P-1 linked to V-1 [Document 2]."
    assert result.tools_used == ["RAG", "GRAPH"]
    assert result.degraded_from == []
    # Flattening order: RAG chunks first, then GRAPH chunks -- positionally
    # matching what the Verifier was shown.
    assert len(result.citations) == 2
    assert result.citations[0].document_index == 1
    assert result.citations[0].source_tool == "RAG"
    assert result.citations[1].document_index == 2
    assert result.citations[1].source_tool == "GRAPH"
    # Same exact flattened list, same order, handed to the Verifier.
    assert [c["id"] for c in captured["cited_chunks"]] == ["r1", "g1"]
    # Bounded payload -- no chunk text on the citation objects.
    for citation in result.citations:
        assert not hasattr(citation, "text")


# ═══════════════════════════════════════════════════════════════════════
# (b) RAG-only degradation (GRAPH empty) -- no disclosure text
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_graph_empty_degrades_to_rag_only_no_disclosure(monkeypatch):
    rag_chunk = _rag_chunk()
    _stub_rag_tool(monkeypatch, _RAG_OK([rag_chunk]))
    _stub_graph_tool(monkeypatch, _GRAPH_EMPTY)
    _stub_call_llm(monkeypatch, "Status: open [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await case_summarization(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == ["RAG"]
    assert result.degraded_from == ["GRAPH"]
    assert result.answer_text == "Status: open [Document 1]."
    # [PRESERVE -- SUBAGENT_INTERFACES.md §2.1.2] The RAG-succeeds direction
    # needs NO in-text disclosure -- it's the default-shape summary.
    assert GRAPH_ONLY_SUMMARY_DISCLOSURE not in result.answer_text
    assert len(result.citations) == 1
    assert result.citations[0].source_tool == "RAG"


@pytest.mark.asyncio
async def test_graph_failed_also_degrades_to_rag_only(monkeypatch):
    """GRAPH FAILED (not just EMPTY) is degradation-equivalent to EMPTY."""
    rag_chunk = _rag_chunk()
    err = ToolError(kind="upstream_failure", message="graph db unreachable")
    _stub_rag_tool(monkeypatch, _RAG_OK([rag_chunk]))
    _stub_graph_tool(monkeypatch, GraphToolResult(status=ToolStatus.FAILED, error=err))
    _stub_call_llm(monkeypatch, "Status: open [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await case_summarization(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.degraded_from == ["GRAPH"]
    assert result.tools_used == ["RAG"]


@pytest.mark.asyncio
async def test_graph_tool_raising_is_treated_as_degraded(monkeypatch):
    """graph_tool() has no internal try/except around retrieve_graph() --
    this sub-agent must defensively catch a stray exception itself."""
    rag_chunk = _rag_chunk()
    _stub_rag_tool(monkeypatch, _RAG_OK([rag_chunk]))
    _stub_graph_tool(monkeypatch, exc=RuntimeError("age connection reset"))
    _stub_call_llm(monkeypatch, "Status: open [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await case_summarization(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.degraded_from == ["GRAPH"]
    assert result.tools_used == ["RAG"]


# ═══════════════════════════════════════════════════════════════════════
# (c) GRAPH-only degradation (RAG empty) -- disclosure prepended, ordered
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rag_empty_degrades_to_graph_only_with_disclosure_after_verification(monkeypatch):
    graph_chunk = _graph_chunk()
    _stub_rag_tool(monkeypatch, _RAG_EMPTY)
    _stub_graph_tool(monkeypatch, _GRAPH_OK([graph_chunk]))
    raw_answer = "P-1 linked to V-1 [Document 1]."
    _stub_call_llm(monkeypatch, raw_answer)
    captured = _stub_verify_grounding(monkeypatch, grounded=True)

    result = await case_summarization(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == ["GRAPH"]
    assert result.degraded_from == ["RAG"]
    # [PRESERVE -- §2.1.2 step 3] Verifier ran over evidentiary content ONLY
    # -- the disclosure text must not have been part of what was verified.
    assert captured["answer"] == raw_answer
    assert GRAPH_ONLY_SUMMARY_DISCLOSURE not in captured["answer"]
    # [PRESERVE -- §2.1.2 step 4] Disclosure PREPENDED post-verification,
    # verbatim, never paraphrased/regenerated.
    assert result.answer_text.startswith(GRAPH_ONLY_SUMMARY_DISCLOSURE)
    assert result.answer_text.endswith(raw_answer)
    assert len(result.citations) == 1
    assert result.citations[0].source_tool == "GRAPH"


@pytest.mark.asyncio
async def test_rag_failed_also_degrades_to_graph_only(monkeypatch):
    graph_chunk = _graph_chunk()
    err = ToolError(kind="upstream_failure", message="vector store unreachable")
    _stub_rag_tool(monkeypatch, RagToolResult(status=ToolStatus.FAILED, error=err))
    _stub_graph_tool(monkeypatch, _GRAPH_OK([graph_chunk]))
    _stub_call_llm(monkeypatch, "P-1 linked to V-1 [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await case_summarization(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.degraded_from == ["RAG"]
    assert result.tools_used == ["GRAPH"]
    assert result.answer_text.startswith(GRAPH_ONLY_SUMMARY_DISCLOSURE)


# ═══════════════════════════════════════════════════════════════════════
# (d) both empty -- status=EMPTY, not an error
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_both_empty_returns_empty_not_error(monkeypatch):
    _stub_rag_tool(monkeypatch, _RAG_EMPTY)
    _stub_graph_tool(monkeypatch, _GRAPH_EMPTY)

    result = await case_summarization(_agent_input())

    assert result.status == SubAgentStatus.EMPTY
    assert result.answer_text is None
    assert result.error is None
    assert result.caveats


@pytest.mark.asyncio
async def test_both_failed_also_returns_empty(monkeypatch):
    _stub_rag_tool(monkeypatch, exc=RuntimeError("rag infra down"))
    _stub_graph_tool(monkeypatch, exc=RuntimeError("graph infra down"))

    result = await case_summarization(_agent_input())

    assert result.status == SubAgentStatus.EMPTY
    assert result.answer_text is None


# ═══════════════════════════════════════════════════════════════════════
# (e) Verifier rejection on the GRAPH-only path -> ABSTAINED, no disclosure
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_verifier_rejection_on_graph_only_path_abstains_without_disclosure(monkeypatch):
    graph_chunk = _graph_chunk()
    _stub_rag_tool(monkeypatch, _RAG_EMPTY)
    _stub_graph_tool(monkeypatch, _GRAPH_OK([graph_chunk]))
    _stub_call_llm(monkeypatch, "I cannot answer that.")
    _stub_verify_grounding(monkeypatch, grounded=False, off_topic=True, reason="ungrounded")

    result = await case_summarization(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.citations == []
    assert result.tools_used == []
    assert result.degraded_from == []


@pytest.mark.asyncio
async def test_verifier_rejection_on_rag_only_path_abstains(monkeypatch):
    rag_chunk = _rag_chunk()
    _stub_rag_tool(monkeypatch, _RAG_OK([rag_chunk]))
    _stub_graph_tool(monkeypatch, _GRAPH_EMPTY)
    _stub_call_llm(monkeypatch, "I cannot answer that.")
    _stub_verify_grounding(monkeypatch, grounded=False, reason="unsupported claim")

    result = await case_summarization(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None


@pytest.mark.asyncio
async def test_verifier_rejection_on_combined_path_abstains(monkeypatch):
    rag_chunk = _rag_chunk()
    graph_chunk = _graph_chunk()
    _stub_rag_tool(monkeypatch, _RAG_OK([rag_chunk]))
    _stub_graph_tool(monkeypatch, _GRAPH_OK([graph_chunk]))
    _stub_call_llm(monkeypatch, "I cannot answer that.")
    _stub_verify_grounding(monkeypatch, grounded=False, reason="off topic")

    result = await case_summarization(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None


@pytest.mark.asyncio
async def test_generation_failure_abstains(monkeypatch):
    graph_chunk = _graph_chunk()
    _stub_rag_tool(monkeypatch, _RAG_EMPTY)
    _stub_graph_tool(monkeypatch, _GRAPH_OK([graph_chunk]))
    _stub_call_llm(monkeypatch, exc=RuntimeError("llm unreachable"))

    result = await case_summarization(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None


# ═══════════════════════════════════════════════════════════════════════
# (f) Supervisor integration + self-registration
# ═══════════════════════════════════════════════════════════════════════

def test_case_summarization_is_registered_under_its_own_name():
    assert get_registered(CASE_SUMMARIZATION) is case_summarization


@pytest.mark.asyncio
async def test_supervisor_dispatches_to_real_case_summarization_and_real_tools(monkeypatch):
    """
    Supervisor.handle() -> real Case Summarization -> real rag_tool()/
    graph_tool(), with router.route_query() and each tool's own underlying
    retrieval functions stubbed to deterministic test data (not live
    infra). Proves the Supervisor and this sub-agent actually connect.
    """
    import src.pipeline.harness.supervisor as supervisor_mod

    async def _fake_route_query(query_text: str) -> dict:
        return {"route": "GRAPH", "output_format": "chat"}

    monkeypatch.setattr(supervisor_mod, "route_query", _fake_route_query)

    rag_chunk = _rag_chunk()
    graph_chunk = _graph_chunk()
    _stub_rag_tool(monkeypatch, _RAG_OK([rag_chunk]))
    _stub_graph_tool(monkeypatch, _GRAPH_OK([graph_chunk]))
    _stub_call_llm(monkeypatch, "Status: open [Document 1]. P-1 linked to V-1 [Document 2].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    sup = Supervisor()  # no override -> real module-level registry
    result = await sup.handle(_agent_input(query_text="summarize case CASE-001"))

    assert result.status == SubAgentStatus.OK
    assert result.tools_used == ["RAG", "GRAPH"]
    assert len(result.citations) == 2
