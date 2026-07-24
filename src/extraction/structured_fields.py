# ============================================================
# Structured-field extraction — regex only (Phase 4.3)
#
# CNIC, phone, vehicle plate, and FIR number are fixed-format government/
# administrative identifiers. They are extracted with regex, never an LLM:
# an LLM mis-transcribing a single CNIC digit silently breaks entity
# resolution downstream, since CNIC is the primary resolution key (§7.3).
# Dates and section references are looser but still regex-parseable across
# this corpus, so they live here too rather than in the LLM-based doc
# classifier (4.4) or NER (4.5) — those are for document type and person/
# place names, not identifiers with a fixed grammar.
#
# Real evidence will not be ASCII-only: this corpus mixes ASCII digits with
# Urdu-Indic (U+06F0-06F9) and Arabic-Indic (U+0660-0669) digits, and
# section lists are Urdu-comma-separated (، U+060C), not ASCII-comma. Every
# extractor here normalizes through Phase 2.4's `normalize_urdu()` first,
# which already folds both digit ranges to ASCII — this module does not
# reimplement that mapping.
# ============================================================

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from src.ingestion.text_normalizer import normalize_urdu

# ── Match result ──────────────────────────────────────────────────────────

@dataclass
class FieldMatch:
    """One extracted structured field.

    `start`/`end` are character offsets into the *normalized* text passed
    to the extractor (post `normalize_urdu`), not the original raw text —
    normalization is not length-preserving (see text_normalizer.py), so a
    caller needing offsets into the original string must normalize first
    and keep using that normalized string throughout, the same discipline
    ingestion/service.py already follows for chunking.
    """
    field_type: str      # "cnic" | "phone" | "plate" | "fir_number" | "date" | "section"
    raw_text: str         # the matched substring, as it appears post-normalization
    normalized: str        # canonical form (digits-only CNIC/phone, ISO date, uppercase plate)
    start: int
    end: int


# ── Regexes (operate on ASCII digits — normalize_urdu() runs first) ──────

# CNIC: 5-7-1 digit groups, e.g. 00000-1234567-8.
_CNIC_RE = re.compile(r"\b\d{5}-\d{7}-\d\b")

# Pakistani mobile: 03XX-XXXXXXX (4 digits, dash, 7 digits).
_PHONE_RE = re.compile(r"\b03\d{2}-\d{7}\b")

# Vehicle plate: ICT-XX-NNN (city code - series letters - number). Corpus
# uses 3-letter city codes and 2-letter series consistently (ICT-LE-309,
# ICT-FL-273); kept slightly permissive (2-4 / 1-2 letters, 2-4 digits) so
# a plausible real-world variant isn't silently dropped.
_PLATE_RE = re.compile(r"\b[A-Z]{2,4}-[A-Z]{1,2}-\d{2,4}\b")

# FIR number: FIR-YYYY-<CATEGORY>-NNN, e.g. FIR-2026-ARMS-001.
_FIR_RE = re.compile(r"\bFIR-\d{4}-[A-Z]+-\d{3}\b")

# Dates: ISO (YYYY-MM-DD, optionally with a time) and DD-MM-YYYY. The
# latter shows up in rendered Urdu narrative text (IMPLEMENTATION_PLAN.md
# 3.13's bidi-rendering note); the former is what every structured_fields
# block in the ground truth actually uses.
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?\b")
_DATE_DMY_RE = re.compile(r"\b(\d{2})-(\d{2})-(\d{4})\b")

# Section-reference anchor tokens. A "run" of section references is a
# comma-separated list where at least one token looks like a legal
# citation — digits plus one of these markers (PPC/PECA/Ordinance/Act and
# their Urdu equivalents). Used by find_section_field() to locate the
# declaration inside free text; extract_sections_from_field() does the
# actual Urdu-comma-aware split once that span is isolated.
_SECTION_ANCHOR_RE = re.compile(
    r"[^,،\n]*(?:PPC|PECA|Ordinance|آرڈیننس|Act|ایکٹ|دفعہ)[^,،\n]*"
    r"(?:\s*[,،]\s*[^,،\n]*(?:PPC|PECA|Ordinance|آرڈیننس|Act|ایکٹ|دفعہ)?[^,،\n]*)*"
)

# Splits a section-reference field on Urdu comma or ASCII comma.
_SECTION_SPLIT_RE = re.compile(r"[,،]")

# A field label ("Sections:", "زیر دفعات:") preceding the actual citation
# list — stripped from the front of find_section_field()'s match, since
# the anchor regex above can't help matching it (the label itself doesn't
# contain a marker token, but nothing stops it sitting before one on the
# same comma-free run).
_SECTION_LABEL_PREFIX_RE = re.compile(
    r"^(?:زیر\s+دفعات|زیر\s+دفعہ|دفعات|Sections?)\s*[:：]\s*"
)


def _normalize(text: str) -> str:
    """Digit/script normalization shared by every extractor in this module."""
    return normalize_urdu(text) if text else ""


# ── CNIC ───────────────────────────────────────────────────────────────────

def extract_cnics(text: str) -> list[FieldMatch]:
    """Find every CNIC-shaped identifier (5-7-1 digit groups) in text."""
    norm = _normalize(text)
    return [
        FieldMatch("cnic", m.group(0), m.group(0), m.start(), m.end())
        for m in _CNIC_RE.finditer(norm)
    ]


# ── Phone ──────────────────────────────────────────────────────────────────

def extract_phones(text: str) -> list[FieldMatch]:
    """Find every Pakistani-mobile-shaped number (03XX-XXXXXXX) in text."""
    norm = _normalize(text)
    return [
        FieldMatch("phone", m.group(0), m.group(0), m.start(), m.end())
        for m in _PHONE_RE.finditer(norm)
    ]


# ── Vehicle plate ────────────────────────────────────────────────────────

def extract_plates(text: str) -> list[FieldMatch]:
    """Find every vehicle-plate-shaped identifier (ICT-XX-NNN) in text."""
    norm = _normalize(text)
    out = []
    for m in _PLATE_RE.finditer(norm):
        raw = m.group(0)
        out.append(FieldMatch("plate", raw, raw.upper(), m.start(), m.end()))
    return out


# ── FIR number ───────────────────────────────────────────────────────────

def extract_fir_numbers(text: str) -> list[FieldMatch]:
    """Find every FIR-number-shaped identifier (FIR-YYYY-CAT-NNN) in text."""
    norm = _normalize(text)
    out = []
    for m in _FIR_RE.finditer(norm):
        raw = m.group(0)
        out.append(FieldMatch("fir_number", raw, raw.upper(), m.start(), m.end()))
    return out


# ── Dates ────────────────────────────────────────────────────────────────

def _valid_iso(y: str, mo: str, d: str) -> Optional[str]:
    try:
        date(int(y), int(mo), int(d))
    except ValueError:
        return None
    return f"{y}-{mo}-{d}"


def extract_dates(text: str) -> list[FieldMatch]:
    """
    Find every date-shaped substring, ISO (YYYY-MM-DD[ HH:MM]) or
    DD-MM-YYYY, and normalize to ISO 'YYYY-MM-DD' (or 'YYYY-MM-DD HH:MM'
    when a time was present). Calendar-invalid matches (e.g. month 13) are
    silently dropped rather than raised — a regex can't distinguish a real
    date from a coincidental digit run, so validation via `datetime.date`
    is the actual filter.
    """
    norm = _normalize(text)
    out: list[FieldMatch] = []

    for m in _DATE_ISO_RE.finditer(norm):
        y, mo, d, hh, mm = m.groups()
        iso = _valid_iso(y, mo, d)
        if iso is None:
            continue
        normalized = f"{iso} {hh}:{mm}" if hh else iso
        out.append(FieldMatch("date", m.group(0), normalized, m.start(), m.end()))

    for m in _DATE_DMY_RE.finditer(norm):
        d, mo, y = m.groups()
        # Skip anything the ISO pass already matched (same span would
        # otherwise double-count e.g. no overlap in practice since the
        # two patterns have incompatible group order, but an explicit
        # overlap guard costs nothing and avoids a subtle double-report).
        if any(o.start <= m.start() < o.end for o in out):
            continue
        iso = _valid_iso(y, mo, d)
        if iso is None:
            continue
        out.append(FieldMatch("date", m.group(0), iso, m.start(), m.end()))

    out.sort(key=lambda fm: fm.start)
    return out


# ── Section references ──────────────────────────────────────────────────

def extract_sections_from_field(field_text: str) -> list[str]:
    """
    Split an already-isolated section-reference field into individual
    citations, on the Urdu comma (، U+060C) — the corpus convention — as
    well as ASCII comma. This is the well-defined contract validated
    against the ground truth's `structured_fields.sections` strings, e.g.
    "457 PPC، 380 PPC" -> ["457 PPC", "380 PPC"].
    """
    norm = _normalize(field_text)
    parts = [p.strip() for p in _SECTION_SPLIT_RE.split(norm)]
    return [p for p in parts if p]


def find_section_field(text: str) -> Optional[str]:
    """
    Best-effort locate of a section-reference declaration inside free
    document text (not a pre-isolated field), by anchoring on legal-
    citation marker tokens (PPC/PECA/Ordinance/Act and Urdu equivalents).
    Returns the raw matched span, or None if no anchor token is found.
    Callers pass the result to extract_sections_from_field() for the split.

    Bounded by newlines only, not sentence punctuation — a rendered form
    field on its own line (the expected shape) extracts cleanly; a section
    list followed immediately by more prose on the *same* line, with no
    comma/newline between them, will over-capture the trailing prose into
    the last citation. Regex has no way to know where a legal citation
    grammar ends and narrative Urdu begins without a delimiter.
    """
    norm = _normalize(text)
    m = _SECTION_ANCHOR_RE.search(norm)
    if not m:
        return None
    span = m.group(0).strip()
    span = _SECTION_LABEL_PREFIX_RE.sub("", span)
    return span.strip() or None


def extract_section_refs(text: str) -> list[str]:
    """Convenience: locate + split section references from free text in one call."""
    field = find_section_field(text)
    return extract_sections_from_field(field) if field else []


# ── Aggregate ────────────────────────────────────────────────────────────

def extract_all(text: str) -> dict:
    """
    Run every extractor over `text` and return one dict, keyed by field
    type. Convenience entry point for ingestion (4.10) — callers that only
    need one field type should call the specific extractor directly rather
    than pay for all five regex passes.
    """
    return {
        "cnics": extract_cnics(text),
        "phones": extract_phones(text),
        "plates": extract_plates(text),
        "fir_numbers": extract_fir_numbers(text),
        "dates": extract_dates(text),
        "sections": extract_section_refs(text),
    }
