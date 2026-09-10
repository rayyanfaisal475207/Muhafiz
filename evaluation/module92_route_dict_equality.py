# -*- coding: utf-8 -*-
"""
Module 92 — all-32 EQUALITY control over the WHOLE router output dict.

WHY THIS EXISTS SEPARATELY FROM THE PARAPHRASE HARNESS. That harness compares
`route` only. But `route_query()` returns eight more fields from the same LLM
JSON — `secondary_methods`, `target_entity`, `station`, `district`,
`case_scope`, `output_format`, `target_year`, `confidence` — and Module 92
changes the prompt that produces all of them. A change that left every route
identical while quietly emptying `secondary_methods` would pass a route-only
comparison and drop the second half of every compound question, which
router.py's own comments record as a real failure mode.

So this compares the full dict, field by field, across all 32 gold questions,
on both prompts, and reports every field that moves.

`confidence` and `reason` are reported but NOT failed on: `reason` is free
text the model writes fresh each call, and `confidence` is a self-report the
downstream code only ever uses for logging. Everything else is load-bearing.

    PYTHONPATH=. python -X utf8 evaluation/module92_route_dict_equality.py --prompt compact
    PYTHONPATH=. python -X utf8 evaluation/module92_route_dict_equality.py --compare
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GOLD_PATH = ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
OUT_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module92_harness_cache"

LOAD_BEARING = ["route", "case_scope", "target_entity", "output_format",
                "target_year", "station", "district", "secondary_methods"]
ADVISORY = ["confidence"]


def _load(p: Path):
    return json.load(io.open(p, encoding="utf-8"))


async def run(prompt_kind: str) -> None:
    """
    Capture `route_query()`'s full output dict for all 32 gold questions on
    whatever code is currently checked out, under the label `prompt_kind`.

    Module 92's change is narrow enough that the two arms are captured by
    checking the code in and out (`git stash`) rather than by monkeypatching a
    prompt constant — the point of the control is to compare the real shipped
    function against the real previous one, including any indirect effect the
    new `cloud_system_prompt` argument could have had on the local path.
    Use "reference" for the pre-change code and "shipped" for the new code.
    """
    import src.pipeline.router as router

    gold = _load(GOLD_PATH)
    out = {}
    for n, g in enumerate(gold, 1):
        t0 = time.time()
        try:
            r = await router.route_query(g["question"])
        except Exception as exc:
            r = {"route": f"ERROR:{type(exc).__name__}", "error": str(exc)[:160]}
        out[g["id"]] = r
        print(f"[{prompt_kind}] {n}/32 {g['id']:5} {r.get('route'):10} "
              f"sec={r.get('secondary_methods')} ({time.time()-t0:.1f}s)", flush=True)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with io.open(OUT_DIR / f"route_dict_{prompt_kind}.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)


def compare() -> None:
    a = _load(OUT_DIR / "route_dict_reference.json")
    b = _load(OUT_DIR / "route_dict_shipped.json")
    moved_any = False
    print(f"{'id':6}{'field':18}{'before':28}{'after':28}")
    print("-" * 80)
    for qid in sorted(a):
        for field in LOAD_BEARING + ADVISORY:
            va, vb = a[qid].get(field), b.get(qid, {}).get(field)
            if va != vb:
                tag = "" if field in LOAD_BEARING else "  (advisory)"
                print(f"{qid:6}{field:18}{str(va)[:26]:28}{str(vb)[:26]:28}{tag}")
                if field in LOAD_BEARING:
                    moved_any = True
    print()
    for field in LOAD_BEARING:
        n = sum(1 for qid in a if a[qid].get(field) != b.get(qid, {}).get(field))
        print(f"  {field:18} moves on {n}/32")
    print()
    print("LOAD-BEARING FIELDS MOVED" if moved_any else "ALL LOAD-BEARING FIELDS EQUAL on all 32")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", choices=["shipped", "reference"])
    ap.add_argument("--compare", action="store_true")
    a = ap.parse_args()
    if a.prompt:
        asyncio.run(run(a.prompt))
    if a.compare:
        compare()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT.parent / "Evidence Intelligence Platform" / ".env")
    except Exception:
        pass
    main()
