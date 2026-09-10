# -*- coding: utf-8 -*-
"""
Module 70 — old-vs-new `verify_structured_aggregate_paraphrase()` equality
control.

The live regression arm can only sample, and this gate is shared by most
XAGG-composing questions — a regression here is broad and quiet, which is
the specific reason Module 83 declined to take this on. This control is
exhaustive over the decision surface instead: it loads `origin/main`'s
`verifier.py` side by side with this branch's and drives BOTH over a matrix
of (answer x rendered-source) inputs drawn from the real renderings the
32-question sweep produced, then asserts the two return the SAME dict on
every key `origin/main` has.

The claim being proved is that Module 70's check is purely ADDITIVE: it
adds `omitted_source_figures` / `omitted_source_headline` and changes
nothing else — not `grounded`, not `unsupported_claims`, not `reason`, not
`leaked_case_id`. Any difference on a pre-existing key is a regression and
this script says so and exits 1.

No LLM, no retrieval, no backend.

Usage:
    PYTHONPATH=. python scripts/module70_equality_control.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import itertools
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASELINE_REF = os.environ.get("M70_BASELINE_REF", "origin/main")
NEW_KEYS = {"omitted_source_figures", "omitted_source_headline"}


def _load_baseline_verifier():
    src = subprocess.run(
        ["git", "show", f"{BASELINE_REF}:src/pipeline/verifier.py"],
        cwd=HERE, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    # Written NEXT TO the real verifier.py: the module resolves its prompt
    # file relative to its own __file__, so it must live in src/pipeline/.
    fd, path = tempfile.mkstemp(
        suffix="_baseline_verifier.py", dir=os.path.join(HERE, "src", "pipeline")
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(src)
    spec = importlib.util.spec_from_file_location("m70_baseline_verifier", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["m70_baseline_verifier"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        os.unlink(path)
    return mod


# ── the matrix ──────────────────────────────────────────────────────────
#
# SOURCES are real `raw_summary_text` headlines captured from the
# 32-question sweep of 2026-09-10 (scratchpad/m70_blast.json), one per
# distinct headline SHAPE the renderers produce, plus the empty/degenerate
# cases the function guards.
SOURCES = {
    "m2_proportion_headline": (
        "Growth: caseload is rising fastest at the 15 general-purpose station(s) — "
        "7 FIRs in 2024 to 39 in 2026, against 3 to 5 at the 2 single-crime-type "
        "station(s) — though even so, 9 of 73 FIRs (~12.3%) are carried by just 2 "
        "of 19 stations, the ones set up for a single type of crime.\n\n"
        "9 of 73 FIRs (~12.3%) are filed at the 2 of 19 stations set up for one "
        "specific type of crime:\n  - PS-KHI-CYB: 5 FIRs\n"
    ),
    "bare_total_headline": "**4 matching Person(s) found.**\n- شہزیب عرف شابی: 2 cases\n",
    "note_headline_with_statute_years": (
        "NOTE: Grouped by the statute(s) each case was registered under "
        "(e.g. PPC, CNSA 1997, Arms Ordinance 1965), not by crime type.\n"
        "- PPC: 25 cases\n- Arms Ordinance 1965: 21 cases\n"
    ),
    "count_headline": (
        "32 case(s) recorded a recovered weapon. What those cases were charged "
        "under, by incident year:\n- 2024: 13 cases\n- 2026: 19 cases\n"
    ),
    "delay_proportion_single_line": (
        "9 of 73 FIRs recorded a reporting-delay reason (~12%); the remaining 64 "
        "were reported with no stated delay."
    ),
    "rate_rows_only": (
        "Weapon recovery by district:\n- Karachi: 3 of 12 cases recovered a weapon (~25%)\n"
        "- Lahore: 7 of 20 cases recovered a weapon (~35%)\n"
    ),
    "grand_total_breakdown": (
        "Breakdown by crime category:\n- PPC: 25 cases\n- Arms Ordinance 1965: 21 cases\n"
        "Breakdown by individual legal code\n- PPC §302: 9 cases\n"
    ),
    "total_count": "Total cases: 73",
    "empty": "",
    "whitespace": "   \n\n  ",
}

ANSWERS = {
    "complete_proportion": (
        "Caseload is rising fastest at the 15 general-purpose stations, from 7 FIRs "
        "in 2024 to 39 in 2026, but even so 9 of 73 FIRs (~12.3%) are carried by "
        "just 2 of 19 stations [Document 1]."
    ),
    "dropped_proportion": (
        "The caseload is growing faster at the general-purpose stations. The 15 "
        "general-purpose stations saw an increase from 7 FIRs in 2024 to 39 FIRs in "
        "2026. In contrast, the 2 stations set up for one specific type of crime saw "
        "a much slower increase, from 3 FIRs in 2024 to 5 FIRs in 2026 [Document 1]."
    ),
    "invented_number": "9 of 73 FIRs from 2 of 19 stations, and 61 reached trial [Document 1].",
    "invented_and_dropped": "The 15 general-purpose stations grew, and 61 reached trial [Document 1].",
    "derived_percentage": "Cyber units carry 9 of 73 FIRs (~12%) [Document 1].",
    "fabricated_percentage": "Cyber units carry 9 of 73 FIRs (~85%) [Document 1].",
    "grand_total": "There are 46 FIRs in total across the two categories [Document 1].",
    "no_figures_at_all": (
        "The provided document does not address how causes of death are investigated "
        "[Document 1]."
    ),
    "fabricated_case_id": "Case CASE-999-FAKE shows 9 of 73 FIRs at 2 of 19 stations [Document 1].",
    "urdu_prose": "عام تھانوں میں 9 از 73 FIRs، 2 از 19 تھانوں میں درج ہیں [Document 1]۔",
    "roman_urdu_prose": "73 FIRs mein se 9 sirf 19 thanon mein se 2 par hain [Document 1].",
    "list_shaped": "- 9 of 73 FIRs\n- 2 of 19 stations\n1. general-purpose: 15\n[Document 1]",
    "empty": "",
    "whitespace": "  \n ",
    "citation_only": "[Document 1]",
}

CASE_IDS = [None, "cross_case", "CASE-001"]
CROSS_IDS = [None, ["CASE-001", "CASE-002"]]


async def main() -> int:
    baseline = _load_baseline_verifier()
    from src.pipeline import verifier as new  # noqa: PLC0415

    diffs: list[str] = []
    fired = 0
    cells = 0

    for (sname, src), (aname, ans), cid, xids in itertools.product(
        SOURCES.items(), ANSWERS.items(), CASE_IDS, CROSS_IDS
    ):
        cells += 1
        old_r = await baseline.verify_structured_aggregate_paraphrase(
            answer=ans, source_text=src, case_id=cid, cross_case_ids=xids
        )
        new_r = await new.verify_structured_aggregate_paraphrase(
            answer=ans, source_text=src, case_id=cid, cross_case_ids=xids
        )
        if new_r.get("omitted_source_figures"):
            fired += 1
        for key in old_r:
            if old_r[key] != new_r.get(key):
                diffs.append(
                    f"{sname} x {aname} x case_id={cid} x cross={xids}: "
                    f"{key}: old={old_r[key]!r} new={new_r.get(key)!r}"
                )
        extra = set(new_r) - set(old_r)
        if extra - NEW_KEYS:
            diffs.append(f"{sname} x {aname}: unexpected new key(s) {extra - NEW_KEYS}")

    print(f"cells compared: {cells}")
    print(f"cells where the new omission check fired: {fired}")
    if diffs:
        print(f"REGRESSION — {len(diffs)} cell(s) differ on a pre-existing key:")
        for d in diffs[:40]:
            print("  " + d)
        return 1
    print(
        "PASS — every pre-existing key is byte-identical between "
        f"{BASELINE_REF} and this branch on all {cells} cells; the only "
        "difference is the two additive keys."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
