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
            "supersedes_edge_id": supersedes_edge_id,
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

    # ── Milestone B1: jurisdiction graph nodes ───────────────────────────

    async def test_filed_at_and_part_of_written_for_station_with_district(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(police_station={
            "id": "PS-ISB-CYBER", "name": "Cyber Crime Circle", "code": "CYB",
            "district": {"id": "DIST-06", "name": "Islamabad"},
        })
        await sp.project_fir(fir)

        station_nodes = [n for n in graph_calls["nodes"] if n["label"] == "PoliceStation"]
        district_nodes = [n for n in graph_calls["nodes"] if n["label"] == "District"]
        assert station_nodes == [{
            "label": "PoliceStation", "match": {"station_id": "PS-ISB-CYBER"},
            "properties": {"name": "Cyber Crime Circle", "code": "CYB"},
        }]
        assert district_nodes == [{
            "label": "District", "match": {"district_id": "DIST-06"},
            "properties": {"name": "Islamabad", "province": None},
        }]

        filed_at = [e for e in graph_calls["edges"] if e["edge_label"] == "FILED_AT"]
        assert len(filed_at) == 1
        assert filed_at[0]["from_match"] == {"case_id": "fir-100-26"}
        assert filed_at[0]["to_match"] == {"station_id": "PS-ISB-CYBER"}

        station_part_of = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "PART_OF" and e["from_match"] == {"station_id": "PS-ISB-CYBER"}
        ]
        assert len(station_part_of) == 1
        assert station_part_of[0]["to_match"] == {"district_id": "DIST-06"}

    async def test_station_key_falls_back_to_name_when_no_id(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(police_station={"name": "PS Test"})  # no id, no district
        await sp.project_fir(fir)

        station_nodes = [n for n in graph_calls["nodes"] if n["label"] == "PoliceStation"]
        assert station_nodes == [{
            "label": "PoliceStation", "match": {"station_id": "PS Test"},
            "properties": {"name": "PS Test", "code": None},
        }]
        assert not any(n["label"] == "District" for n in graph_calls["nodes"])
        assert not any(
            e["edge_label"] == "PART_OF" and e["from_label"] == "PoliceStation"
            for e in graph_calls["edges"]
        )

    async def test_no_station_data_writes_no_jurisdiction_nodes(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(police_station={})
        stats = await sp.project_fir(fir)

        assert not any(n["label"] in ("PoliceStation", "District") for n in graph_calls["nodes"])
        assert not any(e["edge_label"] == "FILED_AT" for e in graph_calls["edges"])
        assert stats["errors"] == []

    async def test_district_as_bare_string_is_tolerated(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        """`_station_district()` in muhafiz_records.py already documents
        district as either a nested dict or a bare string — this module
        must tolerate both shapes identically."""
        fir = _minimal_fir(police_station={"id": "PS-1", "name": "PS One", "district": "Lahore"})
        await sp.project_fir(fir)

        district_nodes = [n for n in graph_calls["nodes"] if n["label"] == "District"]
        assert district_nodes == [{
            "label": "District", "match": {"district_id": "Lahore"},
            "properties": {"name": "Lahore"},
        }]

    async def test_reprojecting_the_same_fir_uses_the_same_jurisdiction_keys(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        """write_node()/write_edge() MERGE-and-append-on-the-same-key —
        idempotent re-projection (the M9 --full re-sync backfill) depends
        on the SAME station_id/district_id being derived every run, same
        requirement as `test_incident_entity_id_is_deterministic_...`
        above. The actual no-duplicate guarantee on a real re-sync also
        needs `scripts/sync_muhafiz_data.py`'s purge-by-source-doc-id-
        prefix step (FILED_AT is in its EDGE_LABELS list) — that part is
        exercised live, not by this unit test."""
        fir = _minimal_fir(police_station={
            "id": "PS-ISB-CYBER", "name": "Cyber Crime Circle",
            "district": {"id": "DIST-06", "name": "Islamabad"},
        })
        await sp.project_fir(fir)
        first_station_matches = [n["match"] for n in graph_calls["nodes"] if n["label"] == "PoliceStation"]
        first_district_matches = [n["match"] for n in graph_calls["nodes"] if n["label"] == "District"]

        graph_calls["nodes"].clear()
        await sp.project_fir(fir)
        second_station_matches = [n["match"] for n in graph_calls["nodes"] if n["label"] == "PoliceStation"]
        second_district_matches = [n["match"] for n in graph_calls["nodes"] if n["label"] == "District"]

        assert first_station_matches == second_station_matches
        assert first_district_matches == second_district_matches

    # ── Milestone B2: officer identity resolution ────────────────────────

    async def test_recording_officer_gets_assigned_to_edge(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            recording_officer_name="فیصل", recording_officer_belt_no="GEN-0901",
            recording_officer_designation="ASI", report_datetime="2026-08-18T15:10:00Z",
        )
        await sp.project_fir(fir)

        officer_calls = [c for c in fake_resolve_and_write if c["entity_type"] == "officer"]
        assert len(officer_calls) == 1
        assert officer_calls[0]["mention"]["belt_no"] == "GEN-0901"

        assigned = [e for e in graph_calls["edges"] if e["edge_label"] == "ASSIGNED_TO"]
        assert len(assigned) == 1
        assert assigned[0]["from_label"] == "Officer"
        assert assigned[0]["properties"]["role"] == "recording"
        assert assigned[0]["properties"]["assigned_from"] == "2026-08-18T15:10:00Z"
        assert assigned[0]["supersedes_edge_id"] is None

    async def test_no_recording_officer_name_writes_nothing(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir()  # no recording_officer_* fields at all
        await sp.project_fir(fir)
        assert not any(e["edge_label"] == "ASSIGNED_TO" for e in graph_calls["edges"])

    async def test_single_investigating_officer_gets_assigned_to_no_supersede(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(fir_investigating_officer=[
            {"id": "FIO-1", "officer_name": "فیصل", "belt_no": "GEN-0901",
             "assigned_from": "2024-09-25", "assigned_to": None},
        ])
        await sp.project_fir(fir)

        investigating = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "ASSIGNED_TO" and e["properties"]["role"] == "investigating"
        ]
        assert len(investigating) == 1
        assert investigating[0]["supersedes_edge_id"] is None
        assert investigating[0]["properties"]["assigned_from"] == "2024-09-25"

    async def test_reassignment_writes_a_supersession_chain(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        """Mirrors the real fir-205-26 shape: two investigating-officer
        rows, out of chronological order in the source array — must still
        be written oldest-first, with the SECOND edge superseding the
        first's id, never the reverse."""
        fir = _minimal_fir(fir_investigating_officer=[
            {"id": "FIO-2", "officer_name": "افسر دو", "belt_no": "GEN-0105",
             "assigned_from": "2026-06-22", "assigned_to": None},
            {"id": "FIO-1", "officer_name": "افسر ایک", "belt_no": "1854L",
             "assigned_from": "2026-02-15", "assigned_to": None},
        ])
        await sp.project_fir(fir)

        investigating = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "ASSIGNED_TO" and e["properties"]["role"] == "investigating"
        ]
        assert len(investigating) == 2
        # Written oldest-first, regardless of source array order.
        assert investigating[0]["properties"]["assigned_from"] == "2026-02-15"
        assert investigating[1]["properties"]["assigned_from"] == "2026-06-22"
        assert investigating[0]["supersedes_edge_id"] is None
        # The second (later) edge supersedes the first's own id — the
        # first edge is never deleted, only ever pointed at.
        first_edge_id = graph_calls["edges"].index(investigating[0]) + 1  # fake ids are 1-based positions
        assert investigating[1]["supersedes_edge_id"] == first_edge_id

    async def test_investigating_officer_row_with_no_name_is_skipped(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(fir_investigating_officer=[
            {"id": "FIO-1", "officer_name": None, "belt_no": "GEN-0901", "assigned_from": "2024-09-25"},
        ])
        await sp.project_fir(fir)
        assert not any(e["edge_label"] == "ASSIGNED_TO" for e in graph_calls["edges"])

    async def test_a_single_officer_write_failure_does_not_abort_the_whole_fir(
        self, monkeypatch, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_investigating_officer=[
                {"id": "FIO-1", "officer_name": "First", "belt_no": "B-1", "assigned_from": "2026-01-01"},
                {"id": "FIO-2", "officer_name": "Second", "belt_no": "B-2", "assigned_from": "2026-02-01"},
            ],
        )
        officer_call_count = {"n": 0}
        original = sp.entity_resolution.resolve_and_write  # the fake_resolve_and_write stub

        async def flaky(entity_type, mention, case_id, source_doc_id, source_chunk_id=None, *, graph=None):
            if entity_type == "officer":
                officer_call_count["n"] += 1
                if officer_call_count["n"] == 1:
                    raise RuntimeError("simulated officer resolution failure")
            return await original(entity_type, mention, case_id, source_doc_id, source_chunk_id, graph=graph)

        monkeypatch.setattr(sp.entity_resolution, "resolve_and_write", flaky)

        stats = await sp.project_fir(fir)

        assert stats["errors"], "the failure must be recorded"
        assert stats["officers_resolved"] >= 1, "the OTHER officer must still be written"

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
