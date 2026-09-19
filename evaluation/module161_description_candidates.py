# -*- coding: utf-8 -*-
"""
Module 161 — the description candidates, scored side by side.

WRITTEN BEFORE ANY CANDIDATE OTHER THAN D0 WAS SCORED. D0 is the first
description shipped in this branch; it ranked first for every reaching
target (Q2 0.034, L2 0.018, P1 0.224) but cleared the 0.40 threshold for
none. The candidates below vary only in WHICH DOMAIN NAME is used for each
side of the join — every one is a faithful statement of what
`xagg._applicant_accused_overlap()` computes:

  * the silo side: "applied for a police service / filed a complaint" (the
    record types) vs "used a citizen service" (prompts/router.txt's own name
    for the Khidmat Markaz class of procedures);
  * the accused side: "named as an accused in an FIR" (the edge role) vs
    "an accused under investigation in an FIR" (what that role means; the
    phrase the table's `graph_recurrence_person` entry already uses).

SELECTION RULE, declared here before scoring:
  choose the candidate that maximises the MINIMUM score over the three
  targets that reach the semantic layer (Q2, L2, P1 — P2/P3 are claimed by
  the phrase chain's bare-noun tier and never reach it), subject to every
  pre-written hazard (module145_hazards.json, 24) and every generic-tier
  gold question (the 7 that consult the layer) scoring < 0.35 against it.
  Ties go to the shorter description. If no candidate reaches 0.40 on all
  three, the best is still shipped and the miss is reported as a miss.

Each query is scored ONCE against the four candidates in a single /rerank
request, so the scores are joint (query, description) judgements exactly as
in the shipped scorer.

    PYTHONPATH=. python -X utf8 evaluation/module161_description_candidates.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402
from src.pipeline.xagg import _GENERIC_AGGREGATE_KINDS, phrase_aggregate_kind  # noqa: E402

RESULTS = ROOT / "docs" / "gold-qa-wave2-results"
OUT = RESULTS / "module161_description_candidates.json"

CANDIDATES = {
    "D0": (
        "whether any member of the public who applied for a police service or "
        "filed a complaint with the police, for example at a Khidmat Markaz "
        "or through the complaint system, is also named as an accused or "
        "suspect in an FIR, matched by CNIC"
    ),
    "D1": (
        "whether any citizen who used a citizen service, such as a Khidmat "
        "Markaz application or a walk-in complaint, is also an accused in one "
        "of our FIRs, matched by CNIC"
    ),
    "D2": (
        "whether anyone who applied for a police service or filed a complaint "
        "with the police is also an accused person under investigation in an "
        "FIR: a cross-check of the service applicants against the accused "
        "roster, matched by CNIC"
    ),
    "D3": (
        "whether any citizen who used a citizen service, such as a Khidmat "
        "Markaz application or a walk-in complaint, is also an accused under "
        "investigation in one of our FIRs: a cross-check of service applicants "
        "against the accused roster, matched by CNIC"
    ),
}


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


async def _score(client: httpx.AsyncClient, query: str) -> dict[str, float]:
    kinds = list(CANDIDATES)
    docs = [CANDIDATES[k] for k in kinds]
    r = await client.post(config.RERANKER_URL, json={"query": query, "documents": docs, "top_k": len(docs)})
    r.raise_for_status()
    by_doc = {docs[i]: kinds[i] for i in range(len(kinds))}
    return {by_doc[row["document"]]: float(row["score"]) for row in r.json()}


async def main() -> None:
    targets = _load(RESULTS / "module161_targets.json")["targets"]
    hazards = _load(RESULTS / "module145_hazards.json")["hazards"]
    gold = [g for g in _load(ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json")
            if phrase_aggregate_kind(g["question"]) in _GENERIC_AGGREGATE_KINDS]
    items = (
        [{"set": "target", "id": t["id"], "text": t["text"],
          "reaches": phrase_aggregate_kind(t["text"]) in _GENERIC_AGGREGATE_KINDS} for t in targets]
        + [{"set": "hazard", "id": h["id"], "text": h["text"], "reaches": True} for h in hazards]
        + [{"set": "gold", "id": g["id"], "text": g["question"], "reaches": True} for g in gold]
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        for it in items:
            for attempt in range(6):
                try:
                    it["scores"] = await _score(client, it["text"])
                    break
                except Exception as exc:  # noqa: BLE001 - shared, serial tunnel
                    print(f"  retry {attempt+1} on {it['id']}: {type(exc).__name__}", flush=True)
                    await asyncio.sleep(10)
            print(f"{it['set']:6} {it['id']:6} " + "  ".join(f"{k}={it['scores'][k]:.3f}" for k in CANDIDATES), flush=True)

    reaching = {"Q2", "L2", "P1"}
    report = {}
    for k in CANDIDATES:
        tmin = min(it["scores"][k] for it in items if it["set"] == "target" and it["id"] in reaching)
        hmax = max(it["scores"][k] for it in items if it["set"] in ("hazard", "gold"))
        report[k] = {"min_over_reaching_targets": round(tmin, 4), "max_over_hazards_and_gold": round(hmax, 4),
                     "eligible": hmax < 0.35, "length": len(CANDIDATES[k].split())}
    eligible = [k for k in CANDIDATES if report[k]["eligible"]]
    chosen = sorted(eligible, key=lambda k: (-report[k]["min_over_reaching_targets"], report[k]["length"]))[0] if eligible else None
    print("\nSUMMARY (rule: max of min over {Q2,L2,P1}, hazards+gold < 0.35, shorter wins ties)")
    for k, r in report.items():
        print(f"  {k}: min(Q2,L2,P1)={r['min_over_reaching_targets']:.3f}  max(hazard,gold)={r['max_over_hazards_and_gold']:.3f}  eligible={r['eligible']}")
    print(f"  CHOSEN: {chosen}")
    OUT.write_text(json.dumps({"candidates": CANDIDATES, "rule": __doc__.split("SELECTION RULE")[1].split("Each query")[0].strip(),
                               "items": items, "report": report, "chosen": chosen},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    asyncio.run(main())
