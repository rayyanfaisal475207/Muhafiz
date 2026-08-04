"""Tests for src/extraction/structured_fields.py (Phase 4.3)."""

import pytest

from src.extraction import structured_fields as sf


# ── CNIC ────────────────────────────────────────────────────────────────

def test_extract_cnic_ascii():
    text = "شناختی کارڈ نمبر 00000-9119877-0 درج کیا گیا۔"
    matches = sf.extract_cnics(text)
    assert len(matches) == 1
    assert matches[0].normalized == "00000-9119877-0"


def test_extract_cnic_urdu_indic_digits():
    # Same CNIC as above, written entirely in Urdu-Indic digits (U+06F0-06F9).
    urdu_indic_cnic = "۰۰۰۰۰-۹۱۱۹۸۷۷-۰"
    text = f"شناختی کارڈ نمبر {urdu_indic_cnic} درج کیا گیا۔"
    matches = sf.extract_cnics(text)
    assert len(matches) == 1
    assert matches[0].normalized == "00000-9119877-0"


def test_extract_cnic_arabic_indic_digits():
    # Arabic-Indic digits (U+0660-0669), a different range from Urdu-Indic.
    arabic_indic_cnic = "٠٠٠٠٠-٩١١٩٨٧٧-٠"
    matches = sf.extract_cnics(arabic_indic_cnic)
    assert len(matches) == 1
    assert matches[0].normalized == "00000-9119877-0"


def test_no_false_positive_cnic_on_short_digit_run():
    assert sf.extract_cnics("case FIR-2026-ARMS-001 registered") == []


# ── Module 6.5: separator-tolerant matching (OCR/vision-extraction noise) ──

def test_extract_cnic_with_en_dash_separator():
    # Realistic Gemini Vision OCR output: en-dash (U+2013) instead of a
    # plain hyphen. The canonical form must still come out ASCII-hyphenated
    # so the same CNIC matches across differently-OCR'd documents.
    text = "CNIC 00000–9119877–0 noted"
    matches = sf.extract_cnics(text)
    assert len(matches) == 1
    assert matches[0].normalized == "00000-9119877-0"


def test_extract_cnic_with_space_separator():
    matches = sf.extract_cnics("CNIC 00000 9119877 0 noted")
    assert len(matches) == 1
    assert matches[0].normalized == "00000-9119877-0"


def test_extract_cnic_with_no_separator_at_all():
    matches = sf.extract_cnics("CNIC 0000091198770 noted")
    assert len(matches) == 1
    assert matches[0].normalized == "00000-9119877-0"


def test_extract_phone_with_space_separator():
    matches = sf.extract_phones("call 0300 1234567")
    assert len(matches) == 1
    assert matches[0].normalized == "0300-1234567"


def test_extract_phone_with_no_separator():
    matches = sf.extract_phones("call 03001234567")
    assert len(matches) == 1
    assert matches[0].normalized == "0300-1234567"


def test_extract_plate_is_case_insensitive():
    matches = sf.extract_plates("ict-le-309 spotted")
    assert len(matches) == 1
    assert matches[0].normalized == "ICT-LE-309"


def test_extract_plate_with_em_dash_and_missing_separator():
    matches = sf.extract_plates("plate ICT—LE309 on record")
    assert len(matches) == 1
    assert matches[0].normalized == "ICT-LE-309"


def test_no_false_positive_plate_on_ordinary_word_then_number():
    # Regression: with a fully-optional separator at BOTH of the plate
    # pattern's gaps, "CASE-009" parsed as city_code="CAS" + series="E" +
    # number="009" (zero-length separator between "CAS" and "E") -- a real
    # false positive caught by test_graph_retriever.py's case-wide-
    # enumeration test. The letter-to-letter gap must require an actual
    # separator character; only letter-to-digit gaps may be fully optional.
    assert sf.extract_plates("How many accused are involved in CASE-009?") == []


def test_extract_fir_number_with_space_separators():
    matches = sf.extract_fir_numbers("مقدمہ نمبر FIR 2026 ARMS 001 کے تحت")
    assert len(matches) == 1
    assert matches[0].normalized == "FIR-2026-ARMS-001"


def test_extract_fir_number_with_no_separators():
    matches = sf.extract_fir_numbers("FIR2026ARMS001 filed")
    assert len(matches) == 1
    assert matches[0].normalized == "FIR-2026-ARMS-001"


# ── Phone ───────────────────────────────────────────────────────────────

def test_extract_phone():
    text = "witness_phone: 0300-1234567"
    matches = sf.extract_phones(text)
    assert len(matches) == 1
    assert matches[0].normalized == "0300-1234567"


def test_extract_phone_urdu_indic_digits():
    text = "۰۳۰۰-۱۲۳۴۵۶۷"
    matches = sf.extract_phones(text)
    assert len(matches) == 1
    assert matches[0].normalized == "0300-1234567"


# ── Vehicle plate ──────────────────────────────────────────────────────

def test_extract_plate():
    text = "سوزوکی پک اپ، ICT-LE-309"
    matches = sf.extract_plates(text)
    assert len(matches) == 1
    assert matches[0].normalized == "ICT-LE-309"


def test_extract_near_miss_plates_stay_distinct():
    text = "V-NM1A: ICT-FL-273 vs V-NM1B: ICT-FL-274"
    matches = sf.extract_plates(text)
    plates = {m.normalized for m in matches}
    assert plates == {"ICT-FL-273", "ICT-FL-274"}


# ── FIR number ─────────────────────────────────────────────────────────

def test_extract_fir_number():
    matches = sf.extract_fir_numbers("مقدمہ نمبر FIR-2026-ARMS-001 کے تحت")
    assert len(matches) == 1
    assert matches[0].normalized == "FIR-2026-ARMS-001"


# ── Dates ──────────────────────────────────────────────────────────────

def test_extract_iso_date_with_time():
    matches = sf.extract_dates("date_time: 2026-05-02 14:20")
    assert len(matches) == 1
    assert matches[0].normalized == "2026-05-02 14:20"


def test_extract_iso_date_no_time():
    matches = sf.extract_dates("date_registered: 2026-04-02")
    assert len(matches) == 1
    assert matches[0].normalized == "2026-04-02"


def test_extract_dmy_date():
    matches = sf.extract_dates("incident occurred on 14-06-2026")
    assert len(matches) == 1
    assert matches[0].normalized == "2026-06-14"


def test_invalid_calendar_date_is_dropped():
    # Month 13, day 45 — digit-shaped but not a real date.
    assert sf.extract_dates("ref 2026-13-45") == []


def test_urdu_indic_date():
    urdu_indic = "۲۰۲۶-۰۵-۰۲"
    matches = sf.extract_dates(urdu_indic)
    assert len(matches) == 1
    assert matches[0].normalized == "2026-05-02"


# ── Urdu spelled-out month names (B-3) ──────────────────────────────────

def test_urdu_month_name_date_confirmed_live_example():
    # Real narrative sentence from WITNESS-FIR-2026-BUR-007-01's ground
    # truth — the confirmed live example the audit found silently dropped.
    text = "میں نے بتاریخ 10 فروری 2026ء کو شام 7 بجے ایک شخص کو دیکھا۔"
    matches = sf.extract_dates(text)
    assert len(matches) == 1
    assert matches[0].normalized == "2026-02-10"


@pytest.mark.parametrize("month_name,month_num", [
    ("جنوری", "01"), ("فروری", "02"), ("مارچ", "03"), ("اپریل", "04"),
    ("مئی", "05"), ("جون", "06"), ("جولائی", "07"), ("اگست", "08"),
    ("ستمبر", "09"), ("اکتوبر", "10"), ("نومبر", "11"), ("دسمبر", "12"),
])
def test_urdu_month_name_all_twelve_months(month_name, month_num):
    matches = sf.extract_dates(f"5 {month_name} 2026")
    assert len(matches) == 1
    assert matches[0].normalized == f"2026-{month_num}-05"


def test_urdu_month_name_without_trailing_era_marker():
    matches = sf.extract_dates("5 جون 2026 کو رپورٹ درج کی گئی۔")
    assert len(matches) == 1
    assert matches[0].normalized == "2026-06-05"


def test_urdu_month_name_invalid_day_is_dropped():
    assert sf.extract_dates("32 دسمبر 2026ء") == []


def test_urdu_month_name_not_duplicated_when_iso_date_also_present():
    # A document could plausibly contain both a narrative Urdu-month date
    # and an unrelated ISO-format field date — must not collide/double-count.
    text = "date_registered: 2026-02-01\nمیں نے بتاریخ 10 فروری 2026ء کو دیکھا۔"
    matches = sf.extract_dates(text)
    normalized = {m.normalized for m in matches}
    assert normalized == {"2026-02-01", "2026-02-10"}


# ── Section references (Urdu comma, not ASCII) ────────────────────────

def test_split_sections_urdu_comma():
    assert sf.extract_sections_from_field("457 PPC، 380 PPC") == ["457 PPC", "380 PPC"]


def test_split_sections_single():
    assert sf.extract_sections_from_field("دفعہ 13 آرمز آرڈیننس 1965") == [
        "دفعہ 13 آرمز آرڈیننس 1965"
    ]


def test_split_sections_mixed_markers():
    result = sf.extract_sections_from_field("PECA 2016 دفعہ 13، 420 PPC")
    assert result == ["PECA 2016 دفعہ 13", "420 PPC"]


def test_split_sections_ascii_comma_also_works():
    assert sf.extract_sections_from_field("337-A(i) PPC, 506 PPC") == [
        "337-A(i) PPC",
        "506 PPC",
    ]


def test_find_section_field_in_free_text():
    # Realistic rendered-form shape: the sections field sits on its own
    # line. find_section_field() is best-effort over free text and is not
    # expected to correctly bound a section list followed by more prose on
    # the *same* line with no delimiter — see its docstring.
    text = "زیر دفعات: 457 PPC، 380 PPC\nدرج کیا گیا۔"
    refs = sf.extract_section_refs(text)
    assert refs == ["457 PPC", "380 PPC"]


def test_find_section_field_returns_none_without_anchor():
    assert sf.find_section_field("پولیس نے موقع پر پہنچ کر تفتیش کی۔") is None


# ── Aggregate ──────────────────────────────────────────────────────────

def test_extract_all_shape():
    result = sf.extract_all("FIR-2026-ARMS-001, CNIC 00000-9119877-0, phone 0300-1234567")
    assert set(result.keys()) == {"cnics", "phones", "plates", "fir_numbers", "dates", "sections"}
    assert len(result["fir_numbers"]) == 1
    assert len(result["cnics"]) == 1
    assert len(result["phones"]) == 1
