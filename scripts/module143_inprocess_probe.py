# -*- coding: utf-8 -*-
"""
Module 143 — in-process, per-attempt instrumented runner for the RAG retry
loop.

Runs the REAL `semantic_search()` sub-agent (real Chroma retrieval, real
reranker, real relevance evaluator, real retry rewriter, real generation)
and records, per attempt: wall time of retrieval / rerank / evaluation /
rewrite / generation, the reranker and RRF scores of the window handed to
the evaluator, the chunk ids in that window, the evaluator's verdict and
reason verbatim, and which model answered each LLM call (local vs cloud —
Module 101 §1.4's confound).

Nothing in `src/` is altered by this script; every wrapper calls the real
function and returns its result unchanged.

Usage:
    PYTHONPATH=. PYTHONIOENCODING=utf-8 python -X utf8 \
        scripts/module143_inprocess_probe.py --ids M143,KB3 --runs 2 \
        --arm before --out scratchpad/m143_before.json
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(HERE, ".env"), override=True)

import src.pipeline.harness.tools.rag as rag_mod  # noqa: E402
import src.pipeline.harness.agents.semantic_search as ss_mod  # noqa: E402
from src.pipeline.harness.types import (  # noqa: E402
    CallerContext,
    ExecutionContext,
    SubAgentInput,
)

GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")

EXTRA_QUESTIONS = {
    # The live query Module 143 was filed from (108.4 s to abstain).
    "M143": "How often is the officer who registers an FIR also the officer "
            "who investigates the case?",
    # §6 paraphrases — written before any live run.
    "M143-P1": "In what proportion of our cases does the same officer both register "
               "the FIR and carry out the investigation?",
    "M143-P2": "Hamare cases mein kitni dafa aisa hota hai ke FIR likhne wala afsar "
               "hi tafteesh bhi karta hai?",
    # Plain-path (non-legal-KB) controls. The corpus's case documents are
    # Urdu FIR narratives, zimni entries and crime-scene notes; the eight
    # gold KB questions all take the legal-KB path, so the plain path that
    # M143 runs on has no committed control of its own.
    # -- answerable from a case file (attempt 1 should be relevant)
    "P1": "What weapon was recovered from the accused in FIR 1001/26, and who was the complainant?",
    "P2": "Which sections of law were applied in FIR 1001/26?",
    "P3": "ایف آئی آر 1001/26 میں ملزم کے قبضے سے کون سا ہتھیار برآمد ہوا؟",
    # -- vaguer case questions (candidate near-misses on the plain path)
    "N1": "Tell me about the armed robbery reported from Iqbal Town where a pistol was seized.",
    "N2": "Kashif naam ke mulzim ke qabze se kya baramad hua tha?",
    # -- zero-signal: the corpus holds no document of this kind
    "Z2": "What is the average number of days between an FIR's registration and its first zimni entry?",
    "Z3": "Which police station has the highest conviction rate this year?",
    "Z4": "Kitni baar FIR darj karne wala afsar hi us case ki tafteesh bhi karta hai?",
}

_events: list[dict] = []
_t_start = 0.0


def _ev(kind: str, **kw) -> None:
    _events.append({"t": round(time.time() - _t_start, 2), "kind": kind, **kw})


def _scores(chunks: list[dict]) -> list[dict]:
    out = []
    for c in chunks:
        md = c.get("metadata") or {}
        out.append({
            "id": c.get("id"),
            "source": md.get("source") or md.get("source_file"),
            "rerank_score": c.get("rerank_score"),
            "rrf_score": c.get("rrf_score"),
        })
    return out


# --- wrappers ---------------------------------------------------------------
_real_retrieve = rag_mod._retrieve_candidates
_real_rerank = rag_mod.cross_rerank
_real_rerank_multi = rag_mod.cross_rerank_multi
_real_eval = rag_mod.evaluate_relevance
_real_rewrite = rag_mod.rewrite_for_retry
_real_gen = ss_mod.call_llm
_real_verify = ss_mod.verify_grounding


async def _w_retrieve(query, *a, **kw):
    t0 = time.time()
    sem, bm = await _real_retrieve(query, *a, **kw)
    _ev("retrieval", secs=round(time.time() - t0, 2), query=query[:300],
        n_semantic=len(sem), n_lexical=len(bm),
        top_semantic=[round(float(d.get("rrf_score", 0.0) or 0.0), 4) for d in sem[:10]],
        top_lexical=[round(float(d.get("rrf_score", d.get("score", 0.0)) or 0.0), 4) for d in bm[:10]])
    return sem, bm


async def _w_rerank(query, cands, top_k=None):
    t0 = time.time()
    out = await _real_rerank(query, cands, top_k=top_k)
    _ev("rerank", secs=round(time.time() - t0, 2), n_in=len(cands), window=_scores(out))
    return out


async def _w_rerank_multi(queries, cands, top_k=None):
    t0 = time.time()
    out = await _real_rerank_multi(queries, cands, top_k=top_k)
    _ev("rerank_multi", secs=round(time.time() - t0, 2), n_in=len(cands),
        n_queries=len(queries), window=_scores(out))
    return out


async def _w_eval(original, rewritten, chunks):
    t0 = time.time()
    res = await _real_eval(original, rewritten, chunks)
    _ev("evaluate", secs=round(time.time() - t0, 2), n_chunks=len(chunks),
        window_ids=[c.get("id") for c in chunks],
        relevant=res.get("relevant"), reason=res.get("reason"))
    return res


async def _w_rewrite(**kw):
    t0 = time.time()
    out = await _real_rewrite(**kw)
    _ev("rewrite", secs=round(time.time() - t0, 2),
        feedback=(kw.get("evaluator_feedback") or "")[:300], new_query=out[:300])
    return out


async def _w_gen(*a, **kw):
    t0 = time.time()
    out = await _real_gen(*a, **kw)
    _ev("generation", secs=round(time.time() - t0, 2), chars=len(out or ""))
    return out


async def _w_verify(**kw):
    t0 = time.time()
    out = await _real_verify(**kw)
    _ev("verify", secs=round(time.time() - t0, 2), grounded=out.get("grounded"),
        off_topic=out.get("off_topic"))
    return out


rag_mod._retrieve_candidates = _w_retrieve


def _timed(name, real):
    async def _w(*a, **kw):
        t0 = time.time()
        out = await real(*a, **kw)
        _ev(name, secs=round(time.time() - t0, 2))
        return out
    return _w


# Sub-steps inside `_retrieve_candidates` — which of them the retry pays for.
rag_mod.expand_query = _timed("expand_query", rag_mod.expand_query)
rag_mod.generate_cross_script_variant = _timed("cross_script", rag_mod.generate_cross_script_variant)
rag_mod.embed_text = _timed("embed", rag_mod.embed_text)
rag_mod.bm25_candidate_pool = _timed("bm25_pool", rag_mod.bm25_candidate_pool)
rag_mod.query_similar = _timed("query_similar", rag_mod.query_similar)
rag_mod.generate_statute_queries = _timed("statute_queries", rag_mod.generate_statute_queries)
rag_mod.render_question_in_english = _timed("english_query", rag_mod.render_question_in_english)
rag_mod.cross_rerank = _w_rerank


async def _w_gate(question, reason):
    t0 = time.time()
    out = await rag_mod.__dict__["_real_gate"](question, reason)
    _ev("gate", secs=round(time.time() - t0, 2), verdict=out, reason=reason[:300])
    return out


rag_mod._real_gate = rag_mod.retry_could_help
rag_mod.retry_could_help = _w_gate
rag_mod.cross_rerank_multi = _w_rerank_multi
rag_mod.evaluate_relevance = _w_eval
rag_mod.rewrite_for_retry = _w_rewrite
ss_mod.call_llm = _w_gen
ss_mod.verify_grounding = _w_verify


class _FallbackCatcher(logging.Handler):
    """Counts `call_llm()`'s silent local->cloud fallbacks (Module 101 §1.4)."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record):
        msg = record.getMessage()
        if "Falling back to" in msg:
            self.lines.append(msg[:200])


_catcher = _FallbackCatcher()
logging.getLogger("src.llm.client").addHandler(_catcher)
logging.getLogger("src.llm.client").setLevel(logging.WARNING)


def _probes(ids):
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    out = []
    for i in ids:
        if i in gold:
            out.append((i, gold[i]["question"]))
        elif i in EXTRA_QUESTIONS:
            out.append((i, EXTRA_QUESTIONS[i]))
        else:
            raise SystemExit(f"unknown probe id {i!r}")
    return out


async def _one(qid, question, run, arm):
    global _t_start
    _events.clear()
    _catcher.lines.clear()
    agent_input = SubAgentInput(
        query_text=question,
        execution=ExecutionContext(
            caller=CallerContext(user_id="m143", role="platform-admin", active_case_id=None)
        ),
    )
    _t_start = time.time()
    err = None
    try:
        res = await ss_mod.semantic_search(agent_input)
        status = res.status.value if hasattr(res.status, "value") else str(res.status)
        served = res.answer_text
        caveats = list(res.caveats or [])
    except Exception as exc:  # noqa: BLE001
        status, served, caveats = "EXCEPTION", None, []
        err = repr(exc)
    elapsed = round(time.time() - _t_start, 1)
    verdicts = [(e["relevant"], e["reason"]) for e in _events if e["kind"] == "evaluate"]
    return {
        "id": qid, "run": run, "arm": arm, "question": question,
        "status": status, "served_answer": served, "caveats": caveats, "error": err,
        "elapsed_s": elapsed, "evaluator_rounds": len(verdicts),
        "verdicts": [{"relevant": r, "reason": s} for r, s in verdicts],
        "cloud_fallbacks": list(_catcher.lines),
        "generation": "cloud" if _catcher.lines else "local",
        "events": list(_events),
    }


def _print_events(row):
    for e in row["events"]:
        extra = ""
        if e["kind"] == "evaluate":
            extra = f" relevant={e['relevant']} :: {str(e['reason'])[:160]}"
        elif e["kind"] in ("rerank", "rerank_multi"):
            extra = " scores=" + str([round(float(w["rerank_score"] or 0), 4) for w in e["window"]])
        elif e["kind"] == "retrieval":
            extra = f" sem={e['n_semantic']} lex={e['n_lexical']} q={e['query'][:70]!r}"
        elif e["kind"] == "rewrite":
            extra = f" -> {e['new_query'][:90]!r}"
        if e["kind"] in ("embed", "query_similar"):
            continue
        print(f"    t={e['t']:6.1f} {e['kind']:14} {e['secs']:5.1f}s{extra}", flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="M143")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--arm", default="before")
    ap.add_argument("--out", default=os.path.join("scratchpad", "m143_probe.json"))
    args = ap.parse_args()
    rows = []
    if os.path.exists(args.out):
        rows = json.load(open(args.out, encoding="utf-8"))
    done = {(r["id"], r["run"], r["arm"]) for r in rows}
    for qid, q in _probes([i for i in args.ids.split(",") if i]):
        for run in range(1, args.runs + 1):
            if (qid, run, args.arm) in done:
                continue
            row = await _one(qid, q, run, args.arm)
            rows.append(row)
            v = "".join("T" if x["relevant"] else "F" for x in row["verdicts"])
            print(f"{qid} run{run} arm={args.arm} status={row['status']} verdicts={v} "
                  f"gen={row['generation']} {row['elapsed_s']}s", flush=True)
            gate = [e for e in row["events"] if e["kind"] == "gate"]
            if gate:
                print(f"    gate: retry_could_help={gate[0]['verdict']} ({gate[0]['secs']}s)", flush=True)
            _print_events(row)
            os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
            json.dump(rows, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"wrote {args.out} ({len(rows)} rows)")


if __name__ == "__main__":
    asyncio.run(main())
