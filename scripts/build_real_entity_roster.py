# ============================================================
# Derive entity-resolution ground truth from REAL CNIC data — M11 of the
# Muhafiz Data API migration (docs/decisions/0001-muhafiz-api-migration.md).
#
#   python scripts/build_real_entity_roster.py
#   python scripts/build_real_entity_roster.py --snapshot tests/fixtures/muhafiz_api_snapshot.json
#
# data/memory/entity_roster.csv (the synthetic corpus's hand-designed
# confusable-pairs/name-variants/repeat-offenders) was the single hardest
# artefact to port in this whole migration — no real dataset ships with
# labeled must-merge/must-not-merge pairs. It doesn't need to: real CNIC
# data GIVES this ground truth directly, for free.
#
#   - MUST-NOT-MERGE ("confusable-pair" in eval_entity_resolution.py's
#     schema) — two different real people who happen to share a name.
#     Measured live: 44 such name groups exist across the 73 real FIRs
#     (e.g. one name shared by people with 10 different real CNICs). One
#     representative pair is emitted per group (the first two occurrences)
#     rather than every pairwise combination, to keep the roster's size
#     proportionate rather than combinatorially exploding a 10-member
#     name group into 45 pairs.
#   - MUST-MERGE ("name-variant" — same real person, must resolve to one
#     canonical entity) — the measured 4 CNICs that genuinely recur
#     across two different FIRs. Real cross-case identity, not invented.
#
# Output matches data/memory/entity_roster.csv's exact column schema, so
# scripts/eval_entity_resolution.py consumes it UNCHANGED via its new
# --roster flag (also added this migration) — no roster-parsing logic is
# duplicated here.
# ============================================================
import argparse
import asyncio
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data_gateway.muhafiz_api.client import MuhafizApiClient
from src.data_gateway.muhafiz_api.models import FirRecord
from src.data_gateway.muhafiz_api.snapshot import load_snapshot, records_for

ROSTER_HEADER = (
    "entity_id", "type", "canonical_name", "canonical_attributes",
    "surface_variants", "designed_as", "pair_or_group_id", "case_ids",
    "appears_in", "cnic_shown_in",
)


def _person_mentions(firs: list[FirRecord]) -> list[dict]:
    """Every person mention with a real CNIC, across complainant/accused/
    witness roles on every FIR — the same universe M6a's structured
    projection resolves from."""
    mentions = []
    for fir in firs:
        if fir.complainant_cnic and fir.raw.get("complainant_full_name"):
            mentions.append({
                "full_name": fir.raw["complainant_full_name"], "cnic": fir.complainant_cnic,
                "father_name": fir.raw.get("complainant_father_name"), "fir_id": fir.fir_id, "role": "complainant",
            })
        for a in fir.child_rows("fir_accused"):
            if a.get("cnic") and a.get("full_name"):
                mentions.append({
                    "full_name": a["full_name"], "cnic": a["cnic"], "father_name": a.get("father_name"),
                    "fir_id": fir.fir_id, "role": "accused",
                })
        for w in fir.child_rows("fir_witness"):
            if w.get("cnic") and w.get("full_name"):
                mentions.append({
                    "full_name": w["full_name"], "cnic": w["cnic"], "father_name": None,
                    "fir_id": fir.fir_id, "role": "witness",
                })
    return mentions


def _attributes(mention: dict) -> str:
    parts = [f"role={mention['role']}", f"cnic={mention['cnic']}"]
    if mention.get("father_name"):
        parts.append(f"father's_name={mention['father_name']}")
    return "; ".join(parts)


def build_confusable_pairs(mentions: list[dict]) -> list[dict]:
    """One representative must-NOT-merge pair per real name group spanning
    more than one distinct CNIC. Deterministic (first-seen order), so
    re-running against the same data produces the same roster."""
    by_name: dict[str, dict[str, dict]] = defaultdict(dict)  # name -> {cnic: first_mention}
    for m in mentions:
        by_name[m["full_name"]].setdefault(m["cnic"], m)

    rows = []
    group_num = 0
    for name, by_cnic in by_name.items():
        if len(by_cnic) < 2:
            continue
        group_num += 1
        group_id = f"CP-REAL-{group_num:03d}"
        pair = list(by_cnic.values())[:2]
        for i, m in enumerate(pair, start=1):
            rows.append({
                "entity_id": f"P-REAL-CP-{group_num:03d}-{i}",
                "type": "person",
                "canonical_name": name,
                "canonical_attributes": _attributes(m),
                "surface_variants": name,
                "designed_as": "confusable-pair",
                "pair_or_group_id": group_id,
                "case_ids": m["fir_id"],
                "appears_in": "",
                "cnic_shown_in": m["fir_id"],
            })
    return rows


def build_name_variant_rows(mentions: list[dict]) -> list[dict]:
    """One row per real CNIC that genuinely recurs across >1 FIR — the
    measured cross-case must-MERGE cases (cnic_auto tier)."""
    by_cnic: dict[str, list[dict]] = defaultdict(list)
    for m in mentions:
        by_cnic[m["cnic"]].append(m)

    rows = []
    n = 0
    for cnic, occurrences in by_cnic.items():
        fir_ids = {o["fir_id"] for o in occurrences}
        if len(fir_ids) < 2:
            continue
        n += 1
        first = occurrences[0]
        rows.append({
            "entity_id": f"P-REAL-NV-{n:03d}",
            "type": "person",
            "canonical_name": first["full_name"],
            "canonical_attributes": _attributes(first),
            "surface_variants": first["full_name"],
            "designed_as": "name-variant",
            "pair_or_group_id": f"NV-REAL-{n:03d}",
            "case_ids": ";".join(sorted(fir_ids)),
            "appears_in": "",
            "cnic_shown_in": ";".join(sorted(fir_ids)),
        })
    return rows


async def fetch_firs(snapshot_path: str | None) -> list[FirRecord]:
    if snapshot_path:
        snapshot = load_snapshot(Path(snapshot_path))
        return [FirRecord(r) for r in records_for(snapshot, "fir")]
    async with MuhafizApiClient() as client:
        return [FirRecord(r) for r in await client.fetch_all("fir")]


def write_roster(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ROSTER_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", metavar="PATH", default=None)
    parser.add_argument(
        "--output", metavar="PATH",
        default=str(Path(__file__).resolve().parent.parent / "data" / "eval" / "real_entity_roster.csv"),
    )
    args = parser.parse_args()

    firs = await fetch_firs(args.snapshot)
    mentions = _person_mentions(firs)
    print(f"{len(firs)} FIRs -> {len(mentions)} CNIC-bearing person mentions")

    confusable = build_confusable_pairs(mentions)
    variants = build_name_variant_rows(mentions)
    rows = confusable + variants
    print(f"  confusable-pairs (must-NOT-merge): {len(confusable) // 2} pairs ({len(confusable)} rows)")
    print(f"  name-variants (must-MERGE, real cross-FIR CNIC): {len(variants)} rows")

    write_roster(rows, Path(args.output))
    print(f"Written to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
