# ============================================================
# One-off refresh: re-run graph extraction for documents whose
# APPEARS_IN/ASSOCIATED_WITH edges point at chunk_ids that no longer exist
# in the current Chroma collection.
#
# ROOT CAUSE (found during a post-OPEN_GAPS_FIX_PROMPT.md audit,
# 2026-08-07): Chroma was reset/rebuilt at some point after the initial
# corpus ingestion, and only later batches were re-ingested into the fresh
# collection. The Apache AGE graph is deliberately append-only/versioned
# (see versioning.py's own docstring) and was never reset alongside it, so
# it still carries provenance pointing at chunk text that no longer
# resolves. Confirmed this is NOT content loss: every affected document's
# text is still fully present in Chroma under the SAME parent doc_id, just
# re-chunked with slightly different boundaries since the graph data was
# last extracted (e.g. 6 stale chunk refs vs. 7 current chunks for the
# same document) -- see this script's own dry-run output for the full
# accounting.
#
# WHAT THIS DOES: re-runs ingest_file() (load -> chunk -> embed -> upsert
# -> graph extraction) for each affected file, using its ORIGINAL case_id/
# is_global scope (read from the `documents` table). Chunking/embedding is
# deterministic (Document.doc_id hashes scope+source+page+text), so this
# is expected to reproduce the SAME chunk ids already in Chroma (an
# idempotent upsert, not a duplicate) -- the actual point is the graph-
# extraction step re-running against those CURRENT chunk ids, appending
# fresh Person/entity/ASSOCIATED_WITH data with valid provenance. The old,
# stale-chunk-id graph data is NOT deleted (append-only by design, per
# versioning.py) -- it just becomes additional superseded-by-nothing
# history that graph_retriever.py already silently skips when its chunk
# is missing.
#
# This makes real LLM calls (NER low-confidence adjudication, domain-
# entities, relationship extraction) -- NOT free, NOT instant. Budget
# accordingly; see --limit for a pilot run before doing the rest.
#
# Run: python scripts/refresh_stale_graph_provenance.py --dry-run
#      python scripts/refresh_stale_graph_provenance.py --limit 5
#      python scripts/refresh_stale_graph_provenance.py            (all)
# ============================================================

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from sqlalchemy import text

from src import config
from src.database.postgres import get_session
from src.graph import age_client
from src.ingestion.service import ingest_file
from src.retrieval.vector_store import get_chunks_by_ids


async def find_affected_doc_ids() -> list[str]:
    """Parent doc_ids with at least one APPEARS_IN edge whose
    source_chunk_id no longer resolves in the current Chroma collection."""
    rows = await age_client.execute_cypher(
        "MATCH ()-[r:APPEARS_IN]->() WHERE r.superseded_by IS NULL "
        "RETURN DISTINCT r.source_chunk_id AS chunk_id, r.source_doc_id AS doc_id",
        columns=["chunk_id", "doc_id"],
    )
    chunk_to_doc = {r["chunk_id"]: r["doc_id"] for r in rows if r["chunk_id"]}
    ids = list(chunk_to_doc.keys())
    found = await get_chunks_by_ids(ids)
    found_ids = {c["id"] for c in found}
    missing = [i for i in ids if i not in found_ids]
    return sorted({chunk_to_doc[c] for c in missing})


async def fetch_scope(doc_ids: list[str]) -> list[dict]:
    async with get_session() as db:
        res = await db.execute(
            text("SELECT doc_id, filename, is_global, case_id, project_id FROM documents WHERE doc_id = ANY(:ids)"),
            {"ids": doc_ids},
        )
        return [dict(row) for row in res.mappings()]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Only report what would be refreshed.")
    parser.add_argument("--limit", type=int, default=None, help="Refresh only the first N affected documents (a pilot run).")
    args = parser.parse_args()

    affected = await find_affected_doc_ids()
    print(f"Documents with stale graph provenance: {len(affected)}")

    rows = await fetch_scope(affected)
    unmatched = set(affected) - {r["doc_id"] for r in rows}
    if unmatched:
        print(f"  WARNING: {len(unmatched)} affected doc_ids have no row in `documents` "
              f"(can't determine scope, skipping): {sorted(unmatched)}")

    if args.limit is not None:
        rows = rows[: args.limit]
        print(f"Limiting to first {len(rows)} for this run.")

    for r in rows:
        print(f"  {r['doc_id']}: {r['filename']} (case_id={r['case_id']}, is_global={r['is_global']})")

    if args.dry_run:
        print("\nDRY RUN -- no ingestion performed. Re-run with --limit N or with neither flag to actually refresh.")
        return

    print(f"\nRefreshing {len(rows)} document(s)...")
    succeeded, failed = 0, []
    for r in rows:
        file_path = config.DOCUMENTS_DIR / r["filename"]
        if not file_path.exists():
            print(f"  [{r['filename']}] SKIPPED — file not found on disk at {file_path}")
            failed.append(r["filename"])
            continue
        try:
            stats = await ingest_file(
                file_path, is_global=bool(r["is_global"]), case_id=r["case_id"], project_id=r["project_id"],
            )
            # ingest_file() degrades to a stats dict with an "error" key on
            # an infra failure (e.g. the embedding endpoint unreachable)
            # rather than raising -- confirmed live (2026-08-07): an ngrok
            # tunnel outage produced chunks_added=0 + stats["error"] set,
            # and this loop originally only checked for a raised exception,
            # so it printed "OK" and counted it as succeeded anyway.
            if stats.get("error"):
                print(f"  [{r['filename']}] FAILED — {stats['error']}")
                failed.append(r["filename"])
                continue
            print(f"  [{r['filename']}] OK — {stats}")
            succeeded += 1
        except Exception as exc:
            print(f"  [{r['filename']}] FAILED — {exc}")
            failed.append(r["filename"])

    print(f"\nDone. Succeeded: {succeeded} | Failed: {len(failed)}")
    if failed:
        print("Failed files:", failed)

    remaining = await find_affected_doc_ids()
    print(f"\nDocuments still with stale provenance after this run: {len(remaining)}")


if __name__ == "__main__":
    asyncio.run(main())
