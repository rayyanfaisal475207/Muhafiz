"""
M6a of the Muhafiz Data API migration (docs/decisions/0001-muhafiz-api-migration.md) —
src/graph/structured_projection.py.

No real AGE — src.graph.versioning and src.graph.entity_resolution are
monkeypatched (same approach as tests/test_ingestion_graph_extraction.py).
Two kinds of coverage: control-flow/wiring tests against small hand-built
FIR records, and a full sweep against the real recorded snapshot.
"""
import json
from pathlib import Path

import pytest

from src import config
from src.data_gateway.muhafiz_api.models import FirRecord
from src.graph import entity_resolution, structured_projection as sp, versioning

FIXTURE = Path(__file__).parent / "fixtures" / "muhafiz_api_snapshot.json"


@pytest.fixture(scope="module")
def firs_raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["endpoints"]["fir"]


@pytest.fixture
def graph_calls(monkeypatch):
    """Stub versioning.write_node/write_edge, recording every call. Nodes
    keyed by (label, frozenset(match.items())) so re-writes (MERGE
    semantics) are visible as repeats, matching real write_node behavior."""
    calls = {"nodes": [], "edges": []}

    async def fake_write_node(label, match, properties=None, *, source_doc_id=None, confidence=1.0, graph=None):
        calls["nodes"].append({"label": label, "match": match, "properties": properties or {}})
        return {"id": len(calls["nodes"]), "label": label, "properties": {**match, **(properties or {})}}

    async def fake_write_edge(edge_label, from_label, from_match, to_label, to_match,
                               properties=None, *, source_doc_id, source_chunk_id=None,
                               confidence=1.0, supersedes_edge_id=None, graph=None):
        calls["edges"].append({
            "edge_label": edge_label, "from_label": from_label, "from_match": from_match,
            "to_label": to_label, "to_match": to_match, "properties": properties or {},
        })
        return {"id": len(calls["edges"]), "label": edge_label, "properties": properties or {}}

    monkeypatch.setattr(versioning, "write_node", fake_write_node)
    monkeypatch.setattr(versioning, "write_edge", fake_write_edge)
    return calls


@pytest.fixture
def no_candidates_by_default(monkeypatch):
    """
    Stubs entity_resolution._generate_candidates to return [] — the
    corroboration gate's own network call for a no-CNIC mention
    (independent of resolve_and_write, which fake_resolve_and_write stubs
    separately). Broad/sweep tests need this so a no-CNIC mention's gate
    check doesn't reach for a real AGE connection; tests that care about
    the gate's actual corroboration logic override this fixture's stub
    with their own candidates.
    """
    async def fake(label, mention, case_id, id_key=None, *, graph=None):
        return []
    monkeypatch.setattr(entity_resolution, "_generate_candidates", fake)


@pytest.fixture
def fake_resolve_and_write(monkeypatch):
    """entity_resolution.resolve_and_write stub — always resolves to a
    fresh entity_id, tier 'new'. Individual tests override this where the
    tier/target matters."""
    calls = []

    async def fake(entity_type, mention, case_id, source_doc_id, source_chunk_id=None, *, graph=None):
        entity_id = f"{entity_type.upper()}-{len(calls)}"
        calls.append({"entity_type": entity_type, "mention": mention, "case_id": case_id})
        return {"entity_id": entity_id, "tier": "new", "confidence": 1.0, "basis": "test",
                "is_new_node": True, "candidates_considered": 0}

    monkeypatch.setattr(entity_resolution, "resolve_and_write", fake)
    return calls


# ── the corroboration gate ──────────────────────────────────────────────

class TestResolveStructuredPerson:
    async def test_cnic_present_always_delegates_to_resolve_and_write(self, fake_resolve_and_write):
        mention = {"canonical_name": "کاشف", "cnic": "00000-1111111-1"}
        result = await sp.resolve_structured_person(mention, "fir-1-26", "doc-1")
        assert fake_resolve_and_write
        assert result["entity_id"] == "PERSON-0"

    async def test_no_cnic_gate_disabled_bypasses_resolve_and_write(
        self, monkeypatch, graph_calls, fake_resolve_and_write,
    ):
        monkeypatch.setattr(config, "ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED", False)
        mention = {"canonical_name": "کاشف"}
        result = await sp.resolve_structured_person(mention, "fir-1-26", "doc-1")

        assert not fake_resolve_and_write, "resolve_and_write must never be called with the gate disabled"
        assert result["tier"] == "new"
        assert any(n["label"] == "Person" for n in graph_calls["nodes"])
        # No SAME_AS-capable path was even attempted.
        assert not any(e["edge_label"] == "SAME_AS" for e in graph_calls["edges"])

    async def test_no_cnic_gate_enabled_no_corroboration_bypasses_resolve_and_write(
        self, monkeypatch, graph_calls, fake_resolve_and_write,
    ):
        monkeypatch.setattr(config, "ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED", True)

        async def fake_generate_candidates(label, mention, case_id, id_key=None, *, graph=None):
            return []  # no candidates at all -> no corroboration possible

        monkeypatch.setattr(entity_resolution, "_generate_candidates", fake_generate_candidates)

        mention = {"canonical_name": "کاشف"}
        result = await sp.resolve_structured_person(mention, "fir-1-26", "doc-1")

        assert not fake_resolve_and_write
        assert result["tier"] == "new"

    async def test_no_cnic_gate_enabled_corroborated_by_shared_case_delegates(
        self, monkeypatch, fake_resolve_and_write,
    ):
        monkeypatch.setattr(config, "ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED", True)

        class _FakeCandidate:
            name_similarity = 0.95
            shared_case = True
            shared_structured_id = False
            node = {"properties": {}}

        async def fake_generate_candidates(label, mention, case_id, id_key=None, *, graph=None):
            return [_FakeCandidate()]

        monkeypatch.setattr(entity_resolution, "_generate_candidates", fake_generate_candidates)

        mention = {"canonical_name": "کاشف"}
        result = await sp.resolve_structured_person(mention, "fir-1-26", "doc-1")

        assert len(fake_resolve_and_write) == 1, "corroborated candidate must go through the real resolver"
        assert result["entity_id"] == "PERSON-0"

    async def test_no_cnic_corroborated_by_matching_address_delegates(
        self, monkeypatch, fake_resolve_and_write,
    ):
        monkeypatch.setattr(config, "ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED", True)

        class _FakeCandidate:
            name_similarity = 0.95
            shared_case = False
            shared_structured_id = False
            node = {"properties": {"address_text": "محلہ اقبال ٹاؤن"}}

        async def fake_generate_candidates(label, mention, case_id, id_key=None, *, graph=None):
            return [_FakeCandidate()]

        monkeypatch.setattr(entity_resolution, "_generate_candidates", fake_generate_candidates)

        mention = {"canonical_name": "کاشف", "address_text": "محلہ اقبال ٹاؤن"}
        result = await sp.resolve_structured_person(mention, "fir-1-26", "doc-1")

        assert len(fake_resolve_and_write) == 1

    async def test_no_cnic_weak_name_similarity_never_corroborates(self, monkeypatch, graph_calls, fake_resolve_and_write):
        monkeypatch.setattr(config, "ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED", True)

        class _FakeCandidate:
            name_similarity = 0.10  # below REVIEW_FLOOR
            shared_case = True
            shared_structured_id = True
            node = {"properties": {}}

        async def fake_generate_candidates(label, mention, case_id, id_key=None, *, graph=None):
            return [_FakeCandidate()]

        monkeypatch.setattr(entity_resolution, "_generate_candidates", fake_generate_candidates)

        mention = {"canonical_name": "X"}
        await sp.resolve_structured_person(mention, "fir-1-26", "doc-1")

        assert not fake_resolve_and_write


# ── project_fir wiring ────────────────────────────────────────────────────

def _minimal_fir(**overrides) -> FirRecord:
    raw = {
        "fir_id": "fir-100-26", "fir_display_code": "100/26",
        "incident_datetime": "2026-08-18T15:10:00Z",
        "complainant_full_name": "احمد", "complainant_cnic": "00000-1000000-1",
        "police_station": {"name": "PS Test"},
    }
    raw.update(overrides)
    return FirRecord(raw)


class TestProjectFirWiring:
    async def test_writes_case_document_and_incident_nodes(self, graph_calls, no_candidates_by_default, fake_resolve_and_write):
        fir = _minimal_fir()
        stats = await sp.project_fir(fir)

        labels = [n["label"] for n in graph_calls["nodes"]]
        assert "Case" in labels
        assert "Document" in labels
        assert "Incident" in labels
        assert stats["errors"] == []

    async def test_part_of_edge_written(self, graph_calls, no_candidates_by_default, fake_resolve_and_write):
        fir = _minimal_fir()
        await sp.project_fir(fir)
        assert any(e["edge_label"] == "PART_OF" for e in graph_calls["edges"])

    async def test_complainant_gets_involved_in_edge(self, graph_calls, no_candidates_by_default, fake_resolve_and_write):
        fir = _minimal_fir()
        await sp.project_fir(fir)
        involved = [e for e in graph_calls["edges"] if e["edge_label"] == "INVOLVED_IN"]
        assert any(e["properties"].get("role") == "complainant" for e in involved)

    async def test_accused_and_witness_get_involved_in_with_correct_roles(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_accused=[{"id": "a1", "full_name": "ملزم ایک", "cnic": "00000-2000000-1"}],
            fir_witness=[{"id": "w1", "full_name": "گواہ ایک", "cnic": "00000-3000000-1"}],
        )
        await sp.project_fir(fir)
        involved = [e for e in graph_calls["edges"] if e["edge_label"] == "INVOLVED_IN"]
        roles = {e["properties"].get("role") for e in involved}
        assert roles == {"complainant", "accused", "witness"}

    async def test_located_at_written_only_when_address_present(self, graph_calls, no_candidates_by_default, fake_resolve_and_write):
        fir = _minimal_fir(
            fir_accused=[
                {"id": "a1", "full_name": "With Address", "cnic": "00000-2000000-1", "address_text": "Model Town"},
                {"id": "a2", "full_name": "No Address", "cnic": "00000-2000000-2"},
            ],
        )
        await sp.project_fir(fir)
        located = [e for e in graph_calls["edges"] if e["edge_label"] == "LOCATED_AT"]
        assert len(located) == 1

    async def test_structured_records_written_for_all_five_tables(self, graph_calls, no_candidates_by_default, fake_resolve_and_write):
        fir = _minimal_fir(
            fir_section=[{"id": "s1", "section_code": "379", "act": "PPC"}],
            malkhana_register=[{"id": "m1", "item_detail": "cash"}],
            chalaan_dispatch=[{"id": "cd1", "property_involved": "x"}],
            chalaan_outcome=[{"id": "co1", "case_outcome": "convicted"}],
            fir_zimni_index=[{"id": "zi1", "sr_no": 1}],
        )
        stats = await sp.project_fir(fir)
        sr_nodes = [n for n in graph_calls["nodes"] if n["label"] == "StructuredRecord"]
        assert len(sr_nodes) == 5
        assert stats["structured_records"] == 5

    async def test_structured_record_row_with_no_id_is_skipped(self, graph_calls, no_candidates_by_default, fake_resolve_and_write):
        fir = _minimal_fir(fir_section=[{"section_code": "379", "act": "PPC"}])  # no "id"
        await sp.project_fir(fir)
        sr_nodes = [n for n in graph_calls["nodes"] if n["label"] == "StructuredRecord"]
        assert sr_nodes == []

    async def test_weapon_written_and_owns_edge_only_for_in_fir_match(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_accused=[{"id": "a1", "full_name": "کاشف", "cnic": "00000-2000000-1"}],
            weapon_register=[
                {"id": "w1", "item_detail": "30 بور پستول", "recovered_from": "کاشف"},
                {"id": "w2", "item_detail": "ڈنڈا", "recovered_from": "کوئی اور شخص"},  # NOT in this FIR's accused
            ],
        )
        stats = await sp.project_fir(fir)

        weapon_nodes = [n for n in graph_calls["nodes"] if n["label"] == "Weapon"]
        assert len(weapon_nodes) == 2
        assert stats["weapons_written"] == 2

        owns_edges = [e for e in graph_calls["edges"] if e["edge_label"] == "OWNS"]
        assert len(owns_edges) == 1, "only the matched-by-name weapon should get an OWNS edge"

    async def test_occurred_on_written_for_incident_zimni_and_dispatch(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_zimni=[{"id": "z1", "entry_number": 1, "entry_date": "2026-08-19"}],
            chalaan_dispatch=[{"id": "cd1", "dispatch_datetime": "2026-08-20T10:00:00Z"}],
        )
        await sp.project_fir(fir)
        occurred_on = [e for e in graph_calls["edges"] if e["edge_label"] == "OCCURRED_ON"]
        event_types = {e["properties"]["event_type"] for e in occurred_on}
        assert event_types == {"incident", "zimni_entry", "chalaan_dispatch"}

    async def test_missing_incident_datetime_writes_no_incident_occurred_on(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(incident_datetime=None)
        await sp.project_fir(fir)
        occurred_on = [e for e in graph_calls["edges"] if e["edge_label"] == "OCCURRED_ON"]
        assert not any(e["properties"]["event_type"] == "incident" for e in occurred_on)

    async def test_incident_entity_id_is_deterministic_across_reprojection(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        """write_node MERGEs on match dict — idempotent re-projection depends
        on the SAME entity_id being derived for the same FIR every run."""
        fir = _minimal_fir()
        await sp.project_fir(fir)
        first_incident_matches = [n["match"] for n in graph_calls["nodes"] if n["label"] == "Incident"]

        graph_calls["nodes"].clear()
        await sp.project_fir(fir)
        second_incident_matches = [n["match"] for n in graph_calls["nodes"] if n["label"] == "Incident"]

        assert first_incident_matches == second_incident_matches

    async def test_a_single_person_write_failure_does_not_abort_the_whole_fir(
        self, monkeypatch, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_accused=[
                {"id": "a1", "full_name": "First", "cnic": "00000-2000000-1"},
                {"id": "a2", "full_name": "Second", "cnic": "00000-2000000-2"},
            ],
        )
        real_resolve = sp.resolve_structured_person
        call_count = {"n": 0}

        async def flaky_resolve(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated failure")
            return await real_resolve(*args, **kwargs)

        monkeypatch.setattr(sp, "resolve_structured_person", flaky_resolve)

        stats = await sp.project_fir(fir)

        assert stats["errors"], "the failure must be recorded"
        assert stats["persons_resolved"] >= 1, "the OTHER accused must still be written"


# ── against the real snapshot ────────────────────────────────────────────

class TestAgainstRealSnapshot:
    async def test_every_fir_projects_without_raising(self, firs_raw, graph_calls, no_candidates_by_default, fake_resolve_and_write):
        for raw in firs_raw:
            fir = FirRecord(raw)
            stats = await sp.project_fir(fir)
            assert stats["errors"] == [], f"{fir.fir_id}: {stats['errors']}"

    async def test_every_fir_gets_a_case_and_incident_node(self, firs_raw, graph_calls, no_candidates_by_default, fake_resolve_and_write):
        for raw in firs_raw:
            fir = FirRecord(raw)
            graph_calls["nodes"].clear()
            await sp.project_fir(fir)
            labels = [n["label"] for n in graph_calls["nodes"]]
            assert "Case" in labels
            assert "Incident" in labels
