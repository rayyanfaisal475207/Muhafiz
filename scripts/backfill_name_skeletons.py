# ============================================================
# One-off backfill: set name_skeleton on every existing Person/Officer/
# Organization node that doesn't have one yet.
#
# WHY THIS EXISTS: entity_resolution.resolve_and_write() now precomputes
# name_skeleton (a coarse Roman-Urdu <-> Urdu-script phonetic reduction,
# the same _consonant_skeleton() function ingestion-time merge decisions
# already use) at write time, so graph_retriever._find_seed_nodes() can
# match a node by name regardless of which script its canonical_name
# ended up in. Every node written BEFORE that change has no
# name_skeleton property at all -- this script fills that in for the
# existing graph, using only a value already stored (canonical_name),
# never touching evidence or provenance.
#
# Deliberately NOT routed through versioning.write_node(): that call
# also stamps as_of/confidence/source_doc_id, which are evidentiary
# provenance fields -- this is a derived-index backfill, not new
# evidence, so it sets name_skeleton directly via a raw Cypher SET and
# leaves every other property (including provenance) untouched.
#
# Idempotent: only ever targets nodes where name_skeleton IS NULL, so a
# second run after resolve_and_write() has covered the rest is a no-op.
#
# Run: python scripts/backfill_name_skeletons.py --dry-run
#      python scripts/backfill_name_skeletons.py --apply
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

from src.graph import age_client
from src.graph.entity_resolution import _consonant_skeleton

_LABELS = ("Person", "Officer", "Organization")


async def find_nodes_missing_skeleton(label: str) -> list[dict]:
    rows = await age_client.execute_cypher(
        f"MATCH (n:{label}) WHERE n.canonical_name IS NOT NULL AND n.name_skeleton IS NULL "
        "RETURN n.entity_id AS entity_id, n.canonical_name AS canonical_name",
        columns=["entity_id", "canonical_name"],
    )
    return [r for r in rows if r.get("entity_id") and r.get("canonical_name")]


async def set_name_skeleton(label: str, entity_id: str, skeleton: str) -> None:
    await age_client.execute_cypher(
        f"MATCH (n:{label} {{entity_id: $entity_id}}) SET n.name_skeleton = $skeleton RETURN n",
        params={"entity_id": entity_id, "skeleton": skeleton},
        columns=["n"],
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Only report what would be backfilled.")
    parser.add_argument("--apply", action="store_true", help="Actually write name_skeleton values.")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print("Pass --dry-run to preview or --apply to actually backfill.")
        return

    total = 0
    for label in _LABELS:
        rows = await find_nodes_missing_skeleton(label)
        print(f"{label}: {len(rows)} node(s) missing name_skeleton")
        total += len(rows)

        if args.dry_run:
            for r in rows[:10]:
                skeleton = _consonant_skeleton(r["canonical_name"])
                print(f"  {r['entity_id']}: {r['canonical_name']!r} -> {skeleton!r}")
            if len(rows) > 10:
                print(f"  ... and {len(rows) - 10} more")
            continue

        for r in rows:
            skeleton = _consonant_skeleton(r["canonical_name"])
            await set_name_skeleton(label, r["entity_id"], skeleton)
        print(f"  backfilled {len(rows)} node(s)")

    if args.dry_run:
        print(f"\nDRY RUN -- {total} node(s) would be backfilled. Re-run with --apply to actually write.")
    else:
        print(f"\nDone -- {total} node(s) backfilled.")


if __name__ == "__main__":
    asyncio.run(main())
