# -*- coding: utf-8 -*-
"""
Module 151 — run KB questions end-to-end IN PROCESS and capture the
Verifier's FULL verdict (every `unsupported_claims` entry, not the 80-char
log prefix), the schema-absence classifier's output, and which model served
each LLM call.

Built on `evaluation/module92_inprocess_run.py::run_one()` (the same
router -> cutover -> Supervisor path `main.py::chat_endpoint()` uses); this
file adds capture only, no source edits:

  * `src.pipeline.harness.agents.semantic_search.verify_grounding` is wrapped
    so the dict the Verifier returned is recorded verbatim per call;
  * `src.pipeline.verifier.call_llm_json` is wrapped so the judge's RAW
    verdict (before any Module 61/101/151 post-pass) is recorded too — this
    is what distinguishes "the judge rejected and Module 151 overturned"
    from "the judge accepted";
  * a logging handler captures `Falling back to` (generation = cloud),
    `Verifier [Module 151]` and `Schema-absence classifier` lines.

    PYTHONPATH=. python -X utf8 evaluation/module151_inprocess_run.py \
        --questions KB2,KB5,KB9 --runs 3 --arm before
    SCHEMA_ABSENCE_GROUNDING_ENABLED=false ... --arm before   (flag off)

`--arm` is a label; the code that runs is whatever this worktree holds.
Rows are appended to docs/gold-qa-wave2-results/module151_live/<arm>.json
and a (id, run) already present is skipped, so a run can be resumed.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import io
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module151_live"
GOLD_PATH = ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
PARA_PATH = ROOT / "docs" / "gold-qa-wave2-results" / "module151_paraphrases.json"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if (
            "Falling back to" in msg
            or "Verifier [Module 151]" in msg
            or "Schema-absence classifier" in msg
            or "Verifier: grounded=" in msg
            or "verifier rejected" in msg
            or "Schema inventory" in msg
            or "Evaluator: relevant=" in msg
            or "chunk(s) to evaluator" in msg
            or "Retry gate:" in msg
        ):
            self.lines.append(msg[:1200])


def _questions(qids: list[str]) -> list[dict]:
    gold = {g["id"]: g for g in json.load(io.open(GOLD_PATH, encoding="utf-8"))}
    paras = {}
    if PARA_PATH.exists():
        paras = {p["id"]: p for p in json.load(io.open(PARA_PATH, encoding="utf-8"))}
    out = []
    for q in qids:
        if q in gold:
            out.append({"id": q, "language": gold[q]["language"], "text": gold[q]["question"],
                        "expected": gold[q].get("answer")})
        elif q in paras:
            out.append({"id": q, "language": paras[q]["language"], "text": paras[q]["text"],
                        "expected": None, "gold_id": paras[q]["gold_id"]})
        else:
            raise SystemExit(f"unknown question id {q}")
    return out


async def main_async(a) -> None:
    import src.pipeline.verifier as vmod
    import src.pipeline.harness.agents.semantic_search as ss
    from evaluation.module92_inprocess_run import run_one

    verifier_calls: list[dict] = []
    judge_raw: list[dict] = []

    real_verify = ss.verify_grounding
    real_json = vmod.call_llm_json

    async def wrapped_verify(**kw):
        res = await real_verify(**kw)
        verifier_calls.append({
            "answer": kw.get("answer"),
            "chunk_ids": [c.get("id") for c in kw.get("cited_chunks") or []],
            # Full chunks, so the verifier can be REPLAYED offline on this
            # exact input (scripts/module151_verifier_replay.py) — the way
            # to measure the verifier change without the evaluator's noise.
            "cited_chunks": kw.get("cited_chunks"),
            "case_id": kw.get("case_id"),
            "result": res,
        })
        return res

    async def wrapped_json(*args, **kw):
        res, raw = await real_json(*args, **kw)
        sp = (args[0] if args else kw.get("system_prompt")) or ""
        if sp[:40] == vmod._SYSTEM_PROMPT[:40]:
            # deep-copied: verify_grounding() mutates the dict the judge returned
            # in place, and the RAW verdict is exactly what must survive that.
            judge_raw.append(copy.deepcopy(res) if isinstance(res, dict) else {"_unparsed": (raw or "")[:300]})
        return res, raw

    ss.verify_grounding = wrapped_verify
    vmod.call_llm_json = wrapped_json

    cap = _Capture()
    logging.getLogger().addHandler(cap)
    logging.getLogger().setLevel(logging.INFO)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"module151_{a.arm}.json"
    rows = json.load(io.open(path, encoding="utf-8")) if path.exists() else []
    done = {(r["id"], r["run"]) for r in rows}

    items = _questions([q.strip() for q in a.questions.split(",") if q.strip()])
    for run in range(1, a.runs + 1):
        for it in items:
            if (it["id"], run) in done:
                continue
            verifier_calls.clear(); judge_raw.clear(); cap.lines.clear()
            print(f"[{a.arm} run{run}] {it['id']} ... (SCHEMA_ABSENCE_GROUNDING_ENABLED="
                  f"{__import__('src.config', fromlist=['x']).SCHEMA_ABSENCE_GROUNDING_ENABLED})", flush=True)
            t0 = time.monotonic()
            try:
                r = await run_one(it["text"])
            except Exception as exc:  # noqa: BLE001
                r = {"route": None, "answer": "", "steps": [], "status": "error",
                     "elapsed_s": round(time.monotonic() - t0, 1),
                     "route_error": f"{type(exc).__name__}: {exc}"}
            fallbacks = [l for l in cap.lines if "Falling back to" in l]
            row = {
                "id": it["id"], "run": run, "arm": a.arm, "language": it["language"],
                "question": it["text"], "expected_answer": it.get("expected"),
                "route": r.get("route"), "status": r.get("status"),
                "elapsed_s": r.get("elapsed_s"), "answer": r.get("answer"),
                "generation": "cloud" if fallbacks else "local",
                "local_llm_fallbacks": len(fallbacks),
                "fallback_lines": fallbacks[:10],
                "verifier_calls": list(verifier_calls),
                "judge_raw": list(judge_raw),
                "module151_lines": [l for l in cap.lines if "Module 151" in l or "Schema-absence" in l or "Schema inventory" in l],
                "verifier_log_lines": [l for l in cap.lines if "Verifier: grounded=" in l or "verifier rejected" in l],
                "evaluator_lines": [l[:700] for l in cap.lines if "Evaluator: relevant=" in l or "chunk(s) to evaluator" in l or "Retry gate:" in l],
                "steps": r.get("steps"),
            }
            rows.append(row)
            with io.open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=1)
            vc = "".join("G" if c["result"].get("grounded") else "X" for c in verifier_calls)
            print(f"    route={row['route']} status={row['status']} verifier={vc} "
                  f"gen={row['generation']} m151={len(row['module151_lines'])} "
                  f"{row['elapsed_s']}s answer={len(row['answer'] or '')} chars", flush=True)
    print(f"wrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--arm", required=True)
    a = ap.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass
    asyncio.run(main_async(a))


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
