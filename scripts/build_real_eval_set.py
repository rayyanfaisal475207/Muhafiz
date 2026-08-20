# ============================================================
# Regenerate data/eval/eic_eval_set.json against REAL FIR ids and answers
# — M11 of the Muhafiz Data API migration
# (docs/decisions/0001-muhafiz-api-migration.md).
#
#   python scripts/build_real_eval_set.py
#   python scripts/build_real_eval_set.py --snapshot tests/fixtures/muhafiz_api_snapshot.json
#
# SCOPE, STATED HONESTLY: the synthetic corpus's eval set (204 queries
# per docs/SYNTHETIC_DATASET_PLAN.md §3.2) was hand-authored against a
# cast the corpus's own generator designed for exactly this purpose
# (confusable pairs, repeat offenders, recurring vehicles/phones). No
# such designed cast exists in the real dataset. This script generates a
# SMALLER set, deliberately: every question and answer key here is
# derived programmatically from measured, verifiable facts in the live
# API response (a real section code, a real station name, a real
# cross-case CNIC) rather than hand-guessed — correct by construction,
# not by review. Expanding this set with genuinely hand-authored
# investigative questions (the "within_case_multihop"/"cross_case_pattern"
# categories the old set covered more richly) is real, separate future
# work once a human reviewer is available to author and verify them
# against this specific real dataset — not attempted here.
#
# Same output schema as the file it replaces (a bare list; question_ur/
# question_en/question_roman_ur/category/scope/case_id/expected_route/
# expected_answer_entities/expected_source_docs/difficulty/notes) — the
# fixed scripts/eval_end_to_end.py and scripts/eval_keyword_search.py
# (M11) consume it unchanged.
# ============================================================
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data_gateway.muhafiz_api.client import MuhafizApiClient
from src.data_gateway.muhafiz_api.models import FirRecord
from src.data_gateway.muhafiz_api.snapshot import load_snapshot, records_for

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "eval" / "eic_eval_set.json"


def _source_for(fir: FirRecord) -> str:
    return f"psrms/fir/{fir.fir_id}#narrative"


def build_queries(firs: list[FirRecord]) -> list[dict]:
    queries: list[dict] = []
    n = 0

    # content_rag — one per FIR with a real narrative, asking for the
    # station. Answer verified directly from the same record, not guessed.
    for fir in firs[:5]:
        if not fir.narrative_text:
            continue
        n += 1
        station = fir.police_station.get("name") or ""
        queries.append({
            "id": f"REAL-{n:03d}", "category": "content_rag", "scope": "case",
            "case_id": fir.fir_id,
            "question_en": f"Which police station registered FIR {fir.fir_display_code}?",
            "question_ur": None, "question_roman_ur": None,
            "expected_route": "RAG",
            "expected_answer_entities": [station] if station else [],
            "expected_source_docs": [_source_for(fir)],
            "difficulty": "easy",
            "notes": "Station name read directly from the FIR's own police_station field.",
        })

    # structured_sql — section lookups, real sections measured live.
    seen_sections = set()
    for fir in firs:
        for s in fir.child_rows("fir_section"):
            code, act = s.get("section_code"), s.get("act")
            if code and act and (code, act) not in seen_sections:
                seen_sections.add((code, act))
                n += 1
                queries.append({
                    "id": f"REAL-{n:03d}", "category": "structured_sql", "scope": "global",
                    "case_id": None,
                    "question_en": f"What is Section {code} of {act}?",
                    "question_ur": None, "question_roman_ur": None,
                    "expected_route": "SQL",
                    "expected_answer_entities": [f"{code} {act}"],
                    "expected_source_docs": [],
                    "difficulty": "easy",
                    "notes": "Additive police_reference_data row from scripts/load_real_offense_sections.py.",
                })
            if len(seen_sections) >= 3:
                break
        if len(seen_sections) >= 3:
            break

    # cross_case_pattern — the measured real cross-case CNICs (genuine
    # repeat-appearance identity, not invented).
    cnic_to_firs: dict[str, set] = {}
    for fir in firs:
        for a in fir.child_rows("fir_accused"):
            if a.get("cnic"):
                cnic_to_firs.setdefault(a["cnic"], set()).add(fir.fir_id)
    for cnic, fir_ids in cnic_to_firs.items():
        if len(fir_ids) < 2:
            continue
        n += 1
        fir_ids_sorted = sorted(fir_ids)
        queries.append({
            "id": f"REAL-{n:03d}", "category": "cross_case_pattern", "scope": "cross-case",
            "case_id": None,
            "question_en": f"Does an accused with CNIC {cnic} appear in more than one FIR?",
            "question_ur": None, "question_roman_ur": None,
            "expected_route": "XGRAPH",
            "expected_answer_entities": fir_ids_sorted,
            "expected_source_docs": [f"psrms/fir/{fid}#structured" for fid in fir_ids_sorted],
            "difficulty": "medium",
            "notes": f"Measured live: CNIC {cnic} appears as an accused on {len(fir_ids)} real FIRs.",
        })

    # no_answer — a real CNIC/station that does not exist in this dataset.
    n += 1
    queries.append({
        "id": f"REAL-{n:03d}", "category": "no_answer", "scope": "global", "case_id": None,
        "question_en": "What is the case history for CNIC 99999-9999999-9?",
        "question_ur": None, "question_roman_ur": None,
        "expected_route": "RAG",
        "expected_answer_entities": [],
        "expected_source_docs": [],
        "difficulty": "medium",
        "notes": "This CNIC does not appear anywhere in the live dataset — must not fabricate a case history.",
    })

    return queries


async def fetch_firs(snapshot_path: str | None) -> list[FirRecord]:
    if snapshot_path:
        snapshot = load_snapshot(Path(snapshot_path))
        return [FirRecord(r) for r in records_for(snapshot, "fir")]
    async with MuhafizApiClient() as client:
        return [FirRecord(r) for r in await client.fetch_all("fir")]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", metavar="PATH", default=None)
    parser.add_argument("--output", metavar="PATH", default=str(OUTPUT_PATH))
    args = parser.parse_args()

    firs = await fetch_firs(args.snapshot)
    queries = build_queries(firs)
    print(f"{len(firs)} FIRs -> {len(queries)} real, verified eval queries")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(queries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
