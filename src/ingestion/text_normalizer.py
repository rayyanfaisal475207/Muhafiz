# ============================================================
# Text Normalizer — Whitespace Cleanup Before Chunking
#
# Docling's markdown table export right-pads every cell in a column to
# match that column's widest cell (a single long narrative field can pad
# every other row's short value out to 200+ spaces). That padding carries
# no meaning for an LLM reading the chunk later, and it also makes the
# chunker's break-point search far more likely to hit its worst case (see
# src/ingestion/chunker.py's advance-floor fix). This is a real cleanup
# step in its own right, not a workaround for that fix — belt and
# suspenders: the chunker is now robust against pathological input
# regardless of source, and this makes the actual input less pathological
# in the first place.
#
# ── Phase 2.4: Urdu character/diacritic normalization ──
#
# Urdu is written in the Arabic script, and the same Urdu letter has
# multiple codepoints depending on whether the text was typed with an
# Urdu keyboard, an Arabic keyboard, or extracted from a PDF font that
# mapped to Arabic forms. ک (U+06A9, Urdu keheh) and ك (U+0643, Arabic
# kaf) render near-identically and compare unequal. So do ی/ي and ہ/ه.
# Every one of those pairs appears in this corpus.
#
# The consequence before this existed: BM25 scored a query written with
# one variant as zero against a chunk written with the other, and the
# embedder saw two different tokens for the same word. `.lower()` — the
# only normalization the pipeline had — does nothing at all here; Urdu
# is caseless.
#
# Hand-rolled rather than imported: urduhack ships exactly these
# utilities, but it does not install on Python 3.13 (see the decision
# note in src/ingestion/tokenizer.py). The mappings below follow the
# same Unicode targets it uses.
# ============================================================

import re
import unicodedata

# 3+ consecutive spaces (or tabs) collapse to one. Deliberately narrower
# than "any whitespace" — newlines (including the \n\n paragraph breaks
# the chunker relies on) are left untouched.
_LONG_SPACE_RUN = re.compile(r"[ \t]{3,}")


def normalize_whitespace(text: str) -> str:
    """
    Collapse runs of 3+ consecutive spaces/tabs down to a single space.

    Leaves newlines untouched, so paragraph breaks (\\n\\n) the chunker
    splits on are preserved exactly.
    """
    if not text:
        return text
    return _LONG_SPACE_RUN.sub(" ", text)


# ── Urdu character normalization ──────────────────────────────────────────────

# Arabic-script letter variants -> their canonical Urdu form.
#
# Deliberately NOT mapped:
#   آ U+0622 alef-madda      — a distinct Urdu letter, not a variant of ا
#   ھ U+06BE do-chashmi heh  — a distinct Urdu letter (بھ, تھ, دھ...);
#                              collapsing it into ہ destroys aspiration
#   ؤ U+0624 / ئ U+0626      — real Urdu letters, carry meaning
_CHARACTER_MAP = {
    # Yeh: Arabic yeh and alef-maksura -> Urdu farsi-yeh
    "ي": "ی",  # U+064A -> U+06CC
    "ى": "ی",  # U+0649 -> U+06CC
    "ﻱ": "ی",  # presentation form
    "ﻲ": "ی",
    # Yeh-barree stays (ے U+06D2) — a distinct Urdu letter.
    # Kaf: Arabic kaf -> Urdu keheh
    "ك": "ک",  # U+0643 -> U+06A9
    "ﻙ": "ک",
    "ﻚ": "ک",
    # Heh: Arabic heh and teh-marbuta -> Urdu gol-heh
    "ه": "ہ",  # U+0647 -> U+06C1
    "ة": "ہ",  # U+0629 -> U+06C1
    "ۀ": "ہ",  # U+06C0
    "ﻩ": "ہ",
    "ﻪ": "ہ",
    # Alef with hamza / wasla -> plain alef (آ excluded above)
    "أ": "ا",  # U+0623
    "إ": "ا",  # U+0625
    "ٱ": "ا",  # U+0671
    "ﺃ": "ا",
    "ﺇ": "ا",
    # Arabic comma/semicolon/question already match Urdu usage; left alone.
}

# Arabic-Indic (U+0660–0669) and Extended Arabic-Indic (U+06F0–06F9)
# digits -> ASCII. FIR numbers, CNICs, dates and section numbers are
# written in both forms across this corpus; indexing them apart means a
# query for "FIR-2026-THEFT-001" misses a document that wrote ۲۰۲۶.
_DIGIT_MAP = {
    **{chr(0x0660 + i): str(i) for i in range(10)},
    **{chr(0x06F0 + i): str(i) for i in range(10)},
}

_TRANSLATION_TABLE = str.maketrans({**_CHARACTER_MAP, **_DIGIT_MAP})

# Harakat / diacritics and Quranic annotation marks. Optional in Urdu
# orthography — almost always omitted in running text, occasionally
# present in scanned/typed formal documents — so leaving them in creates
# two spellings of the same word.
_DIACRITICS = re.compile(
    "["
    "ً-ٕ"   # fathatan .. hamza-below  (harakat)
    "ٰ"           # superscript alef
    "ۖ-ۭ"   # Quranic annotation marks
    "࢘-࢟"   # Arabic Extended-B marks
    "࣊-ࣿ"   # Arabic Extended-A marks
    "]"
)

# Tatweel/kashida: a pure typographic stretch character with no
# phonetic value. Always removed.
_TATWEEL = re.compile("ـ+")

# Zero-width and bidi control characters. These ride along in text
# extracted from RTL PDFs and are invisible, so a chunk containing them
# silently fails to match an identical-looking query. ZWNJ (U+200C) is
# KEPT — it marks a real orthographic break inside Urdu compounds, and
# the tokenizer uses it as a word separator.
_INVISIBLES = re.compile(
    "["
    "​"           # zero-width space
    "‍"           # zero-width joiner
    "‎‏"    # LRM / RLM
    "‪-‮"   # bidi embedding / override
    "⁦-⁩"   # bidi isolates
    "﻿"           # BOM / zero-width no-break space
    "]"
)


def normalize_urdu(text: str, strip_diacritics: bool = True) -> str:
    """
    Unify Arabic-script variant characters into their canonical Urdu form.

    Applied at ingestion (before chunking) and inside `tokenize()`, so
    the same word indexes and queries identically no matter which
    keyboard or PDF font produced it.

    Steps:
      1. Unicode NFKC — see the note below on presentation forms.
      2. Strip invisible zero-width/bidi controls (keeping ZWNJ).
      3. Remove tatweel (kashida).
      4. Optionally strip harakat/diacritics.
      5. Map Arabic letter variants -> Urdu forms, Arabic-Indic digits
         -> ASCII.

    Length-preserving is NOT a goal — diacritics and tatweel are
    removed, so offsets into the input do not survive. Call it before
    the chunker computes any offsets, never after.

    Args:
        text:             Any text; pure-English input passes through
                          essentially untouched.
        strip_diacritics: Set False to keep harakat (e.g. if a downstream
                          consumer needs vowelled text for display).

    Returns:
        The normalized string. Empty/None-ish input is returned as-is.
    """
    if not text:
        return text

    # NFKC, not NFC — this one line does more for this corpus than every
    # mapping below combined. Docling extracts the Urdu PDFs as Arabic
    # PRESENTATION FORMS (U+FB50–U+FEFF): the shaped per-position glyphs
    # a font renders, not the logical letters. "ملزم" comes out as
    # U+FEE3 U+FEE0 U+FEB0 U+FEE1 — four codepoints that render
    # identically to the real word and compare equal to nothing. Every
    # Urdu chunk in the pre-Phase-2 collection was stored this way, which
    # is why an Urdu query matched almost nothing lexically.
    # NFC leaves presentation forms alone; NFKC folds them back to their
    # base letters (and unpacks the lam-alef ligature ﻻ into ل + ا).
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLES.sub("", text)
    text = _TATWEEL.sub("", text)
    if strip_diacritics:
        text = _DIACRITICS.sub("", text)
    return text.translate(_TRANSLATION_TABLE)
