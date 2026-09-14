# -*- coding: utf-8 -*-
"""
Module 150 — does a served G1 / G6 answer still carry gold's findings?

Substance, not wording (the standard the judge prompt encodes): each finding
is a small set of alternative markers — the computed number, or the Urdu /
Roman-Urdu / English term for it — and a finding counts as carried when ANY
marker is present. Applied to every run in one or more module150_*.json
files so the before/after comparison is over all runs, not a hand-picked one.

    PYTHONPATH=. python -X utf8 evaluation/module150_findings_check.py docs/gold-qa-wave2-results/module150_live/module150_before_g6.json ...
"""
from __future__ import annotations

import io
import json
import re
import sys

# Gold's findings, mapped to what the plan's sub-queries actually compute
# (MODULE110_RESULT.md §1.1 for G6; MODULE29/50 for G1's caseload_review).
FINDINGS = {
    "G6": {
        "1 districts (73 FIRs, Faisalabad/Lahore lead)": [r"فیصل آباد|faisalabad", r"لاہور|lahore"],
        "2 case mix shifted (robbery -> narcotics/cyber)": [r"cyber|PECA|narcotic|CNSA|منشیات|سائبر", r"robbery|arms|ڈکیتی|اسلحہ"],
        "3 accused profile (age 24-49 / stranger)": [r"24[–-]49|31\.5", r"اجنبی|stranger|ajnabi"],
        "4 arrest rate (11 of 73, ~1 in 6)": [r"\b11\b.{0,40}(FIR|case|arrest)|1 in 6|11 of 73|11/73"],
        "5 mostly still pending in court (32 of 33)": [r"32.{0,60}(progress|pending|ongoing|trial|زیر|court)|zeir-e-samaat|still in progress"],
        "6 fraud/cyber reported later (2026 1401 min vs 2024 15 min)": [r"1,?401|23\.4 hours", r"\b15(\.0)? min"],
        "7 weapons recovered, almost all unlicensed (30 of 32)": [r"\b30\b.{0,80}(licen|لائسنس)|unlicens|بلا لائسنس|without (a )?licen"],
    },
    "G1": {
        "1 uniform offender age (24-49, mean ~31)": [r"24[–-]49|31\.5"],
        "2 stranger-perpetrated skew": [r"اجنبی|stranger"],
        "3 seized property points to fatalities (13 forensic / 7 heirs)": [r"\b13\b.{0,120}forensic|forensic.{0,120}\b13\b", r"\b7\b.{0,120}(heir|deceased|ورثا)|(heir|deceased|ورثا).{0,120}\b7\b"],
        "4 incident times flat, mild evening lean": [r"evening|شام|afternoon|time of day|morning"],
    },
}


def check(qid: str, answer: str) -> dict[str, bool]:
    out = {}
    for name, alts in FINDINGS[qid].items():
        # every alternative-group in the list must match (they are the
        # finding's components); within a group, the regex alternation is the OR.
        out[name] = all(re.search(rx, answer, re.IGNORECASE) for rx in alts)
    return out


def main(paths: list[str]) -> None:
    for p in paths:
        data = json.load(io.open(p, encoding="utf-8"))
        print(f"\n== {p}")
        for key in sorted(data):
            qid = key.split("::")[0]
            if qid not in FINDINGS:
                continue
            r = data[key]
            res = check(qid, r.get("answer", ""))
            carried = sum(res.values())
            missing = [k for k, v in res.items() if not v]
            s = r.get("signals", {})
            print(f"  {key:14} findings {carried}/{len(res)}  model={s.get('model','?'):13} "
                  f"fallbacks={s.get('groq_fallbacks','?')} wall={r.get('wall_s','?')}s answer={len(r.get('answer',''))} chars"
                  + (f"  MISSING: {missing}" if missing else ""))


if __name__ == "__main__":
    main(sys.argv[1:])
