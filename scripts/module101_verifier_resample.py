# -*- coding: utf-8 -*-
"""
Module 101 — resample `verify_grounding()` on ONE fixed (answer, chunks) pair.

Phase 1 asks whether KB9's intermittency is the LLM judge's own variance or a
real difference between runs. A live run varies both at once: the retrieval
window and the generated answer change from run to run, so a rejection could
come from either. This script holds BOTH fixed — it replays a captured pair
straight into the real `verify_grounding()` N times — so any variation in the
verdict is the judge's sampling and nothing else.

No backend, no retrieval, no generation: one verifier call per iteration.

Usage:
    PYTHONPATH=. python scripts/module101_verifier_resample.py \
        --runs-file scratchpad/m101_before.json --row 0 --n 10 \
        --out scratchpad/m101_resample.json
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(HERE, ".env"), override=True)

from src.pipeline.verifier import verify_grounding  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-file", required=True)
    ap.add_argument("--row", type=int, default=0)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--out", default=os.path.join("scratchpad", "m101_resample.json"))
    ap.add_argument(
        "--mark-computed",
        action="store_true",
        help="Add the module's new marker to the XAGG chunk before verifying — "
             "the A/B arm, run over the identical pair.",
    )
    args = ap.parse_args()

    rows = json.load(open(args.runs_file, encoding="utf-8"))
    call = rows[args.row]["verifier_calls"][-1]
    answer = call["answer"]
    chunks = [
        {"id": c["id"], "text": c["text"],
         "metadata": {"source": c["source"], "source_tool": c["source_tool"]}}
        for c in call["chunks"]
    ]
    if args.mark_computed:
        from src.pipeline.verifier import COMPUTED_EVIDENCE_META_KEY
        for c in chunks:
            if (c["metadata"].get("source_tool") or "") == "XAGG":
                c["metadata"][COMPUTED_EVIDENCE_META_KEY] = True

    out = []
    for i in range(1, args.n + 1):
        r = await verify_grounding(answer=answer, cited_chunks=chunks, case_id=None)
        out.append({
            "i": i,
            "grounded": r.get("grounded"),
            "off_topic": r.get("off_topic"),
            "unsupported_claims": r.get("unsupported_claims"),
            "reason": r.get("reason"),
            "computed_evidence_override": r.get("computed_evidence_override"),
        })
        print(f"{i}: grounded={r.get('grounded')} off_topic={r.get('off_topic')} "
              f":: {str(r.get('reason'))[:150]}", flush=True)
        for c in (r.get("unsupported_claims") or []):
            print(f"     - {str(c)[:180]}", flush=True)
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        json.dump(out, open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    n_rej = sum(1 for r in out if not r["grounded"] or r["off_topic"])
    print(f"\nrejected {n_rej} of {len(out)}  ->  {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
