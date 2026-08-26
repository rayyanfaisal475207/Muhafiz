# ============================================================
# One-off: record the discovered audit gap for confirmed SAME_AS edges
# whose `reviewed_by` resolves to no real user.
#
# WHAT HAPPENED (see scripts/_script_admin.py for the mechanism):
# bulk graph-review scripts used a locally-minted uuid.uuid4() as the
# acting admin. audit_logs.user_id has a foreign key to users, so every
# audit write raised ForeignKeyViolationError and was swallowed. The graph
# mutations succeeded; nothing recorded them.
#
# WHY ONE INCIDENT RECORD AND NOT ONE ROW PER EDGE — the decision that
# shapes this whole script:
# An audit log records events that happened, when they happened. Exactly
# one event is happening today: this gap was discovered. The affected
# confirmations happened at various unknown past times under an
# unattributable identity. Writing one row per edge, dated today, would
# record hundreds of events that did not occur today — and the
# "backfilled" marker does not save it, because the failure mode is
# precisely that such markers get dropped downstream: any report counting
# confirmations per admin, or charting review activity over time, would
# then show a burst of review activity by a named person that never
# happened. In a police evidence system an audit log that can mislead an
# auditor is worse than one with an acknowledged, explained hole.
#
# Traceability is not lost by choosing one record: the affected set is
# re-derivable from the graph at any time (confirmed SAME_AS whose
# reviewed_by is not in users) — that is how it was found — and every
# affected edge id is written into this record's own details payload.
#
# The event_type is deliberately NOT graph_review_confirm: this entry must
# never be mistaken for review activity. It asserts only that the gap
# exists and was investigated; it does not claim any person reviewed
# anything.
#
# Run: python scripts/record_audit_gap.py --dry-run
#      python scripts/record_audit_gap.py --apply --admin-email you@example.com
# ============================================================

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from scripts._script_admin import AdminIdentityError, resolve_admin
from src.graph import age_client

EVENT_TYPE = "audit_gap_identified"


def _admin_email_arg() -> str:
    for i, arg in enumerate(sys.argv):
        if arg == "--admin-email" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--admin-email="):
            return arg.split("=", 1)[1]
    return ""


async def find_orphaned_confirmations() -> list[dict]:
    """
    Confirmed, non-superseded SAME_AS edges whose reviewed_by is not a real
    user id. Directed match, not undirected — see
    graph_retriever._both_directions() for why the undirected form is
    pathological on AGE.
    """
    rows = await age_client.execute_cypher(
        "MATCH (a:Person)-[r:SAME_AS]->(b:Person) "
        "WHERE r.status = 'confirmed' AND r.superseded_by IS NULL "
        "RETURN id(r) AS edge_id, r.reviewed_by AS reviewed_by, "
        "a.entity_id AS a_id, b.entity_id AS b_id",
        params={}, columns=["edge_id", "reviewed_by", "a_id", "b_id"],
    )

    from src.database.postgres import get_session
    from sqlalchemy import text as sql_text

    async with get_session() as db:
        res = await db.execute(sql_text("SELECT id::text FROM users"))
        real_ids = {row[0] for row in res.fetchall()}

    return [
        r for r in rows
        if r.get("reviewed_by") and str(r["reviewed_by"]) not in real_ids
    ]


def build_details(orphans: list[dict]) -> dict:
    by_reviewer = Counter(str(r["reviewed_by"]) for r in orphans)
    return {
        "gap": "confirmed SAME_AS edges whose reviewed_by resolves to no user",
        "cause": (
            "bulk graph-review scripts used a locally-minted uuid4 as the acting "
            "admin; audit_logs.user_id has a foreign key to users, so every audit "
            "write raised ForeignKeyViolationError and was swallowed by "
            "DirectGateway.log_audit_event()'s try/except. The graph mutation "
            "itself succeeded, so the runs appeared to work."
        ),
        "affected_edge_count": len(orphans),
        "by_unresolvable_reviewer": dict(by_reviewer),
        "original_timestamps": "unknown - not reconstructable",
        "original_actors": "unknown - the identities were never real",
        "remediation": (
            "scripts/_script_admin.py now requires --admin-email and refuses to run "
            "unattributed; both collapse scripts resolve a real admin before any mutation"
        ),
        "note": (
            "This entry records the DISCOVERY of the gap. It does NOT assert that any "
            "person reviewed these edges, and must not be counted as review activity."
        ),
        "edge_ids": sorted(int(r["edge_id"]) for r in orphans if r.get("edge_id") is not None),
    }


async def main() -> None:
    apply = "--apply" in sys.argv

    admin = None
    if apply:
        try:
            admin = await resolve_admin(_admin_email_arg())
        except AdminIdentityError as exc:
            print(f"REFUSING TO APPLY - {exc}")
            return
        print(f"Authorised by: {admin.email} ({admin.role}, id={admin.id})\n")

    orphans = await find_orphaned_confirmations()
    print(f"Orphaned confirmations found: {len(orphans)}")
    if not orphans:
        print("Nothing to record.")
        return

    details = build_details(orphans)
    print("\nBy unresolvable reviewer:")
    for rev, n in details["by_unresolvable_reviewer"].items():
        print(f"  {rev}: {n} edges")

    preview = dict(details)
    preview["edge_ids"] = f"<{len(details['edge_ids'])} ids, first 5: {details['edge_ids'][:5]}>"
    print("\nRecord that will be written:")
    print(f"  event_type: {EVENT_TYPE}")
    print(json.dumps(preview, indent=2, ensure_ascii=False))

    if not apply:
        print("\nDRY RUN - nothing written. Re-run with --apply --admin-email <email>.")
        return

    from src.data_gateway.selector import get_gateway

    gateway = await get_gateway()
    await gateway.log_audit_event(EVENT_TYPE, details, user_id=str(admin.id))

    # log_audit_event swallows its own exceptions, so verify rather than assume.
    from src.database.postgres import get_session
    from sqlalchemy import text as sql_text
    async with get_session() as db:
        res = await db.execute(sql_text(
            "SELECT count(*) FROM audit_logs WHERE event_type = :et"), {"et": EVENT_TYPE})
        written = res.scalar()
    if written:
        print(f"\nWritten. {EVENT_TYPE} rows now in audit_logs: {written}")
    else:
        print("\nFAILED - no row present after the write. The audit write was swallowed again; "
              "check the users FK and the admin id.")


if __name__ == "__main__":
    asyncio.run(main())
