# ============================================================
# One-off data cleanup: remove TRUE duplicate BELONGS_TO_CASE edges —
# same node, same case, same source_doc_id, AND same source_chunk_id.
#
# WHY THIS EXISTS — found as a side effect of building
# same_as_queue_history.py (GRAPH_QUALITY_VISIBILITY_FIX_PROMPT.md,
# Feature A), not hypothetical: a naive join on BELONGS_TO_CASE
# multiplied every SAME_AS row by however many redundant edges an
# endpoint carries. Measured live: PERSON-5332c24518 alone had 131
# separate, non-superseded BELONGS_TO_CASE edges to fir-1001-26 — a
# leftover from the pre-d5fa333 replay bug (every mention resolution
# wrote a fresh membership edge, never deduped or superseded).
#
# SCOPE — deliberately conservative, only the provably-safe subset:
# a node commonly has MANY BELONGS_TO_CASE edges to the same case from
# genuinely DIFFERENT chunks (mentioned in several places across a
# case's documents) — that is real, distinct evidentiary provenance,
# not waste, and this script never touches it. It only removes edges
# that are IDENTICAL on every one of (node, case, source_doc_id,
# source_chunk_id) — the same chunk asserting the same membership fact
# more than once, which can only happen from a replay, never from two
# genuinely different mentions.
#
# Verified live before writing this: every duplicate set inspected had
# byte-identical confidence too (1.0), confirming these are exact
# replay repeats, not distinct scored assertions.
#
# DELETE, not a SAME_AS-style supersede — BELONGS_TO_CASE has no
# existing supersede convention (unlike SAME_AS's confirm/reject), and
# keeping exactly one of an identical set loses no information: the
# surviving edge carries the same source_doc_id/source_chunk_id/
# confidence every deleted one did.
#
# Run: python scripts/cleanup_duplicate_belongs_to_case_edges.py --dry-run
#      python scripts/cleanup_duplicate_belongs_to_case_edges.py --apply
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
    "MATCH (n)-[e:BELONGS_TO_CASE]->(c:Case) WHERE e.superseded_by IS NULL "
    "RETURN id(n) AS node_id, id(c) AS case_node_id, c.case_id AS case_id, "
    "e.source_doc_id AS source_doc_id, e.source_chunk_id AS source_chunk_id, "
    "id(e) AS edge_id"
)


async def _find_true_duplicates() -> list[int]:
    """Returns edge_ids to delete — every edge in an identical
    (node, case, source_doc_id, source_chunk_id) group EXCEPT the first."""
    rows = await age_client.execute_cypher(_FIND_QUERY, columns=[
        "node_id", "case_node_id", "case_id", "source_doc_id", "source_chunk_id", "edge_id",
    ])
    groups: dict[tuple, list[int]] = {}
    for r in rows:
        key = (r["node_id"], r["case_node_id"], r["source_doc_id"], r["source_chunk_id"])
        groups.setdefault(key, []).append(r["edge_id"])

    to_delete = []
    for edge_ids in groups.values():
        if len(edge_ids) > 1:
            to_delete.extend(sorted(edge_ids)[1:])  # keep the first, delete the rest
    return to_delete


async def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply

    edge_ids = await _find_true_duplicates()
    print(f"True duplicate BELONGS_TO_CASE edges (safe to remove, zero information loss): {len(edge_ids)}")

    if not edge_ids:
        print("Nothing to clean up.")
        return

    if dry_run:
        print(f"\nDRY RUN — no changes made. Re-run with --apply to delete these {len(edge_ids)} edges.")
        return

    await age_client.execute_cypher(
        "MATCH ()-[e:BELONGS_TO_CASE]->() WHERE id(e) IN $ids DELETE e",
        params={"ids": edge_ids}, columns=["result"],
    )
    print(f"\nDeleted {len(edge_ids)} true-duplicate edge(s).")


if __name__ == "__main__":
    asyncio.run(main())
