# -*- coding: utf-8 -*-
"""
Module 116 — measure the TOP-LEVEL route only, N times per question.

Deliberately calls `route_query()` and nothing else: no retrieval, no
sub-agent, no backend process. That is the quantity Module 92 measured and
the quantity Module 27's `gold32_pass*_outputs.json` does NOT contain (see
MODULE116_RESULT.md §1 — that file's `route` column is the LAST
`supervisor:dispatch` event in the stream, which for a Meta-Analysis question
is a decomposed SUB-query's route, not the question's own).

    PYTHONPATH=. python -X utf8 evaluation/module116_route_probe.py \
        --questions CR3,G1,G6 --runs 8 --out scratch/probe.json
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GOLD_PATH = ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"


def _load(p: Path):
    return json.load(io.open(p, encoding="utf-8"))


async def probe(text: str, runs: int) -> list[dict]:
    from src.pipeline.router import route_query

    out = []
    for _ in range(runs):
        try:
            r = await route_query(text)
            out.append({"route": r.get("route"), "confidence": r.get("confidence"),
                        "reason": r.get("reason"), "error": None})
        except Exception as exc:  # noqa: BLE001
            out.append({"route": None, "confidence": None, "reason": None,
                        "error": f"{type(exc).__name__}: {exc}"})
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default="CR3,G1,G6")
    ap.add_argument("--runs", type=int, default=8)
    ap.add_argument("--out", default="")
    ap.add_argument("--text", default="", help="ad-hoc query text instead of gold ids")
    ap.add_argument("--label", default="adhoc")
    args = ap.parse_args()

    items = []
    if args.text:
        items.append({"id": args.label, "text": args.text})
    else:
        gold = {g["id"]: g for g in _load(GOLD_PATH)}
        for qid in [q for q in args.questions.split(",") if q]:
            items.append({"id": qid, "text": gold[qid]["question"]})

    results = {}
    for it in items:
        runs = await probe(it["text"], args.runs)
        tally = Counter(r["route"] for r in runs)
        results[it["id"]] = {"text": it["text"], "runs": runs, "tally": dict(tally)}
        print(f"{it['id']:6} {dict(tally)}")
        for r in runs:
            print(f"    {str(r['route']):10} {str(r['reason'])[:150]}")
        sys.stdout.flush()

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        io.open(p, "w", encoding="utf-8").write(
            json.dumps(results, ensure_ascii=False, indent=2))
        print(f"wrote {p}")


if __name__ == "__main__":
    asyncio.run(main())
