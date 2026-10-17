"""Embed the schema so a question can be matched to it by meaning.

WHAT THIS FIXES

Binding matched a question's words against identifiers and stored values
with trigram comparison. That is spelling, not meaning, and it works for
English only by coincidence: English questions use English words and the
identifiers are in English.

Urdu has no such coincidence. Its words collide with stored VALUES,
because the values are the part of this database that is in Urdu.
Measured, on "how many weapons were recovered" asked in Urdu:

    'ہتھیار' -> weapons                     table  1.000
    'برآمد'  -> case_events.event_type_raw  value  1.000   <- tie

The agent took the event type and answered 7. The truth is 96.

The same question, ranked by the multilingual embedding model already
running for passage retrieval:

    0.8348  weapons        <- correct, and higher than the English
    0.8213  seized_items      question's own top score (0.7940)
    0.8066  vehicles

The model had the answer. Nothing asked it. This builds the index that
lets binding ask.

WHAT IS EMBEDDED, AND WHAT IS NOT

The schema: an identifier, the COMMENT saying what it is FOR, the
Urdu/English/Roman terms in schema_terms — and, for a column that holds
a small fixed set of values, THOSE VALUES.

That last part was missing and it cost a whole class of questions.
Measured on "کتنے تھانے ہیں؟" (how many police stations):

    places terms:     مقامات, مقام, places, place      <- generic
    kind_raw values:  police_station (19), city (9),
                      administrative_parent (7)

Nineteen police stations, and the concept "police station" is not a
table in this schema — it is a VALUE. Embedding the column as "places.kind
raw — any geographic or organisational unit" gave the model nothing to
match "تھانے" against, and it ranked weapons first.

The model was not the problem. I had suspected Urdu morphology; measured,
cos(تھانہ, تھانے) = 0.9867 and cos(تھانے, "police station") = 0.8208. The
embedding model handles the plural perfectly. It cannot match a concept
that was never written down.

WHICH VALUES, AND WHY THAT IS NOT UNBOUNDED

Postgres already knows which columns are enumerable: n_distinct in
column_stats. A column with a handful of distinct values is a controlled
vocabulary and listing it is bounded and stable. A free-text column has
thousands and is skipped automatically by the same number — no list of
"columns worth expanding" is written anywhere, because the statistics
decide.

So this stays bounded work: tens of tables, hundreds of columns, and a
vocabulary that only grows when the DATA grows a new category, not when
the corpus grows a new row.

RE-RUNNABLE

Each row records the exact text embedded and the model that embedded it.
Re-running replaces rows whose source text or model changed and leaves
the rest, so a schema tweak costs a handful of calls rather than all of
them.

    python scripts/embed_schema.py            # build or update
    python scripts/embed_schema.py --rebuild  # ignore what is stored
"""

from __future__ import annotations

import argparse
import asyncio
import os
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

from src.retrieval.embedder import embed_texts  # noqa: E402


def dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        sys.exit("DATABASE_URL is not set")
    return url.replace("postgresql+asyncpg://", "postgresql://")


def model_name() -> str:
    return os.getenv("EMBEDDING_PROVIDER", "e5")


# The text that gets embedded. Deliberately reads like a description of
# the thing rather than a row of metadata: the embedding model was trained
# on prose, and "cases: one criminal case, opened by an FIR (مقدمات,
# مقدمہ, cases)" sits much closer to a real question than "rebuild_cases".
async def documents(conn: asyncpg.Connection) -> list[dict]:
    tables = await conn.fetch(
        """
        SELECT tr.table_name,
               coalesce(obj_description(c.oid, 'pg_class'), '') AS purpose,
               coalesce(string_agg(DISTINCT st.term, ', '), '') AS terms
        FROM table_roles tr
        JOIN pg_class c ON c.relname = tr.table_name
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        LEFT JOIN schema_terms st ON st.table_name = tr.table_name
                                 AND st.column_name IS NULL
        WHERE tr.is_evidence AND tr.role IN ('fact','dimension','entity')
        GROUP BY 1, 2
        ORDER BY 1
        """)
    out: list[dict] = [
        {"table": r["table_name"], "column": None,
         "doc": _phrase(r["table_name"], r["purpose"], r["terms"])}
        for r in tables
    ]

    columns = await conn.fetch(
        """
        SELECT cc.table_name, cc.column_name,
               coalesce(col_description(c.oid, a.attnum), '')  AS purpose,
               coalesce(string_agg(DISTINCT st.term, ', '), '') AS terms,
               coalesce(obj_description(c.oid, 'pg_class'), '') AS of_table
        FROM column_catalog cc
        JOIN table_roles tr ON tr.table_name = cc.table_name
        JOIN pg_class c ON c.relname = cc.table_name
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        JOIN pg_attribute a ON a.attrelid = c.oid
                           AND a.attname = cc.column_name
        LEFT JOIN schema_terms st ON st.table_name = cc.table_name
                                 AND st.column_name = cc.column_name
        WHERE tr.is_evidence AND tr.role IN ('fact','dimension','entity')
          AND NOT cc.is_primary_key
          AND cc.references_table IS NULL
          AND NOT cc.is_generated
        GROUP BY 1, 2, 3, 5
        ORDER BY 1, 2
        """)
    for r in columns:
        # Surrogate keys and bookkeeping columns are never what a question
        # is about, and embedding them only adds plausible-looking noise
        # to every ranking.
        if r["column_name"].endswith("_id") or r["column_name"] in (
                "asserted_at", "valid_from", "valid_to", "created_at",
                "updated_at", "confidence", "ordinal", "sequence_no"):
            continue
        values = await _vocabulary(conn, r["table_name"], r["column_name"])
        out.append({
            "table": r["table_name"], "column": r["column_name"],
            "doc": _phrase(f"{r['table_name']}.{r['column_name']}",
                           r["purpose"] or r["of_table"], r["terms"],
                           values),
        })
    return out


# How many distinct values still counts as a vocabulary rather than data.
# Above this a column is free text or an identifier, and listing it would
# be unbounded; below it the list IS the column's meaning.
VOCAB_MAX = 30


async def _vocabulary(conn: asyncpg.Connection, table: str,
                      column: str) -> list[str]:
    """The values a column actually holds, when they form a fixed set.

    Postgres decides, not a hand-written list: n_distinct says whether
    this column is a controlled vocabulary. When it is, the values are
    read live rather than taken from the statistics sample, so the doc
    reflects the data and not the last time ANALYZE ran.
    """
    stat = await conn.fetchrow(
        "SELECT n_distinct, distinct_estimate FROM column_stats"
        " WHERE table_name = $1 AND column_name = $2", table, column)
    if not stat:
        return []
    est = stat["distinct_estimate"]
    if est is None or est <= 0 or est > VOCAB_MAX:
        return []
    try:
        rows = await conn.fetch(
            f'SELECT DISTINCT "{column}"::text AS v FROM "{table}"'
            f' WHERE "{column}" IS NOT NULL ORDER BY 1 LIMIT {VOCAB_MAX + 1}')
    except Exception:                                   # noqa: BLE001
        return []
    if len(rows) > VOCAB_MAX:
        return []
    return [r["v"] for r in rows if r["v"] and len(r["v"]) <= 80]


def _phrase(label: str, purpose: str, terms: str,
            values: list[str] | None = None) -> str:
    bits = [label.replace("_", " ")]
    if purpose:
        bits.append(purpose)
    if terms:
        bits.append(f"also called: {terms}")
    if values:
        # Written as prose rather than a bare list: the embedding model
        # was trained on sentences, and "holds one of: police_station,
        # city" sits closer to a real question than a comma-separated row.
        bits.append("holds one of: " + ", ".join(values))
    return " — ".join(bits)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild", action="store_true",
                    help="re-embed everything, ignoring what is stored")
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()

    model = model_name()
    conn = await asyncpg.connect(dsn())
    try:
        wanted = await documents(conn)
        print(f"{len(wanted)} schema objects to cover (model: {model})")

        have: dict[tuple[str, str | None], str] = {}
        if not args.rebuild:
            for r in await conn.fetch(
                    "SELECT table_name, column_name, doc FROM"
                    " schema_embeddings WHERE model = $1", model):
                have[(r["table_name"], r["column_name"])] = r["doc"]

        # Only what changed. A COMMENT edit or a new term should cost a
        # few calls, not a full rebuild — otherwise the index stops being
        # maintained, which is how it goes stale without anyone noticing.
        todo = [d for d in wanted
                if have.get((d["table"], d["column"])) != d["doc"]]
        print(f"{len(todo)} need embedding, {len(wanted) - len(todo)} unchanged")
        if not todo:
            await _report(conn)
            return

        written = 0
        for i in range(0, len(todo), args.batch):
            chunk = todo[i:i + args.batch]
            vectors = await embed_texts([d["doc"] for d in chunk],
                                        task_type="RETRIEVAL_DOCUMENT")
            if len(vectors) != len(chunk):
                sys.exit(f"embedder returned {len(vectors)} vectors for "
                         f"{len(chunk)} documents; refusing to write a "
                         f"misaligned index")
            async with conn.transaction():
                for d, vec in zip(chunk, vectors):
                    await conn.execute(
                        """
                        INSERT INTO schema_embeddings
                            (table_name, column_name, doc, embedding, dims,
                             model, built_at)
                        VALUES ($1, $2, $3, $4, $5, $6, now())
                        ON CONFLICT (table_name, (coalesce(column_name, ''))) DO UPDATE
                        SET doc = EXCLUDED.doc,
                            embedding = EXCLUDED.embedding,
                            dims = EXCLUDED.dims,
                            model = EXCLUDED.model,
                            built_at = now()
                        """,
                        d["table"], d["column"], d["doc"],
                        [float(x) for x in vec], len(vec), model)
            written += len(chunk)
            print(f"  {written}/{len(todo)}", flush=True)

        # An index built from two models is not an index — cosine between
        # vectors from different models is meaningless. Say so loudly.
        stale = await conn.fetch(
            "SELECT DISTINCT model, dims FROM schema_embeddings")
        if len(stale) > 1:
            print("\nWARNING: rows from more than one model/dimension: "
                  + ", ".join(f"{r['model']}({r['dims']})" for r in stale)
                  + " — run with --rebuild, mixed vectors cannot be compared")

        await _report(conn)
    finally:
        await conn.close()


async def _report(conn: asyncpg.Connection) -> None:
    r = await conn.fetchrow("SELECT * FROM schema_embedding_freshness")
    print(f"\ntables {r['tables_embedded']}/{r['tables_expected']}, "
          f"columns {r['columns_embedded']}, models {r['models']}, "
          f"built {r['built_at']}")


if __name__ == "__main__":
    asyncio.run(main())
