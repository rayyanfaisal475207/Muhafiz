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

        async def fake_ingest_documents(documents, source_name, **kwargs):
            order.append(("ingest", kwargs.get("run_graph_extraction")))
            return {"chunks_added": 1}

        async def fake_project_fir(fir, *, graph=None):
            order.append(("project", fir.fir_id))
            return {"errors": []}

        monkeypatch.setattr(sync_script, "purge_edges_by_source_prefix", fake_purge)
        monkeypatch.setattr(sync_script, "ingest_documents", fake_ingest_documents)
        monkeypatch.setattr(sync_script, "project_fir", fake_project_fir)

        fir = FirRecord({"fir_id": "fir-1-26", "narrative_text": "کچھ بیانیہ متن یہاں لکھا گیا"})
        await sync_script.sync_fir(fir, dry_run=False)

        assert order[0] == ("purge", "psrms/fir/fir-1-26#")
        assert order[1] == ("ingest", False), "run_graph_extraction must be False for API-sourced records"
        assert order[-1] == ("project", "fir-1-26")

    async def test_sync_fir_skips_ingest_when_no_free_text_but_still_projects(self, monkeypatch):
        """An FIR with no renderable free text still gets its structured
        graph projected — only the ingest step is naturally skipped."""
        called = []

        async def fake_purge(prefix, *, graph=None):
            return 0
        async def fake_ingest_documents(documents, source_name, **kwargs):
            called.append("ingest")
            return {"chunks_added": 0}
        async def fake_project_fir(fir, *, graph=None):
            called.append("project")
            return {"errors": []}

        monkeypatch.setattr(sync_script, "purge_edges_by_source_prefix", fake_purge)
        monkeypatch.setattr(sync_script, "ingest_documents", fake_ingest_documents)
        monkeypatch.setattr(sync_script, "project_fir", fake_project_fir)

        fir = FirRecord({"fir_id": "fir-2-26"})  # no narrative_text at all
        await sync_script.sync_fir(fir, dry_run=False)

        assert called == ["project"]


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
