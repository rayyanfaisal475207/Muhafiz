# -*- coding: utf-8 -*-
"""
Module 151 — §7 regression: this branch's 32-question run on `:8151` against
(a) the Module 116 route baseline and (b) the 2026-09-14 `main` run
(`evaluation/postfix-check/outputs.json`, the run this module was filed from),
plus per-question Groq fallbacks read from the backend log.

    PYTHONPATH=. python -X utf8 evaluation/module151_regression_compare.py
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "docs" / "gold-qa-wave2-results" / "module151_live"
OURS = LIVE / "module151_gold32_outputs.json"
LOG = LIVE / "backend_8151.log"
BASELINE_ROUTES = ROOT / "evaluation" / "gold32_route_baseline.json"
MAIN_RUN = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    "D:/Rapids AI/Evidence Intelligence Platform/evaluation/postfix-check/outputs.json")
MAIN_LOG = MAIN_RUN.parent / "backend.log"


def _load(p):
    return json.load(io.open(p, encoding="utf-8"))


def _fallbacks_per_request(log_path: Path) -> list[int]:
    """Count `Falling back to groq` lines per /api/chat request. uvicorn writes
    the `POST /api/chat ... 200 OK` access line when the SSE response STARTS,
    so the lines between POST(k) and POST(k+1) are question k's work; the
    last question's work follows the last POST line."""
    buckets, cur, started = [], 0, False
    for ln in io.open(log_path, encoding="utf-8", errors="replace"):
        if 'POST /api/chat' in ln:
            if started:
                buckets.append(cur)
            cur, started = 0, True
        elif "Falling back to" in ln and started:
            cur += 1
    if started:
        buckets.append(cur)
    return buckets


def main() -> None:
    ours = _load(OURS)
    base = _load(BASELINE_ROUTES)
    base_routes = base["routes"]  # {id: route}, Module 116's shape
    main_run = {r["id"]: r for r in _load(MAIN_RUN)} if MAIN_RUN.exists() else {}
    ours_fb = _fallbacks_per_request(LOG)
    main_fb = _fallbacks_per_request(MAIN_LOG) if MAIN_LOG.exists() else []
    print(f"{'id':5} {'route(ours)':11} {'route(base)':11} {'=':1} {'sub_agent':22} {'fb ours':7} {'fb main':7} {'s ours':7} {'s main':7} {'len ours':8} {'len main':8} status")
    route_mismatch, total_fb = 0, 0
    for i, r in enumerate(ours):
        qid = r["id"]
        br = base_routes.get(qid)
        eq = "=" if (br is None or br == r["route"]) else "X"
        if eq == "X":
            route_mismatch += 1
        fb = ours_fb[i] if i < len(ours_fb) else None
        mfb = main_fb[i] if i < len(main_fb) else None
        total_fb += fb or 0
        m = main_run.get(qid, {})
        print(f"{qid:5} {str(r['route']):11} {str(br):11} {eq:1} {str(r.get('sub_agent'))[:22]:22} {str(fb):7} {str(mfb):7} "
              f"{r['elapsed_s']:7} {str(m.get('elapsed_s','')):7} {len(r['actual_answer']):8} {len(m.get('actual_answer','')):8} {r.get('status')}"
              + ("" if r.get("transport_ok", True) else "  <-- NO ANSWER"))
    print(f"\nroute mismatches vs baseline: {route_mismatch}; total Groq fallbacks this run: {total_fb} "
          f"(main 2026-09-14: {sum(main_fb)}); total wall {sum(r['elapsed_s'] for r in ours)/60:.1f} min "
          f"(main: {sum(m['elapsed_s'] for m in main_run.values())/60:.1f} min)")
    cr3 = next(r for r in ours if r["id"] == "CR3")
    print(f"CR3: route={cr3['route']} sub_agent={cr3.get('sub_agent')} subquery_routes={cr3.get('subquery_routes')}")


if __name__ == "__main__":
    main()
