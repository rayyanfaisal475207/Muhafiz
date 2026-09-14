# -*- coding: utf-8 -*-
"""
Module 145 — all-32 equality control over the WHOLE `route_query()` output
dict, before and after, plus the aggregate each question dispatches to.

Module 92's `module92_route_dict_equality.py` does the capture and the
field-by-field compare; this wrapper reuses both and only redirects the
output so Module 92's committed artefacts are not overwritten. It also
records `resolve_aggregate_kind()` for every question alongside the route
dict, so the control covers both layers this module touches.

    # on the pre-module tree (origin/main worktree):
    PYTHONPATH=. python -X utf8 evaluation/module145_route_dict_equality.py --capture before
    # on this tree:
    PYTHONPATH=. python -X utf8 evaluation/module145_route_dict_equality.py --capture after
    PYTHONPATH=. python -X utf8 evaluation/module145_route_dict_equality.py --compare
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
OUT_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module145_live"
LOAD_BEARING = ["route", "case_scope", "target_entity", "output_format",
                "target_year", "station", "district", "secondary_methods", "aggregate_kind"]
ADVISORY = ["confidence", "reason"]


def _load(p: Path):
    return json.load(io.open(p, encoding="utf-8"))


async def capture(tag: str) -> None:
    import src.pipeline.router as router
    from src.pipeline.xagg import resolve_aggregate_kind

    gold = _load(GOLD_PATH)
    out = {}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for n, g in enumerate(gold, 1):
        t0 = time.time()
        try:
            r = await router.route_query(g["question"])
        except Exception as exc:  # noqa: BLE001
            r = {"route": f"ERROR:{type(exc).__name__}", "error": str(exc)[:160]}
        r["aggregate_kind"] = resolve_aggregate_kind(g["question"])
        out[g["id"]] = r
        print(f"[{tag}] {n}/32 {g['id']:5} {str(r.get('route')):10} kind={r['aggregate_kind']:34} ({time.time()-t0:.1f}s)", flush=True)
        with io.open(OUT_DIR / f"module145_route_dict_{tag}.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)


def compare() -> None:
    a = _load(OUT_DIR / "module145_route_dict_before.json")
    b = _load(OUT_DIR / "module145_route_dict_after.json")
    moved_any = False
    print(f"{'id':6}{'field':18}{'before':30}{'after':30}")
    print("-" * 84)
    for qid in sorted(a):
        for field in LOAD_BEARING + ADVISORY:
            va, vb = a[qid].get(field), b.get(qid, {}).get(field)
            if va != vb:
                tag = "" if field in LOAD_BEARING else "  (advisory)"
                print(f"{qid:6}{field:18}{str(va)[:28]:30}{str(vb)[:28]:30}{tag}")
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
    ap.add_argument("--capture", choices=["before", "after"])
    ap.add_argument("--compare", action="store_true")
    a = ap.parse_args()
    if a.capture:
        asyncio.run(capture(a.capture))
    if a.compare:
        compare()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    main()
