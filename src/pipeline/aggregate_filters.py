"""
[Gold-QA fix — Module 144] Filter extraction for XAGG's count aggregates.

The defect this module fixes, measured live on 2026-09-14: *"How many FIRs
were registered in 2019 or earlier?"* returned `Total cases: 73`. The
dispatch was right (`resolve_aggregate_kind()` picked the grand-total FIR
count), but the count aggregates took no filters at all, so the year bound
was silently dropped and the grand total was served as the answer to a
question it does not answer. The true figure is 0 — every FIR in the corpus
was reported in 2024, 2025 or 2026.

This module is the EXTRACTION half: question text in, an
`AggregateFilters` value out. It is deliberately

  - PURE and DETERMINISTIC. No LLM, no I/O. The all-32 gold regression
    control has to be able to assert that the extractor fires on none of
    the gold questions, byte for byte, and that is only a meaningful
    assertion for a function that returns the same thing every time. (The
    brief pointed at `sql_extractor.extract_sql_params()`; on reading it,
    that is an LLM prompt for the `police_reference_data` SQL route that
    returns a single `date` string — it has no range, age, section or
    district output, its prompt is shared with the SQL route, and its
    output is not stable enough for an equality control. Reusing it would
    have meant editing a shared prompt and adding a model call to every
    XAGG count. See MODULE144_RESULT.md §2.)
  - CONSERVATIVE. A year becomes a date bound only when it is attached to
    a bound word ("in 2024", "2019 or earlier", "between 2024 and 2025",
    "2024 mein", "2024 میں"). Two separate single-year mentions with no
    range connector ("... in 2026 ... in 2024 ...") are a COMPARISON, not
    a filter, and yield no date bound at all — that is gold M7's shape.
    A number becomes an age bound only next to an age cue ("aged", "years
    old", "umar", "عمر", "سال"). A section is taken only from the same
    patterns Module 76's KB9 family already trusts, and a four-digit
    "section" is rejected because it is a year ("PECA 2016").
  - NOT special-cased to any value. Any year, any range, any district in
    the corpus, any section code.

The APPLICATION half — turning these filters into a case-id allow-list
against the graph — lives in `xagg.py` (`resolve_filter_case_ids()`),
next to the aggregates that consume it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# ── Districts ──────────────────────────────────────────────────────────────
#
# The nine District nodes this corpus actually has, keyed by the spellings a
# question can carry. Values are the STORED Urdu `District.name`, because
# that is the only thing the graph can be matched on (`district_id` is an
# opaque "DIST-04"). Same nine-district scope, and same "aliases only for
# jurisdictions the corpus stores" rule, as
# `graph_retriever._DISTRICT_ALIASES`; extended with the space-free Urdu
# spellings and the Roman-Urdu forms a chat question uses.
#
# "karachi" maps to BOTH Karachi districts. `graph_retriever` treats it as
# ambiguous because its resolver returns ONE id; a count filter can take the
# union, and "how many FIRs in Karachi" plainly means both.
#
# There is no separate city field anywhere in the graph. In this data the
# city IS the district (Faisalabad, Lahore, ...), so a city name is treated
# as a district name here and the rendered text says "district".
DISTRICT_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "lahore": ("لاہور",),
    "lahor": ("لاہور",),
    "لاہور": ("لاہور",),
    "rawalpindi": ("راولپنڈی",),
    "pindi": ("راولپنڈی",),
    "راولپنڈی": ("راولپنڈی",),
    "faisalabad": ("فیصل آباد",),
    "fsd": ("فیصل آباد",),
    "فیصل آباد": ("فیصل آباد",),
    "فیصلآباد": ("فیصل آباد",),
    "islamabad": ("اسلام آباد",),
    "اسلام آباد": ("اسلام آباد",),
    "اسلامآباد": ("اسلام آباد",),
    "hyderabad": ("حیدر آباد",),
    "حیدر آباد": ("حیدر آباد",),
    "حیدرآباد": ("حیدر آباد",),
    "multan": ("ملتان",),
    "ملتان": ("ملتان",),
    "chiniot": ("چنیوٹ",),
    "چنیوٹ": ("چنیوٹ",),
    "karachi central": ("کراچی وسطی",),
    "کراچی وسطی": ("کراچی وسطی",),
    "karachi east": ("کراچی ایسٹ",),
    "کراچی ایسٹ": ("کراچی ایسٹ",),
    "karachi": ("کراچی وسطی", "کراچی ایسٹ"),
    "کراچی": ("کراچی وسطی", "کراچی ایسٹ"),
}

# English display name for each stored Urdu name, for the rendered text.
DISTRICT_DISPLAY: dict[str, str] = {
    "لاہور": "Lahore",
    "راولپنڈی": "Rawalpindi",
    "فیصل آباد": "Faisalabad",
    "اسلام آباد": "Islamabad",
    "حیدر آباد": "Hyderabad",
    "ملتان": "Multan",
    "چنیوٹ": "Chiniot",
    "کراچی وسطی": "Karachi Central",
    "کراچی ایسٹ": "Karachi East",
}


@dataclass(frozen=True)
class AggregateFilters:
    """The filter parameter set the count aggregates accept.

    Every field is optional; `is_empty()` is True when nothing was
    extracted, and in that case every aggregate runs exactly as it did
    before this module. `date_to` is INCLUSIVE ("2019 or earlier" is
    `date_to=2019-12-31`), as is `date_from`. `age_min`/`age_max` are
    inclusive too ("aged 25 to 40" keeps 25 and 40; "under 30" is
    `age_max=29`).

    `districts` holds the STORED Urdu `District.name` values (one alias can
    expand to two, see `DISTRICT_NAME_ALIASES`). `district` is the phrase
    the question used, kept for the rendered text.
    """

    date_from: Optional[date] = None
    date_to: Optional[date] = None
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    section: Optional[str] = None
    district: Optional[str] = None
    districts: tuple[str, ...] = field(default_factory=tuple)

    def is_empty(self) -> bool:
        return not (
            self.date_from or self.date_to or self.age_min is not None
            or self.age_max is not None or self.section or self.districts
        )

    @property
    def has_date(self) -> bool:
        return bool(self.date_from or self.date_to)

    @property
    def has_age(self) -> bool:
        return self.age_min is not None or self.age_max is not None

    def to_log(self) -> str:
        """Compact `key=value` form for the `XAGG <kind>:` log line — the
        only evidence of what a live run computed (Module 55)."""
        parts = []
        if self.date_from:
            parts.append(f"date_from={self.date_from.isoformat()}")
        if self.date_to:
            parts.append(f"date_to={self.date_to.isoformat()}")
        if self.age_min is not None:
            parts.append(f"age_min={self.age_min}")
        if self.age_max is not None:
            parts.append(f"age_max={self.age_max}")
        if self.section:
            parts.append(f"section={self.section}")
        if self.districts:
            parts.append("district=" + "|".join(self.districts))
        return ", ".join(parts) if parts else "none"

    def describe_date(self) -> Optional[str]:
        """"in 2019 or earlier" / "between 2024 and 2025" / "in 2024"."""
        if not self.has_date:
            return None
        f, t = self.date_from, self.date_to
        if f and t:
            if f.year == t.year and _is_year_start(f) and _is_year_end(t):
                return f"in {f.year}"
            if _is_year_start(f) and _is_year_end(t):
                return f"between {f.year} and {t.year}"
            return f"between {f.isoformat()} and {t.isoformat()}"
        if t:
            return f"in {t.year} or earlier" if _is_year_end(t) else f"on or before {t.isoformat()}"
        return f"in {f.year} or later" if _is_year_start(f) else f"on or after {f.isoformat()}"

    def describe_age(self) -> Optional[str]:
        if not self.has_age:
            return None
        lo, hi = self.age_min, self.age_max
        if lo is not None and hi is not None:
            return f"aged {lo}–{hi}" if lo != hi else f"aged {lo}"
        if hi is not None:
            return f"aged {hi} or younger"
        return f"aged {lo} or older"

    def describe_district(self) -> Optional[str]:
        if not self.districts:
            return None
        names = [DISTRICT_DISPLAY.get(d, d) for d in self.districts]
        if len(names) == 1:
            return f"in {names[0]} district"
        return "in " + " / ".join(names) + " districts"

    def describe_section(self) -> Optional[str]:
        return f"under section {self.section}" if self.section else None

    def describe(self) -> str:
        """The human-readable constraint the rendered text carries, so the
        reader and the verifier both see that it was honoured."""
        parts = [
            p for p in (
                self.describe_date(), self.describe_district(),
                self.describe_section(), self.describe_age(),
            ) if p
        ]
        return " ".join(parts)


EMPTY_FILTERS = AggregateFilters()


def _is_year_start(d: date) -> bool:
    return d.month == 1 and d.day == 1


def _is_year_end(d: date) -> bool:
    return d.month == 12 and d.day == 31


def _norm(text: str) -> str:
    # NFKC so Urdu presentation forms, Arabic-Indic digits (۲۰۱۹ -> 2019)
    # and full-width ASCII all compare equal; casefold for the Latin half.
    text = unicodedata.normalize("NFKC", text or "")
    # NFKC does NOT fold Arabic-Indic / Extended Arabic-Indic digits to ASCII;
    # a year written as ۲۰۱۹ must still be a year.
    text = "".join(
        str(unicodedata.digit(ch)) if ch.isdigit() and not ch.isascii() else ch
        for ch in text
    )
    return " ".join(text.split()).casefold()


# ── Dates ──────────────────────────────────────────────────────────────────

# A year: four digits, 19xx/20xx, not part of a longer number or of a
# dotted/dashed/slashed compound ("12.2024", "2024-09-14", "2024/25"). A
# sentence-final "2020." is still a year — only a digit after the
# punctuation makes it a compound.
_Y = r"(?<![\d.\-/])((?:19|20)\d{2})(?!\d|[.\-/]\d)"

# Ranges: both bounds present. Order matters — tried before single bounds.
_DATE_RANGE_RES = (
    re.compile(rf"\bbetween\s+{_Y}\s+(?:and|&|to|-)\s+{_Y}"),
    re.compile(rf"\bfrom\s+{_Y}\s+(?:to|till|until|through|-)\s+{_Y}"),
    re.compile(rf"{_Y}\s*(?:to|-|–|—)\s*{_Y}"),
    # Roman-Urdu: "2024 aur 2025 ke darmiyan", "2024 se 2025 tak"
    re.compile(rf"{_Y}\s+(?:aur|or)\s+{_Y}\s+ke\s+darmiy?an"),
    re.compile(rf"{_Y}\s+(?:se|say)\s+{_Y}\s+tak"),
    # Urdu: "2024 اور 2025 کے درمیان", "2024 سے 2025 تک"
    re.compile(rf"{_Y}\s+اور\s+{_Y}\s+کے\s+درمیان"),
    re.compile(rf"{_Y}\s+سے\s+{_Y}\s+تک"),
)

# Upper bounds. (inclusive, pattern) — exclusive "before 2020" is 2019-12-31.
_DATE_TO_RES = (
    (True, re.compile(rf"{_Y}\s+(?:or|and)\s+(?:earlier|before|prior|older)\b")),
    (True, re.compile(rf"\b(?:up\s+to|until|till|through|no\s+later\s+than|on\s+or\s+before)\s+(?:the\s+end\s+of\s+)?{_Y}")),
    (True, re.compile(rf"\b(?:in|during)\s+or\s+before\s+{_Y}")),
    (False, re.compile(rf"\b(?:before|prior\s+to|earlier\s+than|pre)\s*-?\s*{_Y}")),
    # Roman-Urdu: "2019 ya us se pehle", "2019 ya is se pehle", "2019 tak",
    # "2020 se pehle" (exclusive)
    (True, re.compile(rf"{_Y}\s+ya\s+(?:us|is)\s+se\s+(?:pehle|pehlay|pehlay|qabal)")),
    (True, re.compile(rf"{_Y}\s+tak\b")),
    (False, re.compile(rf"{_Y}\s+se\s+(?:pehle|pehlay|qabal)")),
    # Urdu: "2019 یا اس سے پہلے", "2019 تک", "2020 سے پہلے" (exclusive)
    (True, re.compile(rf"{_Y}\s+یا\s+اس\s+سے\s+(?:پہلے|قبل)")),
    (True, re.compile(rf"{_Y}\s+تک\b")),
    (False, re.compile(rf"{_Y}\s+سے\s+(?:پہلے|قبل)")),
)

# Lower bounds. (inclusive, pattern) — exclusive "after 2023" is 2024-01-01.
_DATE_FROM_RES = (
    (True, re.compile(rf"{_Y}\s+(?:or|and)\s+(?:later|after|onwards?|newer)\b")),
    (True, re.compile(rf"\b(?:since|from|starting|as\s+of|on\s+or\s+after)\s+(?:the\s+start\s+of\s+)?{_Y}")),
    (False, re.compile(rf"\b(?:after|later\s+than|post)\s*-?\s*{_Y}")),
    # Roman-Urdu: "2024 ya us ke baad", "2024 se ab tak", "2024 se le kar",
    # "2023 ke baad" (exclusive)
    (True, re.compile(rf"{_Y}\s+ya\s+(?:us|is)\s+ke\s+ba+d")),
    (True, re.compile(rf"{_Y}\s+se\s+(?:ab\s+tak|le\s*kar|lekar|aaj\s+tak)")),
    (False, re.compile(rf"{_Y}\s+ke\s+ba+d")),
    # Urdu: "2024 یا اس کے بعد", "2024 سے اب تک", "2023 کے بعد" (exclusive)
    (True, re.compile(rf"{_Y}\s+یا\s+اس\s+کے\s+بعد")),
    (True, re.compile(rf"{_Y}\s+سے\s+(?:اب\s+تک|لے\s+کر)")),
    (False, re.compile(rf"{_Y}\s+کے\s+بعد")),
)

# A single bounded year: "in 2024", "during 2024", "2024 mein", "2024 میں",
# "of 2024", "for 2024". A BARE four-digit number is never a filter.
_DATE_YEAR_RES = (
    re.compile(rf"\b(?:in|during|for|of|within|throughout)\s+(?:the\s+year\s+)?{_Y}"),
    re.compile(rf"\b(?:year|saal|سال)\s+{_Y}"),
    re.compile(rf"{_Y}\s+(?:mein|main|me|mai|may|mn|ما|میں|مين)\b"),
    re.compile(rf"{_Y}\s+(?:ke\s+dauran|کے\s+دوران)"),
)

_ANY_YEAR = re.compile(_Y)


def _extract_date(text: str) -> tuple[Optional[date], Optional[date]]:
    for pat in _DATE_RANGE_RES:
        m = pat.search(text)
        if m:
            y1, y2 = int(m.group(1)), int(m.group(2))
            if y1 > y2:
                y1, y2 = y2, y1
            return date(y1, 1, 1), date(y2, 12, 31)

    date_to: Optional[date] = None
    date_from: Optional[date] = None
    for inclusive, pat in _DATE_TO_RES:
        m = pat.search(text)
        if m:
            y = int(m.group(1))
            date_to = date(y, 12, 31) if inclusive else date(y - 1, 12, 31)
            break
    for inclusive, pat in _DATE_FROM_RES:
        m = pat.search(text)
        if m:
            y = int(m.group(1))
            date_from = date(y, 1, 1) if inclusive else date(y + 1, 1, 1)
            break
    if date_to or date_from:
        if date_from and date_to and date_from > date_to:
            return None, None
        return date_from, date_to

    # Single bounded year. Two DIFFERENT bounded years with no range
    # connector is a comparison ("in 2026 ... in 2024"), not a filter.
    years: list[int] = []
    for pat in _DATE_YEAR_RES:
        for m in pat.finditer(text):
            years.append(int(m.group(1)))
    distinct = sorted(set(years))
    if len(distinct) == 1:
        y = distinct[0]
        return date(y, 1, 1), date(y, 12, 31)
    return None, None


# ── Ages ───────────────────────────────────────────────────────────────────

_AGE_CUES = (
    "aged", "age", "years old", "year old", "year-old", "years of age",
    "umar", "umr", "saal", "sal ", "baras",
    "عمر", "سال", "برس",
)
_N = r"(?<!\d)(\d{1,3})(?!\d)"

_AGE_RANGE_RES = (
    re.compile(rf"\b(?:aged?|ages?)\s+(?:between\s+)?{_N}\s*(?:to|and|-|–|—|through)\s*{_N}"),
    re.compile(rf"\bbetween\s+(?:the\s+ages?\s+(?:of\s+)?)?{_N}\s+and\s+{_N}(?:\s+years?)?"),
    re.compile(rf"{_N}\s*(?:to|-|–|—)\s*{_N}\s*(?:years?\s*(?:old|of\s+age)|yrs?|saal|sal|baras|سال|برس)"),
    # Roman-Urdu / Urdu: "25 se 40 saal", "umar 25 se 40", "25 سے 40 سال"
    re.compile(rf"{_N}\s+(?:se|say|سے)\s+{_N}\s*(?:saal|sal|baras|سال|برس)?"),
)
# (inclusive, pattern) for max/min single bounds
_AGE_MAX_RES = (
    (True, re.compile(rf"{_N}\s+(?:or|and)\s+(?:younger|under|below|less)\b")),
    (True, re.compile(rf"\b(?:up\s+to|at\s+most|no\s+older\s+than)\s+{_N}")),
    (False, re.compile(rf"\b(?:under|below|younger\s+than|less\s+than)\s+(?:the\s+age\s+of\s+)?{_N}")),
    (False, re.compile(rf"{_N}\s*(?:saal|sal|baras|سال|برس)?\s*(?:se|سے)\s+(?:kam|کم|neeche|نیچے)")),
    (True, re.compile(rf"{_N}\s*(?:saal|sal|baras|سال|برس)?\s*(?:tak|تک)\b")),
)
_AGE_MIN_RES = (
    (True, re.compile(rf"{_N}\s+(?:or|and)\s+(?:older|over|above|more)\b")),
    (True, re.compile(rf"\b(?:at\s+least|no\s+younger\s+than)\s+{_N}")),
    (False, re.compile(rf"\b(?:over|above|older\s+than|more\s+than)\s+(?:the\s+age\s+of\s+)?{_N}")),
    (True, re.compile(rf"{_N}\s*\+")),
    (False, re.compile(rf"{_N}\s*(?:saal|sal|baras|سال|برس)?\s*(?:se|سے)\s+(?:zyada|ziada|zaida|زیادہ|oopar|اوپر|bara|بڑ)")),
)


def _plausible_age(n: int) -> bool:
    return 0 < n < 120


def _extract_age(text: str) -> tuple[Optional[int], Optional[int]]:
    if not any(cue in text for cue in _AGE_CUES):
        return None, None
    for pat in _AGE_RANGE_RES:
        m = pat.search(text)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if _plausible_age(lo) and _plausible_age(hi):
                if lo > hi:
                    lo, hi = hi, lo
                return lo, hi
    age_max: Optional[int] = None
    age_min: Optional[int] = None
    for inclusive, pat in _AGE_MAX_RES:
        m = pat.search(text)
        if m and _plausible_age(int(m.group(1))):
            n = int(m.group(1))
            age_max = n if inclusive else n - 1
            break
    for inclusive, pat in _AGE_MIN_RES:
        m = pat.search(text)
        if m and _plausible_age(int(m.group(1))):
            n = int(m.group(1))
            age_min = n if inclusive else n + 1
            break
    if age_min is not None and age_max is not None and age_min > age_max:
        return None, None
    return age_min, age_max


# ── Sections ───────────────────────────────────────────────────────────────
#
# The same shapes Module 76's `_section_code_in_query()` trusts, plus the
# reversed "302 PPC" / "u/s 302" forms. A four-digit code is rejected: no
# section in any act this corpus cites has four digits, and "PECA 2016" /
# "CNSA 1997" would otherwise read as sections 2016 / 1997.
_SECTION_CODE = r"([0-9]{1,3}(?:-?[a-z]\([a-z]+\)|\([a-z]\)|-[a-z])?)"
_SECTION_RES = (
    re.compile(
        r"\b(ppc|pakistan penal code|crpc|cnsa|peca|arms ordinance|tazirat)\b"
        r"[^0-9a-z]{0,12}(?:section|sec\.?|s\.?|§|ki\s+dafa|dafa)?\s*" + _SECTION_CODE + r"(?![0-9])"
    ),
    re.compile(r"\b(?:section|sec\.|u/s|§)\s*" + _SECTION_CODE + r"(?![0-9])"),
    re.compile(r"(?:دفعہ|دفعات|dafa|dafaat)\s*" + _SECTION_CODE + r"(?![0-9])"),
    re.compile(_SECTION_CODE + r"\s*(?:ppc|crpc|cnsa|peca|تعزیرات|ت\.پ)\b"),
)


def _extract_section(text: str) -> Optional[str]:
    for pat in _SECTION_RES:
        m = pat.search(text)
        if m:
            code = m.group(m.lastindex).strip()
            if code.isdigit() and len(code) >= 4:
                continue
            return code
    return None


# ── Districts ──────────────────────────────────────────────────────────────

# Longest alias first so "karachi central" wins over "karachi".
_DISTRICT_ALIAS_ORDER = sorted(DISTRICT_NAME_ALIASES, key=len, reverse=True)


def _extract_district(text: str) -> tuple[Optional[str], tuple[str, ...]]:
    for alias in _DISTRICT_ALIAS_ORDER:
        # Whole-word for Latin aliases; substring is fine for Urdu (no
        # \b semantics for Arabic script in `re`, and no Urdu district
        # name here is a substring of another word in practice).
        if alias.isascii():
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text):
                return alias, DISTRICT_NAME_ALIASES[alias]
        elif alias in text:
            return alias, DISTRICT_NAME_ALIASES[alias]
    return None, ()


# ── Public entry point ─────────────────────────────────────────────────────

def extract_aggregate_filters(query_text: str) -> AggregateFilters:
    """Question text -> the filters it carries. Pure, deterministic,
    conservative; `EMPTY_FILTERS` when nothing bound was found."""
    text = _norm(query_text)
    if not text:
        return EMPTY_FILTERS
    date_from, date_to = _extract_date(text)
    age_min, age_max = _extract_age(text)
    section = _extract_section(text)
    district, districts = _extract_district(text)
    return AggregateFilters(
        date_from=date_from, date_to=date_to,
        age_min=age_min, age_max=age_max,
        section=section, district=district, districts=districts,
    )
