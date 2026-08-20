# ============================================================
# Generic NER — person / location / organization (Phase 4.5)
#
# Statistical pass (regex + gazetteer over Phase 2.2's tokenizer/
# normalization), Qwen3-14B fallback on LOW-CONFIDENCE SPANS ONLY — not a
# second independent full-document pass. This keeps the LLM cost
# proportional to genuine ambiguity rather than every mention in the corpus.
#
# Why regex/gazetteer instead of Stanza, even though tokenizer.py's own
# decision note leaves the door open for NER to reconsider it: Stanza has
# no published Urdu NER processor (its Urdu model covers tokenize/pos/
# lemma/depparse, not NER), so adopting it here would mean a new heavy
# dependency (torch) for a capability it doesn't actually provide for this
# language — the cost/benefit tokenizer.py weighed for tokenization doesn't
# carry over. Statistical here means pattern-and-frequency-based, not a
# trained statistical model; this corpus's structural regularities (the
# "X ولد Y" kinship formula, role-marker-adjacent names, "تھانہ X" station
# names) are strong, cheap signals that a trained NER model would also
# ultimately be leaning on as features.
#
# Known limitation, stated plainly: this pass finds CANDIDATE spans via
# pattern coverage — it does not catch a person/place/org mentioned with
# none of these structural cues nearby (a bare name with no role marker,
# no kinship formula, no gazetteer hit). That is a real recall ceiling,
# not a hidden one; the LLM fallback adjudicates candidates the statistical
# pass IS uncertain about, it does not backstop candidates the statistical
# pass never found in the first place. Revisit if eval shows recall
# lagging (same posture tokenizer.py's own decision note takes).
# ============================================================

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.extraction.llm_json import parse_json_response
from src.ingestion.text_normalizer import normalize_urdu
from src.ingestion.tokenizer import _URDU_LETTERS
from src.llm.client import call_llm

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "ner_fallback.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

ENTITY_TYPES = ("person", "location", "organization")

LOW_CONFIDENCE_THRESHOLD = 0.6


@dataclass
class NERMention:
    text: str
    type: str            # "person" | "location" | "organization"
    start: int
    end: int
    confidence: float
    method: str            # "statistical" | "llm_fallback"
    source_chunk_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "type": self.type,
            "char_span": [self.start, self.end],
            "source_chunk_id": self.source_chunk_id,
            "confidence": self.confidence,
            # Uniform with domain_entities.py's mention shape (4.6) — empty
            # here since NER doesn't extract type-specific attributes;
            # structured_fields.py/entity_resolution.py attach CNIC etc.
            "attributes": {},
        }


# ── Statistical pass: patterns ────────────────────────────────────────────

# A "name run" — 1 to 4 space-separated Urdu word tokens, used as the
# capture unit inside every pattern below. Built on tokenizer.py's own
# _URDU_LETTERS (imported, not re-transcribed — a hand-copied Unicode
# range class is exactly the kind of thing that silently goes wrong).
# Deliberately not the full tokenizer.py _WORD regex (which also matches
# digits/identifiers) — names are letter runs only.
_URDU_WORD = rf"[{_URDU_LETTERS}]+"
_NAME_RUN = rf"{_URDU_WORD}(?:\s+{_URDU_WORD}){{0,3}}"
# Shorter cap for place names (1-2 words covers every station name in this
# corpus) — a 4-word cap over-captures the "تھانہ X کی حدود میں" boilerplate
# ("within the jurisdiction of station X") that routinely follows a station
# mention, since "حدود" (jurisdiction/boundary) is a real word, not a
# stopword, and trimming only removes stopwords.
_SHORT_NAME_RUN = rf"{_URDU_WORD}(?:\s+{_URDU_WORD}){{0,1}}"

# Common Urdu function words/copulas/labels (ہے/تھا/کا/نے/نام...) are
# themselves runs of Urdu letters, so they match _URDU_WORD like any name
# token — a greedy multi-word name run has no way to know "کا نام عمران"
# should start at "عمران", or "غلام ستار ہے" should stop before "ہے",
# without an explicit list. Rather than fight this with regex lookahead
# tricks (a mid-name space and an end-of-name space are indistinguishable
# to a lookahead), captured name runs are trimmed of leading/trailing
# stopwords in _trim_and_reposition() below, after the match — simpler to
# reason about and to test.
_STOPWORDS = {
    "ہے", "ہیں", "تھا", "تھی", "تھے", "کا", "کی", "کے", "نے", "میں",
    "کو", "سے", "پر", "اور", "یا", "کیا", "گیا", "گئی", "لیا", "دیا",
    "ہو", "ہوں", "کر", "بھی", "تو", "اس", "یہ", "وہ", "نام",
}

# "<name> ولد/بنت <father name>" — the standard Urdu kinship formula in FIRs
# and CNIC-adjacent identification. Unambiguous structural cue, so this is
# the highest-confidence pattern in the module.
#
# Every marker literal below (ولد/بنت/role markers/تھانہ/گروہ) is wrapped in
# \b — Python's `re` treats Arabic-script letters as \w characters, so \b
# works correctly here, and without it a marker matches as a bare substring
# of an unrelated longer word (e.g. "ملزم" inside "ملزمان", the plural
# "accused persons" — caught by the corpus smoke test, not a hypothetical).
_KINSHIP_RE = re.compile(
    rf"(?P<child>{_NAME_RUN})\s+\b(?:ولد|بنت)\b\s+(?P<parent>{_NAME_RUN})(?=[\s،۔,\.]|$)"
)

# Role marker immediately before a name — "مدعی X", "ملزم X", "گواہ X".
# "شاہد" (Shahid) deliberately excluded despite also meaning "witness" —
# it's a common Urdu given name too, and using it as a role marker would
# misfire on every person actually named Shahid.
_ROLE_MARKERS = ("مدعی", "ملزم", "گواہ", "مشیر")
_ROLE_RE = re.compile(
    rf"\b(?:{'|'.join(_ROLE_MARKERS)})\b\s*(?:کا نام)?\s*[:：]?\s*(?P<name>{_NAME_RUN})(?=[\s،۔,\.]|$)"
)

# "میں <name>، رہائشی/تھانہ .../رہنے والا/کا رہائشی ہوں" — the standard
# first-person self-introduction a complainant/witness opens their
# statement with (the corpus's single most common construction for
# naming the reporting party, per the audit — 11+ ground-truth examples,
# e.g. "میں فیصل شہزاد قریشی، رہائشی ترنول، تھانہ ترنول کا رہائشی ہوں۔",
# "میں، محمد علی، سیکٹر ایچ ڈیبلیو ایچ ایس، اسلام آباد کا رہائشی ہوں۔").
# Unlike _KINSHIP_RE/_ROLE_RE's distinctive markers, "میں <name>" alone is
# not distinctive enough on its own — an ordinary "میں نے ..." ("I did
# ...") action sentence would misfire — so this requires "رہائشی" or
# "رہنے والا" (resident-of) to appear later in the SAME sentence (bounded
# lookahead, stopped at the next ۔) as the disambiguating structural cue,
# not just "میں" by itself.
_SELF_INTRO_RE = re.compile(
    rf"\bمیں\b\s*،?\s*(?P<name>{_NAME_RUN})\s*،?\s*"
    r"(?=[^۔]{0,60}(?:رہائشی|رہنے\s*والا))"
)

# "تھانہ <station name>" — police station, the standard reference form.
_STATION_RE = re.compile(rf"\bتھانہ\b\s+(?P<name>{_SHORT_NAME_RUN})(?=[\s،۔,\.]|$)")

# "<name> گروہ" / "<name> نیٹ ورک" — gang/group/network naming pattern.
_ORG_SUFFIX_RE = re.compile(rf"(?P<name>{_NAME_RUN})\s*\b(?:گروہ|نیٹ\s*ورک)\b(?=[\s،۔,\.]|$)")

# Weak fallback for English-language documents: capitalized-word runs,
# excluding common structural tokens that are never names.
#
# Extended after live-tracing a real bug (root-caused, not the community-
# detection-side filter that was papering over this): one document
# rendering tier ("complex"/"retrofitted" per data/memory/case_index.csv —
# every FIR/case-diary/charge-sheet in the CASE-B0-* batch) is an entirely
# English, numbered form with colon-terminated field labels ("Police
# Station:", "Entry Dates:", "Action Taken:", "Progress Summary:") rather
# than free narrative prose — a document shape the original stopword list
# was never tuned against (that was built from MP-2026-001.pdf/
# TAR-2026-001.pdf's narrative style per the 2026-08-04 audit). Every one
# of these labels is a capitalized-word run with no structural cue telling
# _ENGLISH_NAME_RE it isn't a name — confirmed live: extracting
# FIR-2026-THEFT-001.pdf/CASEDIARY-FIR-2026-THEFT-001-01.pdf/
# CHARGESHEET-FIR-2026-THEFT-001.pdf directly showed dozens of these
# labels shipping through as low-confidence "person" candidates, most
# surviving to the graph because _adjudicate_low_confidence's own
# documented fail-open behavior (return candidates unchanged on any LLM
# failure, "a failed adjudication call should degrade to unresolved, not
# silently delete evidence") means a struggling adjudication call for a
# document with this many low-confidence candidates ships them all through
# unfiltered rather than dropping them.
_ENGLISH_STOPWORDS = {
    "FIR", "PPC", "PECA", "CNIC", "Section", "Sections", "Police", "Station",
    "Report", "Case", "Diary", "Statement", "Witness", "Recovery", "Memo",
    # Form-label vocabulary directly observed in the CASE-B0-* batch above —
    # a secondary, explicit defense alongside the structural colon-adjacency
    # check below (some labels, e.g. "General Diary (Roznamcha) reference
    # and number:", don't have the label word immediately before the colon,
    # so the structural check alone doesn't catch every instance).
    "Date", "Dates", "Day", "Total", "Name", "Place", "Street", "District",
    "Document", "Description", "Identity", "Reasons", "Delay", "Related",
    "Submitted", "Entry", "Roznamcha", "Action", "Taken", "Progress",
    "Summary", "Current", "Status", "Investigation", "Investigating",
    "Officer", "Complainant", "Signature", "Thumb", "Impression",
    "Judicial", "Magistrate", "Under", "House", "General", "Court",
    "Property", "Disposal", "Remarks", "Recommendation", "Sections",
    "Whether", "List", "Prosecution", "Class", "Number",
    # Crime-category vocabulary and an English rendering of the station
    # name bleeding into the "person" channel — a real name-typed mistag
    # (should ideally be "location"/dropped entirely, not just suppressed
    # from "person"), but suppressing it here is an honest partial fix,
    # not a claim this also fixes the location-mistyping gap.
    "Bhara", "Kahu", "Vehicle", "Theft", "Mobile", "Unknown",
}
_ENGLISH_NAME_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b")

# Structural exclusion (the actual root-cause fix, generalizes past any
# specific label ever seen): a capitalized-word run immediately followed
# by a colon (optionally one space between) is a form-field LABEL, not a
# name — "Police Station:", "Entry Dates:", "Action Taken:" all match this
# shape regardless of what the label text is, the same way the Urdu side's
# patterns key off a structural marker (ولد/تھانہ/role words) rather than
# an enumerable name list. A real name is never immediately followed by a
# colon in this corpus's usage (it's followed by a comma, a kinship
# marker, or a sentence boundary).
_LABEL_COLON_RE = re.compile(r"\s?:")

# English-side location/org structural cues (B-4). The Urdu side has
# _STATION_RE/_ORG_SUFFIX_RE/the gazetteer above proposing a non-"person"
# type from structure alone; the English side had nothing equivalent —
# EVERY capitalized-word run (_ENGLISH_NAME_RE above) proposed "person"
# regardless of content, relying 100% on the LLM fallback to correct it,
# which the audit found has a silent-failure mode (an LLM-call exception
# lets the mistagged "person" candidate through unchanged). Mirrors the
# Urdu side's approach: a suffix/keyword list, not a gazetteer of specific
# place names, so it generalizes past the two documents the audit sampled.
_ENGLISH_LOCATION_SUFFIXES = (
    "Highway", "Road", "Street", "Avenue", "Chowk", "Town", "Sector",
    "Colony", "Bazaar", "Bazar", "Markaz",
)
_ENGLISH_LOCATION_RE = re.compile(
    rf"\b(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){{0,3}}\s+"
    rf"(?:{'|'.join(_ENGLISH_LOCATION_SUFFIXES)}|Police\s+Station))\b"
)

# "<Name...> Police" NOT followed by "Station" — an organization/unit
# name ("Islamabad Traffic Police"), distinct from "<Name> Police
# Station" above, which is a place. Also matches a trailing parenthetical
# all-caps abbreviation ("Islamabad Traffic Police (ITP)") as one unit
# when present.
_ENGLISH_ORG_RE = re.compile(
    r"\b(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\s+Police)\b"
    r"(?!\s+Station)(?:\s*\((?P<abbr>[A-Z]{2,6})\))?"
)

# Minimal gazetteer of Islamabad-area place-name tokens seen in this
# corpus, used only as a location confidence booster — the تھانہ-prefixed
# pattern above is the primary, generalizable location signal.
_LOCATION_GAZETTEER = {
    "اسلام آباد", "مارگلہ", "ترنول", "رمنہ", "گولڑہ", "شالیمار", "کوہسار",
    "سبزی منڈی", "بارہ کہو", "نیلور", "آبپارہ", "سیکرٹریٹ", "شہزاد ٹاؤن",
    "صنعتی علاقہ",
} | {
    # Extended for the Muhafiz Data API migration (M7, docs/decisions/
    # 0001-muhafiz-api-migration.md) — every real district and station
    # area-name token confirmed against the live dataset's 9 districts /
    # 19 stations, which span Lahore/Karachi/Rawalpindi/Faisalabad/
    # Hyderabad/Multan/Chiniot, not just Islamabad (the original list's
    # only city). Additive, same "confidence booster, not a hard
    # requirement" role as the set above — a real place name absent from
    # both sets still resolves via the تھانہ-prefixed pattern's own signal.
    "حیدر آباد", "راولپنڈی", "فیصل آباد", "لاہور", "ملتان", "چنیوٹ",
    "کراچی", "کراچی ایسٹ", "کراچی وسطی",
    "اقبال ٹاؤن", "برکی", "جھنگ روڈ", "راجہ بازار", "سول لائنز",
    "شاہ فیصل کالونی", "صدر", "لطیف آباد", "ماڈل ٹاؤن", "مدینہ ٹاؤن",
    "نیو کراچی", "وارث خان", "کوتوالی", "کینٹ",
}


# Legal/procedural content words that commonly follow a role marker
# ("ملزم", "مدعی"...) in a clause ABOUT the person rather than a name FOR
# them — "ملزم کے خلاف قانونی کارروائی" ("legal action against the
# accused") or "ملزم نے میرے گھر کا تالا توڑا" ("the accused broke my
# house's lock"), neither of which names anyone. Unlike _STOPWORDS (pure
# function words), these are real content words, so the leading/trailing
# trim above never removes them — live-caught in the real corpus
# (FIR-2026-BUR-009's ground truth) producing "خلاف قانونی کارروائی" and
# "میرے گھر" as bogus Person candidates. A real personal name never
# contains any of these, so their presence anywhere in the captured run
# (not just leading/trailing) is treated the same as a mid-run stopword
# below: reject the whole candidate.
_NON_NAME_CONTENT_WORDS = {"خلاف", "قانونی", "کارروائی", "گھر", "شناخت"}


def _trim_and_reposition(raw: str, group_start: int) -> tuple[str, int, int]:
    """
    Trim leading AND trailing stopwords from a captured name-run group,
    returning (trimmed_text, new_start, new_end) with offsets correctly
    recomputed into the same normalized text `group_start` was measured
    against — not just a `.strip()`, since dropping words shrinks the span
    from either end (a leading "کا نام" label, a trailing copula/particle).
    """
    word_spans = [(m.group(0), m.start(), m.end()) for m in re.finditer(_URDU_WORD, raw)]
    while word_spans and word_spans[-1][0] in _STOPWORDS:
        word_spans.pop()
    while word_spans and word_spans[0][0] in _STOPWORDS:
        word_spans.pop(0)
    # A stopword surviving in the MIDDLE of the trimmed run (not leading
    # or trailing) means the pattern actually matched a sentence fragment
    # describing the role marker's subject ("ملزم کی شناخت کی جائے" — "the
    # accused should be identified"), not a name — a real name never
    # contains a function word. Reject the whole candidate rather than
    # keep a contaminated partial match; the module favors precision over
    # forcing a salvage here.
    if any(w in _STOPWORDS or w in _NON_NAME_CONTENT_WORDS for w, _, _ in word_spans):
        return "", group_start, group_start
    if not word_spans:
        return "", group_start, group_start
    first_local_start = word_spans[0][1]
    last_local_end = word_spans[-1][2]
    return raw[first_local_start:last_local_end], group_start + first_local_start, group_start + last_local_end


def _dedupe_overlaps(mentions: list[NERMention]) -> list[NERMention]:
    """Where two candidates overlap, keep the higher-confidence one."""
    mentions = sorted(mentions, key=lambda m: (-m.confidence, m.start))
    kept: list[NERMention] = []
    for m in mentions:
        if any(not (m.end <= k.start or m.start >= k.end) for k in kept):
            continue
        kept.append(m)
    return sorted(kept, key=lambda m: m.start)


def extract_statistical(text: str, source_chunk_id: Optional[str] = None) -> list[NERMention]:
    """
    Regex/gazetteer candidate pass. Returns every candidate span found,
    confident and low-confidence alike — extract_entities() is the entry
    point that also runs the LLM fallback on the low-confidence ones.
    """
    if not text or not text.strip():
        return []

    norm = normalize_urdu(text)
    out: list[NERMention] = []

    for m in _KINSHIP_RE.finditer(norm):
        child, cs, ce = _trim_and_reposition(m.group("child"), m.start("child"))
        if child:
            out.append(NERMention(child, "person", cs, ce, 0.85, "statistical", source_chunk_id))
        parent, ps, pe = _trim_and_reposition(m.group("parent"), m.start("parent"))
        if parent:
            out.append(NERMention(parent, "person", ps, pe, 0.8, "statistical", source_chunk_id))

    for m in _ROLE_RE.finditer(norm):
        name, ns, ne = _trim_and_reposition(m.group("name"), m.start("name"))
        if name:
            out.append(NERMention(name, "person", ns, ne, 0.75, "statistical", source_chunk_id))

    for m in _SELF_INTRO_RE.finditer(norm):
        name, ns, ne = _trim_and_reposition(m.group("name"), m.start("name"))
        # Below LOW_CONFIDENCE_THRESHOLD deliberately: "میں <name>" is less
        # structurally distinctive than the kinship/role markers above (a
        # generic self-reference like "میں شہری مارگلہ کا رہائشی ہوں" — "I,
        # a resident of Margalla" — can match the same shape without a real
        # name present), so this always routes through LLM adjudication
        # rather than shipping unreviewed.
        if name:
            out.append(NERMention(name, "person", ns, ne, 0.55, "statistical", source_chunk_id))

    for m in _STATION_RE.finditer(norm):
        name, ns, ne = _trim_and_reposition(m.group("name"), m.start("name"))
        if name:
            out.append(NERMention(name, "location", ns, ne, 0.85, "statistical", source_chunk_id))

    for gazetteer_term in _LOCATION_GAZETTEER:
        idx = norm.find(gazetteer_term)
        while idx != -1:
            out.append(NERMention(gazetteer_term, "location", idx, idx + len(gazetteer_term), 0.8, "statistical", source_chunk_id))
            idx = norm.find(gazetteer_term, idx + 1)

    for m in _ORG_SUFFIX_RE.finditer(norm):
        name, ns, ne = _trim_and_reposition(m.group("name"), m.start("name"))
        if name:
            out.append(NERMention(name, "organization", ns, ne, 0.75, "statistical", source_chunk_id))

    for m in _ENGLISH_LOCATION_RE.finditer(norm):
        out.append(NERMention(m.group("name"), "location", m.start("name"), m.end("name"), 0.7, "statistical", source_chunk_id))

    for m in _ENGLISH_ORG_RE.finditer(norm):
        out.append(NERMention(m.group("name"), "organization", m.start("name"), m.end("name"), 0.7, "statistical", source_chunk_id))

    for m in _ENGLISH_NAME_RE.finditer(norm):
        candidate = m.group(0)
        words = candidate.split()
        # Single capitalized English word ("No", "Cr", "Islamabad",
        # "Honda", "Nil", "This") — live-confirmed this is overwhelmingly
        # noise in this corpus (boilerplate headers, page-break artifacts,
        # abbreviations, isolated form values), never a bare single-token
        # person name; every real English name sampled so far is 2-3 words
        # ("Irfan Mirza", "Inspector Fariha Saeed"). Same "single-token
        # filter" principle already validated for exactly this failure
        # class in src/graph/community_detection.py's node-level guard —
        # applied here at the source instead of only downstream.
        if len(words) < 2:
            continue
        # Any word in the candidate, not just the first — a real bug fixed
        # alongside the form-label additions above: "General Diary" was
        # never excluded despite "Diary" already being listed, because the
        # old check only looked at candidate.split()[0] ("General").
        if any(w in _ENGLISH_STOPWORDS for w in words):
            continue
        # Structural check: immediately followed by a colon = a form-field
        # label, not a name (see _LABEL_COLON_RE's comment above).
        if _LABEL_COLON_RE.match(norm, m.end()):
            continue
        out.append(NERMention(candidate, "person", m.start(), m.end(), 0.45, "statistical", source_chunk_id))

    return _dedupe_overlaps([m for m in out if m.text])


# Confidence floor a statistical-only candidate must clear to survive an
# ADJUDICATION FAILURE (LLM error or malformed response) unadjudicated.
# Closes the fail-open hole confirmed live during the Muhafiz Data API
# migration (M7, docs/decisions/0001-muhafiz-api-migration.md):
# previously EVERY low-confidence candidate — including
# _ENGLISH_NAME_RE's bare capitalized-word-run matches at confidence
# 0.45, the diagnosed root cause of English-language form labels
# ("Police Station:", "Entry Dates:") reaching the graph as fabricated
# Person nodes — passed through unchanged whenever the adjudication call
# itself failed, with no way for a caller to tell "the LLM confirmed
# this" from "the LLM never got a chance to look at this." Below this
# floor, a candidate is now dropped on failure rather than passed
# through: a candidate whose ONLY signal was a bare capitalization
# heuristic was never going to be trustworthy without the LLM's
# judgment, so losing it on failure is not a regression from "unresolved"
# to "deleted" the way it would be for a stronger candidate.
# _SELF_INTRO_RE's 0.55 candidates survive a failure (they clear this
# floor) — that pattern fires on genuine "میں <name>..."
# self-introduction structure, not a bare capitalization heuristic, so a
# failure there is closer to "the LLM was unavailable to confirm a
# plausible candidate" than "this was never going to be a name."
_ADJUDICATION_FAILURE_SURVIVAL_FLOOR = 0.50


def _degrade_on_adjudication_failure(candidates: list[NERMention]) -> list[NERMention]:
    return [c for c in candidates if c.confidence >= _ADJUDICATION_FAILURE_SURVIVAL_FLOOR]


async def _adjudicate_low_confidence(
    text: str, candidates: list[NERMention]
) -> list[NERMention]:
    """
    Batch every low-confidence candidate from one document into a single
    Qwen3-14B call. On failure, degrades via
    _degrade_on_adjudication_failure() — candidates that clear
    _ADJUDICATION_FAILURE_SURVIVAL_FLOOR pass through unchanged (a failed
    adjudication call should degrade to "unresolved," not silently delete
    real evidence); weaker candidates are dropped rather than flooding
    the graph with unreviewed low-confidence noise (see that function's
    own docstring for the diagnosed bug this closes).
    """
    if not candidates:
        return []

    candidate_lines = "\n".join(
        f'{i}: "{c.text}" (guessed type: {c.type})' for i, c in enumerate(candidates)
    )
    user_message = f"Passage: {text[:1500]}\n\nCandidates:\n{candidate_lines}"

    try:
        response = await call_llm(
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_message,
            temperature=0.0,
            max_tokens=800,
        )
    except Exception as exc:
        logger.warning("NER fallback LLM call failed: %s", exc)
        return _degrade_on_adjudication_failure(candidates)

    parsed = parse_json_response(response, context="ner_fallback")
    if not isinstance(parsed, list):
        return _degrade_on_adjudication_failure(candidates)

    out: list[NERMention] = []
    for item in parsed:
        idx = item.get("index")
        if idx is None or not (0 <= idx < len(candidates)):
            continue
        if not item.get("keep"):
            continue
        original = candidates[idx]
        entity_type = item.get("type") or original.type
        if entity_type not in ENTITY_TYPES:
            entity_type = original.type
        out.append(NERMention(
            original.text, entity_type, original.start, original.end,
            float(item.get("confidence", original.confidence) or original.confidence),
            "llm_fallback", original.source_chunk_id,
        ))
    return out


async def extract_entities(
    text: str,
    source_chunk_id: Optional[str] = None,
    low_confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD,
) -> list[dict]:
    """
    Full 4.5 pipeline: statistical pass, then Qwen3-14B adjudication of
    whichever candidates scored below `low_confidence_threshold`.

    Returns the uniform mention shape shared with domain_entities.py (4.6):
    {text, type, char_span, source_chunk_id, confidence}.
    """
    candidates = extract_statistical(text, source_chunk_id)
    confident = [c for c in candidates if c.confidence >= low_confidence_threshold]
    uncertain = [c for c in candidates if c.confidence < low_confidence_threshold]

    adjudicated = await _adjudicate_low_confidence(text, uncertain) if uncertain else []

    return [m.to_dict() for m in confident + adjudicated]
