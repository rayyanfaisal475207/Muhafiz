# -*- coding: utf-8 -*-
"""
Module 65 — KB2 retrieval probe (Module 30's method, on Qanun-e-Shahadat).

Two questions, both offline of the HTTP API:
  1. Are gold's chunks (QSO Arts 38/39, CrPC s.162) IN the corpus at all?
     Answered by literal text match over the whole store, by chunk id.
  2. Are they REACHABLE by any query the pipeline generates?
     Answered by running `query_similar` at the KB-only `{"is_global": True}`
     scope, top-30, for: the gold question, its English rendering, the live
     statute hypotheses (n=2 and n=3), and hand-written statute-vocabulary
     controls.

Usage: PYTHONPATH=. python scripts/module65_probe.py [n_hyp]
"""
from __future__ import annotations

import asyncio, io, json, os, re, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src.retrieval.embedder import embed_text  # noqa: E402
from src.retrieval.vector_store import ChromaVectorStore, query_similar  # noqa: E402
from src.pipeline.statute_hypothesis import (  # noqa: E402
    generate_statute_queries, render_question_in_english,
)

GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")
OUT = os.environ.get("M65_PROBE_OUT", os.path.join(HERE, "module65_probe.json"))

QSO = "2_qanun-e-shahadat-order-1984_pdf_3e604153_c"
CRPC = "1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_f9908363_c"
TARGETS = {
    "QSO Art.38 (confession to police not proved)": QSO + "176",
    "QSO Art.39 (custody, unless before a Magistrate)": QSO + "177",
    "QSO Art.39 Explanation": QSO + "178",
    "CrPC s.162 heading": CRPC + "673",
    "CrPC s.162(1) operative": CRPC + "675",
    "CrPC s.162 proviso": CRPC + "677",
}

CONTROLS = [
    "Qanun-e-Shahadat Order 1984 Article 38 confession to police officer not to be proved: "
    "no confession made to a police officer shall be proved as against a person accused of any offence",
    "Qanun-e-Shahadat Order 1984 Article 39 confession by accused while in custody of police not to be "
    "proved against him unless made in the immediate presence of a Magistrate",
    "Code of Criminal Procedure 1898 section 162 statements to police not to be signed; use of such "
    "statements in evidence: no statement made by any person to a police-officer in the course of an "
    "investigation shall, if reduced into writing, be signed by the person making it, nor be used for any "
    "purpose at any inquiry or trial",
    "Qanun-e-Shahadat Order 1984 Article 38",   # statute NAME only (Module 30's third row)
    "Code of Criminal Procedure 1898 section 162",
]


def existence_check():
    st = ChromaVectorStore.get_instance()
    ids = list(TARGETS.values())
    got = {c["id"]: c for c in st.get_by_ids(ids)}
    out = {}
    for label, cid in TARGETS.items():
        c = got.get(cid)
        out[label] = {"chunk_id": cid, "present": c is not None,
                      "text": (c or {}).get("text", "")[:600]}
        print(f"  [{'OK ' if c else 'ABSENT'}] {label}: {cid}")
    print(f"  store count: {st.count()}")
    return out


async def rank_of_targets(label, query, k=30):
    emb = await embed_text(query)
    hits = await query_similar(query, emb, top_k=k, where={"is_global": True})
    ranks = {}
    for name, cid in TARGETS.items():
        pos = next((i + 1 for i, h in enumerate(hits) if h.get("id") == cid), None)
        ranks[name] = pos
    print(f"\n--- {label}\n    {query[:150]}")
    for name, pos in ranks.items():
        print(f"      {name:52} rank {pos if pos else 'ABSENT'}")
    return {"label": label, "query": query, "ranks": ranks,
            "top5": [h.get("id") for h in hits[:5]]}


async def main():
    n_hyp = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    kb2 = gold["KB2"]["question"]
    result = {"question": kb2, "existence": {}, "retrieval": []}

    print("=== 1. Do gold's chunks exist in the corpus? ===")
    result["existence"] = existence_check()

    print("\n=== 2. Are they reachable? (is_global scope, top-30) ===")
    result["retrieval"].append(await rank_of_targets("the gold question itself", kb2))

    english = await render_question_in_english(kb2)
    result["english_rendering"] = english
    if english:
        result["retrieval"].append(await rank_of_targets("English rendering (Module 52)", english))

    hyps = await generate_statute_queries(kb2, n=n_hyp)
    result["statute_hypotheses"] = hyps
    print(f"\n  live statute hypotheses (n={n_hyp}): {json.dumps(hyps, indent=2)}")
    for i, h in enumerate(hyps, 1):
        result["retrieval"].append(await rank_of_targets(f"statute hypothesis {i} (live, n={n_hyp})", h))

    for c in CONTROLS:
        result["retrieval"].append(await rank_of_targets("CONTROL", c))

    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
