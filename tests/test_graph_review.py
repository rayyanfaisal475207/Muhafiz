"""
Tests for src/api/graph_review.py (Phase 4.11 review queue).

age_client and versioning are monkeypatched with fakes — no real
Postgres/AGE (matches the `no_network` guard, conftest, autouse). The
full flow (list pending, confirm, the double-confirm 409, stats) was
verified live against a real AGE-enabled Postgres instance during
development, including the append-only-history bug this test suite
guards against (a double-confirm silently creating two "confirmed"
edges — fixed in versioning.py's write_edge and the pre-check here).
`admin` dependencies are passed as None directly — these tests call the
route functions, not through FastAPI's dependency-injected HTTP layer.
"""
import pytest
from fastapi import HTTPException

import src.api.graph_review as graph_review


class FakeAgeClient:
    def __init__(self):
        self.calls: list[dict] = []
        self.responses: list[list[dict]] = []

    def queue(self, response):
        self.responses.append(response)

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.calls.append({"cypher": cypher_query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return []


class FakeVersioning:
    def __init__(self):
        self.edges_written: list[dict] = []
        self.next_id = 100

    async def write_edge(self, edge_label, from_label, from_match, to_label, to_match,
                          properties=None, *, source_doc_id, source_chunk_id=None,
                          confidence=1.0, supersedes_edge_id=None):
        self.next_id += 1
        record = {
            "edge_label": edge_label, "from_match": from_match, "to_match": to_match,
            "properties": properties or {}, "supersedes_edge_id": supersedes_edge_id,
            "id": self.next_id,
        }
        self.edges_written.append(record)
        return {"id": self.next_id, "label": edge_label, "properties": properties or {}}


@pytest.fixture
def fake_age(monkeypatch):
    client = FakeAgeClient()
    monkeypatch.setattr(graph_review, "age_client", client)
    return client


@pytest.fixture
def fake_versioning(monkeypatch):
    v = FakeVersioning()
    monkeypatch.setattr(graph_review, "versioning", v)
    return v


class FakeGateway:
    def __init__(self):
        self.audit_log: list[dict] = []

    async def log_audit_event(self, event_type, details=None, user_id=None, case_id=None):
        self.audit_log.append({
            "event_type": event_type, "details": details, "user_id": user_id, "case_id": case_id,
        })


@pytest.fixture
def fake_gateway(monkeypatch):
    gw = FakeGateway()

    async def _get_gateway():
        return gw

    monkeypatch.setattr(graph_review, "get_gateway", _get_gateway)
    return gw


class _Admin:
    """Phase 5, Module 5.2: reviewed_by/audit-log identity now comes from
    the authenticated admin dependency, not a client-supplied field."""
    def __init__(self, user_id="investigator-1"):
        self.id = user_id


def _person(entity_id, name):
    return {"id": 1, "label": "Person", "properties": {"entity_id": entity_id, "canonical_name": name}}


def _same_as_edge(edge_id, tier="flagged_unverified", status="pending", superseded_by=None):
    return {
        "id": edge_id, "label": "SAME_AS",
        "properties": {
            "tier": tier, "status": status, "basis": "matched on near-identical name",
            "confidence": 0.85, "as_of": "2026-01-01T00:00:00+00:00",
            "source_doc_id": "DOC-1", "source_chunk_id": None,
            "superseded_by": superseded_by,
        },
    }


# ── list_pending ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_pending_shape(fake_age):
    fake_age.queue([{
        "a": _person("P-NEW", "عدنان قریشی وحید"),
        "r": _same_as_edge(1),
        "b": _person("P-006", "عدنان قریشی وحید"),
    }])

    result = await graph_review.list_pending(case_id=None, tier=None, admin=None)
    assert result["count"] == 1
    entry = result["pending"][0]
    assert entry["edge_id"] == 1
    assert entry["tier"] == "flagged_unverified"
    assert entry["mention"]["entity_id"] == "P-NEW"
    assert entry["candidate"]["entity_id"] == "P-006"
    assert "basis" in entry


@pytest.mark.asyncio
async def test_list_pending_case_id_filter(fake_age):
    """
    Regression: case_id used to be silently ignored (the endpoint's own
    docstring said so). Passing it now actually narrows the queue to
    matches touching that case, without making case-scoping the default
    (case_id=None still returns everything, including cross-case matches
    — see the None-branch test above).
    """
    fake_age.responses = [[
        {"a": _person("P-1", "X"), "r": _same_as_edge(1), "b": _person("P-2", "X"),
         "a_case_id": "CASE-001", "b_case_id": "CASE-001"},
        {"a": _person("P-3", "Y"), "r": _same_as_edge(2), "b": _person("P-4", "Y"),
         "a_case_id": "CASE-001", "b_case_id": "CASE-002"},  # cross-case match
        {"a": _person("P-5", "Z"), "r": _same_as_edge(3), "b": _person("P-6", "Z"),
         "a_case_id": "CASE-003", "b_case_id": "CASE-003"},
    ]]

    result = await graph_review.list_pending(case_id="CASE-001", tier=None, admin=None)

    assert result["count"] == 2
    assert {e["edge_id"] for e in result["pending"]} == {1, 2}


@pytest.mark.asyncio
async def test_list_pending_no_case_id_returns_everything_including_cross_case(fake_age):
    fake_age.responses = [[
        {"a": _person("P-1", "X"), "r": _same_as_edge(1), "b": _person("P-2", "X"),
         "a_case_id": "CASE-001", "b_case_id": "CASE-002"},
    ]]

    result = await graph_review.list_pending(case_id=None, tier=None, admin=None)

    assert result["count"] == 1


@pytest.mark.asyncio
async def test_list_pending_tier_filter(fake_age):
    # execute_cypher returns a single list of rows, not per-row queues.
    fake_age.responses = [[
        {"a": _person("P-1", "X"), "r": _same_as_edge(1, tier="flagged_unverified"), "b": _person("P-2", "X")},
        {"a": _person("P-3", "Y"), "r": _same_as_edge(2, tier="human_review"), "b": _person("P-4", "Y")},
    ]]

    result = await graph_review.list_pending(case_id=None, tier="human_review", admin=None)
    assert result["count"] == 1
    assert result["pending"][0]["edge_id"] == 2


# ── confirm / reject ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_match_writes_superseding_confirmed_edge(fake_age, fake_versioning, fake_gateway):
    fake_age.queue([{"a": _person("P-NEW", "X"), "r": _same_as_edge(1), "b": _person("P-EXISTING", "X")}])

    result = await graph_review.confirm_match(1, graph_review.ReviewAction(), admin=_Admin("inv1"))

    assert result["status"] == "confirmed"
    write = fake_versioning.edges_written[0]
    assert write["edge_label"] == "SAME_AS"
    assert write["properties"]["status"] == "confirmed"
    assert write["properties"]["reviewed_by"] == "inv1"
    assert write["supersedes_edge_id"] == 1


@pytest.mark.asyncio
async def test_confirm_match_ignores_client_supplied_reviewed_by(fake_age, fake_versioning, fake_gateway):
    """
    Regression: reviewed_by used to be a client-supplied string on the
    request body, trivially spoofable. It must now always be the
    authenticated admin's own id, even if a caller sends an extra
    reviewed_by field in the request JSON (harmless — ignored, not
    honored).
    """
    fake_age.queue([{"a": _person("P-NEW", "X"), "r": _same_as_edge(1), "b": _person("P-EXISTING", "X")}])

    action = graph_review.ReviewAction.model_validate({"reviewed_by": "someone-else"})
    result = await graph_review.confirm_match(1, action, admin=_Admin("real-admin-id"))

    assert result["status"] == "confirmed"
    assert fake_versioning.edges_written[0]["properties"]["reviewed_by"] == "real-admin-id"


@pytest.mark.asyncio
async def test_confirm_match_writes_audit_log_entry(fake_age, fake_versioning, fake_gateway):
    fake_age.queue([{"a": _person("P-NEW", "X"), "r": _same_as_edge(1), "b": _person("P-EXISTING", "X")}])

    await graph_review.confirm_match(1, graph_review.ReviewAction(), admin=_Admin("inv1"))

    assert len(fake_gateway.audit_log) == 1
    entry = fake_gateway.audit_log[0]
    assert entry["event_type"] == "graph_review_confirm"
    assert entry["user_id"] == "inv1"
    assert entry["details"]["edge_id"] == 1


@pytest.mark.asyncio
async def test_reject_match_writes_superseding_rejected_edge(fake_age, fake_versioning, fake_gateway):
    fake_age.queue([{"a": _person("P-NEW", "X"), "r": _same_as_edge(1), "b": _person("P-EXISTING", "X")}])

    result = await graph_review.reject_match(1, graph_review.ReviewAction(), admin=_Admin("inv1"))

    assert result["status"] == "rejected"
    assert fake_versioning.edges_written[0]["properties"]["status"] == "rejected"


@pytest.mark.asyncio
async def test_reject_match_writes_audit_log_entry(fake_age, fake_versioning, fake_gateway):
    fake_age.queue([{"a": _person("P-NEW", "X"), "r": _same_as_edge(1), "b": _person("P-EXISTING", "X")}])

    await graph_review.reject_match(1, graph_review.ReviewAction(), admin=_Admin("inv1"))

    assert len(fake_gateway.audit_log) == 1
    assert fake_gateway.audit_log[0]["event_type"] == "graph_review_reject"


@pytest.mark.asyncio
async def test_double_confirm_returns_409(fake_age, fake_versioning):
    # Already superseded (by some earlier review action) — must be
    # rejected before any write is attempted.
    fake_age.queue([{"a": _person("P-NEW", "X"), "r": _same_as_edge(1, superseded_by=42), "b": _person("P-EXISTING", "X")}])

    with pytest.raises(HTTPException) as exc_info:
        await graph_review.confirm_match(1, graph_review.ReviewAction(), admin=_Admin("inv2"))
    assert exc_info.value.status_code == 409
    assert fake_versioning.edges_written == []  # no write attempted


@pytest.mark.asyncio
async def test_confirm_nonexistent_edge_returns_404(fake_age, fake_versioning):
    fake_age.queue([])  # _get_same_as_edge finds nothing

    with pytest.raises(HTTPException) as exc_info:
        await graph_review.confirm_match(999, graph_review.ReviewAction(), admin=_Admin("inv1"))
    assert exc_info.value.status_code == 404


# ── stats ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_review_stats_counts_by_tier_and_status(fake_age):
    fake_age.responses = [[
        {"r": _same_as_edge(1, tier="flagged_unverified", status="pending")},
        {"r": _same_as_edge(2, tier="flagged_unverified", status="pending")},
        {"r": _same_as_edge(3, tier="human_review", status="pending")},
    ]]

    stats = await graph_review.review_stats(admin=None)
    assert stats["flagged_unverified"]["pending"] == 2
    assert stats["human_review"]["pending"] == 1


# ── queue/history (GRAPH_QUALITY_VISIBILITY_FIX_PROMPT.md, Feature A) ────

@pytest.mark.asyncio
async def test_queue_history_defaults_to_global_scope(monkeypatch):
    captured = {}

    async def fake_read_history(case_id=None, days=30):
        captured["case_id"] = case_id
        captured["days"] = days
        return [{"snapshot_at": "2026-08-27T00:00:00+00:00", "case_id": None,
                  "tier": "flagged_unverified", "status": "pending", "edge_count": 5}]

    monkeypatch.setattr(graph_review.same_as_queue_history, "read_history", fake_read_history)

    result = await graph_review.queue_history(admin=None)

    assert captured == {"case_id": None, "days": 30}
    assert result["case_id"] is None
    assert result["days"] == 30
    assert result["snapshots"][0]["edge_count"] == 5


@pytest.mark.asyncio
async def test_queue_history_passes_through_case_id_and_days(monkeypatch):
    captured = {}

    async def fake_read_history(case_id=None, days=30):
        captured["case_id"] = case_id
        captured["days"] = days
        return []

    monkeypatch.setattr(graph_review.same_as_queue_history, "read_history", fake_read_history)

    await graph_review.queue_history(case_id="CASE-1", days=7, admin=None)

    assert captured == {"case_id": "CASE-1", "days": 7}


@pytest.mark.asyncio
async def test_queue_history_clamps_days_to_a_sane_range(monkeypatch):
    captured = {}

    async def fake_read_history(case_id=None, days=30):
        captured["days"] = days
        return []

    monkeypatch.setattr(graph_review.same_as_queue_history, "read_history", fake_read_history)

    await graph_review.queue_history(days=99999, admin=None)
    assert captured["days"] == 365

    await graph_review.queue_history(days=0, admin=None)
    assert captured["days"] == 1


# ── CITES review queue — M6b of the Muhafiz Data API migration
# (docs/decisions/0001-muhafiz-api-migration.md), a PARALLEL queue to
# SAME_AS above, keyed on case_id (Case nodes have no entity_id) ────────

def _case(case_id):
    return {"id": 1, "label": "Case", "properties": {"case_id": case_id}}


def _cites_edge(edge_id, status="pending", superseded_by=None, basis="FIR 423/26 referenced in narrative"):
    return {
        "id": edge_id, "label": "CITES",
        "properties": {
            "status": status, "basis": basis, "confidence": 0.6,
            "as_of": "2026-01-01T00:00:00+00:00", "source_doc_id": "psrms/fir/fir-424-26#structured",
            "superseded_by": superseded_by,
        },
    }


@pytest.mark.asyncio
async def test_list_pending_citations_shape(fake_age):
    fake_age.queue([{"a": _case("fir-424-26"), "r": _cites_edge(1), "b": _case("fir-423-26")}])

    result = await graph_review.list_pending_citations(admin=None)

    assert result["count"] == 1
    entry = result["pending"][0]
    assert entry["edge_id"] == 1
    assert entry["citing_case"]["case_id"] == "fir-424-26"
    assert entry["cited_case"]["case_id"] == "fir-423-26"
    assert "basis" in entry


@pytest.mark.asyncio
async def test_confirm_citation_writes_superseding_confirmed_edge(fake_age, fake_versioning, fake_gateway):
    fake_age.queue([{"a": _case("fir-424-26"), "r": _cites_edge(1), "b": _case("fir-423-26")}])

    result = await graph_review.confirm_citation(1, graph_review.ReviewAction(), admin=_Admin("inv1"))

    assert result["status"] == "confirmed"
    write = fake_versioning.edges_written[0]
    assert write["edge_label"] == "CITES"
    assert write["from_match"] == {"case_id": "fir-424-26"}
    assert write["to_match"] == {"case_id": "fir-423-26"}
    assert write["properties"]["status"] == "confirmed"
    assert write["properties"]["reviewed_by"] == "inv1"
    assert write["supersedes_edge_id"] == 1


@pytest.mark.asyncio
async def test_reject_citation_writes_superseding_rejected_edge(fake_age, fake_versioning, fake_gateway):
    fake_age.queue([{"a": _case("fir-424-26"), "r": _cites_edge(1), "b": _case("fir-423-26")}])

    result = await graph_review.reject_citation(1, graph_review.ReviewAction(), admin=_Admin("inv1"))

    assert result["status"] == "rejected"
    assert fake_versioning.edges_written[0]["properties"]["status"] == "rejected"


@pytest.mark.asyncio
async def test_confirm_citation_writes_audit_log_entry(fake_age, fake_versioning, fake_gateway):
    fake_age.queue([{"a": _case("fir-424-26"), "r": _cites_edge(1), "b": _case("fir-423-26")}])

    await graph_review.confirm_citation(1, graph_review.ReviewAction(), admin=_Admin("inv1"))

    assert len(fake_gateway.audit_log) == 1
    entry = fake_gateway.audit_log[0]
    assert entry["event_type"] == "graph_review_citation_confirmed"
    assert entry["details"]["citing_case_id"] == "fir-424-26"
    assert entry["details"]["cited_case_id"] == "fir-423-26"


@pytest.mark.asyncio
async def test_double_confirm_citation_returns_409(fake_age, fake_versioning):
    fake_age.queue([{"a": _case("fir-424-26"), "r": _cites_edge(1, superseded_by=42), "b": _case("fir-423-26")}])

    with pytest.raises(HTTPException) as exc_info:
        await graph_review.confirm_citation(1, graph_review.ReviewAction(), admin=_Admin("inv2"))
    assert exc_info.value.status_code == 409
    assert fake_versioning.edges_written == []


@pytest.mark.asyncio
async def test_confirm_nonexistent_citation_returns_404(fake_age, fake_versioning):
    fake_age.queue([])

    with pytest.raises(HTTPException) as exc_info:
        await graph_review.confirm_citation(999, graph_review.ReviewAction(), admin=_Admin("inv1"))
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_same_as_queue_and_cites_queue_are_fully_independent(fake_age):
    """A SAME_AS-shaped row must never be picked up by the CITES cypher
    query and vice versa — cheap regression guard that the two queues
    query on different edge labels, not a shared/overlapping pattern."""
    same_as_calls = [c for c in fake_age.calls if "SAME_AS" in c["cypher"]]
    cites_calls = [c for c in fake_age.calls if "CITES" in c["cypher"]]
    assert same_as_calls == [] and cites_calls == []  # nothing called yet

    fake_age.queue([])
    await graph_review.list_pending_citations(admin=None)
    assert "CITES" in fake_age.calls[-1]["cypher"]
    assert "SAME_AS" not in fake_age.calls[-1]["cypher"]


# ── /queue — Milestone D1 reordered/grouped review queue ───────────────

def _priority_row(edge_id, group_id="GROUP-SAME_AS-1", priority_score=0.8, why="reinforced", deprioritized=False):
    return {
        "edge_id": edge_id, "tier": "flagged_unverified", "a_key": f"P-A{edge_id}",
        "b_key": f"P-B{edge_id}", "original_confidence": 0.7, "original_basis": "matched on near-identical name",
        "priority_score": priority_score, "why": why, "group_id": group_id,
        "deprioritized": deprioritized, "last_scored_at": None, "created_at": None, "source_doc_id": None,
    }


@pytest.fixture
def fake_priority_rows(monkeypatch):
    rows: list[dict] = []

    async def fake_list_rows(edge_label, include_deprioritized=True):
        if not include_deprioritized:
            return [r for r in rows if not r["deprioritized"]]
        return list(rows)

    monkeypatch.setattr(graph_review.pending_candidate_priority, "list_rows", fake_list_rows)
    monkeypatch.setattr(graph_review, "_fetch_node_by_key", lambda entity_id: _fake_node(entity_id))
    return rows


async def _fake_node(entity_id):
    return {"id": 1, "label": "Person", "properties": {"entity_id": entity_id, "canonical_name": entity_id}}


@pytest.mark.asyncio
async def test_list_queue_shape(fake_priority_rows):
    fake_priority_rows.append(_priority_row(301, priority_score=0.9))
    fake_priority_rows.append(_priority_row(302, group_id="GROUP-SAME_AS-2", priority_score=0.4))

    result = await graph_review.list_queue(admin=None)

    assert result["count"] == 2
    edge_ids = {r["edge_id"] for r in result["queue"]}
    assert edge_ids == {301, 302}
    row = next(r for r in result["queue"] if r["edge_id"] == 301)
    assert row["why"] == "reinforced"
    assert row["priority_score"] == 0.9
    assert row["mention"]["entity_id"] == "P-A301"
    assert row["candidate"]["entity_id"] == "P-B301"


@pytest.mark.asyncio
async def test_list_queue_groups_clusters_by_group_id(fake_priority_rows):
    fake_priority_rows.append(_priority_row(401, group_id="GROUP-SAME_AS-1", priority_score=0.9))
    fake_priority_rows.append(_priority_row(402, group_id="GROUP-SAME_AS-1", priority_score=0.5))
    fake_priority_rows.append(_priority_row(403, group_id="GROUP-SAME_AS-9", priority_score=0.2))

    result = await graph_review.list_queue_groups(admin=None)

    assert result["count"] == 2
    g1 = next(g for g in result["groups"] if g["group_id"] == "GROUP-SAME_AS-1")
    assert g1["member_count"] == 2
    assert set(g1["edge_ids"]) == {401, 402}
    assert g1["top_priority_score"] == 0.9  # highest-scored member leads


@pytest.mark.asyncio
async def test_list_queue_never_returns_a_deprioritized_row_ahead_of_a_live_one(fake_priority_rows):
    """Reordering only sinks stale candidates — it must never delete/hide them."""
    fake_priority_rows.append(_priority_row(501, priority_score=0.95, deprioritized=True))
    fake_priority_rows.append(_priority_row(502, priority_score=0.1, deprioritized=False))

    result = await graph_review.list_queue(admin=None)
    assert {r["edge_id"] for r in result["queue"]} == {501, 502}, "deprioritize must sink, never drop, a candidate"


@pytest.mark.asyncio
async def test_reprioritize_endpoint_never_confirms_or_rejects_anything(monkeypatch, fake_versioning):
    """Milestone D1's hard rule, checked at the API boundary: the manual full-sweep endpoint must never write a SAME_AS/CITES edge."""
    async def fake_reprioritize_all():
        return 7

    monkeypatch.setattr(graph_review.candidate_reprioritization, "reprioritize_all", fake_reprioritize_all)

    result = await graph_review.reprioritize_queue(admin=None)

    assert result == {"rescored": 7}
    assert fake_versioning.edges_written == [], "reprioritization must never write a graph edge"


# ── /queue/batches — human batch confirm/reject ─────────────────────────

@pytest.mark.asyncio
async def test_confirm_batch_confirms_every_member_via_the_same_single_edge_path(fake_age, fake_versioning, fake_gateway, monkeypatch):
    """A batch action is several calls to the EXACT same confirm_match() path, not a new graph-write primitive."""
    rows = [_priority_row(601, group_id="GROUP-SAME_AS-1"), _priority_row(602, group_id="GROUP-SAME_AS-1")]

    async def fake_list_rows(edge_label, include_deprioritized=True):
        return rows

    monkeypatch.setattr(graph_review.pending_candidate_priority, "list_rows", fake_list_rows)
    fake_age.queue([{"a": _person("P-A601", "A"), "r": _same_as_edge(601), "b": _person("P-B601", "B")}])
    fake_age.queue([{"a": _person("P-A602", "C"), "r": _same_as_edge(602), "b": _person("P-B602", "D")}])

    result = await graph_review.confirm_batch("GROUP-SAME_AS-1", graph_review.ReviewAction(), _Admin())

    assert result["group_id"] == "GROUP-SAME_AS-1"
    assert len(result["results"]) == 2
    assert all(r["status"] == "confirmed" for r in result["results"])
    assert len(fake_versioning.edges_written) == 2
    assert all(e["properties"]["status"] == "confirmed" for e in fake_versioning.edges_written)


@pytest.mark.asyncio
async def test_confirm_batch_reports_an_already_reviewed_member_without_aborting_the_rest(fake_age, fake_versioning, fake_gateway, monkeypatch):
    rows = [_priority_row(701, group_id="GROUP-SAME_AS-2"), _priority_row(702, group_id="GROUP-SAME_AS-2")]

    async def fake_list_rows(edge_label, include_deprioritized=True):
        return rows

    monkeypatch.setattr(graph_review.pending_candidate_priority, "list_rows", fake_list_rows)
    # edge 701 already superseded (reviewed independently) -> 409
    fake_age.queue([{"a": _person("P-A701", "A"), "r": _same_as_edge(701, superseded_by=999), "b": _person("P-B701", "B")}])
    fake_age.queue([{"a": _person("P-A702", "C"), "r": _same_as_edge(702), "b": _person("P-B702", "D")}])

    result = await graph_review.confirm_batch("GROUP-SAME_AS-2", graph_review.ReviewAction(), _Admin())

    by_id = {r["edge_id"]: r for r in result["results"]}
    assert by_id[701]["status_code"] == 409
    assert by_id[702]["status"] == "confirmed"
    assert len(fake_versioning.edges_written) == 1, "only the not-yet-reviewed member should have written a new edge"


@pytest.mark.asyncio
async def test_reject_batch_writes_rejecting_edges(fake_age, fake_versioning, fake_gateway, monkeypatch):
    rows = [_priority_row(801, group_id="GROUP-SAME_AS-3")]

    async def fake_list_rows(edge_label, include_deprioritized=True):
        return rows

    monkeypatch.setattr(graph_review.pending_candidate_priority, "list_rows", fake_list_rows)
    fake_age.queue([{"a": _person("P-A801", "A"), "r": _same_as_edge(801), "b": _person("P-B801", "B")}])

    result = await graph_review.reject_batch("GROUP-SAME_AS-3", graph_review.ReviewAction(), _Admin())

    assert result["results"][0]["status"] == "rejected"
    assert fake_versioning.edges_written[0]["properties"]["status"] == "rejected"


@pytest.mark.asyncio
async def test_confirm_batch_unknown_group_returns_404(monkeypatch):
    async def fake_list_rows(edge_label, include_deprioritized=True):
        return []

    monkeypatch.setattr(graph_review.pending_candidate_priority, "list_rows", fake_list_rows)

    with pytest.raises(HTTPException) as exc_info:
        await graph_review.confirm_batch("GROUP-DOES-NOT-EXIST", graph_review.ReviewAction(), _Admin())
    assert exc_info.value.status_code == 404


# ── Consistency findings (Ingestion Quality Control at Scale, Module G3) ──

class _FindingRow:
    def __init__(self, d):
        self._mapping = d


class _FindingResult:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self):
        return [_FindingRow(r) for r in self._rows]


class _FindingSession:
    def __init__(self, results: list):
        self._results = list(results)
        self.executed: list[tuple] = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return self._results.pop(0) if self._results else _FindingResult()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_get_session_for(session):
    def _factory():
        return session
    return _factory


async def test_list_consistency_findings_returns_open_findings_only(monkeypatch):
    rows = [{"finding_id": 1, "edge_id": 500, "finding_reason": "Corroborating evidence has become inconsistent."}]
    session = _FindingSession([_FindingResult(rows)])
    import src.database.postgres as postgres_module
    monkeypatch.setattr(postgres_module, "get_session", _fake_get_session_for(session))

    result = await graph_review.list_consistency_findings(admin=_Admin())

    assert result["count"] == 1
    assert result["findings"][0]["edge_id"] == 500
    assert "WHERE acknowledged = false" in session.executed[0][0]


async def test_acknowledge_consistency_finding_updates_and_logs_audit(monkeypatch, fake_gateway):
    session = _FindingSession([_FindingResult(rowcount=1)])
    import src.database.postgres as postgres_module
    monkeypatch.setattr(postgres_module, "get_session", _fake_get_session_for(session))

    result = await graph_review.acknowledge_consistency_finding(1, admin=_Admin())

    assert result == {"finding_id": 1, "acknowledged": True}
    assert fake_gateway.audit_log[0]["event_type"] == "graph_review_consistency_finding_acknowledge"


async def test_acknowledge_consistency_finding_404s_when_not_found_or_already_acknowledged(monkeypatch):
    session = _FindingSession([_FindingResult(rowcount=0)])
    import src.database.postgres as postgres_module
    monkeypatch.setattr(postgres_module, "get_session", _fake_get_session_for(session))

    with pytest.raises(HTTPException) as exc_info:
        await graph_review.acknowledge_consistency_finding(999, admin=_Admin())
    assert exc_info.value.status_code == 404


async def test_acknowledge_consistency_finding_never_touches_the_graph(monkeypatch, fake_age, fake_versioning):
    """The whole point of this endpoint — confirm it makes zero AGE calls
    and zero versioning writes."""
    session = _FindingSession([_FindingResult(rowcount=1)])
    import src.database.postgres as postgres_module
    monkeypatch.setattr(postgres_module, "get_session", _fake_get_session_for(session))

    class _NoGateway:
        async def log_audit_event(self, *a, **k):
            pass

    async def _get_gateway():
        return _NoGateway()

    monkeypatch.setattr(graph_review, "get_gateway", _get_gateway)

    await graph_review.acknowledge_consistency_finding(1, admin=_Admin())

    assert fake_age.calls == []
    assert fake_versioning.edges_written == []
