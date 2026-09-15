# -*- coding: utf-8 -*-
"""
Module 161 — the aggregate's OWN role gate, live: real gateway (audit log),
real graph, real `run_aggregate()`. The only thing planted is the dispatch
decision (the cross-encoder scores Q2 at 0.034 for this kind, so live Q2
never reaches `run_aggregate()` as this kind — §4/§6). Counts every
`age_client.execute_cypher` call so "denied with zero graph reads" is a
measurement, not a claim.

    PYTHONPATH=. python -X utf8 evaluation/module161_gate_live.py
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

Q2 = ("Is there anyone who used one of our citizen services who also turns "
      "out to be under investigation for a crime?")
OUT = ROOT / "docs" / "gold-qa-wave2-results" / "module161_live" / "module161_gate_live.json"


async def main() -> None:
    from src.data_gateway import get_gateway
    from src.graph import age_client
    from src.pipeline import semantic_dispatch as sd
    from src.pipeline import xagg

    gateway = await get_gateway()
    sd.seed_for_tests(Q2, sd.SemanticMatch("applicant_accused_overlap", 0.9, "graph_recurrence_person", 0.05))
    assert xagg.resolve_aggregate_kind(Q2) == "applicant_accused_overlap"

    calls: list[str] = []
    orig = age_client.execute_cypher

    async def counted(query, *a, **kw):
        calls.append(query[:60])
        return await orig(query, *a, **kw)
    age_client.execute_cypher = counted

    out = {}
    for role in ("investigator", "supervisor"):
        calls.clear()
        try:
            r = await xagg.run_aggregate(Q2, None, gateway, user_id="81f347d0-7634-40c2-86a0-2fa42469604a", user_role=role)
            out[role] = {"outcome": "answered", "kind": r["kind"], "matched_count": r["matched_count"],
                         "cypher_reads": len(calls)}
        except PermissionError as exc:
            out[role] = {"outcome": "PermissionError", "message": str(exc), "cypher_reads": len(calls)}
        print(role, json.dumps(out[role], ensure_ascii=False))
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    asyncio.run(main())
