"""
Tests for src/pipeline/harness/agents/investigative_analysis.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 6, "Phase 7").

Covers:
  (a) full success -- all three tools contribute -> status=OK, flattened
      RAG-then-GRAPH-then-SQL citation order, tools_used=["RAG","GRAPH","SQL"],
      degraded_from=[];
  (b) one or two tools degrade to RAG, with correct dedup -> status=PARTIAL,
      tools_used never lists RAG more than once regardless of how many
      siblings degraded toward it;
  (c) all three fail/empty -> status=ABSTAINED (NOT EMPTY -- this row's own
      explicit RESOLVED-4 text, deliberately unlike Case Summarization);
  (d) the live per-source PipelineEvent sequence (RESOLVED-4a/§2.1.4) --
      one event per tool outcome, using the five-value SSE vocabulary, and
      agreeing with the roll-up whenever a result actually stands;
  (e) a Verifier rejection -> ABSTAINED with tools_used/degraded_from reset
      to [] even though the per-tool events already said "done" -- the
      documented, deliberate divergence for a discarded draft;
  (f) module-level self-registration, and a Supervisor.handle() -> real
      Investigative Analysis -> real rag_tool()/graph_tool()/sql_tool()
      integration test via the SQL route (the only route this sub-agent is
      reachable through today -- see module docstring's classification-
      reachability note).

`rag_tool`, `graph_tool`, `sql_tool`, `call_llm`, and `verify_grounding` are
monkeypatched at the module level (`ia_mod.*`) in every test -- none of
these hit live infra.
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.agents.investigative_analysis as ia_mod
from src.pipeline.harness.agents.investigative_analysis import investigative_analysis
from src.pipeline.harness.supervisor import (
    INVESTIGATIVE_ANALYSIS,
    Supervisor,
    get_registered,
)
from src.pipeline.harness.tools.graph import GraphToolResult
from src.pipeline.harness.tools.rag import RagToolResult
from src.pipeline.harness.tools.sql import SqlToolResult
from src.pipeline.harness.types import (
    CallerContext,
    ChunkMetadata,
    EvidenceChunk,
    ExecutionContext,
    PipelineEvent,
    Role,
    SubAgentInput,
    SubAgentStatus,
    ToolError,
    ToolStatus,
)


def _rag_chunk(id_="r1", text="the suspect fled the scene", case_id="CASE-001", source="doc.pdf"):
    return EvidenceChunk(
        id=id_, text=text, metadata=ChunkMetadata(source_tool="RAG", case_id=case_id, source_file=source)
    )


def _graph_chunk(id_="g1", text="Person P-1 is linked to Vehicle V-1", case_id="CASE-001"):
    return EvidenceChunk(id=id_, text=text, metadata=ChunkMetadata(source_tool="GRAPH", case_id=case_id))


def _sql_chunk(id_="sql-row-1", text="{'section_ref': '302'}"):
    return EvidenceChunk(id=id_, text=text, metadata=ChunkMetadata(source_tool="SQL"))


def _caller(case_id="CASE-001", role=Role.INVESTIGATOR, **kw):
    return CallerContext(user_id="u1", role=role, active_case_id=case_id, **kw)


def _execution(caller=None):
    return ExecutionContext(caller=caller or _caller())


def _agent_input(caller=None, query_text="what section applies and who was involved?", **kw):
    return SubAgentInput(query_text=query_text, execution=_execution(caller=caller), **kw)


def _stub_tool(monkeypatch, attr_name: str, result=None, exc=None):
    async def _fake(tool_input):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(ia_mod, attr_name, _fake)


def _stub_call_llm(monkeypatch, answer="Synthesized finding [Document 1].", exc=None):
    async def _fake(system_prompt, user_message, **kwargs):
        if exc is not None:
            raise exc
        return answer

    monkeypatch.setattr(ia_mod, "call_llm", _fake)


def _stub_validate_answer(monkeypatch, status=None, claims=None):
    """
    Stubs the Validation gate at the boundary this module actually calls
    (`ia_mod.validate_answer`) -- same mocking discipline as
    `_stub_verify_grounding` below. Defaults to PASSED/[] so tests that are
    not specifically about Validation's own caveat-appending behavior see no
    change from before this gate was wired in.
    """
    from src.pipeline.harness.types import ValidationStatus

    resolved_status = status if status is not None else ValidationStatus.PASSED
    resolved_claims = claims if claims is not None else []

    async def _fake(*args, **kwargs):
        return resolved_status, resolved_claims

    monkeypatch.setattr(ia_mod, "validate_answer", _fake)


def _stub_verify_grounding(monkeypatch, grounded: bool, off_topic: bool = False, reason: str = "ok"):
    captured = {}

    async def _fake(answer, cited_chunks, case_id, cross_case_ids=None, target_date=None):
        captured["answer"] = answer
        captured["cited_chunks"] = cited_chunks
        return {
            "grounded": grounded,
            "off_topic": off_topic,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": reason,
        }

    monkeypatch.setattr(ia_mod, "verify_grounding", _fake)
    return captured


_RAG_OK = lambda chunks: RagToolResult(status=ToolStatus.OK, chunks=chunks)
_RAG_EMPTY = RagToolResult(status=ToolStatus.EMPTY)
_GRAPH_OK = lambda chunks: GraphToolResult(status=ToolStatus.OK, chunks=chunks)
_GRAPH_FALLBACK = GraphToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True)
_SQL_OK = lambda chunks: SqlToolResult(status=ToolStatus.OK, chunks=chunks, row_count=len(chunks))
_SQL_FALLBACK = SqlToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True, row_count=0)


# ═══════════════════════════════════════════════════════════════════════
# (a) full success -- all three tools contribute
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_full_success_flattens_rag_then_graph_then_sql_and_returns_ok(monkeypatch):
    rag_chunk, graph_chunk, sql_chunk = _rag_chunk(), _graph_chunk(), _sql_chunk()
    _stub_tool(monkeypatch, "rag_tool", _RAG_OK([rag_chunk]))
    _stub_tool(monkeypatch, "graph_tool", _GRAPH_OK([graph_chunk]))
    _stub_tool(monkeypatch, "sql_tool", _SQL_OK([sql_chunk]))
    _stub_call_llm(monkeypatch, "Finding A [Document 1]. Finding B [Document 2]. Section 302 [Document 3].")
    _stub_validate_answer(monkeypatch)
    captured = _stub_verify_grounding(monkeypatch, grounded=True)

    result = await investigative_analysis(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert result.tools_used == ["RAG", "GRAPH", "SQL"]
    assert result.degraded_from == []
    assert result.caveats == []
    assert len(result.citations) == 3
    assert [c.source_tool for c in result.citations] == ["RAG", "GRAPH", "SQL"]
    assert [c["id"] for c in captured["cited_chunks"]] == ["r1", "g1", "sql-row-1"]
    for citation in result.citations:
        assert not hasattr(citation, "text")


@pytest.mark.asyncio
async def test_target_entity_threaded_to_graph_tool(monkeypatch):
    """[Reconciliation fix — harness-reconciliation Unit 8] target_entity
    was previously hardcoded None on the GRAPH call -- an entity-focused
    analytical question had no traversal anchor. Regression guard."""
    calls = []

    async def _fake_graph(tool_input):
        calls.append(tool_input)
        return _GRAPH_OK([_graph_chunk()])

    monkeypatch.setattr(ia_mod, "graph_tool", _fake_graph)
    _stub_tool(monkeypatch, "rag_tool", _RAG_OK([_rag_chunk()]))
    _stub_tool(monkeypatch, "sql_tool", _SQL_OK([_sql_chunk()]))
    _stub_call_llm(monkeypatch, "Finding [Document 1].")
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)

    await investigative_analysis(_agent_input(target_entity="ABC-123"))

    assert len(calls) == 1
    assert calls[0].target_entity == "ABC-123"


@pytest.mark.asyncio
async def test_validation_gate_runs_full_tier_and_surfaces_issues_as_caveats(monkeypatch):
    from src.pipeline.harness.types import ClaimSupport, ValidationClaimResult, ValidationStatus

    _stub_tool(monkeypatch, "rag_tool", _RAG_OK([_rag_chunk()]))
    _stub_tool(monkeypatch, "graph_tool", _GRAPH_OK([_graph_chunk()]))
    _stub_tool(monkeypatch, "sql_tool", _SQL_OK([_sql_chunk()]))
    _stub_call_llm(monkeypatch, "Finding A [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    flagged = ValidationClaimResult(
        document_index=1,
        claim_excerpt="Finding A",
        support=ClaimSupport.NOT_SUPPORTED,
        reason="Not stated by the source.",
    )
    captured_tier = {}

    async def _fake_validate(answer_text, cited_chunks, *, tier):
        captured_tier["tier"] = tier
        return ValidationStatus.ISSUES_FOUND, [flagged]

    monkeypatch.setattr(ia_mod, "validate_answer", _fake_validate)

    result = await investigative_analysis(_agent_input())

    # [PRESERVE -- plan §5's table] MANDATORY full tier here, not structural.
    assert captured_tier["tier"] == "full"
    assert result.status == SubAgentStatus.OK  # caveat-only, never blocking
    assert result.validation_status == ValidationStatus.ISSUES_FOUND
    assert result.validation_claims == [flagged]
    assert any("Not stated by the source" in c for c in result.caveats)


# ═══════════════════════════════════════════════════════════════════════
# (b) degradation + dedup
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_graph_and_sql_both_fall_back_to_rag_dedup_to_one_rag_entry(monkeypatch):
    rag_chunk = _rag_chunk()
    _stub_tool(monkeypatch, "rag_tool", _RAG_OK([rag_chunk]))
    _stub_tool(monkeypatch, "graph_tool", _GRAPH_FALLBACK)
    _stub_tool(monkeypatch, "sql_tool", _SQL_FALLBACK)
    _stub_call_llm(monkeypatch, "The suspect fled [Document 1].")
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await investigative_analysis(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    # [RESOLVED-4] Never three, never RAG counted twice -- exactly one RAG
    # entry even though BOTH siblings fell back toward it.
    assert result.tools_used == ["RAG"]
    assert result.degraded_from == ["GRAPH", "SQL"]
    assert len(result.citations) == 1
    assert result.citations[0].source_tool == "RAG"
    assert result.caveats  # names what was unavailable


@pytest.mark.asyncio
async def test_only_graph_degrades_sql_and_rag_contribute(monkeypatch):
    rag_chunk, sql_chunk = _rag_chunk(), _sql_chunk()
    _stub_tool(monkeypatch, "rag_tool", _RAG_OK([rag_chunk]))
    _stub_tool(monkeypatch, "graph_tool", GraphToolResult(status=ToolStatus.FAILED, fallback_to_rag=True))
    _stub_tool(monkeypatch, "sql_tool", _SQL_OK([sql_chunk]))
    _stub_call_llm(monkeypatch, "Finding [Document 1]. Section 302 [Document 2].")
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await investigative_analysis(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == ["RAG", "SQL"]
    assert result.degraded_from == ["GRAPH"]
    assert len(result.citations) == 2


@pytest.mark.asyncio
async def test_graph_tool_raising_is_treated_as_degraded(monkeypatch):
    """graph_tool() has no internal try/except around retrieve_graph() --
    this sub-agent must defensively catch a stray exception itself (same
    asymmetry case_summarization.py already documents)."""
    rag_chunk = _rag_chunk()
    _stub_tool(monkeypatch, "rag_tool", _RAG_OK([rag_chunk]))
    _stub_tool(monkeypatch, "graph_tool", exc=RuntimeError("age connection reset"))
    _stub_tool(monkeypatch, "sql_tool", _SQL_FALLBACK)
    _stub_call_llm(monkeypatch, "Finding [Document 1].")
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await investigative_analysis(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == ["RAG"]
    assert result.degraded_from == ["GRAPH", "SQL"]


# ═══════════════════════════════════════════════════════════════════════
# (c) all three fail/empty -> ABSTAINED, not EMPTY
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_all_three_empty_returns_abstained_not_empty(monkeypatch):
    _stub_tool(monkeypatch, "rag_tool", _RAG_EMPTY)
    _stub_tool(monkeypatch, "graph_tool", _GRAPH_FALLBACK)
    _stub_tool(monkeypatch, "sql_tool", _SQL_FALLBACK)

    result = await investigative_analysis(_agent_input())

    # [RESOLVED-4, this row's own explicit text] ABSTAINED, deliberately NOT
    # EMPTY -- unlike Case Summarization's "both empty -> EMPTY" convention.
    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.tools_used == []
    assert result.caveats


@pytest.mark.asyncio
async def test_all_three_raising_exceptions_also_returns_abstained(monkeypatch):
    _stub_tool(monkeypatch, "rag_tool", exc=RuntimeError("rag infra down"))
    _stub_tool(monkeypatch, "graph_tool", exc=RuntimeError("graph infra down"))
    _stub_tool(monkeypatch, "sql_tool", exc=RuntimeError("sql infra down"))

    result = await investigative_analysis(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None


# ═══════════════════════════════════════════════════════════════════════
# (d) live per-source PipelineEvent sequence
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_live_events_one_per_source_tool_outcome(monkeypatch):
    rag_chunk = _rag_chunk()
    _stub_tool(monkeypatch, "rag_tool", _RAG_OK([rag_chunk]))
    _stub_tool(monkeypatch, "graph_tool", _GRAPH_FALLBACK)
    _stub_tool(monkeypatch, "sql_tool", SqlToolResult(status=ToolStatus.FAILED, fallback_to_rag=True, row_count=0))
    _stub_call_llm(monkeypatch, "Finding [Document 1].")
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)

    events: list[PipelineEvent] = []
    await investigative_analysis(_agent_input(), on_event=events.append)

    # Exactly one event per tool -- never batched into a single "ran" event.
    assert len(events) == 3
    by_step = {evt.step: evt for evt in events}
    assert set(by_step) == {"analysis:rag", "analysis:graph", "analysis:sql"}
    for evt in events:
        assert isinstance(evt, PipelineEvent)
        assert evt.status in ("active", "done", "error", "retry", "skipped")
        assert isinstance(evt.detail, str) and evt.detail

    # [RESOLVED-4a] Existing five-value SSE vocabulary, no new values.
    assert by_step["analysis:rag"].status == "done"
    assert by_step["analysis:graph"].status == "retry"  # fell back to RAG
    assert by_step["analysis:sql"].status == "retry"  # fell back to RAG


@pytest.mark.asyncio
async def test_hard_failure_with_no_fallback_signal_reads_as_error(monkeypatch):
    """A tool that degrades with fallback_to_rag=False (RAG's own case,
    since it is the fallback TARGET and has none of its own) reads as
    'error', not 'retry' -- matching §2.1.4's own third illustrative
    example."""
    _stub_tool(monkeypatch, "rag_tool", RagToolResult(status=ToolStatus.EMPTY))
    _stub_tool(monkeypatch, "graph_tool", _GRAPH_FALLBACK)
    _stub_tool(monkeypatch, "sql_tool", _SQL_FALLBACK)

    events: list[PipelineEvent] = []
    await investigative_analysis(_agent_input(), on_event=events.append)

    by_step = {evt.step: evt for evt in events}
    assert by_step["analysis:rag"].status == "error"


@pytest.mark.asyncio
async def test_on_event_is_optional_and_events_still_resolve_without_a_sink(monkeypatch):
    _stub_tool(monkeypatch, "rag_tool", _RAG_OK([_rag_chunk()]))
    _stub_tool(monkeypatch, "graph_tool", _GRAPH_FALLBACK)
    _stub_tool(monkeypatch, "sql_tool", _SQL_FALLBACK)
    _stub_call_llm(monkeypatch, "Finding [Document 1].")
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await investigative_analysis(_agent_input())  # no on_event given

    assert result.status == SubAgentStatus.PARTIAL


# ═══════════════════════════════════════════════════════════════════════
# (e) Verifier rejection -- events already said "done", roll-up resets
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_verifier_rejection_abstains_and_resets_tools_used_despite_done_events(monkeypatch):
    rag_chunk, graph_chunk = _rag_chunk(), _graph_chunk()
    _stub_tool(monkeypatch, "rag_tool", _RAG_OK([rag_chunk]))
    _stub_tool(monkeypatch, "graph_tool", _GRAPH_OK([graph_chunk]))
    _stub_tool(monkeypatch, "sql_tool", _SQL_FALLBACK)
    _stub_call_llm(monkeypatch, "I cannot answer that.")
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=False, reason="unsupported claim")

    events: list[PipelineEvent] = []
    result = await investigative_analysis(_agent_input(), on_event=events.append)

    # The per-tool events reflect what genuinely happened at the tool level
    # -- RAG and GRAPH really did contribute data.
    by_step = {evt.step: evt for evt in events}
    assert by_step["analysis:rag"].status == "done"
    assert by_step["analysis:graph"].status == "done"

    # But the generated answer was rejected and discarded entirely -- the
    # documented, deliberate divergence: no answer is served, and the
    # roll-up does not assert those tools backed a served answer, matching
    # every other sub-agent's verifier-rejection convention.
    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.tools_used == []
    assert result.degraded_from == []
    assert result.citations == []


@pytest.mark.asyncio
async def test_generation_failure_abstains(monkeypatch):
    _stub_tool(monkeypatch, "rag_tool", _RAG_OK([_rag_chunk()]))
    _stub_tool(monkeypatch, "graph_tool", _GRAPH_FALLBACK)
    _stub_tool(monkeypatch, "sql_tool", _SQL_FALLBACK)
    _stub_call_llm(monkeypatch, exc=RuntimeError("llm unreachable"))
    _stub_validate_answer(monkeypatch)

    result = await investigative_analysis(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None


# ═══════════════════════════════════════════════════════════════════════
# (f) Supervisor integration + self-registration
# ═══════════════════════════════════════════════════════════════════════

def test_investigative_analysis_is_registered_under_its_own_name():
    assert get_registered(INVESTIGATIVE_ANALYSIS) is investigative_analysis


@pytest.mark.asyncio
async def test_supervisor_dispatches_to_real_investigative_analysis_and_real_tools(monkeypatch):
    """
    Supervisor.handle() -> real Investigative Analysis -> real rag_tool()/
    graph_tool()/sql_tool(), with router.route_query() and each tool's own
    underlying calls stubbed to deterministic test data. Uses the SQL
    route -- the only route this sub-agent is reachable through today (see
    module docstring's classification-reachability note; `_ROUTE_TO_SUBAGENT`
    maps RAG->Semantic Search and GRAPH/GRAPH_HYBRID->Case Summarization,
    not to this sub-agent).
    """
    import src.pipeline.harness.supervisor as supervisor_mod

    async def _fake_route_query(query_text: str) -> dict:
        return {"route": "SQL", "output_format": "chat"}

    monkeypatch.setattr(supervisor_mod, "route_query", _fake_route_query)

    rag_chunk, graph_chunk, sql_chunk = _rag_chunk(), _graph_chunk(), _sql_chunk()
    _stub_tool(monkeypatch, "rag_tool", _RAG_OK([rag_chunk]))
    _stub_tool(monkeypatch, "graph_tool", _GRAPH_OK([graph_chunk]))
    _stub_tool(monkeypatch, "sql_tool", _SQL_OK([sql_chunk]))
    _stub_call_llm(monkeypatch, "Finding A [Document 1]. Finding B [Document 2]. Section 302 [Document 3].")
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)

    sup = Supervisor()  # no override -> real module-level registry
    events: list[PipelineEvent] = []
    result = await sup.handle(_agent_input(query_text="what section applies?"), on_event=events.append)

    assert result.status == SubAgentStatus.OK
    assert result.tools_used == ["RAG", "GRAPH", "SQL"]
    assert len(result.citations) == 3
    # Supervisor's own two events plus this sub-agent's three per-source
    # events -- the on_event sink threads all the way down, per the
    # pre-Phase-7 contract amendment.
    assert len(events) == 5
    per_source_steps = {evt.step for evt in events if evt.step.startswith("analysis:")}
    assert per_source_steps == {"analysis:rag", "analysis:graph", "analysis:sql"}
