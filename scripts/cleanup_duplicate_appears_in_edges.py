# ============================================================
# One-off data cleanup: remove TRUE duplicate APPEARS_IN edges — same
# source node, same target chunk, AND identical confidence/source_doc_id/
# role. Sibling script to cleanup_duplicate_belongs_to_case_edges.py,
# same shape, different edge type.
#
# WHY THIS EXISTS — found while verifying the duplicate/orphan health
# audit: PERSON-5332c24518 (کاشف, the CNIC-bearing anchor node for
# fir-1001-26) carries 285 active APPEARS_IN edges to ONE narrative chunk
# (psrms_fir_fir-1001-26#narrative_c8bf2613 — note the underscore: the
# sanitized replay namespace, same provenance class the BELONGS_TO_CASE
# duplicates came from). Of those 285, 282 are byte-identical on every
# observed property (confidence=0.75, same source_doc_id, role=None) —
# the same chunk asserting the same mention fact 282 times over, not 282
# distinct pieces of evidence. Two smaller nodes (PERSON-64f765d678,
# PERSON-c908b251cb) each carry one true-duplicate pair alongside a third,
# genuinely distinct edge to a different chunk with a different role
# ("chalaan_witness") — that third edge is real provenance and is never
# touched by this script.
#
# SCOPE — deliberately conservative, only the provably-safe subset, same
# bar as the BELONGS_TO_CASE sibling: a node legitimately has MANY
# APPEARS_IN edges to different chunks (mentioned in several places across
# a case's documents) — that is real, distinct evidentiary provenance and
# is never touched. This only removes edges that are IDENTICAL on every
# one of (source node, target chunk, confidence, source_doc_id, role) —
# which can only happen from a replay, never from two genuinely different
# extractions (a different chunk, or a different role/confidence on the
# same chunk, is preserved).
#
# DELETE, not a SAME_AS-style supersede — APPEARS_IN has no existing
# supersede convention, and keeping exactly one of an identical set loses
# no information: the surviving edge carries the same
# source_doc_id/confidence/role every deleted one did.
#
# Run: python scripts/cleanup_duplicate_appears_in_edges.py --dry-run
#      python scripts/cleanup_duplicate_appears_in_edges.py --apply
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

from src.graph import age_client

_FIND_QUERY = (
    "MATCH (n)-[e:APPEARS_IN]->(ch) WHERE e.superseded_by IS NULL "
    "RETURN id(n) AS node_id, n.entity_id AS entity_id, id(ch) AS chunk_id, "
    "e.confidence AS confidence, e.source_doc_id AS source_doc_id, e.role AS role, "
    "id(e) AS edge_id"
)


async def _find_true_duplicates() -> tuple[list[int], list[dict]]:
    """
    Returns (edge_ids to delete, group summaries for reporting) — every
    edge in an identical (node, chunk, confidence, source_doc_id, role)
    group EXCEPT the first is marked for deletion.
    """
    rows = await age_client.execute_cypher(_FIND_QUERY, columns=[
        "node_id", "entity_id", "chunk_id", "confidence", "source_doc_id", "role", "edge_id",
    ])
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r["node_id"], r["chunk_id"], r["confidence"], r["source_doc_id"], r["role"])
        groups.setdefault(key, []).append(r)

    to_delete: list[int] = []
    summaries: list[dict] = []
    for key, rows_in_group in groups.items():
        if len(rows_in_group) > 1:
            ordered = sorted(rows_in_group, key=lambda r: r["edge_id"])
            to_delete.extend(r["edge_id"] for r in ordered[1:])  # keep the first
            summaries.append({
                "entity_id": ordered[0]["entity_id"],
                "chunk_id": key[1],
                "source_doc_id": key[3],
                "role": key[4],
                "total": len(rows_in_group),
                "removed": len(rows_in_group) - 1,
            })
    return to_delete, summaries


async def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply

    edge_ids, summaries = await _find_true_duplicates()
    print(f"True duplicate APPEARS_IN edges (safe to remove, zero information loss): {len(edge_ids)}")

    if summaries:
        print(f"\nAcross {len(summaries)} duplicate group(s):")
        for s in sorted(summaries, key=lambda s: -s["removed"]):
            print(f"  {s['entity_id']} -> chunk {s['chunk_id']}: {s['total']} edges, "
                  f"removing {s['removed']} (source_doc_id={s['source_doc_id']!r}, role={s['role']!r})")

    if not edge_ids:
        print("Nothing to clean up.")
        return

    if dry_run:
        print(f"\nDRY RUN — no changes made. Re-run with --apply to delete these {len(edge_ids)} edges.")
        return

    await age_client.execute_cypher(
        "MATCH ()-[e:APPEARS_IN]->() WHERE id(e) IN $ids DELETE e",
        params={"ids": edge_ids}, columns=["result"],
    )
    print(f"\nDeleted {len(edge_ids)} true-duplicate edge(s).")


if __name__ == "__main__":
    asyncio.run(main())
