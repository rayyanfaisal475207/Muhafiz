# ============================================================
# One-off backfill: set has_case on every existing Chroma chunk that
# doesn't have it yet.
#
# WHY THIS EXISTS: src/retrieval/vector_store.py's upsert_documents() now
# writes has_case = bool(case_id) on every chunk at ingest time, so the
# "All Cases" retrieval scope (supervisor+, orchestrator.py's
# _build_retrieval_where()) can filter on it -- Chroma's `where` DSL has
# no $exists operator, and case_id itself is dropped entirely from a
# chunk's metadata when it's None, which rules out $ne too. Every chunk
# upserted BEFORE this change has no has_case field at all -- this script
# derives it from that chunk's own already-stored case_id and writes it
# back.
#
# Read-and-write against Chroma directly (collection.update()), not a
# re-ingest -- text/embeddings/every other metadata field are untouched.
#
# Idempotent: only ever targets chunks with no has_case field yet, so a
# second run after upsert_documents() has covered the rest is a no-op.
#
# Run: python scripts/backfill_has_case_metadata.py --dry-run
#      python scripts/backfill_has_case_metadata.py --apply
# ============================================================

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src import config
from src.retrieval.vector_store import ChromaVectorStore

_COLLECTIONS = ("muhafiz_kb", "muhafiz_community_reports", "muhafiz_entity_descriptions")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Only report what would be backfilled.")
    parser.add_argument("--apply", action="store_true", help="Actually write has_case values.")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print("Pass --dry-run to preview or --apply to actually backfill.")
        return

    client = ChromaVectorStore.get_instance()._client  # noqa: SLF001 -- one-off script, direct client access

    total = 0
    for name in _COLLECTIONS:
        try:
            col = client.get_collection(name)
        except Exception:
            print(f"{name}: collection does not exist, skipping")
            continue

        # has_case is never dropped once written (always a real bool, same
        # as is_global) -- a chunk missing it entirely predates this change.
        all_rows = col.get(include=["metadatas"])
        missing_ids = [
            id_ for id_, meta in zip(all_rows["ids"], all_rows["metadatas"])
            if "has_case" not in (meta or {})
        ]
        print(f"{name}: {len(missing_ids)} chunk(s) missing has_case (of {len(all_rows['ids'])} total)")
        total += len(missing_ids)

        if not missing_ids or args.dry_run:
            continue

        # Re-fetch case_id for exactly the chunks being updated, batched --
        # avoids holding two full-collection metadata copies in memory at once.
        batch_size = 500
        for i in range(0, len(missing_ids), batch_size):
            batch_ids = missing_ids[i:i + batch_size]
            rows = col.get(ids=batch_ids, include=["metadatas"])
            updates = [
                {**(meta or {}), "has_case": bool((meta or {}).get("case_id"))}
                for meta in rows["metadatas"]
            ]
            col.update(ids=rows["ids"], metadatas=updates)
        print(f"  backfilled {len(missing_ids)} chunk(s)")

    if args.dry_run:
        print(f"\nDRY RUN -- {total} chunk(s) would be backfilled. Re-run with --apply to actually write.")
    else:
        print(f"\nDone -- {total} chunk(s) backfilled.")


if __name__ == "__main__":
    main()
