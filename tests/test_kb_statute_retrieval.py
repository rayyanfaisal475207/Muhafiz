"""
Module 30 — KB3/KB8/KB9: the governing statutory chunk never reached the
retrieval candidate pool.

Root cause established by probing the live corpus (see
`docs/gold-qa-wave2-results/MODULE30_RESULT.md`): every query string the
pipeline generates is a paraphrase of the QUESTION, never of the PROVISION
— `query_expander.py` is forbidden from switching language, and
`cross_script_variant.py` sends a Latin-script query to Urdu script. The
legal KB corpus is seven ENGLISH statute books, so a Roman-Urdu question
(KB8, KB9) is embedded only as Roman-Urdu and Urdu-script text against an
English corpus.

These tests pin the three mechanisms that closed it, each against the
LITERAL gold text of the questions it fixes:

  1. a legal-KB-intent question gets extra ENGLISH retrieval queries, each
     naming a candidate governing statute (`statute_hypothesis.py`);
  2. the multi-variant dedupe keeps a chunk's BEST similarity, not the
     first variant's (KB3's Article 18 chunk was locked to a weak score and
     sorted out of the pool);
  3. the cross-encoder rerank scores against the statute phrasing too, and
     keeps each chunk's best (KB8's s.173 chunk was RRF rank 1 and still
     cut, because a Roman-Urdu query scores an English statute as noise).

No network: every LLM/embedding/reranker boundary is stubbed.
"""
import json
from pathlib import Path

import pytest

import src.pipeline.harness.tools.rag as rag_mod
import src.pipeline.statute_hypothesis as statute_mod
import src.retrieval.cross_reranker as cross_reranker
from src.pipeline.harness.tools.rag import (
    RagToolInput,
    _is_legal_kb_intent,
    _retrieve_candidates,
    rag_tool,
)
from src.pipeline.harness.types import CallerContext, ExecutionContext, ToolStatus
from src.pipeline.statute_hypothesis import generate_statute_queries

# The dataset WITH answers is the only Gold-32 file that exists. A test
# referencing a bare `Gold_QA_Dataset_Final32.json` silently skipped for
# weeks before PR #21 caught it — assert the file is there rather than
# skipping if it is not.
_GOLD_PATH = (
    Path(__file__).resolve().parent.parent
    / "evaluation"
    / "Gold_QA_Dataset_Final32_With_Answers.json"
)


def _gold(question_id: str) -> dict:
    assert _GOLD_PATH.exists(), f"Gold dataset missing at {_GOLD_PATH}"
    rows = json.loads(_GOLD_PATH.read_text(encoding="utf-8"))
    for row in rows:
        if row["id"] == question_id:
            return row
    raise AssertionError(f"{question_id} not in {_GOLD_PATH.name}")


# ── 1. Intent gate fires on the three questions' literal gold text ────────

@pytest.mark.parametrize("qid", ["KB1", "KB2", "KB3", "KB4", "KB5", "KB6", "KB8", "KB9"])
def test_gold_kb_questions_are_legal_kb_intent(qid):
    """The statute-hypothesis query is gated on `_is_legal_kb_intent`, so a
    gate that stopped matching these questions would silently disable the
    whole fix. Pinned to the literal gold text of all eight KB questions
    (English, Urdu script and Roman-Urdu), not to paraphrases."""
    assert _is_legal_kb_intent(_gold(qid)["question"]) is True


# ── 2. statute_hypothesis.py's own contract ──────────────────────────────

def _stub_llm_json(monkeypatch, parsed, raw="", exc=None):
    async def _call_llm_json(**kwargs):
        if exc is not None:
            raise exc
        return parsed, raw

    monkeypatch.setattr(statute_mod, "call_llm_json", _call_llm_json)


@pytest.mark.asyncio
async def test_statute_queries_returns_the_models_queries(monkeypatch):
    lines = [
        "Code of Criminal Procedure 1898 section 173 report of police officer: "
        "interim report to the Magistrate through the Public Prosecutor where "
        "investigation is not completed within fourteen days",
        "Punjab Police Rules 1934 case diaries and the progress of investigation",
    ]
    _stub_llm_json(monkeypatch, lines)
    assert await generate_statute_queries(_gold("KB8")["question"]) == lines


@pytest.mark.asyncio
async def test_statute_queries_are_capped_at_n(monkeypatch):
    _stub_llm_json(monkeypatch, ["one", "two", "three", "four"])
    assert await generate_statute_queries(_gold("KB3")["question"], n=2) == ["one", "two"]


@pytest.mark.asyncio
@pytest.mark.parametrize("parsed", [[], ["", "   "], [None, 5]])
async def test_statute_queries_empty_escape_hatch(monkeypatch, parsed):
    """Rule 5 of the prompt: no plausible governing provision -> [], which the
    caller treats exactly like `expand_query()` returning []."""
    _stub_llm_json(monkeypatch, parsed)
    assert await generate_statute_queries("What is the weather today?") == []


@pytest.mark.asyncio
async def test_statute_queries_reject_a_non_english_entry(monkeypatch):
    """The variants exist to be ENGLISH — the corpus is. An Urdu-script entry
    cannot serve that purpose (and duplicates cross_script_variant.py), so it
    is dropped while its English sibling is kept."""
    _stub_llm_json(
        monkeypatch,
        [
            "\u0636\u0627\u0628\u0637\u06c1 \u0641\u0648\u062c\u062f\u0627\u0631\u06cc 1898",
            "Code of Criminal Procedure 1898 section 174 inquest",
        ],
    )
    assert await generate_statute_queries(_gold("KB9")["question"]) == [
        "Code of Criminal Procedure 1898 section 174 inquest"
    ]


@pytest.mark.asyncio
async def test_statute_queries_return_empty_on_unparseable_output(monkeypatch):
    """call_llm_json exhausting its retries yields (None, raw) — that must
    degrade to pre-Module-30 retrieval, not raise."""
    _stub_llm_json(monkeypatch, None, raw="Sure! Here are two options...")
    assert await generate_statute_queries(_gold("KB3")["question"]) == []


@pytest.mark.asyncio
async def test_statute_queries_return_empty_on_llm_failure(monkeypatch):
    """Same []-on-failure contract as the other query-widening steps: a dead
    LLM must degrade to pre-Module-30 retrieval, never raise."""
    _stub_llm_json(monkeypatch, None, exc=RuntimeError("model server down"))
    assert await generate_statute_queries(_gold("KB3")["question"]) == []


# ── 3. The statute queries actually reach retrieval and rerank ───────────

def _chunk(id_, score=0.5, text=None):
    return {
        "id": id_,
        "text": text or f"text-{id_}",
        "metadata": {"source": "statute.pdf", "is_global": True},
        "rrf_score": score,
    }


def _kb_execution():
    return ExecutionContext(
        caller=CallerContext(user_id="u1", role="platform-admin", active_case_id=None)
    )


@pytest.fixture
def stub_retrieval(monkeypatch):
    """Deterministic stand-ins for every wrapped boundary, plus recorders for
    the two things these tests assert on: which query strings were embedded,
    and which queries the cross-encoder rerank was scored against."""
    state = {"embedded": [], "rerank_queries": []}

    async def _embed_text(q, **kwargs):
        state["embedded"].append(q)
        return [0.1, 0.2]

    async def _query_similar(q, emb, top_k=10, where=None, **kwargs):
        return [_chunk("c1")]

    async def _bm25_candidate_pool(query_text, where=None):
        return [_chunk("c1")]

    def _retrieve_bm25(query, docs, top_k=10):
        return docs[:top_k]

    def _rerank_results(semantic, bm25, top_k=5):
        merged = {c["id"]: c for c in semantic + bm25}
        return list(merged.values())[:top_k]

    async def _cross_rerank(query, candidates, top_k=None):
        state["rerank_queries"].append(query)
        return candidates[: (top_k or len(candidates))]

    async def _cross_rerank_multi(queries, candidates, top_k=None):
        state["rerank_queries"].extend(queries)
        return candidates[: (top_k or len(candidates))]

    async def _expand_query(q, n=2):
        return []

    async def _cross_script_variant(q):
        return None

    async def _evaluate(orig, cur, reranked):
        return {"relevant": True, "reason": "ok"}

    monkeypatch.setattr(rag_mod, "embed_text", _embed_text)
    monkeypatch.setattr(rag_mod, "query_similar", _query_similar)
    monkeypatch.setattr(rag_mod, "bm25_candidate_pool", _bm25_candidate_pool)
    monkeypatch.setattr(rag_mod, "retrieve_bm25", _retrieve_bm25)
    monkeypatch.setattr(rag_mod, "rerank_results", _rerank_results)
    monkeypatch.setattr(rag_mod, "cross_rerank", _cross_rerank)
    monkeypatch.setattr(rag_mod, "cross_rerank_multi", _cross_rerank_multi)
    monkeypatch.setattr(rag_mod, "expand_query", _expand_query)
    monkeypatch.setattr(rag_mod, "generate_cross_script_variant", _cross_script_variant)
    monkeypatch.setattr(rag_mod, "evaluate_relevance", _evaluate)
    return state


_STATUTE_LINE = (
    "Code of Criminal Procedure 1898 section 174 police to inquire and report "
    "on suicide: apparent cause of death reported to the nearest Magistrate"
)
_STATUTE_LINE_2 = (
    "Police Order 2002 Article 18 posting of head of investigation: registered "
    "cases shall be investigated by the investigation staff"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("qid", ["KB3", "KB8", "KB9"])
async def test_gold_kb_question_embeds_and_reranks_with_the_statute_query(
    monkeypatch, stub_retrieval, qid
):
    """The end-to-end wiring, pinned to the three questions' literal gold
    text: the English statute phrasing must be BOTH embedded as a retrieval
    query and used as a cross-encoder rerank query. Getting the provision
    into the pool without also reranking against it is what still lost KB8
    live — the reranker cut an RRF-rank-1 chunk."""
    async def _statute(question, n=2):
        return [_STATUTE_LINE, _STATUTE_LINE_2]

    monkeypatch.setattr(rag_mod, "generate_statute_queries", _statute)

    result = await rag_tool(
        RagToolInput(query_text=_gold(qid)["question"], execution=_kb_execution())
    )
    assert result.status == ToolStatus.OK
    for hypothesis in (_STATUTE_LINE, _STATUTE_LINE_2):
        assert hypothesis in stub_retrieval["embedded"]
        assert hypothesis in stub_retrieval["rerank_queries"]
    # The original question is still scored too — the statute phrasing widens
    # the rerank, it does not replace the user's own question.
    assert _gold(qid)["question"] in stub_retrieval["rerank_queries"]


@pytest.mark.asyncio
async def test_non_legal_question_never_asks_for_a_statute_query(
    monkeypatch, stub_retrieval
):
    """Scoped, not global: a plain case-data question must pay neither the
    extra LLM call nor the extra reranker pass."""
    called = []

    async def _statute(question, n=2):
        called.append(question)
        return [_STATUTE_LINE]

    monkeypatch.setattr(rag_mod, "generate_statute_queries", _statute)

    result = await rag_tool(
        RagToolInput(
            query_text="How many FIRs were registered in Ramna police station?",
            execution=_kb_execution(),
        )
    )
    assert result.status == ToolStatus.OK
    assert called == []
    assert _STATUTE_LINE not in stub_retrieval["embedded"]
    assert stub_retrieval["rerank_queries"] == [
        "How many FIRs were registered in Ramna police station?"
    ]


@pytest.mark.asyncio
async def test_statute_query_failure_degrades_to_previous_behaviour(
    monkeypatch, stub_retrieval
):
    """A dead statute-hypothesis call must leave retrieval exactly as it was
    before Module 30 — single-query cross-rerank, no extra embedding."""
    async def _statute(question, n=2):
        return []

    monkeypatch.setattr(rag_mod, "generate_statute_queries", _statute)

    question = _gold("KB9")["question"]
    result = await rag_tool(RagToolInput(query_text=question, execution=_kb_execution()))
    assert result.status == ToolStatus.OK
    assert stub_retrieval["embedded"] == [question]
    assert stub_retrieval["rerank_queries"] == [question]


# ── 4. Multi-variant dedupe keeps the BEST score, not the first ──────────

@pytest.mark.asyncio
async def test_dedupe_keeps_the_best_similarity_across_query_variants(monkeypatch):
    """KB3's live failure in one assertion.

    The Police Order Article 18 chunk carrying "shall be investigated by the
    investigation staff" was surfaced weakly by the original question (0.87)
    and strongly by the statute variant (0.91). Locked to the first-seen
    0.87 it sorted 54th in the merged pool and was cut before the reranker
    ever saw it."""
    async def _embed_text(q, **kwargs):
        return [0.1, 0.2]

    scores = {
        "question": [_chunk("article18", 0.87), _chunk("other", 0.90)],
        "statute": [_chunk("article18", 0.91)],
    }

    async def _query_similar(q, emb, top_k=10, where=None, **kwargs):
        return scores["statute"] if q == "statute" else scores["question"]

    async def _bm25_candidate_pool(query_text, where=None):
        return []

    async def _expand_query(q, n=2):
        return []

    async def _cross_script_variant(q):
        return None

    monkeypatch.setattr(rag_mod, "embed_text", _embed_text)
    monkeypatch.setattr(rag_mod, "query_similar", _query_similar)
    monkeypatch.setattr(rag_mod, "bm25_candidate_pool", _bm25_candidate_pool)
    monkeypatch.setattr(rag_mod, "expand_query", _expand_query)
    monkeypatch.setattr(rag_mod, "generate_cross_script_variant", _cross_script_variant)

    semantic, _ = await _retrieve_candidates(
        "question", {"is_global": True}, 10, 10, is_cross_case=True,
        statute_queries=["statute"],
    )
    by_id = {c["id"]: c for c in semantic}
    assert by_id["article18"]["rrf_score"] == pytest.approx(0.91)
    # ...and that better score is what decides its place in the pool.
    assert semantic[0]["id"] == "article18"


# ── 5. cross_rerank_multi merges by best score ───────────────────────────

@pytest.mark.asyncio
async def test_cross_rerank_multi_keeps_each_chunks_best_query_score(monkeypatch):
    """KB8's live failure in one assertion: the correct chunk scores as noise
    against the Roman-Urdu question and well against the English statute
    phrasing, so the max over both is what retains it."""
    per_query = {
        "roman urdu question": {"s173": 0.001, "noise": 0.002},
        "english statute phrasing": {"s173": 0.80, "noise": 0.10},
    }

    async def _cross_rerank(query, candidates, top_k=None):
        scored = [
            {**c, "rerank_score": per_query[query][c["id"]]} for c in candidates
        ]
        scored.sort(key=lambda c: c["rerank_score"], reverse=True)
        return scored[: (top_k or len(scored))]

    monkeypatch.setattr(cross_reranker, "cross_rerank", _cross_rerank)

    candidates = [_chunk("s173"), _chunk("noise")]
    merged = await cross_reranker.cross_rerank_multi(
        ["roman urdu question", "english statute phrasing"], candidates, top_k=2
    )
    assert [c["id"] for c in merged] == ["s173", "noise"]
    assert merged[0]["rerank_score"] == pytest.approx(0.80)


@pytest.mark.asyncio
async def test_cross_rerank_multi_with_one_query_matches_cross_rerank(monkeypatch):
    """Single-query behaviour must be unchanged — this is the path every
    non-legal question keeps taking."""
    async def _cross_rerank(query, candidates, top_k=None):
        scored = [
            {**c, "rerank_score": 1.0 / (i + 1)} for i, c in enumerate(candidates)
        ]
        return scored[: (top_k or len(scored))]

    monkeypatch.setattr(cross_reranker, "cross_rerank", _cross_rerank)
    candidates = [_chunk("a"), _chunk("b"), _chunk("c")]
    merged = await cross_reranker.cross_rerank_multi(["q"], candidates, top_k=2)
    assert [c["id"] for c in merged] == ["a", "b"]


@pytest.mark.asyncio
async def test_cross_rerank_multi_handles_no_usable_query():
    candidates = [_chunk("a"), _chunk("b")]
    assert await cross_reranker.cross_rerank_multi(["", "   "], candidates, top_k=1) == [
        candidates[0]
    ]
    assert await cross_reranker.cross_rerank_multi(["q"], [], top_k=1) == []


# ── 6. The case-diversity cap does not truncate the case-less KB corpus ──

def _stub_pool(monkeypatch, pool):
    async def _embed_text(q, **kwargs):
        return [0.1, 0.2]

    async def _query_similar(q, emb, top_k=10, where=None, **kwargs):
        return pool

    async def _bm25_candidate_pool(query_text, where=None):
        return []

    async def _expand_query(q, n=2):
        return []

    async def _cross_script_variant(q):
        return None

    monkeypatch.setattr(rag_mod, "embed_text", _embed_text)
    monkeypatch.setattr(rag_mod, "query_similar", _query_similar)
    monkeypatch.setattr(rag_mod, "bm25_candidate_pool", _bm25_candidate_pool)
    monkeypatch.setattr(rag_mod, "expand_query", _expand_query)
    monkeypatch.setattr(rag_mod, "generate_cross_script_variant", _cross_script_variant)


@pytest.mark.asyncio
async def test_kb_only_scope_is_not_truncated_by_the_case_diversity_cap(monkeypatch):
    """Every chunk in the legal KB corpus is case-less, so all 12 here bucket
    under `case_id=None` and `CROSS_CASE_PER_CASE_CAP` would cut the pool to
    5 before RRF — diversifying nothing, since there are no cases to
    diversify across. Measured live on KB3/KB8/KB9: 71–94 statutory
    candidates cut to 5, which left only one of Article 18's chunks in the
    final set."""
    pool = [_chunk(f"kb{i}", score=1.0 - i / 100) for i in range(12)]
    _stub_pool(monkeypatch, pool)

    semantic, _ = await _retrieve_candidates(
        "q", {"is_global": True}, 30, 10, is_cross_case=True
    )
    assert [c["id"] for c in semantic] == [f"kb{i}" for i in range(10)]


@pytest.mark.asyncio
async def test_mixed_all_cases_scope_still_gets_the_diversity_cap(monkeypatch):
    """Scoped, not removed: a pool that really does span cases must still be
    capped per case, exactly as before — that is Fix 2's whole purpose."""
    pool = [
        _chunk(f"c{i}", score=1.0 - i / 100) for i in range(12)
    ]
    for chunk in pool:
        chunk["metadata"]["case_id"] = "CASE-001"
    _stub_pool(monkeypatch, pool)

    semantic, _ = await _retrieve_candidates(
        "q", {"all_cases": True}, 30, 10, is_cross_case=True
    )
    assert len(semantic) == 5


# ── 7. Neighbour widening: retrieve narrow, read wide ────────────────────

@pytest.mark.asyncio
async def test_expand_with_neighbors_widens_a_mid_sentence_statutory_chunk():
    """KB3's residual failure in one assertion: the Article 18 chunk was
    retrieved at rank 1 on every attempt and the evaluator still returned
    relevant=False, because the chunk ends mid-sentence and the words that
    answer the question are in the next chunk."""
    import src.retrieval.vector_store as vs

    corpus = {
        ("doc-po", 113): "...shall be responsible to his own hierarchy subject to "
                         "general control of the District Police Officer",
        ("doc-po", 115): "under the supervision of the head of investigation: ... "
                         "(5) The District Police Officer shall not interfere with "
                         "the process of investigation.",
    }

    class _FakeStore:
        def get_by_metadata(self, metadata_filter):
            clauses = metadata_filter["$and"]
            doc_id = clauses[0]["doc_id"]["$eq"]
            indices = clauses[1]["chunk_index"]["$in"]
            return [
                {"id": f"{doc_id}_c{i}", "text": corpus[(doc_id, i)],
                 "metadata": {"doc_id": doc_id, "chunk_index": i}}
                for i in indices if (doc_id, i) in corpus
            ]

    original = vs._get_store
    vs._get_store = lambda: _FakeStore()
    try:
        retrieved = [{
            "id": "doc-po_c114",
            "text": "(4) All registered cases shall be investigated by the "
                    "investigation staff in the district under",
            "metadata": {"doc_id": "doc-po", "chunk_index": 114, "source": "po.pdf"},
            "rerank_score": 0.9,
        }]
        widened = await vs.expand_with_neighbors(retrieved, window=1)
    finally:
        vs._get_store = original

    assert len(widened) == 1
    text = widened[0]["text"]
    assert "shall be investigated by the investigation staff" in text
    assert "shall not interfere with the process of investigation" in text
    # Identity, provenance and score are untouched — citations still point
    # at the chunk that was actually retrieved.
    assert widened[0]["id"] == "doc-po_c114"
    assert widened[0]["metadata"]["source"] == "po.pdf"
    assert widened[0]["rerank_score"] == 0.9
    assert retrieved[0]["text"].endswith("in the district under")


@pytest.mark.asyncio
async def test_expand_with_neighbors_degrades_to_the_unwidened_chunks():
    """A chunk with no positional metadata, and a failing store, must both
    yield the retrieved chunks unchanged — a narrower read, never a lost
    one."""
    import src.retrieval.vector_store as vs

    plain = [{"id": "synthetic", "text": "case record", "metadata": {"source": "db"}}]
    assert await vs.expand_with_neighbors(plain, window=1) == plain

    class _BrokenStore:
        def get_by_metadata(self, metadata_filter):
            raise RuntimeError("chroma is down")

    original = vs._get_store
    vs._get_store = lambda: _BrokenStore()
    try:
        chunks = [{"id": "c1", "text": "t", "metadata": {"doc_id": "d", "chunk_index": 5}}]
        assert await vs.expand_with_neighbors(chunks, window=1) == chunks
    finally:
        vs._get_store = original
