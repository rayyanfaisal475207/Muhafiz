"""
Pipeline components — rewriter, router, evaluator.

These all consume raw LLM text, so the tests focus on the failure modes real
models produce: chatty preambles, markdown fences, malformed JSON, empty
output. A component that mishandles those degrades answer quality silently.
"""
import pytest

from src.pipeline.query_rewriter import _sanitize_rewrite, rewrite_query
from src.pipeline.router import route_query
from src.pipeline.evaluator import evaluate_relevance, _format_chunks_for_prompt


# ── Query rewriter ────────────────────────────────────────────────────────────

def test_rewrite_passes_through_a_clean_query():
    assert _sanitize_rewrite("What PPC section covers mobile theft?", "orig") == "What PPC section covers mobile theft?"


@pytest.mark.parametrize("raw", [
    "Output: What PPC section covers mobile theft?",
    "Rewritten query: What PPC section covers mobile theft?",
    '"What PPC section covers mobile theft?"',
    "  What PPC section covers mobile theft?  ",
])
def test_rewrite_strips_preambles_and_quotes(raw):
    """Models add labels and quotes despite being told not to."""
    assert _sanitize_rewrite(raw, "orig") == "What PPC section covers mobile theft?"


def test_rewrite_takes_only_the_first_line():
    raw = "What PPC section covers mobile theft?\n\nExplanation: this resolves the pronoun."
    assert _sanitize_rewrite(raw, "orig") == "What PPC section covers mobile theft?"


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_rewrite_falls_back_to_the_original(raw):
    assert _sanitize_rewrite(raw, "original message") == "original message"


def test_rewrite_that_answers_the_question_falls_back():
    """
    A "rewrite" the length of an essay means the model answered instead of
    rewriting. Searching the vector DB with an essay wrecks retrieval.
    """
    essay = "The PPC section for mobile phone theft is 379 PPC. " * 20
    assert _sanitize_rewrite(essay, "orig") == "orig"


async def test_rewrite_skips_the_llm_when_there_is_no_history(monkeypatch):
    """First message in a session is already standalone — don't pay for a call."""
    called = False

    async def _boom(*args, **kwargs):
        nonlocal called
        called = True
        return "should not be called"

    monkeypatch.setattr("src.pipeline.query_rewriter.call_llm", _boom)

    result = await rewrite_query("What PPC section covers mobile theft?", [])

    assert result == "What PPC section covers mobile theft?"
    assert not called


async def test_rewrite_resolves_followups_using_history(monkeypatch):
    async def _fake_llm(system_prompt, user_message, **kwargs):
        assert "mobile theft" in user_message, "history was not passed to the rewriter"
        return "What PPC section covers mobile theft committed at night?"

    monkeypatch.setattr("src.pipeline.query_rewriter.call_llm", _fake_llm)

    history = [
        {"role": "user", "content": "What PPC section covers mobile theft?"},
        {"role": "assistant", "content": "379 PPC."},
    ]
    result = await rewrite_query("what about if it happens at night?", history)

    assert "night" in result


# ── Router ────────────────────────────────────────────────────────────────────

async def test_router_parses_a_clean_decision(monkeypatch):
    async def _fake_llm(*args, **kwargs):
        return '{"route": "RAG", "output_format": "chat", "confidence": "high", "reason": "procedure question"}'

    monkeypatch.setattr("src.pipeline.router.call_llm", _fake_llm)

    result = await route_query("What documents are required for a certified copy of an FIR?")

    assert result["route"] == "RAG"
    assert result["output_format"] == "chat"


async def test_router_extracts_json_wrapped_in_prose(monkeypatch):
    async def _fake_llm(*args, **kwargs):
        return 'Here is my decision:\n{"route": "SQL", "output_format": "file_xlsx"}\nDone.'

    monkeypatch.setattr("src.pipeline.router.call_llm", _fake_llm)

    result = await route_query("Export the penal code reference table as excel")

    assert result["route"] == "SQL"
    assert result["output_format"] == "file_xlsx"


async def test_router_defaults_to_rag_on_unparseable_output(monkeypatch):
    """RAG is the safe default: retrieving is better than hallucinating."""
    async def _fake_llm(*args, **kwargs):
        return "I think this is a police procedure question, probably RAG?"

    monkeypatch.setattr("src.pipeline.router.call_llm", _fake_llm)

    result = await route_query("Explain the FIR copy procedure")

    assert result["route"] == "RAG"
    assert result["confidence"] == "low"


@pytest.mark.parametrize("bad_route", ["MAGIC", "sql; DROP TABLE", ""])
async def test_router_rejects_unknown_routes(monkeypatch, bad_route):
    async def _fake_llm(*args, **kwargs):
        return f'{{"route": "{bad_route}", "output_format": "chat"}}'

    monkeypatch.setattr("src.pipeline.router.call_llm", _fake_llm)

    result = await route_query("anything")

    assert result["route"] == "RAG"


async def test_router_rejects_unknown_output_formats(monkeypatch):
    async def _fake_llm(*args, **kwargs):
        return '{"route": "RAG", "output_format": "file_exe"}'

    monkeypatch.setattr("src.pipeline.router.call_llm", _fake_llm)

    result = await route_query("anything")

    assert result["output_format"] == "chat"


# ── Evaluator ─────────────────────────────────────────────────────────────────

async def test_evaluator_returns_not_relevant_for_empty_retrieval(monkeypatch):
    result = await evaluate_relevance("q", "q", [])

    assert result["relevant"] is False
    assert result["reason"]


async def test_evaluator_parses_a_verdict(monkeypatch):
    async def _fake_llm(*args, **kwargs):
        return '{"relevant": true, "reason": "379 PPC covers mobile theft"}'

    monkeypatch.setattr("src.pipeline.evaluator.call_llm", _fake_llm)

    result = await evaluate_relevance("q", "q", [{"text": "379 PPC...", "metadata": {}}])

    assert result["relevant"] is True


async def test_evaluator_defaults_to_not_relevant_on_bad_json(monkeypatch):
    """Failing closed triggers the retry loop rather than answering from junk."""
    async def _fake_llm(*args, **kwargs):
        return "the documents seem fine to me"

    monkeypatch.setattr("src.pipeline.evaluator.call_llm", _fake_llm)

    result = await evaluate_relevance("q", "q", [{"text": "x", "metadata": {}}])

    assert result["relevant"] is False


def test_evaluator_prompt_shows_the_real_source_filename():
    """
    Regression: chunks carry the source under metadata.source, but the
    formatter only read a top-level source_file key — so the evaluator saw
    "Source: unknown" for every chunk and judged them without provenance.
    """
    chunks = [{"text": "Theft of movable property...", "metadata": {"source": "offense_sections.csv"}}]

    formatted = _format_chunks_for_prompt(chunks)

    assert "offense_sections.csv" in formatted
    assert "unknown" not in formatted
