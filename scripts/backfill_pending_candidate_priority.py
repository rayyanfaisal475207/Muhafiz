# ============================================================
# One-off backfill: insert a pending_candidate_priority row (migration
# 027) for every PENDING SAME_AS edge that doesn't already have one.
#
# WHY THIS EXISTS — a real, measured gap, not a hypothetical:
# pending_candidate_priority.py's own header states the invariant "a
# row's existence always mirrors a real pending edge in AGE (inserted
# the moment one is written)" — maintain_pending() is called from
# versioning.write_edge()'s own write path, and that is the ONLY place a
# row gets created. Any pending SAME_AS edge written before D1
# (Milestone D1, GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md) landed, or written
# through any path that predates that hook, has no row — and
# candidate_reprioritization.py's own reprioritize_all() only re-scores
# rows that ALREADY exist in the side table (it lists edge_ids FROM
# pending_candidate_priority, not from AGE), so it can never discover or
# backfill a missing one on its own.
#
# Measured live: 2,162 pending SAME_AS edges in AGE, only 373 rows in
# pending_candidate_priority — 83% of the queue was invisible to D1's
# own batch-grouping/reprioritization machinery entirely.
#
# THIS SCRIPT NEVER SCORES ANYTHING — it inserts rows with NULL
# priority_score/group_id/why, using maintain_pending()'s own idempotent
# INSERT ... ON CONFLICT DO NOTHING (identical INSERT shape
# versioning.write_edge() already uses, just replayed for edges that
# never got it the first time). Run candidate_reprioritization.
# reprioritize_all() (or POST /api/admin/graph-review/queue/reprioritize)
# AFTER this to actually score/group the newly-backfilled rows — kept as
# two separate steps, matching D1's own "reprioritization never inserts,
# only maintain_pending() does" discipline stated in that module's header.
#
# Read AGE + Postgres, write only to the side table — never touches a
# SAME_AS edge itself, never confirms/rejects anything. Safe to re-run;
# already-tracked edges are skipped via ON CONFLICT DO NOTHING.
#
# Run: python scripts/backfill_pending_candidate_priority.py --dry-run
#      python scripts/backfill_pending_candidate_priority.py --apply
# ============================================================

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.graph import age_client, pending_candidate_priority
from sqlalchemy import text
from src.database.postgres import get_session

_PENDING_QUERY = (
    "MATCH (a)-[r:SAME_AS]->(b) WHERE r.status = 'pending' AND r.superseded_by IS NULL "
    "RETURN id(r) AS edge_id, a.entity_id AS a_id, b.entity_id AS b_id, "
    "r.tier AS tier, r.confidence AS confidence, r.basis AS basis, "
    "r.name_similarity AS name_similarity, r.shared_case AS shared_case, "
    "r.shared_structured_id AS shared_structured_id, r.source_doc_id AS source_doc_id"
)


async def _already_tracked_edge_ids() -> set[int]:
    async with get_session() as db:
        result = await db.execute(text("SELECT edge_id FROM pending_candidate_priority WHERE edge_label = 'SAME_AS'"))
        return {row[0] for row in result.fetchall()}


async def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply

    pending = await age_client.execute_cypher(
        _PENDING_QUERY,
        columns=["edge_id", "a_id", "b_id", "tier", "confidence", "basis",
                 "name_similarity", "shared_case", "shared_structured_id", "source_doc_id"],
    )
    tracked = await _already_tracked_edge_ids()
    missing = [r for r in pending if r["edge_id"] not in tracked]

    print(f"Pending SAME_AS edges in AGE: {len(pending)}")
    print(f"Already tracked in pending_candidate_priority: {len(tracked)}")
    print(f"Missing (will be backfilled): {len(missing)}")

    if not missing:
        print("Nothing to backfill.")
        return

    if dry_run:
        print("\nDRY RUN — no changes made. Re-run with --apply to insert these rows.")
        return

    inserted = 0
    for row in missing:
        try:
            await pending_candidate_priority.maintain_pending(
                row["edge_id"], "SAME_AS",
                a_key=str(row["a_id"]), b_key=str(row["b_id"]),
                tier=row["tier"], confidence=row["confidence"], basis=row["basis"],
                name_similarity=row["name_similarity"], shared_case=row["shared_case"],
                shared_structured_id=row["shared_structured_id"], source_doc_id=row["source_doc_id"],
            )
            inserted += 1
        except Exception as exc:
            print(f"  FAILED for edge_id={row['edge_id']}: {exc}")

    print(f"\nBackfilled {inserted}/{len(missing)} row(s).")
    print("Next step: run candidate_reprioritization.reprioritize_all() (or "
          "POST /api/admin/graph-review/queue/reprioritize) to score and group them.")


if __name__ == "__main__":
    asyncio.run(main())
