# -*- coding: utf-8 -*-
"""
Module 70 — in-process Large-Scale Aggregate runner with three-way capture.

Runs the REAL `large_scale_aggregate()` sub-agent (real `xagg_tool()`, real
Postgres aggregate, real deterministic rendering, real generation, real
`verify_structured_aggregate_paraphrase()`) directly in this process, and
records, for every run, the three layers Module 83's capture named:

  1. the aggregate payload kind + the rendered `raw_summary_text`
  2. the LLM paraphrase, before the gate sees it
  3. the served answer and the verifier verdict

plus WHICH MODEL actually answered — `call_llm()` is local-first and falls
back to Groq/Gemini on ANY local failure, and Module 101 found that an
unrecorded silent fallback made its first eight runs worthless.

Why in-process rather than through `/api/chat`: the backend budget is two
machine-wide and other tracks hold them; and neither the SSE stream nor
`backend.log` carries `raw_summary_text` alongside the pre-gate paraphrase,
which is exactly what Phase 1 asks to see. Nothing in `src/` is modified;
the wrappers below only record and return the real values unaltered.

Usage:
    PYTHONPATH=. python scripts/module70_inprocess_runs.py \
        --ids M2 --runs 3 --arm before --out scratchpad/m70_before.json
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(HERE, ".env"), override=True)

import src.pipeline.harness.agents.large_scale_aggregate as lsa_mod  # noqa: E402
from src.pipeline.harness.types import (  # noqa: E402
    CallerContext,
    ExecutionContext,
    SubAgentInput,
)

GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")

_gen_log: list[dict] = []
_paraphrases: list[str] = []
_verifier_calls: list[dict] = []

_real_call_llm = lsa_mod.call_llm
_real_verify = lsa_mod.verify_structured_aggregate_paraphrase
_real_xagg = lsa_mod.xagg_tool
_last_tool_result: dict = {}


async def _recording_call_llm(*a, **kw):
    """Record WHICH model answered — see module docstring."""
    import src.llm.client as _c

    sys_p = kw.get("system_prompt") if "system_prompt" in kw else (a[0] if a else "")
    usr_p = kw.get("user_message") if "user_message" in kw else (a[1] if len(a) > 1 else "")
    try:
        out = await _c._call_local(
            sys_p,
            usr_p,
            kw.get("temperature", 0.3),
            kw.get("max_tokens", 1000),
            kw.get("role", "reasoning"),
        )
        _gen_log.append({"model": "local", "detail": os.environ.get("LOCAL_LLM_MODEL")})
        return out
    except Exception as exc:  # noqa: BLE001
        out = await _real_call_llm(*a, **kw)
        _gen_log.append({"model": "cloud_fallback", "detail": f"{type(exc).__name__}: {exc}"})
        return out


async def _recording_paraphrase_call(*a, **kw):
    out = await _recording_call_llm(*a, **kw)
    _paraphrases.append(out or "")
    return out


async def _recording_xagg(tool_input):
    res = await _real_xagg(tool_input)
    _last_tool_result.clear()
    _last_tool_result.update(
        {
            "status": getattr(res.status, "value", str(res.status)),
            "aggregate_kind": getattr(res, "aggregate_kind", None),
            "raw_summary_text": getattr(res, "raw_summary_text", None),
            "case_ids_touched": list(getattr(res, "case_ids_touched", None) or []),
        }
    )
    return res


async def _recording_verify(**kw):
    result = await _real_verify(**kw)
    _verifier_calls.append(
        {
            "answer": kw.get("answer"),
            "source_text": kw.get("source_text"),
            "verdict": {
                k: result.get(k)
                for k in (
                    "grounded",
                    "off_topic",
                    "leaked_case_id",
                    "unsupported_claims",
                    "omitted_source_figures",
                    "reason",
                )
            },
        }
    )
    return result


lsa_mod.call_llm = _recording_paraphrase_call
lsa_mod.verify_structured_aggregate_paraphrase = _recording_verify
lsa_mod.xagg_tool = _recording_xagg

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _nums(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in _NUM_RE.finditer(text or "")}


def _headline(raw: str) -> str:
    for line in (raw or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


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
    _gen_log.clear()
    _paraphrases.clear()
    _verifier_calls.clear()
    _last_tool_result.clear()

    agent_input = SubAgentInput(
        query_text=question,
        execution=ExecutionContext(
            caller=CallerContext(user_id="m70", role="platform-admin", active_case_id=None)
        ),
    )
    t0 = time.time()
    err = None
    try:
        res = await lsa_mod.large_scale_aggregate(agent_input)
        status = getattr(res.status, "value", str(res.status))
        served = res.answer_text
        caveats = list(res.caveats or [])
    except Exception as exc:  # noqa: BLE001
        status, served, caveats, err = "EXCEPTION", None, [], repr(exc)

    raw = _last_tool_result.get("raw_summary_text") or ""
    head = _headline(raw)
    served_nums = _nums(served or "")
    return {
        "id": qid,
        "run": run,
        "arm": arm,
        "question": question,
        "status": status,
        "error": err,
        "elapsed_s": round(time.time() - t0, 1),
        "models": list(_gen_log),
        "aggregate_kind": _last_tool_result.get("aggregate_kind"),
        "raw_summary_text": raw,
        "headline_line": head,
        "headline_figures": sorted(_nums(head)),
        "headline_figures_missing_from_served": sorted(_nums(head) - served_nums),
        "paraphrases": list(_paraphrases),
        "verifier_calls": list(_verifier_calls),
        "served_answer": served,
        "served_is_raw": (served or "").strip() == raw.strip(),
        "caveats": caveats,
        "served_chars": len(served or ""),
        "served_list_lines": sum(
            1 for ln in (served or "").splitlines() if re.match(r"\s*(?:[-*•]|\d+[.)])\s", ln)
        ),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="M2")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--arm", default="before")
    ap.add_argument("--out", default=os.path.join("scratchpad", "m70_runs.json"))
    ap.add_argument("--extra", default="")
    args = ap.parse_args()
    extra = {
        k: v for k, v in (json.load(open(args.extra, encoding="utf-8")) if args.extra else {}).items()
        if not k.startswith("_")
    }

    rows: list[dict] = []
    for qid, question in _probes([i for i in args.ids.split(",") if i], extra):
        for run in range(1, args.runs + 1):
            row = await _one(qid, question, run, args.arm)
            rows.append(row)
            v = (row["verifier_calls"] or [{}])
            last = v[-1].get("verdict", {}) if v else {}
            print(
                f"{qid} run{run} arm={args.arm} status={row['status']} "
                f"kind={row['aggregate_kind']} model={[m['model'] for m in row['models']]} "
                f"grounded={last.get('grounded')} raw_served={row['served_is_raw']} "
                f"missing_headline={row['headline_figures_missing_from_served']} "
                f"{row['elapsed_s']}s",
                flush=True,
            )
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False, indent=1)
    print(f"wrote {len(rows)} rows -> {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
