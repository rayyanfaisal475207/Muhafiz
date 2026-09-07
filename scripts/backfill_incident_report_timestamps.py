"""
[Gold-QA fix — Module 22, question M7] Backfill
`Incident.incident_datetime` / `Incident.report_datetime` onto the EXISTING
graph, so the mean-incident-to-report-minutes aggregate has data without a
full re-projection.

WHY THIS EXISTS INSTEAD OF A RE-PROJECTION
------------------------------------------
structured_projection.py now projects both timestamps (Module 22), but the
graph already in Postgres was projected before that change, so every
Incident node is missing them. The obvious way to fix that is to re-run the
full sync/projection — and that is exactly what this script exists to
AVOID, for two reasons documented on this project:

1. A previous re-projection, run on top of differently-keyed existing
   nodes, created DUPLICATE EDGES — every relationship type roughly doubled
   (189 accused edges for 94 real pairs) — and needed a bespoke dedupe
   afterwards to restore correct counts. See MODULE_18_FINAL_REPORT.md §3.
2. The graph is shared. Other Gold-QA tracks run live verification against
   this same database concurrently; a full re-projection mutates nodes and
   edges underneath them.

This script writes NO nodes and NO edges. It only SETs two properties on
Incident nodes that already exist, matched by their existing `entity_id`.
Nothing currently reads those properties except Module 22's own aggregate,
so it cannot change the behaviour of any other query — which is what makes
it safe to run while other work is in flight.

Idempotent: re-running sets the same values again. Safe to re-run.

Source of truth is the recorded API snapshot
(`tests/fixtures/muhafiz_api_snapshot.json`) — the same clean-UTF-8 source
the current graph's own repair was driven from, so the two stay consistent.

Run:  python scripts/backfill_incident_report_timestamps.py [--dry-run]
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


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    rows = _fir_rows()
    print(f"Snapshot carries {len(rows)} FIR records.")

    planned: list[tuple[str, str, str]] = []
    missing = 0
    for raw in rows:
        fir_id = raw.get("fir_id")
        incident_dt = (raw.get("incident_datetime") or "").strip()
        report_dt = (raw.get("report_datetime") or "").strip()
        if not fir_id or not incident_dt or not report_dt:
            missing += 1
            continue
        planned.append((f"INCIDENT-FIR-{fir_id}", incident_dt, report_dt))

    print(f"{len(planned)} record(s) carry both timestamps; {missing} do not (left untouched).")
    if dry_run:
        for entity_id, incident_dt, report_dt in planned[:5]:
            print(f"  would set {entity_id}: incident={incident_dt} report={report_dt}")
        print("--dry-run: nothing written.")
        return

    updated = 0
    not_found = 0
    for entity_id, incident_dt, report_dt in planned:
        # MATCH-only: if the Incident node does not exist this is a no-op,
        # never a CREATE. A missing node means the graph and the snapshot
        # have drifted, which is worth reporting rather than silently
        # papering over by inventing a node here.
        result = await age_client.execute_cypher(
            "MATCH (i:Incident {entity_id: $entity_id}) "
            "SET i.incident_datetime = $incident_dt, i.report_datetime = $report_dt "
            "RETURN i.entity_id AS entity_id",
            params={
                "entity_id": entity_id,
                "incident_dt": incident_dt,
                "report_dt": report_dt,
            },
            columns=["entity_id"],
        )
        if result:
            updated += 1
        else:
            not_found += 1
            print(f"  WARN: no Incident node for {entity_id} (graph/snapshot drift)")

    print(f"\nDone. {updated} Incident node(s) updated, {not_found} not found.")


if __name__ == "__main__":
    asyncio.run(main())
