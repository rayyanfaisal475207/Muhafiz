"""Name every table and column in the languages questions arrive in.

WHY

    "How many cases are registered?"   -> 73
    "کتنے مقدمات درج ہیں؟"              -> refuses

Same question, same data. The table is `rebuild_cases`; the Urdu word is
مقدمات. Nothing connects them, because identifier matching folds spelling,
not meaning across languages.

This generates that connection once and stores it. The schema is small —
54 tables, 560 columns — and does not grow with the corpus, so translating
it is bounded work that stays bounded at a million cases.

WHAT IT READS

Identifier names and the COMMENTs already in the schema, which state what
each table is FOR. A comment like "one criminal case, opened by an FIR"
gives a translator far more to work with than the string `rebuild_cases`.

WHAT IT DOES NOT DO

It never reads corpus values. This describes the schema, not the data.
Translating values would be unbounded and would go stale on every
ingestion; that job belongs to cross-script value matching, which already
exists and runs at query time.

RE-RUNNABLE

Every term carries its source and confidence. Running this again replaces
the model-generated set and leaves anything marked 'human' untouched, so a
correction survives regeneration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.llm.client import call_llm  # noqa: E402


def dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        sys.exit("DATABASE_URL is not set")
    return url.replace("postgresql+asyncpg://", "postgresql://")


SYSTEM = """You name database tables and columns in the languages people
actually ask questions in.

You are given an identifier and a description of what it holds. Return the
words a person would use for that thing, in Urdu and in English.

Return ONLY this JSON:

{"terms": [
   {"term": "مقدمات", "lang": "ur"},
   {"term": "مقدمہ", "lang": "ur"},
   {"term": "cases", "lang": "en"},
   {"term": "case", "lang": "en"}]}

Rules:
1. Give the words a QUESTION would use, not a translation of the
   identifier's spelling. `rebuild_cases` is asked about as "cases" or
   "مقدمات"; nobody says "rebuild".
2. Include singular and plural for each language. Questions use both.
3. Include a common Roman-Urdu spelling when one exists (lang "roman"),
   because people type Urdu words in Latin script.
4. Four to eight terms. Precision matters more than coverage: a wrong term
   makes an unrelated question match this table.
5. If the identifier is internal plumbing with no natural question word —
   a surrogate key, a timestamp of when a row was written — return an
   empty list rather than inventing one."""


_JSON = re.compile(r"\{.*\}", re.S)


async def targets(conn: asyncpg.Connection, include_columns: bool
                  ) -> list[dict]:
    """Tables and the columns worth naming.

    Internal plumbing is skipped: primary keys, foreign keys and the
    bitemporal bookkeeping columns are never what a question is about, and
    naming them would only create false matches.
    """
    rows = await conn.fetch(
        """
        SELECT tr.table_name,
               obj_description(c.oid, 'pg_class') AS table_purpose
        FROM table_roles tr
        JOIN pg_class c ON c.relname = tr.table_name
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        WHERE tr.is_evidence AND tr.role IN ('fact','dimension','entity')
        ORDER BY tr.table_name
        """)
    out: list[dict] = [
        {"table": r["table_name"], "column": None,
         "purpose": r["table_purpose"] or ""}
        for r in rows
    ]
    if not include_columns:
        return out

    skip = {"span_id", "asserted_by_doc_id", "asserted_at", "valid_from",
            "valid_to", "superseded_by_doc_id", "created_at", "updated_at",
            "confidence", "ordinal", "sequence_no"}
    cols = await conn.fetch(
        """
        SELECT cc.table_name, cc.column_name,
               col_description(c.oid, a.attnum) AS purpose
        FROM column_catalog cc
        JOIN table_roles tr ON tr.table_name = cc.table_name
        JOIN pg_class c ON c.relname = cc.table_name
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        JOIN pg_attribute a ON a.attrelid = c.oid
                           AND a.attname = cc.column_name
        WHERE tr.is_evidence AND tr.role IN ('fact','dimension','entity')
          AND NOT cc.is_primary_key
          AND cc.references_table IS NULL
          AND NOT cc.is_generated
        ORDER BY cc.table_name, cc.column_name
        """)
    for r in cols:
        if r["column_name"] in skip or r["column_name"].endswith("_id"):
            continue
        out.append({"table": r["table_name"], "column": r["column_name"],
                    "purpose": r["purpose"] or ""})
    return out


async def name_one(item: dict) -> list[dict]:
    label = item["column"] or item["table"]
    what = "column" if item["column"] else "table"
    context = (f"{what}: {label}\n"
               f"in table: {item['table']}\n"
               f"description: {item['purpose'] or '(none given)'}")
    try:
        raw = await call_llm(SYSTEM, context, temperature=0.0,
                             max_tokens=2000)
    except Exception:                                   # noqa: BLE001
        return []
    m = _JSON.search(raw or "")
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    terms = obj.get("terms")
    if not isinstance(terms, list):
        return []
    out = []
    for t in terms[:8]:
        if not isinstance(t, dict):
            continue
        term = str(t.get("term") or "").strip()
        lang = str(t.get("lang") or "").strip().lower()
        if term and lang and len(term) <= 60:
            out.append({"term": term, "lang": lang})
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables-only", action="store_true")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = await asyncpg.connect(dsn())
    try:
        items = await targets(conn, include_columns=not args.tables_only)
        if args.limit:
            items = items[:args.limit]
        print(f"Naming {len(items)} schema objects")

        sem = asyncio.Semaphore(args.concurrency)
        written = 0
        empty = 0

        async def work(item: dict) -> None:
            nonlocal written, empty
            async with sem:
                terms = await name_one(item)
            if not terms:
                empty += 1
                return
            async with conn.transaction():
                # Model output replaces model output; a human correction is
                # never overwritten by a regeneration.
                await conn.execute(
                    "DELETE FROM schema_terms"
                    " WHERE table_name = $1"
                    "   AND column_name IS NOT DISTINCT FROM $2"
                    "   AND source = 'model'",
                    item["table"], item["column"])
                for t in terms:
                    await conn.execute(
                        "INSERT INTO schema_terms"
                        " (table_name, column_name, term, lang, source,"
                        "  confidence)"
                        " VALUES ($1,$2,$3,$4,'model',0.8)"
                        " ON CONFLICT (table_name, column_name, term)"
                        " DO NOTHING",
                        item["table"], item["column"], t["term"], t["lang"])
            written += len(terms)
            label = f"{item['table']}" + (f".{item['column']}"
                                          if item["column"] else "")
            print(f"  {label}: " + ", ".join(t["term"] for t in terms),
                  flush=True)

        await asyncio.gather(*[work(i) for i in items])

        print(f"\n{written} terms written, {empty} objects produced none")
        for r in await conn.fetch(
                "SELECT lang, count(*) n FROM schema_terms"
                " GROUP BY lang ORDER BY 2 DESC"):
            print(f"  {r['lang']}: {r['n']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
