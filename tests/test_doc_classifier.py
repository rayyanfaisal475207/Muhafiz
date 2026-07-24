"""
Tests for src/extraction/doc_classifier.py (Phase 4.4).

No real LLM calls — src.extraction.doc_classifier.call_llm is monkeypatched,
matching the pattern in tests/test_orchestrator.py. `no_network` (conftest,
autouse) guards against an unpatched call slipping through.
"""
import pytest

import src.extraction.doc_classifier as doc_classifier


@pytest.mark.asyncio
async def test_classifies_case_diary_and_attaches_regex_date(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return '{"doc_type": "Case Diary", "confidence": 0.9, "reasoning": "ongoing status"}'

    monkeypatch.setattr(doc_classifier, "call_llm", fake_call_llm)

    text = (
        "date_registered: 2026-01-20\n"
        "مذکورہ مقدمہ FIR-2026-BUR-007 کے سلسلے میں تفتیشی کارروائی جاری ہے۔"
    )
    result = await doc_classifier.classify_document(text)

    assert result["doc_type"] == "Case Diary"
    assert result["confidence"] == 0.9
    # Date comes from the regex extractor (4.3), not the LLM's own words.
    assert result["date_registered"] == "2026-01-20"


@pytest.mark.asyncio
async def test_unknown_doc_type_is_discarded(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return '{"doc_type": "Something Else", "confidence": 0.5}'

    monkeypatch.setattr(doc_classifier, "call_llm", fake_call_llm)

    result = await doc_classifier.classify_document("some text")
    assert result is None


@pytest.mark.asyncio
async def test_malformed_json_returns_none_not_raise(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return "not json at all"

    monkeypatch.setattr(doc_classifier, "call_llm", fake_call_llm)

    result = await doc_classifier.classify_document("some text")
    assert result is None


@pytest.mark.asyncio
async def test_llm_exception_returns_none_not_raise(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        raise RuntimeError("model server unreachable")

    monkeypatch.setattr(doc_classifier, "call_llm", fake_call_llm)

    result = await doc_classifier.classify_document("some text")
    assert result is None


@pytest.mark.asyncio
async def test_empty_text_short_circuits_without_llm_call(monkeypatch):
    calls = []

    async def fake_call_llm(system_prompt, user_message, **kwargs):
        calls.append(1)
        return "{}"

    monkeypatch.setattr(doc_classifier, "call_llm", fake_call_llm)

    result = await doc_classifier.classify_document("   ")
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_no_date_in_text_gives_none_not_error(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return '{"doc_type": "Recovery Memo", "confidence": 0.8}'

    monkeypatch.setattr(doc_classifier, "call_llm", fake_call_llm)

    result = await doc_classifier.classify_document("چوری شدہ سامان برآمد کیا گیا۔")
    assert result["doc_type"] == "Recovery Memo"
    assert result["date_registered"] is None
