"""
Investigative Analysis sub-agent — contract tests.

The case worth guarding hardest is THE COLLAPSE (RESOLVED-4): GRAPH and SQL
both degrade *to RAG*, so a three-tool call can leave one effective source.
Reporting three would tell the supervisor that three independent sources agreed
when only one did. `tools_used=["RAG"]`, `degraded_from=["GRAPH","SQL"]`.

Production boundaries are mocked, so these need no database, model server, or
network.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness import supervisor
from src.pipeline.harness.agents import investigative_analysis
from src.pipeline.harness.contracts import (
    CallerContext,
    Citation,
    EvidenceChunk,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)
from src.pipeline.harness.events import EventRecorder, build_degradation_trace
from src.pipeline.harness.tools import registry
from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER


@pytest.fixture(autouse=True)
def _real_tools():
    registry.use_real()
    yield
    registry.use_real()


def _caller() -> CallerContext:
    return CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id="CASE-A")


def _input(query: str = "Analyse this case") -> SubAgentInput:
    return SubAgentInput(query_text=query, caller=_caller())


_DOC = {"id": "d1", "text": "FIR narrative.", "metadata": {"case_id": "CASE-A"}, "rrf_score": 0.9}
_NODE = {"id": "g1", "text": "Vehicle linked.", "metadata": {"case_id": "CASE-A"}}
_ROW = {"id": 1, "category": "theft", "subject": "Theft", "section_ref": "PPC 380"}


@pytest.fixture
def legs(monkeypatch, gateway):
    """
    Drive all three tools independently.

    Each leg is configured by outcome, not by mechanics, so a test reads as
    "GRAPH fell back" rather than as a pile of patches.
    """
    def _configure(rag="ok", graph="ok", sql="ok", rag_evaluator_raises=False):
        # ── RAG ──
        async def _expand(_q, n=2):
            return []

        async def _variant(_q):
            return None

        async def _embed(_q, **k):
            return [0.0, 0.1]

        rag_chunks = [_DOC] if rag == "ok" else []

        async def _query_similar(*a, **k):
            return list(rag_chunks)

        async def _all_chunks(**k):
            return list(rag_chunks)

        async def _cross_rerank(_q, candidates, top_k=None):
            return list(candidates)

        async def _evaluate(*a, **k):
            if rag_evaluator_raises:
                raise RuntimeError("evaluator endpoint timed out")
            return {"relevant": bool(rag_chunks), "reason": "ok"}

        monkeypatch.setattr("src.pipeline.query_expander.expand_query", _expand)
        monkeypatch.setattr(
            "src.pipeline.cross_script_variant.generate_cross_script_variant", _variant)
        monkeypatch.setattr("src.retrieval.embedder.embed_text", _embed)
        monkeypatch.setattr("src.retrieval.vector_store.query_similar", _query_similar)
        monkeypatch.setattr("src.retrieval.vector_store.get_all_chunks", _all_chunks)
        monkeypatch.setattr(
            "src.retrieval.bm25_retriever.retrieve_bm25", lambda *a, **k: list(rag_chunks))
        monkeypatch.setattr(
            "src.retrieval.reranker.rerank_results", lambda *a, **k: list(rag_chunks))
        monkeypatch.setattr("src.retrieval.cross_reranker.cross_rerank", _cross_rerank)
        monkeypatch.setattr("src.pipeline.evaluator.evaluate_relevance", _evaluate)

        # ── GRAPH ──
        async def _retrieve(*a, **k):
            nodes = [_NODE] if graph == "ok" else []
            return {
                "chunks": nodes, "hop_count": 1 if nodes else 0,
                "compounded_confidence": 0.8 if nodes else 1.0,
                "seed_entities": [{"entity_id": "E1"}] if nodes else [],
                "unconfirmed_links": [],
            }

        monkeypatch.setattr("src.retrieval.graph_retriever.retrieve_graph", _retrieve)

        # ── SQL ──
        async def _params(_q):
            return {"category": "theft", "subject": None, "section_ref": None}

        monkeypatch.setattr("src.pipeline.sql_extractor.extract_sql_params", _params)
        gateway.police_reference_data = [_ROW] if sql == "ok" else []

        return gateway

    return _configure


# ── All three succeed ────────────────────────────────────────────────────

async def test_all_three_contribute_is_ok(legs, gateway):
    legs(rag="ok", graph="ok", sql="ok")

    result = await investigative_analysis.run(_input(), gateway=gateway)

    assert result.status is SubAgentStatus.OK
    assert sorted(result.tools_used) == ["GRAPH", "RAG", "SQL"]
    assert result.degraded_from == []
    assert result.answer_text


async def test_citations_roll_up_across_all_three_sources(legs, gateway):
    """One answer, citations spanning every contributing source."""
    legs(rag="ok", graph="ok", sql="ok")

    result = await investigative_analysis.run(_input(), gateway=gateway)

    assert {c.source_tool for c in result.citations} == {"RAG", "GRAPH", "SQL"}
    # One continuous numbering, not three restarting sequences.
    assert [c.document_index for c in result.citations] == list(
        range(1, len(result.citations) + 1)
    )


# ── THE COLLAPSE CASE — RESOLVED-4's reason for existing ─────────────────

async def test_graph_and_sql_collapse_to_rag(legs, gateway):
    """
    Both fall back. Only RAG contributed. Reporting three would tell the
    supervisor that three independent sources agreed when one did.
    """
    legs(rag="ok", graph="empty", sql="empty")

    result = await investigative_analysis.run(_input(), gateway=gateway)

    assert result.tools_used == ["RAG"]
    assert result.degraded_from == ["GRAPH", "SQL"]
    assert result.status is SubAgentStatus.PARTIAL


async def test_collapse_never_reports_three_tools(legs, gateway):
    legs(rag="ok", graph="empty", sql="empty")

    result = await investigative_analysis.run(_input(), gateway=gateway)

    assert len(result.tools_used) == 1
    assert "GRAPH" not in result.tools_used
    assert "SQL" not in result.tools_used


async def test_collapse_cites_only_the_contributing_source(legs, gateway):
    legs(rag="ok", graph="empty", sql="empty")

    result = await investigative_analysis.run(_input(), gateway=gateway)

    assert {c.source_tool for c in result.citations} == {"RAG"}


@pytest.mark.parametrize("rag,graph,sql,used,degraded", [
    ("ok", "ok", "empty", ["RAG", "GRAPH"], ["SQL"]),
    ("ok", "empty", "ok", ["RAG", "SQL"], ["GRAPH"]),
    ("empty", "ok", "ok", ["GRAPH", "SQL"], ["RAG"]),
])
async def test_partial_combinations(rag, graph, sql, used, degraded, legs, gateway):
    legs(rag=rag, graph=graph, sql=sql)

    result = await investigative_analysis.run(_input(), gateway=gateway)

    assert result.tools_used == used
    assert result.degraded_from == degraded
    assert result.status is SubAgentStatus.PARTIAL


# ── The overlap: contributed AND degraded ────────────────────────────────

async def test_internally_degraded_tool_appears_in_both_lists(legs, gateway):
    """
    RAG returns evidence but its relevance gate could not run (c4a06fe). It
    contributed, so it stays in tools_used — and it degraded, so it is also in
    degraded_from. The one legitimate overlap.
    """
    legs(rag="ok", graph="ok", sql="ok", rag_evaluator_raises=True)

    result = await investigative_analysis.run(_input(), gateway=gateway)

    assert "RAG" in result.tools_used
    assert "RAG" in result.degraded_from
    assert result.status is SubAgentStatus.PARTIAL
    assert result.caveats, "the nested degradation caveat was swallowed"


# ── All three fail ───────────────────────────────────────────────────────

async def test_all_three_empty_abstains(legs, gateway):
    legs(rag="empty", graph="empty", sql="empty")

    result = await investigative_analysis.run(_input(), gateway=gateway)

    assert result.status is SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.tools_used == []
    assert sorted(result.degraded_from) == ["GRAPH", "RAG", "SQL"]


async def test_failing_verifier_abstains(legs, gateway):
    legs(rag="ok", graph="ok", sql="ok")

    result = await investigative_analysis.run(
        _input(query=f"{UNGROUNDED_TRIGGER} analyse"), gateway=gateway
    )

    assert result.status is SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.citations == []


# ── Bounded payload ──────────────────────────────────────────────────────

def test_subagent_result_has_no_field_that_can_hold_evidence():
    offenders = [
        name for name, field in SubAgentResult.model_fields.items()
        if "EvidenceChunk" in repr(field.annotation)
    ]
    assert not offenders, (
        f"SubAgentResult fields {offenders} can hold EvidenceChunk objects. "
        "Design §3: the bounded payload must never carry raw evidence upward."
    )


async def test_handoff_carries_citations_not_three_result_sets(legs, gateway):
    """§2's explicit warning: one synthesized answer, never three raw sets."""
    legs(rag="ok", graph="ok", sql="ok")

    result = await investigative_analysis.run(_input(), gateway=gateway)

    assert all(isinstance(c, Citation) for c in result.citations)
    assert not any(isinstance(c, EvidenceChunk) for c in result.citations)
    assert isinstance(result.answer_text, str)


async def test_citations_carry_no_chunk_text(legs, gateway):
    legs(rag="ok", graph="ok", sql="ok")

    result = await investigative_analysis.run(_input(), gateway=gateway)

    for citation in result.citations:
        serialized = citation.model_dump_json()
        for body in (_DOC["text"], _NODE["text"]):
            assert body[:20] not in serialized


# ── [RESOLVED-4a] live per-source trace events ───────────────────────────

async def test_emits_one_event_per_source_outcome(legs, gateway):
    """
    The roll-up is after-the-fact. Without per-source events a user sees
    nothing until all three resolve, then only the bundle.
    """
    legs(rag="ok", graph="empty", sql="empty")

    recorder = EventRecorder()
    await investigative_analysis.run(_input(), events=recorder, gateway=gateway)

    steps = [e.step for e in recorder.events]
    assert "analysis:rag" in steps
    assert "analysis:graph" in steps
    assert "analysis:sql" in steps


async def test_per_source_events_distinguish_contributed_from_fell_back(legs, gateway):
    legs(rag="ok", graph="empty", sql="empty")

    recorder = EventRecorder()
    await investigative_analysis.run(_input(), events=recorder, gateway=gateway)

    by_step = {e.step: e.status for e in recorder.events if e.step.startswith("analysis:")}
    assert by_step["analysis:rag"] == "done"
    assert by_step["analysis:graph"] == "retry"
    assert by_step["analysis:sql"] == "retry"


async def test_per_source_events_use_only_the_sse_vocabulary(legs, gateway):
    legs(rag="ok", graph="empty", sql="empty")

    recorder = EventRecorder()
    await investigative_analysis.run(_input(), events=recorder, gateway=gateway)

    allowed = {"active", "done", "error", "retry", "skipped"}
    assert {e.status for e in recorder.events} <= allowed


# ── Automatic tracing, no extra wiring ───────────────────────────────────

async def test_traced_automatically_without_extra_wiring(legs, gateway):
    """
    Same guarantee as test_a_new_sub_agent_persists_a_trace_without_extra_wiring:
    registered as a node, traced by construction. Nothing in this sub-agent
    calls build_degradation_trace().
    """
    legs(rag="ok", graph="empty", sql="empty")

    supervisor._NODES[investigative_analysis.NAME] = investigative_analysis.run
    original = supervisor._route
    try:
        supervisor._route = lambda _i: investigative_analysis.NAME
        recorder = EventRecorder()
        state = await supervisor.invoke(_input(), events=recorder)
    finally:
        supervisor._route = original
        supervisor._NODES.pop(investigative_analysis.NAME, None)

    traced = [e for e in recorder.events if getattr(e, "trace", None)]
    assert len(traced) == 1

    trace = traced[0].trace
    assert trace["contributed_only"] == ["RAG"]
    assert trace["degraded_only"] == ["GRAPH", "SQL"]
    assert state.result.status is SubAgentStatus.PARTIAL


async def test_trace_persists_the_collapse_shape(legs, gateway):
    """The collapse survives into the durable per-message trace intact."""
    legs(rag="ok", graph="empty", sql="empty")

    result = await investigative_analysis.run(_input(), gateway=gateway)
    trace = build_degradation_trace(result)

    await gateway.save_message("s1", "assistant", "answer", degradation_trace=trace)
    stored = (await gateway.get_session_history("s1"))[0]["degradation_trace"]

    assert stored["contributed_only"] == ["RAG"]
    assert stored["degraded_only"] == ["GRAPH", "SQL"]
