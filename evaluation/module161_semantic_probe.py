# -*- coding: utf-8 -*-
"""
Module 161 — re-runs Module 145's offline probe against the ENLARGED
description table (one new entry, `applicant_accused_overlap`) and adds
this module's own pre-written targets (`module161_targets.json`).

Writes `module161_probe_{before,after}.json` — never Module 145's own
`module145_probe.json`, which tests/test_semantic_dispatch.py pins.

USAGE
    PYTHONPATH=. python -X utf8 evaluation/module161_semantic_probe.py --tag after
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

import module145_semantic_probe as m145  # noqa: E402

M161_TARGETS = m145.RESULTS / "module161_targets.json"
_build_items_145 = m145.build_items


def build_items() -> list[dict]:
    items = _build_items_145()
    for t in m145._load(M161_TARGETS)["targets"]:
        items.append({
            "id": f"M161-{t['id']}", "gold_id": None, "set": "target",
            "language": t["language"], "text": t["text"], "gold_route": "XAGG",
            "llm_route": None, "expected_kind": t["expected_kind"],
        })
    return items


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", choices=["before", "after"], required=True)
    args = ap.parse_args()
    m145.build_items = build_items
    m145.OUT["rerank"] = m145.RESULTS / f"module161_probe_{args.tag}.json"
    asyncio.run(m145.main("rerank"))
