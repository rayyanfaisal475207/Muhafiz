"""
Phase 2 — Urdu-aware text processing.

Covers the sentence splitter (2.1), the shared tokenizer (2.2), the Urdu
character normalizer (2.4) and the Roman-Urdu detector (2.6).

The Urdu samples here are lifted from the real corpus — narrative_statement
fields in data/memory/_ground_truth/*.json and the FIR/case-diary bodies in
data/documents/ — not invented, so the edge cases they exercise are the ones
that actually occur.
"""
import pytest

from src.ingestion.sentence_splitter import (
    boundary_at_or_before,
    sentence_boundaries,
    split_sentences,
)
from src.ingestion.script_detector import is_roman_urdu
from src.ingestion.text_normalizer import normalize_urdu, normalize_whitespace
from src.ingestion.tokenizer import tokenize


# Real narrative_statement from CASEDIARY-FIR-2026-ARMS-003-01.json.
CORPUS_URDU_NARRATIVE = (
    "مطابق مقدمہ نمبر FIR-2026-ARMS-003، تھانہ مارگلہ، Illegal Weapon Possession "
    "کے معاملے میں تفتیش جاری ہے۔ اب تک ملزم کے بیان ریکارڈ کیے گئے ہیں اور شہادت "
    "نامے جمع کیے جا چکے ہیں۔ مزید تفتیشی کارروائی جاری ہے۔"
)


# ── 2.1 Sentence splitter ─────────────────────────────────────────────────────

def test_splits_urdu_on_the_urdu_full_stop():
    """
    The whole point of the phase: ۔ (U+06D4), not the ASCII period, ends
    an Urdu sentence. An English-tuned splitter sees zero boundaries here.
    """
    text = "یہ پہلا جملہ ہے۔ یہ دوسرا جملہ ہے۔"

    assert split_sentences(text) == ["یہ پہلا جملہ ہے۔", "یہ دوسرا جملہ ہے۔"]


def test_splits_urdu_on_the_urdu_question_mark():
    """؟ is U+061F — a different codepoint from ASCII '?', not a glyph variant."""
    text = "یہ پہلا جملہ ہے۔ یہ دوسرا جملہ ہے؟"

    assert split_sentences(text) == ["یہ پہلا جملہ ہے۔", "یہ دوسرا جملہ ہے؟"]


def test_splits_a_real_corpus_narrative_into_its_three_sentences():
    sentences = split_sentences(CORPUS_URDU_NARRATIVE)

    assert len(sentences) == 3
    assert all(s.endswith("۔") for s in sentences)
    assert sentences[0].startswith("مطابق مقدمہ نمبر")


def test_does_not_split_on_the_urdu_comma():
    """
    IMPLEMENTATION_PLAN.md §2.1 lists ، alongside ۔ as a sentence-final
    mark. It is not — ، (U+060C) is Urdu's comma. The corpus narrative
    above carries two of them inside its first sentence; splitting on
    them would shred every sentence in the corpus into fragments.
    """
    first = split_sentences(CORPUS_URDU_NARRATIVE)[0]

    assert first.count("،") == 2


def test_splits_english_on_ascii_marks():
    text = "The complainant reported a theft. Was the phone recovered? Yes!"

    assert split_sentences(text) == [
        "The complainant reported a theft.",
        "Was the phone recovered?",
        "Yes!",
    ]


def test_splits_mixed_urdu_and_english():
    text = "The FIR was registered. مقدمہ درج کر لیا گیا ہے۔ Investigation continues."

    assert split_sentences(text) == [
        "The FIR was registered.",
        "مقدمہ درج کر لیا گیا ہے۔",
        "Investigation continues.",
    ]


def test_splits_roman_urdu():
    text = "Mulzim ko giraftar kiya gaya. Kya woh bayan de chuka hai? Haan."

    assert split_sentences(text) == [
        "Mulzim ko giraftar kiya gaya.",
        "Kya woh bayan de chuka hai?",
        "Haan.",
    ]


# ── 2.1 edge cases: ۔ and . that are NOT sentence ends ────────────────────────

def test_urdu_abbreviation_dots_are_not_sentence_ends():
    """
    ۔ doubles as Urdu's abbreviation dot: ڈی۔ایس۔پی is "DSP", one word,
    not three sentences.
    """
    text = "ڈی۔ایس۔پی صاحب نے کہا کہ تفتیش مکمل ہے۔"

    assert split_sentences(text) == [text]


def test_english_abbreviations_are_not_sentence_ends():
    text = "Dr. Khan of P.S. Margalla filed it."

    assert split_sentences(text) == [text]


def test_decimal_numbers_are_not_sentence_ends():
    text = "The recovered amount was 3.5 lakh rupees."

    assert split_sentences(text) == [text]


def test_single_letter_initials_are_not_sentence_ends():
    text = "Inspector A. R. Malik recorded the statement."

    assert split_sentences(text) == [text]


def test_numbered_form_fields_are_not_sentence_ends():
    """
    Docling exports the FIR forms as markdown tables whose first cell is
    a field number — "| 2. Day: | Thursday |". Treating that "2." as a
    sentence end sheds a two-character fragment off every row.
    """
    text = "| 1. Date and time of occurrence: | 2026-01-01 |\n| 2. Day: | Thursday |"

    assert split_sentences(text) == [
        "| 1. Date and time of occurrence: | 2026-01-01 |",
        "| 2. Day: | Thursday |",
    ]


def test_a_mid_sentence_period_with_no_following_space_is_not_a_boundary():
    text = "Refer to www.punjabpolice.gov.pk for the status."

    assert split_sentences(text) == [text]


def test_line_breaks_are_boundaries():
    """
    Form fields carry no terminator at all. Without newline boundaries a
    whole FIR form is one unsplittable "sentence" and the chunker has
    nothing to snap to.
    """
    text = "نام: احمد علی\nعمر: 34 سال\nپتہ: بھارہ کہو"

    assert split_sentences(text) == ["نام: احمد علی", "عمر: 34 سال", "پتہ: بھارہ کہو"]


def test_empty_and_whitespace_input_yields_no_sentences():
    assert split_sentences("") == []
    assert split_sentences("   \n  ") == []


def test_text_with_no_terminator_is_a_single_sentence():
    assert split_sentences("ملزم کو گرفتار کر لیا گیا") == ["ملزم کو گرفتار کر لیا گیا"]


# ── 2.1 boundary offsets (what the chunker actually consumes) ─────────────────

def test_boundaries_are_offsets_into_the_original_text():
    text = "پہلا جملہ۔ دوسرا جملہ۔"

    boundaries = sentence_boundaries(text)

    assert boundaries == [text.index("۔") + 1]
    assert text[: boundaries[0]] == "پہلا جملہ۔"


def test_end_of_text_is_not_reported_as_a_boundary():
    """The chunker treats the tail specially; a trailing boundary would
    make it emit an empty final chunk."""
    text = "ایک جملہ۔"

    assert sentence_boundaries(text) == []


def test_boundary_lookup_picks_the_last_one_that_fits():
    assert boundary_at_or_before([10, 20, 30], limit=25, floor=0) == 20
    assert boundary_at_or_before([10, 20, 30], limit=30, floor=0) == 30


def test_boundary_lookup_rejects_boundaries_at_or_before_the_floor():
    """Returning a boundary <= start would make the chunker emit an empty
    chunk and never advance."""
    assert boundary_at_or_before([10, 20], limit=25, floor=20) is None
    assert boundary_at_or_before([], limit=25, floor=0) is None


# ── 2.2 Tokenizer ─────────────────────────────────────────────────────────────

def test_tokenizes_urdu_into_words_without_punctuation():
    tokens = tokenize("پولیس نے ملزم کو گرفتار کر لیا۔")

    assert tokens == ["پولیس", "نے", "ملزم", "کو", "گرفتار", "کر", "لیا"]


def test_urdu_punctuation_is_stripped_from_the_final_word():
    """
    The old `.lower().split()` left "لیا۔" glued together, so the same
    word indexed as two different terms depending on where in a sentence
    it happened to fall.
    """
    with_stop = tokenize("گرفتار کر لیا۔")
    without_stop = tokenize("گرفتار کر لیا")

    assert with_stop == without_stop


def test_script_variants_tokenize_to_the_same_term():
    """Arabic kaf ك vs Urdu keheh ک — visually identical, different
    codepoints. This is what made BM25 score zero across keyboards."""
    assert tokenize("كتاب") == tokenize("کتاب")


def test_arabic_indic_digits_tokenize_as_ascii():
    assert tokenize("۲۰۲۶") == tokenize("2026")


def test_lowercases_english():
    assert tokenize("Police STATION") == ["police", "station"]


def test_identifiers_survive_whole_and_are_also_emitted_in_parts():
    """
    BM25 is exact-match: a query for "ARMS-003" must hit a chunk holding
    the full FIR number, and a query for the full number must not be
    diluted into four common terms. Both forms are indexed.
    """
    tokens = tokenize("FIR-2026-ARMS-003")

    assert "fir-2026-arms-003" in tokens
    assert {"fir", "2026", "arms", "003"} <= set(tokens)


def test_tokenizes_mixed_urdu_english_text():
    tokens = tokenize("تھانہ مارگلہ، Illegal Weapon Possession کے معاملے میں")

    assert "تھانہ" in tokens
    assert "illegal" in tokens
    assert "،" not in "".join(tokens)


def test_empty_input_yields_no_tokens():
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_punctuation_only_input_yields_no_tokens():
    assert tokenize("۔؟!،") == []


# ── 2.4 Urdu normalization ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "arabic_form, urdu_form",
    [
        ("كتاب", "کتاب"),      # ك U+0643 -> ک U+06A9
        ("مية", "میہ"),        # ي U+064A -> ی, ة U+0629 -> ہ
        ("ه", "ہ"),            # ه U+0647 -> ہ U+06C1
        ("أحمد", "احمد"),      # أ U+0623 -> ا
    ],
)
def test_arabic_letter_forms_normalize_to_urdu_forms(arabic_form, urdu_form):
    assert normalize_urdu(arabic_form) == normalize_urdu(urdu_form)


def test_folds_arabic_presentation_forms_to_logical_letters():
    """
    The single highest-impact case in this corpus. Docling extracts the
    Urdu PDFs as Arabic presentation forms (U+FB50–U+FEFF) — the shaped
    per-position glyphs, not the logical letters. The pre-Phase-2 Chroma
    collection stored every Urdu chunk that way, so an Urdu query written
    normally matched none of it lexically. The text below is verbatim
    from FIR-2026-BUR-009.pdf's stored chunk.
    """
    presentation = "ﻣﻠﺰﻡ ﮐﮯ ﺧﻼﻑ ﻗﺎﻧﻮﻧﯽ ﮐﺎﺭﺭﻭﺍﺋﯽ ﮐﯽ ﺟﺎﺋﮯ۔"
    logical = "ملزم کے خلاف قانونی کارروائی کی جائے۔"

    assert normalize_urdu(presentation) == normalize_urdu(logical)
    assert tokenize(presentation) == tokenize(logical)


def test_strips_diacritics():
    assert normalize_urdu("کِتَاب") == "کتاب"


def test_keeps_diacritics_when_asked():
    assert normalize_urdu("کِتَاب", strip_diacritics=False) != "کتاب"


def test_strips_tatweel():
    assert normalize_urdu("کـــتاب") == "کتاب"


def test_normalizes_arabic_indic_digits_to_ascii():
    assert normalize_urdu("۲۰۲۶") == "2026"


def test_does_not_collapse_alef_madda():
    """آ (U+0622) is its own Urdu letter, not a variant of ا."""
    assert "آ" in normalize_urdu("آپ")


def test_does_not_collapse_do_chashmi_heh():
    """ھ (U+06BE) marks aspiration — بھائی is not بہائی."""
    assert "ھ" in normalize_urdu("بھائی")


def test_english_text_passes_through_unchanged():
    text = "Police Station Bhara Kahu, FIR-2026-THEFT-001."

    assert normalize_urdu(text) == text


def test_strips_invisible_bidi_controls():
    """RLM/LRM ride along in text extracted from RTL PDFs and make a
    chunk fail to match an identical-looking query."""
    assert normalize_urdu("ملزم‏‎") == "ملزم"


def test_urdu_normalization_leaves_whitespace_normalization_intact():
    """The two functions compose; 2.4 did not replace 2.0's behaviour."""
    assert normalize_whitespace(normalize_urdu("a" + " " * 10 + "b")) == "a b"


def test_empty_input_is_a_noop():
    assert normalize_urdu("") == ""


# ── 2.6 Roman-Urdu detection ──────────────────────────────────────────────────

def test_detects_roman_urdu():
    text = (
        "Mulzim ko giraftar kar liya gaya. Gawah ne bayan diya ke wo raat ko "
        "thana pohancha tha aur muqadma daraz karwaya."
    )

    assert is_roman_urdu(text) is True


def test_english_police_prose_is_not_flagged():
    """
    The only false-positive risk that matters: the corpus's 9 English
    documents. A tag that fires on those makes the Phase 9 Roman-Urdu
    slice meaningless.
    """
    text = (
        "The complainant reported that his mobile phone was stolen from the "
        "market. Police registered the case under section 379 of the PPC and "
        "the investigation is ongoing."
    )

    assert is_roman_urdu(text) is False


def test_urdu_script_is_not_roman_urdu():
    assert is_roman_urdu(CORPUS_URDU_NARRATIVE) is False


def test_english_prose_borrowing_an_urdu_noun_is_not_flagged():
    """
    Regression, found on the real corpus during the Phase 2 re-ingest:
    CHARGESHEET-FIR-2026-THEFT-001.pdf is English legal prose that uses
    "Mulzim" as a borrowed term of art, repeatedly. Counting marker
    OCCURRENCES tagged it as Roman Urdu. English police prose borrows
    Urdu nouns freely but never Urdu grammar — so the test is distinct
    grammar markers, not raw hits. Text below is verbatim from the chunk.
    """
    text = (
        "2. Name(s) and particulars of accused (Mulzim (accused)): Mulzim not yet "
        "identified/traced. 5. Whether offence proved, and against whom: Commission "
        "of the offence stands established from the evidence on record; however, "
        "since the Mulzim has not been traced or identified, the question of proof "
        "against a specific individual does not arise at this stage."
    )

    assert is_roman_urdu(text) is False


def test_short_text_is_not_guessed_at():
    assert is_roman_urdu("Mulzim giraftar.") is False


def test_empty_text_is_not_flagged():
    assert is_roman_urdu("") is False
    assert is_roman_urdu("   ") is False
