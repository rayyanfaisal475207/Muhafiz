"""Read what a question MEANS, against what the data means.

WHY LEXICAL BINDING IS NOT ENOUGH

The binder matches strings: a term against identifier names and stored
values. That is why "how many cases are registered" bound `registered` to
`registered_at` and the agent went looking for a registration marker,
found almost none, and concluded the data could not answer. The truth was
73 — every FIR in a police database is registered, so the word describes
the population rather than selecting part of it.

Column names exist to give meaning to data. They are not match targets.
A system that maps question words onto column names will keep making that
class of error, in every domain, forever:

    "how many weapons are there"     -> not a weapon_count column
    "which cases are still open"     -> not an is_open flag
    "patients admitted last year"    -> "admitted" may describe all rows

WHAT THIS DOES INSTEAD

It reads the question alongside what the schema SAYS IT MEANS — the table
and column comments, which describe purpose rather than spelling — and
returns an interpretation:

    subject      what is being counted or listed
    restrictive  terms that genuinely narrow the population
    descriptive  terms that describe the population and must NOT become
                 filters
    measure      count, sum, average, list

The model reads meaning; nothing here knows about police data. Swap the
corpus and the comments change, so the interpretation changes with it.

WHY THIS IS NOT A VOCABULARY

No term, status, category or column name is listed here. The distinction
between restrictive and descriptive is decided per question against the
live schema description, not looked up. A word that is descriptive in one
corpus is restrictive in another, and this asks rather than assumes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import asyncpg

from src.llm.client import call_llm


@dataclass
class Interpretation:
    subject: str | None = None
    measure: str = "count"                 # count | list | sum | average | describe
    restrictive: list[str] = field(default_factory=list)
    descriptive: list[str] = field(default_factory=list)
    reasoning: str = ""
    available: bool = True                 # False when the model could not run

    def brief(self) -> str:
        parts = [f"subject={self.subject!r}", f"measure={self.measure}"]
        if self.restrictive:
            parts.append("narrows on: " + ", ".join(self.restrictive))
        if self.descriptive:
            parts.append("describes (do NOT filter on): "
                         + ", ".join(self.descriptive))
        return " | ".join(parts)


SYSTEM = """You read a question about a database and say what it MEANS,
using the descriptions of what the data holds. You never write SQL.

Return ONLY this JSON:

{"subject": "<the thing being counted or listed>",
 "measure": "count|list|sum|average|describe",
 "restrictive": ["<term that genuinely narrows the population>"],
 "descriptive": ["<term that describes the population, not a filter>"],
 "reasoning": "<one sentence>"}

The distinction that matters:

  RESTRICTIVE  the question would have a different answer without it.
               "cases in Lahore" — Lahore excludes other cities.

  DESCRIPTIVE  the term describes what the records ARE, so filtering on it
               would wrongly shrink the answer. If every record in this
               data is already X, then "X records" means all of them.

Judge this from the DESCRIPTIONS given, not from the spelling of column
names. A word matching a column name is not evidence that it is a filter:
columns are named to describe data, not to be matched against questions.

If you are unsure whether a term restricts, put it in restrictive — a
filter that is checked is safer than an answer that is silently too wide."""


_JSON = re.compile(r"\{.*\}", re.S)


async def _schema_meaning(conn: asyncpg.Connection, limit: int = 14) -> str:
    """What the schema says it is FOR, in its own words."""
    rows = await conn.fetch(
        """
        SELECT tr.table_name, tr.role,
               obj_description(c.oid, 'pg_class') AS purpose,
               (SELECT count(*) FROM information_schema.columns ic
                 WHERE ic.table_schema = 'public'
                   AND ic.table_name = tr.table_name) AS n_cols
        FROM table_roles tr
        JOIN pg_class c ON c.relname = tr.table_name
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        WHERE tr.is_evidence AND tr.role IN ('fact','dimension','entity')
        ORDER BY tr.role, tr.table_name
        LIMIT $1
        """, limit)
    lines = []
    for r in rows:
        purpose = " ".join((r["purpose"] or "").split())[:220]
        lines.append(f"  {r['table_name']} ({r['role']}): "
                     + (purpose or "no description"))
    return "\n".join(lines)


async def interpret(conn: asyncpg.Connection, question: str,
                    provider: str | None = None) -> Interpretation:
    meaning = await _schema_meaning(conn)
    user = (f"Question: {question}\n\n"
            f"What this database holds:\n{meaning}\n\n"
            f"Interpretation JSON:")

    try:
        raw = await call_llm(SYSTEM, user, temperature=0.0, max_tokens=2000,
                             provider_override=provider,
                             force_cloud=provider is not None)
    except Exception:                                   # noqa: BLE001
        # Interpretation is an improvement, not a prerequisite. Without it
        # the planner falls back to lexical binding, which is weaker but
        # still works — and the caller is told so it can say so.
        return Interpretation(available=False,
                              reasoning="interpretation unavailable")

    m = _JSON.search(raw or "")
    if not m:
        return Interpretation(available=False,
                              reasoning="no parseable interpretation")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return Interpretation(available=False,
                              reasoning="malformed interpretation")

    def as_list(v) -> list[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()][:8]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    return Interpretation(
        subject=(str(obj.get("subject")).strip()
                 if obj.get("subject") else None),
        measure=str(obj.get("measure") or "count").strip().lower(),
        restrictive=as_list(obj.get("restrictive")),
        descriptive=as_list(obj.get("descriptive")),
        reasoning=str(obj.get("reasoning") or "")[:300],
    )
