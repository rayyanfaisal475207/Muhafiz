# ============================================================
# Generate docs/schema-snapshot.json from the live database schema.
#
#   python scripts/generate_schema_snapshot.py
#   python scripts/generate_schema_snapshot.py --out docs/schema-snapshot.json
#
# WHY THIS DIDN'T EXIST UNTIL NOW (Documentation Gaps Fix Prompt, Module
# D): docs/schema-snapshot.json has always had a real *consumer*
# (scripts/build_erd.py, `json.load(open("schema.json"))`), but no script
# in this repo ever produced the file — it was hand-maintained or
# produced once, out of band, and left to drift. Confirmed during this
# audit: 15 of 27 real tables were missing entirely (cases,
# case_assignments, audit_logs, every community-detection/graph-scale
# table, entity_resolution_consistency_findings, ingestion_run_quality,
# ...).
#
# NOTE ON scripts/build_erd.py: that script is NOT a generic
# schema-to-ERD renderer for this repo — its own title ("TaxIQ —
# Database Schema") and hardcoded LAYOUT (tax_rates, document_chunks/
# pgvector — none of which exist in Muhafiz's schema) confirm it's a
# leftover artifact from an unrelated product. A fresh, accurate JSON
# snapshot from THIS script does not make build_erd.py produce a valid
# Muhafiz ERD image — that would need a real Muhafiz-schema-aware
# renderer, which is deliberately NOT what this script attempts (see
# DOCUMENTATION_GAPS_FIX_PROMPT.md Module D and the corresponding
# README.md/docs/DATABASE_DESIGN.md staleness notes, which say so
# plainly rather than implying a working regenerate-the-image path
# exists).
#
# SHAPE MATCHES THE PRE-EXISTING docs/schema-snapshot.json EXACTLY (not
# invented fresh) — reverse-engineered from that file's own real content
# before writing this script, so any future consumer (build_erd.py or
# a real replacement) sees zero shape drift from what's shipped today:
#
#   {
#     "<table_name>": {
#       "columns": [{"name": str, "type": str, "nullable": bool}, ...],
#       "pk": [str, ...]
#     },
#     ...,
#     "_fks": [{"src": str, "src_col": str, "tgt": str, "tgt_col": str}, ...]
#   }
#
# `type` is Postgres's `udt_name` (information_schema.columns), not
# `data_type` — confirmed by inspecting the pre-existing file's own
# values ('int4'/'uuid'/'bool'/'timestamp'/'timestamptz', not
# 'integer'/'character varying'), which is the internal type name
# `udt_name` returns, not the SQL-standard display name `data_type`
# would ('varchar' vs 'character varying', etc.).
#
# Every `public` base table (not views, not the Apache AGE internal
# per-label vertex/edge tables `ag_catalog` owns) is included — no
# hand-maintained exclusion list to fall out of sync with the schema
# the way the JSON file itself did.
# ============================================================
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text

from src.database.postgres import get_session

_TABLES_QUERY = text("""
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY table_name
""")

_COLUMNS_QUERY = text("""
    SELECT column_name, udt_name, is_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = :table_name
    ORDER BY ordinal_position
""")

_PK_QUERY = text("""
    SELECT kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
     AND tc.table_schema = kcu.table_schema
    WHERE tc.constraint_type = 'PRIMARY KEY'
      AND tc.table_schema = 'public'
      AND tc.table_name = :table_name
    ORDER BY kcu.ordinal_position
""")

_FKS_QUERY = text("""
    SELECT
        tc.table_name AS src, kcu.column_name AS src_col,
        ccu.table_name AS tgt, ccu.column_name AS tgt_col
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
     AND tc.table_schema = kcu.table_schema
    JOIN information_schema.constraint_column_usage ccu
      ON tc.constraint_name = ccu.constraint_name
     AND tc.table_schema = ccu.table_schema
    WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
    ORDER BY tc.table_name, kcu.column_name
""")


async def generate_snapshot() -> dict:
    snapshot: dict = {}
    async with get_session() as db:
        tables = [row[0] for row in (await db.execute(_TABLES_QUERY)).all()]

        for table_name in tables:
            columns = [
                {"name": name, "type": udt_name, "nullable": is_nullable == "YES"}
                for name, udt_name, is_nullable in (
                    await db.execute(_COLUMNS_QUERY, {"table_name": table_name})
                ).all()
            ]
            pk = [row[0] for row in (await db.execute(_PK_QUERY, {"table_name": table_name})).all()]
            snapshot[table_name] = {"columns": columns, "pk": pk}

        fks = [
            {"src": src, "src_col": src_col, "tgt": tgt, "tgt_col": tgt_col}
            for src, src_col, tgt, tgt_col in (await db.execute(_FKS_QUERY)).all()
        ]
    snapshot["_fks"] = fks
    return snapshot


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/schema-snapshot.json", help="Output path (default: docs/schema-snapshot.json)")
    args = parser.parse_args()

    snapshot = await generate_snapshot()
    table_count = len(snapshot) - 1  # exclude "_fks" itself
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"Wrote {args.out}: {table_count} table(s), {len(snapshot['_fks'])} foreign key(s).")


if __name__ == "__main__":
    asyncio.run(main())
