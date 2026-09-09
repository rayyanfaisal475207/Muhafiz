"""
Module 52 — Layer 1 probe.

Two things, both offline of the HTTP API (HOW_TO_REPRODUCE_THIS_EVALUATION.md
section 4.1's Layer 1):

  1. What `render_question_in_english()` actually produces for each of the
     eight gold KB questions.
  2. Module 42's 2x2 re-run with THIS module's own rendering in the English
     cell — `evaluate_relevance()` called directly, temperature 0.0,
     `prompts/evaluator.txt` unmodified, the chunk set held CONSTANT and
     containing gold's own statutory text either way.

Usage:  PYTHONPATH=. python scripts/module52_layer1_probe.py [runs]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from src.pipeline.evaluator import evaluate_relevance  # noqa: E402
from src.pipeline.statute_hypothesis import render_question_in_english  # noqa: E402
from src.retrieval.vector_store import expand_with_neighbors, get_chunks_by_ids  # noqa: E402

GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")
OUT = os.environ.get("M52_PROBE_OUT", os.path.join(HERE, "module52_layer1.json"))
KB_IDS = ["KB1", "KB2", "KB3", "KB4", "KB5", "KB6", "KB8", "KB9"]

# Module 42's IDEAL set: c19 carries gold's statutory half verbatim.
IDEAL = [
    "5_Forensics_guidelines_pdf_62ee00b3_c19",
    "5_Forensics_guidelines_pdf_62ee00b3_c18",
    "5_Forensics_guidelines_pdf_62ee00b3_c20",
    "5_Forensics_guidelines_pdf_62ee00b3_c114",
]


async def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    result = {"renderings": {}, "two_by_two": []}

    for qid in KB_IDS:
        q = gold[qid]["question"]
        rendered = await render_question_in_english(q)
        result["renderings"][qid] = {"language": gold[qid]["language"],
                                     "question": q, "english": rendered}
        print(f"{qid} [{gold[qid]['language']}] -> {rendered}")

    chunks = await get_chunks_by_ids(IDEAL)
    chunks = await expand_with_neighbors(chunks, window=1)
    joined = " ".join(c.get("text", "") for c in chunks)
    contains_gold = "safety on" in joined
    result["chunk_set"] = {"ids": [c.get("id") for c in chunks],
                           "contains_gold_text": contains_gold}
    print(f"\nchunk set {len(chunks)} chunks, contains gold's 'safety on': {contains_gold}")

    kb6 = gold["KB6"]["question"]
    english = result["renderings"]["KB6"]["english"]
    for label, text in (("roman-Urdu (gold KB6)", kb6), ("English rendering", english)):
        if not text:
            continue
        for run in range(1, runs + 1):
            verdict = await evaluate_relevance(text, text, chunks)
            result["two_by_two"].append(
                {"arm": label, "run": run, "relevant": bool(verdict.get("relevant")),
                 "reason": (verdict.get("reason") or "")[:300]})
            print(f"  {label:24} run{run}  relevant={verdict.get('relevant')}")

    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
