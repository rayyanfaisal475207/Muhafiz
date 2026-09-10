# -*- coding: utf-8 -*-
"""
Module 92 — route-accuracy harness over 32 gold questions + 96 paraphrases.

WHAT THIS MEASURES
------------------
`router.py::route_query()` decides a route in two stages: a deterministic
regex pre-classification (`_deterministic_route_override()`) that
short-circuits to SQL/RAG/XAGG/XGRAPH/XNETWORK on a match, and — on a miss —
a local Qwen3-14B classifier that router.py's own module comment records as
unreliable on novel phrasings.

Gold looks healthy because three rounds of Gold-QA fixes added regex patterns
for gold's own wordings. This harness asks the question gold cannot: does a
route survive a REWORDING, in each of the three languages the product serves?

GROUND TRUTH
------------
`evaluation/gold32_pass{1,2,3}_outputs.json` carry a `route` per gold question.
Module 27 recorded those routes stable 32/32 across all three passes, and this
harness re-asserts that stability before using them. A paraphrase is scored
correct when it reaches the SAME route as the gold question it paraphrases.

CANDIDATES
----------
  override_only  The deterministic regex layer alone. A miss counts as wrong.
                 Purely offline — no model server, no network, no database.
                 This is the diagnostic, not a shippable design: it is what
                 the fast path can do by itself.
  local          Shipped behaviour: override, else the local Qwen3-14B
                 classifier with prompts/router.txt. Needs LOCAL_LLM_URL.
  cloud          Override, else a cloud classifier (Gemini) with the SAME
                 prompt. Candidate direction 1.
  normalise      Override, else translate the query to English, re-run the
                 override on the translation, else classify the translation.
                 Candidate direction 2.
  knn            Override, else multilingual-e5 nearest-neighbour over route
                 exemplars parsed from prompts/router.txt's own few-shot
                 block. Candidate direction 3.
  knn_cloud      Override, else kNN when it is confident, else cloud.
                 The hybrid the measurements pointed at.

Every candidate answers with a route string, and every run is cached per
(candidate, item) so a rerun costs nothing and a partial run can be resumed.

USAGE
-----
    PYTHONPATH=. python -X utf8 evaluation/router_paraphrase_harness.py \
        --candidate override_only
    PYTHONPATH=. python -X utf8 evaluation/router_paraphrase_harness.py \
        --candidate cloud --report
    PYTHONPATH=. python -X utf8 evaluation/router_paraphrase_harness.py --table
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GOLD_PATH = ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
PARA_PATH = ROOT / "evaluation" / "gold32_paraphrases.json"
PASS_PATHS = [ROOT / "evaluation" / f"gold32_pass{i}_outputs.json" for i in (1, 2, 3)]
ROUTER_PROMPT = ROOT / "prompts" / "router.txt"
CACHE_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module92_harness_cache"

LANGS = ["en", "roman_ur", "ur"]


# ── Items and ground truth ────────────────────────────────────────────────────

def _load_json(p: Path):
    return json.load(io.open(p, encoding="utf-8"))


# Two gold questions were DELIBERATELY re-routed after the three-pass
# artefacts were recorded, so the artefacts are stale for them and scoring
# against the recorded value would penalise a landed fix.
#
# Module 78 (`fix/router-paraphrase-generalisation`, merged as PR #62) routes a
# compound legal-KB question — "what does the law require, AND does our record
# match it?" — to RAG whenever `rag.py::_is_legal_kb_intent()` is True. Its own
# writeup records the before state as exactly what the artefacts hold: KB3
# XNETWORK 3/3 and KB9 XAGG 3/3, with both questions' `_KB_DATA_HALF_PLANS`
# entries therefore never consulted. RAG is the intended after state for both.
#
# Recorded here as a named, justified exception rather than by regenerating the
# artefacts, so the ground truth stays auditable and this harness keeps saying
# out loud which two questions it is not taking from the committed run.
DELIBERATE_ROUTE_CHANGES = {
    "KB3": ("RAG", "Module 78 (PR #62) — was XNETWORK 3/3, deliberately routed to RAG"),
    "KB9": ("RAG", "Module 78 (PR #62) — was XAGG 3/3, deliberately routed to RAG"),
}


def expected_routes() -> dict[str, str]:
    """
    The route each gold question should take.

    Taken from the committed three-pass artefacts, which Module 27 recorded
    stable 32/32 — re-asserted here rather than assumed — with the two
    deliberate post-artefact changes above applied on top.
    """
    per_pass: dict[str, list[str]] = {}
    for p in PASS_PATHS:
        for row in _load_json(p):
            per_pass.setdefault(row["id"], []).append(row.get("route"))
    unstable = {k: v for k, v in per_pass.items() if len(set(v)) != 1}
    if unstable:
        raise SystemExit(f"Ground truth is not stable across passes: {unstable}")
    exp = {k: v[0] for k, v in per_pass.items()}
    for qid, (route, _why) in DELIBERATE_ROUTE_CHANGES.items():
        exp[qid] = route
    return exp


def items() -> list[dict]:
    """32 gold questions + 96 paraphrases, each with its expected route."""
    gold = {g["id"]: g for g in _load_json(GOLD_PATH)}
    exp = expected_routes()
    out = []
    for qid, g in gold.items():
        out.append({
            "key": f"{qid}::gold",
            "gold_id": qid,
            "kind": "gold",
            "language": g["language"],
            "text": g["question"],
            "expected": exp[qid],
        })
    for p in _load_json(PARA_PATH)["paraphrases"]:
        out.append({
            "key": f"{p['gold_id']}::para::{p['language']}",
            "gold_id": p["gold_id"],
            "kind": "paraphrase",
            "language": p["language"],
            "text": p["text"],
            "expected": exp[p["gold_id"]],
        })
    return out


# ── Route exemplars, parsed from the router prompt's own few-shot block ───────

def route_exemplars() -> list[tuple[str, str]]:
    """
    (query, route) pairs from prompts/router.txt.

    Deliberately parsed from the prompt rather than hand-written here: these
    are the router's OWN documented definition of each route, they already
    span English, Urdu script and Roman Urdu, and — the point that matters
    for this module — they are not gold. Building an exemplar bank out of the
    gold questions would repeat exactly the mistake this module exists to
    stop: fitting the router to the 32 strings it is scored on.

    The `ACTIVE_CASE:`-prefixed block at the end of router.txt is NOT parsed.
    It has a different shape, and three of its entries are gold questions
    verbatim (CR3, CR4, G6) — including them would smuggle gold back in.
    """
    txt = io.open(ROUTER_PROMPT, encoding="utf-8").read()
    pairs = re.findall(r'^Query:\s*"(.*?)"\s*\nOutput:\s*(\{.*?\})\s*$', txt, re.M | re.S)
    out = []
    for q, o in pairs:
        try:
            out.append((q, json.loads(o)["route"]))
        except Exception:
            continue
    return out


# ── Candidate classifiers ─────────────────────────────────────────────────────
# Each returns (route, detail) where detail records HOW the route was reached.

MISS = "MISS"  # the deterministic layer had no opinion


def _override(text: str) -> str | None:
    from src.pipeline.router import _deterministic_route_override
    d = _deterministic_route_override(text, case_id=None) or {}
    return d.get("route")


async def cand_override_only(text: str) -> tuple[str, str]:
    r = _override(text)
    return (r, "override") if r else (MISS, "miss")


def _no_case_message(text: str) -> str:
    """The exact user message route_query() builds when no case is active."""
    return (
        "ACTIVE_CASE: none (no case is currently selected — the query below "
        f"is being asked without any specific case active)\n\nQUESTION: {text}"
    )


# The cloud classifier is pinned to GEMINI, not to config.LLM_PROVIDER's Groq.
# This is a measured constraint, not a preference: router.py's own comment
# records the router system prompt at ~7650 tokens against this Groq account's
# 8000 TPM on-demand cap, which allows roughly ONE router call per minute and
# makes a 128-item sweep both impossible and a way to exhaust the shared
# free-tier quota every other agent on this machine depends on. Gemini
# flash-lite is the same lever the judge upgrade uses (see
# evaluation/gold32_score.py::_judge), and its key is already configured and
# rotated four ways.
CLOUD_PROVIDER = "gemini"
CLOUD_MODEL_NOTE = "gemini-2.5-flash (config.GEMINI_MODEL)"


async def _classify_llm(text: str, force_cloud: bool) -> tuple[str, str]:
    """
    The router's own classification call, with the router's own prompt and
    validation, pinned to either the local model or the cloud one.
    """
    from src.llm.client import call_llm
    from src.pipeline.json_extract import call_llm_json
    from src.pipeline.router import _SYSTEM_PROMPT, _VALID_ROUTES

    async def _llm(*a, **kw):
        kw["force_cloud"] = force_cloud
        if force_cloud:
            kw["provider_override"] = CLOUD_PROVIDER
        return await call_llm(*a, **kw)

    result, raw = await call_llm_json(
        system_prompt=_SYSTEM_PROMPT,
        user_message=_no_case_message(text),
        temperature=0.0,
        max_tokens=2000,
        cloud_max_tokens=1200,
        reasoning_effort="low",
        validate=lambda r: (
            isinstance(r, dict)
            and isinstance(r.get("route"), str)
            and r["route"].strip().upper() in _VALID_ROUTES
        ),
        schema_hint='"route", "case_scope", "target_entity", "output_format", "target_year", "confidence", "reason", "secondary_methods"',
        _call_llm=_llm,
        escalate_to_cloud_on_failure=False,
    )
    if not result:
        return "RAG", f"llm-unparsed:{str(raw)[:60]}"
    return str(result.get("route") or "RAG").upper(), "llm"


COMPACT_PROMPT_PATH = ROOT / "prompts" / "router_compact.txt"


async def _classify_compact(text: str, force_cloud: bool) -> tuple[str, str]:
    """
    The same classification, against prompts/router_compact.txt.

    WHY A SECOND PROMPT EXISTS AT ALL. prompts/router.txt has grown to ~13,000
    tokens across three rounds of Gold-QA few-shot additions. Groq's on_demand
    tier caps a single request at 8,000 tokens, so a router classification sent
    to Groq does not merely cost a round trip — it returns HTTP 413, always,
    before the model sees anything. Measured 2026-09-09: "Requested 13003,
    Limit 8000". That makes the cloud arm of candidate direction 1 impossible
    with the shipped prompt, and it is also why route_query()'s own
    `escalate_to_cloud_on_failure=True` cannot fire today.

    router_compact.txt carries the same route definitions and the same 74
    few-shot examples compressed from full JSON outputs to one line each, plus
    an explicit "language is never a routing signal" rule. It is built from
    router.txt's own content — NOT from the gold questions, which is the
    distinction this whole module rests on.
    """
    from src.llm.client import call_llm
    from src.pipeline.json_extract import call_llm_json
    from src.pipeline.router import _VALID_ROUTES

    prompt = io.open(COMPACT_PROMPT_PATH, encoding="utf-8").read()

    async def _llm(*a, **kw):
        kw["force_cloud"] = force_cloud
        return await call_llm(*a, **kw)

    result, raw = await call_llm_json(
        system_prompt=prompt,
        user_message=_no_case_message(text),
        temperature=0.0,
        max_tokens=2000,
        # 400, not the local 2000: with the compact prompt at ~2.5k tokens a
        # cloud request lands near 2.9k against Groq's 8000 TPM, which is what
        # makes a 95-call sweep possible at all without exhausting the shared
        # free-tier quota. router.py's own measurement of this model at
        # reasoning_effort="low" is 82 reasoning + 151 completion tokens, so
        # 400 is headroom, not a squeeze.
        cloud_max_tokens=400,
        reasoning_effort="low",
        validate=lambda r: (
            isinstance(r, dict)
            and isinstance(r.get("route"), str)
            and r["route"].strip().upper() in _VALID_ROUTES
        ),
        schema_hint='"route", "case_scope", "target_entity", "output_format", "target_year", "confidence", "reason", "secondary_methods"',
        _call_llm=_llm,
        escalate_to_cloud_on_failure=False,
    )
    if not result:
        return "RAG", f"compact-unparsed:{str(raw)[:60]}"
    return str(result.get("route") or "RAG").upper(), "compact"


async def cand_cloud_compact(text: str) -> tuple[str, str]:
    r = _override(text)
    if r:
        return r, "override"
    return await _classify_compact(text, force_cloud=True)


async def cand_local_compact(text: str) -> tuple[str, str]:
    """Control: is the win the compact PROMPT, or the cloud MODEL?"""
    r = _override(text)
    if r:
        return r, "override"
    return await _classify_compact(text, force_cloud=False)


async def cand_local(text: str) -> tuple[str, str]:
    r = _override(text)
    if r:
        return r, "override"
    return await _classify_llm(text, force_cloud=False)


async def cand_cloud(text: str) -> tuple[str, str]:
    r = _override(text)
    if r:
        return r, "override"
    return await _classify_llm(text, force_cloud=True)


_TRANSLATE_SYS = (
    "You translate a police-domain question into plain English. The input may "
    "be English, Urdu script, or Roman/Latin-script Urdu. Output ONLY the "
    "English translation of the question — no explanation, no transliteration "
    "notes, no quotes. Keep every entity, number, year and identifier exactly "
    "as written. If the input is already English, repeat it unchanged."
)


async def _to_english(text: str, force_cloud: bool) -> str:
    from src.llm.client import call_llm
    kw = {}
    if force_cloud:
        kw = {"force_cloud": True, "provider_override": CLOUD_PROVIDER}
    out = await call_llm(
        system_prompt=_TRANSLATE_SYS,
        user_message=text,
        temperature=0.0,
        max_tokens=800,
        cloud_max_tokens=400,
        reasoning_effort="low",
        **kw,
    )
    return (out or text).strip().strip('"')


async def _normalise(text: str, force_cloud: bool) -> tuple[str, str]:
    r = _override(text)
    if r:
        return r, "override"
    en = await _to_english(text, force_cloud)
    r = _override(en)
    if r:
        return r, f"override-after-translate | {en[:70]}"
    route, how = await _classify_llm(en, force_cloud=force_cloud)
    return route, f"translate+{how} | {en[:70]}"


async def cand_normalise(text: str) -> tuple[str, str]:
    return await _normalise(text, force_cloud=True)


async def cand_normalise_local(text: str) -> tuple[str, str]:
    """
    Candidate direction 2, run entirely on the local model.

    Worth measuring separately because direction 2's appeal is that it makes
    the ENGLISH regex patterns and English-competent classification apply to
    every language — and that claim can be tested without spending any cloud
    quota at all. It also isolates the translate step: `detail` records the
    English the router actually saw, so a wrong route can be attributed to a
    bad translation rather than to the classifier.
    """
    return await _normalise(text, force_cloud=False)


# ── Embedding nearest-neighbour ───────────────────────────────────────────────

_EXEMPLAR_VECS: list[tuple[str, str, list[float]]] | None = None


def _cos(a, b) -> float:
    n = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / n if n else 0.0


async def _exemplar_vectors():
    global _EXEMPLAR_VECS
    if _EXEMPLAR_VECS is None:
        from src.retrieval.embedder import embed_texts
        ex = route_exemplars()
        vecs = await embed_texts([q for q, _ in ex], task_type="RETRIEVAL_QUERY")
        _EXEMPLAR_VECS = [(q, r, v) for (q, r), v in zip(ex, vecs)]
    return _EXEMPLAR_VECS


async def _knn(text: str, k: int = 5) -> tuple[str, float, float]:
    """
    Return (route, margin, top_similarity).

    Routes are scored by summed similarity over the k nearest exemplars, not
    by a single nearest neighbour — e5 similarities are compressed (two
    unrelated police questions still sit around 0.80), so one neighbour is
    noisy while the mass of the top-k is not. `margin` is the winning route's
    share of the total top-k mass minus the runner-up's, and is what the
    hybrid candidate gates on.
    """
    from src.retrieval.embedder import embed_texts
    ex = await _exemplar_vectors()
    v = (await embed_texts([text], task_type="RETRIEVAL_QUERY"))[0]
    sims = sorted(((_cos(v, ev), r) for _, r, ev in ex), reverse=True)
    top = sims[:k]
    scores: dict[str, float] = {}
    for s, r in top:
        scores[r] = scores.get(r, 0.0) + s
    total = sum(scores.values()) or 1.0
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best = ranked[0][1] / total
    second = (ranked[1][1] / total) if len(ranked) > 1 else 0.0
    return ranked[0][0], best - second, top[0][0]


async def cand_knn(text: str) -> tuple[str, str]:
    r = _override(text)
    if r:
        return r, "override"
    route, margin, top = await _knn(text)
    return route, f"knn margin={margin:.3f} top={top:.3f}"


KNN_MARGIN_GATE = 0.35


async def cand_knn_cloud(text: str) -> tuple[str, str]:
    r = _override(text)
    if r:
        return r, "override"
    route, margin, top = await _knn(text)
    if margin >= KNN_MARGIN_GATE:
        return route, f"knn margin={margin:.3f}"
    route, how = await _classify_llm(text, force_cloud=True)
    return route, f"knn-abstain->{how}"


CANDIDATES = {
    "override_only": cand_override_only,
    "local": cand_local,
    "local_compact": cand_local_compact,
    "cloud": cand_cloud,
    "cloud_compact": cand_cloud_compact,
    "normalise": cand_normalise,
    "normalise_local": cand_normalise_local,
    "knn": cand_knn,
    "knn_cloud": cand_knn_cloud,
}


# ── Running and caching ───────────────────────────────────────────────────────

# ── Within-case control ───────────────────────────────────────────────────────

def exemplar_items() -> list[dict]:
    """
    The 74 `Query:`/`Output:` pairs in prompts/router.txt as a scored item set.

    WHY THIS EXISTS. The 32 gold questions and their 96 paraphrases are almost
    entirely cross-case (24 of 32 are XAGG), so they say nothing about
    DIRECT / SQL / WEB / GRAPH / GRAPH_HYBRID — the within-case surface that
    every ordinary query uses. A change to the classifier's PROMPT touches
    that surface too, and a regression there would be invisible in the
    paraphrase table. This control asks the classifier to reproduce the
    router's own documented answer for each of its own examples.

    It is a control, not a target: router.txt's examples are IN router.txt's
    prompt, so the `local`/`cloud` arms are being asked to repeat something
    they can see, while the compact arms see a compressed form of the same
    thing. A DROP here is meaningful; a small absolute number is not.
    """
    return [
        {"key": f"EX{i:02d}::{route}", "gold_id": f"EX{i:02d}", "kind": "exemplar",
         "language": "en", "text": q, "expected": route}
        for i, (q, route) in enumerate(route_exemplars())
    ]


# Set by --control: swaps the scored item set and the cache namespace, so a
# control run can never overwrite a paraphrase run's cache or vice versa.
ITEM_SET = "corpus"


def active_items() -> list[dict]:
    return exemplar_items() if ITEM_SET == "control" else items()


def cache_path(candidate: str) -> Path:
    suffix = "" if ITEM_SET == "corpus" else f".{ITEM_SET}"
    return CACHE_DIR / f"{candidate}{suffix}.json"


def load_cache(candidate: str) -> dict:
    p = cache_path(candidate)
    return _load_json(p) if p.exists() else {}


def save_cache(candidate: str, cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with io.open(cache_path(candidate), "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)


async def run(candidate: str, limit: int | None, refresh: bool) -> dict:
    fn = CANDIDATES[candidate]
    cache = {} if refresh else load_cache(candidate)
    todo = [it for it in active_items() if it["key"] not in cache]
    if limit:
        todo = todo[:limit]
    for n, it in enumerate(todo, 1):
        t0 = time.time()
        try:
            route, detail = await fn(it["text"])
        except Exception as e:  # a provider failure must be visible, not scored
            route, detail = "ERROR", f"{type(e).__name__}: {str(e)[:120]}"
        cache[it["key"]] = {
            "route": route, "detail": detail, "elapsed_s": round(time.time() - t0, 2),
        }
        print(f"[{candidate}] {n}/{len(todo)} {it['key']:26} -> {route:9} "
              f"({cache[it['key']]['elapsed_s']:5.1f}s) {detail[:60]}", flush=True)
        save_cache(candidate, cache)
    return cache


# ── Scoring ───────────────────────────────────────────────────────────────────

def score(candidate: str) -> dict | None:
    cache = load_cache(candidate)
    if not cache:
        return None
    rows = []
    for it in active_items():
        got = cache.get(it["key"])
        if not got:
            continue
        rows.append({**it, "got": got["route"], "detail": got["detail"],
                     "ok": got["route"] == it["expected"],
                     "elapsed_s": got.get("elapsed_s", 0.0)})
    if not rows:
        return None

    def acc(sel):
        s = [r for r in rows if sel(r)]
        return (sum(r["ok"] for r in s), len(s))

    out = {
        "candidate": candidate,
        "n": len(rows),
        "errors": sum(r["got"] == "ERROR" for r in rows),
        "overall": acc(lambda r: True),
        "gold": acc(lambda r: r["kind"] == "gold"),
        "para": acc(lambda r: r["kind"] == "paraphrase"),
        "rows": rows,
    }
    for lg in LANGS:
        out[f"para_{lg}"] = acc(lambda r, lg=lg: r["kind"] == "paraphrase" and r["language"] == lg)
        out[f"gold_{lg}"] = acc(lambda r, lg=lg: r["kind"] == "gold" and r["language"] == lg)
    # Everything that is NOT a deterministic-override hit is the miss path —
    # defined by exclusion so a new candidate's `detail` string can never
    # silently drop out of the latency figure (an earlier version listed the
    # prefixes to include and under-reported `local_compact` as 0.0s).
    miss = [r for r in rows if r["detail"] != "override"]
    out["miss_path_n"] = len(miss)
    out["miss_path_mean_s"] = round(sum(r["elapsed_s"] for r in miss) / len(miss), 2) if miss else 0.0
    return out


def _pct(t):
    hit, n = t
    return f"{hit}/{n}" + (f" ({100*hit/n:.0f}%)" if n else "")


def report(candidate: str) -> None:
    s = score(candidate)
    if not s:
        print(f"no cached results for {candidate}")
        return
    label = candidate if ITEM_SET == "corpus" else f"{candidate} [{ITEM_SET}]"
    print(f"\n=== {label} — {s['n']} items, {s['errors']} provider errors ===")
    print(f"  overall            {_pct(s['overall'])}")
    if ITEM_SET != "corpus":
        print(f"  miss-path items    {s['miss_path_n']}, mean {s['miss_path_mean_s']}s")
        for r in [r for r in s["rows"] if not r["ok"]]:
            print(f"    want {r['expected']:13} got {r['got']:13} {r['text'][:56]}")
        return
    print(f"  gold (32)          {_pct(s['gold'])}")
    print(f"  paraphrase (96)    {_pct(s['para'])}")
    for lg in LANGS:
        print(f"    paraphrase {lg:9} {_pct(s[f'para_{lg}'])}")
    print(f"  miss-path items    {s['miss_path_n']}, mean {s['miss_path_mean_s']}s")
    wrong = [r for r in s["rows"] if not r["ok"]]
    if wrong:
        print(f"  --- {len(wrong)} wrong ---")
        for r in sorted(wrong, key=lambda r: (r["gold_id"], r["kind"])):
            print(f"    {r['gold_id']:5}{r['kind'][:4]:5}{r['language']:9} "
                  f"want {r['expected']:9} got {r['got']:9}  {r['detail'][:44]}")


def table() -> None:
    if ITEM_SET != "corpus":
        hdr = ["candidate", "exemplars", "miss lat"]
        print("".join(h.ljust(w) for h, w in zip(hdr, [16, 14, 10])))
        print("-" * 40)
        for c in CANDIDATES:
            sc = score(c)
            if sc:
                print("".join(str(x).ljust(w) for x, w in zip(
                    [c, _pct(sc["overall"]), f"{sc['miss_path_mean_s']}s"], [16, 14, 10])))
        return
    cols = ["overall", "gold", "para", "para_en", "para_roman_ur", "para_ur"]
    hdr = ["candidate", "overall", "gold 32", "para 96", "para en", "para ru", "para ur", "miss lat"]
    widths = [14, 12, 11, 11, 10, 10, 10, 9]
    print("".join(h.ljust(w) for h, w in zip(hdr, widths)))
    print("-" * sum(widths))
    for c in CANDIDATES:
        s = score(c)
        if not s:
            continue
        cells = [c] + [_pct(s[k]) for k in cols] + [f"{s['miss_path_mean_s']}s"]
        print("".join(str(x).ljust(w) for x, w in zip(cells, widths)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", choices=sorted(CANDIDATES))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--table", action="store_true")
    ap.add_argument("--control", action="store_true",
                    help="score router.txt's own 74 few-shot examples instead of the gold corpus")
    a = ap.parse_args()

    global ITEM_SET
    if a.control:
        ITEM_SET = "control"

    if a.candidate:
        asyncio.run(run(a.candidate, a.limit, a.refresh))
        report(a.candidate)
    elif a.report:
        for c in CANDIDATES:
            report(c)
    if a.table or (not a.candidate and not a.report):
        table()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(os.environ.get("MUHAFIZ_ENV_FILE") or (ROOT.parent / "Evidence Intelligence Platform" / ".env"))
    except Exception:
        pass
    main()
