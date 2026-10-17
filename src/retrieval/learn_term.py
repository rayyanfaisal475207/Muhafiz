"""When a word binds to nothing, look it up rather than give up.

THE LAYER THIS COMPLETES

Binding fails in two very different ways, and they look identical:

    the corpus has no such thing        -> the right answer is a refusal
    the system does not know the word   -> the right answer is to find out

Treating the second as the first is how a system built for one language
stays built for one language. A question in Urdu meets English identifiers,
one term fails to match, and the whole question is refused — while the data
sits there, answerable.

So an unbound term is looked up on the open web, and whatever it turns out
to mean is bound again against the schema. Nothing here holds a dictionary:
the meaning is fetched at the moment it is needed, from sources that are
recorded alongside it.

WHAT IT LEARNS, AND WHAT IT DOES NOT

It learns what a WORD means — public knowledge, the same category as
"Karachi East is part of Karachi Division". It never learns what this
corpus contains; that is private, and asking the web about it would be
both useless and a leak.

A confirmed mapping is written to schema_terms with its source, so the same
word is free next time and a wrong one is visible and removable. The list
grows with the vocabulary people actually use, not with the data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import asyncpg

from src.llm.client import call_llm
from src.retrieval.world_knowledge import Evidence, search


@dataclass
class LearnedTerm:
    term: str
    english: str | None = None
    meaning: str | None = None
    confidence: float = 0.0
    bound_to: list[tuple[str, str | None]] = field(default_factory=list)
    # (table, column, value) for a term that turned out to name a stored
    # VALUE rather than a table or column — "police station" is not an
    # identifier here, it is places.kind_raw = 'police_station'.
    bound_values: list[tuple[str, str | None, str]] = field(
        default_factory=list)
    sources: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.english) and self.confidence >= 0.6


SYSTEM = """You are given a word from a question about Pakistani police
records, and search snippets about it. Say what it means in English.

Return ONLY this JSON:

{"english": "<the English word a database column would use, or null>",
 "meaning": "<one short sentence, or null>",
 "confidence": <0.0-1.0>}

Rules:
1. Answer from the snippets. If they do not explain the word, return null
   with confidence 0 — a guess here becomes a filter on real data.
2. `english` must be the plain noun a schema would use: "cases",
   "weapons", "arrest", "station". Not a paraphrase, not a sentence.
3. A word that is grammar rather than content — "how", "many", "the" —
   returns null. It names nothing to look up."""


_JSON = re.compile(r"\{.*\}", re.S)


async def learn(conn: asyncpg.Connection, term: str,
                provider: str | None = None) -> LearnedTerm:
    """Find out what a term means, then bind the meaning to the schema."""
    out = LearnedTerm(term=term)

    # Already known? The catalog is checked first so a repeated word costs
    # nothing, and so a human correction wins over a fresh lookup.
    known = await conn.fetchrow(
        "SELECT table_name, column_name, source FROM schema_terms"
        " WHERE term_skeleton = normalize_name($1)"
        " ORDER BY CASE source WHEN 'human' THEN 0 ELSE 1 END,"
        "          coalesce(confidence, 0) DESC LIMIT 1", term)
    if known:
        out.english = term
        out.confidence = 1.0
        out.bound_to = [(known["table_name"], known["column_name"])]
        out.reason = f"already known ({known['source']})"
        return out

    # Query shape decides the answer. MEASURED on the same word:
    #
    #   "ملزمان meaning Pakistan police FIR English translation"
    #       -> a thesis PDF and a news article. Useless.
    #   "ملزمان English translation Urdu word"
    #       -> "The Urdu Word ... Meaning in English is Accused."
    #
    # Over-qualifying with domain words buries the dictionary result the
    # lookup actually needs. So it asks plainly first and only narrows if
    # that finds nothing — broadest and simplest first, which is the
    # opposite of how I wrote it the first time.
    evidence: list[Evidence] = []
    for query in (f"{term} English translation Urdu word",
                  f"{term} meaning in English",
                  f"{term} Pakistan police meaning"):
        evidence = search(query, max_results=5)
        if evidence:
            break
    if not evidence:
        out.reason = "no search results"
        return out
    out.sources = [e.url for e in evidence][:4]

    snippets = "\n\n".join(
        f"[{i+1}] {e.title}\n{e.body}" for i, e in enumerate(evidence))
    try:
        raw = await call_llm(SYSTEM, f"Word: {term}\n\n{snippets}",
                             temperature=0.0, max_tokens=2000,
                             provider_override=provider,
                             force_cloud=provider is not None)
    except Exception as exc:                            # noqa: BLE001
        out.reason = f"model unavailable: {exc}"[:150]
        return out

    m = _JSON.search(raw or "")
    if not m:
        out.reason = "no parseable answer"
        return out
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        out.reason = "malformed answer"
        return out

    english = obj.get("english")
    out.english = str(english).strip() if english else None
    out.meaning = (str(obj.get("meaning")).strip()
                   if obj.get("meaning") else None)
    out.confidence = float(obj.get("confidence") or 0.0)

    if not out.usable:
        out.reason = out.reason or "snippets did not explain the word"
        return out

    # Bind the English meaning against the schema — the same binder every
    # other term goes through, so a learned word gets no special treatment.
    # Value matches are INCLUDED, and excluding them was a real defect.
    #
    # MEASURED: تھانے was looked up correctly — "police station",
    # confidence 1.0, from two dictionaries — and then bound to nothing,
    # because this query asked only for tables and columns. But a police
    # station is not a table in this schema. It is a VALUE:
    #
    #   search_schema('police station')
    #     value  places.kind_raw = 'police_station'   1.0   <- excluded
    #     column rebuild_cases.station_place_id       0.455 <- kept
    #
    # An exact stored value at 1.0 was thrown away in favour of a column
    # name at 0.455, and the question was refused. Concepts live in values
    # as often as in identifiers, and a translation layer that can only
    # land on identifiers cannot reach half the schema.
    rows = await conn.fetch(
        "SELECT table_name, column_name, match_kind, value_raw, score"
        " FROM search_schema($1)"
        " WHERE match_kind IN ('table','column','value','cross_script')"
        " ORDER BY score DESC LIMIT 3", out.english)
    out.bound_to = [(r["table_name"], r["column_name"]) for r in rows
                    if float(r["score"] or 0) >= 0.6]
    # The value itself is carried too, so the planner can apply the filter
    # the word actually meant rather than only reaching the right table.
    out.bound_values = [
        (r["table_name"], r["column_name"], r["value_raw"])
        for r in rows
        if r["value_raw"] is not None and float(r["score"] or 0) >= 0.9
    ]
    if not out.bound_to:
        out.reason = (f"learned {term!r} means {out.english!r}, but that "
                      f"matches nothing in this schema either")
    return out


async def remember(conn: asyncpg.Connection, learned: LearnedTerm,
                   lang: str = "ur") -> bool:
    """Record a confirmed mapping so the next question does not pay for it.

    Only a term that actually bound is kept. Remembering a word that
    resolved to nothing would fill the catalog with noise and make the
    cache itself a source of wrong matches.
    """
    if not learned.usable or not learned.bound_to:
        return False
    table, column = learned.bound_to[0]
    await conn.execute(
        """
        INSERT INTO schema_terms
            (table_name, column_name, term, lang, source, confidence)
        VALUES ($1, $2, $3, $4, 'web', $5)
        ON CONFLICT (table_name, column_name, term) DO NOTHING
        """,
        table, column, learned.term, lang,
        min(learned.confidence, 0.75))     # below a human or model term
    return True
