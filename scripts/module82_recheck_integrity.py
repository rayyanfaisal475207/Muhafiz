# -*- coding: utf-8 -*-
"""
Modules 82/85/86 — re-check an already-written runs file for the two run-integrity
failures this module actually hit, and drop the contaminated rows.

Both fired live and neither is visible in `status`:

  * **Cutover fallback** (Module 54). When the cutover classifier's provider
    returns 429, `src.main` logs `Cutover classification failed, falling back to
    orchestrator.py` and **a different pipeline answers than the one being
    measured**. One such row appeared in each arm of this module.
  * **Postgres unavailable.** The `muhafiz-postgres` container restarted
    mid-batch; every request in that window returned HTTP 500.

Rows carrying either are removed rather than averaged in, and the removal is
printed so the count is reportable.

Usage: PYTHONPATH=. python scripts/module82_recheck_integrity.py <runs.json> <backend.log>
"""
from __future__ import annotations

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CUTOVER = "Cutover classification failed"


def main() -> None:
    runs_path, log_path = sys.argv[1], sys.argv[2]
    rows = json.load(open(runs_path, encoding="utf-8"))
    log = open(log_path, encoding="utf-8", errors="replace").read()
    fallback_ts = [
        line.split(" [")[0]
        for line in log.splitlines()
        if CUTOVER in line
    ]
    print(f"{len(fallback_ts)} cutover fallback(s) in {os.path.basename(log_path)}: {fallback_ts}")

    kept, dropped = [], []
    for r in rows:
        bad = []
        if r.get("cutover_fallbacks"):
            bad.append("cutover fallback")
        if not r.get("transport_ok", True):
            bad.append(f"transport: {r.get('error')}")
        if bad:
            dropped.append((r["id"], r["run"], r["arm"], "; ".join(bad)))
        else:
            kept.append(r)

    for d in dropped:
        print("DROPPED", d)
    if dropped:
        json.dump(kept, open(runs_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"kept {len(kept)} of {len(rows)}")


if __name__ == "__main__":
    main()
