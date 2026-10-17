"""Build the semantic index the planner can choose to use.

Semantic retrieval is an OPTION, not a stage every question pays for. The
planner reaches for `search` when a question is about what the records
SAY; counts and traversals never touch this index. So the point of
building it is to make that option real -- today `search` is
`websearch_to_tsquery` and nothing else, which is why a question about
"reasons for delays in reporting" landed on statute text about cattle
pounds.

Resumable by design: ids already in the collection are skipped, so an
interrupted run continues rather than restarting. Embedding 12,000+ spans
one HTTP call at a time takes a while, and a run that cannot resume will
never finish.

Uses ONLY the configured local endpoint. No cloud fallback.

    python scripts/evidence_index_vectors.py --dry-run
    python scripts/evidence_index_vectors.py
    python scripts/evidence_index_vectors.py --endpoint fir
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.evidence.vectors import COLLECTION, client, embed  # noqa: E402


async def spans_to_index(conn, endpoint: str | None):
    """Every span of a current document, newest revision only."""
    return await conn.fetch(
        """SELECT s.span_id, s.text, s.document_id,
                  coalesce(d.source_metadata->>'endpoint','legacy-import') AS src
           FROM evidence.spans s
           JOIN evidence.documents d ON d.document_id = s.document_id
           WHERE NOT EXISTS(SELECT 1 FROM evidence.documents n
                            WHERE n.supersedes = d.document_id)
             AND length(trim(s.text)) > 20
             AND ($1::text IS NULL
                  OR coalesce(d.source_metadata->>'endpoint','legacy-import') = $1)
           ORDER BY s.span_id""", endpoint)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--endpoint", help="index one source kind only")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = await asyncpg.connect(os.getenv("EVIDENCE_INGEST_DATABASE_URL"))
    for name in ("json", "jsonb"):
        await conn.set_type_codec(name, encoder=json.dumps, decoder=json.loads,
                                  schema="pg_catalog")
    rows = await spans_to_index(conn, args.endpoint)
    await conn.close()

    collection = await asyncio.to_thread(
        lambda: client().get_or_create_collection(COLLECTION,
                                                  embedding_function=None))
    existing = set()
    try:
        got = await asyncio.to_thread(collection.get, include=[])
        existing = set(got.get("ids") or [])
    except Exception:                                   # noqa: BLE001
        pass

    todo = [r for r in rows if str(r["span_id"]) not in existing]
    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(rows)} span(s) eligible, {len(existing)} already indexed, "
          f"{len(todo)} to embed")
    if args.dry_run or not todo:
        return

    done = failed = 0
    started = time.monotonic()
    for row in todo:
        try:
            vector = await embed(row["text"], query=False)
        except Exception as exc:                        # noqa: BLE001
            failed += 1
            if failed <= 3:
                print(f"  embed failed ({type(exc).__name__}); continuing")
            if failed > 50:
                print("  too many embedding failures; stopping so the run "
                      "can be resumed once the endpoint is healthy")
                break
            continue
        await asyncio.to_thread(
            collection.upsert, ids=[str(row["span_id"])], embeddings=[vector],
            metadatas=[{"document_id": str(row["document_id"]),
                        "source": row["src"]}])
        done += 1
        if done % 200 == 0:
            rate = done / max(time.monotonic() - started, 1e-6)
            left = (len(todo) - done) / max(rate, 1e-6)
            print(f"  {done}/{len(todo)}  {rate:.1f}/s  ~{left/60:.0f} min left",
                  flush=True)

    print(f"\nindexed {done}, failed {failed}, "
          f"{len(todo)-done-failed} not attempted")


if __name__ == "__main__":
    asyncio.run(main())
