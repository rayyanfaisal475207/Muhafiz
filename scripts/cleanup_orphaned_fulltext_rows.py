# -*- coding: utf-8 -*-
"""
Module 37 — delete the `chunk_fulltext` rows whose `chunk_id` no longer exists
in Chroma, after backing every one of them up to a committed file.

WHY THIS EXISTS
---------------
`chunk_fulltext` (migrations/022_chunk_fulltext_index.sql,
src/retrieval/fulltext_index.py) is the BM25 half of hybrid retrieval. It is
maintained incrementally on ingest by `fulltext_index.maintain()`, called from
`vector_store.upsert_documents()` for every chunk written to Chroma.

Nothing keeps the two stores in step in the *other* direction:

  * `ChromaVectorStore.drop_and_recreate()` (src/retrieval/vector_store.py:136,
    reached by `reset_collection()` and by `scripts/reset_evidence_state.py`'s
    `_reset_chroma()`) empties the Chroma collection and does not touch
    `chunk_fulltext` at all. `scripts/reset_evidence_state.py::_reset_postgres()`
    deletes community_runs / ingestion_jobs / case_assignments / documents /
    cases — and not `chunk_fulltext`.
  * A document's chunk ids are derived from `Document._generate_id()`:
    `md5(scope::source::page::text[:200])[:8]`. Re-ingesting the same source
    file after any change to extraction or chunking produces a *different*
    doc_id, so the new chunks are inserted alongside the old ones instead of
    upserting over them.

The only path that deletes from `chunk_fulltext` by source is the admin
document-delete route (`src/api/admin.py::delete_kb_document`).

The result is rows that BM25 still ranks and still hands to the generator,
whose `ChromaVectorStore.get_by_ids()` returns nothing — the model receives a
`[Document N]` marker pointing at an empty chunk. Module 82 measured 2 of KB2's
5 window chunks being exactly that, with the answer's lead citation on one of
them 3 runs of 3.

CONTRACT
--------
* Orphans are DERIVED LIVE, both stores, every run — never a hardcoded id list,
  because the id set depends on which ingestion the store currently holds.
* Idempotent: a second run finds nothing and exits 0.
* Reversible: every column except the derived `tsv` is written to the backup
  before a single row is deleted, and `--restore` puts them back (`tsv` is
  recomputed from `text` with the same expression `fulltext_index.maintain()`
  uses).
* Refuses to act if Chroma looks empty or unexpectedly small, which would
  otherwise classify the entire index as orphaned.

USAGE
-----
    # report only, writes nothing
    PYTHONPATH=. python scripts/cleanup_orphaned_fulltext_rows.py

    # back up, then delete
    PYTHONPATH=. python scripts/cleanup_orphaned_fulltext_rows.py --apply

    # put them back from the committed backup
    PYTHONPATH=. python scripts/cleanup_orphaned_fulltext_rows.py --restore

CHROMA_PERSIST_DIR is relative in `.env` (`./data/chroma_db`), so run this from
the deployment root or point it at an absolute path — pointing it at a worktree
opens an empty store, which the guard below will catch.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import io
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def _force_utf8_stdout() -> None:
    """Called from __main__ only — rebinding sys.stdout at import time breaks
    pytest's capture plugin, and tests/test_module37_orphan_cleanup.py imports
    this module directly."""
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DEFAULT_BACKUP = os.path.join(
    HERE, "docs", "gold-qa-wave2-results", "module37_orphan_backup.jsonl.gz"
)

# Below this, assume the Chroma store failed to open / points at the wrong
# directory rather than that the whole BM25 index is orphaned. The real store
# held 7,716 documents when this was written.
MIN_CHROMA_DOCS = 1000

COLUMNS = (
    "chunk_id",
    "doc_id",
    "source",
    "project_id",
    "case_id",
    "is_global",
    "text",
    "metadata",
    "updated_at",
)


def _open(path: str, mode: str):
    opener = gzip.open if path.endswith(".gz") else open
    return opener(path, mode, encoding="utf-8")


def chroma_ids() -> set[str]:
    from src.retrieval.vector_store import ChromaVectorStore

    store = ChromaVectorStore.get_instance()
    ids = set(store._collection.get(include=[])["ids"])
    if len(ids) < MIN_CHROMA_DOCS:
        raise SystemExit(
            f"REFUSING TO RUN: Chroma returned only {len(ids)} ids "
            f"(< {MIN_CHROMA_DOCS}). CHROMA_PERSIST_DIR is probably pointing at "
            f"the wrong directory: {os.environ.get('CHROMA_PERSIST_DIR')!r}. "
            f"Every chunk_fulltext row would look orphaned."
        )
    return ids


async def fetch_rows(chunk_ids: list[str] | None = None) -> list[dict]:
    from sqlalchemy import text as sql

    from src.database.postgres import get_session

    cols = ", ".join(COLUMNS)
    async with get_session() as db:
        if chunk_ids is None:
            res = await db.execute(sql(f"SELECT {cols} FROM chunk_fulltext"))
        else:
            res = await db.execute(
                sql(f"SELECT {cols} FROM chunk_fulltext WHERE chunk_id = ANY(:ids)"),
                {"ids": chunk_ids},
            )
        rows = res.fetchall()
    out = []
    for r in rows:
        d = dict(zip(COLUMNS, r))
        if not isinstance(d["metadata"], str):
            d["metadata"] = json.dumps(d["metadata"], default=str, ensure_ascii=False)
        d["updated_at"] = d["updated_at"].isoformat() if d["updated_at"] else None
        d["project_id"] = str(d["project_id"]) if d["project_id"] else None
        out.append(d)
    return out


def write_backup(path: str, rows: list[dict]) -> int:
    """Union-merge into any existing backup, keyed on chunk_id, so an earlier
    cleanup's rows are never dropped by a later one."""
    merged: dict[str, dict] = {}
    if os.path.exists(path):
        with _open(path, "rt") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    merged[r["chunk_id"]] = r
    for r in rows:
        merged[r["chunk_id"]] = r
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Keep the .gz suffix on the temp file — _open() picks its opener from the
    # extension, and a plain-text temp renamed to a .gz name is unreadable.
    tmp = path + (".tmp.gz" if path.endswith(".gz") else ".tmp")
    with _open(tmp, "wt") as fh:
        for cid in sorted(merged):
            fh.write(json.dumps(merged[cid], ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return len(merged)


def read_backup(path: str) -> list[dict]:
    with _open(path, "rt") as fh:
        return [json.loads(line) for line in fh if line.strip()]


async def delete_rows(chunk_ids: list[str]) -> int:
    from sqlalchemy import text as sql

    from src.database.postgres import get_session

    total = 0
    async with get_session() as db:
        for i in range(0, len(chunk_ids), 500):
            res = await db.execute(
                sql("DELETE FROM chunk_fulltext WHERE chunk_id = ANY(:ids)"),
                {"ids": chunk_ids[i : i + 500]},
            )
            total += res.rowcount or 0
        await db.commit()
    return total


async def restore_rows(rows: list[dict]) -> int:
    """Re-insert, rebuilding `tsv` with the SAME expression
    fulltext_index.maintain() uses — to_tsvector('simple', <tokenized text>) —
    so a restored row is byte-identical in behaviour to the deleted one."""
    from sqlalchemy import text as sql

    from src.database.postgres import get_session
    from src.ingestion.tokenizer import tokenize

    stmt = sql(
        """
        INSERT INTO chunk_fulltext
            (chunk_id, doc_id, source, project_id, case_id, is_global, text, metadata, tsv, updated_at)
        VALUES
            (:chunk_id, :doc_id, :source, :project_id, :case_id, :is_global, :text,
             CAST(:metadata AS JSONB), to_tsvector('simple', :tokenized), :updated_at)
        ON CONFLICT (chunk_id) DO NOTHING
        """
    )
    n = 0
    async with get_session() as db:
        for r in rows:
            await db.execute(
                stmt,
                {
                    **r,
                    "tokenized": " ".join(tokenize(r["text"] or "")),
                    # asyncpg binds TIMESTAMPTZ from a datetime, never from a
                    # string — the backup stores the ISO form, so parse it back.
                    "updated_at": (
                        datetime.fromisoformat(r["updated_at"]) if r["updated_at"] else None
                    ),
                },
            )
            n += 1
        await db.commit()
    return n


async def amain() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="back up, then delete")
    ap.add_argument("--restore", action="store_true", help="re-insert from the backup")
    ap.add_argument("--backup", default=DEFAULT_BACKUP)
    args = ap.parse_args()

    if args.restore:
        rows = read_backup(args.backup)
        n = await restore_rows(rows)
        print(f"restore: re-inserted {n} row(s) from {args.backup}")
        return 0

    live = chroma_ids()
    all_rows = await fetch_rows()
    print(f"chunk_fulltext rows : {len(all_rows)}")
    print(f"chroma muhafiz_kb   : {len(live)}")

    orphans = [r for r in all_rows if r["chunk_id"] not in live]
    reverse = len(live - {r["chunk_id"] for r in all_rows})
    print(f"orphans (BM25 \\ Chroma) : {len(orphans)}")
    print(f"reverse (Chroma \\ BM25) : {reverse}   <- Module 99's half, NOT deleted here")

    by_source: dict[str, int] = {}
    for r in orphans:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    for s, c in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"    {c:6d}  {s}")

    if not orphans:
        print("nothing to do.")
        return 0
    if not args.apply:
        print("\ndry run — pass --apply to back up and delete.")
        return 0

    kept = write_backup(args.backup, orphans)
    print(f"\nbacked up {len(orphans)} row(s); backup now holds {kept} row(s) at {args.backup}")

    # Re-read the backup and assert every id we are about to delete is in it,
    # before deleting anything.
    saved = {r["chunk_id"] for r in read_backup(args.backup)}
    missing = [r["chunk_id"] for r in orphans if r["chunk_id"] not in saved]
    if missing:
        raise SystemExit(f"ABORT: {len(missing)} row(s) missing from the backup, e.g. {missing[:3]}")

    deleted = await delete_rows([r["chunk_id"] for r in orphans])
    print(f"deleted {deleted} row(s) from chunk_fulltext")

    remaining = await fetch_rows()
    still = [r["chunk_id"] for r in remaining if r["chunk_id"] not in live]
    print(f"post-check: chunk_fulltext rows = {len(remaining)}, orphans remaining = {len(still)}")
    return 0


if __name__ == "__main__":
    _force_utf8_stdout()
    raise SystemExit(asyncio.run(amain()))
