"""
Retrieval primitives (RRF, BM25, chunking) and conversation memory.

These are pure functions — no mocking needed, so they're the cheapest place to
pin down behaviour the rest of the pipeline depends on.
"""
import pytest

from src.retrieval.reranker import reciprocal_rank_fusion, rerank_results
from src.retrieval.bm25_retriever import retrieve_bm25
from src.ingestion.chunker import split_text_into_chunks
from src.memory.conversation import (
    format_history_for_prompt,
    _truncate_to_token_budget,
)


def _chunk(chunk_id, text="some text", rrf_score=None):
    chunk = {"id": chunk_id, "text": text, "metadata": {"source": "doc.pdf"}}
    if rrf_score is not None:
        chunk["rrf_score"] = rrf_score
    return chunk


# ── Reciprocal Rank Fusion ────────────────────────────────────────────────────

def test_rrf_ranks_a_document_found_by_both_retrievers_first():
    """
    The whole point of RRF: agreement across signals beats a single strong hit.
    'a' is the top semantic hit but invisible to BM25; 'b' places well in both,
    so 'b' must win.
    """
    semantic = [_chunk("a"), _chunk("b"), _chunk("c")]
    bm25 = [_chunk("b")]

    fused = reciprocal_rank_fusion([semantic, bm25], top_k=3)

    assert fused[0]["id"] == "b", "the doc ranked by BOTH retrievers should win"


def test_rrf_is_order_stable_for_tied_documents():
    """A fully symmetric input ties on score; ranking must still be deterministic."""
    semantic = [_chunk("a"), _chunk("b"), _chunk("c")]
    bm25 = [_chunk("c"), _chunk("b"), _chunk("a")]

    first = reciprocal_rank_fusion([semantic, bm25], top_k=3)
    second = reciprocal_rank_fusion([semantic, bm25], top_k=3)

    assert [c["id"] for c in first] == [c["id"] for c in second]


def test_rrf_deduplicates_documents():
    semantic = [_chunk("a"), _chunk("b")]
    bm25 = [_chunk("a"), _chunk("b")]

    fused = reciprocal_rank_fusion([semantic, bm25], top_k=10)

    assert len(fused) == 2
    assert {c["id"] for c in fused} == {"a", "b"}


def test_rrf_attaches_a_score():
    fused = reciprocal_rank_fusion([[_chunk("a")], [_chunk("a")]], top_k=1)
    assert fused[0]["rrf_score"] > 0


def test_rrf_respects_top_k():
    semantic = [_chunk(str(i)) for i in range(10)]
    assert len(reciprocal_rank_fusion([semantic], top_k=3)) == 3


def test_rrf_handles_one_empty_retriever():
    """BM25 returns nothing on a query with no lexical overlap — must not crash."""
    fused = reciprocal_rank_fusion([[_chunk("a")], []], top_k=5)
    assert [c["id"] for c in fused] == ["a"]


def test_rrf_handles_both_empty():
    assert reciprocal_rank_fusion([[], []], top_k=5) == []


def test_rerank_results_is_the_orchestrator_facing_wrapper():
    """The orchestrator calls this two-list wrapper, not RRF directly."""
    reranked = rerank_results([_chunk("a"), _chunk("b")], [_chunk("b")], top_k=1)

    assert len(reranked) == 1
    assert reranked[0]["id"] == "b"


# ── Semantic confidence floor ──────────────────────────────────────────────────

def test_semantic_floor_rescues_a_high_confidence_bm25_invisible_chunk():
    """
    Root cause confirmed live (2026-08-02): a chunk with very high raw
    semantic similarity but zero BM25 presence loses pure RRF fusion to
    chunks that rank only moderately in BOTH lists, because RRF only ever
    weighs ORDINAL rank, never similarity magnitude. rerank_results() must
    rescue it rather than silently drop it.
    """
    # "real" is the single strongest semantic hit (0.93) but BM25 never
    # finds it at all — exactly the observed REAL-004-copy-of-fir-procedure
    # failure. "d1".."d10" are 10 mediocre chunks that rank in BOTH lists,
    # which is enough for plain RRF to push "real" out of top_k=10 entirely.
    semantic = [_chunk("real", rrf_score=0.93)] + [
        _chunk(f"d{i}", rrf_score=0.5) for i in range(1, 11)
    ]
    bm25 = [_chunk(f"d{i}") for i in range(10, 0, -1)]

    reranked = rerank_results(semantic, bm25, top_k=10)

    assert "real" not in {c["id"] for c in reciprocal_rank_fusion([semantic, bm25], top_k=10)}, (
        "test setup check: plain RRF must actually drop it, or this test proves nothing"
    )
    assert "real" in {c["id"] for c in reranked}, (
        "a very-high-confidence semantic-only hit must be rescued into the pool"
    )


def test_semantic_floor_ignores_chunks_below_the_threshold():
    """A merely-decent semantic-only score must NOT be force-included — only
    confidently-strong hits get the floor, or this becomes an unbounded
    semantic-search bypass of hybrid fusion."""
    semantic = [_chunk("weak", rrf_score=0.5)] + [
        _chunk(f"d{i}", rrf_score=0.5) for i in range(1, 11)
    ]
    bm25 = [_chunk(f"d{i}") for i in range(10, 0, -1)]

    reranked = rerank_results(semantic, bm25, top_k=10)

    assert "weak" not in {c["id"] for c in reranked}


# ── BM25 ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def corpus():
    return [
        _chunk("1", "Vehicle theft on a busy road is recorded as follows"),
        _chunk("2", "Reported theft of a mobile phone by the complainant was booked officially"),
        _chunk("3", "FIR registration threshold requires an original CNIC for verification"),
        _chunk("4", "Penalty for late filing of an FIR complaint under section 182 of the code"),
    ]


def test_bm25_ranks_lexically_matching_documents_higher(corpus):
    results = retrieve_bm25("mobile phone complainant", corpus, top_k=4)

    assert results[0]["id"] == "2"


def test_bm25_attaches_a_score(corpus):
    results = retrieve_bm25("mobile", corpus, top_k=1)

    assert results[0]["bm25_score"] > 0


def test_bm25_drops_documents_with_no_lexical_overlap(corpus):
    """Zero-score docs are filtered out — they contribute nothing to fusion."""
    results = retrieve_bm25("mobile", corpus, top_k=4)

    assert [r["id"] for r in results] == ["2"]


def test_bm25_on_empty_corpus_returns_nothing():
    assert retrieve_bm25("anything", [], top_k=5) == []


def test_bm25_contributes_nothing_on_a_tiny_corpus():
    """
    Documents a real BM25Okapi quirk: on a corpus of 2-3 docs, IDF for a term
    present in half of them goes negative and is clamped to zero, so every score
    is 0 and the >0 filter drops everything. Harmless in production (the
    candidate pool is TOP_K_RETRIEVAL chunks), but it means BM25 silently adds
    no signal if that pool ever shrinks — RRF then rides on semantic alone.
    """
    tiny = [
        _chunk("1", "vehicle theft on a busy road"),
        _chunk("2", "reported theft of a mobile phone by the complainant"),
    ]

    assert retrieve_bm25("mobile complainant", tiny, top_k=2) == []


# ── Chunking ──────────────────────────────────────────────────────────────────

def test_chunking_respects_the_size_limit():
    text = "word " * 500

    chunks = split_text_into_chunks(text, chunk_size=100, chunk_overlap=20)

    assert chunks
    assert all(len(c) <= 100 for c in chunks)


def test_chunking_never_cuts_mid_word():
    text = "supercalifragilistic " * 30

    chunks = split_text_into_chunks(text, chunk_size=50, chunk_overlap=10)

    for chunk in chunks:
        assert not chunk.startswith("ic "), "a word was split across chunks"


def test_short_text_stays_a_single_chunk():
    assert split_text_into_chunks("A short clause.", chunk_size=512) == ["A short clause."]


def test_empty_text_yields_no_chunks():
    assert split_text_into_chunks("", chunk_size=512) == []


def test_chunk_boundaries_land_on_urdu_sentence_ends():
    """
    Phase 2.3. The old break-point search could only find spaces and
    \\n\\n, so in Urdu — where the sentence-final mark is ۔ (U+06D4) —
    every chunk boundary was arbitrary and usually mid-clause.
    """
    sentence = "ملزم کو گرفتار کر لیا گیا اور مقدمہ درج کر لیا گیا ہے۔ "
    text = sentence * 20

    chunks = split_text_into_chunks(text, chunk_size=200, chunk_overlap=20)

    assert len(chunks) > 1
    # Every chunk but the last ends where a sentence ends.
    for chunk in chunks[:-1]:
        assert chunk.endswith("۔"), f"chunk breaks mid-sentence: ...{chunk[-40:]!r}"


def test_chunking_falls_back_to_whitespace_without_sentence_boundaries():
    """The sentence-boundary snap is a preference, not a requirement: a
    single sentence longer than chunk_size must still chunk, and still
    never cut a word."""
    text = "ملزم " * 200  # no terminator anywhere

    chunks = split_text_into_chunks(text, chunk_size=100, chunk_overlap=20)

    assert chunks
    assert all(len(c) <= 100 for c in chunks)
    assert all(c.strip() == c for c in chunks)


def test_chunking_respects_size_limit_on_urdu_text():
    text = "یہ ایک جملہ ہے۔ " * 60

    chunks = split_text_into_chunks(text, chunk_size=150, chunk_overlap=30)

    assert all(len(c) <= 150 for c in chunks)


def _markdown_table_row(label: str, value: str, label_width: int, value_width: int) -> str:
    """Mimics Docling's markdown table export, which right-pads every cell
    in a column to match that column's widest cell."""
    return f"| {label.ljust(label_width)} | {value.ljust(value_width)} |"


def test_chunking_does_not_flood_on_docling_style_table_padding():
    """
    Regression (Phase 4 dry-pass finding): Docling's markdown table export
    right-pads every cell in a column to match the widest cell in that
    column — real FIR forms measured whitespace runs up to 299 spaces from
    one long narrative field padding out short ones.

    The old advance formula (max(1, (end-start) - chunk_overlap)) could
    rediscover the same nearby paragraph break on every iteration once
    `start` crept within chunk_overlap of it, advancing 1 character at a
    time and emitting a near-duplicate micro-chunk at every step.
    FIR-2026-THEFT-001.pdf: 5070 real chars produced 141 chunks, 128 of
    them under 100 chars — this test reproduces that shape directly,
    independent of Docling or any real file.
    """
    label_width, value_width = 80, 220
    rows = [
        _markdown_table_row("1. Date and time of occurrence:", "2026-01-01, 23:17 hours", label_width, value_width),
        _markdown_table_row("2. Day:", "Thursday", label_width, value_width),
        _markdown_table_row("3. Date and time of report at P.S.:", "2026-01-03, 01:01 hours", label_width, value_width),
        _markdown_table_row("4. General Diary reference and number:", "DD No. 46 dated 2026-01-03", label_width, value_width),
        _markdown_table_row("5. Name and address of informant:", "Irfan Mirza, contact 0363-8536477", label_width, value_width),
    ]
    text = "## FIRST INFORMATION REPORT\n\n" + "\n".join(rows)

    chunks = split_text_into_chunks(text, chunk_size=512, chunk_overlap=64)

    # A sane chunker produces roughly text_len / chunk_size chunks, not a
    # flood of hundreds of near-duplicate fragments.
    max_sane = (len(text) // 512) + 3
    assert len(chunks) <= max_sane, (
        f"expected at most ~{max_sane} chunks for {len(text)} chars, got {len(chunks)} "
        "— looks like the 1-char advance-floor crawl bug"
    )

    # The crawl's signature: many chunks that are 1-character-shifted
    # near-duplicates of their neighbor. At most the legitimate short
    # header/trailing-remainder chunks should be tiny.
    tiny = [c for c in chunks if len(c) < 30]
    assert len(tiny) <= 2, f"found {len(tiny)} near-empty chunks (the crawl signature): {tiny[:5]}"


def test_chunking_advance_never_stalls():
    """
    Direct test of the advance-floor fix itself: construct text where the
    nearest break point is always right at the edge of chunk_overlap from
    `start` (the exact trap condition), and assert consecutive chunks
    don't degrade into 1-character-shrinking near-duplicates of each other.
    """
    text = "Header.\n\n" + ("x" * 2000)

    chunks = split_text_into_chunks(text, chunk_size=512, chunk_overlap=64)

    for i in range(1, len(chunks)):
        prev, curr = chunks[i - 1], chunks[i]
        is_one_char_shift = (
            len(curr) == len(prev) - 1 and prev.endswith(curr[-10:]) and curr in prev
        )
        assert not is_one_char_shift, (
            f"chunk {i} looks like a 1-char-shifted near-duplicate of chunk {i-1}: "
            f"{prev[:40]!r} -> {curr[:40]!r}"
        )


# ── Conversation memory ───────────────────────────────────────────────────────

def test_history_is_formatted_with_speaker_labels():
    history = [
        {"role": "user", "content": "What PPC section covers mobile theft?"},
        {"role": "assistant", "content": "379 PPC."},
    ]

    formatted = format_history_for_prompt(history)

    assert "User: What PPC section covers mobile theft?" in formatted
    assert "Assistant: 379 PPC." in formatted


def test_injected_system_context_is_labelled_system_not_assistant():
    """
    Regression: project context is injected as a system message, but the
    formatter labelled every non-user message "Assistant" — so the model read
    the project brief as something it had said itself.
    """
    history = [{"role": "system", "content": "Client exports towels"}]

    formatted = format_history_for_prompt(history)

    assert formatted.startswith("System:")


def test_empty_history_formats_to_empty_string():
    assert format_history_for_prompt([]) == ""


def test_token_budget_drops_the_oldest_turns_first():
    history = [
        {"role": "user", "content": "old question " * 100},
        {"role": "assistant", "content": "old answer " * 100},
        {"role": "user", "content": "recent question"},
        {"role": "assistant", "content": "recent answer"},
    ]

    trimmed = _truncate_to_token_budget(history, max_tokens=50)

    assert trimmed[-1]["content"] == "recent answer", "the newest turn must survive"
    assert len(trimmed) < len(history)


def test_token_budget_keeps_history_under_the_limit_when_possible():
    history = [{"role": "user", "content": "x" * 4000},
               {"role": "assistant", "content": "y" * 4000},
               {"role": "user", "content": "short"},
               {"role": "assistant", "content": "short"}]

    trimmed = _truncate_to_token_budget(history, max_tokens=100)

    assert sum(len(m["content"]) for m in trimmed) // 4 <= 100


def test_short_history_is_untouched():
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert _truncate_to_token_budget(history, max_tokens=1000) == history


# ── Project-memory denial gate (Phase 8, Bug 2) ───────────────────────────────

from src.pipeline.memory_updater import _establishes_new_fact, update_project_memory


def test_denial_responses_establish_no_fact():
    """A 'no information' / denial turn must be recognised as establishing
    nothing — folding it into memory corrupts the rolling summary."""
    denials = [
        "The provided documents do not contain information about case Zeta.",
        "There is no record of a previously requested codename.",
        "I couldn't find any information on that.",
        "I don't have access to that record.",
    ]
    for d in denials:
        assert _establishes_new_fact(d) is False, f"should be treated as a denial: {d!r}"


def test_real_answers_establish_a_fact():
    facts = [
        "The evidence locker access code is FALCON-NINE-ZULU.",
        "This case is internally codenamed 'Operation Nightingale'.",
        "Tenant registration requires the tenant's CNIC and a rent agreement.",
    ]
    for f in facts:
        assert _establishes_new_fact(f) is True, f"should be treated as a fact: {f!r}"


class _MemoryGatewaySpy:
    """Minimal gateway double: records whether memory was written."""
    def __init__(self, existing=""):
        self._existing = existing
        self.upserted = None

    async def get_project_memory(self, project_id):
        return {"summary_text": self._existing} if self._existing else None

    async def upsert_project_memory(self, project_id, summary_text):
        self.upserted = summary_text


@pytest.mark.asyncio
async def test_denial_turn_does_not_update_memory(monkeypatch):
    """
    The compounding-corruption root cause (Phase 8, Bug 2): a RAG turn that
    denied a fact was folded into memory as an established absence. The gate
    must short-circuit BEFORE any LLM call or upsert on a denial turn.
    """
    import src.pipeline.memory_updater as mu

    called = {"llm": False}

    async def _fake_llm(*a, **k):
        called["llm"] = True
        return "SHOULD NOT BE CALLED"

    monkeypatch.setattr(mu, "call_llm", _fake_llm)
    gw = _MemoryGatewaySpy(existing="Case Alpha-7749 is codenamed 'Operation Nightingale'.")

    result = await update_project_memory(
        "proj-1",
        [{"role": "user", "content": "what codename?"},
         {"role": "assistant", "content": "The provided documents do not contain information about that."}],
        gw,
    )

    assert called["llm"] is False, "denial turn must not reach the summarizer LLM"
    assert gw.upserted is None, "denial turn must not write to project memory"
    assert result == "Case Alpha-7749 is codenamed 'Operation Nightingale'.", "existing memory must be returned unchanged"


@pytest.mark.asyncio
async def test_fact_turn_does_update_memory(monkeypatch):
    """A genuine fact turn must proceed to summarize and upsert."""
    import src.pipeline.memory_updater as mu

    async def _fake_llm(*a, **k):
        return "Case Alpha-7749 is codenamed 'Operation Nightingale'; code is FALCON-NINE-ZULU."

    monkeypatch.setattr(mu, "call_llm", _fake_llm)
    gw = _MemoryGatewaySpy(existing="")

    await update_project_memory(
        "proj-1",
        [{"role": "user", "content": "what's the code?"},
         {"role": "assistant", "content": "The code is FALCON-NINE-ZULU [Document 4]."}],
        gw,
    )

    assert gw.upserted is not None, "a genuine fact turn must write to project memory"
    assert "FALCON-NINE-ZULU" in gw.upserted
