"""
Tests for the shadow-comparison reporting tool.

Same reasoning as `test_evaluate_harness.py`: a reporting tool that quietly
starts flattering the system is worse than no tool. The property that matters
most here is that an ERROR — a shadow run that would have failed a real user —
can never be summarised away.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts", "compare_shadow_runs.py",
)


def _load():
    if "compare_shadow_runs" in sys.modules:
        return sys.modules["compare_shadow_runs"]
    spec = importlib.util.spec_from_file_location("compare_shadow_runs", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["compare_shadow_runs"] = module
    spec.loader.exec_module(module)
    return module


cmp = _load()


def _row(**over):
    base = {
        "shadow_id": "s1", "run_id": None, "session_id": "sess",
        "case_id": "CASE-1", "original_query": "q",
        "legacy_route": "RAG", "legacy_outcome": "done",
        "harness_sub_agent": "semantic_search", "routing_basis": "route=RAG",
        "harness_status": "ok", "harness_answer": "an answer [Document 1]",
        "citation_count": 1, "tools_used": ["RAG"], "degraded_from": [],
        "caveats": [], "routes_agree": True, "duration_ms": 1500,
        "error": None, "sampled_reason": "sampled at 5%",
        "created_at": "2026-08-10T00:00:00",
    }
    base.update(over)
    return base


def test_an_empty_log_explains_itself(capsys):
    """
    "No rows" has several causes and an operator should not have to guess which.
    """
    cmp.print_summary([])
    out = capsys.readouterr().out
    assert "No shadow runs recorded" in out
    assert "HARNESS_SHADOW_MODE" in out
    assert "HARNESS_SHADOW_SAMPLE_RATE" in out


def test_errors_are_reported_as_blocking(capsys):
    """
    A shadow run that raised means this query shape would have FAILED for a real
    user. It must never be summarised into a clean report.
    """
    cmp.print_summary([
        _row(), _row(error="RuntimeError: boom", harness_status=None),
    ])
    out = capsys.readouterr().out
    assert "BLOCKER" in out
    assert "errored" in out


def test_disagreements_are_surfaced_when_there_are_no_errors(capsys):
    cmp.print_summary([_row(), _row(routes_agree=False, harness_status="abstained")])
    out = capsys.readouterr().out
    assert "BLOCKER" not in out
    assert "disagreement" in out


def test_a_clean_sample_still_warns_against_over_reading_it(capsys):
    """
    Agreeing on WHETHER to answer says nothing about whether the answer is good.
    A clean run must not be presented as proof of quality.
    """
    cmp.print_summary([_row(), _row()])
    out = capsys.readouterr().out
    assert "no outcome disagreements" in out.lower()
    assert "--verbose" in out


def test_abstentions_are_labelled_not_just_counted(capsys):
    """
    An operator reading "abstained: 40%" needs to know that is the harness
    declining on purpose, not failing.
    """
    cmp.print_summary([_row(harness_status="abstained", routes_agree=False)])
    out = capsys.readouterr().out
    assert "declined rather than serve unverified prose" in out


def test_partial_is_labelled_as_an_answer_with_a_gap(capsys):
    cmp.print_summary([_row(harness_status="partial")])
    out = capsys.readouterr().out
    assert "answered, with a stated gap" in out


def test_routing_disagreements_are_visible(capsys):
    """
    The legacy route -> harness sub-agent table is how a reader sees that the
    two paths classified the same question differently.
    """
    cmp.print_summary([
        _row(legacy_route="RAG", harness_sub_agent="semantic_search"),
        _row(legacy_route="RAG", harness_sub_agent="case_summary"),
    ])
    out = capsys.readouterr().out
    assert "Legacy route -> harness sub-agent" in out
    assert "semantic_search" in out
    assert "case_summary" in out


def test_latency_reports_p95_not_only_the_median(capsys):
    """
    A median hides the tail, and the tail is what a user would feel.
    """
    rows = [_row(duration_ms=1000) for _ in range(19)] + [_row(duration_ms=40000)]
    cmp.print_summary(rows)
    out = capsys.readouterr().out
    assert "p95 shadow latency" in out
    assert "40.0s" in out


def test_degraded_tools_are_summarised(capsys):
    cmp.print_summary([_row(degraded_from=["GRAPH"]), _row(degraded_from=["GRAPH"])])
    out = capsys.readouterr().out
    assert "did not contribute" in out
    assert "GRAPH" in out


def test_row_detail_marks_errors_and_disagreements(capsys):
    cmp.print_rows(
        [_row(error="boom"), _row(routes_agree=False), _row()], verbose=False,
    )
    out = capsys.readouterr().out
    assert "[ERROR]" in out
    assert "[DISAGREE]" in out
    assert "[ok]" in out


def test_verbose_prints_the_harness_answer(capsys):
    cmp.print_rows([_row(harness_answer="the full answer text")], verbose=True)
    out = capsys.readouterr().out
    assert "the full answer text" in out


def test_non_verbose_does_not_print_the_answer(capsys):
    """The summary view must stay scannable; answers are opt-in."""
    cmp.print_rows([_row(harness_answer="SHOULD_NOT_APPEAR")], verbose=False)
    assert "SHOULD_NOT_APPEAR" not in capsys.readouterr().out
