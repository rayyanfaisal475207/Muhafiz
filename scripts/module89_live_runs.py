"""
Module 89 — live runner for KB1's data half.

A thin wrapper around `scripts/module74_live_runs.py`: same login, same SSE
parse, same `backend.log` slicing, so the "before" arm and the "after" arm
and Modules 74/75/76/77's own numbers are all measured by identical
machinery. Only the probe set differs.

What it adds:
  * KB1's four PRE-REGISTERED paraphrases (committed in
    `docs/gold-qa-wave2-results/module89_paraphrases.json` before the first
    run, and read from that file here rather than restated, so they cannot
    be quietly edited after a miss).
  * The canned data-half sub-query, sent through the real `/api/chat` — the
    only thing that proves the new aggregate survives the harness wrapper
    and its hand-maintained `AggregateKind` Literal, which no unit test
    reaches.

ROUTE AND PLAN ARE TWO DIFFERENT MEASUREMENTS. Module 94 exists because
they were conflated: a question can route to RAG and still fire no plan.
The runner reports `route=` from the SSE stream and the
`RAG tool: KB data-half plan '<plan>' answered by aggregate '<kind>'` line
from `backend.log` separately.

Usage:
    GOLD32_BASE_URL=http://127.0.0.1:8089 \
    M74_LOG="D:/Rapids AI/muhafiz-m89/backend.log" \
    M74_OUT=.../module89_runs.json M74_RUNS=3 M74_ARM=after \
    M89_IDS=KB1,P1,P2,P3,P4 \
    python scripts/module89_live_runs.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import scripts.module74_live_runs as m74  # noqa: E402

_PARAPHRASES = os.path.join(
    HERE, "docs", "gold-qa-wave2-results", "module89_paraphrases.json"
)

# The canned data-half sub-query, verbatim from
# `rag.py::_KB_DATA_HALF_PLANS` / `tests/test_xagg.py`. Sent directly for
# the same reason Module 74 sent its three: a `_KB_DATA_HALF_PLANS`
# sub-query is handed straight to `xagg_tool()` and never sees the router,
# so this is the arm that exercises the wrapper end to end.
_SQ89 = (
    "How many cases in the FIR register record complainant details, "
    "a recording officer, and a report time, across all cases?"
)


def _probes() -> dict:
    committed = json.load(open(_PARAPHRASES, encoding="utf-8"))
    assert committed["written_before_any_run"] is True
    probes = {p["id"]: p["text"] for p in committed["paraphrases"]}
    probes["SQ89"] = _SQ89
    return probes


def main() -> None:
    m74.PROBES = _probes()
    ids = [i for i in (os.environ.get("M89_IDS") or "").split(",") if i]
    m74.IDS = ids or (["KB1"] + list(m74.PROBES))
    m74.main()


if __name__ == "__main__":
    main()
