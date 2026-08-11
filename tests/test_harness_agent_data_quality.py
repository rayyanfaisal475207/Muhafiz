"""
Tests for src/pipeline/harness/agents/data_quality.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 8, §7.3 — "Phase 9" per this
session's brief, the 8th and final sub-agent.)

Covers:
  (a) `_run_metric()`'s readiness classification at its actual boundary
      conditions: primary count == 0 -> UNAVAILABLE, primary count > 0 ->
      READY, the underlying fetch raising -> UNKNOWN (never silently
      UNAVAILABLE -- the checked-vs-never-checked distinction this
      contract amendment exists for);
  (b) the query-failure-vs-empty distinction at the whole-result level:
      some groups UNKNOWN -> PARTIAL with a caveat naming which; all six
      UNKNOWN -> ABSTAINED;
  (c) no active case -> EMPTY, not an exception;
  (d) a full success case wired through the REAL fetch functions with
      Postgres/AGE/Chroma stubbed at their own boundaries (not `_FETCHERS`
      itself), proving the actual query construction for each backend;
  (e) structural caveats (document_coverage's failed/quarantined gap,
      conflict_coverage's checked-vs-never-run ambiguity) are always
      present;
  (f) no role gate -- DENIED is never produced regardless of caller role;
  (g) module self-registration and a Supervisor integration test that
      bypasses route_query()'s real classification (per this session's
      "no classification-trigger guessing" scope, same shape Timeline
      Building's own test uses).

`_FETCHERS` entries are monkeypatched directly for (a)/(b)/(c)/(e)/(f) --
that dict is this module's own seam between "which group produced what"
and "how each group's data is actually fetched," and is the natural unit
boundary for testing the readiness/status logic without needing three
different live backends per test. (d) instead stubs `scoped_cypher`/
`get_session`/`ChromaVectorStore` directly, proving the real backend
wiring at least once per backend kind.
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.agents.data_quality as dq_mod
from src.pipeline.harness.agents.data_quality import data_quality
from src.pipeline.harness.supervisor import (
    DATA_QUALITY,
    Supervisor,
    get_registered,
)
from src.pipeline.harness.types import (
    CallerContext,
    DataQualityReadiness,
    ExecutionContext,
    Role,
    SubAgentInput,
    SubAgentStatus,
)


def _caller(role=Role.INVESTIGATOR, active_case_id="CASE-001", **kw):
    return CallerContext(user_id="u1", role=role, active_case_id=active_case_id, **kw)


def _execution(caller=None):
    return ExecutionContext(caller=caller or _caller())


def _agent_input(caller=None, query_text="how much evidence exists for this case", **kw):
    return SubAgentInput(query_text=query_text, execution=_execution(caller=caller), **kw)


def _stub_all_fetchers(monkeypatch, *, counts_by_group=None, raise_groups=()):
    """
    Monkeypatches every entry in `_FETCHERS` -- groups named in
    `raise_groups` raise; every other group returns its configured counts
    dict (default: an empty dict, i.e. primary count 0 -> UNAVAILABLE).
    """
    counts_by_group = counts_by_group or {}

    def _make(name):
        async def _fake(case_id):
            assert case_id
            if name in raise_groups:
                raise RuntimeError(f"{name} backend unreachable")
            return counts_by_group.get(name, {})

        return _fake

    fake_fetchers = {name: _make(name) for name in dq_mod._GROUP_META}
    monkeypatch.setattr(dq_mod, "_FETCHERS", fake_fetchers)


# ═══════════════════════════════════════════════════════════════════════
# (a) _run_metric()'s readiness classification at its boundary conditions
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_metric_zero_primary_count_is_unavailable(monkeypatch):
    _stub_all_fetchers(monkeypatch, counts_by_group={"timeline_readiness": {"dated_incidents": 0}})
    metric = await dq_mod._run_metric("timeline_readiness", "CASE-001")
    assert metric.readiness == DataQualityReadiness.UNAVAILABLE
    assert metric.error is None
    assert metric.counts == {"dated_incidents": 0}


@pytest.mark.asyncio
async def test_run_metric_positive_primary_count_is_ready(monkeypatch):
    _stub_all_fetchers(monkeypatch, counts_by_group={"timeline_readiness": {"dated_incidents": 4}})
    metric = await dq_mod._run_metric("timeline_readiness", "CASE-001")
    assert metric.readiness == DataQualityReadiness.READY
    assert metric.counts["dated_incidents"] == 4


@pytest.mark.asyncio
async def test_run_metric_fetch_exception_is_unknown_not_unavailable(monkeypatch):
    # The exact ambiguity this contract amendment exists to prevent: a
    # failed query must never silently read as "checked, nothing there."
    _stub_all_fetchers(monkeypatch, raise_groups=("identity_health",))
    metric = await dq_mod._run_metric("identity_health", "CASE-001")
    assert metric.readiness == DataQualityReadiness.UNKNOWN
    assert metric.counts == {}
    assert metric.error is not None
    assert "unreachable" in metric.error


@pytest.mark.asyncio
async def test_run_metric_missing_primary_key_defaults_to_zero(monkeypatch):
    # A fetch returning a dict that happens to omit its own primary key
    # (shouldn't happen in practice, but _run_metric must not crash) reads
    # as UNAVAILABLE, not an exception.
    _stub_all_fetchers(monkeypatch, counts_by_group={"embedding_coverage": {}})
    metric = await dq_mod._run_metric("embedding_coverage", "CASE-001")
    assert metric.readiness == DataQualityReadiness.UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════════
# (b) whole-result status: PARTIAL on some-UNKNOWN, ABSTAINED on all-UNKNOWN
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_all_groups_ready_returns_ok(monkeypatch):
    _stub_all_fetchers(
        monkeypatch,
        counts_by_group={
            "document_coverage": {"total_documents": 5},
            "entity_extraction": {"total_entities": 12},
            "timeline_readiness": {"dated_incidents": 3},
            "identity_health": {"total_identity_matches": 2},
            "conflict_coverage": {"incidents_checkable": 3, "conflicts_found": 1},
            "embedding_coverage": {"chunks_embedded": 40, "documents_ingested": 5},
        },
    )
    result = await data_quality(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert len(result.metrics) == 6
    assert all(m.readiness == DataQualityReadiness.READY for m in result.metrics)
    assert result.answer_text is not None
    assert "CASE-001" in result.answer_text
    assert result.tools_used == []
    assert result.degraded_from == []
    # Structural caveats always present.
    assert any("quarantined" in c.lower() for c in result.caveats)
    assert any("never run" in c.lower() or "never was" in c.lower() for c in result.caveats)


@pytest.mark.asyncio
async def test_some_groups_unknown_returns_partial_with_caveat(monkeypatch):
    _stub_all_fetchers(monkeypatch, raise_groups=("conflict_coverage", "embedding_coverage"))

    result = await data_quality(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    unknown = {m.name for m in result.metrics if m.readiness == DataQualityReadiness.UNKNOWN}
    assert unknown == {"conflict_coverage", "embedding_coverage"}
    assert any("could not be computed" in c.lower() for c in result.caveats)
    assert result.answer_text is not None  # still served -- partial data is still useful


@pytest.mark.asyncio
async def test_all_six_groups_unknown_returns_abstained(monkeypatch):
    _stub_all_fetchers(monkeypatch, raise_groups=tuple(dq_mod._GROUP_META))

    result = await data_quality(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.metrics == []  # bounded payload -- nothing computed, nothing served
    assert result.error is not None
    assert result.error.kind == "upstream_failure"


# ═══════════════════════════════════════════════════════════════════════
# (c) no active case -> EMPTY, not an exception
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_no_active_case_returns_empty():
    result = await data_quality(_agent_input(caller=_caller(active_case_id=None)))

    assert result.status == SubAgentStatus.EMPTY
    assert result.answer_text is None
    assert result.metrics == []
    assert result.caveats


# ═══════════════════════════════════════════════════════════════════════
# (d) real fetch functions, backends stubbed at their own boundary
# ═══════════════════════════════════════════════════════════════════════


class _FakeExecResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, doc_type_rows, doc_id_rows):
        self._doc_type_rows = doc_type_rows
        self._doc_id_rows = doc_id_rows

    async def execute(self, query):
        compiled = str(query)
        if "doc_type" in compiled:
            return _FakeExecResult(self._doc_type_rows)
        return _FakeExecResult(self._doc_id_rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _stub_postgres(monkeypatch, doc_type_rows, doc_id_rows):
    monkeypatch.setattr(dq_mod, "get_session", lambda: _FakeDB(doc_type_rows, doc_id_rows))


def _stub_scoped_cypher_real(monkeypatch, *, entity_counts, dated_incidents, conflicts_found, identity_counts):
    async def _fake(cypher_query, case_id, params=None, columns=("result",), **kw):
        assert case_id
        if "CONFLICTS_WITH" in cypher_query:
            return [{"cnt": conflicts_found}]
        if "SAME_AS" in cypher_query:
            status = (params or {}).get("status")
            return [{"cnt": identity_counts.get(status, 0)}]
        if "OCCURRED_ON" in cypher_query:
            return [{"cnt": dated_incidents}]
        for label in dq_mod._ENTITY_LABELS:
            if f"(n:{label})" in cypher_query:
                return [{"cnt": entity_counts.get(label, 0)}]
        raise AssertionError(f"Unexpected Cypher query in test stub: {cypher_query!r}")

    monkeypatch.setattr(dq_mod, "scoped_cypher", _fake)


class _FakeChromaStore:
    def __init__(self, metadata):
        self._metadata = metadata

    def get_all_metadata(self):
        return self._metadata


def _stub_chroma(monkeypatch, metadata):
    monkeypatch.setattr(
        dq_mod.ChromaVectorStore, "get_instance", classmethod(lambda cls: _FakeChromaStore(metadata))
    )


@pytest.mark.asyncio
async def test_full_success_through_real_fetch_functions(monkeypatch):
    _stub_postgres(
        monkeypatch,
        doc_type_rows=[("fir",), ("witness_statement",), ("fir",)],
        doc_id_rows=[("D1",), ("D2",), ("D3",)],
    )
    _stub_scoped_cypher_real(
        monkeypatch,
        entity_counts={"Person": 4, "Vehicle": 1, "PhoneNumber": 0, "Address": 0, "Organization": 0, "Weapon": 0},
        dated_incidents=2,
        conflicts_found=0,
        identity_counts={"pending": 1, "confirmed": 2, "rejected": 0},
    )
    _stub_chroma(
        monkeypatch,
        metadata=[
            {"case_id": "CASE-001", "source": "a.pdf"},
            {"case_id": "CASE-001", "source": "a.pdf"},
            {"case_id": "CASE-999", "source": "other.pdf"},
        ],
    )

    result = await data_quality(_agent_input())

    assert result.status == SubAgentStatus.OK
    by_name = {m.name: m for m in result.metrics}

    assert by_name["document_coverage"].readiness == DataQualityReadiness.READY
    assert by_name["document_coverage"].counts["total_documents"] == 3
    assert by_name["document_coverage"].counts["by_type:fir"] == 2

    assert by_name["entity_extraction"].readiness == DataQualityReadiness.READY
    assert by_name["entity_extraction"].counts["total_entities"] == 5
    assert by_name["entity_extraction"].counts["by_label:Person"] == 4

    assert by_name["timeline_readiness"].readiness == DataQualityReadiness.READY
    assert by_name["timeline_readiness"].counts["dated_incidents"] == 2

    assert by_name["identity_health"].readiness == DataQualityReadiness.READY
    assert by_name["identity_health"].counts["total_identity_matches"] == 3
    assert by_name["identity_health"].counts["confirmed"] == 2

    assert by_name["conflict_coverage"].readiness == DataQualityReadiness.READY  # 2 checkable, 0 found
    assert by_name["conflict_coverage"].counts["incidents_checkable"] == 2
    assert by_name["conflict_coverage"].counts["conflicts_found"] == 0

    # Only CASE-001-tagged chunks counted -- CASE-999's chunk is excluded.
    assert by_name["embedding_coverage"].readiness == DataQualityReadiness.READY
    assert by_name["embedding_coverage"].counts["chunks_embedded"] == 2
    assert by_name["embedding_coverage"].counts["documents_ingested"] == 3


@pytest.mark.asyncio
async def test_conflict_coverage_unknown_independent_of_timeline_readiness(monkeypatch):
    # The CONFLICTS_WITH sub-query fails; the shared OCCURRED_ON query
    # (used by both timeline_readiness AND conflict_coverage's own
    # incidents_checkable count) still succeeds -- proving conflict_
    # coverage's own asyncio.gather() propagates its sibling's failure
    # without corrupting timeline_readiness's independent fetch.
    async def _fake(cypher_query, case_id, params=None, columns=("result",), **kw):
        if "CONFLICTS_WITH" in cypher_query:
            raise RuntimeError("AGE unreachable")
        if "OCCURRED_ON" in cypher_query:
            return [{"cnt": 2}]
        return [{"cnt": 0}]

    monkeypatch.setattr(dq_mod, "scoped_cypher", _fake)
    _stub_postgres(monkeypatch, doc_type_rows=[], doc_id_rows=[])
    _stub_chroma(monkeypatch, metadata=[])

    result = await data_quality(_agent_input())
    by_name = {m.name: m for m in result.metrics}

    assert by_name["conflict_coverage"].readiness == DataQualityReadiness.UNKNOWN
    assert by_name["timeline_readiness"].readiness == DataQualityReadiness.READY
    assert by_name["timeline_readiness"].counts["dated_incidents"] == 2
    assert result.status == SubAgentStatus.PARTIAL


# ═══════════════════════════════════════════════════════════════════════
# (f) no role gate -- DENIED never produced regardless of caller role
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_no_role_gate_every_role_gets_the_same_shape(monkeypatch):
    _stub_all_fetchers(monkeypatch, counts_by_group={"document_coverage": {"total_documents": 1}})

    for role in Role:
        result = await data_quality(_agent_input(caller=_caller(role=role)))
        assert result.status != SubAgentStatus.DENIED
        assert result.status == SubAgentStatus.OK  # all six checked; only one has a positive count


# ═══════════════════════════════════════════════════════════════════════
# (g) registration + Supervisor integration, classification bypassed
# ═══════════════════════════════════════════════════════════════════════


def test_data_quality_is_registered_under_its_own_name():
    assert get_registered(DATA_QUALITY) is data_quality


@pytest.mark.asyncio
async def test_supervisor_dispatches_to_real_data_quality_via_forced_classification(monkeypatch):
    """
    Supervisor.handle() -> real Data-Quality -> real fetch functions
    (stubbed at their own backend boundary, not live infra).

    [Classification-reachability caveat, stated per this session's scope,
    same as Timeline Building's own precedent] route_query() has no real
    signal for Data-Quality today (supervisor.py's own module docstring;
    AGENT_HARNESS_IMPLEMENTATION_PLAN.md §9 Phase 1 entry) -- this test
    does not exercise real end-user classification. It forces the
    dispatch by monkeypatching classify_to_subagent() directly, proving
    the Supervisor <-> sub-agent wiring itself works, while leaving real
    classification reachability as the documented, separately-tracked gap
    it is.
    """
    import src.pipeline.harness.supervisor as supervisor_mod

    async def _fake_route_query(query_text: str) -> dict:
        return {"route": "RAG", "output_format": "chat"}

    monkeypatch.setattr(supervisor_mod, "route_query", _fake_route_query)
    monkeypatch.setattr(supervisor_mod, "classify_to_subagent", lambda route_result, query_text="": DATA_QUALITY)

    _stub_all_fetchers(monkeypatch, counts_by_group={"timeline_readiness": {"dated_incidents": 1}})

    sup = Supervisor()  # no override -> real module-level registry
    result = await sup.handle(_agent_input())

    assert result.status == SubAgentStatus.OK  # all six checked successfully
    assert len(result.metrics) == 6
