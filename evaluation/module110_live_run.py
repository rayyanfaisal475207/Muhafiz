# -*- coding: utf-8 -*-
"""
Module 110 — G6 (and G1) live, in process, with the two things this module
has to report per run that no existing runner records:

  1. **WHICH MODEL ANSWERED.** `call_llm()` falls back from the local
     Qwen3-14B to Groq on any local failure and says so only in a log line
     (`src/llm/client.py`: "Local LLM failed: ... Falling back to groq").
     Module 101's first eight runs were invalidated by exactly this, so the
     handler below captures that warning and every run is tagged
     `local` / `groq-fallback` rather than assumed.
  2. **THE SYNTHESIS COLLAPSE RATE.** Module 83's `_is_degenerate()` guard
     logs the collapse, the regeneration, and whether the regeneration
     recovered. Those three lines are the collapse measurement this module's
     §4 has to report before and after a change that LENGTHENS the synthesis
     prompt — the exact stressor Module 83 measured as length-sensitive.

No backend is started: this reuses `module92_inprocess_run.run_one()`
verbatim, as Module 116 did, and for the same reason (a machine-wide cap of
2 live backends).

    PYTHONPATH=. python -X utf8 evaluation/module110_live_run.py --runs 6 --tag before
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module110_live"
GOLD_PATH = ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
PARA_PATH = ROOT / "docs" / "gold-qa-wave2-results" / "module110_paraphrases.json"

# Substrings of the log lines this module has to count. Matched against the
# formatted message so a wording change in either source shows up as a zero
# rather than as a silent miscount — the counts are cross-checked against the
# raw `log_lines` list kept alongside them in the output.
_WATCH = {
    "local_llm_failed": "Local LLM failed",
    "synthesis_collapsed": "synthesis collapsed into a repetition loop",
    "regeneration_recovered": "regeneration recovered a usable synthesis",
    "regeneration_collapsed": "regeneration also collapsed",
    "plan_matched": "deterministic decomposition plan",
    "subquery_timeout": "sub-query timed out after",
    "salvaged": "salvag",
    "provenance_attached": "re-attached provenance deterministically",
    "misattributed": "attached to a document that",
}


class _Capture(logging.Handler):
    """Collects the pipeline's own log lines for one run."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self.lines.append(f"{record.levelname} {record.name}: {record.getMessage()}")
        except Exception:  # noqa: BLE001 — a broken log line must not kill a run.
            pass


def _tally(lines: list[str]) -> dict:
    counts = {k: sum(1 for ln in lines if v in ln) for k, v in _WATCH.items()}
    counts["model"] = "groq-fallback" if counts["local_llm_failed"] else "local"
    return counts


def _load(p: Path):
    return json.load(io.open(p, encoding="utf-8"))


def build_items(qids: list[str], gold_only: bool, para_only: bool) -> list[dict]:
    gold = {g["id"]: g for g in _load(GOLD_PATH)}
    if qids == ["ALL32"]:
        # Section 7's regression guard. Routes are compared against
        # `evaluation/gold32_route_baseline.json` (Module 116); ANSWERS need
        # their own before/after arms, because that baseline records routes
        # only. G1 is in here by name and on purpose: it shares
        # `_SQ_ACCUSED_AGE` and `_SQ_RELATIONSHIP` with the plan this module
        # changed, and Module 95 measured a shared sub-query's cost rather
        # than assuming it away.
        qids = [g["id"] for g in _load(GOLD_PATH)]
    out = []
    for qid in qids:
        if not para_only:
            out.append({"id": qid, "variant": "gold",
                        "language": gold[qid]["language"], "text": gold[qid]["question"]})
    if not gold_only and PARA_PATH.exists():
        for p in _load(PARA_PATH)["paraphrases"]:
            if p["gold_id"] in qids:
                out.append({"id": p["gold_id"], "variant": "para",
                            "language": p["language"], "text": p["text"]})
    return out


async def main_async(a) -> None:
    from evaluation.module92_inprocess_run import run_one  # noqa: WPS433

    qids = [q.strip() for q in a.questions.split(",") if q.strip()]
    items = build_items(qids, a.gold_only, a.para_only)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"module110_{a.tag}.json"
    results = _load(path) if path.exists() else {}

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for it in items:
        for run in range(1, a.runs + 1):
            key = f"{it['id']}::{it['variant']}::run{run}"
            if key in results:
                continue
            print(f"{key} ...", flush=True)
            cap = _Capture()
            root.addHandler(cap)
            t0 = time.monotonic()
            try:
                r = await run_one(it["text"])
            except Exception as exc:  # noqa: BLE001
                r = {"route": None, "route_error": f"{type(exc).__name__}: {exc}",
                     "answer": "", "steps": [], "status": "error", "elapsed_s": 0.0}
            finally:
                root.removeHandler(cap)
            r["wall_s"] = round(time.monotonic() - t0, 1)
            r["question"] = it["text"]
            r["variant"] = it["variant"]
            r["signals"] = _tally(cap.lines)
            r["log_lines"] = [ln for ln in cap.lines
                              if any(v in ln for v in _WATCH.values())]
            results[key] = r
            s = r["signals"]
            print(f"    route={r['route']} status={r['status']} {r['wall_s']}s "
                  f"model={s['model']} collapse={s['synthesis_collapsed']} "
                  f"recovered={s['regeneration_recovered']} "
                  f"answer={len(r['answer'])} chars", flush=True)
            with io.open(path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=1, sort_keys=True)
            if it["variant"] == "para":
                break
    print(f"\nwrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default="G6")
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--gold-only", action="store_true")
    ap.add_argument("--para-only", action="store_true")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass
    main()
