# -*- coding: utf-8 -*-
"""
Module 116 — the gold-32 route baseline recorded a Meta-Analysis SUB-QUERY's
route as the question's route.

`gold32_pass{1,2,3}_outputs.json` records CR3, G1 and G6 as `route=XAGG` on all
three passes; measured, their top-level route is XNETWORK 11 runs of 11 at
temperature 0. Nothing about the router moved. `supervisor.py` emits its
`supervisor:dispatch` event once for the question asked and once for every
sub-query `meta_analysis.py` decomposes it into, the only distinction lived in
English prose inside `detail`, and `gold32_run.py::parse()` regex-scraped that
prose and kept the LAST match — a decomposed sub-query, chosen by the race
between concurrently-dispatched sub-queries.

These tests pin the three halves of the fix: the event carries the facts, the
adapter forwards them, and the recorder reads them.

    PYTHONPATH=. python -m pytest tests/test_module116_route_recording.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_gold32_run():
    """`evaluation/` is not a package; load the recorder by path."""
    path = ROOT / "evaluation" / "gold32_run.py"
    spec = importlib.util.spec_from_file_location("module116_gold32_run", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sse(events: list[dict]) -> str:
    return "".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n" for e in events)


# The shape Module 92's own captured trace records for CR3 and G6 on unchanged
# code (docs/gold-qa-wave2-results/module92_live/module92_inprocess_before.json):
# one top-level XNETWORK dispatch to Meta-Analysis, then N XAGG sub-dispatches.
_META_ANALYSIS_STREAM = [
    {"step": "supervisor", "status": "active", "detail": "Routing through the agent harness..."},
    {"step": "supervisor:dispatch", "status": "active",
     "detail": "Classified query as route='XNETWORK' -> sub-agent='Meta-Analysis'",
     "route": "XNETWORK", "sub_agent": "Meta-Analysis", "nested": False},
    {"step": "supervisor:dispatch", "status": "active",
     "detail": "Classified query as route='XAGG' -> sub-agent='Large-Scale Aggregate'",
     "route": "XAGG", "sub_agent": "Large-Scale Aggregate", "nested": True},
    {"step": "supervisor:dispatch", "status": "active",
     "detail": "Classified query as route='XAGG' -> sub-agent='Large-Scale Aggregate'",
     "route": "XAGG", "sub_agent": "Large-Scale Aggregate", "nested": True},
    {"step": "supervisor:dispatch", "status": "active",
     "detail": "Classified query as route='XAGG' -> sub-agent='Large-Scale Aggregate'",
     "route": "XAGG", "sub_agent": "Large-Scale Aggregate", "nested": True},
    {"step": "response", "status": "done",
     "answer": "No - not identically. 64/26 has a matching walk-in complaint; 65/26 has none."},
]


def test_module116_parse_records_the_question_route_not_the_last_subquery():
    """THE defect. Pre-fix this returns 'XAGG' — the last sub-dispatch."""
    parsed = _load_gold32_run().parse(_sse(_META_ANALYSIS_STREAM))
    assert parsed["route"] == "XNETWORK"
    assert parsed["sub_agent"] == "Meta-Analysis"


def test_module116_parse_keeps_the_subquery_routes_separately():
    """The sub-query routes are real data — recorded, just not as *the* route."""
    parsed = _load_gold32_run().parse(_sse(_META_ANALYSIS_STREAM))
    assert parsed["subquery_routes"] == ["XAGG", "XAGG", "XAGG"]


def test_module116_parse_is_unaffected_for_a_single_dispatch_question():
    """The 28 gold questions that never decompose must record exactly as before."""
    parsed = _load_gold32_run().parse(_sse([
        {"step": "supervisor:dispatch", "status": "active",
         "detail": "Classified query as route='XAGG' -> sub-agent='Large-Scale Aggregate'",
         "route": "XAGG", "sub_agent": "Large-Scale Aggregate", "nested": False},
        {"step": "response", "status": "done", "answer": "73 FIRs are registered."},
    ]))
    assert parsed["route"] == "XAGG"
    assert parsed["subquery_routes"] == []


def test_module116_parse_falls_back_to_the_first_detail_match_on_a_legacy_stream():
    """A backend older than this change emits no machine-readable fields. The
    regex fallback must then take the FIRST dispatch (the question), not the
    last (a sub-query) — the old behaviour, corrected."""
    legacy = [
        {k: v for k, v in e.items() if k not in ("route", "sub_agent", "nested")}
        for e in _META_ANALYSIS_STREAM
    ]
    parsed = _load_gold32_run().parse(_sse(legacy))
    assert parsed["route"] == "XNETWORK"


def test_module116_cutover_forwards_pipeline_event_extras_to_the_sse_dict():
    """`PipelineEvent` is extra='allow'; cutover.py used to drop every extra."""
    from src.pipeline.harness.types import PipelineEvent

    evt = PipelineEvent(
        step="supervisor:dispatch", status="active", detail="d",
        route="XNETWORK", sub_agent="Meta-Analysis", nested=False,
    )
    assert (evt.model_extra or {}) == {
        "route": "XNETWORK", "sub_agent": "Meta-Analysis", "nested": False,
    }

    # Read the adapter itself rather than re-implementing its loop here: the
    # thing under test is that cutover.py stops dropping extras.
    src = (ROOT / "src" / "pipeline" / "harness" / "cutover.py").read_text(encoding="utf-8")
    anchor = 'out: dict = {"step": evt.step, "status": evt.status, "detail": evt.detail}'
    assert anchor in src
    block = src[src.index(anchor): src.index(anchor) + 1400]
    assert "evt.model_extra" in block
    # …and that it cannot overwrite a core field it has already set.
    assert "if _k not in out:" in block


def test_module116_supervisor_dispatch_event_carries_route_and_nesting():
    """Reads the emit site itself: the three fields must be attached to the
    dispatch PipelineEvent, and `nested` must be derived from
    `allow_meta_analysis` (meta_analysis.py is its only False caller) rather
    than from a second, drifting parameter."""
    src = (ROOT / "src" / "pipeline" / "harness" / "supervisor.py").read_text(encoding="utf-8")
    marker = 'f"Classified query as route={route_result.get(\'route\')!r} "'
    assert marker in src
    tail = src[src.index(marker): src.index(marker) + 500]
    assert 'route=route_result.get("route")' in tail
    assert "sub_agent=sub_agent_name" in tail
    assert "nested=not allow_meta_analysis" in tail


@pytest.mark.parametrize("qid", ["CR3", "G1", "G6"])
def test_module116_the_three_questions_still_have_no_deterministic_override(qid):
    """Module 116 ships NO router change: these three are decided by the LLM
    classifier before and after, and are measured stable there (11/11 XNETWORK
    each). If a later module adds a gold-worded regex for them, this fails and
    the result file's reasoning has to be revisited rather than quietly lost."""
    from src.pipeline.router import _deterministic_route_override

    gold = {g["id"]: g for g in json.loads(
        (ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json").read_text(encoding="utf-8"))}
    assert _deterministic_route_override(gold[qid]["question"]) is None


def test_module116_recorded_route_baseline_matches_the_measured_top_level_routes():
    """The corrected baseline is the artefact this module exists to leave
    behind — pinned so it cannot silently drift back to the sub-query routes."""
    baseline = json.loads((ROOT / "evaluation" / "gold32_route_baseline.json").read_text(encoding="utf-8"))
    routes = baseline["routes"]
    assert len(routes) == 32
    assert routes["CR3"] == routes["G1"] == routes["G6"] == "XNETWORK"
    assert routes["KB3"] == "RAG" and routes["M4"] == "XAGG"
