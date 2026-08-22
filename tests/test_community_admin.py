"""
Tests for src/api/community_admin.py (Milestone E3 — manual full-sweep
endpoint, GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md). Route functions called
directly, `admin` passed as a plain object — same style as
tests/test_graph_review.py.
"""
import src.api.community_admin as community_admin


class _Admin:
    def __init__(self, user_id="supervisor-1"):
        self.id = user_id


async def test_staleness_endpoint_returns_get_staleness_verbatim(monkeypatch):
    async def fake_get_staleness():
        return {"stale": True, "reason": "node drift 20.0%, edge drift 0.0%", "last_run_id": "RUN-1"}

    monkeypatch.setattr(community_admin.community_detection, "get_staleness", fake_get_staleness)

    result = await community_admin.staleness(admin=_Admin())

    assert result["stale"] is True
    assert result["last_run_id"] == "RUN-1"


async def test_refresh_endpoint_runs_detect_and_summarize_unconditionally(monkeypatch):
    """Unlike the automatic background trigger, the manual endpoint always
    runs both steps regardless of the staleness check."""
    calls = []

    async def fake_detect_communities():
        calls.append("detect")
        return {"run_id": "RUN-2", "node_count": 10, "edge_count": 5, "community_count": 2}

    monkeypatch.setattr(community_admin.community_detection, "detect_communities", fake_detect_communities)

    import src.graph.community_summarization as community_summarization

    async def fake_summarize_communities():
        calls.append("summarize")
        return {"attempted": 2, "written": 2, "skipped": 0}

    monkeypatch.setattr(community_summarization, "summarize_communities", fake_summarize_communities)

    result = await community_admin.refresh(admin=_Admin())

    assert calls == ["detect", "summarize"]
    assert result["run_id"] == "RUN-2"
    assert result["written"] == 2
