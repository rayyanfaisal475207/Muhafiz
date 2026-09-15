# -*- coding: utf-8 -*-
"""
Module 151 — replay the REAL verifier on captured live inputs, flag off vs on.

`evaluation/module151_inprocess_run.py` records, for every run that reached
the grounding gate, the exact (answer, cited_chunks, case_id) the Verifier
was given. This script feeds each such input back into the unmodified
`verify_grounding()` N times with `SCHEMA_ABSENCE_GROUNDING_ENABLED` off and
N times with it on, and records the judge's raw verdict, the final verdict,
and the Module 151 classification per run.

Why this exists alongside the end-to-end arms: the relevance evaluator is
sampled and, on the days this module ran, rejected KB5/KB9's windows often
enough that the verifier was reached on a minority of runs (§4 of the
result file). Replaying the captured inputs measures the ONE thing this
module changed — what the verifier does with a correct answer that states
an absence — without the evaluator's coin flips in front of it.

    PYTHONPATH=. python -X utf8 scripts/module151_verifier_replay.py \
        --inputs docs/gold-qa-wave2-results/module151_live/module151_before.json \
                 docs/gold-qa-wave2-results/module151_live/module151_after.json \
        --runs 3 --out docs/gold-qa-wave2-results/module151_verifier_replay.json
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import io
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if "Module 151" in msg or "Schema-absence classifier" in msg or "Falling back to" in msg:
            self.lines.append(msg[:900])


async def main_async(a) -> None:
    from src import config
    import src.pipeline.verifier as vmod

    lines = _Lines()
    logging.getLogger().addHandler(lines)
    logging.getLogger().setLevel(logging.INFO)

    judge_raw: list[dict] = []
    real_json = vmod.call_llm_json

    async def wrapped_json(*args, **kw):
        res, raw = await real_json(*args, **kw)
        sp = (args[0] if args else kw.get("system_prompt")) or ""
        if sp[:40] == vmod._SYSTEM_PROMPT[:40]:
            # deep-copied: verify_grounding() mutates the dict the judge returned
            # in place, and the RAW verdict is exactly what must survive that.
            judge_raw.append(copy.deepcopy(res) if isinstance(res, dict) else {"_unparsed": (raw or "")[:300]})
        return res, raw

    vmod.call_llm_json = wrapped_json

    inputs = []
    for p in a.inputs:
        for row in json.load(io.open(p, encoding="utf-8")):
            for i, call in enumerate(row.get("verifier_calls") or []):
                if call.get("cited_chunks"):
                    inputs.append({
                        "source_file": Path(p).name, "id": row["id"], "run": row["run"],
                        "arm": row["arm"], "call_index": i, "answer": call["answer"],
                        "cited_chunks": call["cited_chunks"], "case_id": call.get("case_id"),
                        "live_result": call["result"],
                    })
    print(f"{len(inputs)} captured verifier input(s)")

    out = json.load(io.open(a.out, encoding="utf-8")) if Path(a.out).exists() else []
    done = {(r["source_file"], r["id"], r["run"], r["call_index"], r["flag"], r["replay"]) for r in out}

    for inp in inputs:
        for flag in (False, True):
            config.SCHEMA_ABSENCE_GROUNDING_ENABLED = flag
            for replay in range(1, a.runs + 1):
                key = (inp["source_file"], inp["id"], inp["run"], inp["call_index"], flag, replay)
                if key in done:
                    continue
                judge_raw.clear(); lines.lines.clear()
                t0 = time.monotonic()
                res = await vmod.verify_grounding(
                    answer=inp["answer"], cited_chunks=inp["cited_chunks"], case_id=inp["case_id"],
                )
                row = {
                    "source_file": inp["source_file"], "id": inp["id"], "run": inp["run"],
                    "call_index": inp["call_index"], "flag": flag, "replay": replay,
                    "secs": round(time.monotonic() - t0, 1),
                    "judge_raw": list(judge_raw),
                    "final": {k: res.get(k) for k in ("grounded", "off_topic", "unsupported_claims", "reason",
                                                       "schema_absence_grounded", "schema_absence_claims",
                                                       "exhaustive_negative_override", "citation_format_degraded")},
                    "module151_lines": [l for l in lines.lines if "Falling back" not in l],
                    "generation": "cloud" if any("Falling back" in l for l in lines.lines) else "local",
                }
                out.append(row)
                jr = judge_raw[0] if judge_raw else {}
                print(f"{inp['id']} live-run{inp['run']} flag={'on ' if flag else 'off'} replay{replay}: "
                      f"judge={jr.get('grounded')} final={res.get('grounded')} "
                      f"m151={res.get('schema_absence_grounded')} gen={row['generation']} {row['secs']}s "
                      f":: {str(res.get('reason'))[:110]}", flush=True)
                for l in row["module151_lines"]:
                    print(f"      {l[:260]}", flush=True)
                Path(a.out).parent.mkdir(parents=True, exist_ok=True)
                with io.open(a.out, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, indent=1)
    try:
        from src.graph import age_client
        await age_client.close_pool()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    asyncio.run(main_async(a))
