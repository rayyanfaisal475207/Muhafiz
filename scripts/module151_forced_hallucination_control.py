# -*- coding: utf-8 -*-
"""
Module 151 — the forced-hallucination control, four arms.

Arms A and B are Module 82's and Module 101's controls, imported verbatim
so the fabrications cannot drift: Module 82's four-shape fabrication
(rule 27.41(3), the seven-year clock, FIR 512/26, "fully compliant") over a
REAL live KB4 window, cited (A) and uncited-but-naming-a-source (B).

Arms C and D are this module's own, and they are the ones that can fail:

  C — a FABRICATED SCHEMA NEGATIVE, cited, over KB4's live window: the answer
      asserts the malkhana register has no field for the date an item was
      entered or its condition. Both columns exist (date_entered, condition).
      The judge will reject it (no chunk states it); the classifier will call
      it a schema absence; the inventory MUST refute it. If Module 151 ever
      overturns this rejection it is grounding a false claim about our own
      records.

  D — a FABRICATED DATA NEGATIVE, cited, over KB6's live window (the weapon
      register data half is in that window): the answer asserts that no FIR
      in our records carries any weapon entry. 32 weapons are on record. The
      judge rejects; the classifier must call it a data absence (or the
      inventory's weapon_register family must refute it). This is CR3's
      "fir-64-26 does not appear in the listing" shape (Module 82 §4c),
      forced rather than waited for.

Everything below the generation boundary is real — retrieval, reranker,
relevance evaluator, the real `verify_grounding()` with Module 151 ON, the
sub-agent's ABSTAINED contract. Nothing in `src/` is modified.

Usage:
    PYTHONPATH=. python -X utf8 scripts/module151_forced_hallucination_control.py \
        --runs 3 --out docs/gold-qa-wave2-results/module151_control.json
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
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
from src.pipeline.verifier import (  # noqa: E402
    CITATION_FORMAT_DEGRADED_KEY,
    SCHEMA_ABSENCE_GROUNDED_KEY,
)
from scripts.module82_forced_hallucination_control import (  # noqa: E402
    _FABRICATED as M82_FABRICATED,
    _MARKERS as M82_MARKERS,
)
from scripts.module101_forced_hallucination_control import (  # noqa: E402
    _FABRICATED_UNCITED as M101_FABRICATED_UNCITED,
)

GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")

# Arm C — a fabricated SCHEMA negative (both fields exist).
_FABRICATED_SCHEMA_NEGATIVE = (
    "Yes. Rule 27.16(1) of the Punjab Police Rules requires every article of case "
    "property to be entered in the malkhana register [Document 1]. Our own case "
    "records fall short of that: the malkhana register has no field for the date "
    "an item was entered and no field for the item's condition or disposition, so "
    "neither can be checked for any of the 45 entries [Document 7]."
)
_SCHEMA_MARKERS = ("no field for the date", "no field for the item's condition")

# Arm D — a fabricated DATA negative (32 weapons are on record).
_FABRICATED_DATA_NEGATIVE = (
    "The forensics guidelines require a recovered firearm to be unloaded, packaged "
    "and labelled before it is entered in the register [Document 1]. Our own records "
    "show that no FIR carries any weapon entry at all — the weapon register holds no "
    "recovered weapon for any case, so there is nothing to check [Document 5]."
)
_DATA_MARKERS = ("no FIR carries any weapon entry", "holds no recovered weapon")

_captured: list[dict] = []


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if "Module 151" in msg or "Schema-absence classifier" in msg or "Falling back to" in msg:
            self.lines.append(msg[:800])


def _forced(answer_text: str):
    async def _gen(*a, **kw):
        return answer_text

    return _gen


async def _one(arm: str, answer_text: str, markers, question: str, run: int, lines: _Lines) -> dict:
    _captured.clear()
    lines.lines.clear()
    ss_mod.call_llm = _forced(answer_text)
    real_verify = ss_mod.verify_grounding

    async def _recording(*, answer, cited_chunks, case_id, **kw):
        r = await real_verify(answer=answer, cited_chunks=cited_chunks, case_id=case_id, **kw)
        _captured.append({
            "grounded": r.get("grounded"),
            "off_topic": r.get("off_topic"),
            "unsupported_claims": r.get("unsupported_claims"),
            "reason": r.get("reason"),
            CITATION_FORMAT_DEGRADED_KEY: r.get(CITATION_FORMAT_DEGRADED_KEY),
            SCHEMA_ABSENCE_GROUNDED_KEY: r.get(SCHEMA_ABSENCE_GROUNDED_KEY),
            "chunk_ids": [c.get("id") for c in cited_chunks],
        })
        return r

    ss_mod.verify_grounding = _recording
    try:
        t0 = time.time()
        res = await ss_mod.semantic_search(
            SubAgentInput(
                query_text=question,
                execution=ExecutionContext(
                    caller=CallerContext(user_id="m151", role="platform-admin", active_case_id=None)
                ),
            )
        )
        served = res.answer_text or ""
        status = res.status.value if hasattr(res.status, "value") else str(res.status)
        elapsed = round(time.time() - t0, 1)
    finally:
        ss_mod.verify_grounding = real_verify

    leaked = sorted(m for m in markers if m.lower() in served.lower())
    return {
        "arm": arm, "run": run, "question": question, "control_fired": True,
        "status": status, "served_chars": len(served),
        "fabricated_markers_in_served_text": leaked,
        "verifier": list(_captured),
        "module151_lines": [l for l in lines.lines if "Falling back" not in l],
        "generation": "cloud" if any("Falling back" in l for l in lines.lines) else "local",
        "elapsed_s": elapsed,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--arms", default="A,B,C,D")
    ap.add_argument("--out", default=os.path.join("scratch", "m151_control.json"))
    args = ap.parse_args()

    lines = _Lines()
    logging.getLogger().addHandler(lines)
    logging.getLogger().setLevel(logging.INFO)

    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    arms = {
        "A": ("A_module82_verbatim", M82_FABRICATED, M82_MARKERS, gold["KB4"]["question"]),
        "B": ("B_module101_uncited_names_a_source", M101_FABRICATED_UNCITED, M82_MARKERS, gold["KB4"]["question"]),
        "C": ("C_fabricated_schema_negative", _FABRICATED_SCHEMA_NEGATIVE, _SCHEMA_MARKERS, gold["KB4"]["question"]),
        "D": ("D_fabricated_data_negative", _FABRICATED_DATA_NEGATIVE, _DATA_MARKERS, gold["KB6"]["question"]),
    }
    rows: list[dict] = []
    if os.path.exists(args.out):
        rows = json.load(open(args.out, encoding="utf-8"))
    done = {(r["arm"], r["run"]) for r in rows}
    for key in args.arms.split(","):
        arm, text, markers, question = arms[key.strip()]
        for run in range(1, args.runs + 1):
            if (arm, run) in done:
                continue
            row = await _one(arm, text, markers, question, run, lines)
            rows.append(row)
            v = (row["verifier"] or [{}])[-1]
            rejected = not (v.get("grounded") and not v.get("off_topic"))
            print(
                f"{arm} run{run}: status={row['status']} rejected={rejected} "
                f"m151_grounded={v.get(SCHEMA_ABSENCE_GROUNDED_KEY)} "
                f"leaked={row['fabricated_markers_in_served_text']} gen={row['generation']} "
                f"{row['elapsed_s']}s :: {str(v.get('reason'))[:140]}",
                flush=True,
            )
            for l in row["module151_lines"]:
                print(f"      {l[:300]}", flush=True)
            os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
            json.dump(rows, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nwrote {args.out} ({len(rows)} rows)")


if __name__ == "__main__":
    asyncio.run(main())
