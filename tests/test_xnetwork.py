"""
Tests for src/pipeline/xnetwork.py (GraphRAG-inspired layer, Section 2 —
cross-case open-ended network/theme queries).

community_vector_store.query_similar_communities is faked — no real Chroma
(matches the `no_network` guard, conftest, autouse).
"""
import pytest

import src.pipeline.xnetwork as xnetwork


class FakeGateway:
    def __init__(self):
        self.audit_log = []

    async def log_audit_event(self, **kwargs):
        self.audit_log.append(kwargs)


def _report(community_id, case_ids, summary="A summary."):
    return {
        "community_id": community_id, "summary_text": summary,
        "case_ids": case_ids, "member_count": len(case_ids), "distance": 0.1,
    }


async def test_investigator_denied_cross_case_network_query():
    with pytest.raises(PermissionError):
        await xnetwork.run_network_query("what's the overall picture", FakeGateway(), user_role="investigator")


async def test_supervisor_gets_results_unfiltered_by_default(monkeypatch):
    reports = [_report("COMM-1", ["CASE-A"]), _report("COMM-2", ["CASE-B"])]

    async def fake_query_similar_communities(query, top_k=5):
        return reports

    monkeypatch.setattr(xnetwork, "query_similar_communities", fake_query_similar_communities)

    result = await xnetwork.run_network_query("overall picture across cases", FakeGateway(), user_role="supervisor")

    assert result["kind"] == "network_synthesis"
    assert {r["community_id"] for r in result["results"]} == {"COMM-1", "COMM-2"}
    assert sorted(result["case_ids"]) == ["CASE-A", "CASE-B"]


# ── Milestone E1: jurisdiction-narrowed candidate set ───────────────────────

async def test_jurisdiction_case_ids_filters_out_communities_with_no_overlap(monkeypatch):
    reports = [
        _report("COMM-1", ["CASE-A", "CASE-C"]),  # overlaps the jurisdiction allow-list
        _report("COMM-2", ["CASE-B"]),             # no overlap — must be dropped
    ]

    async def fake_query_similar_communities(query, top_k=5):
        return reports

    monkeypatch.setattr(xnetwork, "query_similar_communities", fake_query_similar_communities)

    result = await xnetwork.run_network_query(
        "overall picture", FakeGateway(), user_role="supervisor", jurisdiction_case_ids=["CASE-A"],
    )

    assert [r["community_id"] for r in result["results"]] == ["COMM-1"]
    # case_ids reflects every case_id of the SURVIVING communities (COMM-1
    # here) — not further narrowed to the allow-list itself, since CASE-C
    # is a real case this surviving, in-jurisdiction community also touches.
    assert result["case_ids"] == ["CASE-A", "CASE-C"]


async def test_jurisdiction_case_ids_none_leaves_results_unfiltered(monkeypatch):
    """The default (None) must reproduce the exact pre-E1 behavior — no post-filter applied at all."""
    reports = [_report("COMM-1", ["CASE-A"]), _report("COMM-2", ["CASE-B"])]

    async def fake_query_similar_communities(query, top_k=5):
        return reports

    monkeypatch.setattr(xnetwork, "query_similar_communities", fake_query_similar_communities)

    result = await xnetwork.run_network_query("overall picture", FakeGateway(), user_role="supervisor")

    assert len(result["results"]) == 2
