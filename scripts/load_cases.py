"""
Load the synthetic Case records into Postgres and attach existing evidence to them.

    python scripts/load_cases.py
    python scripts/load_cases.py --assign-to investigator@example.com

Phase 1 built the Case data model (cases table, case_id FK on documents,
case_id in Chroma metadata, case-scoped retrieval filters) but nothing ever
populated it: the corpus was ingested before Cases existed, so every document
and chunk carried case_id = NULL and case-scoped retrieval had nothing to
scope to. No phase in the plan covered this — it's the gap between the model
being *implemented* and being *in use*.

Four steps, all idempotent (safe to re-run):

  1. Load data/memory/case_index.csv (34 Cases) into the `cases` table.
  2. Backfill documents.case_id from each Case's linked_doc_ids.
  3. Backfill case_id into Chroma chunk metadata, matched by source filename.
  4. If --assign-to is given, assign every loaded Case to that user
     (case_assignments). This table has nothing to do with data ownership —
     it's what src.data_gateway.direct_backend.get_cases() INNER JOINs
     against for any non-platform-admin caller (GET /cases/, the case
     dropdown). A Case with no assignment row is invisible to every
     investigator/station-admin account even though the row, its documents,
     and its evidence all exist — this script used to leave every case in
     that state, since nothing else in the bulk-load path ever wrote to
     case_assignments (only the single-case POST /cases/ endpoint does,
     for its creator). Opt-in via a flag rather than a hardcoded default:
     which account should see 34 freshly-loaded cases is an operator
     decision, not something this script should guess.

Step 3 is a metadata-only update — no re-embedding, same approach as the
is_global backfill in Phase 0.8.

Documents with no owning Case (the Batch-0 corpus, which predates the Case
model) are left with case_id = NULL deliberately. They remain reachable as
global knowledge-base documents; they are not evidence belonging to an
investigation, so inventing a Case for them would be wrong.
"""
import argparse
import asyncio
import csv
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select, update

from src.database.postgres import get_session
from src.database.models import Case, CaseAssignment, Document, User

ROOT = Path(__file__).resolve().parent.parent
CASE_INDEX = ROOT / "data" / "memory" / "case_index.csv"


def _parse_date(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _clean(value: str):
    """Empty string / the literal em-dash placeholder both mean 'not recorded'."""
    value = (value or "").strip()
    return None if value in ("", "—", "-") else value


def load_case_rows() -> list[dict]:
    with open(CASE_INDEX, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


async def upsert_cases(rows: list[dict]) -> tuple[int, int]:
    """Insert or update one `cases` row per case_index.csv row."""
    inserted = updated = 0
    async with get_session() as session:
        for row in rows:
            case_id = row["case_id"].strip()
            fields = {
                "fir_number": _clean(row.get("fir_number")),
                "crime_category": _clean(row.get("crime_category")),
                "investigation_officer": _clean(row.get("investigation_officer")),
                "police_station": _clean(row.get("police_station")),
                "incident_date": _parse_date(row.get("incident_date")),
                "investigation_status": _clean(row.get("investigation_status")),
                "location": _clean(row.get("location")),
                # case_index.csv calls it initial_description; the Case model
                # calls it description. Same field.
                "description": _clean(row.get("initial_description")),
            }

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


def build_filename_to_case(rows: list[dict]) -> dict[str, str]:
    """Map each evidence filename -> owning case_id, from linked_doc_ids."""
    mapping: dict[str, str] = {}
    for row in rows:
        case_id = row["case_id"].strip()
        for path in (row.get("linked_doc_ids") or "").split(";"):
            path = path.strip()
            if path:
                mapping[Path(path).name] = case_id
    return mapping


async def backfill_documents(filename_to_case: dict[str, str]) -> tuple[int, int]:
    """Set documents.case_id for every document whose filename maps to a Case."""
    linked = 0
    async with get_session() as session:
        result = await session.execute(select(Document))
        docs = result.scalars().all()
        for doc in docs:
            case_id = filename_to_case.get(doc.filename)
            if case_id and doc.case_id != case_id:
                doc.case_id = case_id
                linked += 1
        await session.commit()
        total = len(docs)
    return linked, total


def backfill_chroma(filename_to_case: dict[str, str]) -> tuple[int, int]:
    """Write case_id into chunk metadata, matched on the chunk's `source` filename."""
    from src.retrieval.vector_store import ChromaVectorStore

    store = ChromaVectorStore.get_instance()
    result = store._collection.get(include=["metadatas"])
    ids = result.get("ids") or []
    metas = result.get("metadatas") or []

    update_ids, update_metas = [], []
    for chunk_id, meta in zip(ids, metas):
        meta = dict(meta or {})
        case_id = filename_to_case.get(meta.get("source", ""))
        if case_id and meta.get("case_id") != case_id:
            meta["case_id"] = case_id
            update_ids.append(chunk_id)
            update_metas.append(meta)

    if update_ids:
        store._collection.update(ids=update_ids, metadatas=update_metas)
    return len(update_ids), len(ids)


async def assign_all_cases(rows: list[dict], email: str, role: str = "investigator") -> int:
    """Give `email` a case_assignments row for every Case in `rows` — see the
    module docstring for why this is opt-in rather than automatic."""
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
        for row in rows:
            case_id = row["case_id"].strip()
            if case_id not in already_assigned:
                session.add(CaseAssignment(case_id=case_id, user_id=user.id, role=role))
                assigned += 1
        await session.commit()
    return assigned


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--assign-to", metavar="EMAIL", default=None,
        help="Give this user a case_assignments row for every loaded case, "
             "so they actually appear in that account's case dropdown "
             "(GET /cases/ INNER JOINs case_assignments for non-platform-admin users).",
    )
    parser.add_argument(
        "--assign-role", default="investigator",
        help="Role to record on the case_assignments rows created by --assign-to (default: investigator).",
    )
    args = parser.parse_args()

    rows = load_case_rows()
    print(f"Read {len(rows)} Case records from {CASE_INDEX.name}")

    inserted, updated = await upsert_cases(rows)
    print(f"  cases table: {inserted} inserted, {updated} updated")

    filename_to_case = build_filename_to_case(rows)
    print(f"  {len(filename_to_case)} evidence filenames mapped to a Case")

    linked, total_docs = await backfill_documents(filename_to_case)
    print(f"  documents: {linked} newly linked (of {total_docs} total)")

    chunks_updated, total_chunks = backfill_chroma(filename_to_case)
    print(f"  chroma chunks: {chunks_updated} updated (of {total_chunks} total)")

    if args.assign_to:
        assigned = await assign_all_cases(rows, args.assign_to, args.assign_role)
        print(f"  case_assignments: {assigned} new assignment(s) for {args.assign_to}")


if __name__ == "__main__":
    asyncio.run(main())
