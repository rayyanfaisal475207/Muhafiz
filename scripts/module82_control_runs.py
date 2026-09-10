"""
Modules 82/85/86 — driver for the forced-hallucination controls.

Sends one question N times at a control backend and records, per run: the
status the user sees, the served text, whether any fabrication marker survived
into it, and the verifier's own rejection line read out of the control log.

Two controls are driven by this one script, because the hard constraint has
two halves on two different code paths:

  * `scripts/module82_forced_hallucination_control.py` — the RAG path, which
    is the path this module's prompt change is on. Question: KB4's gold text.
  * `scripts/module71_forced_hallucination_control.py` — Module 71's own,
    unchanged, on the Meta-Analysis path. Question: G1's gold text. Run to
    show Module 71's guarantee still holds under this module's change.

Usage:
    M82_CTRL_PORT=8032 M82_CTRL_QID=KB4 M82_CTRL_LOG=.../control.log \
    M82_CTRL_OUT=.../control.json M82_CTRL_RUNS=3 M82_CTRL_TAG=rag \
    python scripts/module82_control_runs.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "scripts"))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "m82runs", os.path.join(HERE, "scripts", "module82_kb_runs.py")
)
_m82 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m82)

GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")
PORT = os.environ.get("M82_CTRL_PORT", "8032")
QID = os.environ.get("M82_CTRL_QID", "KB4")
LOG = os.environ.get("M82_CTRL_LOG", os.path.join(HERE, "control.log"))
OUT = os.environ.get("M82_CTRL_OUT", os.path.join(HERE, "module82_control.json"))
RUNS = int(os.environ.get("M82_CTRL_RUNS", "3"))
TAG = os.environ.get("M82_CTRL_TAG", "rag")

# Every fabricated string either control injects. A run "leaked" if any of
# them reaches the served text — the property that must hold 3 of 3.
MARKERS = {
    "rag": ["27.41", "seven years", "seven-year", "512/26", "fully compliant"],
    "meta": ["73-case", "data discrepancy"],
}


def main() -> None:
    _m82.BASE = f"http://127.0.0.1:{PORT}"
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    question = gold[QID]["question"]
    markers = MARKERS[TAG]

    out = []
    if os.path.exists(OUT):
        out = json.load(open(OUT, encoding="utf-8"))
    ac, cs = _m82.login()
    for run in range(1, RUNS + 1):
        off = _m82.log_size(LOG)
        t0 = time.time()
        try:
            p = _m82.parse(_m82.ask(question, ac, cs))
            p["transport_ok"] = True
        except Exception as e:  # noqa: BLE001
            p = {"actual_answer": "", "route": None, "status": "error",
                 "transport_ok": False, "error": f"{type(e).__name__}: {e}"}
        p["elapsed_s"] = round(time.time() - t0, 1)
        time.sleep(2)
        slice_ = _m82.log_slice(LOG, off)
        p.update(_m82.dissect(slice_))
        served = p.get("actual_answer") or ""
        p["leaked_markers"] = [m for m in markers if m.lower() in served.lower()]
        p["control_fired"] = bool(re.search(r"CONTROL: returning a fabricated", slice_))
        rec = {"id": QID, "run": run, "tag": TAG, "port": PORT, **p}
        out.append(rec)
        json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(
            f"[{TAG} run{run}] {QID} status={p.get('status')} "
            f"control_fired={p['control_fired']} "
            f"verifier_rejections={len(p.get('verifier_rejections') or [])} "
            f"leaked={p['leaked_markers']} len={len(served)} {p['elapsed_s']}s"
        )
        sys.stdout.flush()
    print(f"wrote {len(out)} rows to {OUT}")


if __name__ == "__main__":
    main()
