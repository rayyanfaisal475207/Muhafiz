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


def test_location_gazetteer_covers_real_multi_city_stations():
    """
    M7 (Muhafiz Data API migration, docs/decisions/0001-muhafiz-api-migration.md):
    the gazetteer used to be Islamabad-only. The real dataset spans
    Lahore/Karachi/Rawalpindi/Faisalabad/Hyderabad/Multan/Chiniot.
    """
    for term in ("لاہور", "کراچی", "راولپنڈی", "فیصل آباد", "ماڈل ٹاؤن", "شاہ فیصل کالونی"):
        mentions = ner.extract_statistical(f"واقعہ {term} میں پیش آیا۔")
        assert any(m.text == term and m.type == "location" for m in mentions), f"{term!r} not found in gazetteer"


# ── Organization pattern ───────────────────────────────────────────────

def test_gang_suffix_pattern():
    text = "یہ سائبر فراڈ گروہ کا رکن ہے۔"
    mentions = ner.extract_statistical(text)
    assert any(m.type == "organization" and "فراڈ" in m.text for m in mentions)


# ── Role marker: content-word rejection (B-4 scope) ────────────────────

def test_role_marker_rejects_legal_clause_not_a_name():
    # Live-confirmed in FIR-2026-BUR-009's ground truth: "ملزم کے خلاف
    # قانونی کارروائی" ("legal action against the accused") was mistagged
    # as a Person because none of its words are in _STOPWORDS.
    text = "میں پولیس سے مطالبہ کرتا ہوں کہ ملزم کے خلاف قانونی کارروائی کی جائے۔"
    mentions = ner.extract_statistical(text)
    assert not any(m.type == "person" and "خلاف" in m.text for m in mentions)


def test_role_marker_rejects_action_clause_not_a_name():
    # Live-confirmed in the same document: "ملزم نے میرے گھر کا تالا توڑا"
    # mistagged "میرے گھر" ("my house") as a Person.
    text = "ملزم نے میرے گھر کا تالا توڑ کر اندر داخل ہوا۔"
    mentions = ner.extract_statistical(text)
    assert not any(m.type == "person" and "گھر" in m.text for m in mentions)


# ── Kinship-formula boundary bleed (findings.md Module 11) ─────────────
# Live-confirmed on fir-1001-26: a temporal/prepositional word or a role
# marker immediately preceding a name inside a KINSHIP_RE capture, or a
# residency clause immediately following one, bled into the captured
# span the same way an uncaught copula would — each of these produced a
# distinct duplicate/noise Person node in the live graph.

def test_kinship_child_trims_leading_temporal_marker():
    text = "2024-09-25 کو 17:10 بجے فیصل ولد محمد رمضان کی اطلاع پر۔"
    mentions = ner.extract_statistical(text)
    names = {m.text for m in mentions if m.type == "person"}
    assert "فیصل" in names
    assert "بجے فیصل" not in names


def test_kinship_child_trims_leading_preposition():
    text = "ایف آئی آر 1001/26 کے تحت فیصل ولد محمد رمضان کی شکایت پر۔"
    mentions = ner.extract_statistical(text)
    names = {m.text for m in mentions if m.type == "person"}
    assert "فیصل" in names
    assert "تحت فیصل" not in names


def test_kinship_child_trims_leading_role_marker():
    # Regression for the overlap-dedup interaction: _ROLE_RE alone would
    # already strip "مدعی" (see test_role_marker_complainant), but
    # _KINSHIP_RE's untrimmed "مدعی فیصل" child capture used to win the
    # overlap on higher confidence (0.85 vs 0.75) before both patterns
    # agreed on where the name starts.
    text = "مدعی فیصل ولد محمد رمضان ساکنہ محلہ اقبال ٹاؤن، اسلام آباد۔"
    mentions = ner.extract_statistical(text)
    names = {m.text for m in mentions if m.type == "person"}
    assert "فیصل" in names
    assert "مدعی فیصل" not in names


def test_kinship_parent_rejects_trailing_residency_clause():
    # The parent-side counterpart: "ساکنہ محلہ" ("resident of the
    # neighborhood of...") ran onto the father's name. "محلہ" can't be a
    # blanket stopword (it's a real station-name token elsewhere in this
    # corpus, e.g. "تھانہ محلہ اقبال ٹاؤن"), so this is rejected outright
    # rather than partially trimmed — losing one redundant mention of a
    # name captured correctly elsewhere is the accepted trade.
    text = "مدعی فیصل ولد محمد رمضان ساکنہ محلہ اقبال ٹاؤن، اسلام آباد۔"
    mentions = ner.extract_statistical(text)
    names = {m.text for m in mentions if m.type == "person"}
    assert "محمد رمضان ساکنہ محلہ" not in names


def test_station_name_containing_mohalla_still_extracted():
    # Guards against the obvious regression: "محلہ" must stay usable
    # inside a real station name even though "ساکنہ" (a different word)
    # is now a rejection trigger elsewhere.
    text = "تھانہ محلہ اقبال ٹاؤن میں مقدمہ درج کیا گیا۔"
    mentions = ner.extract_statistical(text)
    assert any(m.type == "location" and "محلہ" in m.text for m in mentions)


def test_role_marker_rejects_possession_clause_not_a_name():
    # findings.md's own "root cause B", live-confirmed as the same
    # failure class as the two rejection tests above, not a separate
    # one: "ملزم کے قبضے سے ..." ("from the accused's possession...") has
    # no name after the role marker at all — _ROLE_RE's name group still
    # matches the following clause, and stopword trimming happens to
    # strip it down to the one real content word in the middle, "قبضے"
    # ("possession"), which used to survive because nothing rejected it.
    text = "ملزم کے قبضے سے 30 بور پستول بمعہ 6 گولیاں برآمدگی پر۔"
    mentions = ner.extract_statistical(text)
    assert not any(m.type == "person" and "قبضے" in m.text for m in mentions)


# ── English structural cues for location/organization (B-4) ────────────

def test_english_location_suffix_highway():
    text = "The incident occurred near Kashmir Highway around 8 PM."
    mentions = ner.extract_statistical(text)
    assert any(m.text == "Kashmir Highway" and m.type == "location" for m in mentions)


def test_english_location_police_station_suffix():
    text = "The complainant was taken to Nilore Police Station for statement recording."
    mentions = ner.extract_statistical(text)
    assert any(m.text == "Nilore Police Station" and m.type == "location" for m in mentions)


def test_english_org_police_with_abbreviation():
    text = "The case was reported to Islamabad Traffic Police (ITP) on the same day."
    mentions = ner.extract_statistical(text)
    assert any(m.text == "Islamabad Traffic Police" and m.type == "organization" for m in mentions)


def test_english_org_police_not_mistagged_as_location():
    text = "The case was reported to Islamabad Traffic Police (ITP) on the same day."
    mentions = ner.extract_statistical(text)
    assert not any(m.text == "Islamabad Traffic Police" and m.type == "location" for m in mentions)


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
async def test_llm_failure_keeps_stronger_uncertain_candidates_unresolved_not_dropped(monkeypatch):
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        raise RuntimeError("model server unreachable")

    monkeypatch.setattr(ner, "call_llm", fake_call_llm)

    # Self-intro pattern, confidence 0.55 — clears
    # _ADJUDICATION_FAILURE_SURVIVAL_FLOOR (0.50), so it degrades to
    # "unresolved" (still present, low confidence) rather than vanishing.
    text = "میں شہری فریحہ سعید، رہائشی ماڈل ٹاؤن ہوں۔"
    result = await ner.extract_entities(text)
    assert any("فریحہ" in e["text"] or "سعید" in e["text"] for e in result)


@pytest.mark.asyncio
async def test_llm_failure_drops_weak_english_candidates_instead_of_flooding(monkeypatch):
    """
    M7 (Muhafiz Data API migration, docs/decisions/0001-muhafiz-api-migration.md):
    closes the fail-open hole. _ENGLISH_NAME_RE's bare capitalized-run
    candidates (confidence 0.45) are below _ADJUDICATION_FAILURE_SURVIVAL_FLOOR
    and must now be DROPPED on an LLM failure, not passed through
    unreviewed — the diagnosed "form-label flood" this fix exists to close.
    """
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        raise RuntimeError("model server unreachable")

    monkeypatch.setattr(ner, "call_llm", fake_call_llm)

    text = "Entry Dates: 2026-01-20"  # a form-label shape, not a real name
    result = await ner.extract_entities(text)
    assert not any("Entry Dates" in e["text"] for e in result)


@pytest.mark.asyncio
async def test_malformed_adjudication_response_also_drops_weak_candidates(monkeypatch):
    """Same floor applies to the OTHER fail-open path — a non-list/malformed
    LLM response, not just a raised exception."""
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return "not valid json at all"

    monkeypatch.setattr(ner, "call_llm", fake_call_llm)

    text = "Entry Dates: 2026-01-20"
    result = await ner.extract_entities(text)
    assert not any("Entry Dates" in e["text"] for e in result)
