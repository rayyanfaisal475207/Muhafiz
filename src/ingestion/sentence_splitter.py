# ============================================================
# Sentence Splitter — Urdu-Aware Sentence Boundary Detection
#
# WHY THIS EXISTS:
# The chunker used to break text on raw character offsets and whitespace
# (text.rfind(" ", ...)). That is English-first thinking, and it is wrong
# for this corpus: Urdu's sentence-final mark is ۔ (U+06D4 ARABIC FULL
# STOP), not the ASCII period, so an English-tuned splitter finds *no*
# sentence structure in Urdu at all and silently cuts chunks mid-sentence.
# 87 of the 96 documents in data/documents/ are Urdu-script.
#
# WHY REGEX AND NOT THE MODEL SERVER:
# The model server exposes /split_sentences, but the finalized model
# roster lists sentence splitting as "rule-based regex (no model)" — the
# endpoint is itself a regex, not a model. Calling it would make every
# ingestion run depend on a free-tier ngrok tunnel (which rotates) for
# something pure regex does in-process in microseconds. This module
# reproduces that endpoint's behaviour locally; the marks it splits on
# (۔ ؟ !) are taken from it deliberately.
#
# WHAT COUNTS AS A BOUNDARY:
#   - Urdu sentence-final marks:  ۔ (U+06D4)  ؟ (U+061F)
#   - ASCII marks, for the mixed EN/UR text this corpus is full of: . ? !
#   - Line breaks. The corpus is largely Docling markdown-table export of
#     FIR forms — "| 2. Day: | Thursday |" rows carry no terminator at
#     all, and without treating a newline as a boundary the chunker would
#     find no snap point in a whole form and fall back to whitespace.
#
# NOT a boundary: the Urdu comma ، (U+060C). IMPLEMENTATION_PLAN.md §2.1
# lists it alongside ۔ as a "sentence-final mark" — that is an error in
# the plan. ، is Urdu's comma; splitting on it shreds every sentence in
# the corpus (real sample: "مطابق مقدمہ نمبر FIR-2026-ARMS-003، تھانہ
# مارگلہ، ... میں تفتیش جاری ہے۔" — one sentence, two commas).
#
# THE MID-SENTENCE ۔ PROBLEM:
# ۔ is also Urdu's abbreviation dot: ڈی۔ایس۔پی (DSP), ایم۔اے (M.A.).
# Same for ASCII "." in "Dr.", "No.", "3.5", "sec. 302". The guards below
# handle these; see _is_real_boundary().
# ============================================================

import re
from bisect import bisect_right

# Urdu + ASCII sentence-final marks. Note ؟ is U+061F (Arabic question
# mark), visually mirrored from ASCII '?' and a different codepoint.
SENTENCE_END_MARKS = "۔؟!?."

# A run of terminators (handles "؟!" and "...") plus any closing
# quote/bracket that trails them.
_CANDIDATE = re.compile(r"[۔؟!?.]+[\"'”’)\]»》]*")

# A line break, optionally padded with spaces/tabs. The boundary sits at
# the start of the match — i.e. at the end of the visible text.
_LINE_BREAK = re.compile(r"[ \t]*\n[ \t\n]*")

# The last word before a terminator, without the terminator itself.
_TRAILING_WORD = re.compile(r"([^\s۔؟!?.]+)[۔؟!?.]*$")

# Only a line start (optionally a markdown table pipe / bullet) sits
# between here and the preceding word.
_LIST_MARKER = re.compile(r"(?:^|\n)[ \t]*[|\-*>]?[ \t]*\d+$")

# English abbreviations that end in "." mid-sentence. Deliberately short:
# every entry is a false-negative risk (a real sentence genuinely ending
# in "No." stops being split), so only ones that actually occur in police
# / legal / FIR prose are listed.
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st",
    "no", "nos", "vs", "etc", "fig", "sec", "art", "para",
    "govt", "dept", "inc", "ltd", "pvt", "co", "rs",
    "i.e", "e.g", "a.m", "p.m", "u.s", "p.s",       # P.S. = Police Station
    "approx", "vol", "ch", "cl",
})


def _is_real_boundary(text: str, mark_start: int, cand_end: int) -> bool:
    """
    Decide whether a terminator run is genuinely sentence-final.

    `mark_start` is the index of the first terminator character,
    `cand_end` the index just past the candidate (terminators + any
    trailing closers).
    """
    # Must be followed by whitespace or end-of-text. This single rule
    # kills the majority of false positives at zero cost:
    #   ایم۔اے  (M.A.)   3.5   www.example.gov.pk   FIR-2026/A.B
    if cand_end < len(text) and not text[cand_end].isspace():
        return False

    prefix = text[:mark_start]
    match = _TRAILING_WORD.search(prefix)
    if not match:
        # Nothing but whitespace before it — a stray terminator.
        return False
    word = match.group(1)

    # A single letter before the dot is an initial or an acronym letter,
    # never a sentence end: "J. R. R. Tolkien", "ڈی۔ ایس۔ پی".
    if len(word) == 1 and not word.isdigit():
        return False

    terminator = text[mark_start]
    if terminator == ".":
        if word.lower().strip(".") in _ABBREVIATIONS:
            return False
        # A bare number at the start of a line is a list/field marker, not
        # a sentence end. The FIR forms in this corpus are numbered field
        # lists — "| 1. Date and time of occurrence: | ... |" — and
        # without this guard every one of those rows sheds a two-character
        # "| 1." fragment. Anchored to line-start on purpose: "the loss
        # was Rs. 500." mid-line still splits.
        if word.isdigit() and _LIST_MARKER.search(prefix):
            return False
        # A decimal number that got a space injected mid-way by upstream
        # extraction ("Rs. 3. 5 lakh"). Cheap to guard, harmless if absent.
        if word[-1].isdigit():
            rest = text[cand_end:].lstrip()
            if rest[:1].isdigit():
                return False

    return True


def sentence_boundaries(text: str) -> list[int]:
    """
    Return the sorted offsets at which sentences END (exclusive slice
    indices), excluding the end of the text itself.

    Offsets, not strings, are the primitive here on purpose: the chunker
    needs to snap a break point onto a boundary while slicing the
    *original* text, so re-joining split sentence strings (and guessing
    at the whitespace between them) is not good enough.

        >>> t = "پہلا جملہ۔ دوسرا جملہ۔"
        >>> [t[:b] for b in sentence_boundaries(t)]
        ['پہلا جملہ۔']
    """
    if not text:
        return []

    offsets: set[int] = set()

    for match in _CANDIDATE.finditer(text):
        if _is_real_boundary(text, match.start(), match.end()):
            offsets.add(match.end())

    for match in _LINE_BREAK.finditer(text):
        if match.start() > 0:
            offsets.add(match.start())

    text_len = len(text)
    return sorted(o for o in offsets if 0 < o < text_len)


def split_sentences(text: str) -> list[str]:
    """
    Split `text` into sentences on Urdu (۔ ؟) and ASCII (. ? !) marks and
    on line breaks.

    Each returned sentence is stripped; empty ones are dropped. The
    terminator stays attached to the sentence it ends.

    Args:
        text: Any mix of Urdu-script, English, and Roman-Urdu text.

    Returns:
        List of sentence strings. Never returns [""]; returns [] for
        empty/whitespace-only input.
    """
    if not text or not text.strip():
        return []

    sentences: list[str] = []
    start = 0
    for boundary in sentence_boundaries(text):
        piece = text[start:boundary].strip()
        if piece:
            sentences.append(piece)
        start = boundary

    tail = text[start:].strip()
    if tail:
        sentences.append(tail)

    return sentences


def boundary_at_or_before(boundaries: list[int], limit: int, floor: int) -> int | None:
    """
    Largest boundary <= `limit` that is strictly greater than `floor`,
    or None if there isn't one.

    Pulled out of the chunker so the binary search is testable on its own
    and the chunker reads as intent rather than as bisect arithmetic.
    """
    index = bisect_right(boundaries, limit)
    if index == 0:
        return None
    candidate = boundaries[index - 1]
    return candidate if candidate > floor else None
