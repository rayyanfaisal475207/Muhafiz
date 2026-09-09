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
    _match_kb_data_half_plan,
    _retrieve_candidates,
    rag_tool,
)
from src.pipeline.harness.types import CallerContext, ExecutionContext, ToolStatus
from src.pipeline.statute_hypothesis import (
    generate_statute_queries,
    render_question_in_english,
)

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
    state = {"embedded": [], "rerank_queries": [], "evaluator_args": [],
             "render_calls": []}

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
        state["evaluator_args"].append((orig, cur))
        return {"relevant": True, "reason": "ok"}

    async def _render(question):
        # [Module 52] Defaults to the FAILURE return (None), so every test
        # written before Module 52 keeps asserting the behaviour it was
        # written for. The tests that exercise the English rendering
        # override this with a rendering of their own.
        state["render_calls"].append(question)
        return None

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
    monkeypatch.setattr(rag_mod, "render_question_in_english", _render)
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


# ── 3b. Module 52: the RELEVANCE GATE reads the question in English ──────

_ENGLISH_RENDERING = (
    "Do the forensics guidelines say anything specific about how a recovered "
    "weapon must be handled before it is recorded, and does our weapon "
    "register record whether that was done?"
)


def _stub_render(monkeypatch, rendering):
    async def _render(question):
        return rendering
    monkeypatch.setattr(rag_mod, "render_question_in_english", _render)


@pytest.mark.asyncio
@pytest.mark.parametrize("qid", ["KB4", "KB5", "KB6", "KB8", "KB9"])
async def test_non_english_kb_question_reaches_the_evaluator_in_english(
    monkeypatch, stub_retrieval, qid
):
    """Module 52's whole point, pinned to the five gold KB questions that are
    NOT asked in English (KB4/KB5 Urdu script, KB6/KB8/KB9 Roman-Urdu).

    Measured at Layer 1 with the chunk set held constant and gold's own
    statutory text present in every cell, the gate judged the English
    phrasing relevant 6/6 and the Roman-Urdu one 1/6. So on attempt 1 BOTH
    evaluator arguments must be the English rendering — an English string in
    `rewritten_query` alone was measured at 1/3, and appending one to the
    original at 0/3. The rendering must also be embedded as a retrieval
    query."""
    _stub_render(monkeypatch, _ENGLISH_RENDERING)

    question = _gold(qid)["question"]
    result = await rag_tool(RagToolInput(query_text=question, execution=_kb_execution()))

    assert result.status == ToolStatus.OK
    assert stub_retrieval["evaluator_args"] == [(_ENGLISH_RENDERING, _ENGLISH_RENDERING)]
    # The raw question never reaches the gate on the legal-KB path.
    assert question not in stub_retrieval["evaluator_args"][0]
    assert _ENGLISH_RENDERING in stub_retrieval["embedded"]


@pytest.mark.asyncio
async def test_case_narrative_question_is_unaffected(monkeypatch, stub_retrieval):
    """The other side of the gate: a plain case-data question pays no
    rendering call, and the evaluator sees exactly the strings it saw before
    Module 52 existed."""
    called = []

    async def _render(question):
        called.append(question)
        return _ENGLISH_RENDERING

    monkeypatch.setattr(rag_mod, "render_question_in_english", _render)

    question = "How many FIRs were registered in Ramna police station?"
    result = await rag_tool(RagToolInput(query_text=question, execution=_kb_execution()))

    assert result.status == ToolStatus.OK
    assert called == []
    assert stub_retrieval["evaluator_args"] == [(question, question)]
    assert _ENGLISH_RENDERING not in stub_retrieval["embedded"]


@pytest.mark.asyncio
async def test_english_gold_kb_question_is_rendered_to_itself(
    monkeypatch, stub_retrieval
):
    """KB1/KB2/KB3 are already English, and the prompt requires those to come
    back verbatim. The rendering is then the question, so nothing about what
    the gate reads changes — and the retrieval variant list must not carry
    the same string twice."""
    question = _gold("KB1")["question"]
    _stub_render(monkeypatch, question)

    result = await rag_tool(RagToolInput(query_text=question, execution=_kb_execution()))

    assert result.status == ToolStatus.OK
    assert stub_retrieval["evaluator_args"] == [(question, question)]
    assert stub_retrieval["embedded"].count(question) == 1


@pytest.mark.asyncio
async def test_english_rendering_failure_degrades_to_previous_behaviour(
    monkeypatch, stub_retrieval
):
    """A dead rendering call must leave the gate reading exactly what it read
    before Module 52 — the raw question and the current search query — and
    must not add a retrieval variant."""
    _stub_render(monkeypatch, None)

    question = _gold("KB6")["question"]
    result = await rag_tool(RagToolInput(query_text=question, execution=_kb_execution()))

    assert result.status == ToolStatus.OK
    assert stub_retrieval["evaluator_args"] == [(question, question)]
    assert stub_retrieval["embedded"] == [question]


@pytest.mark.asyncio
async def test_on_a_retry_the_rewritten_query_is_the_rewrite_not_the_rendering(
    monkeypatch, stub_retrieval
):
    """Attempt 1 has no rewrite to report, so both arguments are the English
    rendering. Once the retry rewriter HAS produced a new search query, that
    query is genuinely what was searched and belongs in `rewritten_query` —
    but the question field stays English, which is the field the 2x2
    measured."""
    _stub_render(monkeypatch, _ENGLISH_RENDERING)

    async def _evaluate(orig, cur, reranked):
        stub_retrieval["evaluator_args"].append((orig, cur))
        if len(stub_retrieval["evaluator_args"]) == 1:
            return {"relevant": False, "reason": "no weapon register"}
        return {"relevant": True, "reason": "ok"}

    async def _rewrite(original_message, previous_query, evaluator_feedback):
        return "REWRITE: weapon register entries and firearm packaging"

    monkeypatch.setattr(rag_mod, "evaluate_relevance", _evaluate)
    monkeypatch.setattr(rag_mod, "rewrite_for_retry", _rewrite)

    result = await rag_tool(
        RagToolInput(query_text=_gold("KB6")["question"], execution=_kb_execution())
    )

    assert result.status == ToolStatus.OK
    assert stub_retrieval["evaluator_args"] == [
        (_ENGLISH_RENDERING, _ENGLISH_RENDERING),
        (_ENGLISH_RENDERING, "REWRITE: weapon register entries and firearm packaging"),
    ]


# ── 3c. Module 52: render_question_in_english()'s own contract ───────────

@pytest.mark.asyncio
async def test_rendering_returns_the_models_question(monkeypatch):
    _stub_llm_json(monkeypatch, {"question": _ENGLISH_RENDERING})
    assert await render_question_in_english(_gold("KB6")["question"]) == _ENGLISH_RENDERING


@pytest.mark.asyncio
@pytest.mark.parametrize("parsed", [None, {"question": ""}, {"question": "   "}])
async def test_rendering_returns_none_when_there_is_nothing_usable(monkeypatch, parsed):
    """None, never a partial string: every caller falls back to the raw
    question, which is exactly the pre-Module-52 behaviour."""
    _stub_llm_json(monkeypatch, parsed, raw="Sure! Here is the translation...")
    assert await render_question_in_english(_gold("KB6")["question"]) is None


@pytest.mark.asyncio
async def test_rendering_rejects_an_urdu_script_response(monkeypatch):
    """The entire purpose is that the string is ENGLISH — an Urdu-script
    rendering cannot serve it, so it is dropped rather than folded in."""
    _stub_llm_json(monkeypatch, {"question": _gold("KB4")["question"]})
    assert await render_question_in_english(_gold("KB4")["question"]) is None


@pytest.mark.asyncio
async def test_rendering_returns_none_on_llm_failure(monkeypatch):
    _stub_llm_json(monkeypatch, None, exc=RuntimeError("model server down"))
    assert await render_question_in_english(_gold("KB6")["question"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["", "   "])
async def test_rendering_of_an_empty_question_never_calls_the_llm(monkeypatch, question):
    def _boom(*a, **k):
        raise AssertionError("should not have called the LLM")
    monkeypatch.setattr(statute_mod, "call_llm_json", _boom)
    assert await render_question_in_english(question) is None


@pytest.mark.asyncio
async def test_rendering_is_truncated_rather_than_discarded(monkeypatch):
    """An over-long rendering means the model started answering; keep the
    usable head, matching `_MAX_QUERY_CHARS`' reasoning for the hypotheses."""
    _stub_llm_json(monkeypatch, {"question": "x" * 5000})
    rendered = await render_question_in_english(_gold("KB6")["question"])
    assert rendered == "x" * 1000


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
async def test_cross_rerank_multi_gives_every_phrasing_an_equal_vote(monkeypatch):
    """No phrasing outvotes another, the original question included.

    A per-list weight multiplier was built for this and then removed: swept
    over 0.25/0.5/1.0/2.0/3.0 on live KB1/KB4/KB8/KB9 pools
    (MODULE38_RESULT.md §2), every value from 0.5 to 3.0 returned the same
    governing statute book in the same window, and 0.25 was the only one that
    made KB4 worse. This pins the consequence: the fused order depends only
    on the ranks, not on which position in `queries` a phrasing occupies, so
    the fusion cannot quietly regrow a scale of its own."""
    per_query = {
        "question": {"a": 0.5, "b": 0.4, "c": 0.3},
        "hypothesis": {"b": 0.9, "c": 0.5, "a": 0.1},
    }
    _stub_per_query_rerank(monkeypatch, per_query)
    candidates = [_chunk("a"), _chunk("b"), _chunk("c")]

    question_first = await cross_reranker.cross_rerank_multi(
        ["question", "hypothesis"], candidates, top_k=3
    )
    hypothesis_first = await cross_reranker.cross_rerank_multi(
        ["hypothesis", "question"], candidates, top_k=3
    )
    assert [c["id"] for c in question_first] == [c["id"] for c in hypothesis_first]
    # …and the tie between "a" (ranks 1, 3) and "b" (ranks 2, 1) is broken by
    # rank alone: b's 1/61 + 1/62 beats a's 1/61 + 1/63.
    assert [c["id"] for c in question_first] == ["b", "a", "c"]


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


# ═══════════════════════════════════════════════════════════════════════
# Module 39 — the "and does our data show it?" half.
#
# Every gold KB answer is compound (a statutory norm PLUS a figure from our
# own case database) and Module 52 measured 0 of 48 live runs producing the
# second half. These pin the composition that closes it: the gate that
# decides which questions get a data half, the sub-queries that reach a
# real aggregate, and the degradation contract that says a failed aggregate
# can never make a KB answer worse than it was before this module.
# ═══════════════════════════════════════════════════════════════════════

# The complete expected gate over all 32 gold questions. An EQUALITY
# assertion, not a subset one: the brief's own negative control, because
# too broad a gate is as bad as too narrow — a non-KB question that
# suddenly picked up a cross-case aggregate would change an answer that is
# already correct today.
# [Gold-QA fix — Module 77] Six entries, not four. Modules 74/75/76 built
# the three aggregates KB3, KB8 and KB9 needed (`officer_role_pair_overlap`,
# `chalaan_dispatch_count`, `fir_section_case_count`); Module 77 is the
# one-entry-each addition to `rag.py::_KB_DATA_HALF_PLANS` that lands them,
# plus the RE-POINT of KB9 off `statute_court_stage_join`'s whole-caseload
# grain. This table is the equality half of the control: any question NOT
# listed here must resolve to None.
_EXPECTED_DATA_HALF_GATE = {
    "KB3": "officer_role_pair",
    "KB4": "property_register",
    "KB5": "violence_against_women",
    "KB6": "weapon_register",
    "KB8": "chalaan_dispatch",
    "KB9": "death_investigation_charging",
}


def _all_gold_rows() -> list:
    assert _GOLD_PATH.exists(), f"Gold dataset missing at {_GOLD_PATH}"
    return json.loads(_GOLD_PATH.read_text(encoding="utf-8"))


def _gate(question: str):
    """The live three-way gate, minus the scope clause (which is a property
    of the caller, not of the question) — exactly what `rag_tool()` composes."""
    if not _is_legal_kb_intent(question):
        return None
    plan = _match_kb_data_half_plan(question)
    return plan.name if plan is not None else None


def test_module39_all32_data_half_gate_is_exactly_the_six_compound_questions():
    """All-32 EQUALITY control. Exactly KB3/KB4/KB5/KB6/KB8/KB9 get a data
    half; the other 26 — including the five questions most likely to be
    caught by a widened gate (G2, G5, G3, CR7, M2) and the three that live
    in the decomposition machinery (CR3, G1, G6) — get None.

    Module 77 moved exactly two questions into this table (KB3, KB8) and
    re-pointed a third (KB9) WITHOUT moving it. CR7 is the boundary that
    matters most for KB8: it names a criminal record and a court outcome,
    never an investigation reported to a court, and it keeps its own
    `criminal_record_court_crosscheck` on the XAGG route. CP6 is the
    boundary for KB3 — it names an investigating officer and no registering
    one, so a general officer question keeps its honest refusal."""
    rows = _all_gold_rows()
    assert len(rows) == 32
    actual = {r["id"]: _gate(r["question"]) for r in rows}
    assert {k: v for k, v in actual.items() if v is not None} == _EXPECTED_DATA_HALF_GATE
    for qid in ("G2", "G5", "G3", "CR7", "M2", "CR3", "G1", "G6"):
        assert actual[qid] is None, f"{qid} must not acquire a data half"


def test_module39_gate_needs_both_clauses_not_just_the_subject_match():
    """CR8 is the reason the gate is an AND, and this is the live proof.

    CR8 ("کیا واقعی گھریلو تشدد کی رپورٹیں...") matches the
    violence-against-women plan on subject alone — but it is a plain
    case-data question, not a legal-KB one, `_is_legal_kb_intent()` returns
    False for it, and it already reaches the SAME aggregate on its own XAGG
    route today. A subject-only gate would have added a second, redundant
    dispatch to a question scoring correctly."""
    cr8 = _gold("CR8")["question"]
    assert _match_kb_data_half_plan(cr8) is not None
    assert _is_legal_kb_intent(cr8) is False
    assert _gate(cr8) is None


@pytest.mark.parametrize("qid,expected", sorted(_EXPECTED_DATA_HALF_GATE.items()))
def test_module39_each_compound_question_matches_its_own_plan(qid, expected):
    """Pinned to the LITERAL gold text — Urdu script (KB4, KB5) and
    Roman-Urdu (KB6, KB9) included, since three of the four are not asked
    in English."""
    assert _gate(_gold(qid)["question"]) == expected


def test_module39_every_sub_query_resolves_to_the_aggregate_its_plan_names():
    """The plans dispatch straight to `xagg_tool()`, so `xagg.py`'s keyword
    chain — not a router — decides which family answers. Every sub-query was
    checked against `resolve_aggregate_kind()` before being written into the
    plan; this keeps it true as that chain grows. `_run_kb_data_half()` also
    re-checks it at runtime and drops a mismatch rather than citing an
    unrelated figure, so a drift here is a test failure, never a wrong
    number in a user's answer."""
    from src.pipeline.xagg import resolve_aggregate_kind

    for plan in rag_mod._KB_DATA_HALF_PLANS:
        assert resolve_aggregate_kind(plan.sub_query) == plan.expected_kind, plan.name


def test_module39_property_sub_query_is_byte_identical_to_module_33s_pinned_string():
    """Module 33 pinned its seized-property sub-query in `tests/test_xagg.py`
    precisely so later modules could copy it across unchanged; Module 50
    already holds one copy. This is the third, and the three must not
    drift."""
    from src.pipeline.harness.agents.meta_analysis import _SQ_SEIZED_PROPERTY

    plan = next(p for p in rag_mod._KB_DATA_HALF_PLANS if p.name == "property_register")
    assert plan.sub_query == _SQ_SEIZED_PROPERTY


# ══════════════════════════════════════════════════════════════════════
# [Gold-QA fix — Module 77] The three landing entries.
#
# Each sub-query below is a COPY of a string Modules 74/75/76 already
# pinned in `tests/test_xagg.py`, checked there against
# `resolve_aggregate_kind()`. The wording is load-bearing: `xagg.py`'s
# keyword chain — not a router — decides which family answers, and a
# re-worded string reaches a different one. `_run_kb_data_half()` DROPS a
# data half whose aggregate is not the one its plan named, so a drift here
# is a silent 0-of-N, never a wrong number in a user's answer. These tests
# make the drift loud instead.
#
# Importing the constants from `tests.test_xagg` rather than restating them
# is the whole point: a restatement would drift independently, which is
# exactly the failure the pins exist to prevent.
# ══════════════════════════════════════════════════════════════════════

def _plan(name: str):
    return next(p for p in rag_mod._KB_DATA_HALF_PLANS if p.name == name)


def test_module77_kb3_sub_query_is_module74s_pinned_string():
    """KB3 -> `officer_role_pair_overlap` (68 of 74 pairs, 91.9%)."""
    from tests.test_xagg import _KB3_SQ_OFFICER_ROLE_PAIR

    plan = _plan("officer_role_pair")
    assert plan.sub_query == _KB3_SQ_OFFICER_ROLE_PAIR
    assert plan.expected_kind == "officer_role_pair_overlap"
    assert plan.sub_query == (
        "How many cases record the same person as both the recording "
        "officer and the investigating officer, and how many split those "
        "roles, across all cases?"
    )


def test_module77_kb8_sub_query_is_module75s_pinned_string():
    """KB8 -> `chalaan_dispatch_count` (26 challans sent to court)."""
    from tests.test_xagg import _KB8_SQ_CHALAAN_DISPATCH

    plan = _plan("chalaan_dispatch")
    assert plan.sub_query == _KB8_SQ_CHALAAN_DISPATCH
    assert plan.expected_kind == "chalaan_dispatch_count"
    assert plan.sub_query == (
        "How many challans have been sent to court, and how many cases "
        "do they cover, across all cases?"
    )


def test_module77_kb9_is_repointed_off_the_whole_caseload_join():
    """KB9 -> `fir_section_case_count` (10 FIRs cite PPC 302).

    The RE-POINT is the assertion. Module 39 dispatched KB9 to
    `statute_court_stage_join`, which holds the right row (`PPC §302: 10
    case(s)`) buried at position six of a 15-row whole-caseload table, and
    measured two distinct grain failures reading it. The plan must no
    longer name that family — while the literal Module 39 string, still
    pinned in `tests/test_xagg.py`, must keep resolving to it, because M4
    owns it."""
    from src.pipeline.xagg import resolve_aggregate_kind
    from tests.test_xagg import _KB9_SQ_FIR_SECTION_COUNT, _MODULE39_KB9_SUB_QUERY

    plan = _plan("death_investigation_charging")
    assert plan.sub_query == _KB9_SQ_FIR_SECTION_COUNT
    assert plan.expected_kind == "fir_section_case_count"
    assert plan.sub_query != _MODULE39_KB9_SUB_QUERY
    assert plan.expected_kind != "statute_court_stage_join"
    # M4's family is untouched by the re-point.
    assert resolve_aggregate_kind(_MODULE39_KB9_SUB_QUERY) == "statute_court_stage_join"


def test_module77_officer_plan_needs_both_roles_not_just_the_word_officer():
    """The refusal Module 74 preserved in `xagg.py` is preserved HERE too.

    `unsupported_officer` is the RIGHT answer to a general officer-identity
    question, and this plan must not steal one. CP6 names an investigating
    officer and nothing else; a bare "which officer" question names neither
    role pairing. Both must miss."""
    assert _match_kb_data_half_plan(_gold("CP6")["question"]) is None
    for q in (
        "Which officer handled the most cases?",
        "Who is the investigating officer on this FIR?",
        "How many officers are on record?",
    ):
        assert _match_kb_data_half_plan(q) is None, q


def test_module77_challan_plan_does_not_reach_cr7():
    """CR7's boundary, in the gate rather than in `xagg.py`.

    CR7 asks how many criminal-record cases are complete vs. pending and
    whether a separate court record agrees. It names no challan, no
    investigation and no report to a court — and it scores today on
    `criminal_record_court_crosscheck`, which this plan must not divert."""
    assert _match_kb_data_half_plan(_gold("CR7")["question"]) is None
    assert _gate(_gold("CR7")["question"]) is None


# The three NON-GOLD paraphrases Module 77 measured in process, against the
# real graph, through `rag_tool()` itself. Two of them (KB3's and KB8's)
# MISSED the patterns as first written — an ordinary English rewording of a
# question the module had just wired — and the patterns were widened in
# response. They are pinned here so the widening cannot silently rot, and
# recorded in `MODULE77_RESULT.md` §6 as a miss that happened rather than a
# success that was designed.
_PARAPHRASES = {
    "officer_role_pair": (
        "Under police law, is the person who records an FIR supposed to be a "
        "different officer from the one who investigates it — and what does "
        "our own data actually show about that?"
    ),
    "chalaan_dispatch": (
        "If an investigation drags on, does the law make the police report "
        "something to the court before it is finished, and does our own "
        "tracking data show whether that happened?"
    ),
    "death_investigation_charging": (
        "When a death looks suspicious the police must formally investigate "
        "the cause of death — does our system record that anywhere?"
    ),
}


@pytest.mark.parametrize("expected,text", sorted(_PARAPHRASES.items()))
def test_module77_non_gold_paraphrases_reach_the_same_plan(expected, text):
    """A capability fix, not a curve fit: none of these is the gold wording,
    and each must still reach the plan its subject belongs to — through the
    full gate, intent clause included."""
    assert _gate(text) == expected


def test_module77_widened_officer_pattern_still_needs_the_investigating_half():
    """The widening replaced a KB3-only literal with `<role-noun> who
    records/registers … investigat…`. It must not have turned into a bare
    "who records" match: a registration-only question has no second role to
    compare against and belongs to no plan."""
    for q in (
        "Which officer records an FIR when a complaint comes in?",
        "Is the person who records the FIR required to sign it?",
    ):
        assert _match_kb_data_half_plan(q) is None, q


def test_module39_plan_names_and_expected_kinds_are_unique():
    """A duplicated name would make the log line ambiguous and a duplicated
    aggregate would mean two question shapes silently share one figure."""
    names = [p.name for p in rag_mod._KB_DATA_HALF_PLANS]
    kinds = [p.expected_kind for p in rag_mod._KB_DATA_HALF_PLANS]
    assert len(set(names)) == len(names)
    assert len(set(kinds)) == len(kinds)


# ── The wiring: a compound KB question produces BOTH halves ──────────────

class _FakeXAggResult:
    def __init__(self, status, kind=None, text=None):
        self.status = status
        self.aggregate_kind = kind
        self.raw_summary_text = text


def _stub_xagg(monkeypatch, result, calls=None):
    """Stub `xagg_tool` at its own module, which is where `_run_kb_data_half`
    imports it from (lazily, inside the function)."""
    import src.pipeline.harness.tools.xagg as xagg_mod

    async def _xagg_tool(tool_input):
        if calls is not None:
            calls.append(tool_input.query_text)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(xagg_mod, "xagg_tool", _xagg_tool)


_AGG_TEXT = (
    "What happens to seized property — 45 property-register entr(ies) across "
    "28 FIR(s), grouped by the disposition recorded against each item: ..."
)


@pytest.mark.asyncio
async def test_module39_compound_kb_question_returns_a_norm_half_and_a_data_half(
    monkeypatch, stub_retrieval
):
    """THE MODULE'S CENTRAL PIN. KB4's literal gold question, asked in the
    All-Cases scope it is asked in live, must come back with BOTH halves:
    the retrieved statutory chunk(s) that answer "is there a standard?", and
    one machine-computed chunk that answers "does our property record follow
    it?". Before this module the second chunk did not exist and the answer
    said the documents could not confirm it."""
    calls = []
    _stub_xagg(
        monkeypatch,
        _FakeXAggResult(ToolStatus.OK, "seized_property_disposition", _AGG_TEXT),
        calls,
    )

    async def _statute(q):
        return []

    monkeypatch.setattr(rag_mod, "generate_statute_queries", _statute)

    result = await rag_tool(
        RagToolInput(query_text=_gold("KB4")["question"], execution=_kb_execution())
    )

    assert result.status is ToolStatus.OK
    data_halves = [c for c in result.chunks if c.metadata.source_tool == "XAGG"]
    norm_halves = [c for c in result.chunks if c.metadata.source_tool != "XAGG"]
    assert len(data_halves) == 1, "exactly one data half"
    assert norm_halves, "the statutory half must still be there"
    assert data_halves[0].text == _AGG_TEXT
    assert data_halves[0].id == "kb-data-half:property_register"
    # Appended LAST, so every legal chunk keeps the [Document N] position it
    # had before Module 39 — `verify_grounding()`'s citation check is
    # positional.
    assert result.chunks[-1] is data_halves[0]
    # One aggregate, and it is the one the plan names.
    assert len(calls) == 1
    plan = next(p for p in rag_mod._KB_DATA_HALF_PLANS if p.name == "property_register")
    assert calls[0] == plan.sub_query


@pytest.mark.asyncio
async def test_module39_non_compound_kb_question_dispatches_no_aggregate(
    monkeypatch, stub_retrieval
):
    """KB1 and KB2 are legal-KB questions whose data half is a SCHEMA claim,
    not a count. Both must behave byte-for-byte as they did before Module
    39 — no aggregate call, no extra chunk.

    KB3 and KB8 USED to be in this list, because their figures had no
    aggregate to reach (filed then as Modules 67/68). Modules 74/75 built
    those aggregates and Module 77 wired them, so they now belong to the
    positive control above; they were removed from here rather than left
    passing vacuously."""
    for qid in ("KB1", "KB2"):
        calls = []
        _stub_xagg(
            monkeypatch,
            _FakeXAggResult(ToolStatus.OK, "seized_property_disposition", _AGG_TEXT),
            calls,
        )

        async def _statute(q):
            return []

        monkeypatch.setattr(rag_mod, "generate_statute_queries", _statute)
        result = await rag_tool(
            RagToolInput(query_text=_gold(qid)["question"], execution=_kb_execution())
        )
        assert calls == [], f"{qid} must dispatch no aggregate"
        assert all(c.metadata.source_tool != "XAGG" for c in result.chunks), qid


@pytest.mark.asyncio
async def test_module39_case_narrative_question_dispatches_no_aggregate(
    monkeypatch, stub_retrieval
):
    """The negative control at the tool boundary rather than the pattern
    boundary: a plain case-data question never reaches the legal-KB path, so
    it never reaches this either."""
    calls = []
    _stub_xagg(
        monkeypatch,
        _FakeXAggResult(ToolStatus.OK, "seized_property_disposition", _AGG_TEXT),
        calls,
    )

    async def _statute(q):
        return []

    monkeypatch.setattr(rag_mod, "generate_statute_queries", _statute)
    result = await rag_tool(
        RagToolInput(
            query_text="What property was seized in FIR 214/26 and who recovered it?",
            execution=_kb_execution(),
        )
    )
    assert calls == []
    assert all(c.metadata.source_tool != "XAGG" for c in result.chunks)


@pytest.mark.asyncio
async def test_module39_case_scoped_caller_never_gets_a_data_half(
    monkeypatch, stub_retrieval
):
    """The scope clause of the gate. A caller with an active case is not in
    the `all_cases` pool the KB-only retry is offered on, so the compound
    dispatch is not offered either — a within-case question must not pull a
    whole-corpus figure into its answer."""
    calls = []
    _stub_xagg(
        monkeypatch,
        _FakeXAggResult(ToolStatus.OK, "seized_property_disposition", _AGG_TEXT),
        calls,
    )

    async def _statute(q):
        return []

    monkeypatch.setattr(rag_mod, "generate_statute_queries", _statute)
    scoped = ExecutionContext(
        caller=CallerContext(user_id="u1", role="platform-admin", active_case_id="fir-214-26")
    )
    result = await rag_tool(
        RagToolInput(query_text=_gold("KB4")["question"], execution=scoped)
    )
    assert calls == []
    assert all(c.metadata.source_tool != "XAGG" for c in result.chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        _FakeXAggResult(ToolStatus.DENIED, None, None),
        _FakeXAggResult(ToolStatus.FAILED, None, None),
        _FakeXAggResult(ToolStatus.OK, "seized_property_disposition", ""),
        # The drift guard: the right status, real text, WRONG family.
        _FakeXAggResult(ToolStatus.OK, "case_listing", "73 cases ..."),
        RuntimeError("age_client is down"),
    ],
)
async def test_module39_a_failed_aggregate_degrades_to_the_statutory_half_alone(
    monkeypatch, stub_retrieval, outcome
):
    """The degradation contract, which is the whole safety argument: a
    denial, an upstream failure, an empty rendering, a drifted aggregate
    family and a raised exception all return the pre-Module-39 answer rather
    than an error or a wrong figure."""
    _stub_xagg(monkeypatch, outcome)

    async def _statute(q):
        return []

    monkeypatch.setattr(rag_mod, "generate_statute_queries", _statute)
    result = await rag_tool(
        RagToolInput(query_text=_gold("KB4")["question"], execution=_kb_execution())
    )
    assert result.status is ToolStatus.OK
    assert result.chunks, "the statutory half survives every aggregate failure"
    assert all(c.metadata.source_tool != "XAGG" for c in result.chunks)


@pytest.mark.asyncio
async def test_module39_an_abstaining_retrieval_is_not_rescued_by_the_data_half(
    monkeypatch, stub_retrieval
):
    """A run where retrieval abstained has NO statutory half, and answering
    such a question with a bare corpus figure would be a new failure mode —
    a confident half-answer replacing an honest abstention. The abstention
    stands."""
    _stub_xagg(
        monkeypatch,
        _FakeXAggResult(ToolStatus.OK, "seized_property_disposition", _AGG_TEXT),
    )

    async def _statute(q):
        return []

    async def _evaluate_reject(orig, cur, reranked):
        return {"relevant": False, "reason": "not statutory"}

    async def _rewrite(*args, **kwargs):
        return "rewritten"

    monkeypatch.setattr(rag_mod, "generate_statute_queries", _statute)
    monkeypatch.setattr(rag_mod, "evaluate_relevance", _evaluate_reject)
    monkeypatch.setattr(rag_mod, "rewrite_for_retry", _rewrite)

    result = await rag_tool(
        RagToolInput(query_text=_gold("KB4")["question"], execution=_kb_execution())
    )
    assert result.status is ToolStatus.EMPTY
    assert not result.chunks


@pytest.mark.asyncio
async def test_module39_the_data_half_is_shown_to_the_relevance_gate(
    monkeypatch, stub_retrieval
):
    """The correction the first live sweep forced, pinned so it cannot be
    undone by accident.

    Wired only into the RESULT, the data half arrived after the gate — and
    KB9 abstained 3 of 3 with the gate saying, verbatim on all 18 refusals,
    that the documents describe inquest procedure and do not say whether OUR
    system records one. That verdict is correct on a compound question judged
    against statute-only evidence. The chunk that answers the second half now
    goes in front of the gate, last, so the legal chunks keep their
    `[Document N]` positions."""
    seen = {}

    async def _evaluate(orig, cur, reranked):
        seen["ids"] = [c.get("id") for c in reranked]
        return {"relevant": True, "reason": "ok"}

    async def _statute(q):
        return []

    monkeypatch.setattr(rag_mod, "evaluate_relevance", _evaluate)
    monkeypatch.setattr(rag_mod, "generate_statute_queries", _statute)
    _stub_xagg(
        monkeypatch,
        _FakeXAggResult(ToolStatus.OK, "seized_property_disposition", _AGG_TEXT),
    )

    result = await rag_tool(
        RagToolInput(query_text=_gold("KB4")["question"], execution=_kb_execution())
    )
    assert seen["ids"][-1] == "kb-data-half:property_register"
    assert len(seen["ids"]) > 1, "the statutory chunks are still there, and first"
    assert [c.id for c in result.chunks] == seen["ids"]


@pytest.mark.asyncio
async def test_module39_the_aggregate_runs_once_across_scope_retries(
    monkeypatch, stub_retrieval
):
    """The KB path can run its retrieve→evaluate loop several times (up to
    `MAX_RETRIES` per scope, over two scopes). The aggregate is dispatched
    ONCE and memoised — re-running it per round would turn a 0.6 s overlap
    into a serial cost paid six times."""
    calls = []
    _stub_xagg(
        monkeypatch,
        _FakeXAggResult(ToolStatus.OK, "seized_property_disposition", _AGG_TEXT),
        calls,
    )

    async def _statute(q):
        return []

    verdicts = iter([False, False, True])

    async def _evaluate(orig, cur, reranked):
        return {"relevant": next(verdicts, True), "reason": "…"}

    async def _rewrite(*args, **kwargs):
        return "rewritten"

    monkeypatch.setattr(rag_mod, "generate_statute_queries", _statute)
    monkeypatch.setattr(rag_mod, "evaluate_relevance", _evaluate)
    monkeypatch.setattr(rag_mod, "rewrite_for_retry", _rewrite)

    await rag_tool(
        RagToolInput(query_text=_gold("KB4")["question"], execution=_kb_execution())
    )
    assert len(calls) == 1


# ── The prompt half: Semantic Search must be TOLD the question is compound ─

def test_module39_semantic_search_adds_the_compound_rule_only_for_a_data_half():
    """`rag_tool()` supplies the figure; this is what makes the answer spend
    a sentence on it. Gated on the data-half chunk alone, so the prompt every
    other question sees is byte-for-byte the pre-Module-39 one."""
    from src.pipeline.harness.agents.semantic_search import (
        _COMPOUND_ANSWER_RULE,
        _compound_block,
    )
    from src.pipeline.harness.types import ChunkMetadata, EvidenceChunk

    legal = EvidenceChunk(
        id="c1", text="s.154 ...", metadata=ChunkMetadata(source_tool="RAG", source_file="crpc.pdf")
    )
    data = EvidenceChunk(
        id="kb-data-half:property_register",
        text=_AGG_TEXT,
        metadata=ChunkMetadata(source_tool="XAGG", source_file="our own case records"),
    )
    assert _compound_block([legal]) == ""
    assert _compound_block([]) == ""
    assert _compound_block([legal, data]) == _COMPOUND_ANSWER_RULE
    # It must not smuggle gold's own figures into the prompt.
    for forbidden in ("45", "32", "8 ", "26", "68", "302", "27.16"):
        assert forbidden not in _COMPOUND_ANSWER_RULE


# ── 9. Module 65: the evidence-book branch of the statute-hypothesis prompt ──
#
# KB2's gold rests on Qanun-e-Shahadat Order 1984 Arts 38/39 and CrPC s.162,
# and Module 52 measured 0 of 6 live runs retrieving any of them. Module 65's
# probe (scripts/module65_probe.py) established, by chunk id, that all three
# ARE in the corpus —
#     2_qanun-e-shahadat-order-1984_pdf_3e604153_c176   (Art. 38)
#     2_qanun-e-shahadat-order-1984_pdf_3e604153_c177   (Art. 39)
#     1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_f9908363_c675  (s.162(1))
# — and that no query the pipeline generated reached ANY of them, because the
# model read "why is this not recorded?" as a record-keeping question and spent
# both hypotheses on the Punjab Police Rules' case diary and CrPC s.161.
#
# The fix is entirely in `prompts/statute_hypothesis.txt`: an evidence-book
# selection branch, plus the corpus's own verb for it. Measured on the live
# store, `{"is_global": True}`, top-30 per query:
#     before — every target ABSENT from every generated query
#     after  — Art. 38 rank 3, Art. 39 rank 6 (hypothesis 1, Qanun-e-Shahadat)
#              s.162(1) rank 1, s.162 heading rank 12 (hypothesis 2, CrPC)
#
# A prompt is the mechanism, so the prompt is what these tests pin: the rule
# must survive in the template AND reach the model in the built system prompt.
# `generate_statute_queries()` stubs the LLM everywhere else in this file, so
# nothing here would notice the rule being deleted.

_STATUTE_PROMPT = (
    Path(__file__).resolve().parent.parent / "prompts" / "statute_hypothesis.txt"
).read_text(encoding="utf-8")


def test_statute_prompt_has_an_evidence_book_selection_branch():
    """Module 65. Before this rule the model had three book-selection branches
    — a STEP (CrPC), WHO in the force (Police Order), WHAT IS WRITTEN DOWN
    (Punjab Police Rules) — and a question about whether material may be USED
    fell into the third. That is what sent KB2 to the case diary."""
    assert "WHETHER SOMETHING MAY BE USED" in _STATUTE_PROMPT
    lowered = _STATUTE_PROMPT.lower()
    assert "qanun-e-shahadat order, 1984, and" in lowered, (
        "the evidence branch must name the book it routes to"
    )
    # The measured lexical point: "may not be used as evidence" retrieved
    # Art. 38 at rank 88; the book's own "shall be proved" put it at rank 3.
    assert '"shall be proved"' in _STATUTE_PROMPT
    assert '"admissible"' in _STATUTE_PROMPT


def test_statute_prompt_routes_an_absent_record_to_admissibility():
    """Rule 4c. Gold's shape is that an apparent data gap is legally REQUIRED,
    so 'why is this not kept?' has to reach the provision that bars the use of
    the material, not only the one that says what may be written down."""
    assert "4c." in _STATUTE_PROMPT
    rule = _STATUTE_PROMPT.split("4c.", 1)[1].split("\n", 1)[0].lower()
    assert "admissibility" in rule
    assert "not written down" in rule or "not kept" in rule
    # Measured live, and this is the part that had to be PROHIBITIVE rather
    # than advisory: "spend at least one query on..." left the model spending
    # its second slot on the Punjab Police Rules' case diary anyway, and that
    # one query pulled the whole reranked window back to case-diary material —
    # KB2 retrieved 0 of 2 gold chunks live even with Art. 38 at rank 3 of its
    # own query. Forbidding the record-keeping book for THIS question shape is
    # what puts the CrPC bar in the second slot and gold's chunks in the pool.
    assert "spend no query on the recording or register provision" in rule


def test_statute_prompt_corpus_map_describes_the_qso_confession_bar():
    """The Qanun-e-Shahadat's map entry had to say what it actually covers.
    "confessions" alone did not distinguish it from the recording books, and
    the model routed KB2 to the case diary.

    Deliberately asserted on the map line ONLY, not on the CrPC's. Enlarging
    the CrPC map entry with s.162's subject was measured and REVERTED: it
    hijacked KB5's first hypothesis away from the Anti-Rape Act (§7), and the
    same material sits in the WHETHER-SOMETHING-MAY-BE-USED branch instead,
    where it fires on the question shape rather than on the book."""
    qso = next(l for l in _STATUTE_PROMPT.splitlines() if l.startswith("- Qanun-e-Shahadat"))
    crpc = next(l for l in _STATUTE_PROMPT.splitlines() if l.startswith("- Code of Criminal"))
    assert "confession made to a police officer" in qso.lower()
    assert "police custody" in qso.lower()
    assert "inquiry or trial" not in crpc.lower(), (
        "the CrPC map entry must stay as Module 30 left it — see this test's docstring"
    )
    branch = _STATUTE_PROMPT.split("WHETHER SOMETHING MAY BE USED", 1)[1].split(chr(10), 1)[0]
    assert "inquiry or trial" in branch.lower()


def test_statute_prompt_names_the_evidence_books_own_noun():
    """Measured, top-30 on the live store: a hypothesis phrased "statements
    made to police ... shall not be proved" put Art. 38 at rank 17 and lost
    Art. 39 entirely; the same hypothesis phrased "confession made to a police
    officer" put them at ranks 3 and 7."""
    rule = _STATUTE_PROMPT.split("4c.", 1)[1].split(chr(10), 1)[0]
    assert '"confession"' in rule
    assert '"statement"' in rule


def test_module_30_selection_rules_survive_the_module_65_edit():
    """Module 65 added a fourth branch to a map Module 30 measured. Its three
    existing branches, and rules 3/4/4b, are load-bearing for KB3/KB8/KB9 and
    must not be collateral damage."""
    for marker in (
        "A STEP in a criminal case",
        "WHO inside the police force may do something",
        "WHAT IS WRITTEN DOWN",
        "the words the provision ITSELF would use",
        "Each query must target a DIFFERENT statute book",
        "4b. Match the ACTOR",
    ):
        assert marker in _STATUTE_PROMPT, marker


@pytest.mark.asyncio
async def test_kb2_gold_text_reaches_the_llm_with_the_evidence_branch(monkeypatch):
    """End of the chain that matters: the rule has to survive `{n}`/`{query}`
    substitution and actually be in the system prompt sent for KB2's LITERAL
    gold text. A template edit that broke substitution would pass every test
    above and change nothing live."""
    seen = {}

    async def _call_llm_json(**kwargs):
        seen.update(kwargs)
        return ["Qanun-e-Shahadat Order 1984 confession to a police officer not to be proved"], ""

    monkeypatch.setattr(statute_mod, "call_llm_json", _call_llm_json)
    question = _gold("KB2")["question"]
    await generate_statute_queries(question, n=2)

    system_prompt = seen["system_prompt"]
    assert "WHETHER SOMETHING MAY BE USED" in system_prompt
    assert "4c." in system_prompt
    assert question in system_prompt
    assert "{n}" not in system_prompt and "{query}" not in system_prompt


def test_default_hypotheses_stays_at_two():
    """Module 65 measured a third hypothesis on KB2 with the fixed prompt: the
    model spent it on a third Qanun-e-Shahadat phrasing that retrieved none of
    gold's chunks, while hypotheses 1 and 2 already returned all three. Module
    38 showed a wrong third slot is no longer *harmful* under RRF fusion; it is
    still not useful here, so the budget is unchanged and the reason recorded."""
    assert statute_mod.DEFAULT_HYPOTHESES == 2


# ── 10. Module 64: the exhibit-handling branch of the statute-hypothesis prompt ──
#
# KB6's gold answer rests on ONE chunk of the forensics guidelines —
#     5_Forensics_guidelines_pdf_62ee00b3_c19
#       "Every evidence exhibit must be packaged separately. Every firearm must
#        be packaged in unloaded condition with safety on. There must not be
#        live rounds in the chamber of the firearm, magazine or in the parcel."
# — and Module 52 measured 3 of 3 live runs landing on _c116/_c0 instead.
#
# Module 64's offline probe (scripts/module64_probe.py) ruled out the two
# mechanisms the tracker proposed, and those negative results are why this rule
# is worded the way it is. At the KB-only scope, top-30 per query:
#   - NOT Roman-Urdu. A Roman-Urdu paraphrase that says "pack" reaches c19 at
#     rank 2-3; gold's own Urdu-SCRIPT rendering misses it exactly as gold's
#     Roman-Urdu does, so the script is not the variable.
#   - NOT compoundness. Gold's NORM CLAUSE ALONE, in gold's own vocabulary,
#     still misses c19 entirely — deleting the data clause changes nothing.
#   - It is VOCABULARY. c19 never says "handle" or "register"; it is written
#     entirely in the verb "packaged". Gold asks how a weapon is "handled"
#     before it is "recorded", and that phrasing retrieves the guidelines'
#     GENERAL opening passage (_c0, _c94, _c116 — documenting, labelling,
#     inventorying) which the firearm section then sits below.
#
# The one component built to carry the corpus's own vocabulary is the statute
# hypothesis, and it was copying this prompt's own Forensics map line verbatim:
#     "Forensics guidelines handling packaging labelling and transport of
#      recovered evidence ... chain of custody"          -> c19 at rank 29
#     "Forensics guidelines packing of a recovered firearm as evidence: how a
#      firearm exhibit must be packaged before submission to the laboratory"
#                                                        -> c19 at rank 1
# So the map line WAS the query, and it never named an exhibit type.
#
# Measured over the full live variant set (question + 2 expansions +
# cross-script variant + 2 hypotheses + English rendering), merged and cut to
# TOP_K_RETRIEVAL * CROSS_CASE_RETRIEVAL_MULTIPLIER = 30:
#     before — c19 at merged rank 53/54/54, below the cut, 3 of 3 trials
#     after  — c19 at merged rank 1, 3 of 3 trials
#
# The fix is entirely in `prompts/statute_hypothesis.txt`, and is deliberately
# about EXHIBIT TYPES IN GENERAL rather than about firearms: an English
# paraphrase, a Roman-Urdu paraphrase and a question about a BLOOD SAMPLE (a
# different section of the same document) all move to rank 1-3 from 27, absent
# and 12 respectively.


def test_statute_prompt_has_an_exhibit_handling_branch():
    """Module 64. Before this branch, "how must a recovered weapon be handled
    before it is recorded?" fell into WHAT IS WRITTEN DOWN and spent a query on
    the Punjab Police Rules' weapon register — the book that says which
    register the item goes in, not the one that says what state the item must
    be in."""
    assert "HOW A PHYSICAL EXHIBIT MUST BE PREPARED" in _STATUTE_PROMPT
    branch = _STATUTE_PROMPT.split(
        "HOW A PHYSICAL EXHIBIT MUST BE PREPARED", 1
    )[1].split(chr(10), 1)[0].lower()
    assert "forensics guidelines" in branch
    assert "punjab police rules" in branch, (
        "the branch must say which book it is taking the question AWAY from"
    )


def test_statute_prompt_corpus_map_says_the_guidelines_are_per_exhibit_type():
    """The map line was the failing component: the model copied it verbatim as
    its query, and it described the document as one undifferentiated chapter on
    "recovered evidence". It has to say the document is split by exhibit type,
    or the query cannot name one."""
    forensics = next(
        l for l in _STATUTE_PROMPT.splitlines() if l.startswith("- Forensics guidelines")
    )
    lowered = forensics.lower()
    assert "by exhibit type" in lowered
    for exhibit in ("firearms and tool marks", "biological", "digital"):
        assert exhibit in lowered, exhibit


def test_statute_prompt_rule_4d_demands_an_exhibit_type_and_the_packaging_verb():
    """Rule 4d. Naming the book is not enough — a query about "recovered
    evidence" in general retrieves only the guidelines' opening passage. The
    rule has to be prohibitive about the general form, the way Module 65's 4c
    had to be."""
    assert "4d." in _STATUTE_PROMPT
    rule = _STATUTE_PROMPT.split("4d.", 1)[1].split(chr(10), 1)[0]
    lowered = rule.lower()
    assert "name the exhibit type" in lowered
    assert '"packaged"' in rule, "the guidelines' own verb must be named"
    assert 'do not write a query about "recovered evidence" in general' in lowered


def test_module_64_rule_is_not_keyed_to_kb6s_wording():
    """The anti-goal, pinned. Three waves of gold-specific patterns are how the
    router accumulated its debt; a retrieval fix keyed to one question's string
    is the same mistake in a different file. Nothing distinctive to KB6's gold
    text — or to any paraphrase of it used while measuring, or to its gold
    ANSWER — may appear in this prompt."""
    lowered = _STATUTE_PROMPT.lower()
    for gold_ism in (
        "baramad", "aslaha", "hathyar", "hathiyar", "makhsoos", "sambhala",
        "weapon register", "unloaded", "safety on", "live round",
    ):
        assert gold_ism not in lowered, gold_ism
    # And the worked example must be about a DIFFERENT exhibit type than the
    # question that motivated the rule.
    assert "blood sample" in lowered
    assert "namoona" in lowered


def test_module_30_and_65_selection_rules_survive_the_module_64_edit():
    """Module 64 added a fifth branch and a fourth sub-rule to a map Modules 30
    and 65 both measured. Everything they pinned is load-bearing for KB1/KB2/
    KB3/KB8/KB9 and must not be collateral damage."""
    for marker in (
        "A STEP in a criminal case",
        "WHO inside the police force may do something",
        "WHAT IS WRITTEN DOWN",
        "WHETHER SOMETHING MAY BE USED",
        "the words the provision ITSELF would use",
        "Each query must target a DIFFERENT statute book",
        "4b. Match the ACTOR",
        "4c.",
        '"shall be proved"',
    ):
        assert marker in _STATUTE_PROMPT, marker


@pytest.mark.asyncio
async def test_kb6_gold_text_reaches_the_llm_with_the_exhibit_branch(monkeypatch):
    """Same end-of-chain check Module 65 made for KB2: the branch and rule 4d
    have to survive `{n}`/`{query}` substitution and actually be in the system
    prompt sent for KB6's LITERAL gold text."""
    seen = {}

    async def _call_llm_json(**kwargs):
        seen.update(kwargs)
        return ["Forensics guidelines packaging of a firearm exhibit"], ""

    monkeypatch.setattr(statute_mod, "call_llm_json", _call_llm_json)
    question = _gold("KB6")["question"]
    await generate_statute_queries(question, n=2)

    system_prompt = seen["system_prompt"]
    assert "HOW A PHYSICAL EXHIBIT MUST BE PREPARED" in system_prompt
    assert "4d." in system_prompt
    assert question in system_prompt
    assert "{n}" not in system_prompt and "{query}" not in system_prompt


def test_module_64_blast_radius_is_exactly_the_eight_kb_questions():
    """A prompt edit is broad and quiet, so the bound is asserted rather than
    argued: `generate_statute_queries()` is only ever called under
    `_is_legal_kb_intent()`, and that predicate is True for exactly the eight
    gold KB questions and False for the other twenty-four. Nothing outside
    KB1-KB9 can be moved by anything in this prompt."""
    gold = json.loads(
        (
            Path(__file__).resolve().parent.parent
            / "evaluation"
            / "Gold_QA_Dataset_Final32_With_Answers.json"
        ).read_text(encoding="utf-8")
    )
    reached = {q["id"] for q in gold if _is_legal_kb_intent(q["question"])}
    assert reached == {"KB1", "KB2", "KB3", "KB4", "KB5", "KB6", "KB8", "KB9"}
    assert len(gold) == 32
