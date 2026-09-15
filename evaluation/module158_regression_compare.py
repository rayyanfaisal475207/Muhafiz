# -*- coding: utf-8 -*-
"""
Modules 158/164 — §7 regression: this branch's 32-question run on `:8158`
against Module 150's run on `main` (`module150_live/module150_gold32_outputs.json`,
the most recent all-32 pass on the code this branch was cut from) and the
Module 116 route baseline; per-question Groq fallbacks from this run's own
log lines (captured per request by `module158_live_run.py`).

    PYTHONPATH=. python -X utf8 evaluation/module158_regression_compare.py
"""
from __future__ import annotations

import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
R = ROOT / "docs" / "gold-qa-wave2-results"
OURS = R / "module158_live" / "module158_gold32_after.json"
MAIN = R / "module150_live" / "module150_gold32_outputs.json"
BASELINE = ROOT / "evaluation" / "gold32_route_baseline.json"
OUT = R / "module158_live" / "module158_regression_compare.txt"


def _load(p):
    return json.load(io.open(p, encoding="utf-8"))


def main() -> None:
    ours = {r["id"]: r for r in _load(OURS)}
    main_run = {r["id"]: r for r in _load(MAIN)}
    base = _load(BASELINE)["routes"]
    lines = []
    route_eq = sub_eq = ans_eq = 0
    fb_total = 0
    lines.append(f"{'id':5} {'route':9} {'=main':5} {'=base':5} {'sub_agent':24} {'=main':5} {'subq(ours)':22} {'subq(main)':22} {'fb':3} {'ans=':5} {'s(ours)':8} {'s(main)':8}")
    for qid, o in ours.items():
        m = main_run.get(qid, {})
        b = base[qid]
        b_route = b["route"] if isinstance(b, dict) else b
        r_eq = o["route"] == m.get("route")
        b_eq = o["route"] == b_route
        s_eq = o["sub_agent"] == m.get("sub_agent")
        a_eq = (o["actual_answer"] or "").strip() == (m.get("actual_answer") or "").strip()
        route_eq += r_eq
        sub_eq += s_eq
        ans_eq += a_eq
        fb_total += o["fallbacks"]
        lines.append(f"{qid:5} {str(o['route']):9} {str(r_eq):5} {str(b_eq):5} {str(o['sub_agent']):24} {str(s_eq):5} "
                     f"{str(o['subquery_routes'])[:22]:22} {str(m.get('subquery_routes'))[:22]:22} {o['fallbacks']:<3} {str(a_eq):5} "
                     f"{o['elapsed_s']:<8} {m.get('elapsed_s', ''):<8}")
    lines.append("")
    lines.append(f"route == main on {route_eq}/32; sub_agent == main on {sub_eq}/32; answer byte-equal to main on {ans_eq}/32; "
                 f"Groq fallbacks this run: {fb_total} (main/Module 150 run: 3)")
    lines.append(f"total wall: ours {sum(o['elapsed_s'] for o in ours.values())/60:.1f} min; main {sum(m.get('elapsed_s', 0) for m in main_run.values())/60:.1f} min")
    lines.append("")
    lines.append("questions with fallbacks:")
    for qid, o in ours.items():
        if o["fallbacks"]:
            lines.append(f"  {qid}: {o['fallbacks']} — " + " | ".join(l.split('] ')[-1][:110] for l in o["log_lines"] if "Falling back" in l))
    lines.append("")
    lines.append("selection lines (SUBAGENT-SELECT / SEMANTIC-SELECT) seen on gold questions:")
    for qid, o in ours.items():
        for l in o["subagent_select"] + o["semantic_select"]:
            lines.append(f"  {qid}: {l[:140]}")
    txt = "\n".join(lines)
    OUT.write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
