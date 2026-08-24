# ============================================================
# One-off data cleanup: remove Person nodes with zero remaining edges.
#
# MODULE1_GAPS_FIX_PROMPT.md Priority 2a — every Person node this codebase
# ever writes gets at least BELONGS_TO_CASE + APPEARS_IN in the SAME write
# (_write_new_person()/entity_resolution.resolve_and_write()'s TIER_NEW
# path — see structured_projection.py's own docstring), so a Person node
# with literally zero edges can only exist because a LATER re-sync's
# purge_edges_by_source_prefix() (scripts/sync_muhafiz_data.py) deleted
# every edge that used to connect it, without deleting the node itself —
# purging is edge-only by design (see that function's own module
# docstring). This is the exact scenario findings.md Module 1's own live
# verification produced on fir-233-26 (PERSON-053c80c11a) and its Priority
# 1 follow-up produced on fir-97-26 (PERSON-104ea1dafd): re-syncing a
# no-CNIC person mints a brand-new random entity_id every time
# (entity_resolution._new_entity_id() has no dedup), stranding the
# previous run's node.
#
# Same precedented shape as scripts/cleanup_implausible_person_nodes.py:
# a small one-off script, --dry-run reports what would be deleted and why,
# --apply does the real DETACH DELETE. This is a real, destructive,
# hard-to-reverse graph mutation — run --dry-run first, read the report,
# get explicit go-ahead before --apply.
#
# SCOPE: deliberately narrow — checks only the two specific entity_ids
# this session's own live verification is known to have orphaned, NOT a
# full-corpus 0-edge sweep. A full-corpus scan was tried multiple times
# and multiple ways while building the automatic cleanup this script now
# shares logic with (scripts/sync_muhafiz_data.py's
# purge_orphaned_person_nodes_by_source_prefix() — see that function's own
# docstring for the full story: one combined query reliably dropped the
# AGE connection, a per-node unbounded OPTIONAL MATCH didn't error but
# hung for 25+ real seconds on a SINGLE node, caught live via
# pg_stat_activity). MODULE1_GAPS_FIX_PROMPT.md's Priority 2b already
# flags a real, wider-corpus orphan population as a SEPARATE, broader
# design question (structural fix vs. a proper indexed/batched sweep) —
# this script intentionally does not attempt that here. In practice, most
# orphans now get cleaned up automatically the next time their own record
# is re-synced (sync_fir/sync_cms/sync_pkm call the shared cleanup
# directly) — this script exists for whatever's left outside that flow.
_KNOWN_ORPHANED_IDS = [
    "PERSON-053c80c11a",  # findings.md Module 1's own live verification (fir-233-26, حمزہ طارق) — already cleaned up by a later re-sync, kept here for the historical record
    "PERSON-104ea1dafd",  # MODULE1_GAPS_FIX_PROMPT.md Priority 1's own live verification (fir-97-26, نازیہ کوثر (بیک وقت مدعیہ))
]
#
# Run: python scripts/cleanup_orphaned_person_nodes.py --dry-run
#      python scripts/cleanup_orphaned_person_nodes.py --apply
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

from scripts.sync_muhafiz_data import _person_has_any_edge
from src.graph import age_client


async def _fetch_orphaned_person_nodes() -> dict[str, dict]:
    """
    Checks only `_KNOWN_ORPHANED_IDS` — reuses
    sync_muhafiz_data._person_has_any_edge()'s per-EDGE_LABEL existence
    check (never the unbounded any-label OPTIONAL MATCH that hung live)
    to confirm each id is genuinely edgeless before reporting it — never
    assumes the id list above is still accurate.
    """
    found: dict[str, dict] = {}
    for eid in _KNOWN_ORPHANED_IDS:
        if await _person_has_any_edge(eid):
            continue
        rows = await age_client.execute_cypher(
            "MATCH (p:Person {entity_id: $eid}) "
            "RETURN p.entity_id AS entity_id, p.canonical_name AS name, "
            "p.source_doc_id AS source_doc_id, p.as_of AS as_of",
            params={"eid": eid}, columns=["entity_id", "name", "source_doc_id", "as_of"],
        )
        for r in rows:
            if r["entity_id"]:
                found[r["entity_id"]] = r
    return found


async def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply

    orphaned = await _fetch_orphaned_person_nodes()

    print(f"Orphaned Person nodes (zero edges): {len(orphaned)}")
    for eid, row in orphaned.items():
        print(f"  {eid}: {row['name']!r}  source_doc_id={row['source_doc_id']!r}  as_of={row['as_of']}")

    if not orphaned:
        print("\nNothing to remove.")
        return

    if dry_run:
        print(f"\nDRY RUN — no changes made. Re-run with --apply to actually delete these {len(orphaned)} nodes.")
        return

    print(f"\nApplying: DETACH DELETE on {len(orphaned)} nodes...")
    result = await age_client.execute_cypher(
        "MATCH (p:Person) WHERE p.entity_id IN $ids DETACH DELETE p RETURN count(p) AS deleted",
        params={"ids": list(orphaned.keys())},
        columns=["deleted"],
    )
    print(f"Delete call result: {result}")

    remaining = await _fetch_orphaned_person_nodes()
    print(f"\nOrphaned Person nodes remaining (should be 0): {len(remaining)}")
    if remaining:
        print("  (unexpected — investigate before assuming cleanup is complete)")
        for eid, row in list(remaining.items())[:10]:
            print(f"  {eid}: {row['name']!r}")


if __name__ == "__main__":
    asyncio.run(main())
