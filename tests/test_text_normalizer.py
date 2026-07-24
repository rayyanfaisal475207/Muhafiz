"""
Text normalizer — whitespace cleanup applied before chunking (Phase 4).

A small, standalone module by design: the fix for the chunker's
advance-floor bug (see test_retrieval_and_memory.py) makes the chunker
robust against pathological input regardless of source; this makes the
actual input (Docling's markdown table column-padding) less pathological
in the first place. Tested independently of both the chunker and the
Docling loader.
"""
from src.ingestion.text_normalizer import normalize_whitespace


def test_collapses_long_space_runs_to_a_single_space():
    text = "Police Station:" + (" " * 250) + "Bhara Kahu"

    result = normalize_whitespace(text)

    assert "  " not in result
    assert result == "Police Station: Bhara Kahu"


def test_leaves_single_and_double_spaces_untouched():
    assert normalize_whitespace("a b  c") == "a b  c"


def test_collapses_tabs_too():
    assert normalize_whitespace("a\t\t\tb") == "a b"


def test_preserves_paragraph_breaks():
    """Newlines (including \\n\\n) must survive — the chunker splits on them."""
    text = "Header.\n\nField:" + (" " * 40) + "value"

    result = normalize_whitespace(text)

    assert result == "Header.\n\nField: value"


def test_empty_text_is_a_noop():
    assert normalize_whitespace("") == ""


def test_text_with_no_long_runs_is_unchanged():
    text = "A perfectly normal sentence with regular spacing."
    assert normalize_whitespace(text) == text
