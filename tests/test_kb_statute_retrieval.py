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
  3. the cross-encoder rerank scores against the statute phrasing too (KB8's
     s.173 chunk was RRF rank 1 and still cut, because a Roman-Urdu query
     scores an English statute as noise), and — Module 38 — fuses the
     per-query lists by RECIPROCAL RANK rather than by best score, because
     cross-encoder scores are not comparable across queries and the
     max-over-queries merge let whichever phrasing produced the largest
     numbers take the whole final window (measured on KB4).

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


# ── 5. cross_rerank_multi fuses the per-query lists by rank ──────────────
#
# [Module 38] This section used to pin a max-over-queries merge. That merge
# was the defect Module 30 recorded on its way out (MODULE30_RESULT.md §8b):
# cross-encoder scores are a per-query judgement, not a calibrated absolute,
# so the phrasing that happened to produce the largest numbers took the whole
# final window. The property Module 30 actually needed — a chunk that only
# ONE phrasing recognises must survive — is pinned below against the rank
# fusion that replaced it.


def _stub_per_query_rerank(monkeypatch, per_query: dict[str, dict[str, float]]):
    """Install a `cross_rerank` that scores by a per-query score table."""
    async def _cross_rerank(query, candidates, top_k=None):
        scored = [
            {**c, "rerank_score": per_query[query][c["id"]]} for c in candidates
        ]
        scored.sort(key=lambda c: c["rerank_score"], reverse=True)
        return scored[: (top_k or len(scored))]

    monkeypatch.setattr(cross_reranker, "cross_rerank", _cross_rerank)


def _max_merge_order(per_query: dict[str, dict[str, float]]) -> list[str]:
    """The pre-Module-38 merge, reproduced here so each test can show what it
    would have returned on the same data rather than asserting into a void."""
    best: dict[str, float] = {}
    for scores in per_query.values():
        for chunk_id, score in scores.items():
            if score > best.get(chunk_id, float("-inf")):
                best[chunk_id] = score
    return sorted(best, key=lambda cid: best[cid], reverse=True)


@pytest.mark.asyncio
async def test_cross_rerank_multi_keeps_a_chunk_only_one_phrasing_recognises(monkeypatch):
    """KB8's live failure: the correct chunk scores as noise against the
    Roman-Urdu question (rank 5 of 5 there) and rank 1 against the English
    statute phrasing. It must still survive into the final window — that is
    the property Module 30 added this function for, and rank fusion keeps it
    without letting one phrasing's score scale decide everything."""
    per_query = {
        "roman urdu question": {
            "s173": 0.0011, "n1": 0.0022, "n2": 0.0020, "n3": 0.0018, "n4": 0.0015,
        },
        "english statute phrasing": {
            "s173": 0.80, "n1": 0.10, "n2": 0.09, "n3": 0.08, "n4": 0.07,
        },
    }
    _stub_per_query_rerank(monkeypatch, per_query)

    candidates = [_chunk(i) for i in ("s173", "n1", "n2", "n3", "n4")]
    merged = await cross_reranker.cross_rerank_multi(
        ["roman urdu question", "english statute phrasing"], candidates, top_k=3
    )
    assert "s173" in [c["id"] for c in merged]
    # The best cross-encoder score any phrasing gave it is still carried
    # through for provenance, even though it no longer decides the order.
    assert next(c for c in merged if c["id"] == "s173")["rerank_score"] == pytest.approx(0.80)


@pytest.mark.asyncio
async def test_cross_rerank_multi_fuses_by_rank_not_by_score_scale(monkeypatch):
    """KB4's live failure in one assertion (Module 38).

    Two statute hypotheses were generated for KB4 (case property / malkhana
    register): "Punjab Police Rules 1934 case property and malkhana" — the
    book the question is actually about — and "Forensics guidelines handling
    and chain of custody", which is not. The forensics query's absolute
    cross-encoder scores simply ran higher, so a max-over-queries merge gave
    it the entire final window and the answer moved off register material.

    The two hypotheses' RANKINGS disagree, and it is the ranking that carries
    the relevance judgement. Fused by reciprocal rank, the register chunks
    the question and the right hypothesis BOTH place highly win, however
    small their numbers are.
    """
    per_query = {
        # The Urdu-script question — near-noise, as measured live, and only
        # mildly favouring the register chunks.
        "urdu question": {
            "reg1": 0.0021, "for1": 0.0020, "reg2": 0.0019,
            "for2": 0.0018, "reg3": 0.0017, "for3": 0.0016,
        },
        # Right book, modest scale.
        "punjab police rules case property and malkhana register": {
            "reg1": 0.30, "reg2": 0.22, "reg3": 0.18,
            "for1": 0.12, "for2": 0.09, "for3": 0.05,
        },
        # Wrong book, and every one of its scores dwarfs every score above.
        "forensics guidelines handling and chain of custody": {
            "for1": 0.99, "for2": 0.97, "for3": 0.95,
            "reg1": 0.93, "reg2": 0.91, "reg3": 0.90,
        },
    }
    _stub_per_query_rerank(monkeypatch, per_query)

    candidates = [_chunk(i) for i in ("reg1", "for1", "reg2", "for2", "reg3", "for3")]
    merged = await cross_reranker.cross_rerank_multi(
        list(per_query), candidates, top_k=3
    )
    fused_order = [c["id"] for c in merged]

    # The wrong hypothesis no longer owns the window: the chunk the question
    # AND the right hypothesis both rank first comes first, and the register
    # material is the majority of what reaches the evaluator.
    assert fused_order[0] == "reg1"
    assert sum(1 for cid in fused_order if cid.startswith("reg")) >= 2

    # Same data through the merge this replaced: the forensics query's scale
    # takes every slot. Without this the test above could pass for the wrong
    # reason on data that never distinguished the two merges.
    assert _max_merge_order(per_query)[:3] == ["for1", "for2", "for3"]


@pytest.mark.asyncio
async def test_cross_rerank_multi_rescues_a_phrasings_rank_one_chunk(monkeypatch):
    """KB8's cost of pure rank fusion, in one assertion (Module 38).

    The CrPC s.173 proviso chunk carrying gold's "fourteen days" and "interim
    report within three days" is **rank 1** for the statute hypothesis and
    rank 25 of 32 for the Roman-Urdu question, whose cross-encoder scores are
    noise. Consensus fusion alone put it at 10, outside a window of 5 — so a
    fix for KB4 would have broken KB8. Each phrasing keeps a guaranteed voice
    for its own rank-1 candidate, appended and capped.
    """
    # The measured shape: a pool of 32, the proviso 25th for the question and
    # 1st for the hypothesis.
    fillers = [f"c{i}" for i in range(31)]
    question_order = fillers[:24] + ["s173"] + fillers[24:]
    hypothesis_order = ["s173"] + fillers
    per_query = {
        # Noise, as measured live — the whole range was 0.0007–0.0022.
        "roman urdu question": {
            cid: 0.0022 - 0.00005 * i for i, cid in enumerate(question_order)
        },
        "statute hypothesis": {
            cid: 0.94 - 0.02 * i for i, cid in enumerate(hypothesis_order)
        },
    }
    _stub_per_query_rerank(monkeypatch, per_query)
    candidates = [_chunk(cid) for cid in question_order]

    merged = await cross_reranker.cross_rerank_multi(
        list(per_query), candidates, top_k=5
    )
    merged_ids = [c["id"] for c in merged]
    assert "s173" in merged_ids, "the proviso chunk only one phrasing recognises was lost"
    # It is rescued, not promoted: consensus still owns the head of the window.
    assert merged_ids[0] == "c0"
    assert merged_ids[-1] == "s173"
    assert len(merged) <= 5 + cross_reranker.MAX_RESCUED_TOP_HITS

    # Pure fusion, with no rescue, would have cut it — the counterfactual this
    # test exists for.
    monkeypatch.setattr(cross_reranker, "MAX_RESCUED_TOP_HITS", 0)
    unrescued = await cross_reranker.cross_rerank_multi(
        list(per_query), candidates, top_k=5
    )
    assert "s173" not in [c["id"] for c in unrescued]


@pytest.mark.asyncio
async def test_cross_rerank_multi_rescue_is_capped(monkeypatch):
    """The rescue must never become a second route by which the phrasings
    flood the window — same cap, and the same reason, as reranker.py's
    SEMANTIC_FLOOR_MAX_RESCUED."""
    ids = [f"c{i}" for i in range(6)]
    # Four phrasings, each certain about a different chunk the others bury.
    per_query = {
        f"q{k}": {cid: (1.0 if i == k else 0.1 - 0.001 * i) for i, cid in enumerate(ids)}
        for k in range(4)
    }
    _stub_per_query_rerank(monkeypatch, per_query)
    candidates = [_chunk(cid) for cid in ids]

    merged = await cross_reranker.cross_rerank_multi(list(per_query), candidates, top_k=1)
    assert len(merged) <= 1 + cross_reranker.MAX_RESCUED_TOP_HITS


@pytest.mark.asyncio
async def test_cross_rerank_multi_weights_the_original_question_first(monkeypatch):
    """`queries[0]` is the user's own question and is weighted by
    `ORIGINAL_QUERY_WEIGHT`. The default is deliberate and measured (see
    MODULE38_RESULT.md §2): equal weighting, because for exactly the
    questions this path exists for the question is in a language the corpus
    is not, so its cross-encoder ranking is noise and up-weighting it would
    up-weight noise. This test pins that the weight is wired to the FIRST
    query, so a future change of the constant does what it says."""
    per_query = {
        "question": {"a": 0.5, "b": 0.4},
        "hypothesis": {"b": 0.9, "a": 0.1},
    }
    _stub_per_query_rerank(monkeypatch, per_query)
    candidates = [_chunk("a"), _chunk("b")]

    monkeypatch.setattr(cross_reranker, "ORIGINAL_QUERY_WEIGHT", 5.0)
    merged = await cross_reranker.cross_rerank_multi(
        ["question", "hypothesis"], candidates, top_k=2
    )
    assert [c["id"] for c in merged] == ["a", "b"]

    monkeypatch.setattr(cross_reranker, "ORIGINAL_QUERY_WEIGHT", 0.01)
    merged = await cross_reranker.cross_rerank_multi(
        ["question", "hypothesis"], candidates, top_k=2
    )
    assert [c["id"] for c in merged] == ["b", "a"]


@pytest.mark.asyncio
async def test_cross_rerank_multi_does_not_overwrite_the_pools_rrf_score(monkeypatch):
    """The fused chunks still carry the semantic-vs-BM25 `rrf_score` the
    candidate pool was built with — the cross-query fusion writes its own
    key, so retrieval logging and provenance keep meaning what they meant."""
    per_query = {"q1": {"a": 0.5, "b": 0.4}, "q2": {"a": 0.9, "b": 0.1}}
    _stub_per_query_rerank(monkeypatch, per_query)

    candidates = [_chunk("a", score=0.87), _chunk("b", score=0.61)]
    merged = await cross_reranker.cross_rerank_multi(["q1", "q2"], candidates, top_k=2)

    by_id = {c["id"]: c for c in merged}
    assert by_id["a"]["rrf_score"] == pytest.approx(0.87)
    assert by_id["b"]["rrf_score"] == pytest.approx(0.61)
    assert cross_reranker.CROSS_RRF_SCORE_KEY in by_id["a"]


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
