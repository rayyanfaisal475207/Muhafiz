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


# ── Retry semantics: repair vs regenerate ────────────────────────────────────
#
# A retry exists to fix a malformed reply. For a caller whose payload encodes
# an INTERPRETATION, a retry that discards the previous reply and asks again
# is a semantic regeneration, and the second answer is free to differ.
# Measured on the aggregate planner across 26 runs: every single-attempt
# generation produced the correct two-hop traversal, and every retried one
# produced a simpler one-hop traversal that meant something else.
#
# `preserve_interpretation=True` quotes the previous reply back and asks for
# a repair. These guard both modes, because the default is what 60+ other
# call sites rely on.

import json as _json

from src.pipeline.json_extract import (
    _json_only_correction,
    _repair_correction,
    call_llm_json,
)


def _fake_llm(replies):
    """A call_llm stand-in that returns `replies` in order and records prompts."""
    seen: list[str] = []

    async def _call(system_prompt, user_message, **kwargs):
        seen.append(user_message)
        return replies[min(len(seen) - 1, len(replies) - 1)]

    return _call, seen


class TestRepairCorrectionText:
    def test_default_mode_does_not_echo_the_previous_reply(self):
        previous = '{"measure": "count_distinct", "entity": "Person"}'
        assert previous not in _json_only_correction("measure, entity")

    def test_repair_mode_echoes_the_previous_reply(self):
        previous = '{"measure": "count_distinct", "entity": "Person"}'
        assert previous in _repair_correction(previous, "measure, entity")

    def test_repair_mode_forbids_simplifying(self):
        text = _repair_correction("{}", None).lower()
        assert "do not simplify" in text
        assert "do not drop a constraint" in text

    def test_repair_mode_bounds_what_it_echoes(self):
        """A runaway reply must not push the retry past a request cap."""
        huge = "x" * 50_000
        assert len(_repair_correction(huge, None)) < 6_000


class TestRetryPreservesInterpretation:
    """The behaviour the flag exists for, exercised end to end."""

    COMPLEX = _json.dumps({
        "measure": "count_distinct",
        "entity": "Person",
        "traversals": [
            {"rel": "INVOLVED_IN", "target": "Incident"},
            {"rel": "BELONGS_TO_CASE", "target": "Case"},
        ],
    })

    @staticmethod
    def _needs_grain(payload):
        return isinstance(payload, dict) and "grain" in payload

    @pytest.mark.asyncio
    async def test_retry_prompt_carries_the_previous_interpretation(self):
        """The second prompt must contain what the model already decided."""
        call, seen = _fake_llm([self.COMPLEX, _json.dumps(
            {**_json.loads(self.COMPLEX), "grain": "ENTITY"}
        )])
        result, _ = await call_llm_json(
            "sys", "QUESTION: how many?",
            max_tokens=100, validate=self._needs_grain,
            _call_llm=call, preserve_interpretation=True,
        )
        assert len(seen) == 2
        assert "INVOLVED_IN" in seen[1]
        assert "BELONGS_TO_CASE" in seen[1]
        # And the interpretation survived into the accepted result.
        assert len(result["traversals"]) == 2

    @pytest.mark.asyncio
    async def test_default_mode_retry_does_not_carry_it(self):
        """Unchanged behaviour for every other caller."""
        call, seen = _fake_llm([self.COMPLEX, _json.dumps(
            {**_json.loads(self.COMPLEX), "grain": "ENTITY"}
        )])
        await call_llm_json(
            "sys", "QUESTION: how many?",
            max_tokens=100, validate=self._needs_grain,
            _call_llm=call,
        )
        assert len(seen) == 2
        assert "INVOLVED_IN" not in seen[1]

    @pytest.mark.asyncio
    async def test_a_first_attempt_that_validates_never_retries(self):
        """Repair mode must not add a call when nothing was wrong."""
        good = _json.dumps({**_json.loads(self.COMPLEX), "grain": "ENTITY"})
        call, seen = _fake_llm([good])
        result, _ = await call_llm_json(
            "sys", "q", max_tokens=100, validate=self._needs_grain,
            _call_llm=call, preserve_interpretation=True,
        )
        assert len(seen) == 1
        assert result["grain"] == "ENTITY"
