# -*- coding: utf-8 -*-
"""
Module 101 — in-process Semantic Search runner with full verifier I/O capture.

Runs the REAL `semantic_search()` sub-agent (real `rag_tool()`, real Chroma
retrieval, real reranker, real relevance evaluator, real generation, real
`verify_grounding()`) directly in this process, and records for every run the
complete chunk corpus the verifier was handed, the answer, and the verdict.

Why in-process rather than through `/api/chat`: the backend budget is two
machine-wide and both were held by other tracks for the whole of Phase 1, and
neither the SSE stream nor `backend.log` carries the verifier's chunk corpus
or its itemised `unsupported_claims` — which is exactly what the brief's
Phase 1 asks to see. Nothing in `src/` is modified; the wrapper below only
records and returns the real verdict unaltered.

Usage:
    PYTHONPATH=. python scripts/module101_inprocess_runs.py \
        --ids KB9 --runs 3 --arm before --out scratchpad/m101_before.json
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(HERE, ".env"), override=True)

import src.pipeline.harness.agents.semantic_search as ss_mod  # noqa: E402
from src.pipeline.harness.types import (  # noqa: E402
    CallerContext,
    ExecutionContext,
    SubAgentInput,
)
from src.pipeline.verifier import EXHAUSTIVE_SCOPE_META_KEY  # noqa: E402

GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")

_captured: list[dict] = []
_gen_log: list[str] = []
_real_verify = ss_mod.verify_grounding
_real_call_llm = ss_mod.call_llm


async def _recording_call_llm(*a, **kw):
    """Record WHICH model actually answered.

    A silent cloud fallback is the confound that made this module's first
    three in-process runs disagree with Module 78's live ones: `call_llm()`
    is local-first and falls back to Groq/Gemini on ANY local failure — an
    empty-message `httpx.ReadTimeout` under a contended model server looks
    identical, in the answer, to a healthy run. The verifier then judges a
    cloud-written answer instead of the Qwen3-14B one live serves. Recorded
    per run so no count in the result file can rest on a mixed arm.
    """
    import src.llm.client as _c

    local_ok = True
    try:
        out = await _c._call_local(
            kw.get("system_prompt") or a[0],
            kw.get("user_message") or a[1],
            kw.get("temperature", 0.3),
            kw.get("max_tokens", 1000),
            kw.get("role", "reasoning"),
        )
    except Exception as exc:  # noqa: BLE001
        local_ok = False
        _gen_log.append(f"local_failed:{type(exc).__name__}:{exc}")
        out = await _real_call_llm(*a, **kw)
    if local_ok:
        _gen_log.append("local")
    return out


ss_mod.call_llm = _recording_call_llm


async def _recording_verify(*, answer, cited_chunks, case_id, **kw):
    result = await _real_verify(
        answer=answer, cited_chunks=cited_chunks, case_id=case_id, **kw
    )
    _captured.append(
        {
            "answer": answer,
            "chunks": [
                {
                    "n": i,
                    "id": c.get("id"),
                    "source_tool": (c.get("metadata") or {}).get("source_tool"),
                    "source": (c.get("metadata") or {}).get("source")
                    or (c.get("metadata") or {}).get("source_file"),
                    "exhaustive_scope": bool(
                        (c.get("metadata") or {}).get(EXHAUSTIVE_SCOPE_META_KEY)
                    ),
                    "chars": len(c.get("text") or ""),
                    "text": c.get("text") or "",
                }
                for i, c in enumerate(cited_chunks, start=1)
            ],
            "verdict": {
                k: result.get(k)
                for k in (
                    "grounded",
                    "off_topic",
                    "leaked_case_id",
                    "unsupported_claims",
                    "reason",
                    "exhaustive_negative_override",
                    "citation_format_degraded",
                    "named_source",
                )
            },
        }
    )
    return result


ss_mod.verify_grounding = _recording_verify


def _probes(ids: list[str], extra: dict) -> list[tuple[str, str]]:
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    out: list[tuple[str, str]] = []
    for i in ids:
        if i in gold:
            out.append((i, gold[i]["question"]))
        elif i in extra:
            out.append((i, extra[i]))
        else:
            raise SystemExit(f"unknown probe id {i!r}")
    return out


async def _one(qid: str, question: str, run: int, arm: str) -> dict:
    _captured.clear()
    _gen_log.clear()
    agent_input = SubAgentInput(
        query_text=question,
        execution=ExecutionContext(
            caller=CallerContext(user_id="m101", role="platform-admin", active_case_id=None)
        ),
    )
    t0 = time.time()
    err = None
    try:
        res = await ss_mod.semantic_search(agent_input)
        status = res.status.value if hasattr(res.status, "value") else str(res.status)
        served = res.answer_text
        caveats = list(res.caveats or [])
    except Exception as exc:  # noqa: BLE001
        status, served, caveats = "EXCEPTION", None, []
        err = repr(exc)
    return {
        "id": qid,
        "run": run,
        "arm": arm,
        "question": question,
        "status": status,
        "served_answer": served,
        "caveats": caveats,
        "error": err,
        "elapsed_s": round(time.time() - t0, 1),
        "generation": list(_gen_log),
        "verifier_calls": list(_captured),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="KB9")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--arm", default="before")
    ap.add_argument("--out", default=os.path.join("scratchpad", "m101_runs.json"))
    ap.add_argument("--extra", default="")
    args = ap.parse_args()

    extra = json.load(open(args.extra, encoding="utf-8")) if args.extra else {}
    probes = _probes([i for i in args.ids.split(",") if i], extra)

    rows: list[dict] = []
    for qid, question in probes:
        for run in range(1, args.runs + 1):
            row = await _one(qid, question, run, args.arm)
            rows.append(row)
            v = (row["verifier_calls"] or [{}])[-1].get("verdict", {})
            print(
                f"{qid} run{run} arm={args.arm} status={row['status']} "
                f"grounded={v.get('grounded')} off_topic={v.get('off_topic')} "
                f"{row['elapsed_s']}s :: {str(v.get('reason'))[:140]}",
                flush=True,
            )
            os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
            json.dump(rows, open(args.out, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
    print(f"\nwrote {args.out} ({len(rows)} rows)")


if __name__ == "__main__":
    asyncio.run(main())
