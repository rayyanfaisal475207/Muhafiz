# ============================================================
# Script Detector — Roman-Urdu tagging at ingestion time (Phase 2.6)
#
# WHAT ROMAN URDU IS:
# Urdu written in Latin letters — "mulzim ko giraftar kar liya gaya hai"
# instead of "ملزم کو گرفتار کر لیا گیا ہے". It is how most Pakistanis
# actually type Urdu on a phone, so it turns up in complainant
# statements, WhatsApp evidence, and transcribed interviews even when
# the formal document body is Urdu-script or English.
#
# WHY TAG IT:
# Roman Urdu breaks both retrieval legs. Semantic search sees Latin
# characters and embeds it as (bad) English; BM25 can never match it
# against the Urdu-script chunks that say the same thing. Phase 9's eval
# wants a Roman-Urdu slice to measure exactly that gap — this tag is
# what makes that slice selectable.
#
# SCOPE — deliberately a heuristic, not a classifier:
# The current corpus is 9 English + 87 Urdu-script documents with
# little or no Roman Urdu, so this tag mostly will NOT fire yet. That is
# expected. It is infrastructure landed ahead of the content that needs
# it, and the bar it has to clear is "does not produce false positives
# on the English documents we do have" — not "state of the art language
# ID". A trained classifier here would be unfalsifiable against a corpus
# with no positive examples.
# ============================================================

import re

# GRAMMAR markers — postpositions, pronouns, auxiliaries, conjunctions.
#
# These carry the real signal, and the split from vocabulary below is the
# whole design. English police prose freely borrows Urdu *nouns* as terms
# of art ("the Mulzim has not been traced"), but it never borrows Urdu
# *grammar*. Only a genuinely Roman-Urdu sentence strings postpositions
# and auxiliaries together.
#
# Each entry is either not an English word at all or vanishingly rare in
# formal English. Excluded despite being very common in Roman Urdu:
# "hai", "main", "us", "so", "par", "is", "to" — all collide with
# ordinary English.
_GRAMMAR_MARKERS = frozenset({
    "nahi", "nahin", "kya", "kyun", "kyunki", "aur", "lekin", "magar",
    "mein", "mera", "meri", "tera", "teri", "uska", "uski", "unka",
    "yeh", "woh", "wo", "koi", "kuch", "kuchh", "sab", "phir", "abhi",
    "raha", "rahi", "rahe", "gaya", "gayi", "gaye", "kiya", "kiye",
    "karna", "karne", "karke", "hua", "hui", "huye", "hoga", "hogi",
    "tha", "thi", "thay", "diya", "dena", "liya", "lena",
    "sath", "saath", "baad", "pehle", "wala", "wali", "jaldi", "bohat",
    "bahut", "zyada", "thora", "acha", "theek", "matlab", "waqt",
    "ko", "ne", "ke", "ki", "ka", "se", "tak", "hi", "bhi", "ke",
})

# VOCABULARY markers — police/legal/evidence nouns and verbs.
#
# Supporting signal only. On their own these cannot fire the tag: they
# are exactly the words English chargesheets borrow.
_VOCAB_MARKERS = frozenset({
    "mulzim", "muddai", "gawah", "gawahi", "bayan", "muqadma", "muqadama",
    "thana", "thane", "chowki", "giraftar", "giraftari",
    "tafteesh", "tafteeshi", "daraz", "darkhast", "ittila", "wardat",
    "chori", "qatal", "dhoka", "zakhmi", "lash", "barmada",
    "sarkari", "adalat", "zamanat", "sazish", "hathiyar", "nakabandi",
})

_ROMAN_URDU_MARKERS = _GRAMMAR_MARKERS | _VOCAB_MARKERS

# Latin word runs. Apostrophes kept so "don't" stays one word.
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z']*")

# Any Arabic-script character — the signal that a document is Urdu
# SCRIPT, which by definition is not Roman Urdu.
_ARABIC_SCRIPT = re.compile(
    "["
    "؀-ۿ"   # Arabic
    "ݐ-ݿ"   # Arabic Supplement
    "ࢠ-ࣿ"   # Arabic Extended-A
    "ﭐ-﷿"   # Presentation Forms-A
    "ﹰ-﻿"   # Presentation Forms-B
    "]"
)

# Tuning. Every threshold has to clear for the tag to fire.
_MIN_LATIN_WORDS = 12          # too little text to judge
_MIN_DISTINCT_GRAMMAR = 2      # Urdu grammar, not a borrowed noun
_MIN_DISTINCT_MARKERS = 3      # distinct, so repeating one word can't qualify
_MIN_MARKER_RATIO = 0.04       # 4% of words — English prose scores ~0


def is_roman_urdu(text: str) -> bool:
    """
    Heuristic: does this text look like Urdu written in Latin script?

    Signals, all required:
      1. The text is predominantly Latin script (Urdu-script text is
         excluded outright — that is Urdu, not Roman Urdu).
      2. At least two DISTINCT grammar markers. English police prose
         borrows Urdu nouns as terms of art but never Urdu grammar, so
         this is what separates the two.
      3. At least three distinct markers overall, and a marker share of
         total words above the floor.

    Distinctness matters: an English chargesheet that writes "Mulzim"
    four times is not Roman Urdu, and counting occurrences rather than
    distinct words tagged exactly that (real false positive, found on
    CHARGESHEET-FIR-2026-THEFT-001.pdf during the Phase 2 re-ingest).

    Returns False for empty or very short text rather than guessing.
    """
    if not text or not text.strip():
        return False

    latin_words = [w.lower() for w in _LATIN_WORD.findall(text)]
    if len(latin_words) < _MIN_LATIN_WORDS:
        return False

    # If the text is mostly Arabic script, it is Urdu proper. A few
    # stray Urdu characters in an otherwise Latin document (a name, a
    # letterhead) should not disqualify it, hence a ratio not a flag.
    arabic_chars = len(_ARABIC_SCRIPT.findall(text))
    latin_chars = sum(len(w) for w in latin_words)
    if arabic_chars > latin_chars * 0.2:
        return False

    present = {w for w in latin_words if w in _ROMAN_URDU_MARKERS}
    if len(present & _GRAMMAR_MARKERS) < _MIN_DISTINCT_GRAMMAR:
        return False
    if len(present) < _MIN_DISTINCT_MARKERS:
        return False

    occurrences = sum(1 for w in latin_words if w in _ROMAN_URDU_MARKERS)
    return (occurrences / len(latin_words)) >= _MIN_MARKER_RATIO
