# ============================================================
# Word Tokenizer — the shared tokenizer for Urdu + English text
#
# This is the single `tokenize()` that BM25 (Phase 2.5) and NER
# (Phase 4.5) both consume. One tokenizer, one set of rules, so a term
# indexed at ingestion time is the same term produced from a query.
#
# ------------------------------------------------------------
# DECISION (Phase 2.2): urduhack vs. Stanza vs. custom regex
#
# Verdict: custom regex, tested on Python 3.13.1. Both libraries were
# actually installed and run — this is not a paper comparison.
#
# urduhack — REJECTED, does not work on Python 3.13.
#   `pip install urduhack` "succeeds", which is the trap: the resolver
#   cannot satisfy urduhack 1.1.0's `tensorflow~=2.3.0` (no cp313 wheels
#   exist) so it silently backtracks to urduhack 1.0.3 and installs it
#   *without* TensorFlow. Import then dies:
#       urduhack/tokenization/keras_tokenizer.py:12
#       ModuleNotFoundError: No module named 'tensorflow'
#   It also wanted to downgrade click 8.x -> 7.1.2 in this environment,
#   which would break the uvicorn/FastAPI CLI stack. This is exactly the
#   unmaintained-dependency risk ARCHITECTURE.md §4.2 flagged (the
#   package documents Python 3.6/3.7 support); confirmed, not theoretical.
#   Consequence: its normalization utilities are unavailable too, so the
#   Urdu character normalization in text_normalizer.py is hand-rolled
#   against the same Unicode mappings rather than imported.
#
# Stanza — WORKS, but rejected for this call site.
#   stanza 1.14.0 installs cleanly on 3.13 (torch 2.13.0+cpu was already
#   present, so no new heavy dependency), the Urdu UD model downloads in
#   ~6s, and tokenization quality is good:
#       'پولیس نے ملزم کو گرفتار کر لیا۔'
#       -> ['پولیس','نے','ملزم','کو','گرفتار','کر','لیا','۔']
#   It loses on cost, at the one place this function is hottest. BM25
#   rebuilds its index in memory on every query (bm25_retriever.py), so
#   tokenization runs over the whole candidate pool per request.
#   Measured on this machine: 26.7 ms/chunk vs 0.08 ms/chunk for the
#   regex below — ~330x. At TOP_K_RETRIEVAL=10 that is ~0.3s added to
#   every query, plus a 3.7s cold model load and a downloaded model
#   artifact ingestion would then depend on. For splitting on whitespace
#   and punctuation — which is what the Urdu tokenizer's output above
#   actually amounts to on this corpus — that is not a trade worth making.
#
# Custom regex — CHOSEN.
#   The corpus is police/legal prose and form fields, not free web text.
#   The tokens that matter for lexical retrieval are Urdu words, English
#   words, and identifiers (FIR-2026-ARMS-003, CNIC digits, section
#   numbers). Whitespace + punctuation segmentation covers all three, at
#   a cost the per-query BM25 rebuild can absorb.
#
#   Where this loses to Stanza: it cannot segment Urdu compounds written
#   without a space, and it cannot split clitics that are orthographically
#   fused. Neither shows up as a retrieval failure on this corpus, where
#   Urdu is space-separated. Revisit if Phase 9's eval shows Urdu BM25
#   recall lagging its English slice.
#
#   Stanza is left installed but deliberately NOT added to
#   requirements.txt: Phase 4.5's NER may want it (per-document cost,
#   amortized very differently from per-query), and that is the right
#   place to make that call.
# ------------------------------------------------------------

import re

from src.ingestion.text_normalizer import normalize_urdu

# Urdu is written in the Arabic script. The ranges that matter:
#   U+0600–U+06FF  Arabic (contains all core Urdu letters)
#   U+0750–U+077F  Arabic Supplement
#   U+08A0–U+08FF  Arabic Extended-A
#   U+FB50–U+FDFF  Arabic Presentation Forms-A
#   U+FE70–U+FEFF  Arabic Presentation Forms-B
# Punctuation inside those ranges (۔ ؟ ، ؛ ٪) is excluded explicitly by
# the class below, or it would be swallowed into words.
_URDU_LETTERS = (
    "ؠ-ؿ"   # Arabic block letters (excludes ، ؛ ؟ punctuation)
    "ف-ي"   # feh .. yeh
    "ٮ-ۓ"   # extended letters incl. ٹ ڈ ڑ ک گ ں ہ ھ ی ے
    "ە"           # ae
    "ۮ-ۿ"   # further Arabic-script letters
    "ݐ-ݿ"   # Arabic Supplement
    "ࢠ-ࢿ"   # Arabic Extended-A letters
    "ﭐ-﷿"   # Presentation Forms-A
    "ﹰ-ﻼ"   # Presentation Forms-B
)

# A word is a run of Urdu letters, Latin letters, or digits. Internal
# -, /, _, . are kept so "FIR-2026-ARMS-003", "2026/BUR-007" and
# "61101-1234567-8" survive as single tokens (see _split_identifier for
# why they are ALSO emitted in parts).
_WORD = re.compile(
    rf"[{_URDU_LETTERS}0-9A-Za-z]+(?:[-/_.][{_URDU_LETTERS}0-9A-Za-z]+)*"
)

# Zero-width non-joiner / joiner. Urdu uses ZWNJ to break the cursive
# join inside compounds; for matching purposes those halves are separate
# words, so ZWNJ is turned into a space before tokenizing.
_ZERO_WIDTH_JOINERS = re.compile("[‌‍]")

# An identifier worth also emitting in parts.
_IDENTIFIER_SEPARATOR = re.compile(r"[-/_.]")


def _split_identifier(token: str) -> list[str]:
    """
    For a compound identifier, emit its parts alongside the whole.

    "FIR-2026-ARMS-003" -> ["fir-2026-arms-003", "fir", "2026", "arms", "003"]

    Rationale: BM25 is exact-match. Without the parts, a query for
    "ARMS-003" scores zero against a chunk containing the full FIR
    number. Without the whole, a query for the full number is diluted
    across four common terms. Emitting both costs a few tokens of index
    size and gets both queries to hit.
    """
    parts = [p for p in _IDENTIFIER_SEPARATOR.split(token) if p]
    return parts if len(parts) > 1 else []


def tokenize(text: str) -> list[str]:
    """
    Split text into lowercase word tokens, Urdu-aware.

    Steps:
      1. Urdu character/diacritic normalization, so ک/ك, ی/ي, ہ/ه and
         Arabic-Indic vs. ASCII digits index as the same term. This is
         the reason `.lower()` was never enough: casing is meaningless
         in Urdu, but *script variation* is rampant and invisible.
      2. Zero-width joiners become word separators.
      3. Words = runs of Urdu/Latin letters and digits, with internal
         -/_. kept for identifiers.
      4. Latin is lowercased (a no-op on Urdu, which is caseless).
      5. Compound identifiers additionally emit their parts.

    Returns [] for empty input. Never returns empty-string tokens.
    """
    if not text or not text.strip():
        return []

    text = normalize_urdu(text)
    text = _ZERO_WIDTH_JOINERS.sub(" ", text)

    tokens: list[str] = []
    for match in _WORD.finditer(text):
        token = match.group(0).lower().strip("-/_.")
        if not token:
            continue
        tokens.append(token)
        tokens.extend(_split_identifier(token))

    return tokens
