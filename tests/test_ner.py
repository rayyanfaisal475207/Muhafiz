"""Tests for src/extraction/ner.py (Phase 4.5)."""

import pytest

import src.extraction.ner as ner


# ── Statistical pass: kinship formula ─────────────────────────────────

def test_kinship_formula_extracts_both_names_and_stops_before_copula():
    # Real narrative sentence from FIR-2026-ARMS-001's ground truth: the
    # regression case for the greedy-name-run-over-capturing-"ہے" bug.
    text = "جس کا نام عمران ستار ولد غلام ستار ہے۔"
    mentions = ner.extract_statistical(text)
    names = {m.text: m.type for m in mentions}
    assert names.get("عمران ستار") == "person"
    assert names.get("غلام ستار") == "person"
    # The trailing copula "ہے" must never end up inside a captured name.
    assert not any("ہے" in text for text in names)


def test_kinship_offsets_are_correct():
    text = "عمران ستار ولد غلام ستار"
    mentions = ner.extract_statistical(text)
    child = next(m for m in mentions if m.text == "عمران ستار")
    assert text[child.start:child.end] == "عمران ستار"
    parent = next(m for m in mentions if m.text == "غلام ستار")
    assert text[parent.start:parent.end] == "غلام ستار"


# ── Role-marker pattern ────────────────────────────────────────────────

def test_role_marker_complainant():
    text = "مدعی احمد رضا قریشی نے بیان دیا۔"
    mentions = ner.extract_statistical(text)
    assert any(m.text == "احمد رضا قریشی" and m.type == "person" for m in mentions)


# ── Self-introduction pattern (B-1) ────────────────────────────────────

def test_self_intro_extracts_name_with_rihaishi_after_comma():
    # Real narrative sentence from WITNESS-FIR-2026-BUR-007-01's ground truth.
    text = "میں فیصل شہزاد قریشی، رہائشی ترنول، تھانہ ترنول کا رہائشی ہوں۔"
    mentions = ner.extract_statistical(text)
    assert any(m.text == "فیصل شہزاد قریشی" and m.type == "person" for m in mentions)


def test_self_intro_extracts_name_before_thana_clause():
    # Real narrative sentence from DARKHAST-FIR-2026-BUR-009's ground truth —
    # "رہائشی" doesn't follow the name directly, it comes after an
    # intervening "تھانہ X" clause.
    text = "میں عثمان خالد ملک، تھانہ مارگلہ کا رہائشی ہوں۔"
    mentions = ner.extract_statistical(text)
    assert any(m.text == "عثمان خالد ملک" and m.type == "person" for m in mentions)


def test_self_intro_handles_comma_immediately_after_main():
    # Real narrative sentence from FIR-2026-BUR-009's ground truth — a comma
    # directly after "میں" before the name.
    text = "میں، محمد علی، سیکٹر ایچ ڈیبلیو ایچ ایس، اسلام آباد کا رہائشی ہوں۔"
    mentions = ner.extract_statistical(text)
    assert any(m.text == "محمد علی" and m.type == "person" for m in mentions)


def test_self_intro_candidate_is_low_confidence():
    # Deliberately below LOW_CONFIDENCE_THRESHOLD — "میں <name>" alone is
    # less distinctive than the kinship/role markers, so it must always go
    # through LLM adjudication rather than ship unreviewed.
    text = "میں فیصل شہزاد قریشی، رہائشی ترنول کا رہائشی ہوں۔"
    mentions = ner.extract_statistical(text)
    match = next(m for m in mentions if m.text == "فیصل شہزاد قریشی")
    assert match.confidence < ner.LOW_CONFIDENCE_THRESHOLD


def test_self_intro_does_not_fire_on_plain_action_sentence():
    # "میں نے ..." ("I did ...") is an ordinary action sentence with no
    # self-introduction — must not misfire on it just because it starts
    # with "میں".
    text = "میں نے پولیس کو مطلع کیا۔"
    mentions = ner.extract_statistical(text)
    assert not any(m.type == "person" for m in mentions)


def test_self_intro_does_not_cross_sentence_boundary():
    # "رہائشی" appears in a LATER, unrelated sentence — must not be treated
    # as the disambiguating cue for an unrelated "میں ..." in an earlier one.
    text = "میں نے پولیس کو مطلع کیا۔ گواہ ایک رہائشی علاقے میں موجود تھا۔"
    mentions = ner.extract_statistical(text)
    assert not any(m.type == "person" and "پولیس" in m.text for m in mentions)


# ── Station / location ─────────────────────────────────────────────────

def test_station_pattern():
    text = "میں تھانہ رمنہ کی حدود میں موجود تھا۔"
    mentions = ner.extract_statistical(text)
    assert any(m.text == "رمنہ" and m.type == "location" for m in mentions)


def test_location_gazetteer_hit():
    text = "مکان نمبر 12، گلی 4، جی-9/1، اسلام آباد"
    mentions = ner.extract_statistical(text)
    assert any(m.text == "اسلام آباد" and m.type == "location" for m in mentions)


# ── Organization pattern ───────────────────────────────────────────────

def test_gang_suffix_pattern():
    text = "یہ سائبر فراڈ گروہ کا رکن ہے۔"
    mentions = ner.extract_statistical(text)
    assert any(m.type == "organization" and "فراڈ" in m.text for m in mentions)


# ── English weak fallback ──────────────────────────────────────────────

def test_english_candidate_is_low_confidence():
    text = "Inspector Fariha Saeed filed the report."
    mentions = ner.extract_statistical(text)
    assert any(m.confidence < ner.LOW_CONFIDENCE_THRESHOLD for m in mentions)


def test_english_stopword_not_captured():
    text = "FIR Report registered today."
    mentions = ner.extract_statistical(text)
    assert not any(m.text.startswith("FIR") for m in mentions)


# ── Overlap dedup ───────────────────────────────────────────────────────

def test_overlap_dedup_keeps_higher_confidence():
    text = "تھانہ رمنہ"
    mentions = ner.extract_statistical(text)
    # Only the station-pattern hit (0.85) should survive for "رمنہ", not a
    # duplicate lower-confidence overlapping span.
    spans_for_ramna = [m for m in mentions if "رمنہ" in m.text]
    assert len(spans_for_ramna) == 1


# ── Full pipeline with mocked LLM fallback ─────────────────────────────

@pytest.mark.asyncio
async def test_extract_entities_confident_candidates_skip_llm(monkeypatch):
    calls = []

    async def fake_call_llm(system_prompt, user_message, **kwargs):
        calls.append(1)
        return "[]"

    monkeypatch.setattr(ner, "call_llm", fake_call_llm)

    text = "مدعی احمد رضا قریشی نے تھانہ رمنہ میں رپورٹ درج کرائی۔"
    result = await ner.extract_entities(text)

    assert calls == []  # every candidate here is above threshold
    assert any(e["text"] == "احمد رضا قریشی" for e in result)
    assert all(
        set(e.keys()) == {"text", "type", "char_span", "source_chunk_id", "confidence", "attributes"}
        for e in result
    )


@pytest.mark.asyncio
async def test_extract_entities_adjudicates_low_confidence(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return '[{"index": 0, "keep": true, "type": "person", "confidence": 0.9}]'

    monkeypatch.setattr(ner, "call_llm", fake_call_llm)

    text = "Inspector Fariha Saeed filed the report."
    result = await ner.extract_entities(text)

    kept = [e for e in result if e["text"] == "Inspector Fariha Saeed" or "Fariha" in e["text"]]
    assert kept
    assert kept[0]["confidence"] == 0.9


@pytest.mark.asyncio
async def test_llm_rejection_drops_the_candidate(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return '[{"index": 0, "keep": false, "type": null, "confidence": 0.0}]'

    monkeypatch.setattr(ner, "call_llm", fake_call_llm)

    text = "Section Report"  # weak/no real candidates expected either way
    result = await ner.extract_entities(text)
    assert result == [] or all(e["confidence"] >= ner.LOW_CONFIDENCE_THRESHOLD for e in result)


@pytest.mark.asyncio
async def test_llm_failure_keeps_candidates_unresolved_not_dropped(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        raise RuntimeError("model server unreachable")

    monkeypatch.setattr(ner, "call_llm", fake_call_llm)

    text = "Inspector Fariha Saeed filed the report."
    result = await ner.extract_entities(text)
    # Degrades to "unresolved" (still present, low confidence, method
    # statistical) rather than silently vanishing.
    assert any("Fariha" in e["text"] for e in result)
