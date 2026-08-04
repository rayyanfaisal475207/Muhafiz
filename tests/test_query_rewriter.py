"""Unit tests for query_rewriter._sanitize_rewrite()'s structural guards.

Focuses on the guards confirmed live this session (see docs/AUDIT_FINDINGS_
2026-08-04.md D-1): the rewriter can narrate ABOUT the rewriting task
instead of producing a standalone query, and that failure mode had no
dedicated guard despite every other known failure mode having one.
"""
from src.pipeline.query_rewriter import _sanitize_rewrite


def test_valid_single_sentence_query_passes_through():
    text = "What section of the PPC covers mobile phone theft in CASE-009?"
    assert _sanitize_rewrite(text) == text


def test_valid_query_with_comma_is_not_treated_as_multi_sentence():
    text = "Who is the complainant in FIR-2026-THEFT-001, and what items were stolen?"
    assert _sanitize_rewrite(text) == text


def test_rejects_live_observed_narration_about_the_original_question():
    """
    Live-confirmed (2026-08-04): after the evaluator rejected a search, the
    rewriter's "Retry query" was verbatim explanatory prose about the
    question rather than a rewritten query, and this was then used as the
    literal next retrieval query.
    """
    text = (
        'The original question — "How many recurring vehicles have '
        'appeared across multiple cases?" — is similar in intent to '
        'the previous search query, but it may be interpreted differently '
        'depending on how "recurring vehicles" are defined.'
    )
    assert _sanitize_rewrite(text) is None


def test_rejects_live_observed_meta_planning_language():
    """
    Live-confirmed (2026-08-04), a second independent occurrence: the
    rewriter addressed the task itself ("we need to follow a structured
    approach") instead of producing a query.
    """
    text = (
        "To address your query effectively, we need to follow a structured "
        "approach to identify recurring vehicles."
    )
    assert _sanitize_rewrite(text) is None


def test_rejects_multi_sentence_output_generically():
    text = "First we should look at vehicle records. Then cross-reference case IDs."
    assert _sanitize_rewrite(text) is None


def test_still_rejects_prior_known_failure_modes():
    assert _sanitize_rewrite("") is None
    assert _sanitize_rewrite("   ") is None
    assert _sanitize_rewrite("The question 'X' is unclear because it lacks context") is None
    assert _sanitize_rewrite("I don't have specific information about items recovered") is None
    assert _sanitize_rewrite(
        "Based on the information provided in **Document 1**, the items recovered were..."
    ) is None


def test_strips_label_style_preamble():
    assert _sanitize_rewrite("Improved search query: What is the FIR number?") == "What is the FIR number?"
