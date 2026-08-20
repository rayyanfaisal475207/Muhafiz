# ============================================================
# Additive load of real penal-code sections into police_reference_data,
# from the Muhafiz Data API's fir_section rows (M7 of the migration,
# docs/decisions/0001-muhafiz-api-migration.md).
#
#   python scripts/load_real_offense_sections.py
#   python scripts/load_real_offense_sections.py --snapshot tests/fixtures/muhafiz_api_snapshot.json
#
# ADDITIVE, NOT A REPLACEMENT — confirmed decision (round-2 review item 2):
# scripts/seed_police_reference_data.py's 6 hand-described rows
# (source_type='synthetic', rich `description` text) stay untouched. This
# script adds the distinct (section_code, act) pairs actually observed on
# real FIRs (measured: 36 across 6 acts — PPC/PECA 2016/Arms Ordinance
# 1965/CNSA 1997/Illegal Dispossession Act 2005/a Provincial Act) that the
# 6-row seed doesn't cover, so the SQL route has real section coverage to
# answer against instead of just 6 illustrative rows.
#
# `description` is left NULL for every row this script inserts — the API
# gives no offense-description text per section (unlike the 6 hand-curated
# rows, which paraphrase real legal text), and inventing one would be
# exactly the kind of fabrication this whole migration exists to move
# away from. `section_ref` uses the same "<code> <act>" format the
# existing 6 rows use (e.g. "379 PPC") — note this means a section that's
# in BOTH sets (e.g. 379 PPC, already seeded with a descriptive subject)
# gets a SECOND row here with subject=<act> and no description, not a
# merge; reconciling the two into one authoritative row per section is
# real future work, not attempted here (see the module docstring's own
# note on scope).
#
# Idempotent — upserts by (subject, section_ref), same pattern as
# scripts/seed_police_reference_data.py.
# ============================================================
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select

from src.data_gateway.muhafiz_api.client import MuhafizApiClient
from src.data_gateway.muhafiz_api.models import FirRecord
from src.data_gateway.muhafiz_api.snapshot import load_snapshot, records_for
from src.database.models import PoliceReferenceData
from src.database.postgres import get_session


async def fetch_firs(snapshot_path: str | None) -> list[FirRecord]:
    if snapshot_path:
        snapshot = load_snapshot(Path(snapshot_path))
        return [FirRecord(r) for r in records_for(snapshot, "fir")]
    async with MuhafizApiClient() as client:
        return [FirRecord(r) for r in await client.fetch_all("fir")]


def distinct_section_act_pairs(firs: list[FirRecord]) -> list[tuple[str, str]]:
    """Every distinct (section_code, act) pair across all FIRs, in first-seen order."""
    seen: dict[tuple[str, str], None] = {}
    for fir in firs:
        for row in fir.child_rows("fir_section"):
            code, act = row.get("section_code"), row.get("act")
            if code and act:
                seen.setdefault((code, act), None)
    return list(seen.keys())


async def load_rows(pairs: list[tuple[str, str]]) -> tuple[int, int]:
    inserted, skipped = 0, 0
    async with get_session() as db:
        for code, act in pairs:
            subject = act
            section_ref = f"{code} {act}"
            existing = await db.execute(
                select(PoliceReferenceData).where(
                    PoliceReferenceData.subject == subject,
                    PoliceReferenceData.section_ref == section_ref,
                )
            )
            if existing.scalars().first():
                skipped += 1
                continue
            db.add(PoliceReferenceData(
                category="penal_code", subject=subject, description=None,
                section_ref=section_ref, source_document="muhafiz_api",
                source_type="scraped",
            ))
            inserted += 1
        await db.commit()
    return inserted, skipped


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", metavar="PATH", default=None)
    args = parser.parse_args()

    firs = await fetch_firs(args.snapshot)
    pairs = distinct_section_act_pairs(firs)
    print(f"{len(firs)} FIRs -> {len(pairs)} distinct (section_code, act) pairs")

    inserted, skipped = await load_rows(pairs)
    print(f"Inserted {inserted} row(s), skipped {skipped} already-present row(s).")


if __name__ == "__main__":
    asyncio.run(main())
