"""
[Gold-QA fix — Module 95, question G2] Backfill the three completeness
fields Module 95 added to `structured_projection.py` onto the EXISTING
Incident nodes:

    - `station_departure_datetime`  (FIR form field 6, "تھانہ سے روانگی")
    - `zimni_entry_count` / `zimni_typed_count`

...plus `report_datetime` for the FIRs Module 22's own backfill skipped.

WHY THIS EXISTS INSTEAD OF A RE-PROJECTION
------------------------------------------
Exactly the reasoning in `scripts/backfill_incident_report_timestamps.py`,
which this script is modelled on line for line — a full re-projection has
previously duplicated every edge type in this graph (MODULE_18_FINAL_REPORT
§3), and the graph is shared with other Gold-QA tracks running live
verification against it concurrently.

This script writes NO nodes and NO edges. It only SETs properties on
Incident nodes that already exist, matched by their existing `entity_id`.
Every property it writes is new to the graph except `report_datetime`, and
that one is written from the SAME snapshot Module 22's backfill used, so a
node that already carries it is set to the value it already has.

WHY `report_datetime` IS RE-WRITTEN HERE
----------------------------------------
Module 22's backfill required `incident_datetime` AND `report_datetime`
together and skipped a record carrying only one. Nine live FIRs record no
incident date at all (that is G2's own first finding), so nine Incident
nodes carry no `report_datetime` either — measured live: 64 of 73 have it.
G2's chronology check compares departure against the REPORT time and does
not need the incident date, so those nine would silently drop out of a
"13 of 73" count computed off the graph. Writing `report_datetime`
independently of `incident_datetime` closes that hole. It cannot affect
Module 22's own aggregate, which filters on both timestamps being present.

Idempotent: re-running sets the same values again. Safe to re-run.

Source of truth is the recorded API snapshot
(`tests/fixtures/muhafiz_api_snapshot.json`) — the same clean-UTF-8 source
Module 22's backfill and the current graph's own repair were driven from.

Run:  python scripts/backfill_incident_completeness_fields.py [--dry-run]
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.graph import age_client

SNAPSHOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "muhafiz_api_snapshot.json"


def _fir_rows() -> list[dict]:
    payload = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    fir = payload["endpoints"]["fir"]
    return fir["data"] if isinstance(fir, dict) and "data" in fir else fir


def plan_updates(rows: list[dict]) -> list[dict]:
    """One update dict per FIR that has anything to write. Pure — unit-tested."""
    planned: list[dict] = []
    for raw in rows:
        fir_id = raw.get("fir_id")
        if not fir_id:
            continue
        zimni = raw.get("fir_zimni") or []
        update = {
            "entity_id": f"INCIDENT-FIR-{fir_id}",
            "departure_dt": (raw.get("station_departure_datetime") or "").strip() or None,
            "report_dt": (raw.get("report_datetime") or "").strip() or None,
            "zimni_total": len(zimni),
            "zimni_typed": sum(1 for z in zimni if (z.get("entry_type") or "").strip()),
        }
        planned.append(update)
    return planned


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    rows = _fir_rows()
    planned = plan_updates(rows)
    with_departure = sum(1 for u in planned if u["departure_dt"])
    print(f"Snapshot carries {len(rows)} FIR records; {len(planned)} planned.")
    print(f"{with_departure} carry a station_departure_datetime.")

    if dry_run:
        for u in planned[:5]:
            print(
                f"  would set {u['entity_id']}: departure={u['departure_dt']} "
                f"report={u['report_dt']} zimni={u['zimni_typed']}/{u['zimni_total']} typed"
            )
        print("--dry-run: nothing written.")
        return

    updated = 0
    not_found = 0
    for u in planned:
        # MATCH-only: a missing Incident node is a no-op and a reported
        # drift, never a CREATE. Same contract as Module 22's backfill.
        sets = ["i.zimni_entry_count = $zimni_total", "i.zimni_typed_count = $zimni_typed"]
        params = {
            "entity_id": u["entity_id"],
            "zimni_total": u["zimni_total"],
            "zimni_typed": u["zimni_typed"],
        }
        if u["departure_dt"]:
            sets.append("i.station_departure_datetime = $departure_dt")
            params["departure_dt"] = u["departure_dt"]
        if u["report_dt"]:
            sets.append("i.report_datetime = $report_dt")
            params["report_dt"] = u["report_dt"]
        result = await age_client.execute_cypher(
            "MATCH (i:Incident {entity_id: $entity_id}) "
            f"SET {', '.join(sets)} "
            "RETURN i.entity_id AS entity_id",
            params=params,
            columns=["entity_id"],
        )
        if result:
            updated += 1
        else:
            not_found += 1
            print(f"  WARN: no Incident node for {u['entity_id']} (graph/snapshot drift)")

    print(f"\nDone. {updated} Incident node(s) updated, {not_found} not found.")


if __name__ == "__main__":
    asyncio.run(main())
