"""
Analytics aggregation — the maths behind every dashboard chart.

These are pure functions over plain dicts, which is exactly why the aggregation
lives outside the gateways: one implementation, tested once, and the two
database backends cannot disagree about what "p95" means.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.observability import analytics


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ── Percentiles ───────────────────────────────────────────────────────────────

def test_percentiles_of_a_known_distribution():
    stats = analytics.percentiles([100, 200, 300, 400, 500])

    assert stats["count"] == 5
    assert stats["avg_ms"] == 300
    assert stats["p50_ms"] == 300
    assert stats["max_ms"] == 500


def test_p95_reports_a_value_someone_actually_experienced():
    """
    Nearest-rank, not interpolated: with 20 samples the p95 must be a real
    observation (the 19th), not an average of two neighbours that nobody saw.
    """
    stats = analytics.percentiles(list(range(1, 21)))  # 1..20

    assert stats["p95_ms"] in (19, 20)
    assert stats["max_ms"] == 20


def test_percentiles_of_nothing_are_zero_not_a_crash():
    assert analytics.percentiles([])["count"] == 0


def test_percentiles_ignore_missing_durations():
    stats = analytics.percentiles([100, None, 300, -5])
    assert stats["count"] == 2


# ── Usage ─────────────────────────────────────────────────────────────────────

def test_usage_counts_requests_per_bucket_and_route():
    runs = [
        {"created_at": _iso(1), "routed_to": "RAG"},
        {"created_at": _iso(2), "routed_to": "RAG"},
        {"created_at": _iso(3), "routed_to": "SQL"},
    ]

    series = analytics.usage_timeseries(runs, days=1, granularity="day")
    today = [p for p in series if p["total"] > 0]

    assert sum(p["total"] for p in today) == 3
    assert sum(p.get("RAG", 0) for p in today) == 2
    assert sum(p.get("SQL", 0) for p in today) == 1


def test_quiet_periods_plot_as_zero_rather_than_vanishing():
    """
    A gap in a line chart reads as "no data", which is a different claim from
    "nothing happened". Every bucket in the window must exist.
    """
    series = analytics.usage_timeseries([], days=7, granularity="day")

    assert len(series) >= 7
    assert all(point["total"] == 0 for point in series)


# ── Routing ───────────────────────────────────────────────────────────────────

def test_routing_breakdown_computes_share_and_success():
    runs = [
        {"routed_to": "RAG", "final_outcome": "rag", "total_duration_ms": 1000},
        {"routed_to": "RAG", "final_outcome": "rag", "total_duration_ms": 3000},
        {"routed_to": "RAG", "final_outcome": "safe", "total_duration_ms": 2000},
        {"routed_to": "SQL", "final_outcome": "sql", "total_duration_ms": 500},
    ]

    breakdown = {r["route"]: r for r in analytics.routing_breakdown(runs)}

    assert breakdown["RAG"]["count"] == 3
    assert breakdown["RAG"]["share_pct"] == 75.0
    # "safe" is the fallback answer: a completed run, but not a win.
    assert breakdown["RAG"]["success_pct"] == pytest.approx(66.7, abs=0.1)
    assert breakdown["SQL"]["success_pct"] == 100.0


def test_routing_handles_runs_that_never_recorded_a_route():
    breakdown = analytics.routing_breakdown([{"routed_to": None, "final_outcome": "rag"}])
    assert breakdown[0]["route"] == "UNKNOWN"


# ── Latency ───────────────────────────────────────────────────────────────────

def test_latency_by_step_ranks_the_slowest_first():
    """This is the chart that answers *why* we're slow; ordering is the point."""
    steps = [
        {"step_name": "retrieval", "duration_ms": 3000, "status": "success"},
        {"step_name": "retrieval", "duration_ms": 5000, "status": "success"},
        {"step_name": "router", "duration_ms": 200, "status": "success"},
        {"step_name": "evaluator", "duration_ms": 900, "status": "failed"},
    ]

    by_step = analytics.latency_by_step(steps)

    assert by_step[0]["step_name"] == "retrieval"
    assert by_step[0]["avg_ms"] == 4000
    assert {s["step_name"]: s["failures"] for s in by_step}["evaluator"] == 1


def test_latency_timeseries_reports_avg_and_p95_per_bucket():
    runs = [
        {"created_at": _iso(1), "total_duration_ms": 1000},
        {"created_at": _iso(1), "total_duration_ms": 5000},
    ]

    series = analytics.latency_timeseries(runs, days=1, granularity="day")
    populated = [p for p in series if p["count"] > 0]

    assert populated
    assert populated[0]["avg_ms"] == 3000
    assert populated[0]["p95_ms"] >= 1000


# ── Errors ────────────────────────────────────────────────────────────────────

def test_error_trend_splits_by_severity():
    errors = [
        {"occurred_at": _iso(1), "severity": "error"},
        {"occurred_at": _iso(2), "severity": "error"},
        {"occurred_at": _iso(3), "severity": "critical"},
    ]

    series = analytics.error_timeseries(errors, days=1, granularity="day")

    assert sum(p["total"] for p in series) == 3
    assert sum(p["error"] for p in series) == 2
    assert sum(p["critical"] for p in series) == 1


def test_malformed_timestamps_are_skipped_not_fatal():
    series = analytics.error_timeseries(
        [{"occurred_at": "not-a-date", "severity": "error"}], days=1,
    )
    assert sum(p["total"] for p in series) == 0
