"""
Pipeline components — rewriter, router, evaluator.

These all consume raw LLM text, so the tests focus on the failure modes real
models produce: chatty preambles, markdown fences, malformed JSON, empty
output. A component that mishandles those degrades answer quality silently.
"""
import pytest

from src.pipeline.query_rewriter import (
    _echoes_prior_assistant_turn,
    _sanitize_rewrite,
    rewrite_query,
)
from src.pipeline.router import route_query
from src.pipeline.evaluator import evaluate_relevance, _format_chunks_for_prompt


# ── Query rewriter ────────────────────────────────────────────────────────────
#
# _sanitize_rewrite returns None (not a fallback string) when the output is
# unusable — callers decide their own fallback (the original message for
# rewrite_query, the previous query for rewrite_for_retry's multi-attempt
# loop), which a single baked-in "orig" fallback couldn't express.

def test_rewrite_passes_through_a_clean_query():
    assert _sanitize_rewrite("What PPC section covers mobile theft?") == "What PPC section covers mobile theft?"


@pytest.mark.parametrize("raw", [
    "Output: What PPC section covers mobile theft?",
    "Rewritten query: What PPC section covers mobile theft?",
    '"What PPC section covers mobile theft?"',
    "  What PPC section covers mobile theft?  ",
])
def test_rewrite_strips_preambles_and_quotes(raw):
    """Models add labels and quotes despite being told not to."""
    assert _sanitize_rewrite(raw) == "What PPC section covers mobile theft?"


def test_rewrite_takes_only_the_first_line():
    raw = "What PPC section covers mobile theft?\n\nExplanation: this resolves the pronoun."
    assert _sanitize_rewrite(raw) == "What PPC section covers mobile theft?"


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_rewrite_returns_none(raw):
    assert _sanitize_rewrite(raw) is None


def test_rewrite_that_answers_the_question_returns_none():
    """
    A "rewrite" the length of an essay means the model answered instead of
    rewriting. Searching the vector DB with an essay wrecks retrieval.
    """
    essay = "The PPC section for mobile phone theft is 379 PPC. " * 20
    assert _sanitize_rewrite(essay) is None


@pytest.mark.parametrize("raw", [
    "Improved search query:",
    "**Improved search query:**",
])
def test_rewrite_echoing_the_retry_prompt_label_returns_none(raw):
    """
    Regression: rewrite_for_retry()'s own trailing prompt line used to read
    "Write an improved search query:", and the model sometimes echoed that
    exact label back verbatim instead of writing a real query — which used
    to pass sanitization as a very short "valid" single line and get used
    as the actual search query outright.
    """
    assert _sanitize_rewrite(raw) is None


def test_rewrite_that_discusses_the_question_returns_none():
    """
    Regression: distinct from the label-echo case above — the model treats
    the rewrite task as a real question to discuss ("The question 'X' is
    unclear because...") instead of producing a new query. Short enough to
    survive the length check, so needs its own detection.
    """
    commentary = (
        'The question "What PPC section covers mobile phone theft?" is '
        "unclear because PPC could refer to different things."
    )
    assert _sanitize_rewrite(commentary) is None


@pytest.mark.parametrize("raw", [
    "I currently do not have access to specific information regarding which "
    "reports are handled by Nilore Station. For the most accurate and "
    "up-to-date information, I recommend contacting the local police "
    "department or referring to official police records.",
    "I don't have access to that information. I recommend contacting the "
    "relevant department directly.",
    "Based on the information available in the documents, there is no "
    "specific mention of items that have been recovered in the "
    "investigation. I recommend referring to official case records.",
])
def test_rewrite_that_refuses_instead_of_rewriting_returns_none(raw):
    """
    Regression (2026-08-03, live query_ids 887/889): distinct from the
    commentary case above — the model treats the message as a real question
    and answers it with a first-person refusal ("I currently do not have
    access to...") instead of rewriting it. This is short enough (under 400
    chars) and phrased differently enough from "the question is unclear"
    that it survived every prior check and got used verbatim as the actual
    search query handed to embedding + BM25 — and, sitting in conversation
    history, contaminated the very next turn's rewrite in the same session
    into producing a second refusal.
    """
    assert _sanitize_rewrite(raw) is None


@pytest.mark.parametrize("raw", [
    # Live-observed, 2026-08-03: after the phrase-list version of the fix
    # above shipped, re-running the SAME two-turn scenario against the
    # restored model server produced two MORE first-person refusals that
    # matched none of that phrase list — confirming a literal list is as
    # incomplete here as verifier.py's own docstring warns for refusal
    # phrases generally. These wordings are why the detection was
    # rewritten as structural regex patterns instead of literal phrases.
    "I don't have specific information about items recovered in "
    "investigations handled by Nilore Station or any other specific "
    "station, as this requires access to case records.",
    "To provide a more accurate and helpful response, it's important to "
    "clarify that without specific details about the investigation in "
    "question, I cannot determine what items were recovered.",
])
def test_rewrite_that_refuses_with_a_new_unlisted_phrasing_returns_none(raw):
    """
    Regression guard for the whack-a-mole failure mode itself: these exact
    two sentences were NOT in the literal phrase list and would have
    slipped through it — they must still be caught by the structural
    (regex) patterns that replaced/extended it.
    """
    assert _sanitize_rewrite(raw) is None


@pytest.mark.parametrize("raw", [
    "What PPC section applies to mobile phone theft?",
    "Did the witness say I saw the accused near the scene?",
    "What did the witness who said I saw the man leave in a hurry report to police?",
])
def test_rewrite_refusal_patterns_do_not_false_positive_on_real_queries(raw):
    """
    Regression guard: the refusal patterns are anchored on first-person
    negated-possession/meta-commentary structure, not the bare word "I" —
    a legitimate rewritten query that happens to quote the user's or a
    witness's own first-person phrasing must not be rejected.
    """
    assert _sanitize_rewrite(raw) == raw


def test_rewrite_that_addresses_the_user_instead_of_rewriting_returns_none():
    """
    Regression (2026-08-03, live re-test): a retry-rewrite of a genuinely
    vague query addressed the user directly instead of producing an
    improved query — "To improve the search query and make it more
    specific and effective, you can provide more context such as:".
    """
    raw = (
        "To improve the search query and make it more specific and "
        "effective, you can provide more context such as: the station name, "
        "the FIR number, or the date of the incident."
    )
    assert _sanitize_rewrite(raw) is None


def test_rewrite_that_answers_using_a_document_citation_returns_none():
    """
    Regression (2026-08-03, live re-test): given enough conversation
    history, the model answered a follow-up directly instead of rewriting
    it — "Based on the information provided in **Document 1 (Recovery
    Memo)** for **FIR-2026-THEFT-012**, the specific items that have been
    recovered..." The rewriter runs BEFORE retrieval and has never seen a
    "Document N" citation, so any such reference in its output can only be
    the model echoing history while answering — never a legitimate rewrite.
    """
    raw = (
        "Based on the information provided in **Document 1 (Recovery Memo)** "
        "for **FIR-2026-THEFT-012**, the specific items that have been "
        "recovered are stolen mobile phones."
    )
    assert _sanitize_rewrite(raw) is None


def test_rewrite_that_echoes_the_apps_own_abstention_text_returns_none():
    """
    Regression (2026-08-03, second live re-test): the rewriter verbatim-
    echoed the app's own canned abstention text (`orchestrator.py`'s
    `_SAFE_RESPONSE`) from the previous turn's assistant message — "I
    couldn't find sufficient information in the knowledge base to
    accurately answer your question. You may want to try rephrasing your
    question or ensure the relevant documents have been ingested..." — and
    used that as the search query for a completely unrelated follow-up
    question. This is an exact, permanently-recurring known string (not a
    novel wording), so it's matched directly in addition to the general
    history-echo check below.
    """
    raw = (
        "I couldn't find sufficient information in the knowledge base to "
        "accurately answer your question. You may want to try rephrasing "
        "your question or ensure the relevant documents have been ingested "
        "into the system."
    )
    assert _sanitize_rewrite(raw) is None


def test_echoes_prior_assistant_turn_detects_a_verbatim_copy():
    history = [
        {"role": "user", "content": "What happened in FIR-2026-HAR-001?"},
        {"role": "assistant", "content": (
            "I couldn't find sufficient information in the knowledge base "
            "to accurately answer your question. You may want to try "
            "rephrasing your question or ensure the relevant documents "
            "have been ingested into the system."
        )},
    ]
    rewritten = (
        "I couldn't find sufficient information in the knowledge base to "
        "accurately answer your question."
    )
    assert _echoes_prior_assistant_turn(rewritten, history) is True


def test_echoes_prior_assistant_turn_ignores_a_genuinely_new_rewrite():
    history = [
        {"role": "user", "content": "What happened in FIR-2026-HAR-001?"},
        {"role": "assistant", "content": (
            "The complainant reported harassment at Kohsar police station "
            "on 2026-02-10, filed under Section 509 PPC."
        )},
    ]
    rewritten = "What was the harassment complaint filed under Section 509 PPC at Kohsar?"
    assert _echoes_prior_assistant_turn(rewritten, history) is False


def test_echoes_prior_assistant_turn_ignores_short_coincidental_overlap():
    """A short shared fragment (a station name) is not suspicious on its own."""
    history = [
        {"role": "user", "content": "Which station handled it?"},
        {"role": "assistant", "content": "Nilore police station handled the case."},
    ]
    rewritten = "What reports are handled by Nilore police station?"
    assert _echoes_prior_assistant_turn(rewritten, history) is False


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
    async def _fake_llm(*, system_prompt, user_message, **kwargs):
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


# ── Module 19b: compound-question relaxation (order-independent) ──────────────
#
# Module 5 added the compound-question rule ("answer the primary part") but
# it assumed the LEGAL half is always "primary" and a closing single-topic
# strictness line could still override it — so a data-clause-first question
# (KB2/KB3's actual shape) was misjudged. These tests pin to KB2/KB3/KB4's
# literal gold text (Gold_QA_Dataset_Final32.json) and mock the LLM, so they
# assert the evaluate_relevance()/prompt contract, not live model behavior —
# the live verification (backend log capture) is what actually proves the
# prompt change works.

KB2_QUESTION = (
    "Why doesn't our system keep a record of what a witness or an accused "
    "person actually said in a police interview — is that a data gap?"
)
KB3_QUESTION = (
    "Does the law expect the officer who first registers a case to be the "
    "same one who investigates it, or are those meant to be separate roles "
    "— and does that match what actually happens in our data?"
)
KB4_QUESTION = (
    "جب پولیس کسی مقدمے سے متعلق اشیاء اپنی تحویل میں لیتی ہے، تو کیا اِس "
    "بارے میں کوئی باقاعدہ معیار موجود ہے کہ اُنہیں کیسے درج اور بالآخر "
    "کیسے تلف کیا جائے — اور کیا ہمارا پراپرٹی ریکارڈ اُس پر عمل کرتا ہے؟"
)


def test_evaluator_prompt_states_compound_detection_is_order_independent():
    """
    The prompt must no longer frame compound handling around "the primary
    part" (which invited the KB2 primary/secondary inversion) — it must say
    explicitly that leading with the data clause doesn't change the verdict.
    """
    from src.pipeline.evaluator import _SYSTEM_PROMPT

    assert "regardless of which" in _SYSTEM_PROMPT
    assert "norm clause" in _SYSTEM_PROMPT.lower()
    assert "our-data clause" in _SYSTEM_PROMPT.lower() or "our data clause" in _SYSTEM_PROMPT.lower()


def test_evaluator_prompt_compound_rule_is_not_overridden_by_single_topic_default():
    """
    Hypothesis B: the prompt's closing "evaluate strictly for a SINGLE-topic
    question" line was the last instruction the model read, and for any
    question not confidently classified as compound, it won. The fix must
    make the compound check happen first and say explicitly that the
    single-topic default does not apply once a question is compound.
    """
    from src.pipeline.evaluator import _SYSTEM_PROMPT

    assert "does NOT apply to it" in _SYSTEM_PROMPT or "cannot override" in _SYSTEM_PROMPT


async def test_evaluator_accepts_data_clause_first_compound_question(monkeypatch):
    """
    KB2's literal shape: the sentence leads with "why doesn't our system…"
    (the our-data clause) before ever mentioning law. A compliant evaluator
    call still returns relevant=True when the documents answer the norm
    clause — this pins the mocked LLM's own reasoning to KB2's exact text so
    a regression here fails this test rather than only being caught live.
    """
    async def _fake_llm(*, system_prompt, user_message, **kwargs):
        assert KB2_QUESTION in user_message
        return (
            '{"relevant": true, "reason": "Documents describe the legal '
            'restrictions on recording and using police-interview statements '
            '— compound question, norm clause answered even though it is not '
            'the sentence\'s lead clause."}'
        )

    monkeypatch.setattr("src.pipeline.evaluator.call_llm", _fake_llm)

    result = await evaluate_relevance(
        KB2_QUESTION,
        KB2_QUESTION,
        [{"text": "Qanun-e-Shahadat Article 38/39...", "metadata": {"source": "qanun-e-shahadat.pdf"}}],
    )

    assert result["relevant"] is True


async def test_evaluator_accepts_kb3_role_separation_question(monkeypatch):
    async def _fake_llm(*, system_prompt, user_message, **kwargs):
        assert KB3_QUESTION in user_message
        return '{"relevant": true, "reason": "Police Order Article 18 establishes a separate investigation wing — norm clause answered."}'

    monkeypatch.setattr("src.pipeline.evaluator.call_llm", _fake_llm)

    result = await evaluate_relevance(
        KB3_QUESTION,
        KB3_QUESTION,
        [{"text": "Article 18. Posting of head of investigation...", "metadata": {"source": "PoliceOrder2002.pdf"}}],
    )

    assert result["relevant"] is True


async def test_evaluator_accepts_kb4_urdu_compound_question(monkeypatch):
    """Urdu-phrased compound question (norm clause first here) — order-independence must hold across scripts too."""
    async def _fake_llm(*, system_prompt, user_message, **kwargs):
        assert KB4_QUESTION in user_message
        return '{"relevant": true, "reason": "Punjab Police Rules describe property register destruction and handover procedure — norm clause answered."}'

    monkeypatch.setattr("src.pipeline.evaluator.call_llm", _fake_llm)

    result = await evaluate_relevance(
        KB4_QUESTION,
        KB4_QUESTION,
        [{"text": "This register may be destroyed three years after being completed...", "metadata": {"source": "Punjab-Police-Rules-III.pdf"}}],
    )

    assert result["relevant"] is True


async def test_evaluator_still_rejects_genuinely_off_topic_documents(monkeypatch):
    """
    Regression guard (required by the module brief): the prompt's own
    "Examples of correct false decisions" list must still produce false —
    a fix that makes the evaluator return true for everything is a
    regression, not a fix.
    """
    async def _fake_llm(*args, **kwargs):
        return (
            '{"relevant": false, "reason": "Retrieved chunks are about the '
            'foreigner registration procedure, not tenant registration as '
            'the user asked"}'
        )

    monkeypatch.setattr("src.pipeline.evaluator.call_llm", _fake_llm)

    result = await evaluate_relevance(
        "What documents do I need for tenant registration?",
        "What documents do I need for tenant registration?",
        [{"text": "Foreigner registration requires...", "metadata": {"source": "foreigner_reg.pdf"}}],
    )

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
