"""Tests for src/extraction/structured_fields.py (Phase 4.3)."""

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
