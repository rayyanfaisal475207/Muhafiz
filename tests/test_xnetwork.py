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


# ── Module 12 — RC-1 relevance gate ─────────────────────────────────────────
# Threshold picked from measured cosine-distance evidence over the live
# community-report collection (see xnetwork.py's own comment above
# RELEVANCE_DISTANCE_THRESHOLD for the full distribution this was derived
# from) — these tests exercise the gate's mechanics, not re-derive the
# cutoff.

async def test_genuinely_relevant_cluster_is_unaffected_by_the_gate(monkeypatch):
    """A query with a genuinely close community (well under the cutoff) behaves
    exactly as before Module 12 — nothing filtered, no refusal reason set."""
    reports = [_report("COMM-1", ["CASE-A"])]
    reports[0]["distance"] = 0.03  # near-verbatim match, e.g. a named-entity ask

    async def fake_query_similar_communities(query, top_k=5):
        return reports

    monkeypatch.setattr(xnetwork, "query_similar_communities", fake_query_similar_communities)

    result = await xnetwork.run_network_query(
        "What cases involve Hammad Aslam?", FakeGateway(), user_role="supervisor",
    )

    assert [r["community_id"] for r in result["results"]] == ["COMM-1"]
    assert result["no_relevant_reason"] is None


async def test_no_relevant_cluster_is_filtered_with_an_honest_reason(monkeypatch):
    """Nearest communities all sit at/above the cutoff — a broad/evaluative
    query like the RC-1 gold questions (G1, CR3, ...) with nothing on-topic
    in the corpus. `results` comes back empty and a specific, non-generic
    reason is set — never a recited cluster dump."""
    reports = [
        _report("COMM-1", ["CASE-A"]),
        _report("COMM-2", ["CASE-B"]),
    ]
    reports[0]["distance"] = 0.20
    reports[1]["distance"] = 0.21

    async def fake_query_similar_communities(query, top_k=5):
        return reports

    monkeypatch.setattr(xnetwork, "query_similar_communities", fake_query_similar_communities)

    result = await xnetwork.run_network_query(
        "Acting as a crime analyst, flag anything unusual in our caseload.",
        FakeGateway(), user_role="supervisor",
    )

    assert result["results"] == []
    assert result["community_ids"] == []
    assert result["case_ids"] == []
    assert result["no_relevant_reason"] is not None
    # Honest and specific, not a generic "no information available" (RC-6's
    # failure mode) and not silent.
    assert "0.20" in result["no_relevant_reason"] or "0.200" in result["no_relevant_reason"]
    # Names the actual question asked (traceable, not a canned line)...
    assert "caseload" in result["no_relevant_reason"].lower()
    # ...but never recites the filtered-out cluster summaries as content.
    assert "COMM-1" not in result["no_relevant_reason"] and "COMM-2" not in result["no_relevant_reason"]
    assert "not" in result["no_relevant_reason"].lower() or "no " in result["no_relevant_reason"].lower()


async def test_boundary_at_the_cutoff_is_treated_as_relevant(monkeypatch):
    """Exactly at RELEVANCE_DISTANCE_THRESHOLD clears the gate (<=, not <) —
    a deliberate boundary choice tested explicitly rather than left implicit."""
    reports = [_report("COMM-1", ["CASE-A"])]
    reports[0]["distance"] = xnetwork.RELEVANCE_DISTANCE_THRESHOLD

    async def fake_query_similar_communities(query, top_k=5):
        return reports

    monkeypatch.setattr(xnetwork, "query_similar_communities", fake_query_similar_communities)

    result = await xnetwork.run_network_query("boundary query", FakeGateway(), user_role="supervisor")

    assert [r["community_id"] for r in result["results"]] == ["COMM-1"]
    assert result["no_relevant_reason"] is None


async def test_boundary_just_past_the_cutoff_is_filtered(monkeypatch):
    reports = [_report("COMM-1", ["CASE-A"])]
    reports[0]["distance"] = xnetwork.RELEVANCE_DISTANCE_THRESHOLD + 0.001

    async def fake_query_similar_communities(query, top_k=5):
        return reports

    monkeypatch.setattr(xnetwork, "query_similar_communities", fake_query_similar_communities)

    result = await xnetwork.run_network_query("boundary query", FakeGateway(), user_role="supervisor")

    assert result["results"] == []
    assert result["no_relevant_reason"] is not None


async def test_relevance_gate_applies_after_jurisdiction_filter(monkeypatch):
    """The two post-filters compose: jurisdiction narrows the candidate set
    first, then relevance is judged only among the survivors, and the
    'nearest' reported in the refusal reason is the nearest AMONG the
    jurisdiction-narrowed set, not a globally nearer out-of-jurisdiction one."""
    reports = [
        _report("COMM-1", ["CASE-A"]),  # in jurisdiction, but far
        _report("COMM-2", ["CASE-B"]),  # out of jurisdiction, and close — must not leak in
    ]
    reports[0]["distance"] = 0.25
    reports[1]["distance"] = 0.01

    async def fake_query_similar_communities(query, top_k=5):
        return reports

    monkeypatch.setattr(xnetwork, "query_similar_communities", fake_query_similar_communities)

    result = await xnetwork.run_network_query(
        "overall picture", FakeGateway(), user_role="supervisor", jurisdiction_case_ids=["CASE-A"],
    )

    assert result["results"] == []
    assert result["no_relevant_reason"] is not None
    assert "0.25" in result["no_relevant_reason"]
