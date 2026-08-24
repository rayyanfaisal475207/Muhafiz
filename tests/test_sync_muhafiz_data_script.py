"""
scripts/sync_muhafiz_data.py — M9 of the Muhafiz Data API migration
(docs/decisions/0001-muhafiz-api-migration.md).

Covers: the purge-by-source-prefix primitive against a fake age_client,
dry-run short-circuiting, real-mode wiring (purge before
ingest/project, run_graph_extraction=False), and — the actual promise
this module exists to keep — that running the SAME FIR through sync_fir()
TWICE produces the SAME edge count, not double.
"""
import re

import pytest

import scripts.sync_muhafiz_data as sync_script
from src.data_gateway.muhafiz_api.models import FirRecord
from src.graph import entity_resolution, versioning


# ── purge_edges_by_source_prefix ─────────────────────────────────────────

class _FakeAgeClient:
    def __init__(self):
        self.calls: list[dict] = []

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.calls.append({"cypher": cypher_query, "params": params or {}})
        return [{"c": 3}]  # pretend 3 edges deleted per label


@pytest.fixture
def fake_age_client(monkeypatch):
    client = _FakeAgeClient()
    monkeypatch.setattr(sync_script, "age_client", client)
    return client


async def test_purge_sweeps_every_edge_label(fake_age_client):
    total = await sync_script.purge_edges_by_source_prefix("psrms/fir/fir-1-26#")
    assert len(fake_age_client.calls) == len(sync_script.EDGE_LABELS)
    assert total == 3 * len(sync_script.EDGE_LABELS)


async def test_purge_passes_the_prefix_as_a_bound_param(fake_age_client):
    await sync_script.purge_edges_by_source_prefix("cms/complaint/C1#")
    assert all(c["params"]["prefix"] == "cms/complaint/C1#" for c in fake_age_client.calls)


async def test_purge_never_touches_nodes(fake_age_client):
    """Only edges: a shared Person node (CNIC-auto-merged across two
    different records) must never be at risk from a single-record purge."""
    await sync_script.purge_edges_by_source_prefix("psrms/fir/fir-1-26#")
    assert all("DELETE r" in c["cypher"] and "DELETE n" not in c["cypher"] for c in fake_age_client.calls)


# ── purge_orphaned_person_nodes_by_source_prefix (findings.md Module 1
# follow-up, MODULE1_GAPS_FIX_PROMPT.md Priority 2b Option A) ──────────────

class _FakeAgeClientForOrphanPurge:
    """
    Models the THREE query shapes purge_orphaned_person_nodes_by_source_prefix()
    / _person_has_any_edge() issue (two earlier combined/unbounded shapes
    were tried first and either dropped the connection or hung for 25+
    real seconds on a single node — see purge_orphaned_person_nodes_by_source_prefix()'s
    own docstring for the full story):
      1. a plain STARTS WITH lookup returning candidate entity_ids
      2. one existence check per (candidate, EDGE_LABEL) pair — stops at
         the first label that hits, so `edges_by_id` only needs to list
         the ONE label (if any) that should report "found" for a given
         candidate; every other label is reported "not found"
      3. a DETACH DELETE for candidates where every label came back empty

    `edges_by_id`: entity_id -> the one EDGE_LABELS entry that should
    report as present for that id (None/absent = genuinely orphaned,
    every label empty).
    """
    def __init__(self, candidate_ids: list[str], edges_by_id: dict[str, str] | None = None):
        self.calls: list[dict] = []
        self._candidate_ids = candidate_ids
        self._edges_by_id = edges_by_id or {}

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.calls.append({"cypher": cypher_query, "params": params or {}})
        if "STARTS WITH" in cypher_query:
            return [{"entity_id": eid} for eid in self._candidate_ids]
        if "DETACH DELETE p" in cypher_query:
            return [{"deleted": 1}]
        # a per-label existence check: MATCH (p {entity_id: $eid})-[r:LABEL]-() ...
        eid = (params or {}).get("eid")
        found_label = self._edges_by_id.get(eid)
        this_label = re.search(r"\[r:(\w+)\]", cypher_query).group(1)
        return [{"r": {}}] if found_label == this_label else []


async def test_purge_orphans_candidate_lookup_passes_the_prefix_as_a_bound_param(monkeypatch):
    client = _FakeAgeClientForOrphanPurge(candidate_ids=[])
    monkeypatch.setattr(sync_script, "age_client", client)

    await sync_script.purge_orphaned_person_nodes_by_source_prefix("psrms/fir/fir-1-26#")

    assert len(client.calls) == 1  # no candidates -> no per-label/delete follow-up calls
    assert client.calls[0]["params"]["prefix"] == "psrms/fir/fir-1-26#"
    assert "p.source_doc_id STARTS WITH $prefix" in client.calls[0]["cypher"]


async def test_purge_orphans_checks_labels_one_at_a_time_never_unbounded(monkeypatch):
    """The exact shapes that either dropped the connection or hung for 25s
    live must never appear again — every per-node check is single-label."""
    client = _FakeAgeClientForOrphanPurge(candidate_ids=["PERSON-A"])
    monkeypatch.setattr(sync_script, "age_client", client)

    await sync_script.purge_orphaned_person_nodes_by_source_prefix("cms/complaint/C1#")

    per_node_calls = [c for c in client.calls if "STARTS WITH" not in c["cypher"]]
    assert per_node_calls, "must have checked at least one label before deleting"
    for call in per_node_calls:
        if "DETACH DELETE p" in call["cypher"]:
            continue
        assert re.search(r"\[r:\w+\]", call["cypher"]), "must be scoped to exactly one label"
        assert "OPTIONAL MATCH" not in call["cypher"], "no unbounded/aggregate degree pattern"


async def test_purge_orphans_deletes_only_the_genuinely_zero_degree_candidates(monkeypatch):
    """Two candidates from the same prefix: one still has a surviving edge
    from a DIFFERENT record (e.g. a cross-silo ASSOCIATED_WITH) — that one
    must survive; only the truly orphaned one gets deleted."""
    client = _FakeAgeClientForOrphanPurge(
        candidate_ids=["PERSON-ORPHAN", "PERSON-STILL-LINKED"],
        edges_by_id={"PERSON-STILL-LINKED": "ASSOCIATED_WITH"},
    )
    monkeypatch.setattr(sync_script, "age_client", client)

    deleted = await sync_script.purge_orphaned_person_nodes_by_source_prefix("psrms/fir/fir-1-26#")

    assert deleted == 1
    deleted_ids = [
        c["params"]["eid"] for c in client.calls if "DETACH DELETE p" in c["cypher"]
    ]
    assert deleted_ids == ["PERSON-ORPHAN"]


async def test_purge_orphans_stops_at_the_first_label_that_hits(monkeypatch):
    """A candidate with a surviving edge must not trigger a check of every
    remaining label — early exit as soon as one is found."""
    client = _FakeAgeClientForOrphanPurge(
        candidate_ids=["PERSON-STILL-LINKED"],
        edges_by_id={"PERSON-STILL-LINKED": sync_script.EDGE_LABELS[0]},
    )
    monkeypatch.setattr(sync_script, "age_client", client)

    await sync_script.purge_orphaned_person_nodes_by_source_prefix("psrms/fir/fir-1-26#")

    per_node_calls = [c for c in client.calls if "STARTS WITH" not in c["cypher"]]
    assert len(per_node_calls) == 1, "must stop checking further labels after the first hit"


async def test_purge_orphans_no_candidates_returns_zero(monkeypatch):
    client = _FakeAgeClientForOrphanPurge(candidate_ids=[])
    monkeypatch.setattr(sync_script, "age_client", client)
    deleted = await sync_script.purge_orphaned_person_nodes_by_source_prefix("psrms/fir/fir-1-26#")
    assert deleted == 0


async def test_purge_orphans_never_raises_on_a_connection_failure(monkeypatch):
    """Live-caught: this step failing must never crash the sync it's
    attached to — degrades to 0, logs a warning, keeps going."""
    class _FailingClient:
        async def execute_cypher(self, *a, **k):
            raise ConnectionError("connection was closed in the middle of operation")

    monkeypatch.setattr(sync_script, "age_client", _FailingClient())
    deleted = await sync_script.purge_orphaned_person_nodes_by_source_prefix("psrms/fir/fir-1-26#")
    assert deleted == 0


# ── dry-run short-circuits ───────────────────────────────────────────────

class TestDryRunNeverWrites:
    async def test_sync_fir_dry_run_makes_no_graph_or_ingest_calls(self, monkeypatch):
        called = []
        monkeypatch.setattr(sync_script, "purge_edges_by_source_prefix", lambda *a, **k: called.append(1))
        monkeypatch.setattr(sync_script, "ingest_documents", lambda *a, **k: called.append(1))
        monkeypatch.setattr(sync_script, "project_fir", lambda *a, **k: called.append(1))

        fir = FirRecord({"fir_id": "fir-1-26", "narrative_text": "متن"})
        stats = await sync_script.sync_fir(fir, dry_run=True)

        assert not called
        assert stats["would_purge_prefix"] == "psrms/fir/fir-1-26#"

    async def test_sync_cms_dry_run_makes_no_calls(self, monkeypatch):
        called = []
        monkeypatch.setattr(sync_script, "purge_edges_by_source_prefix", lambda *a, **k: called.append(1))
        monkeypatch.setattr(sync_script, "project_cms_complaint", lambda *a, **k: called.append(1))

        from src.data_gateway.muhafiz_api.models import CmsComplaint
        cms = CmsComplaint({"complaint_id": "C1", "one_line_summary": "خلاصہ"})
        stats = await sync_script.sync_cms(cms, case_id=None, dry_run=True)

        assert not called
        assert stats["would_purge_prefix"] == "cms/complaint/C1#"

    async def test_sync_criminal_record_dry_run_makes_no_calls(self, monkeypatch):
        called = []
        monkeypatch.setattr(sync_script, "purge_edges_by_source_prefix", lambda *a, **k: called.append(1))
        monkeypatch.setattr(sync_script, "project_criminal_record", lambda *a, **k: called.append(1))

        from src.data_gateway.muhafiz_api.models import CriminalRecord
        record = CriminalRecord({"id": "CR1", "subject_cnic": "00000-1-1"})
        stats = await sync_script.sync_criminal_record(record, dry_run=True)

        assert not called


# ── real-mode wiring ──────────────────────────────────────────────────────

class TestRealModeWiring:
    async def test_sync_fir_purges_before_ingesting_and_projecting(self, monkeypatch):
        order = []

        async def fake_purge(prefix, *, graph=None):
            order.append(("purge", prefix))
            return 0

        async def fake_purge_orphans(prefix, *, graph=None):
            order.append(("purge_orphans", prefix))
            return 0

        async def fake_ingest_documents(documents, source_name, **kwargs):
            order.append(("ingest", kwargs.get("run_graph_extraction")))
            return {"chunks_added": 1}

        async def fake_project_fir(fir, *, graph=None):
            order.append(("project", fir.fir_id))
            return {"errors": []}

        monkeypatch.setattr(sync_script, "purge_edges_by_source_prefix", fake_purge)
        monkeypatch.setattr(sync_script, "purge_orphaned_person_nodes_by_source_prefix", fake_purge_orphans)
        monkeypatch.setattr(sync_script, "ingest_documents", fake_ingest_documents)
        monkeypatch.setattr(sync_script, "project_fir", fake_project_fir)

        fir = FirRecord({"fir_id": "fir-1-26", "narrative_text": "کچھ بیانیہ متن یہاں لکھا گیا"})
        await sync_script.sync_fir(fir, dry_run=False)

        assert order[0] == ("purge", "psrms/fir/fir-1-26#")
        assert order[1] == ("purge_orphans", "psrms/fir/fir-1-26#"), (
            "orphan-node cleanup must run right after the edge purge, before re-projection"
        )
        assert order[2] == ("ingest", False), "run_graph_extraction must be False for API-sourced records"
        assert order[-1] == ("project", "fir-1-26")

    async def test_sync_fir_skips_ingest_when_no_free_text_but_still_projects(self, monkeypatch):
        """An FIR with no renderable free text still gets its structured
        graph projected — only the ingest step is naturally skipped."""
        called = []

        async def fake_purge(prefix, *, graph=None):
            return 0
        async def fake_purge_orphans(prefix, *, graph=None):
            return 0
        async def fake_ingest_documents(documents, source_name, **kwargs):
            called.append("ingest")
            return {"chunks_added": 0}
        async def fake_project_fir(fir, *, graph=None):
            called.append("project")
            return {"errors": []}

        monkeypatch.setattr(sync_script, "purge_edges_by_source_prefix", fake_purge)
        monkeypatch.setattr(sync_script, "purge_orphaned_person_nodes_by_source_prefix", fake_purge_orphans)
        monkeypatch.setattr(sync_script, "ingest_documents", fake_ingest_documents)
        monkeypatch.setattr(sync_script, "project_fir", fake_project_fir)

        fir = FirRecord({"fir_id": "fir-2-26"})  # no narrative_text at all
        await sync_script.sync_fir(fir, dry_run=False)

        assert called == ["project"]

    async def test_sync_cms_purges_orphans_right_after_edge_purge(self, monkeypatch):
        order = []

        async def fake_purge(prefix, *, graph=None):
            order.append(("purge", prefix))
            return 0
        async def fake_purge_orphans(prefix, *, graph=None):
            order.append(("purge_orphans", prefix))
            return 0
        async def fake_project_cms(cms, case_id, *, graph=None):
            order.append(("project", cms.complaint_id))
            return {"errors": []}

        monkeypatch.setattr(sync_script, "purge_edges_by_source_prefix", fake_purge)
        monkeypatch.setattr(sync_script, "purge_orphaned_person_nodes_by_source_prefix", fake_purge_orphans)
        monkeypatch.setattr(sync_script, "project_cms_complaint", fake_project_cms)

        from src.data_gateway.muhafiz_api.models import CmsComplaint
        cms = CmsComplaint({"complaint_id": "C1"})
        await sync_script.sync_cms(cms, case_id="fir-1-26", dry_run=False)

        assert order == [
            ("purge", "cms/complaint/C1#"),
            ("purge_orphans", "cms/complaint/C1#"),
            ("project", "C1"),
        ]

    async def test_sync_pkm_purges_orphans_right_after_edge_purge(self, monkeypatch):
        order = []

        async def fake_purge(prefix, *, graph=None):
            order.append(("purge", prefix))
            return 0
        async def fake_purge_orphans(prefix, *, graph=None):
            order.append(("purge_orphans", prefix))
            return 0
        async def fake_project_pkm(pkm, case_id, *, graph=None):
            order.append(("project", pkm.application_id))
            return {"errors": []}

        monkeypatch.setattr(sync_script, "purge_edges_by_source_prefix", fake_purge)
        monkeypatch.setattr(sync_script, "purge_orphaned_person_nodes_by_source_prefix", fake_purge_orphans)
        monkeypatch.setattr(sync_script, "project_pkm_application", fake_project_pkm)

        from src.data_gateway.muhafiz_api.models import PkmApplication
        pkm = PkmApplication({"application_id": "P1", "service_type": "women_violence_report"})
        await sync_script.sync_pkm(pkm, case_id="fir-1-26", dry_run=False)

        assert order == [
            ("purge", "pkm/application/P1#"),
            ("purge_orphans", "pkm/application/P1#"),
            ("project", "P1"),
        ]


# ── the actual promise: running sync_fir twice does not duplicate edges ──

class _FakeGraphStore:
    """
    Faithful enough model of the two primitives the idempotency guarantee
    depends on: versioning.write_edge() always CREATEs (append), and a
    label-scoped DELETE ... WHERE source_doc_id STARTS WITH $prefix
    actually removes matching entries — exactly what
    purge_edges_by_source_prefix() issues.
    """
    def __init__(self):
        self.edges: list[dict] = []
        self._next_entity = 0

    def _new_entity_id(self) -> str:
        self._next_entity += 1
        return f"P-{self._next_entity}"

    async def write_node(self, label, match, properties=None, *, source_doc_id=None, confidence=1.0, graph=None):
        return {"id": 1, "label": label, "properties": {**match, **(properties or {})}}

    async def write_edge(self, edge_label, from_label, from_match, to_label, to_match,
                          properties=None, *, source_doc_id, source_chunk_id=None,
                          confidence=1.0, supersedes_edge_id=None, graph=None):
        self.edges.append({"label": edge_label, "source_doc_id": source_doc_id})
        return {"id": len(self.edges), "label": edge_label, "properties": properties or {}}

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        # Only two shapes reach this fake in this test: the purge's DELETE,
        # and entity_resolution's candidate-lookup MATCHes (kept harmless —
        # always "no candidates" — see the fixture below for why).
        if "DELETE r" in cypher_query:
            m = re.search(r"\[r:(\w+)\]", cypher_query)
            label = m.group(1) if m else None
            prefix = (params or {}).get("prefix", "")
            before = len(self.edges)
            self.edges = [
                e for e in self.edges
                if not (e["label"] == label and e["source_doc_id"] and e["source_doc_id"].startswith(prefix))
            ]
            return [{"c": before - len(self.edges)}]
        return []


@pytest.fixture
def fake_graph_store(monkeypatch):
    store = _FakeGraphStore()
    monkeypatch.setattr(versioning, "write_node", store.write_node)
    monkeypatch.setattr(versioning, "write_edge", store.write_edge)
    monkeypatch.setattr(sync_script, "age_client", store)

    # These tests isolate to the GRAPH-write idempotency claim specifically
    # (what sync_fir's purge-then-project_fir sequence promises) — the
    # embed/chunk/store half is exercised separately by
    # tests/test_muhafiz_records.py's real end-to-end test, so it's
    # stubbed out here rather than needing live embedding/Chroma/Postgres.
    async def fake_ingest_documents(documents, source_name, **kwargs):
        return {"chunks_added": len(documents)}
    monkeypatch.setattr(sync_script, "ingest_documents", fake_ingest_documents)

    # Every person mention mints a brand-new node (TIER_NEW) — no
    # candidates, no existing CNIC match — so entity_resolution's real
    # write_node/write_edge calls run for real (against the fake store
    # above), without needing a full fake AGE MATCH implementation.
    async def fake_generate_candidates(label, mention, case_id, id_key=None, *, graph=None):
        return []
    async def fake_find_by_primary_id(label, id_key, id_value, *, graph=None):
        return None
    monkeypatch.setattr(entity_resolution, "_generate_candidates", fake_generate_candidates)
    monkeypatch.setattr(entity_resolution, "_find_by_primary_id", fake_find_by_primary_id)

    return store


async def test_running_sync_fir_twice_does_not_duplicate_edges(fake_graph_store):
    fir = FirRecord({
        "fir_id": "fir-100-26", "fir_display_code": "100/26",
        "incident_datetime": "2026-08-18T15:10:00Z",
        "complainant_full_name": "احمد", "complainant_cnic": "00000-1000000-1",
        "police_station": {"name": "PS Test"},
        "fir_accused": [{"id": "a1", "full_name": "ملزم ایک", "cnic": "00000-2000000-1"}],
        "fir_witness": [{"id": "w1", "full_name": "گواہ ایک", "cnic": "00000-3000000-1"}],
        "fir_section": [{"id": "s1", "section_code": "379", "act": "PPC"}],
        "weapon_register": [{"id": "wp1", "item_detail": "پستول", "recovered_from": "ملزم ایک"}],
    })

    await sync_script.sync_fir(fir, dry_run=False)
    first_run_count = len(fake_graph_store.edges)
    assert first_run_count > 0, "sanity check: the first run must actually write something"

    await sync_script.sync_fir(fir, dry_run=False)
    second_run_count = len(fake_graph_store.edges)

    assert second_run_count == first_run_count, (
        f"re-running sync_fir on the same FIR duplicated edges: "
        f"{first_run_count} -> {second_run_count}"
    )


async def test_running_sync_fir_three_times_is_still_stable(fake_graph_store):
    """Not just twice — the guarantee must hold indefinitely."""
    fir = FirRecord({
        "fir_id": "fir-101-26", "narrative_text": "متن",
        "fir_section": [{"id": "s1", "section_code": "379", "act": "PPC"}],
    })

    counts = []
    for _ in range(3):
        await sync_script.sync_fir(fir, dry_run=False)
        counts.append(len(fake_graph_store.edges))

    assert counts[0] == counts[1] == counts[2]
