# -*- coding: utf-8 -*-
"""
Module 145 — offline measurement of the semantic dispatch layer.

Scores every item in the corpus ONCE per scorer (cached to disk), ranks it
against the capability descriptions in `src/pipeline/semantic_dispatch.py`,
and reports:

  * per item: phrase kind (origin/main behaviour), whether the semantic step
    is reached (phrase kind is a generic catch-all AND no deterministic
    router override claims the question first), best description, score,
    runner-up, and the verdict at the shipped threshold;
  * the true-positive and hazard score distributions and the margin between
    them, per threshold — the number the threshold is chosen from;
  * reach over the 96-paraphrase corpus, per language, before and after;
  * the all-32 gold equality control at the phrase layer AND with the
    semantic layer force-armed on every gold question.

Two scorers, both measured; the shipped one is `rerank`:
  rerank   the bge-reranker-v2-m3 cross-encoder at RERANKER_URL (shipped)
  e5       multilingual-e5 cosine via EMBEDDINGS_URL (the plan's original
           design; measured and rejected — see MODULE145_RESULT.md §6)

Corpus:
  evaluation/Gold_QA_Dataset_Final32_With_Answers.json      (32 gold)
  evaluation/gold32_paraphrases.json                        (96, Module 92)
  evaluation/gold32_route_baseline.json                     (top-level routes)
  docs/gold-qa-wave2-results/module92_harness_cache/local.json (LLM routes)
  docs/gold-qa-wave2-results/module116_paraphrases.json     (6, XNETWORK)
  docs/gold-qa-wave2-results/module144_paraphrases.json     (13, filtered counts)
  prompts/router.txt                                        (74 few-shot exemplars)
  docs/gold-qa-wave2-results/module145_hazards.json         (24, pre-written)
  docs/gold-qa-wave2-results/module145_targets.json         (6, pre-written)

USAGE
    PYTHONPATH=. python -X utf8 evaluation/module145_semantic_probe.py
    PYTHONPATH=. python -X utf8 evaluation/module145_semantic_probe.py --scorer e5
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

from src import config  # noqa: E402
from src.pipeline import semantic_dispatch as sd  # noqa: E402
from src.pipeline.xagg import (  # noqa: E402
    _GENERIC_AGGREGATE_KINDS,
    phrase_aggregate_kind,
    resolve_aggregate_kind,
)
from src.pipeline.router import _deterministic_route_override  # noqa: E402
from router_paraphrase_harness import route_exemplars  # noqa: E402

RESULTS = ROOT / "docs" / "gold-qa-wave2-results"
GOLD_PATH = ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
PARA_PATH = ROOT / "evaluation" / "gold32_paraphrases.json"
ROUTE_BASELINE = ROOT / "evaluation" / "gold32_route_baseline.json"
LOCAL_CACHE = RESULTS / "module92_harness_cache" / "local.json"
M116 = RESULTS / "module116_paraphrases.json"
M144 = RESULTS / "module144_paraphrases.json"
HAZARDS = RESULTS / "module145_hazards.json"
TARGETS = RESULTS / "module145_targets.json"
SCORE_CACHE = {"rerank": RESULTS / "module145_rerank_cache.json",
               "e5": RESULTS / "module145_embed_cache.json"}
OUT = {"rerank": RESULTS / "module145_probe.json",
       "e5": RESULTS / "module145_probe_e5.json"}


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build_items() -> list[dict]:
    gold = _load(GOLD_PATH)
    routes = _load(ROUTE_BASELINE)["routes"]
    local = _load(LOCAL_CACHE)
    gold_by_id = {g["id"]: g for g in gold}

    def route_of(gid):
        r = routes[gid]
        return r["route"] if isinstance(r, dict) else r

    items = []
    for g in gold:
        items.append({
            "id": f"{g['id']}::gold", "gold_id": g["id"], "set": "gold",
            "language": g.get("language", "en"), "text": g["question"],
            "gold_route": route_of(g["id"]),
            "llm_route": local.get(f"{g['id']}::gold", {}).get("route"),
        })
    for p in _load(PARA_PATH)["paraphrases"]:
        items.append({
            "id": f"{p['gold_id']}::para::{p['language']}", "gold_id": p["gold_id"],
            "set": "para", "language": p["language"], "text": p["text"],
            "gold_route": route_of(p["gold_id"]),
            "llm_route": local.get(f"{p['gold_id']}::para::{p['language']}", {}).get("route"),
        })
    for i, p in enumerate(_load(M116)["paraphrases"]):
        items.append({
            "id": f"M116-{i}::{p['gold_id']}::{p['language']}", "gold_id": p["gold_id"],
            "set": "m116", "language": p["language"], "text": p["text"],
            "gold_route": "XNETWORK", "llm_route": None,
        })
    for key, p in _load(M144).items():
        items.append({
            "id": f"M144-{key}", "gold_id": None, "set": "m144",
            "language": p.get("language", "en"), "text": p["question"],
            "gold_route": "XAGG", "llm_route": None, "why": "filtered count; the count kinds own it",
        })
    for i, (q, route) in enumerate(route_exemplars()):
        items.append({
            "id": f"EX{i:02d}::{route}", "gold_id": None, "set": "exemplar",
            "language": "mixed", "text": q, "gold_route": route, "llm_route": None,
        })
    for h in _load(HAZARDS)["hazards"]:
        items.append({
            "id": h["id"], "gold_id": None, "set": "hazard", "language": h["language"],
            "text": h["text"], "gold_route": None, "llm_route": None, "why": h["why"],
        })
    for t in _load(TARGETS)["targets"]:
        items.append({
            "id": t["id"], "gold_id": None, "set": "target", "language": t["language"],
            "text": t["text"], "gold_route": "XAGG", "llm_route": None,
            "expected_kind": t["expected_kind"],
        })
    for it in items:
        if it["set"] in ("gold", "para", "m116"):
            it["gold_kind"] = phrase_aggregate_kind(gold_by_id[it["gold_id"]]["question"])
    return items


# ── Scorers ──────────────────────────────────────────────────────────────────

async def _embed(text: str, is_query: bool) -> list[float]:
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(config.EMBEDDINGS_URL, json={"text": text, "is_query": is_query})
        r.raise_for_status()
        return r.json()["embedding"]


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def table_hash() -> str:
    """The cache is only valid for the description table it was scored
    against — any edit to a description invalidates every cross-encoder
    score (each is a joint (question, description) judgement)."""
    blob = json.dumps(sd.CAPABILITY_DESCRIPTIONS, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]


async def score_items(items: list[dict], scorer: str) -> None:
    """Attach `scores` (kind -> score) to every item, from cache when present."""
    path = SCORE_CACHE[scorer]
    cache = _load(path) if path.exists() else {}
    if scorer == "rerank":
        h = table_hash()
        for it in items:
            key = f"{h}::{it['text']}"
            if key not in cache:
                t = time.monotonic()
                cache[key] = {
                    "scores": await sd._score_descriptions(it["text"]),
                    "elapsed": round(time.monotonic() - t, 2),
                }
                path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            it["scores"] = cache[key]["scores"]
            it["elapsed"] = cache[key]["elapsed"]
    else:
        desc = {}
        for kind, text in sd.CAPABILITY_DESCRIPTIONS.items():
            key = f"d::{text}"
            if key not in cache:
                cache[key] = await _embed(text, is_query=False)
            desc[kind] = cache[key]
        for it in items:
            key = f"q::{it['text']}"
            if key not in cache:
                cache[key] = await _embed(it["text"], is_query=True)
            it["scores"] = {k: _cosine(cache[key], v) for k, v in desc.items()}
            it["elapsed"] = None
        path.write_text(json.dumps(cache), encoding="utf-8")


# ── Report ───────────────────────────────────────────────────────────────────

def _f(x):
    return f"{x:.3f}"


async def main(scorer: str) -> None:
    items = build_items()
    await score_items(items, scorer)
    thr = sd.SEMANTIC_DISPATCH_THRESHOLD if scorer == "rerank" else 0.86
    sd.SEMANTIC_DISPATCH_THRESHOLD = thr  # so the force-armed control below uses this arm's threshold

    for it in items:
        it["phrase_kind"] = phrase_aggregate_kind(it["text"])
        ov = _deterministic_route_override(it["text"])
        it["router_override"] = ov["route"] if ov else None
        # The semantic step is reached only when the phrase chain lands on a
        # generic catch-all AND no deterministic router override claimed the
        # question first (the router calls prepare() after every override).
        it["reaches_semantic"] = (
            it["phrase_kind"] in _GENERIC_AGGREGATE_KINDS and it["router_override"] is None
        )
        m = sd.rank_scores(it["scores"])
        it["best"], it["score"] = m.kind, round(m.score, 4)
        it["runner_up"], it["runner_up_score"] = m.runner_up, round(m.runner_up_score, 4)
        it["margin"] = round(m.score - m.runner_up_score, 4)
        if it["set"] == "target":
            want = it["expected_kind"]
        elif it["set"] in ("para", "gold") and it["gold_route"] == "XAGG":
            want = it["gold_kind"]
        else:
            want = None  # must not fire
        if want in _GENERIC_AGGREGATE_KINDS:
            want = None
        it["want"] = want
        it["fires"] = (
            it["reaches_semantic"] and it["score"] >= thr
            and it["best"] not in sd.NON_DISPATCHABLE_KINDS
        )
        del it["scores"]

    reached = [it for it in items if it["reaches_semantic"]]
    tp = [it for it in reached if it["want"] and it["best"] == it["want"]]
    hz = [it for it in reached if it["best"] not in sd.NON_DISPATCHABLE_KINDS and it["best"] != it["want"]]
    absorbed = [it for it in reached if it["best"] in sd.NON_DISPATCHABLE_KINDS]

    print(f"\nscorer={scorer}  threshold={thr}")
    print(f"Items: {len(items)}  reach the semantic step: {len(reached)}  "
          f"(by set: {', '.join(f'{s}={sum(1 for it in reached if it['set']==s)}' for s in ('gold','para','m116','m144','exemplar','hazard','target'))})")
    print(f"  true positives (best == wanted kind): {len(tp)}")
    print(f"  absorbed by a generic/refusal description: {len(absorbed)}")
    print(f"  hazards (best is a WRONG dispatchable kind): {len(hz)}")

    def scores_of(xs):
        return sorted((x["score"] for x in xs), reverse=True)

    print("\nTRUE-POSITIVE scores (desc):", " ".join(_f(s) for s in scores_of(tp)))
    print("HAZARD scores (desc):       ", " ".join(_f(s) for s in scores_of(hz)))
    fired_tp = [x for x in tp if x["score"] >= thr]
    if fired_tp and hz:
        print(f"\nlowest FIRED true positive = {min(scores_of(fired_tp)):.3f}   "
              f"highest hazard = {max(scores_of(hz)):.3f}   "
              f"MARGIN = {min(scores_of(fired_tp)) - max(scores_of(hz)):+.3f}")
    if tp and hz:
        print(f"lowest true positive OF ALL = {min(scores_of(tp)):.3f}  (strict margin "
              f"{min(scores_of(tp)) - max(scores_of(hz)):+.3f}; the ones below threshold fall through as today)")
    print("\nHazards, highest first:")
    for it in sorted(hz, key=lambda x: -x["score"])[:12]:
        print(f"  {it['score']:.3f}  {it['id']:<28} best={it['best']:<34} want={it['want']}  | {it['text'][:70]}")
    print("\nTrue positives, all:")
    for it in sorted(tp, key=lambda x: -x["score"]):
        print(f"  {it['score']:.3f}  {it['id']:<28} best={it['best']:<34} {'FIRES' if it['score'] >= thr else 'below'} | {it['text'][:70]}")

    print("\nTHRESHOLD SWEEP (items reaching the semantic step):")
    print("  thr    TP-fired  hazards-fired")
    steps = [x / 100 for x in range(10, 96, 5)] if scorer == "rerank" else [x / 100 for x in range(80, 93)]
    for t in steps:
        print(f"  {t:.2f}   {sum(1 for it in tp if it['score'] >= t):>3}/{len(tp):<3}    {sum(1 for it in hz if it['score'] >= t):>3}/{len(hz)}")

    # ── The 96-paraphrase reach, before/after, per language ──
    print(f"\nPARAPHRASE REACH at threshold {thr} (XAGG-gold paraphrases, n=63)")
    print("  'kind'  = resolve_aggregate_kind() returns gold's kind")
    print("  'place' = route is XAGG (override, semantic, else Module 92's local-LLM route) AND gold's kind")
    per = {}
    for it in items:
        if it["set"] != "para" or it["gold_route"] != "XAGG":
            continue
        d = per.setdefault(it["language"], {"n": 0, "kind_before": 0, "kind_after": 0, "place_before": 0, "place_after": 0})
        d["n"] += 1
        after_kind = it["best"] if it["fires"] else it["phrase_kind"]
        kb, ka = it["phrase_kind"] == it["gold_kind"], after_kind == it["gold_kind"]
        route_before = it["router_override"] or it["llm_route"]
        route_after = it["router_override"] or ("XAGG" if it["fires"] else it["llm_route"])
        d["kind_before"] += kb
        d["kind_after"] += ka
        d["place_before"] += (route_before == "XAGG" and kb)
        d["place_after"] += (route_after == "XAGG" and ka)
        it["after_kind"], it["route_before"], it["route_after"] = after_kind, route_before, route_after
    tot = {k: 0 for k in ("n", "kind_before", "kind_after", "place_before", "place_after")}
    for lang, d in per.items():
        for k in tot:
            tot[k] += d[k]
        print(f"  {lang:<9} n={d['n']:<3} kind {d['kind_before']:>2}->{d['kind_after']:<2}   place {d['place_before']:>2}->{d['place_after']:<2}")
    print(f"  {'total':<9} n={tot['n']:<3} kind {tot['kind_before']:>2}->{tot['kind_after']:<2}   place {tot['place_before']:>2}->{tot['place_after']:<2}")

    pulled = [it for it in items if it["set"] in ("para", "m116", "exemplar", "m144", "hazard")
              and it["gold_route"] != "XAGG" and it["fires"]]
    print(f"\nNon-XAGG items the layer would pull to XAGG at {thr}: {len(pulled)}")
    for it in pulled:
        print(f"  {it['score']:.3f}  {it['id']:<28} best={it['best']:<34} | {it['text'][:70]}")
    wrong_xagg = [it for it in items if it["gold_route"] == "XAGG" and it["fires"] and it["want"] and it["best"] != it["want"]]
    print(f"XAGG items the layer would send to the WRONG kind at {thr}: {len(wrong_xagg)}")
    for it in wrong_xagg:
        print(f"  {it['score']:.3f}  {it['id']:<28} best={it['best']:<34} want={it['want']} | {it['text'][:70]}")

    # ── Robustness: generic-tier items a router override claims today ──
    # They never reach the layer (the router prepares AFTER every override),
    # but a rewording that dodged the override regex would — so what the
    # layer would do with them is the honest measure of the count family's
    # absorbing descriptions.
    claimed = [it for it in items if it["phrase_kind"] in _GENERIC_AGGREGATE_KINDS
               and it["router_override"] is not None]
    would_fire = [it for it in claimed if it["score"] >= thr and it["best"] not in sd.NON_DISPATCHABLE_KINDS]
    print("")
    print(f"ROBUSTNESS — generic-tier items claimed by a router override: {len(claimed)}; "
          f"the layer WOULD fire on {len(would_fire)} if they reached it:")
    for it in sorted(would_fire, key=lambda x: -x["score"]):
        print(f"  {it['score']:.3f}  {it['id']:<28} ov={it['router_override']:<8} best={it['best']:<34} | {it['text'][:60]}")

    # ── All-32 gold equality control: force-armed on every gold question ──
    sd.reset_for_tests()
    for it in items:
        if it["set"] == "gold":
            sd.seed_for_tests(it["text"], sd.SemanticMatch(it["best"], it["score"], it["runner_up"], it["runner_up_score"]))
    moved = [it for it in items if it["set"] == "gold" and resolve_aggregate_kind(it["text"]) != it["phrase_kind"]]
    print(f"\nALL-32 GOLD EQUALITY CONTROL (semantic layer FORCE-ARMED on all 32 at {thr}): moved {len(moved)}/32")
    for it in moved:
        print(f"  {it['id']} {it['phrase_kind']} -> {resolve_aggregate_kind(it['text'])} ({it['score']:.3f})")
    sd.reset_for_tests()

    if scorer == "rerank":
        el = [it["elapsed"] for it in items if it.get("elapsed")]
        print(f"\nreranker latency over {len(el)} calls: mean {sum(el)/len(el):.2f}s max {max(el):.2f}s")

    OUT[scorer].write_text(json.dumps({
        "scorer": scorer, "threshold": thr, "table_hash": table_hash(),
        "descriptions": sd.CAPABILITY_DESCRIPTIONS, "items": items,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT[scorer]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scorer", choices=["rerank", "e5"], default="rerank")
    args = ap.parse_args()
    asyncio.run(main(args.scorer))
