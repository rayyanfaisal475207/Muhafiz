# -*- coding: utf-8 -*-
"""
Phase 1 closure verification harness.

Runs UNSEEN questions through the production path and records the full
audit trail. None of these questions was used during Phase 1 development,
and the harness computes nothing itself: it executes and records.

    PYTHONPATH=. python blind_closure_runner.py --set semantic --runs 3
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

# ── Section 1: semantically adjacent questions ────────────────────────
# Deliberately similar wording, different populations. If the pipeline
# preserves constraints, these must NOT collapse onto one another.
SEMANTIC = [
    ("SEM-1", "How many accused persons are linked to cases?"),
    ("SEM-2", "How many witnesses participated?"),
    ("SEM-3", "How many victims are recorded?"),
    ("SEM-4", "How many suspects have confirmed arrest status?"),
    ("SEM-5", "How many people are involved in cases?"),
]

# ── Section 7: blind category coverage ────────────────────────────────
BLIND = [
    ("BLIND-COUNT", "How many cases exist?"),
    ("BLIND-REL", "How many people have role witness?"),
    ("BLIND-TIME", "How many incidents occurred after 2024?"),
    ("BLIND-MULTIHOP", "How many people are connected through these cases?"),
    ("BLIND-UNSUPPORTED", "How many people are left-handed?"),
    ("BLIND-CATEGORICAL", "Break down weapons by their licence status."),
    ("BLIND-MINMAX", "What is the most recent incident date?"),
]

# ── Section 8: determinism ────────────────────────────────────────────
STABILITY = [
    ("STAB-FILTERED", "How many accused persons are linked to cases?"),
    ("STAB-UNSUPPORTED", "How many people are left-handed?"),
]

SETS = {"semantic": SEMANTIC, "blind": BLIND, "stability": STABILITY}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="semantic", choices=sorted(SETS))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    questions = SETS[args.set]

    print("Building registry ...", flush=True)
    snapshot = await build_registry()
    value_examples = await route_age.collect_value_examples(snapshot)
    schema_card = route_age.build_schema_card(snapshot, value_examples)
    print(f"  card {len(schema_card)} chars\n", flush=True)

    scope = Scope(kind="cross_case", user_role="supervisor", user_id="qa-closure")
    out_path = Path(args.out)
    records: list[dict] = []

    for qid, question in questions:
        rec: dict = {"query_id": qid, "question": question, "runs": []}
        for run_ix in range(1, args.runs + 1):
            print(f"[{qid}] run {run_ix}/{args.runs} ...", end=" ", flush=True)
            try:
                ans = await orchestrator.answer_question(
                    snapshot, question, scope, schema_card=schema_card
                )
                payload = ans.to_dict()
                payload["run"] = run_ix
                payload["timestamp"] = _now()
                payload["harness_error"] = None
                print(f"{payload['status']} value={payload['value']!r}", flush=True)
            except Exception as exc:  # noqa: BLE001 — a crash is a result
                payload = {
                    "run": run_ix, "timestamp": _now(),
                    "status": "HARNESS_EXCEPTION", "value": None,
                    "harness_error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
                print(f"EXCEPTION {type(exc).__name__}: {exc}", flush=True)
            rec["runs"].append(payload)
            out_path.write_text(
                json.dumps(records + [rec], indent=2, default=str), encoding="utf-8"
            )
        records.append(rec)
        out_path.write_text(
            json.dumps(records, indent=2, default=str), encoding="utf-8"
        )

    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
