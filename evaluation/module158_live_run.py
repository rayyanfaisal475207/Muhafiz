# -*- coding: utf-8 -*-
"""
Modules 158/164 — live runs through `/api/chat`, with the sub-agent
selection evidence per run.

Reuses `gold32_run.py`'s login/ask/parse verbatim (same account, same SSE
parsing, same top-level-vs-nested dispatch handling from Module 116). Adds,
per run, the lines this module's evidence lives in, read from the backend
log between this request's `POST /api/chat` access line and the next:

    SUBAGENT-SELECT ...      which source selected Meta-Analysis (trigger / plan / semantic)
    SEMANTIC-SELECT ...      the cross-encoder decision, fired or not
    SEMANTIC-DISPATCH ...    Module 145's aggregate-layer decision, if reached
    Falling back to ...      which model answered (local vs cloud)

    PYTHONPATH=. python -X utf8 evaluation/module158_live_run.py --base http://127.0.0.1:8158 \
        --log docs/gold-qa-wave2-results/module158_live/backend_8158.log \
        --tag q1_after --runs 3 --text "How is the most frequently cited offence pattern distributed across districts?"
    PYTHONPATH=. python -X utf8 evaluation/module158_live_run.py ... --tag gold32_after --gold all
    PYTHONPATH=. python -X utf8 evaluation/module158_live_run.py ... --tag m116_after --targets M116-CR3-ur,M116-G1-en,...
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

OUT_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module158_live"
GOLD_PATH = ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
TARGETS = ROOT / "docs" / "gold-qa-wave2-results" / "module158_targets.json"

_WATCH = ("SUBAGENT-SELECT", "SEMANTIC-SELECT", "SEMANTIC-DISPATCH", "Falling back to",
          "deterministic decomposition plan", "Local LLM failed", "XAGG ", "chained step")


def _log_lines_since(log_path: Path, offset: int) -> tuple[list[str], int]:
    if not log_path.exists():
        return [], offset
    with io.open(log_path, encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        chunk = f.read()
        return [ln.rstrip("\n") for ln in chunk.splitlines() if any(w in ln for w in _WATCH)], f.tell()


def _log_size(log_path: Path) -> int:
    return log_path.stat().st_size if log_path.exists() else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--text", action="append", default=[])
    ap.add_argument("--targets", default="", help="comma-separated ids from module158_targets.json")
    ap.add_argument("--gold", default="", help="'all' or comma-separated gold ids")
    args = ap.parse_args()

    os.environ["GOLD32_BASE_URL"] = args.base
    import gold32_run as g32  # noqa: E402
    g32.BASE = args.base

    questions: list[tuple[str, str]] = []
    for t in args.text:
        questions.append((f"text:{t[:24]}", t))
    if args.targets:
        by_id = {t["id"]: t["text"] for t in json.loads(TARGETS.read_text(encoding="utf-8"))["targets"]}
        for tid in args.targets.split(","):
            questions.append((tid, by_id[tid]))
    if args.gold:
        gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
        want = None if args.gold == "all" else set(args.gold.split(","))
        for g in gold:
            if want is None or g["id"] in want:
                questions.append((g["id"], g["question"]))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"module158_{args.tag}.json"
    log_path = Path(args.log)
    records = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else []
    done = {(r["id"], r["run"]) for r in records}

    ac, cs = g32.login()
    for qid, text in questions:
        for run in range(1, args.runs + 1):
            if (qid, run) in done:
                continue
            offset = _log_size(log_path)
            t0 = time.time()
            try:
                p = g32.parse(g32.ask(text, ac, cs))
                p["error"] = None
            except Exception as e:  # noqa: BLE001
                p = {"actual_answer": "", "route": None, "sub_agent": None, "subquery_routes": [],
                     "status": "error", "error": f"{type(e).__name__}: {e}"}
            elapsed = round(time.time() - t0, 1)
            time.sleep(1.0)
            lines, _ = _log_lines_since(log_path, offset)
            fallbacks = sum(1 for ln in lines if "Falling back to" in ln)
            select = [ln.split("SUBAGENT-SELECT", 1)[1].strip() for ln in lines if "SUBAGENT-SELECT" in ln]
            semantic = [ln.split("SEMANTIC-SELECT", 1)[1].strip() for ln in lines if "SEMANTIC-SELECT" in ln]
            rec = {"id": qid, "run": run, "text": text, "elapsed_s": elapsed, "fallbacks": fallbacks,
                   "model": "cloud-fallback" if fallbacks else "local",
                   "subagent_select": select, "semantic_select": semantic, "log_lines": lines, **p}
            records.append(rec)
            out_path.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[{args.tag}] {qid:<18} run{run} route={str(p.get('route')):>9} sub_agent={str(p.get('sub_agent')):<24} "
                  f"{elapsed:>6.1f}s fallbacks={fallbacks} select={select[:1]} sem={[s[:60] for s in semantic[:1]]}", flush=True)
            print(f"      answer: {p.get('actual_answer', '')[:160]!r}", flush=True)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
