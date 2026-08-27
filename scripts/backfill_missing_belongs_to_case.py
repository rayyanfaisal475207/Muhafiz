# ============================================================
# Backfill missing BELONGS_TO_CASE edges — Officer and Vehicle nodes whose
# case-scoping edge was never written (2026-08-27 route sweep, BUG-3 in
# testingbugs.md).
#
# ROOT CAUSE, CONFIRMED BEFORE WRITING THIS SCRIPT (not assumed):
# a fresh run of the live ingestion code (structured_projection.project_fir(),
# writing to the physically separate evidence_graph_eval graph, never
# touching production) correctly writes BELONGS_TO_CASE for a fresh Officer
# node every time — entity_resolution.resolve_and_write() writes it
# unconditionally, and that write path is NOT broken today. The 892
# orphaned Officer nodes (86.6% of all Officer nodes) and 1 of 2 orphaned
# Vehicle nodes in the live production graph are therefore HISTORICAL: they
# predate whatever fixed this, or the source FIR's malkhana_register data
# has since changed upstream in the Muhafiz API (confirmed for fir-1001-26
# specifically: it has zero malkhana_register rows today, despite the live
# Vehicle node ICT-LE-309 citing it as source_doc_id). This is a one-time
# backfill, not evidence of a live bug — no code in src/graph/ is touched by
# this script.
#
# WHAT THIS SCRIPT DELIBERATELY DOES NOT TOUCH:
# a Vehicle node written by cross_silo_projection.py::_write_pkm_vehicle()
# (source_doc_id starting "pkm/") is UNSCOPED BY DESIGN — that function's
# own docstring states a PKM vehicle_verification application "never
# resolves to a case (case_id is always None)". Writing a BELONGS_TO_CASE
# edge onto one of those nodes would be a NEW bug, not a fix. This script
# hard-skips any node whose source_doc_id doesn't match the FIR pattern
# below, and reports every skip rather than silently ignoring it.
#
# HOW A CASE_ID IS DERIVED — deterministic, never guessed:
# every orphan this script backfills has a source_doc_id of the exact
# shape "psrms/fir/<fir_id>#<suffix>" (confirmed: 893/893 orphans in the
# live graph match this pattern with zero exceptions, and every derived
# fir_id is a real row in the relational `cases` table). The case_id is
# the FIR id captured by that pattern — nothing else. A node whose
# source_doc_id doesn't match, or whose derived case_id isn't a real row
# in `cases`, is skipped and reported, never guessed at.
#
# WHAT GETS WRITTEN: one new BELONGS_TO_CASE edge per orphan, via
# src/graph/versioning.py's write_edge() — the same, already-audited
# append-only mechanism every other graph mutation script in this repo
# uses. This is a pure ADDITION: no edge is ever deleted or superseded
# (there is no prior BELONGS_TO_CASE edge to supersede — that is the
# entire premise), and no node is ever created (both endpoints, the
# Officer/Vehicle node and the Case node, must already exist).
#
# Run:
#   python scripts/backfill_missing_belongs_to_case.py --dry-run
#   python scripts/backfill_missing_belongs_to_case.py --apply --admin-email <email>
#
# Take a pg_dump of the AGE graph tables first (same discipline as
# merge_confirmed_duplicate_persons.py) before --apply on production data.
# ============================================================
import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._script_admin import AdminIdentityError, resolve_admin
from src.database.models import Case
from src.database.postgres import get_session
from src.graph import age_client, versioning
from sqlalchemy import select

_FIR_SOURCE_RE = re.compile(r"^psrms/fir/([^#]+)#")

_LABELS = ("Officer", "Vehicle")


async def _live_case_ids() -> set[str]:
    async with get_session() as db:
        res = await db.execute(select(Case.case_id))
        return {row[0] for row in res.all()}


async def _find_orphans(label: str) -> list[dict]:
    """
    Every node of `label` with no active BELONGS_TO_CASE edge. AGE doesn't
    parse `WHERE NOT (n)-[:X]->(:Y)` — OPTIONAL MATCH + `WHERE c IS NULL`
    is the working equivalent (same pattern used throughout this
    investigation's own diagnostics).
    """
    q = f"""
        MATCH (n:{label})
        OPTIONAL MATCH (n)-[:BELONGS_TO_CASE]->(c:Case)
        WITH n, c WHERE c IS NULL
        RETURN n.entity_id, n.source_doc_id
    """
    rows = await age_client.execute_cypher(q, columns=["eid", "src"], graph=age_client.GRAPH_NAME)
    return [{"label": label, "entity_id": r["eid"], "source_doc_id": r["src"]} for r in rows]


def _plan(orphans: list[dict], live_case_ids: set[str]) -> dict:
    backfillable = []
    skipped_pkm = []
    skipped_unmatched = []
    skipped_case_missing = []

    for o in orphans:
        src = o["source_doc_id"] or ""
        if src.startswith("pkm"):
            skipped_pkm.append(o)
            continue
        m = _FIR_SOURCE_RE.match(src)
        if not m:
            skipped_unmatched.append(o)
            continue
        case_id = m.group(1)
        if case_id not in live_case_ids:
            skipped_case_missing.append({**o, "derived_case_id": case_id})
            continue
        backfillable.append({**o, "case_id": case_id})

    return {
        "backfillable": backfillable,
        "skipped_pkm": skipped_pkm,
        "skipped_unmatched": skipped_unmatched,
        "skipped_case_missing": skipped_case_missing,
    }


async def _apply(backfillable: list[dict]) -> dict:
    written = 0
    failed = 0
    for item in backfillable:
        edge = await versioning.write_edge(
            "BELONGS_TO_CASE", item["label"], {"entity_id": item["entity_id"]},
            "Case", {"case_id": item["case_id"]},
            {}, source_doc_id=item["source_doc_id"], confidence=1.0,
            graph=age_client.GRAPH_NAME,
        )
        if edge:
            written += 1
        else:
            failed += 1
            print(f"  FAILED: {item['label']} {item['entity_id']} -> {item['case_id']} "
                  f"(write_edge returned None — endpoint missing?)")
    return {"written": written, "failed": failed}


async def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply

    admin = None
    if apply:
        admin_email = None
        if "--admin-email" in sys.argv:
            idx = sys.argv.index("--admin-email")
            admin_email = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        try:
            admin = await resolve_admin(admin_email)
        except AdminIdentityError as exc:
            print(f"REFUSING TO APPLY — {exc}")
            return
        print(f"Acting as: {admin.email} ({admin.role}, id={admin.id})\n")

    live_case_ids = await _live_case_ids()
    print(f"Live cases in Postgres: {len(live_case_ids)}")

    totals = Counter()
    all_backfillable = []
    for label in _LABELS:
        orphans = await _find_orphans(label)
        plan = _plan(orphans, live_case_ids)
        print(f"\n=== {label}: {len(orphans)} orphan node(s) ===")
        print(f"  Backfillable (FIR-sourced, case exists live): {len(plan['backfillable'])}")
        print(f"  Skipped — PKM-sourced, unscoped BY DESIGN, never touched: {len(plan['skipped_pkm'])}")
        if plan["skipped_unmatched"]:
            print(f"  Skipped — source_doc_id doesn't match the FIR pattern: {len(plan['skipped_unmatched'])}")
            for u in plan["skipped_unmatched"][:5]:
                print(f"    {u['entity_id']}  source_doc_id={u['source_doc_id']!r}")
        if plan["skipped_case_missing"]:
            print(f"  Skipped — derived case_id not live in `cases`: {len(plan['skipped_case_missing'])}")
            for u in plan["skipped_case_missing"][:5]:
                print(f"    {u['entity_id']}  derived_case_id={u['derived_case_id']!r}")

        all_backfillable.extend(plan["backfillable"])
        totals["backfillable"] += len(plan["backfillable"])
        totals["skipped_pkm"] += len(plan["skipped_pkm"])
        totals["skipped_unmatched"] += len(plan["skipped_unmatched"])
        totals["skipped_case_missing"] += len(plan["skipped_case_missing"])

    print(f"\n--- TOTAL: {totals['backfillable']} backfillable, "
          f"{totals['skipped_pkm']} correctly-skipped (PKM), "
          f"{totals['skipped_unmatched'] + totals['skipped_case_missing']} skipped (needs human review) ---")

    if not all_backfillable:
        print("Nothing to backfill.")
        return

    if apply:
        result = await _apply(all_backfillable)
        print(f"\nAPPLIED — edges_written={result['written']} edges_failed={result['failed']}")
    else:
        print("\nDRY RUN — no changes made. Re-run with --apply --admin-email <email> to write the edges.")


if __name__ == "__main__":
    asyncio.run(main())
