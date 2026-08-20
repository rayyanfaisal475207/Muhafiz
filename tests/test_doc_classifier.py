"""
Tests for src/extraction/doc_classifier.py (Phase 4.4).

No real LLM calls — src.extraction.doc_classifier.call_llm is monkeypatched,
matching the pattern in tests/test_orchestrator.py. `no_network` (conftest,
autouse) guards against an unpatched call slipping through.
"""
import pytest

import src.extraction.doc_classifier as doc_classifier
from src.extraction import structured_fields as sf


# ── _find_registration_date label coverage (B-2) ────────────────────────
# Real corpus label variants confirmed via a full-corpus text grep (not a
# guessed list) — the original regex only recognized "date registered" /
# "date of registration" / "registration date" / "dated" / "fir date" and
# missed every one of these.

@pytest.mark.parametrize("text,expected_date", [
    ("Some intro text.\nDate Reported: 2026-03-11\nMore text.", "2026-03-11"),
    ("Person Details\nDate Last Seen: 2026-04-02\nDescription follows.", "2026-04-02"),
    ("Recovery Memo\nDate of Submission: 2026-02-20\n", "2026-02-20"),
    ("Report\nEntry Dates: 2026-05-01\n", "2026-05-01"),
    ("Case Diary\nDate: 2026-06-06\n", "2026-06-06"),
    ("تاریخ اندراج: 2026-01-20", "2026-01-20"),
])
def test_find_registration_date_recognizes_real_corpus_labels(text, expected_date):
    dates = sf.extract_dates(text)
    date, confidence = doc_classifier._find_registration_date(text, dates)
    assert date == expected_date
    assert confidence == "labeled"


def test_find_registration_date_falls_back_when_no_label_present():
    text = "Narrative: incident occurred on 2026-05-13, reported later."
    dates = sf.extract_dates(text)
    date, confidence = doc_classifier._find_registration_date(text, dates)
    assert date == "2026-05-13"
    assert confidence == "unlabeled_fallback"


def test_find_registration_date_none_when_no_date_at_all():
    date, confidence = doc_classifier._find_registration_date("No dates here.", [])
    assert date is None
    assert confidence is None


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
async def test_unknown_doc_type_drops_doc_type_only_not_the_whole_result(monkeypatch):
    """
    M7 (Muhafiz Data API migration, docs/decisions/0001-muhafiz-api-migration.md):
    an out-of-vocabulary doc_type used to discard the ENTIRE result,
    including a regex-validated date_registered that has nothing to do
    with whether the LLM's doc_type string was recognized. Only doc_type
    itself is dropped now.
    """
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return '{"doc_type": "Something Else", "confidence": 0.5}'

    monkeypatch.setattr(doc_classifier, "call_llm", fake_call_llm)

    result = await doc_classifier.classify_document("Date Registered: 2026-01-20. Some text.")
    assert result is not None
    assert result["doc_type"] is None
    assert result["date_registered"] == "2026-01-20"
    assert result["date_registered_confidence"] == "labeled"


@pytest.mark.asyncio
async def test_recognized_new_migration_doc_types_are_accepted(monkeypatch):
    """PKM Service Application / Roznamcha Entry — added in M7 for record
    types this classifier now sees via src/ingestion/muhafiz_records.py's
    rendered free text."""
    for doc_type in ("PKM Service Application", "Roznamcha Entry"):
        async def fake_call_llm(system_prompt, user_message, **kwargs):
            return f'{{"doc_type": "{doc_type}", "confidence": 0.7}}'

        monkeypatch.setattr(doc_classifier, "call_llm", fake_call_llm)
        result = await doc_classifier.classify_document("some text")
        assert result["doc_type"] == doc_type


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
