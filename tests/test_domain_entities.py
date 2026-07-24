"""Tests for src/extraction/domain_entities.py (Phase 4.6)."""

import json

import pytest

import src.extraction.domain_entities as domain_entities


@pytest.mark.asyncio
async def test_vehicle_extraction_with_plate_cross_validation(monkeypatch):
    # LLM under-reports the plate (or gets it wrong) — the regex extractor
    # in the passage itself is the source of truth.
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return json.dumps([
            {"text": "سوزوکی پک اپ، ICT-LE-309", "type": "vehicle",
             "attributes": {"plate": "ICT-LE-999", "description": "burglary transport"},
             "confidence": 0.9},
        ])

    monkeypatch.setattr(domain_entities, "call_llm", fake_call_llm)

    text = "سوزوکی پک اپ، ICT-LE-309 مقام سے برآمد ہوئی۔"
    result = await domain_entities.extract_domain_entities(text)

    assert len(result) == 1
    assert result[0]["type"] == "vehicle"
    # Regex-verified plate wins over the LLM's own (wrong) transcription.
    assert result[0]["attributes"]["plate"] == "ICT-LE-309"
    assert result[0]["char_span"] is not None


@pytest.mark.asyncio
async def test_weapon_extraction_located_by_substring(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return json.dumps([
            {"text": "بور 30 پستول", "type": "weapon",
             "attributes": {"description": "فرانسیسی ساختہ بور 30 پستول"},
             "confidence": 0.85},
        ])

    monkeypatch.setattr(domain_entities, "call_llm", fake_call_llm)

    text = "ملزم کے قبضے سے ایک فرانسیسی ساختہ بور 30 پستول برآمد کیا گیا۔"
    result = await domain_entities.extract_domain_entities(text)

    assert result[0]["type"] == "weapon"
    start, end = result[0]["char_span"]
    assert text[start:end] == "بور 30 پستول"


@pytest.mark.asyncio
async def test_organization_and_person_alias(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return json.dumps([
            {"text": "سائبر فراڈ گروہ", "type": "organization",
             "attributes": {"description": "cyber fraud ring"}, "confidence": 0.8},
            {"text": "استاد", "type": "person",
             "attributes": {"role": "ringleader alias"}, "confidence": 0.7},
        ])

    monkeypatch.setattr(domain_entities, "call_llm", fake_call_llm)

    text = "یہ شخص سائبر فراڈ گروہ کا سرغنہ ہے، جسے 'استاد' کہا جاتا ہے۔"
    result = await domain_entities.extract_domain_entities(text)

    types = {e["type"] for e in result}
    assert types == {"organization", "person"}
    person = next(e for e in result if e["type"] == "person")
    assert person["attributes"]["role"] == "ringleader alias"


@pytest.mark.asyncio
async def test_unlocatable_mention_kept_with_none_span(monkeypatch):
    # The LLM paraphrases instead of quoting verbatim — can't be found by
    # substring search. Must still be returned, not silently dropped.
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return json.dumps([
            {"text": "a completely different string not in the source",
             "type": "weapon", "attributes": {}, "confidence": 0.6},
        ])

    monkeypatch.setattr(domain_entities, "call_llm", fake_call_llm)

    result = await domain_entities.extract_domain_entities("پستول برآمد ہوا۔")
    assert len(result) == 1
    assert result[0]["char_span"] is None


@pytest.mark.asyncio
async def test_unknown_type_is_filtered_out(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return json.dumps([
            {"text": "X", "type": "not_a_real_type", "attributes": {}, "confidence": 0.5},
        ])

    monkeypatch.setattr(domain_entities, "call_llm", fake_call_llm)

    result = await domain_entities.extract_domain_entities("some text")
    assert result == []


@pytest.mark.asyncio
async def test_non_list_json_returns_empty(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return '{"not": "a list"}'

    monkeypatch.setattr(domain_entities, "call_llm", fake_call_llm)

    result = await domain_entities.extract_domain_entities("some text")
    assert result == []


@pytest.mark.asyncio
async def test_llm_exception_returns_empty_not_raise(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        raise RuntimeError("model server unreachable")

    monkeypatch.setattr(domain_entities, "call_llm", fake_call_llm)

    result = await domain_entities.extract_domain_entities("some text")
    assert result == []


@pytest.mark.asyncio
async def test_empty_text_short_circuits_without_llm_call(monkeypatch):
    calls = []

    async def fake_call_llm(system_prompt, user_message, **kwargs):
        calls.append(1)
        return "[]"

    monkeypatch.setattr(domain_entities, "call_llm", fake_call_llm)

    result = await domain_entities.extract_domain_entities("   ")
    assert result == []
    assert calls == []
