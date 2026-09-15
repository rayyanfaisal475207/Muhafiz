# -*- coding: utf-8 -*-
"""
Modules 158/164 — offline measurement of semantic sub-agent selection.

Scores every corpus item ONCE against `harness/subagent_selection.py`'s
description table (cached to disk, keyed by the table's hash), and reports:

  * per item: how the deterministic chain decides it today (XAGG one-call
    skip / trigger / plan / undecided), whether the semantic step is
    REACHED (undecided AND cross-case AND not DIRECT/file), the best
    description, score, runner-up, and the verdict at the shipped threshold;
  * the true-positive and hazard distributions and the margin, per
    threshold — the number the threshold is chosen from;
  * reach over the target set (Module 116's six, Module 92's nine
    Meta-Analysis paraphrases, Module 79's four, this module's two)
    before and after;
  * the all-32 selection equality control with the layer force-armed at
    every gold question's measured decision;
  * the row-162 measurement: where Module 145's aggregate-layer top hazard
    (M116-G1-en) lands at THIS layer, and whether adding the plan
    descriptions to Module 145's table as absorbing classes would move it
    out of the aggregate layer's candidate set.

Corpus (every item is a hazard unless it is a Meta-Analysis target):
  evaluation/Gold_QA_Dataset_Final32_With_Answers.json  32 gold (CR3/G1/G6 targets)
  evaluation/gold32_paraphrases.json                    96 (Module 92; 9 targets)
  docs/.../module116_paraphrases.json                   6 targets
  docs/.../module144_paraphrases.json                   13 hazards
  prompts/router.txt                                    exemplars, hazards
  docs/.../module145_hazards.json + module145_targets.json  30 hazards
  docs/.../module158_hazards.json                       20 hazards (pre-written)
  docs/.../module158_targets.json                       22 targets (pre-written; de-duplicated against the above)

USAGE
    PYTHONPATH=. python -X utf8 evaluation/module158_selection_probe.py
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

from src.pipeline import semantic_dispatch as sd  # noqa: E402
from src.pipeline.harness import subagent_selection as ss  # noqa: E402
from src.pipeline.harness import supervisor as sup  # noqa: E402
from src.pipeline.router import _deterministic_route_override  # noqa: E402
from router_paraphrase_harness import route_exemplars  # noqa: E402

RESULTS = ROOT / "docs" / "gold-qa-wave2-results"
GOLD_PATH = ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
PARA_PATH = ROOT / "evaluation" / "gold32_paraphrases.json"
ROUTE_BASELINE = ROOT / "evaluation" / "gold32_route_baseline.json"
LOCAL_CACHE = RESULTS / "module92_harness_cache" / "local.json"
ROUTE_DICTS = RESULTS / "module145_live" / "module145_route_dict_after.json"
M116 = RESULTS / "module116_paraphrases.json"
M144 = RESULTS / "module144_paraphrases.json"
H145 = RESULTS / "module145_hazards.json"
T145 = RESULTS / "module145_targets.json"
H158 = RESULTS / "module158_hazards.json"
T158 = RESULTS / "module158_targets.json"
SCORE_CACHE = RESULTS / "module158_rerank_cache.json"
OUT = RESULTS / "module158_probe.json"

MA_GOLD = {"CR3", "G1", "G6"}


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build_items() -> list[dict]:
    gold = _load(GOLD_PATH)
    routes = _load(ROUTE_BASELINE)["routes"]
    local = _load(LOCAL_CACHE)
    route_dicts = _load(ROUTE_DICTS)

    def route_of(gid):
        r = routes[gid]
        return r["route"] if isinstance(r, dict) else r

    items = []
    seen = set()

    def add(it):
        key = " ".join(it["text"].split())
        if key in seen:
            return
        seen.add(key)
        items.append(it)

    for g in gold:
        rd = route_dicts.get(g["id"], {})
        add({
            "id": f"{g['id']}::gold", "gold_id": g["id"], "set": "gold",
            "language": g.get("language", "en"), "text": g["question"],
            "route": rd.get("route") or route_of(g["id"]),
            "case_scope": rd.get("case_scope") or "cross_case",
            "output_format": rd.get("output_format") or "chat",
            "target": g["id"] in MA_GOLD,
            "plan": {"CR3": "record_consistency", "G1": "caseload_review", "G6": "orientation_note"}.get(g["id"]),
        })
    for p in _load(PARA_PATH)["paraphrases"]:
        add({
            "id": f"{p['gold_id']}::para::{p['language']}", "gold_id": p["gold_id"], "set": "para",
            "language": p["language"], "text": p["text"],
            "route": local.get(f"{p['gold_id']}::para::{p['language']}", {}).get("route") or route_of(p["gold_id"]),
            "case_scope": "within_case" if p["gold_id"].startswith("KB") else "cross_case",
            "output_format": "chat",
            "target": p["gold_id"] in MA_GOLD,
            "plan": {"CR3": "record_consistency", "G1": "caseload_review", "G6": "orientation_note"}.get(p["gold_id"]),
        })
    for i, p in enumerate(_load(M116)["paraphrases"]):
        add({
            "id": f"M116-{p['gold_id']}-{p['language']}", "gold_id": p["gold_id"], "set": "m116",
            "language": p["language"], "text": p["text"], "route": "XNETWORK",
            "case_scope": "cross_case", "output_format": "chat", "target": True,
            "plan": {"CR3": "record_consistency", "G1": "caseload_review", "G6": "orientation_note"}[p["gold_id"]],
        })
    for t in _load(T158)["targets"]:
        add({
            "id": t["id"], "gold_id": t.get("gold_id"), "set": "t158", "language": t["language"],
            "text": t["text"], "route": None, "case_scope": "cross_case", "output_format": "chat",
            "target": True, "plan": t["plan"], "today": t["today"],
        })
    for key, p in _load(M144).items():
        add({
            "id": f"M144-{key}", "gold_id": None, "set": "m144", "language": p.get("language", "en"),
            "text": p["question"], "route": "XAGG", "case_scope": "cross_case", "output_format": "chat",
            "target": False, "plan": None,
        })
    for i, (q, route) in enumerate(route_exemplars()):
        add({
            "id": f"EX{i:02d}::{route}", "gold_id": None, "set": "exemplar", "language": "mixed",
            "text": q, "route": route, "case_scope": "cross_case" if route in ("XAGG", "XGRAPH", "XNETWORK") else "within_case",
            "output_format": "chat", "target": False, "plan": None,
        })
    for h in _load(H145)["hazards"]:
        add({
            "id": h["id"], "gold_id": None, "set": "h145", "language": h["language"], "text": h["text"],
            "route": None, "case_scope": "cross_case", "output_format": "chat", "target": False, "plan": None,
            "why": h["why"],
        })
    for t in _load(T145)["targets"]:
        add({
            "id": t["id"], "gold_id": None, "set": "t145", "language": t["language"], "text": t["text"],
            "route": "XAGG", "case_scope": "cross_case", "output_format": "chat", "target": False, "plan": None,
            "why": f"Module 145 target — a one-call aggregate ({t['expected_kind']})",
        })
    for h in _load(H158)["hazards"]:
        add({
            "id": h["id"], "gold_id": None, "set": "h158", "language": h["language"], "text": h["text"],
            "route": None, "case_scope": "cross_case", "output_format": "chat", "target": False, "plan": None,
            "why": h["why"], "near": h["near"],
        })
    return items


def table_hash(table: dict) -> str:
    blob = json.dumps(table, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]


async def score_items(items: list[dict]) -> None:
    cache = _load(SCORE_CACHE) if SCORE_CACHE.exists() else {}
    h = table_hash(ss.CAPABILITY_DESCRIPTIONS)
    for n, it in enumerate(items, 1):
        key = f"{h}::{it['text']}"
        if key not in cache:
            t = time.monotonic()
            for attempt in range(6):  # the tunnel drops connections; resume, never re-score
                try:
                    scores = await ss.gate.score(it["text"])
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"  retry {attempt + 1} after {type(exc).__name__}", flush=True)
                    await asyncio.sleep(5 * (attempt + 1))
            else:
                raise RuntimeError("scorer unreachable")
            cache[key] = {"scores": scores, "elapsed": round(time.monotonic() - t, 2)}
            SCORE_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            print(f"  scored {n}/{len(items)} {it['id']} ({cache[key]['elapsed']}s)", flush=True)
        it["scores"] = cache[key]["scores"]
        it["elapsed"] = cache[key]["elapsed"]


def _f(x):
    return f"{x:.3f}"


async def main() -> None:
    items = build_items()
    print(f"corpus: {len(items)} items; table: {len(ss.CAPABILITY_DESCRIPTIONS)} descriptions "
          f"({len(ss.DISPATCHABLE_KINDS)} dispatchable)")
    await score_items(items)
    thr = ss.SUBAGENT_SELECTION_THRESHOLD

    for it in items:
        ov = _deterministic_route_override(it["text"])
        it["router_override"] = ov["route"] if ov else None
        route = it["router_override"] or it["route"] or "UNKNOWN"
        it["route_used"] = route
        it["decided"] = sup._meta_analysis_decided_by_phrase(route, it["text"])
        rr = {"route": route if route != "UNKNOWN" else "XNETWORK", "case_scope": it["case_scope"],
              "output_format": it["output_format"]}
        it["reaches"] = sup.semantic_selection_applies(rr, it["text"])
        m = sd.rank_scores(it["scores"])
        it["best"], it["score"] = m.kind, round(m.score, 4)
        it["runner_up"], it["runner_up_score"] = m.runner_up, round(m.runner_up_score, 4)
        # the best PLAN description, whatever the overall best
        plan_best = max(((k, v) for k, v in it["scores"].items() if k in ss.DISPATCHABLE_KINDS), key=lambda kv: kv[1])
        it["best_plan"], it["best_plan_score"] = plan_best[0], round(plan_best[1], 4)
        it["fires"] = it["reaches"] and it["best"] in ss.DISPATCHABLE_KINDS and it["score"] >= thr
        it["selected_before"] = it["decided"] in ("trigger",) or (it["decided"] or "").startswith("plan:") and False
        # before = origin/main: only the trigger selects
        it["selected_before"] = it["decided"] == "trigger"
        it["selected_after"] = it["decided"] == "trigger" or (it["decided"] or "").startswith("plan:") or it["fires"]
        it["semantic_scores"] = {k: round(v, 4) for k, v in it["scores"].items() if k in ss.DISPATCHABLE_KINDS}
        del it["scores"]

    reached = [it for it in items if it["reaches"]]
    tp = [it for it in reached if it["target"] and it["best"] == it["plan"]]
    tp_wrong_plan = [it for it in reached if it["target"] and it["best"] in ss.DISPATCHABLE_KINDS and it["best"] != it["plan"]]
    hz = [it for it in reached if not it["target"] and it["best"] in ss.DISPATCHABLE_KINDS]
    absorbed_hz = [it for it in reached if not it["target"] and it["best"] not in ss.DISPATCHABLE_KINDS]
    absorbed_tp = [it for it in reached if it["target"] and it["best"] not in ss.DISPATCHABLE_KINDS]

    print(f"\nthreshold={thr}")
    print(f"Items: {len(items)}  reach the semantic step: {len(reached)}")
    for s in ("gold", "para", "m116", "t158", "m144", "exemplar", "h145", "t145", "h158"):
        n_all = sum(1 for it in items if it["set"] == s)
        n_r = sum(1 for it in reached if it["set"] == s)
        print(f"  {s:<9} {n_r:>3}/{n_all}")
    print(f"  targets reaching: {sum(1 for it in reached if it['target'])}  "
          f"true positives (best == own plan): {len(tp)}  wrong plan: {len(tp_wrong_plan)}  absorbed: {len(absorbed_tp)}")
    print(f"  non-targets reaching: {sum(1 for it in reached if not it['target'])}  "
          f"hazards (best is a plan): {len(hz)}  absorbed: {len(absorbed_hz)}")

    def scores_of(xs):
        return sorted((x["score"] for x in xs), reverse=True)

    print("\nTRUE-POSITIVE scores (desc):", " ".join(_f(s) for s in scores_of(tp)))
    print("HAZARD scores (desc):       ", " ".join(_f(s) for s in scores_of(hz)) or "(none)")
    fired_tp = [x for x in tp if x["score"] >= thr]
    if fired_tp and hz:
        print(f"\nlowest FIRED true positive = {min(scores_of(fired_tp)):.3f}   highest hazard = {max(scores_of(hz)):.3f}"
              f"   MARGIN = {min(scores_of(fired_tp)) - max(scores_of(hz)):+.3f}")
    elif fired_tp:
        print(f"\nlowest FIRED true positive = {min(scores_of(fired_tp)):.3f}   no hazard has a plan as its best description")
    if tp and hz:
        print(f"lowest true positive OF ALL = {min(scores_of(tp)):.3f}  (strict margin {min(scores_of(tp)) - max(scores_of(hz)):+.3f})")

    # The hazard-side number the brief asks for: over ALL non-targets,
    # whether or not they reach the layer today — the highest score any of
    # them gives to ANY plan description (the number a rewording that dodged
    # the structural gates would face).
    all_nt = [it for it in items if not it["target"]]
    worst = sorted(all_nt, key=lambda x: -x["best_plan_score"])
    print(f"\nHAZARD-SIDE, ALL {len(all_nt)} NON-TARGETS (reach or not): highest best-plan score = {worst[0]['best_plan_score']:.3f}")
    print("  top 15 by best-plan score (reach? / overall best / best plan):")
    for it in worst[:15]:
        print(f"  {it['best_plan_score']:.3f}  {it['id']:<26} reach={'Y' if it['reaches'] else 'n'} "
              f"best={it['best']:<36}({it['score']:.3f}) plan={it['best_plan']:<30} | {it['text'][:60]}")
    gold_nt = [it for it in all_nt if it["set"] == "gold"]
    gw = sorted(gold_nt, key=lambda x: -x["best_plan_score"])
    print(f"\n  the {len(gold_nt)} non-Meta-Analysis GOLD questions: highest best-plan score = {gw[0]['best_plan_score']:.3f} "
          f"({gw[0]['id']} -> {gw[0]['best_plan']}); those whose OVERALL best is a plan: "
          f"{[it['id'] for it in gold_nt if it['best'] in ss.DISPATCHABLE_KINDS]}")
    for it in gw[:8]:
        print(f"    {it['best_plan_score']:.3f}  {it['id']:<10} reach={'Y' if it['reaches'] else 'n'} best={it['best']:<36}({it['score']:.3f}) plan={it['best_plan']}")

    print("\nHazards that REACH the layer and whose best is a plan, highest first:")
    for it in sorted(hz, key=lambda x: -x["score"])[:15]:
        print(f"  {it['score']:.3f}  {it['id']:<26} best={it['best']:<30} runner_up={it['runner_up']}({it['runner_up_score']:.3f}) | {it['text'][:60]}")
    print("\nTrue positives, all (targets reaching the layer whose best is their plan):")
    for it in sorted(tp, key=lambda x: -x["score"]):
        print(f"  {it['score']:.3f}  {it['id']:<26} {'FIRES' if it['score'] >= thr else 'below'} runner_up={it['runner_up']}({it['runner_up_score']:.3f}) | {it['text'][:60]}")
    print("\nTargets reaching the layer that are NOT true positives:")
    for it in tp_wrong_plan + absorbed_tp:
        print(f"  {it['score']:.3f}  {it['id']:<26} best={it['best']:<36} own_plan={it['plan']}({it['semantic_scores'].get(it['plan'], 0):.3f}) | {it['text'][:60]}")

    print("\nTHRESHOLD SWEEP (items reaching the semantic step):")
    print("  thr    TP-fired  hazards-fired   | all-non-target best-plan >= thr")
    for t in [x / 100 for x in range(10, 96, 5)]:
        print(f"  {t:.2f}   {sum(1 for it in tp if it['score'] >= t):>3}/{len(tp):<3}    {sum(1 for it in hz if it['score'] >= t):>3}/{len(hz):<3}"
              f"        | {sum(1 for it in all_nt if it['best_plan_score'] >= t and it['best'] in ss.DISPATCHABLE_KINDS):>3}/{len(all_nt)}")

    # ── Target reach, before/after ──
    print("\nTARGET REACH (selects Meta-Analysis): before = trigger only; after = trigger | plan | semantic")
    targets = [it for it in items if it["target"]]
    for s in ("gold", "m116", "para", "t158"):
        xs = [it for it in targets if it["set"] == s]
        if xs:
            print(f"  {s:<6} {sum(it['selected_before'] for it in xs):>2}/{len(xs)} -> {sum(it['selected_after'] for it in xs):>2}/{len(xs)}")
    print(f"  total  {sum(it['selected_before'] for it in targets):>2}/{len(targets)} -> {sum(it['selected_after'] for it in targets):>2}/{len(targets)}")
    for it in targets:
        how = it["decided"] if it["decided"] else (f"semantic {it['best']}({it['score']:.3f})" if it["fires"] else f"NO: best={it['best']}({it['score']:.3f}) own_plan={it['semantic_scores'].get(it['plan'], 0):.3f}")
        print(f"    {it['id']:<22} {it['language']:<8} before={'Y' if it['selected_before'] else 'n'} after={'Y' if it['selected_after'] else 'n'}  {how}")

    # ── All-32 selection equality, force-armed ──
    print("\nALL-32 SELECTION CONTROL — force-armed with each gold question's measured decision:")
    ss.reset_for_tests()
    moved = 0
    route_dicts = _load(ROUTE_DICTS)
    for it in items:
        if it["set"] != "gold":
            continue
        rr = route_dicts[it["gold_id"]]
        before = sup.classify_to_subagent(rr, it["text"])
        ss.seed_for_tests(it["text"], sd.SemanticMatch(it["best"], it["score"], it["runner_up"], it["runner_up_score"]))
        after = sup.classify_to_subagent(rr, it["text"])
        it["subagent_before"], it["subagent_after"] = before, after
        if before != after:
            moved += 1
            print(f"  MOVED {it['gold_id']}: {before} -> {after}")
    print(f"  moved {moved}/32")
    ss.reset_for_tests()

    # ── Row 162: where the aggregate layer's top hazard lands here, and
    #    whether the plan descriptions absorb it at the aggregate layer ──
    print("\nROW 162 — M116-G1-en at this layer, and at the aggregate layer with the plan descriptions absorbed:")
    g1 = next(it for it in items if it["id"] == "M116-G1-en")
    print(f"  this layer: best={g1['best']}({g1['score']:.3f}) runner_up={g1['runner_up']}({g1['runner_up_score']:.3f}) "
          f"fires={g1['fires']} reaches={g1['reaches']}")
    # Module 145's cache holds its scores against its own table; the plan
    # scores are pair-independent, so the union's argmax is computable
    # offline from the two caches.
    c145 = _load(RESULTS / "module145_rerank_cache.json")
    h145 = table_hash(sd.CAPABILITY_DESCRIPTIONS)
    flips = []
    for it in items:
        k = f"{h145}::{it['text']}"
        if k not in c145:
            continue
        agg = c145[k]["scores"]
        agg_best = max(agg.items(), key=lambda kv: kv[1])
        union = dict(agg)
        union.update({f"plan:{p}": s for p, s in it["semantic_scores"].items()})
        u_best = max(union.items(), key=lambda kv: kv[1])
        it["agg_best"], it["agg_best_score"] = agg_best[0], round(agg_best[1], 4)
        it["union_best"], it["union_best_score"] = u_best[0], round(u_best[1], 4)
        if agg_best[0] != u_best[0]:
            flips.append(it)
    if "agg_best" in g1:
        print(f"  aggregate layer today: best={g1['agg_best']}({g1['agg_best_score']:.3f});  with plans absorbed: best={g1['union_best']}({g1['union_best_score']:.3f})")
    print(f"  items whose aggregate-layer argmax would flip to a plan description: {len(flips)}")
    for it in flips:
        print(f"    {it['id']:<26} {it['agg_best']:<32}({it['agg_best_score']:.3f}) -> {it['union_best']}({it['union_best_score']:.3f}) target={it['target']} | {it['text'][:50]}")

    OUT.write_text(json.dumps({
        "threshold": thr, "table_hash": table_hash(ss.CAPABILITY_DESCRIPTIONS),
        "n_items": len(items), "n_reached": len(reached),
        "true_positives": len(tp), "hazards": len(hz),
        "lowest_fired_tp": min(scores_of(fired_tp)) if fired_tp else None,
        "highest_hazard": max(scores_of(hz)) if hz else None,
        "highest_nontarget_best_plan_score": worst[0]["best_plan_score"],
        "gold_moved": moved,
        "items": items,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
