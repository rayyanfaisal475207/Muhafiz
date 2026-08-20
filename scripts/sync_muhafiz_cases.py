# ============================================================
# Provision `cases` rows from the Muhafiz Data API (M4 of the migration,
# docs/decisions/0001-muhafiz-api-migration.md).
#
#   python scripts/sync_muhafiz_cases.py
#   python scripts/sync_muhafiz_cases.py --snapshot tests/fixtures/muhafiz_api_snapshot.json
#   python scripts/sync_muhafiz_cases.py --assign-to investigator@example.com
#
# Mirrors scripts/load_cases.py's shape (same idempotent-upsert +
# opt-in --assign-to pattern, same reasoning for why case_assignments
# needs an explicit flag rather than a default) — but the case rows come
# from the live/snapshotted Muhafiz Data API instead of
# data/memory/case_index.csv, and "Case = FIR" means there's no separate
# linked_doc_ids backfill step: a rendered FIR/CMS/PKM Document's case_id
# is decided at ingestion time (M9), not backfilled after the fact here.
#
# Four steps, all idempotent (safe to re-run):
#   1. Fetch all FIRs (+ CMS/PKM, needed only to report escalation-match
#      counts — see step 2).
#   2. Upsert one `cases` row per FIR (src.ingestion.muhafiz_cases.case_fields_from_fir).
#   3. Report how many CMS/PKM records resolve to one of those cases
#      (e_tag_number / forwarded_fir_number matching) — informational only,
#      no rows written for them; M9's sync uses the same resolver functions
#      at actual ingestion time.
#   4. If --assign-to is given, assign every provisioned case to that user.
# ============================================================
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select

from src.data_gateway.muhafiz_api.client import MuhafizApiClient
from src.data_gateway.muhafiz_api.models import CmsComplaint, FirRecord, PkmApplication
from src.data_gateway.muhafiz_api.snapshot import load_snapshot, records_for
from src.database.models import Case, CaseAssignment, User
from src.database.postgres import get_session
from src.ingestion.muhafiz_cases import (
    build_display_code_index,
    build_e_tag_index,
    case_fields_from_fir,
    resolve_cms_case_id,
    resolve_pkm_case_id,
)


async def fetch_records(snapshot_path: str | None) -> dict[str, list[dict]]:
    if snapshot_path:
        snapshot = load_snapshot(Path(snapshot_path))
        return {
            "fir": records_for(snapshot, "fir"),
            "cms": records_for(snapshot, "cms"),
            "pkm": records_for(snapshot, "pkm"),
        }
    async with MuhafizApiClient() as client:
        return {
            "fir": await client.fetch_all("fir"),
            "cms": await client.fetch_all("cms"),
            "pkm": await client.fetch_all("pkm"),
        }


async def upsert_cases(firs: list[FirRecord]) -> tuple[int, int]:
    inserted = updated = 0
    async with get_session() as session:
        for fir in firs:
            fields = case_fields_from_fir(fir)
            case_id = fields.pop("case_id")
            existing = await session.get(Case, case_id)
            if existing is None:
                session.add(Case(case_id=case_id, **fields))
                inserted += 1
            else:
                for k, v in fields.items():
                    setattr(existing, k, v)
                updated += 1
        await session.commit()
    return inserted, updated


async def assign_all_cases(case_ids: list[str], email: str, role: str = "investigator") -> int:
    async with get_session() as session:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalars().first()
        if user is None:
            print(f"  WARNING: no user found with email {email!r} — skipping assignment.")
            return 0

        result = await session.execute(
            select(CaseAssignment.case_id).where(CaseAssignment.user_id == user.id)
        )
        already_assigned = {r[0] for r in result.all()}

        assigned = 0
        for case_id in case_ids:
            if case_id not in already_assigned:
                session.add(CaseAssignment(case_id=case_id, user_id=user.id, role=role))
                assigned += 1
        await session.commit()
    return assigned


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot", metavar="PATH", default=None,
        help="Load records from a saved snapshot (scripts/fetch_muhafiz_snapshot.py) "
             "instead of calling the live API.",
    )
    parser.add_argument(
        "--assign-to", metavar="EMAIL", default=None,
        help="Give this user a case_assignments row for every provisioned case "
             "(GET /cases/ INNER JOINs case_assignments for non-platform-admin users — "
             "a case with no assignment row is otherwise invisible in the UI).",
    )
    parser.add_argument("--assign-role", default="investigator")
    args = parser.parse_args()

    raw = await fetch_records(args.snapshot)
    firs = [FirRecord(r) for r in raw["fir"]]
    cms_complaints = [CmsComplaint(r) for r in raw["cms"]]
    pkm_applications = [PkmApplication(r) for r in raw["pkm"]]
    print(f"Fetched {len(firs)} FIR, {len(cms_complaints)} CMS, {len(pkm_applications)} PKM records")

    inserted, updated = await upsert_cases(firs)
    print(f"  cases table: {inserted} inserted, {updated} updated")

    e_tag_index = build_e_tag_index(firs)
    display_code_index = build_display_code_index(firs)
    cms_matched = sum(1 for c in cms_complaints if resolve_cms_case_id(c, e_tag_index))
    pkm_matched = sum(1 for p in pkm_applications if resolve_pkm_case_id(p, display_code_index))
    print(f"  CMS complaints resolving to a case: {cms_matched}/{len(cms_complaints)}")
    print(f"  PKM applications resolving to a case: {pkm_matched}/{len(pkm_applications)}")

    if args.assign_to:
        assigned = await assign_all_cases([f.fir_id for f in firs], args.assign_to, args.assign_role)
        print(f"  case_assignments: {assigned} new assignment(s) for {args.assign_to}")


if __name__ == "__main__":
    asyncio.run(main())
