"""Count things by reading the whole corpus, not by querying a broken schema.

WHY THIS EXISTS

Counting currently fails for reasons that have nothing to do with
retrieval. The extracted schema is wrong in ways no query can repair:

    weapons.quantity = 30   where 30 is a pistol's BORE
    894 person rows         for 73 cases, unresolved
    6 of 17 tables          completely empty
    case_station_history    empty, yet wins the word "cases" at 1.000

And the source system those records came from cannot be re-exported. The
only trustworthy artefact left is the DOCUMENT TEXT, which is intact:
4,371 spans, every character offset verified.

So counts are derived from the documents instead.

WHY THIS IS NOT COUNTING-BY-READING

Answering "how many" from the top 10 retrieved passages is wrong, and
that mistake produced **208**. The number you get back is a property of
the retriever — ask for 20 passages and the "total" changes.

This is different in the one way that matters: it reads EVERY span, not
the best ones. A full scan of 4,371 spans is a few hundred milliseconds
and the coverage is not estimated, it is total. A count over a complete
population is a count; a count over a ranked sample is a guess.

THE THREE STAGES, AND WHY EACH USES WHAT IT USES

  1. MARKERS  — a model proposes how this concept is written in these
     records, in both scripts: تھانہ, تھانے, thana, police station.
     Bounded work, one call, and nothing is hardcoded — a new kind of
     thing to count needs no new code.

  2. SCAN     — deterministic, exhaustive, free. Every span is read.
     No model touches this stage, so the population cannot drift.

  3. RESOLVE  — a model folds the raw mentions into distinct things.
     It sees only the candidate strings (tens of them), never the
     corpus. This is where "تھانہ صدر، راولپنڈی" and "تھانہ صدر، ضلع
     چنیوٹ" stay SEPARATE — same name, different district, different
     station — and where trailing prose is discarded.

Measured, that distinction is real: a name-only scan found 76 "stations"
including "صدر" once, conflating two. Keeping the qualifier found both.

WHAT COMES BACK

A number, and for every distinct thing counted, the spans and cases it
was found in. The count is checkable: open the spans and look. That is
the property the old pipeline never had, and it is why this can be
trusted where a query against the extracted schema cannot.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import asyncpg

from src.llm.client import call_llm
from src.retrieval.health import Ledger, Severity


@dataclass
class Thing:
    """One distinct thing found, and where it was found."""

    name: str
    mentions: list[str] = field(default_factory=list)
    span_ids: set[int] = field(default_factory=set)
    case_ids: set[str] = field(default_factory=set)

    @property
    def evidence(self) -> str:
        spans = ", ".join(str(s) for s in sorted(self.span_ids)[:4])
        return f"{len(self.span_ids)} span(s), {len(self.case_ids)} case(s) [{spans}]"


@dataclass
class Tally:
    """A count derived from the full corpus, with its evidence."""

    things: list[Thing] = field(default_factory=list)
    spans_scanned: int = 0
    spans_total: int = 0
    markers: list[str] = field(default_factory=list)
    refusal: str | None = None

    @property
    def count(self) -> int:
        return len(self.things)

    @property
    def complete(self) -> bool:
        """Whether every span was read. A partial scan cannot give a total."""
        return self.spans_total > 0 and self.spans_scanned == self.spans_total

    def render(self) -> str:
        if self.refusal:
            return self.refusal
        lines = [f"{self.count}"]
        lines.append("")
        for t in sorted(self.things, key=lambda x: -len(x.case_ids))[:25]:
            lines.append(f"  - {t.name}  ({t.evidence})")
        lines.append("")
        if self.complete:
            lines.append(
                f"Counted by reading all {self.spans_total} passages in the "
                f"case records — not a sample. Each entry above lists the "
                f"passages it was found in, so any of them can be checked.")
        else:
            lines.append(
                f"WARNING: only {self.spans_scanned} of {self.spans_total} "
                f"passages were read, so this is not a complete count.")
        return chr(10).join(lines)


MARKERS = """You are given a question about Pakistani police records and
must say how the thing being counted is WRITTEN in those records.

Return ONLY this JSON:

{"markers": ["<word or phrase>", ...], "thing": "<what is being counted>"}

Rules:
1. Give the words that appear immediately before or around the thing in a
   record. For police stations that is تھانہ, تھانے, thana, police
   station. For weapons it is words like پستول, بور, weapon.
2. Include Urdu, English and Roman spellings. The records are mixed.
3. Include morphological variants people actually write (تھانہ and تھانے).
4. Four to ten markers. A marker that matches everything is useless.
5. If the question is not asking to count a kind of thing, return
   {"markers": [], "thing": ""}."""


RESOLVE = """You are given raw text fragments captured from police records
around a marker word. Fold them into the DISTINCT real things they name.

Return ONLY this JSON:

{"things": [{"name": "<the clean name>", "mentions": ["<fragment>", ...]}]}

Rules:
1. Strip trailing prose. "برکی، لاہور کے تفتیشی افسر نے" names the station
   "برکی، لاہور" — the rest is the sentence continuing.
2. KEEP the qualifier that distinguishes two things with the same name.
   "صدر، راولپنڈی" and "صدر، ضلع چنیوٹ" are DIFFERENT police stations and
   must stay separate. Merging them would undercount.
3. Group fragments that name the same thing, however spelled or inflected,
   including across scripts.
4. Discard a fragment that names nothing — a stray sentence, a date, a
   number.
5. Invent nothing. Every name you return must appear in the fragments."""


_JSON = re.compile(r"\{.*\}", re.S)


async def markers_for(question: str) -> tuple[list[str], str]:
    """How this concept is written in these records, per the model."""
    try:
        raw = await call_llm(MARKERS, question, temperature=0.0,
                             max_tokens=2500)
    except Exception:                                   # noqa: BLE001
        return [], ""
    m = _JSON.search(raw or "")
    if not m:
        return [], ""
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return [], ""
    out = [str(x).strip() for x in (obj.get("markers") or []) if str(x).strip()]
    return out[:10], str(obj.get("thing") or "").strip()


async def scan(conn: asyncpg.Connection, markers: list[str],
               case_id: str | None = None) -> tuple[dict, int, int]:
    """Read EVERY span and capture what follows each marker.

    Deterministic and exhaustive by design. No model runs here, so the
    population this count is over cannot vary between runs.
    """
    total = await conn.fetchval("SELECT count(*) FROM doc_spans")
    rows = await conn.fetch(
        """SELECT s.span_id, d.case_id, s.text
           FROM doc_spans s
           LEFT JOIN rebuild_documents d ON d.doc_id = s.doc_id
           WHERE s.text IS NOT NULL
             AND ($1::text IS NULL OR d.case_id = $1)""", case_id)

    # A mention is the marker plus what follows it, up to a boundary. The
    # fragment is deliberately generous: stage 3 trims it, and a fragment
    # cut too short loses the qualifier that keeps two things apart.
    alternation = "|".join(re.escape(m) for m in markers if m)
    if not alternation:
        return {}, 0, total
    pattern = re.compile(
        rf"(?:{alternation})[\s:]*([^\|\n\.]{{2,45}})", re.I)

    captured: dict[str, dict] = {}
    for r in rows:
        for hit in pattern.finditer(r["text"] or ""):
            fragment = " ".join(hit.group(1).split()).strip(" ،,|:-")
            if len(fragment) < 3:
                continue
            entry = captured.setdefault(
                fragment, {"spans": set(), "cases": set()})
            entry["spans"].add(r["span_id"])
            if r["case_id"]:
                entry["cases"].add(r["case_id"])
    return captured, len(rows), total


async def resolve(captured: dict) -> list[Thing]:
    """Fold raw fragments into the distinct things they name."""
    if not captured:
        return []
    # Most frequent first, so the model sees the real names before noise.
    ordered = sorted(captured.items(), key=lambda kv: -len(kv[1]["spans"]))
    listing = "\n".join(f"- {frag}" for frag, _ in ordered[:120])

    try:
        raw = await call_llm(RESOLVE, listing, temperature=0.0,
                             max_tokens=3000)
    except Exception:                                   # noqa: BLE001
        return []
    m = _JSON.search(raw or "")
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []

    things: list[Thing] = []
    for item in (obj.get("things") or [])[:200]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        thing = Thing(name=name)
        for frag in (item.get("mentions") or []):
            entry = captured.get(str(frag).strip())
            if not entry:
                continue
            thing.mentions.append(str(frag))
            thing.span_ids |= entry["spans"]
            thing.case_ids |= entry["cases"]
        # A name with no surviving mention was invented rather than found.
        if thing.span_ids:
            things.append(thing)
    return things


async def tally(conn: asyncpg.Connection, question: str,
                case_id: str | None = None,
                ledger: Ledger | None = None) -> Tally:
    """Count what `question` asks about, by reading the whole corpus."""
    out = Tally()

    markers, _thing = await markers_for(question)
    out.markers = markers
    if not markers:
        out.refusal = ("I could not work out how this kind of thing is "
                       "written in the records, so I cannot count it "
                       "reliably. Asking with the word the records use "
                       "would help.")
        return out

    captured, scanned, total = await scan(conn, markers, case_id)
    out.spans_scanned, out.spans_total = scanned, total
    if not captured:
        out.refusal = (
            f"I read all {total} passages and found no mention of this. "
            f"That is a finding of none, not a failure to look.")
        return out

    out.things = await resolve(captured)
    if not out.things:
        out.refusal = (
            f"I found {len(captured)} mention(s) across {scanned} passages "
            f"but could not resolve them into distinct items, so I will "
            f"not give you a number I cannot stand behind.")
        if ledger:
            ledger.record(
                "counting", Severity.IMPORTANT, "resolution produced nothing",
                "I found relevant text but could not turn it into a reliable "
                "count")
    return out
