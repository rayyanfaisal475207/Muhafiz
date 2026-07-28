"""
src.pipeline.json_extract — the single shared JSON-extraction implementation
used by file_structurer.py, evaluator.py, verifier.py, router.py,
query_expander.py, and sql_extractor.py (Phase 6, Module 6.1).

Guards:
  * Basic extraction (bare JSON, markdown fences, reasoning-trace preambles,
    trailing prose) — moved here from test_file_generation.py, which
    originally targeted file_structurer.py's now-removed local copy.
  * The specific historical failure mode of each of the five call sites this
    module replaces, so a regression in the shared implementation is caught
    regardless of which call site would have hit it first.
"""
import pytest

from src.pipeline.json_extract import extract_json


# ── Basic extraction (moved from test_file_generation.py) ────────────────────

def test_extracts_bare_json():
    assert extract_json('{"title": "Rate Card"}')["title"] == "Rate Card"


def test_extracts_json_from_markdown_fence():
    raw = 'Sure, here you go:\n```json\n{"title": "Rate Card"}\n```\nHope that helps!'
    assert extract_json(raw)["title"] == "Rate Card"


def test_ignores_reasoning_tokens_before_json():
    """Reasoning models emit <think> blocks; braces inside them broke parsing."""
    raw = '<think>The user wants {a table} of rates</think>\n{"title": "Rate Card"}'
    assert extract_json(raw)["title"] == "Rate Card"


def test_extracts_first_balanced_object_despite_trailing_prose():
    raw = 'Note {not json}. Result: {"title": "X", "nested": {"a": 1}} — done }'
    result = extract_json(raw)
    assert result["title"] == "X"
    assert result["nested"] == {"a": 1}


@pytest.mark.parametrize("raw", ["", "no json here at all", "{unclosed: "])
def test_unparseable_output_raises_rather_than_returning_garbage(raw):
    """A failure must be loud — silent failure is what hid this bug for versions."""
    with pytest.raises(ValueError):
        extract_json(raw)


# ── Call-site-specific historical failure modes ───────────────────────────────

def test_evaluator_style_object_with_reasoning_preamble():
    """evaluator.py's old regex worked for this shape, but confirm parity."""
    raw = '<think>checking relevance...</think>\n{"relevant": true, "reason": "covers it"}'
    result = extract_json(raw)
    assert result == {"relevant": True, "reason": "covers it"}


def test_verifier_style_object_with_prose_before_and_after():
    """verifier.py's documented live failure: prose wrapping a grounded=true object."""
    raw = 'Based on the citations, {"grounded": true, "reason": "well supported"} is my answer.'
    result = extract_json(raw)
    assert result == {"grounded": True, "reason": "well supported"}


def test_router_style_bare_object_no_fence():
    raw = '{"route": "GRAPH", "confidence": "high"}'
    result = extract_json(raw)
    assert result["route"] == "GRAPH"


def test_query_expander_style_array_with_thinking_trace_preamble():
    """
    query_expander.py's old stripping only handled markdown fences, not a
    <think> preamble before a JSON *array* — the shared function must
    support list-returning responses, not just objects.
    """
    raw = '<think>generating paraphrases...</think>\n["variant one", "variant two"]'
    result = extract_json(raw)
    assert result == ["variant one", "variant two"]


def test_query_expander_style_fenced_array():
    raw = '```json\n["alt phrasing a", "alt phrasing b"]\n```'
    result = extract_json(raw)
    assert result == ["alt phrasing a", "alt phrasing b"]


def test_sql_extractor_style_fenced_object_without_trailing_newline():
    """sql_extractor.py's old code hardcoded a ```json prefix/suffix strip."""
    raw = '```json\n{"case_id": "C-100", "date_from": "2024-01-01"}```'
    result = extract_json(raw)
    assert result == {"case_id": "C-100", "date_from": "2024-01-01"}
