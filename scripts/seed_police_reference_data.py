"""
Seed a SMALL set of TEST rows into police_reference_data (Phase 3).

    python scripts/seed_police_reference_data.py

These are NOT the full dataset — that's Phase 4, loaded from the real
40-document corpus. This is a 6-row subset of data/memory/offense_sections.csv
(a real, already-accurate PPC/PECA section lookup table the dataset ships
with, see data/memory/README.md), inserted directly so the SQL fast path,
the MCP demo path, and the fallback-to-RAG behavior can be exercised
end-to-end this session. Safe to re-run — upserts by (subject, section_ref).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database.postgres import get_session
from src.database.models import PoliceReferenceData
from sqlalchemy import select

# A 6-row subset of data/memory/offense_sections.csv, spanning distinct
# offense categories. category='penal_code' throughout — the only category
# with real dataset backing this session (see Muhafiz_Upgrade_Plan_v1.md §6
# and Phase 3 notes).
TEST_ROWS = [
    {
        "category": "penal_code",
        "subject": "Mobile/Vehicle Theft",
        "description": "Theft of movable property (e.g. mobile phone, motorcycle). "
                        "Cognizable: Yes. Investigating desk: Investigation Wing - Theft/Property Cell.",
        "section_ref": "379 PPC",
        "source_document": "offense_sections.csv",
        "source_type": "synthetic",
    },
    {
        "category": "penal_code",
        "subject": "Cyber Fraud/Online Scam",
        "description": "Electronic fraud (fraudulent representation via information system). "
                        "Cognizable: Yes. Investigating desk: Cyber Crime Wing (FIA coordination) / Investigation Wing.",
        "section_ref": "PECA 2016 Sec. 13",
        "source_document": "offense_sections.csv",
        "source_type": "synthetic",
    },
    {
        "category": "penal_code",
        "subject": "Road Traffic Accident",
        "description": "Rash or negligent driving on a public way. "
                        "Cognizable: Yes. Investigating desk: Traffic Police Investigation Desk.",
        "section_ref": "279 PPC",
        "source_document": "offense_sections.csv",
        "source_type": "synthetic",
    },
    {
        "category": "penal_code",
        "subject": "Domestic Dispute",
        "description": "Voluntarily causing hurt. "
                        "Cognizable: Yes. Investigating desk: Investigation Wing.",
        "section_ref": "337-A(i) PPC",
        "source_document": "offense_sections.csv",
        "source_type": "synthetic",
    },
    {
        "category": "penal_code",
        "subject": "Cheating/Financial Fraud",
        "description": "Cheating and dishonestly inducing delivery of property. "
                        "Cognizable: Yes. Investigating desk: Investigation Wing - Fraud Cell.",
        "section_ref": "420 PPC",
        "source_document": "offense_sections.csv",
        "source_type": "synthetic",
    },
    {
        "category": "penal_code",
        "subject": "Harassment/Cyber Harassment",
        "description": "Cyber-stalking. "
                        "Cognizable: Yes. Investigating desk: Cyber Crime Wing (FIA coordination) / Investigation Wing.",
        "section_ref": "PECA 2016 Sec. 24",
        "source_document": "offense_sections.csv",
        "source_type": "synthetic",
    },
]


async def main():
    async with get_session() as db:
        inserted, skipped = 0, 0
        for row in TEST_ROWS:
            existing = await db.execute(
                select(PoliceReferenceData).where(
                    PoliceReferenceData.subject == row["subject"],
                    PoliceReferenceData.section_ref == row["section_ref"],
                )
            )
            if existing.scalars().first():
                skipped += 1
                continue
            db.add(PoliceReferenceData(**row))
            inserted += 1
        await db.commit()
        print(f"Inserted {inserted} test row(s), skipped {skipped} already-present row(s).")


if __name__ == "__main__":
    asyncio.run(main())
