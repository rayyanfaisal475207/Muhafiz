"""
Tests for src/graph/community_detection.py's get_staleness() (Milestone
E3 — GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md) and its two consumers:
src/ingestion/community_refresh_bg.py (automatic, staleness-gated) and
scripts/check_community_staleness.py (manual CLI, now a thin wrapper).

No real Postgres/AGE — get_latest_run()/_current_raw_node_count()/
_current_raw_edge_count() are monkeypatched (matches the `no_network`
guard, conftest, autouse).
"""
import pytest

import src.graph.community_detection as community_detection
import src.ingestion.community_refresh_bg as community_refresh_bg


def _patch_counts(monkeypatch, current_nodes, current_edges):
    async def fake_nodes():
        return current_nodes

    async def fake_edges():
        return current_edges

    monkeypatch.setattr(community_detection, "_current_raw_node_count", fake_nodes)
    monkeypatch.setattr(community_detection, "_current_raw_edge_count", fake_edges)


def _patch_latest_run(monkeypatch, run):
    async def fake_get_latest_run():
        return run

    monkeypatch.setattr(community_detection, "get_latest_run", fake_get_latest_run)


# ── get_staleness() ──────────────────────────────────────────────────────────

async def test_no_prior_run_is_stale(monkeypatch):
    _patch_latest_run(monkeypatch, None)
    _patch_counts(monkeypatch, 100, 50)

    result = await community_detection.get_staleness()

    assert result["stale"] is True
    assert result["last_run_id"] is None


async def test_prior_run_missing_raw_counts_is_stale(monkeypatch):
    """A run from before migration 017 has no raw_node_count/raw_edge_count to compare against."""
    _patch_latest_run(monkeypatch, {"run_id": "RUN-1", "raw_node_count": None, "raw_edge_count": None})
    _patch_counts(monkeypatch, 100, 50)

    result = await community_detection.get_staleness()

    assert result["stale"] is True
    assert result["last_run_id"] == "RUN-1"
    assert result["node_drift"] is None


async def test_within_threshold_is_not_stale(monkeypatch):
    _patch_latest_run(monkeypatch, {"run_id": "RUN-1", "raw_node_count": 100, "raw_edge_count": 50})
    _patch_counts(monkeypatch, 105, 51)  # 5% node drift, 2% edge drift — both under 10%

    result = await community_detection.get_staleness()

    assert result["stale"] is False
    assert result["node_drift"] == pytest.approx(0.05)


async def test_node_drift_past_threshold_is_stale(monkeypatch):
    _patch_latest_run(monkeypatch, {"run_id": "RUN-1", "raw_node_count": 100, "raw_edge_count": 50})
    _patch_counts(monkeypatch, 120, 50)  # 20% node drift — past the 10% threshold

    result = await community_detection.get_staleness()

    assert result["stale"] is True
    assert result["node_drift"] == pytest.approx(0.20)
    assert result["edge_drift"] == pytest.approx(0.0)


async def test_edge_drift_past_threshold_is_stale(monkeypatch):
    _patch_latest_run(monkeypatch, {"run_id": "RUN-1", "raw_node_count": 100, "raw_edge_count": 50})
    _patch_counts(monkeypatch, 100, 60)  # 20% edge drift

    result = await community_detection.get_staleness()

    assert result["stale"] is True


# ── community_refresh_bg._run_community_refresh_bg() ────────────────────────

async def test_refresh_bg_skips_when_not_stale(monkeypatch):
    async def fake_get_staleness():
        return {"stale": False, "reason": "within threshold"}

    def fail_if_called(*a, **k):
        raise AssertionError("must not run detect_communities() when not stale")

    monkeypatch.setattr(community_detection, "get_staleness", fake_get_staleness)
    monkeypatch.setattr(community_detection, "detect_communities", fail_if_called)

    await community_refresh_bg._run_community_refresh_bg()  # must not raise


async def test_refresh_bg_runs_detect_and_summarize_when_stale(monkeypatch):
    calls = []

    async def fake_get_staleness():
        return {"stale": True, "reason": "node drift 20.0%, edge drift 0.0%"}

    async def fake_detect_communities():
        calls.append("detect")
        return {"run_id": "RUN-2"}

    monkeypatch.setattr(community_detection, "get_staleness", fake_get_staleness)
    monkeypatch.setattr(community_detection, "detect_communities", fake_detect_communities)

    import src.graph.community_summarization as community_summarization

    async def fake_summarize_communities():
        calls.append("summarize")
        return {"attempted": 3, "written": 3, "skipped": 0}

    monkeypatch.setattr(community_summarization, "summarize_communities", fake_summarize_communities)

    await community_refresh_bg._run_community_refresh_bg()

    assert calls == ["detect", "summarize"]


async def test_refresh_bg_failure_is_best_effort_never_raises(monkeypatch):
    async def fake_get_staleness():
        raise RuntimeError("simulated graph error")

    monkeypatch.setattr(community_detection, "get_staleness", fake_get_staleness)

    await community_refresh_bg._run_community_refresh_bg()  # must not raise
