# -*- coding: utf-8 -*-
"""
Module 143 — offline validation of `retry_could_help()` against the
committed evidence.

MUST-RETRY set: the attempt-1 evaluator reason of every committed run in
docs/gold-qa-wave2-results/ where attempt 1 was `relevant=False` and a
later attempt was `relevant=True` (the retry loop rescued it), plus the
attempt-1 reasons of this module's own rescued probes. A `false` on any of
these is a rescue the gate would have thrown away.

SHOULD-STOP set: the attempt-1 reasons of this module's zero-signal probes
(every attempt rejected, the missing thing an aggregate).

NEVER set (informational): committed runs where every attempt was rejected.

Usage:
    PYTHONPATH=. PYTHONIOENCODING=utf-8 python -X utf8 \
        scripts/module143_gate_offline.py --passes 2 --out scratchpad/m143_gate_offline.json
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(HERE, ".env"), override=True)

from src.pipeline.retry_gate import retry_could_help  # noqa: E402

import logging  # noqa: E402


class _FallbackCatcher(logging.Handler):
    """Which model answered (Module 101 §1.4): `call_llm()` falls back to
    Groq/Gemini silently on any local failure."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record):
        msg = record.getMessage()
        if "Falling back to" in msg:
            self.lines.append(msg[:160])


_catcher = _FallbackCatcher()
logging.getLogger("src.llm.client").addHandler(_catcher)
logging.getLogger("src.llm.client").setLevel(logging.WARNING)

RESULTS = os.path.join(HERE, "docs", "gold-qa-wave2-results")


def _walk(obj):
    if isinstance(obj, dict):
        v = obj.get("evaluator_verdicts")
        if isinstance(v, list) and v and all(isinstance(x, dict) and "relevant" in x for x in v):
            yield obj
        for val in obj.values():
            yield from _walk(val)
    elif isinstance(obj, list):
        for it in obj:
            yield from _walk(it)


def committed_sets():
    must, never = [], []
    for f in sorted(glob.glob(RESULTS + "/**/*.json", recursive=True)):
        try:
            data = json.load(open(f, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for row in _walk(data):
            v = [x["relevant"] for x in row["evaluator_verdicts"]]
            if len(v) < 2 or v[0] is not False:
                continue
            item = {
                "source": os.path.basename(f), "id": row.get("id"), "arm": row.get("arm"),
                "run": row.get("run"), "question": row.get("question") or "",
                "reason": row["evaluator_verdicts"][0].get("reason") or "",
            }
            if any(v[1:]):
                must.append(item)
            elif not any(v):
                never.append(item)
    return must, never


def probe_sets(paths):
    must, stop = [], []
    for p in paths:
        if not os.path.exists(p):
            continue
        for row in json.load(open(p, encoding="utf-8")):
            verdicts = row.get("verdicts") or []
            if not verdicts or verdicts[0]["relevant"]:
                continue
            item = {"source": os.path.basename(p), "id": row["id"], "arm": row.get("arm"),
                    "run": row.get("run"), "question": row["question"],
                    "reason": verdicts[0]["reason"]}
            if any(x["relevant"] for x in verdicts[1:]):
                must.append(item)
            elif row["id"] in ("M143", "Z2", "Z3", "Z4") or row["id"].startswith("Z"):
                stop.append(item)
    return must, stop


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--probes", default="scratchpad/m143_before.json,scratchpad/m143_plain_before.json")
    ap.add_argument("--out", default="scratchpad/m143_gate_offline.json")
    args = ap.parse_args()

    must_c, never_c = committed_sets()
    must_p, stop_p = probe_sets([os.path.join(HERE, p) for p in args.probes.split(",")])
    sets = [("MUST_RETRY", must_c + must_p), ("SHOULD_STOP", stop_p), ("NEVER", never_c)]
    rows = []
    for label, items in sets:
        for item in items:
            outcomes = []
            for p in range(args.passes):
                t0 = time.time()
                _catcher.lines.clear()
                verdict = await retry_could_help(item["question"], item["reason"])
                outcomes.append({"pass": p + 1, "retry_could_help": verdict,
                                 "secs": round(time.time() - t0, 1),
                                 "model": "cloud" if _catcher.lines else "local",
                                 "fallbacks": list(_catcher.lines)})
            wrong = (label == "MUST_RETRY" and not all(o["retry_could_help"] for o in outcomes)) or \
                    (label == "SHOULD_STOP" and any(o["retry_could_help"] for o in outcomes))
            rows.append({"set": label, **item, "outcomes": outcomes, "wrong": wrong})
            flag = "  <-- WRONG" if wrong else ""
            print(f"[{label}] {item['source']}:{item['id']}/{item['arm']}/{item['run']} "
                  f"-> {[o['retry_could_help'] for o in outcomes]} "
                  f"{[o['secs'] for o in outcomes]}s {[o['model'] for o in outcomes]}{flag} :: {item['reason'][:90]}", flush=True)
            json.dump(rows, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for label, _ in sets:
        sub = [r for r in rows if r["set"] == label]
        print(f"{label}: {len(sub)} items, {sum(r['wrong'] for r in sub)} wrong")


if __name__ == "__main__":
    asyncio.run(main())
