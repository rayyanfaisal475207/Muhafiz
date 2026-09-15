# -*- coding: utf-8 -*-
"""
Module 161 — the live question end-to-end, in process, with the model that
answered each run recorded.

Reuses Module 145's runner (`evaluation/module145_inprocess_run.py`), which
drives the same two stages `main.py::chat_endpoint()` does and captures every
`SEMANTIC-DISPATCH` / `XAGG` / `Falling back to` log line per run. Only the
target set and the output directory differ:

  targets   docs/gold-qa-wave2-results/module161_targets.json
  output    docs/gold-qa-wave2-results/module161_live/module161_inprocess_<tag>.json

The caller role is Module 92's `MODULE92_USER_ROLE` (default platform-admin);
set it to `investigator` for the access-boundary arm.

    PYTHONPATH=. python -X utf8 evaluation/module161_inprocess_run.py --tag after --runs 3 --ids Q2
    MODULE92_USER_ROLE=investigator PYTHONPATH=. python -X utf8 evaluation/module161_inprocess_run.py --tag after_investigator --runs 1 --ids Q2
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

import module145_inprocess_run as m145  # noqa: E402

m145.TARGETS = ROOT / "docs" / "gold-qa-wave2-results" / "module161_targets.json"
m145.OUT_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module161_live"


async def _main_async(a) -> None:
    # Module 145's runner names its file by its own module; rename after.
    await m145.main_async(a)
    src = m145.OUT_DIR / f"module145_inprocess_{a.tag}.json"
    dst = m145.OUT_DIR / f"module161_inprocess_{a.tag}.json"
    if src.exists():
        if dst.exists():
            dst.unlink()
        src.rename(dst)
        print(f"renamed -> {dst}")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--ids", default="")
    ap.add_argument("--disable", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    # Resume support: Module 145's runner reads its own file name, so present
    # any previous Module 161 output under that name first.
    prev = m145.OUT_DIR / f"module161_inprocess_{args.tag}.json"
    if prev.exists():
        prev.rename(m145.OUT_DIR / f"module145_inprocess_{args.tag}.json")
    asyncio.run(_main_async(args))
