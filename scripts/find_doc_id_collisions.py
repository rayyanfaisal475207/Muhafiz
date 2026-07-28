"""
One-off audit: find doc_ids with divergent case_id/project_id history
between ChromaDB and Postgres (Phase 4, Module 4.1 — see solution.md and
issues.md's "doc_id/chunk-id collisions across different cases silently
overwrite one case's evidence with another's in Chroma" / "Postgres
insert_documents uses ON CONFLICT (doc_id) DO NOTHING" findings).

Module 4.1 folds case_id/project_id into the doc_id hash seed and changes
Postgres's insert to ON CONFLICT DO UPDATE, so a same-doc_id collision going
forward can only happen for a genuine same-case re-ingest. This script does
NOT fix anything — it is read-only — and exists to find documents that were
ALREADY silently overwritten before that fix landed, so a human can decide
which case's data (if any) is authoritative for each one and re-ingest it
under a corrected, case-scoped doc_id.

Chroma has no version history, so "collision" here means two different
things, both worth flagging:

1. Within Chroma itself: chunks currently sharing one doc_id but disagreeing
   on case_id — the tail end of the collision (Case B's last write partially
   or fully overwrote Case A's chunks under the same id).
2. Between Chroma and Postgres: a doc_id whose Postgres row's case_id
   disagrees with the case_id its Chroma chunks actually carry — the
   ON CONFLICT DO NOTHING bug, where Postgres kept stale ownership while
   Chroma's unconditional upsert already moved on.

Usage:
    python scripts/find_doc_id_collisions.py

Read-only: makes no writes to either store. Requires a live Postgres
connection (DirectGateway backend) and a live ChromaDB collection.
"""
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> None:
    from sqlalchemy import select
    from src.database.postgres import get_session
    from src.database.models import Document as DocumentModel
    from src.retrieval.vector_store import ChromaVectorStore

    # 1. Postgres: doc_id -> (case_id, project_id, is_global)
    async with get_session() as db:
        res = await db.execute(select(DocumentModel))
        pg_docs = {
            d.doc_id: (d.case_id, str(d.project_id) if d.project_id else None, bool(d.is_global))
            for d in res.scalars().all()
        }
    print(f"Postgres: {len(pg_docs)} document row(s).")

    # 2. Chroma: group every chunk's metadata by its parent doc_id.
    store = ChromaVectorStore.get_instance()
    metadatas = store.get_all_metadata()
    chroma_by_doc: dict[str, list[dict]] = defaultdict(list)
    for meta in metadatas:
        doc_id = meta.get("doc_id")
        if doc_id:
            chroma_by_doc[doc_id].append(meta)
    print(f"Chroma: {len(metadatas)} chunk(s) across {len(chroma_by_doc)} doc_id(s).")

    internal_collisions = []
    cross_store_divergence = []

    for doc_id, chunks in chroma_by_doc.items():
        case_ids = {c.get("case_id") for c in chunks}
        project_ids = {c.get("project_id") for c in chunks}

        # 1. Chroma-internal disagreement: this doc_id's own chunks don't
        #    all agree on case_id (or project_id).
        if len(case_ids) > 1 or len(project_ids) > 1:
            internal_collisions.append((doc_id, case_ids, project_ids, len(chunks)))

        # 2. Cross-store disagreement: Postgres's row for this doc_id
        #    doesn't match what Chroma's chunks actually carry.
        pg_row = pg_docs.get(doc_id)
        if pg_row is not None:
            pg_case_id, pg_project_id, _ = pg_row
            chroma_case_id = next(iter(case_ids)) if len(case_ids) == 1 else None
            chroma_project_id = next(iter(project_ids)) if len(project_ids) == 1 else None
            if chroma_case_id != pg_case_id or chroma_project_id != pg_project_id:
                cross_store_divergence.append(
                    (doc_id, pg_case_id, pg_project_id, chroma_case_id, chroma_project_id)
                )

    print()
    print(f"=== Chroma-internal case_id/project_id disagreement: {len(internal_collisions)} doc_id(s) ===")
    for doc_id, case_ids, project_ids, n_chunks in internal_collisions:
        print(f"  {doc_id}  ({n_chunks} chunks)  case_ids={case_ids}  project_ids={project_ids}")

    print()
    print(f"=== Chroma vs Postgres case_id/project_id divergence: {len(cross_store_divergence)} doc_id(s) ===")
    for doc_id, pg_case, pg_proj, ch_case, ch_proj in cross_store_divergence:
        print(
            f"  {doc_id}  Postgres(case_id={pg_case!r}, project_id={pg_proj!r})"
            f"  Chroma(case_id={ch_case!r}, project_id={ch_proj!r})"
        )

    total_flagged = len({d for d, *_ in internal_collisions} | {d for d, *_ in cross_store_divergence})
    print()
    print(f"Total distinct doc_id(s) flagged for manual review: {total_flagged}")
    if total_flagged:
        print(
            "These require a human decision about which case's data is authoritative "
            "and, if needed, re-ingestion under a corrected (post-Module-4.1) doc_id. "
            "This script makes no writes."
        )


if __name__ == "__main__":
    asyncio.run(main())
