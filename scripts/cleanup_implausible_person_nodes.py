# ============================================================
# One-off data cleanup: remove Person nodes written before
# src/extraction/ner.py's form-label-mistagging fix (see git history —
# "Fix NER mistagging English form-field labels as Person entities").
#
# That fix only prevents FUTURE bad extractions. This script removes the
# already-written residue using the exact same plausibility filter
# community_detection.py already applies at the community-analysis layer
# (src/graph/community_detection.py::_is_plausible_person_name), so the
# definition of "implausible" is identical to what's already been
# reviewed and verified live — no new heuristic invented for this script.
#
# Precedented in this repo's own history: commit 3fb031d ("Fix router
# misclassification and XAGG cross-case aggregate pipeline") did the same
# kind of scoped, one-off DETACH DELETE for garbage Vehicle nodes found
# during that fix.
#
# DETACH DELETE removes the node and every edge touching it
# (APPEARS_IN, BELONGS_TO_CASE, SAME_AS, ASSOCIATED_WITH) — confirmed via
# a single-node test before running this against the full set. This is a
# real, destructive, hard-to-reverse graph mutation — run --dry-run first
# and read the report before running for real.
#
# Run: python scripts/cleanup_implausible_person_nodes.py --dry-run
#      python scripts/cleanup_implausible_person_nodes.py --apply
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
from src.graph.community_detection import (
    _compute_prefix_contaminated_names,
    _is_plausible_person_name,
    fetch_known_police_stations,
    fetch_person_names,
)


async def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply

    names = await fetch_person_names()
    stations = await fetch_known_police_stations()
    contaminated = _compute_prefix_contaminated_names(names)
    bad = {
        eid: name for eid, name in names.items()
        if not _is_plausible_person_name(name, stations) or name in contaminated
    }

    print(f"Total Person nodes: {len(names)}")
    print(f"Implausible (to be removed): {len(bad)}")
    print("\nSample (first 20):")
    for eid, name in list(bad.items())[:20]:
        print(f"  {eid}: {name!r}")

    if not bad:
        print("\nNothing to remove.")
        return

    if dry_run:
        print(f"\nDRY RUN — no changes made. Re-run with --apply to actually delete these {len(bad)} nodes.")
        return

    print(f"\nApplying: DETACH DELETE on {len(bad)} nodes...")
    result = await age_client.execute_cypher(
        "MATCH (p:Person) WHERE p.entity_id IN $ids DETACH DELETE p RETURN count(p) AS deleted",
        params={"ids": list(bad.keys())},
        columns=["deleted"],
    )
    print(f"Delete call result: {result}")

    remaining = await fetch_person_names()
    remaining_contaminated = _compute_prefix_contaminated_names(remaining)
    still_bad = {
        eid: name for eid, name in remaining.items()
        if not _is_plausible_person_name(name, stations) or name in remaining_contaminated
    }
    print(f"\nPerson nodes remaining: {len(remaining)}")
    print(f"Still-implausible remaining (should be 0): {len(still_bad)}")
    if still_bad:
        print("  (unexpected — investigate before assuming cleanup is complete)")
        for eid, name in list(still_bad.items())[:10]:
            print(f"  {eid}: {name!r}")


if __name__ == "__main__":
    asyncio.run(main())
