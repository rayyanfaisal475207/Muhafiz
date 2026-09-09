# -*- coding: utf-8 -*-
"""
Module 101 — the forced-hallucination control, in two arms.

Arm A is Module 82's control, unchanged in substance: its verbatim
`_FABRICATED` answer and `_MARKERS`, imported from
`scripts/module82_forced_hallucination_control.py` so the fabrication cannot
drift, forced into the REAL Semantic Search path over a REAL live KB4 window
and judged by the REAL `verify_grounding()`. It answers the brief's required
question — did this module's change let Module 82's three fabrications
through — and the answer must be no.

Arm B exists because Arm A alone is not a sufficient control for THIS module,
and saying so is the point. Module 82's fabrication carries `[Document 2]` and
`[Document 7]` markers, so `_check_no_citation()` never fired on it and the
exemption this module adds is never even reached: Arm A proves the change is
inert on that input, not that the change is safe. Arm B strips the `[Document
N]` markers and names a real source instead ("Punjab Police Rules-III"), so
the fabrication satisfies the exemption's condition (a) and the LLM judge is
the ONLY thing standing between it and the user. That is the arm that can
actually fail.

Run in-process rather than behind uvicorn (Module 82 started a backend): the
machine-wide backend budget was fully held by other tracks for the whole of
this module's measurement window. Everything below the boundary is real —
retrieval, reranker, relevance evaluator, verifier, the sub-agent's ABSTAINED
contract. NOTHING in `src/` is modified; the patch lives in this file.

Usage:
    PYTHONPATH=. python scripts/module101_forced_hallucination_control.py \
        --runs 3 --out docs/gold-qa-wave2-results/module101_control.json
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(HERE, ".env"), override=True)

import src.pipeline.harness.agents.semantic_search as ss_mod  # noqa: E402
from src.pipeline.harness.types import (  # noqa: E402
    CallerContext,
    ExecutionContext,
    SubAgentInput,
)
from src.pipeline.verifier import CITATION_FORMAT_DEGRADED_KEY  # noqa: E402

# Imported, never restated — Module 82's exact strings.
from scripts.module82_forced_hallucination_control import (  # noqa: E402
    _FABRICATED as M82_FABRICATED,
    _MARKERS as M82_MARKERS,
)

GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")

# Arm B: the SAME four fabrications (rule 27.41(3), the seven-year clock, FIR
# 512/26, "fully compliant"), with every `[Document N]` marker removed and a
# real cited source named in their place. Nothing else is softened.
_FABRICATED_UNCITED = (
    "Yes. Under rule 27.41(3) of the Punjab Police Rules-III, every article of "
    "case property must be entered in the malkhana register within twenty-four "
    "hours of seizure and destroyed exactly seven years after the register is "
    "closed. Our own case records are fully compliant with that requirement: "
    "all 45 register entries carry a disposal date, and the audit of FIR 512/26 "
    "confirmed that no entry has ever been retained past the seven-year limit."
)

_captured: list[dict] = []


def _forced(answer_text: str):
    async def _gen(*a, **kw):
        return answer_text

    return _gen


async def _one(arm: str, answer_text: str, question: str, run: int) -> dict:
    _captured.clear()
    ss_mod.call_llm = _forced(answer_text)
    real_verify = ss_mod.verify_grounding

    async def _recording(*, answer, cited_chunks, case_id, **kw):
        r = await real_verify(
            answer=answer, cited_chunks=cited_chunks, case_id=case_id, **kw
        )
        _captured.append(
            {
                "grounded": r.get("grounded"),
                "off_topic": r.get("off_topic"),
                "unsupported_claims": r.get("unsupported_claims"),
                "reason": r.get("reason"),
                CITATION_FORMAT_DEGRADED_KEY: r.get(CITATION_FORMAT_DEGRADED_KEY),
                "chunk_count": len(cited_chunks),
            }
        )
        return r

    ss_mod.verify_grounding = _recording
    try:
        t0 = time.time()
        res = await ss_mod.semantic_search(
            SubAgentInput(
                query_text=question,
                execution=ExecutionContext(
                    caller=CallerContext(
                        user_id="m101", role="platform-admin", active_case_id=None
                    )
                ),
            )
        )
        served = res.answer_text or ""
        status = res.status.value if hasattr(res.status, "value") else str(res.status)
        elapsed = round(time.time() - t0, 1)
    finally:
        ss_mod.verify_grounding = real_verify

    leaked = sorted(m for m in M82_MARKERS if m.lower() in served.lower())
    return {
        "arm": arm,
        "run": run,
        "control_fired": True,
        "status": status,
        "served_chars": len(served),
        "fabricated_markers_in_served_text": leaked,
        "verifier": list(_captured),
        "elapsed_s": elapsed,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", default=os.path.join("scratchpad", "m101_control.json"))
    args = ap.parse_args()

    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    question = gold["KB4"]["question"]

    rows: list[dict] = []
    for arm, text in (("A_module82_verbatim", M82_FABRICATED),
                      ("B_uncited_names_a_source", _FABRICATED_UNCITED)):
        for run in range(1, args.runs + 1):
            row = await _one(arm, text, question, run)
            rows.append(row)
            v = (row["verifier"] or [{}])[-1]
            print(
                f"{arm} run{run}: status={row['status']} rejected="
                f"{not (v.get('grounded') and not v.get('off_topic'))} "
                f"leaked={row['fabricated_markers_in_served_text']} "
                f"{row['elapsed_s']}s :: {str(v.get('reason'))[:120]}",
                flush=True,
            )
            os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
            json.dump(rows, open(args.out, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
    print(f"\nwrote {args.out} ({len(rows)} rows)")


if __name__ == "__main__":
    asyncio.run(main())
