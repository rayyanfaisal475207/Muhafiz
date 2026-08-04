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
_ENGLISH_STOPWORDS = {
    "FIR", "PPC", "PECA", "CNIC", "Section", "Sections", "Police", "Station",
    "Report", "Case", "Diary", "Statement", "Witness", "Recovery", "Memo",
}
_ENGLISH_NAME_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b")

# Minimal gazetteer of Islamabad-area place-name tokens seen in this
# corpus, used only as a location confidence booster — the تھانہ-prefixed
# pattern above is the primary, generalizable location signal.
_LOCATION_GAZETTEER = {
    "اسلام آباد", "مارگلہ", "ترنول", "رمنہ", "گولڑہ", "شالیمار", "کوہسار",
    "سبزی منڈی", "بارہ کہو", "نیلور", "آبپارہ", "سیکرٹریٹ", "شہزاد ٹاؤن",
    "صنعتی علاقہ",
}


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
    if any(w in _STOPWORDS for w, _, _ in word_spans):
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

    for m in _ENGLISH_NAME_RE.finditer(norm):
        candidate = m.group(0)
        if candidate.split()[0] in _ENGLISH_STOPWORDS:
            continue
        out.append(NERMention(candidate, "person", m.start(), m.end(), 0.45, "statistical", source_chunk_id))

    return _dedupe_overlaps([m for m in out if m.text])


async def _adjudicate_low_confidence(
    text: str, candidates: list[NERMention]
) -> list[NERMention]:
    """
    Batch every low-confidence candidate from one document into a single
    Qwen3-14B call. On any failure, returns the candidates unchanged
    (still tagged "statistical", low confidence) rather than dropping them
    — a failed adjudication call should degrade to "unresolved," not
    silently delete evidence.
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
        return candidates

    parsed = parse_json_response(response, context="ner_fallback")
    if not isinstance(parsed, list):
        return candidates

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
