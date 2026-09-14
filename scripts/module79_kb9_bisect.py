"""
Module 79 — KB9 bisect: does the SECOND aggregate in KB9's data half change
what the relevance evaluator / verifier decide?

Runs the real `semantic_search()` sub-agent in-process (real retrieval,
reranker, evaluator, generation, verifier) on KB9's gold text, in two arms:
  - `then`   : the plan as shipped (fir_section_case_count + seized_property_disposition)
  - `single` : the same plan with `then=None` (the pre-module single aggregate)
and records per run the evaluator's full verdict per attempt, the verifier's
verdict, the served answer, which model answered each generation, and the
elapsed time. Same capture pattern as `scripts/module101_inprocess_runs.py`.

Usage:
    PYTHONPATH=. python -X utf8 scripts/module79_kb9_bisect.py --runs 2 --out scratchpad/kb9_bisect.json
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(HERE, ".env"), override=True)

import src.pipeline.harness.agents.semantic_search as ss_mod  # noqa: E402
import src.pipeline.harness.tools.rag as rag_mod  # noqa: E402
from src.pipeline.harness.types import CallerContext, ExecutionContext, SubAgentInput  # noqa: E402

GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")

_evals: list[dict] = []
_verdicts: list[dict] = []
_gen_log: list[str] = []
_real_eval = rag_mod.evaluate_relevance
_real_verify = ss_mod.verify_grounding
_real_call_llm = ss_mod.call_llm


async def _recording_eval(*a, **kw):
    out = await _real_eval(*a, **kw)
    try:
        _evals.append({"relevant": out.get("relevant") if isinstance(out, dict) else getattr(out, "relevant", None),
                       "reason": (out.get("reason") if isinstance(out, dict) else getattr(out, "reason", None))})
    except Exception:  # noqa: BLE001
        _evals.append({"raw": str(out)[:400]})
    return out


async def _recording_call_llm(*a, **kw):
    import src.llm.client as _c

    try:
        out = await _c._call_local(
            kw.get("system_prompt") or a[0], kw.get("user_message") or a[1],
            kw.get("temperature", 0.3), kw.get("max_tokens", 1000), kw.get("role", "reasoning"),
        )
        _gen_log.append("local")
        return out
    except Exception as exc:  # noqa: BLE001
        _gen_log.append(f"local_failed:{type(exc).__name__}")
        return await _real_call_llm(*a, **kw)


async def _recording_verify(*, answer, cited_chunks, case_id, **kw):
    result = await _real_verify(answer=answer, cited_chunks=cited_chunks, case_id=case_id, **kw)
    _verdicts.append({
        "chunks": [{"id": c.get("id"), "chars": len(c.get("text") or "")} for c in cited_chunks],
        "verdict": {k: result.get(k) for k in ("grounded", "off_topic", "unsupported_claims", "reason")},
    })
    return result


rag_mod.evaluate_relevance = _recording_eval
ss_mod.call_llm = _recording_call_llm
ss_mod.verify_grounding = _recording_verify


def _set_arm(arm: str) -> None:
    plans = []
    for p in rag_mod._KB_DATA_HALF_PLANS:
        if p.name == "death_investigation_charging" and arm == "single":
            p = dataclasses.replace(p, then=None)
        plans.append(p)
    rag_mod._KB_DATA_HALF_PLANS = tuple(plans)


async def _one(question: str, arm: str, run: int) -> dict:
    _evals.clear(); _verdicts.clear(); _gen_log.clear()
    _set_arm(arm)
    agent_input = SubAgentInput(
        query_text=question,
        execution=ExecutionContext(caller=CallerContext(
            user_id="00000000-0000-4000-8000-000000000079", role="platform-admin", active_case_id=None)),
    )
    t0 = time.perf_counter()
    try:
        res = await ss_mod.semantic_search(agent_input)
        status = res.status.value if hasattr(res.status, "value") else str(res.status)
        served, caveats = res.answer_text, list(res.caveats or [])
        err = None
    except Exception as exc:  # noqa: BLE001
        status, served, caveats, err = "EXCEPTION", None, [], repr(exc)
    return {"arm": arm, "run": run, "status": status, "elapsed_s": round(time.perf_counter() - t0, 1),
            "evaluator": list(_evals), "verifier": list(_verdicts), "generation": list(_gen_log),
            "served_answer": served, "caveats": caveats, "error": err}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--arms", default="then,single")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    question = next(q["question"] for q in json.load(open(GOLD, encoding="utf-8")) if q["id"] == "KB9")
    rows = []
    for run in range(1, args.runs + 1):
        for arm in args.arms.split(","):
            row = await _one(question, arm, run)
            rows.append(row)
            print(f"KB9 arm={arm} run{run} status={row['status']} {row['elapsed_s']}s "
                  f"eval={[e.get('relevant') for e in row['evaluator']]} "
                  f"verifier={[v['verdict'].get('grounded') for v in row['verifier']]} gen={row['generation']}",
                  flush=True)
            json.dump(rows, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
