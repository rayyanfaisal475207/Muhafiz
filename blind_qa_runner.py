# -*- coding: utf-8 -*-
"""
Blind QA execution harness.

Runs a fixed list of natural-language questions through the PRODUCTION
aggregate path (`orchestrator.answer_question`) and records the full audit
trail each run produces. It computes nothing itself, grades nothing, and
consults no expected answers: it only executes and records.

    PYTHONPATH=. python blind_qa_runner.py --runs 3 --out results.json
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.pipeline.aggregate import orchestrator, route_age  # noqa: E402
from src.pipeline.aggregate.registry import build_registry  # noqa: E402
from src.pipeline.aggregate.spec import Scope  # noqa: E402

logging.basicConfig(level=logging.ERROR)

QUESTIONS: list[tuple[str, str]] = [
    ("QA-001", "How many cases are recorded in the system?"),
    ("QA-002", "How many weapons are in the evidence records?"),
    ("QA-003", "How many police stations are there?"),
    ("QA-004", "How many unlicensed weapons are recorded?"),
    ("QA-005", "How many documents are FIR records?"),
    ("QA-006", "How many female persons are in the records?"),
    ("QA-007", "How many distinct persons appear across all cases?"),
    ("QA-008", "How many distinct CNIC numbers are on record, and how many persons have one?"),
    ("QA-009", "How many different districts have cases filed in them?"),
    ("QA-010", "How many cases have a weapon linked to them?"),
    ("QA-011", "How many officers are assigned to cases, and how many cases have an officer assigned?"),
    ("QA-012", "How many persons are connected to cases that cite another case?"),
    ("QA-013", "How many people have been accused in incidents?"),
    ("QA-014", "How many witnesses are there, and how many victims?"),
    ("QA-015", "How many accused persons are still under investigation rather than arrested?"),
    ("QA-016", "How many incidents occurred in 2026?"),
    ("QA-017", "Were there more incidents in 2025 or in 2024?"),
    ("QA-018", "What is the date of the earliest recorded incident?"),
    ("QA-019", "How many accused persons own a weapon?"),
    ("QA-020", "How many unique persons are linked to cases filed in Faisalabad?"),
    ("QA-021", "How many unicorn weapons were seized?"),
    ("QA-022", "How many persons have a criminal risk score above 80?"),
    ("QA-023", "How many people are connected to cases?"),
    ("QA-024", "How many licensed weapons are in evidence?"),
    ("QA-025", "For cases involving a 30-bore weapon, how many distinct accused "
               "persons are named, and which districts are those cases filed in?"),
]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", default="qa_execution_results.json")
    ap.add_argument("--only", default="", help="comma-separated QA ids")
    args = ap.parse_args()

    questions = QUESTIONS
    if args.only:
        wanted = {q.strip() for q in args.only.split(",")}
        questions = [q for q in QUESTIONS if q[0] in wanted]

    print("Building registry from live data ...", flush=True)
    snapshot = await build_registry()
    print(f"  {len(snapshot.entities)} labels, "
          f"{len(snapshot.relationships)} relationship triples", flush=True)

    value_examples = await route_age.collect_value_examples(snapshot)
    schema_card = route_age.build_schema_card(snapshot, value_examples)
    print(f"  schema card {len(schema_card)} chars, "
          f"{len(value_examples)} value-example sets\n", flush=True)

    # The role the production path requires for cross-case aggregates.
    scope = Scope(kind="cross_case", user_role="supervisor", user_id="qa-blind")

    out_path = Path(args.out)
    records: list[dict] = []

    for qid, question in questions:
        rec: dict = {
            "query_id": qid,
            "question": question,
            "runs": [],
            "final_status": "",
            "routes_used": [],
            "observations": "",
        }
        for run_ix in range(1, args.runs + 1):
            started = _now()
            print(f"[{qid}] run {run_ix}/{args.runs} ...", end=" ", flush=True)
            try:
                ans = await orchestrator.answer_question(
                    snapshot, question, scope, schema_card=schema_card
                )
                payload = ans.to_dict()
                payload["run"] = run_ix
                payload["timestamp"] = started
                payload["harness_error"] = None
                print(f"{payload['status']} value={payload['value']!r}", flush=True)
            except Exception as exc:  # noqa: BLE001 — a crash is a result
                payload = {
                    "run": run_ix,
                    "timestamp": started,
                    "status": "HARNESS_EXCEPTION",
                    "value": None,
                    "harness_error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
                print(f"EXCEPTION {type(exc).__name__}: {exc}", flush=True)
            rec["runs"].append(payload)

            # Write incrementally so a long run is never lost.
            out_path.write_text(
                json.dumps(records + [rec], indent=2, default=str),
                encoding="utf-8",
            )

        statuses = {r.get("status") for r in rec["runs"]}
        values = {json.dumps(r.get("value"), default=str, sort_keys=True)
                  for r in rec["runs"]}
        routes_used: set[str] = set()
        for r in rec["runs"]:
            for name, rr in (r.get("routes") or {}).items():
                if rr is not None:
                    routes_used.add(name)
        rec["routes_used"] = sorted(routes_used)
        rec["final_status"] = (
            sorted(statuses)[0] if len(statuses) == 1 else "NON_DETERMINISTIC"
        )
        rec["value_stable"] = len(values) == 1
        records.append(rec)
        out_path.write_text(
            json.dumps(records, indent=2, default=str), encoding="utf-8"
        )

    print(f"\nWrote {out_path} ({len(records)} queries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
