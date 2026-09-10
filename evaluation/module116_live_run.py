# -*- coding: utf-8 -*-
"""
Module 116 — served ANSWERS for CR3/G1/G6, several runs each, in process.

Reuses `evaluation/module92_inprocess_run.py::run_one()` verbatim (the same two
stages `main.py::chat_endpoint()` uses) rather than copying it — Module 116's
brief caps live backends at 2 machine-wide and both were occupied by other
tracks when this ran, so no third backend was started.

The one thing NOT inherited from Module 92's runner is its `.env` load: that one
loads the main checkout's `.env`, whose `CHROMA_PERSIST_DIR` is RELATIVE
(`./data/chroma_db`) and would therefore resolve against this worktree and
silently create an empty vector store. This loads the worktree's own `.env`,
which pins the absolute shared path.

    PYTHONPATH=. python -X utf8 evaluation/module116_live_run.py --runs 3 --tag after
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module116_live"
GOLD_PATH = ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
PARA_PATH = ROOT / "docs" / "gold-qa-wave2-results" / "module116_paraphrases.json"


def _load(p: Path):
    return json.load(io.open(p, encoding="utf-8"))


def build_items(qids: list[str], gold_only: bool, para_only: bool) -> list[dict]:
    gold = {g["id"]: g for g in _load(GOLD_PATH)}
    out = []
    for qid in qids:
        if not para_only:
            out.append({"id": qid, "variant": "gold",
                        "language": gold[qid]["language"], "text": gold[qid]["question"]})
    if not gold_only:
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
    path = OUT_DIR / f"module116_inprocess_{a.tag}.json"
    results = _load(path) if path.exists() else {}

    for it in items:
        for run in range(1, a.runs + 1):
            key = f"{it['id']}::{it['variant']}::{it['language']}::run{run}"
            if key in results:
                continue
            print(f"{key} ...", flush=True)
            try:
                r = await run_one(it["text"])
            except Exception as exc:  # noqa: BLE001
                r = {"route": None, "route_error": f"{type(exc).__name__}: {exc}",
                     "answer": "", "steps": [], "status": "error", "elapsed_s": 0.0}
            r["question"] = it["text"]
            results[key] = r
            print(f"    route={r['route']} status={r['status']} "
                  f"{r['elapsed_s']}s answer={len(r['answer'])} chars", flush=True)
            with io.open(path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=1, sort_keys=True)
            # A paraphrase only needs one run: §6 reports route and answer, not
            # a rate. The repeat budget belongs to the gold wordings.
            if it["variant"] == "para":
                break
    print(f"\nwrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default="CR3,G1,G6")
    ap.add_argument("--runs", type=int, default=3)
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
