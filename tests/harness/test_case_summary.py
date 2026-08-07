"""
Case Summarization sub-agent — contract tests.

Exercises the REAL tools (`tools/real.py`) with every production boundary
mocked, so these still need no database, model server, or network.

Covered: both legs succeeding, each degradation direction, the GRAPH-only
in-text disclosure, nested tool-internal degradation climbing to the
sub-agent's own caveats, and the structural bounded-payload assertion.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness.agents import case_summary
from src.pipeline.harness.contracts import (
    GRAPH_ONLY_SUMMARY_DISCLOSURE,
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


@pytest.fixture(autouse=True)
def _real_tools():
    """These target the real adapters, not the stubs conftest pins by default."""
    registry.use_real()
    yield
    registry.use_real()


def _caller() -> CallerContext:
    return CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id="CASE-A")


def _input(query: str = "Summarize this case") -> SubAgentInput:
    return SubAgentInput(query_text=query, caller=_caller())


_DOC_CHUNK = {
    "id": "d1", "text": "FIR narrative: forced entry at the rear service door.",
    "metadata": {"case_id": "CASE-A", "source_file": "FIR.pdf"}, "rrf_score": 0.9,
}
_GRAPH_CHUNK = {
    "id": "g1", "text": "Vehicle VEH-0091 linked to the incident.",
    "metadata": {"case_id": "CASE-A"}, "graph_confidence": 0.7,
}


@pytest.fixture
def rag_returns(monkeypatch):
    """Drive the real RAG tool's retrieval chain to a caller-chosen result."""
    def _configure(chunks, evaluator_raises=False):
        async def _expand(_q, n=2):
            return []

        async def _variant(_q):
            return None

        async def _embed(_q, **k):
            return [0.0, 0.1]

        async def _query_similar(*a, **k):
            return list(chunks)

        async def _all_chunks(**k):
            return list(chunks)

        async def _cross_rerank(_q, candidates, top_k=None):
            return list(candidates)[:top_k] if top_k else list(candidates)

        async def _evaluate(*a, **k):
            if evaluator_raises:
                raise RuntimeError("evaluator endpoint timed out")
            return {"relevant": bool(chunks), "reason": "ok"}

        monkeypatch.setattr("src.pipeline.query_expander.expand_query", _expand)
        monkeypatch.setattr(
            "src.pipeline.cross_script_variant.generate_cross_script_variant", _variant
        )
        monkeypatch.setattr("src.retrieval.embedder.embed_text", _embed)
        monkeypatch.setattr("src.retrieval.vector_store.query_similar", _query_similar)
        monkeypatch.setattr("src.retrieval.vector_store.get_all_chunks", _all_chunks)
        monkeypatch.setattr(
            "src.retrieval.bm25_retriever.retrieve_bm25", lambda *a, **k: list(chunks)
        )
        monkeypatch.setattr(
            "src.retrieval.reranker.rerank_results", lambda *a, **k: list(chunks)
        )
        monkeypatch.setattr("src.retrieval.cross_reranker.cross_rerank", _cross_rerank)
        monkeypatch.setattr("src.pipeline.evaluator.evaluate_relevance", _evaluate)

    return _configure


@pytest.fixture
def graph_returns(monkeypatch):
    """Drive the real GRAPH tool to a caller-chosen result."""
    def _configure(chunks):
        async def _retrieve(*a, **k):
            return {
                "chunks": list(chunks),
                "hop_count": 1 if chunks else 0,
                "compounded_confidence": 0.7 if chunks else 1.0,
                "seed_entities": [{"entity_id": "E1"}] if chunks else [],
                "unconfirmed_links": [],
            }

        monkeypatch.setattr("src.retrieval.graph_retriever.retrieve_graph", _retrieve)

    return _configure


# ── Happy path ───────────────────────────────────────────────────────────

async def test_both_tools_succeed(rag_returns, graph_returns):
    rag_returns([_DOC_CHUNK])
    graph_returns([_GRAPH_CHUNK])

    result = await case_summary.run(_input())

    assert result.status is SubAgentStatus.OK
    assert result.answer_text
    assert sorted(result.tools_used) == ["GRAPH", "RAG"]
    assert result.degraded_from == []
    assert result.caveats == []
    assert GRAPH_ONLY_SUMMARY_DISCLOSURE not in result.answer_text


async def test_both_tools_succeed_cites_both_sources(rag_returns, graph_returns):
    rag_returns([_DOC_CHUNK])
    graph_returns([_GRAPH_CHUNK])

    result = await case_summary.run(_input())

    assert {c.source_tool for c in result.citations} == {"RAG", "GRAPH"}


# ── Degradation: GRAPH empty → RAG-only ──────────────────────────────────

async def test_graph_empty_degrades_to_rag_only(rag_returns, graph_returns):
    rag_returns([_DOC_CHUNK])
    graph_returns([])

    result = await case_summary.run(_input())

    assert result.status is SubAgentStatus.PARTIAL
    assert result.tools_used == ["RAG"]
    assert result.degraded_from == ["GRAPH"]
    assert result.answer_text


async def test_graph_empty_adds_no_in_text_disclosure(rag_returns, graph_returns):
    """
    [RESOLVED-2a] Only the GRAPH-ONLY direction discloses in text. A
    document-based summary is the deliverable a reader expects by default, so
    status/degraded_from carry it — an extra line here would be noise.
    """
    rag_returns([_DOC_CHUNK])
    graph_returns([])

    result = await case_summary.run(_input())

    assert GRAPH_ONLY_SUMMARY_DISCLOSURE not in result.answer_text
    assert GRAPH_ONLY_SUMMARY_DISCLOSURE not in result.caveats


# ── Degradation: RAG empty → GRAPH-only, WITH disclosure ─────────────────

async def test_rag_empty_degrades_to_graph_only(rag_returns, graph_returns):
    rag_returns([])
    graph_returns([_GRAPH_CHUNK])

    result = await case_summary.run(_input())

    assert result.status is SubAgentStatus.PARTIAL
    assert result.tools_used == ["GRAPH"]
    assert result.degraded_from == ["RAG"]


async def test_rag_empty_puts_disclosure_in_the_summary_text(rag_returns, graph_returns):
    """
    [RESOLVED-2a] The disclosure must be IN THE TEXT, not only in metadata —
    a GRAPH-only summary is thinner and entity-shaped, and metadata does not
    travel with text that gets quoted, exported, or pasted elsewhere.
    """
    rag_returns([])
    graph_returns([_GRAPH_CHUNK])

    result = await case_summary.run(_input())

    assert GRAPH_ONLY_SUMMARY_DISCLOSURE in result.answer_text
    assert result.answer_text.startswith(GRAPH_ONLY_SUMMARY_DISCLOSURE)
    assert GRAPH_ONLY_SUMMARY_DISCLOSURE in result.caveats


async def test_disclosure_is_not_submitted_to_the_verifier(
    rag_returns, graph_returns, monkeypatch
):
    """
    §2.1.2 step 4: injected AFTER the gate. The disclosure has nothing to cite,
    so verifying it could trip the no-citation check and withhold the summary
    BECAUSE it was honest about being partial.
    """
    rag_returns([])
    graph_returns([_GRAPH_CHUNK])

    seen: list[str] = []
    from src.pipeline.harness.agents import case_summary as mod

    real_verify = mod.verify_grounding

    async def _spy(answer, cited_chunks, case_id, **kwargs):
        seen.append(answer)
        return await real_verify(answer, cited_chunks, case_id, **kwargs)

    monkeypatch.setattr(mod, "verify_grounding", _spy)

    result = await case_summary.run(_input())

    assert seen, "verifier was never called"
    assert GRAPH_ONLY_SUMMARY_DISCLOSURE not in seen[0], (
        "the disclosure reached the grounding gate — it must be injected after"
    )
    assert GRAPH_ONLY_SUMMARY_DISCLOSURE in result.answer_text


# ── Nested degradation: tool succeeded but degraded internally ───────────

async def test_nested_rag_degradation_propagates(rag_returns, graph_returns):
    """
    RAG returns OK but its relevance gate could not run (c4a06fe). That caveat
    must climb into THIS sub-agent's caveats — swallowing it would hide the
    degradation one level below where anyone can see it.
    """
    rag_returns([_DOC_CHUNK], evaluator_raises=True)
    graph_returns([_GRAPH_CHUNK])

    result = await case_summary.run(_input())

    assert result.caveats, "nested tool degradation was swallowed"
    assert any("relevance check" in c.lower() for c in result.caveats)
    assert result.status is SubAgentStatus.PARTIAL


async def test_nested_degradation_keeps_the_tool_in_tools_used(rag_returns, graph_returns):
    """
    The internally-degraded tool DID contribute — it just contributed
    unscreened evidence. It appears in both lists, the one case where that is
    correct.
    """
    rag_returns([_DOC_CHUNK], evaluator_raises=True)
    graph_returns([_GRAPH_CHUNK])

    result = await case_summary.run(_input())

    assert "RAG" in result.tools_used
    assert "RAG" in result.degraded_from


# ── Both empty, and the grounding gate ───────────────────────────────────

async def test_both_empty_returns_empty_not_abstained(rag_returns, graph_returns):
    rag_returns([])
    graph_returns([])

    result = await case_summary.run(_input())

    assert result.status is SubAgentStatus.EMPTY
    assert result.answer_text is None
    assert result.tools_used == []


async def test_failing_verifier_abstains_without_disclosure(rag_returns, graph_returns):
    """
    §2.1.2 step 3: a failed gate produces NO summary and NO disclosure — there
    is no partial artifact to qualify.
    """
    rag_returns([])
    graph_returns([_GRAPH_CHUNK])

    result = await case_summary.run(_input(query=f"{UNGROUNDED_TRIGGER} summarize"))

    assert result.status is SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.citations == []
    assert GRAPH_ONLY_SUMMARY_DISCLOSURE not in result.caveats


# ── Bounded payload — structural, mirroring Semantic Search's check ──────

def test_subagent_result_has_no_field_that_can_hold_evidence():
    """
    Same structural guarantee Semantic Search asserts: leaking evidence upward
    must be a type error, not a code-review catch. Walks declared annotations
    rather than one instance, so a chunk-typed field added later fails here
    even with no test data exercising it.
    """
    offenders = [
        name for name, field in SubAgentResult.model_fields.items()
        if "EvidenceChunk" in repr(field.annotation)
    ]
    assert not offenders, (
        f"SubAgentResult fields {offenders} can hold EvidenceChunk objects. "
        "Design §3: the bounded payload must never carry raw evidence upward."
    )


async def test_handoff_carries_citations_not_chunks(rag_returns, graph_returns):
    rag_returns([_DOC_CHUNK])
    graph_returns([_GRAPH_CHUNK])

    result = await case_summary.run(_input())

    assert result.citations
    assert all(isinstance(c, Citation) for c in result.citations)
    assert not any(isinstance(c, EvidenceChunk) for c in result.citations)


async def test_citations_carry_no_chunk_text(rag_returns, graph_returns):
    """Provenance only — chunk text riding along would defeat the bounding."""
    rag_returns([_DOC_CHUNK])
    graph_returns([_GRAPH_CHUNK])

    result = await case_summary.run(_input())

    for citation in result.citations:
        serialized = citation.model_dump_json()
        for body in (_DOC_CHUNK["text"], _GRAPH_CHUNK["text"]):
            assert body[:30] not in serialized


async def test_summary_text_is_bounded(rag_returns, graph_returns):
    """The summary must not scale with retrieval volume."""
    many = [
        {**_DOC_CHUNK, "id": f"d{i}", "text": _DOC_CHUNK["text"] * 20}
        for i in range(10)
    ]
    rag_returns(many)
    graph_returns([_GRAPH_CHUNK])

    retrieved_size = sum(len(c["text"]) for c in many)
    result = await case_summary.run(_input())

    assert len(result.answer_text) < retrieved_size / 4


# ── Trace events ─────────────────────────────────────────────────────────

async def test_emits_events_for_subagent_and_both_tools(rag_returns, graph_returns):
    rag_returns([_DOC_CHUNK])
    graph_returns([_GRAPH_CHUNK])

    recorder = EventRecorder()
    await case_summary.run(_input(), events=recorder)

    steps = [e.step for e in recorder.events]
    assert f"subagent:{case_summary.NAME}" in steps
    assert "tool:rag" in steps
    assert "tool:graph" in steps


async def test_degradation_is_visible_in_the_trace(rag_returns, graph_returns):
    rag_returns([])
    graph_returns([_GRAPH_CHUNK])

    recorder = EventRecorder()
    await case_summary.run(_input(), events=recorder)

    assert any(
        e.status == "retry" and "degraded" in e.detail.lower()
        for e in recorder.events
    ), "degradation must be visible in the live trace, not only the payload"
