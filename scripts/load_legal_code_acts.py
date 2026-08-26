# ============================================================
# Additive load of real legal-code ACTS into police_reference_data, at the
# act level ("Arms Ordinance 1965") rather than the section level
# ("379 PPC") scripts/load_real_offense_sections.py already covers.
#
#   python scripts/load_legal_code_acts.py            # dry run (default)
#   python scripts/load_legal_code_acts.py --apply     # write for real
#
# WHY A SEPARATE SCRIPT, NOT AN EXTENSION OF load_real_offense_sections.py:
# different granularity, different purpose. That script's job is the SQL
# route's per-section penal-code lookup ("which section covers offense X").
# This script's job is semantic matching + aggregation at the ACT level —
# "unlicensed weapon cases" needs to resolve to Arms Ordinance 1965, and
# "PPC, Arms Ordinance 1965" (21 cases) + "CNSA 1997, Arms Ordinance 1965"
# (8 cases) need to count as one real 29-case Arms-Ordinance total instead
# of two disconnected buckets. See src/pipeline/xagg.py's
# _station_or_category_counts()/_CATEGORY_KEYWORDS for where this data
# actually gets used.
#
# DATA SOURCE: cases.crime_category (already ingested), not a fresh Muhafiz
# API fetch. crime_category is already the exact, pre-joined act list
# src/ingestion/muhafiz_cases.py::_crime_category() writes
# (", ".join(acts)) — splitting it back on ", " is a lossless round trip,
# so there's no need to re-derive from raw fir_section rows the way
# load_real_offense_sections.py does for its own, finer-grained purpose.
#
# NEVER FABRICATES DESCRIPTION TEXT — same discipline
# load_real_offense_sections.py already established for its own rows: an
# act with no real, sourced description in _KNOWN_ACT_DESCRIPTIONS below
# gets description=NULL and shows up in this script's own "uncovered"
# report, never a guessed paraphrase. Only a human adding a real,
# citation-backed entry to that dict (and re-running --apply) turns
# "uncovered" into "covered".
#
# Idempotent — upserts by (category, subject), same pattern
# scripts/load_real_offense_sections.py / scripts/seed_police_reference_data.py
# both already use. Re-sync-safe: re-running after adding a new entry to
# _KNOWN_ACT_DESCRIPTIONS updates the existing row's description in place
# rather than skipping it as "already present."
#
# Reads with RLS inactive — the default for a bare get_session() call
# (current_rls_active defaults False, src/database/postgres.py), same as
# every other script in this directory. Correct here: this deliberately
# reads ACROSS every case's crime_category, not one case's own scope.
# ============================================================
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select

from src.database.models import Case, PoliceReferenceData
from src.database.postgres import get_session
from src.ingestion.muhafiz_cases import split_crime_category

_CATEGORY = "legal_code_act"

# ── Real, sourced descriptions only — see this module's own header note.
# Each entry researched and cross-referenced against official/primary
# sources (national/provincial government sites, or the Act's own hosted
# text) before being added here; `source_document` names the primary
# source used. An act missing here gets description=NULL and is reported
# as "uncovered" below, never a guessed paraphrase. ─────────────────────
# "<exact act string as it appears in crime_category>": (description, source_document)
_KNOWN_ACT_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "PPC": (
        "Pakistan's general criminal code — defines most criminal offenses "
        "(against the person, property, public order, and the state) and "
        "their punishments, applicable throughout Pakistan. Originally "
        "enacted 1860 as the Indian Penal Code, retained and renamed after "
        "independence.",
        "na.gov.pk (Pakistan Penal Code, 1860)",
    ),
    "Arms Ordinance 1965": (
        "Governs licensing, possession, sale, transport, and carrying of "
        "arms, ammunition, and military stores in Pakistan. A license is "
        "required to lawfully possess or carry arms; unlicensed "
        "possession, unlicensed dealing/sale, and failure to maintain "
        "required transfer records are offenses. Extends to all of "
        "Pakistan except the Tribal Areas.",
        "fia.gov.pk/files/act/9.pdf (The Pakistan Arms Ordinance, 1965 — official text)",
    ),
    "CNSA 1997": (
        "Pakistan's primary narcotics law — consolidates control over the "
        "production, processing, trafficking, and possession of narcotic "
        "drugs, psychotropic substances, and controlled precursor "
        "chemicals, and provides for treatment/rehabilitation of addicts. "
        "Covers offenses including cultivation, unauthorized possession/"
        "manufacture, and trafficking of controlled substances.",
        "na.gov.pk (Control of Narcotic Substances Act, 1997 — official text)",
    ),
    "PECA 2016": (
        "Pakistan's cybercrime law — criminalizes offenses committed "
        "through electronic systems and digital networks, including "
        "unauthorized access to information systems/data, electronic "
        "fraud, reputational-damage/privacy-breach offenses, and "
        "distribution of exploitative digital content. Provides the legal "
        "framework for investigating and prosecuting such offenses.",
        "wpc.org.pk (Prevention of Electronic Crimes Act, 2016 — official text hosted)",
    ),
    "Illegal Dispossession Act 2005": (
        "Protects lawful owners/occupiers of immovable property from "
        "illegal or forcible dispossession by property grabbers. "
        "Criminalizes entering, occupying, or forcibly dispossessing "
        "someone from a property without lawful authority — punishable by "
        "imprisonment (up to 10 years for the primary offense under "
        "Section 3) plus compensation to the victim.",
        "na.gov.pk (Illegal Dispossession Act, 2005 — official text)",
    ),
    # "Provincial Act" is DELIBERATELY EXCLUDED from this dict — not a
    # sourcing gap, a data-shape one. Live-checked against the raw FIR
    # source data behind every real "Provincial Act"-tagged case in this
    # corpus (4 cases, all citing the same underlying law): the ACTUAL
    # specific law name ("Punjab Domestic Violence Act") lives in that
    # row's section_code field, not its act field — "Provincial Act" is a
    # generic category label the source data uses, never a real single
    # law with content of its own to describe. Writing a description for
    # it would misrepresent it as one specific, describable act, which it
    # structurally isn't. It will correctly keep showing up in this
    # script's own "uncovered" report — that's the honest state, not a
    # bug. Fixing this properly means teaching
    # src/ingestion/muhafiz_cases.py::_crime_category() to surface
    # section_code instead of/alongside act for this shape — a separate,
    # larger, ingestion-level change (touches what gets WRITTEN to
    # Case.crime_category on every sync, not just this reference layer),
    # deliberately not attempted here. Confirmed only against Punjab
    # Domestic Violence Act specifically; whether section_code always
    # holds the real law name for every "Provincial Act" row, in every
    # province, is unverified and would need checking before that fix is
    # scoped.
}


async def distinct_acts() -> list[str]:
    """Every distinct act string across every case's crime_category, in
    first-seen order, deduped."""
    seen: dict[str, None] = {}
    async with get_session() as db:
        result = await db.execute(select(Case.crime_category))
        for (raw,) in result.all():
            for act in split_crime_category(raw):
                seen.setdefault(act, None)
    return list(seen.keys())


async def load_rows(acts: list[str], apply: bool) -> tuple[int, int, int, list[str]]:
    """
    Upsert one police_reference_data row per act.

    Returns (inserted, updated, unchanged, uncovered_acts). `uncovered_acts`
    is computed regardless of --apply/dry-run, since it's a report on
    _KNOWN_ACT_DESCRIPTIONS's current coverage, not a mutation outcome.
    """
    inserted = updated = unchanged = 0
    uncovered: list[str] = []

    async with get_session() as db:
        for act in acts:
            known = _KNOWN_ACT_DESCRIPTIONS.get(act)
            description, source_document = known if known else (None, "cases.crime_category")
            if description is None:
                uncovered.append(act)

            existing = await db.execute(
                select(PoliceReferenceData).where(
                    PoliceReferenceData.category == _CATEGORY,
                    PoliceReferenceData.subject == act,
                )
            )
            row = existing.scalars().first()

            if row is None:
                inserted += 1
                if apply:
                    db.add(PoliceReferenceData(
                        category=_CATEGORY, subject=act, description=description,
                        section_ref=None, source_document=source_document,
                        source_type="scraped",
                    ))
            elif row.description != description:
                updated += 1
                if apply:
                    row.description = description
                    row.source_document = source_document
            else:
                unchanged += 1

        if apply:
            await db.commit()

    return inserted, updated, unchanged, uncovered


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write for real. Default is dry-run.")
    args = parser.parse_args()
    apply = args.apply

    acts = await distinct_acts()
    print(f"{len(acts)} distinct legal-code act(s) found in cases.crime_category: {acts}")

    inserted, updated, unchanged, uncovered = await load_rows(acts, apply)

    verb = "Would insert" if not apply else "Inserted"
    print(f"{verb} {inserted} new row(s); "
          f"{'would update' if not apply else 'updated'} {updated} existing row(s); "
          f"{unchanged} already up to date.")
    if not apply:
        print("DRY RUN — no changes made. Re-run with --apply to write.")

    covered = len(acts) - len(uncovered)
    print(f"\nCoverage: {covered}/{len(acts)} act(s) have a real, sourced description.")
    if uncovered:
        print(
            "Uncovered (description=NULL — add a real, sourced entry to "
            f"_KNOWN_ACT_DESCRIPTIONS in this script): {uncovered}"
        )


if __name__ == "__main__":
    asyncio.run(main())
