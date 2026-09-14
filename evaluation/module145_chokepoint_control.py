# -*- coding: utf-8 -*-
"""
Module 145 — the all-32 dispatch equality control, through the SHIPPED code
path rather than the harness's re-implementation.

For every item: run `router._deterministic_route_override()` (unchanged by
this module), then the real `router._semantic_xagg_override()` — which
calls `xagg.prepare_semantic_dispatch()` and the live cross-encoder — and
finally `xagg.resolve_aggregate_kind()`. Reports:

  * gold 32: phrase kind vs resolved kind (must be byte-identical), and
    whether the semantic override fired (must not);
  * paraphrases / targets / hazards: what the semantic override did.

Writes docs/gold-qa-wave2-results/module145_chokepoint_control.json.

USAGE
    PYTHONPATH=. python -X utf8 evaluation/module145_chokepoint_control.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.pipeline import semantic_dispatch as sd  # noqa: E402
from src.pipeline import router  # noqa: E402
from src.pipeline.xagg import phrase_aggregate_kind, resolve_aggregate_kind  # noqa: E402

RESULTS = ROOT / "docs" / "gold-qa-wave2-results"
OUT = RESULTS / "module145_chokepoint_control.json"


def _load(p):
    return json.loads(p.read_text(encoding="utf-8"))


async def main() -> None:
    items = []
    for g in _load(ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"):
        items.append({"id": f"{g['id']}::gold", "set": "gold", "text": g["question"]})
    for p in _load(ROOT / "evaluation" / "gold32_paraphrases.json")["paraphrases"]:
        items.append({"id": f"{p['gold_id']}::para::{p['language']}", "set": "para", "text": p["text"]})
    for t in _load(RESULTS / "module145_targets.json")["targets"]:
        items.append({"id": t["id"], "set": "target", "text": t["text"], "expected_kind": t["expected_kind"]})
    for h in _load(RESULTS / "module145_hazards.json")["hazards"]:
        items.append({"id": h["id"], "set": "hazard", "text": h["text"]})

    sd.reset_for_tests()
    for it in items:
        it["phrase_kind"] = phrase_aggregate_kind(it["text"])
        ov = router._deterministic_route_override(it["text"])
        it["deterministic_override"] = ov["route"] if ov else None
        sem = None
        if ov is None:
            sem = await router._semantic_xagg_override(it["text"])
        it["semantic_override"] = sem["reason"] if sem else None
        it["resolved_kind"] = resolve_aggregate_kind(it["text"])
        it["moved"] = it["resolved_kind"] != it["phrase_kind"]

    gold = [it for it in items if it["set"] == "gold"]
    moved = [it for it in gold if it["moved"] or it["semantic_override"]]
    print(f"GOLD 32 through the shipped chokepoint: resolved kind byte-identical on "
          f"{32 - sum(1 for it in gold if it['moved'])}/32; semantic override fired on "
          f"{sum(1 for it in gold if it['semantic_override'])}/32")
    for it in moved:
        print(f"  MOVED {it['id']}: {it['phrase_kind']} -> {it['resolved_kind']} | {it['semantic_override']}")

    for name in ("para", "target", "hazard"):
        rows = [it for it in items if it["set"] == name]
        fired = [it for it in rows if it["semantic_override"]]
        print(f"\n{name}: {len(rows)} items, semantic override fired on {len(fired)}")
        for it in fired:
            print(f"  {it['id']:<22} {it['phrase_kind']} -> {it['resolved_kind']}")
    OUT.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
