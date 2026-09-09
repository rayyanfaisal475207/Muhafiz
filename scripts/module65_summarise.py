# -*- coding: utf-8 -*-
"""
Module 65 — summarise module65_runs.json.

Reports, per question and arm: route, status, runtime, evaluator rounds, and
— by CHUNK ID, never by citation — whether the run retrieved Qanun-e-Shahadat
Arts 38/39 or CrPC s.162.

Usage: PYTHONPATH=. python scripts/module65_summarise.py [runs.json]
"""
from __future__ import annotations
import collections, io, json, os, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
PATH = sys.argv[1] if len(sys.argv) > 1 else "module65_runs.json"

QSO = "2_qanun-e-shahadat-order-1984_pdf_3e604153_c"
CRPC = "1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_f9908363_c"
# Art. 38 spans c176; Art. 39 c177 with its Explanation in c178. s.162's
# heading is c673/c674, the operative sub-section c675/c676, proviso c677.
GOLD_TARGETS = {
    "QSO A38": {QSO + "176"},
    "QSO A39": {QSO + "177", QSO + "178"},
    "CrPC 162": {CRPC + n for n in ("673", "674", "675", "676", "677")},
}


def hits(rec):
    got = {c for a in rec.get("attempts", []) for c in a["chunk_ids"]}
    return {k: bool(got & v) for k, v in GOLD_TARGETS.items()}


def main():
    rows = json.load(open(PATH, encoding="utf-8"))
    by = collections.defaultdict(list)
    for r in rows:
        by[(r["id"], r["arm"])].append(r)
    for (qid, arm) in sorted(by, key=lambda k: (k[0], k[1] != "before")):
        for r in sorted(by[(qid, arm)], key=lambda x: x["run"]):
            h = hits(r)
            flags = " ".join(f"{k}={'Y' if v else '.'}" for k, v in h.items())
            print(f"{qid:5} {arm:6} r{r['run']} route={str(r['route']):>8} "
                  f"{str(r['status']):>5} {r['elapsed_s']:>6}s rounds={r['evaluator_rounds']} "
                  f"len={len(r['actual_answer']):>5}  {flags}")
    print("\n--- gold-chunk retrieval counts (KB2 family) ---")
    for qid in ("KB2", "KB2P", "KB2Q"):
        for arm in ("before", "after"):
            rs = by.get((qid, arm), [])
            if not rs:
                continue
            n = len(rs)
            any_gold = sum(1 for r in rs if any(hits(r).values()))
            print(f"{qid:5} {arm:6}: {any_gold}/{n} runs retrieved at least one gold chunk; "
                  + ", ".join(f"{k} {sum(1 for r in rs if hits(r)[k])}/{n}" for k in GOLD_TARGETS))


if __name__ == "__main__":
    main()
