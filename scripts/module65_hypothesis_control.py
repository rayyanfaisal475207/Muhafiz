# -*- coding: utf-8 -*-
"""
Module 65 — equality control on the statute-hypothesis step.

The Module 65 change is a prompt edit on the SHARED legal-KB path, so every
KB question's hypotheses have to be compared before/after, not just KB2's.
Runs `generate_statute_queries()` N times per question against the CURRENT
prompt and against a saved copy of the pre-change one, and reports which
statute BOOK each hypothesis names — the thing that decides retrieval — so
model wording noise is not mistaken for a behaviour change.

Usage:
  PYTHONPATH=. python scripts/module65_hypothesis_control.py <old_prompt.txt> [runs]
"""
from __future__ import annotations
import asyncio, collections, io, json, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import src.pipeline.statute_hypothesis as sh  # noqa: E402

GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")
OUT = os.environ.get("M65_CONTROL_OUT", os.path.join(HERE, "module65_hypothesis_control.json"))
KB_IDS = ["KB1", "KB2", "KB3", "KB4", "KB5", "KB6", "KB8", "KB9"]

BOOKS = [
    ("CrPC", "code of criminal procedure"),
    ("QSO", "qanun-e-shahadat"),
    ("PoliceOrder", "police order"),
    ("PPR", "punjab police rules"),
    ("AntiRape", "anti-rape"),
    ("Forensics", "forensics"),
    ("PTA", "telecommunication"),
]


def book_of(q: str) -> str:
    low = q.lower()
    for name, needle in BOOKS:
        if needle in low:
            return name
    return "?"


async def main():
    old_path = sys.argv[1]
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    new_tpl = sh._PROMPT_TEMPLATE
    old_tpl = io.open(old_path, encoding="utf-8").read()

    result = {}
    for arm, tpl in (("before", old_tpl), ("after", new_tpl)):
        sh._PROMPT_TEMPLATE = tpl
        for qid in KB_IDS:
            key = f"{qid}/{arm}"
            result[key] = []
            for _ in range(runs):
                hyps = await sh.generate_statute_queries(gold[qid]["question"])
                result[key].append({"books": [book_of(h) for h in hyps], "queries": hyps})

    print(f"{'Q':5} {'before books':34} {'after books':34}")
    for qid in KB_IDS:
        b = collections.Counter(tuple(r["books"]) for r in result[f"{qid}/before"])
        a = collections.Counter(tuple(r["books"]) for r in result[f"{qid}/after"])
        fmt = lambda c: "; ".join(f"{'+'.join(k)}x{v}" for k, v in c.most_common())
        same = "  SAME" if set(b) == set(a) else "  DIFF"
        print(f"{qid:5} {fmt(b):34} {fmt(a):34}{same}")

    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
