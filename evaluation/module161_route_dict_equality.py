# -*- coding: utf-8 -*-
"""
Module 161 — all-32 equality control over the whole `route_query()` output
dict plus `resolve_aggregate_kind()`, before and after. Reuses Module 145's
wrapper (`evaluation/module145_route_dict_equality.py`) with the output
redirected to `module161_live/` so Module 145's committed artefacts are
untouched.

    # on the pristine origin/main checkout:
    PYTHONPATH=. python -X utf8 evaluation/module161_route_dict_equality.py --capture before
    # on this tree:
    PYTHONPATH=. python -X utf8 evaluation/module161_route_dict_equality.py --capture after
    PYTHONPATH=. python -X utf8 evaluation/module161_route_dict_equality.py --compare
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

import module145_route_dict_equality as m145  # noqa: E402

m145.OUT_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module161_live"
_RENAME = {"module145_route_dict_before.json": "module161_route_dict_before.json",
           "module145_route_dict_after.json": "module161_route_dict_after.json"}


def _to_m161_names() -> None:
    for old, new in _RENAME.items():
        src = m145.OUT_DIR / old
        if src.exists():
            dst = m145.OUT_DIR / new
            if dst.exists():
                dst.unlink()
            src.rename(dst)


def _to_m145_names() -> None:
    for old, new in _RENAME.items():
        src = m145.OUT_DIR / new
        if src.exists():
            src.rename(m145.OUT_DIR / old)

if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", choices=["before", "after"])
    ap.add_argument("--compare", action="store_true")
    a = ap.parse_args()
    _to_m145_names()
    try:
        if a.capture:
            asyncio.run(m145.capture(a.capture))
        if a.compare:
            m145.compare()
    finally:
        _to_m161_names()
