"""
Module 79 — in-process runs of the Meta-Analysis sub-agent on chained plans.

Reaching `most_cited_section_by_district` LIVE needs a
`supervisor.py::_META_ANALYSIS_TRIGGER_PATTERNS` entry (Module 145's file),
so this calls `meta_analysis()` directly — the whole sub-agent: plan match,
the chained aggregate step against the live graph, the synthesis model call
and the grounding verifier — with a real execution context, and records per
run: elapsed wall time, the chained step's own seconds (from its log line),
which model answered (`httpx` request hosts + any `Falling back to` line),
and the served answer.

Usage:
    PYTHONPATH=. python -X utf8 scripts/module79_inprocess_runs.py \
        --probes scratchpad/probes.json --runs 3 --arm after --out scratchpad/after.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.getcwd())

from src.pipeline.harness.agents import meta_analysis as ma_mod  # noqa: E402
from src.pipeline.harness.types import CallerContext, ExecutionContext, SubAgentInput  # noqa: E402

GOLD = os.path.join("evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")

_captured: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _captured.append(record.getMessage())
        except Exception:  # noqa: BLE001
            pass


_HTTP = re.compile(r"HTTP Request: POST (https?://[^/ ]+)")
_FALLBACK = re.compile(r"Falling back to (groq|gemini)", re.IGNORECASE)
_CHAIN = re.compile(r"XAGG chained .*?; ([0-9.]+)s")
_XAGG = re.compile(r"XAGG ([a-z0-9_]+): ")
_TIMEOUT = re.compile(r"timed out after")


def _dissect(lines: list[str]) -> dict:
    hosts = sorted({m.group(1) for line in lines for m in [_HTTP.search(line)] if m})
    fallbacks = [line[:160] for line in lines if _FALLBACK.search(line)]
    chain_s = [float(m.group(1)) for line in lines for m in [_CHAIN.search(line)] if m]
    kinds = [m.group(1) for line in lines for m in [_XAGG.search(line)] if m]
    timeouts = [line[:160] for line in lines if _TIMEOUT.search(line)]
    plan = next((line for line in lines if "deterministic decomposition plan" in line), None)
    model = "groq" if fallbacks else ("local" if any("ngrok" in h for h in hosts) else "unknown")
    return {"llm_hosts": hosts, "fallback_lines": fallbacks, "chain_seconds": chain_s,
            "xagg_kinds": kinds, "timeouts": timeouts, "plan_line": plan, "model": model}


async def _one(qid: str, question: str, run: int, arm: str) -> dict:
    _captured.clear()
    agent_input = SubAgentInput(
        query_text=question,
        execution=ExecutionContext(
            caller=CallerContext(user_id="00000000-0000-4000-8000-000000000079",
                                 role="platform-admin", active_case_id=None)
        ),
    )
    t0 = time.perf_counter()
    err = None
    try:
        res = await ma_mod.meta_analysis(agent_input)
        status = res.status.value if hasattr(res.status, "value") else str(res.status)
        served = res.answer_text
        caveats = list(res.caveats or [])
        tools = list(res.tools_used or [])
    except Exception as exc:  # noqa: BLE001
        status, served, caveats, tools = "EXCEPTION", None, [], []
        err = repr(exc)
    elapsed = round(time.perf_counter() - t0, 1)
    row = {"id": qid, "run": run, "arm": arm, "question": question, "status": status,
           "served_answer": served, "caveats": caveats, "tools_used": tools, "error": err,
           "elapsed_s": elapsed, **_dissect(list(_captured))}
    return row


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probes", required=True, help="JSON {id: question}")
    ap.add_argument("--ids", default="")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--arm", default="after")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    logging.getLogger().addHandler(_Capture())
    logging.getLogger().setLevel(logging.INFO)

    probes = json.load(open(args.probes, encoding="utf-8"))
    gold = {q["id"]: q["question"] for q in json.load(open(GOLD, encoding="utf-8"))}
    ids = [i for i in args.ids.split(",") if i] or list(probes)
    rows: list[dict] = []
    for qid in ids:
        question = probes.get(qid) or gold[qid]
        for run in range(1, args.runs + 1):
            row = await _one(qid, question, run, args.arm)
            rows.append(row)
            print(f"{qid} run{run} arm={args.arm} status={row['status']} model={row['model']} "
                  f"chain={row['chain_seconds']} {row['elapsed_s']}s timeouts={len(row['timeouts'])}",
                  flush=True)
            print("   ", (row["served_answer"] or row["error"] or "")[:400].replace("\n", " | "), flush=True)
            json.dump(rows, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
