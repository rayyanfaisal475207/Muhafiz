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
            "supersedes_edge_id": supersedes_edge_id, "confidence": confidence,
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

    # ── Ingestion Quality Control at Scale, Module G1 ──────────────────

    async def test_gate_disabled_records_new_tier_not_gated(
        self, monkeypatch, graph_calls, fake_resolve_and_write,
    ):
        """Fallback administratively off is NOT a gate rejection — gated=False."""
        monkeypatch.setattr(config, "ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED", False)
        recorded = []
        monkeypatch.setattr(sp.ingestion_quality, "record_new_tier_from_gate", lambda gated: recorded.append(gated))

        await sp.resolve_structured_person({"canonical_name": "کاشف"}, "fir-1-26", "doc-1")

        assert recorded == [False]

    async def test_gate_actually_refuses_records_gated_true(
        self, monkeypatch, graph_calls, fake_resolve_and_write,
    ):
        """A real candidate existed and the gate declined it — gated=True."""
        monkeypatch.setattr(config, "ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED", True)

        async def fake_generate_candidates(label, mention, case_id, id_key=None, *, graph=None):
            return []  # no candidates -> _has_corroboration returns False

        monkeypatch.setattr(entity_resolution, "_generate_candidates", fake_generate_candidates)
        recorded = []
        monkeypatch.setattr(sp.ingestion_quality, "record_new_tier_from_gate", lambda gated: recorded.append(gated))

        await sp.resolve_structured_person({"canonical_name": "کاشف"}, "fir-1-26", "doc-1")

        assert recorded == [True]

    async def test_corroborated_path_never_calls_record_new_tier(
        self, monkeypatch, fake_resolve_and_write,
    ):
        """The corroborated branch goes through resolve_and_write() — its own
        chokepoint records the tier, not _write_new_person()'s."""
        monkeypatch.setattr(config, "ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED", True)

        class _FakeCandidate:
            name_similarity = 0.95
            shared_case = True
            shared_structured_id = False
            node = {"properties": {}}

        async def fake_generate_candidates(label, mention, case_id, id_key=None, *, graph=None):
            return [_FakeCandidate()]

        monkeypatch.setattr(entity_resolution, "_generate_candidates", fake_generate_candidates)
        recorded = []
        monkeypatch.setattr(sp.ingestion_quality, "record_new_tier_from_gate", lambda gated: recorded.append(gated))

        await sp.resolve_structured_person({"canonical_name": "کاشف"}, "fir-1-26", "doc-1")

        assert recorded == []

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

    async def test_accused_and_witness_gender_and_age_flow_into_the_person_mention(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        """
        [Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 1d] gender/age exist
        on the real upstream fir_accused/fir_witness tables but were
        previously dropped by _person_mention() — the reason XAGG's gender
        aggregate had no data path at all. Confirms the mention dict
        resolve_and_write() receives (and therefore writes verbatim as
        Person-node properties, per its own generic pass-through) now
        carries them for both accused and witness rows.
        """
        fir = _minimal_fir(
            fir_accused=[{
                "id": "a1", "full_name": "ملزم ایک", "cnic": "00000-2000000-1",
                "gender": "female", "age": 34,
            }],
            fir_witness=[{
                "id": "w1", "full_name": "گواہ ایک", "cnic": "00000-3000000-1",
                "gender": "male", "age": 41,
            }],
        )
        await sp.project_fir(fir)

        mentions_by_name = {c["mention"]["canonical_name"]: c["mention"] for c in fake_resolve_and_write}
        accused_mention = mentions_by_name["ملزم ایک"]
        witness_mention = mentions_by_name["گواہ ایک"]

        assert accused_mention["gender"] == "female"
        assert accused_mention["age"] == 34
        assert witness_mention["gender"] == "male"
        assert witness_mention["age"] == 41

    async def test_missing_gender_and_age_are_not_added_to_the_mention(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        """A row with no gender/age on the source data must not write a
        None/placeholder property onto the Person node."""
        fir = _minimal_fir(
            fir_accused=[{"id": "a1", "full_name": "بلا صنف", "cnic": "00000-2000000-9"}],
        )
        await sp.project_fir(fir)

        mention = next(c["mention"] for c in fake_resolve_and_write if c["mention"]["canonical_name"] == "بلا صنف")
        assert "gender" not in mention
        assert "age" not in mention

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


class TestPersonRelationshipEdges:
    """Milestone C1 — RELATED_TO{role} from fir_accused.relationship_to_victim/
    relationship_to_complainant."""

    async def test_relationship_to_complainant_writes_related_to(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_accused=[{
                "id": "a1", "full_name": "ملزم ایک", "cnic": "00000-2000000-1",
                "relationship_to_complainant": "بھائی",
            }],
        )
        await sp.project_fir(fir)
        related = [e for e in graph_calls["edges"] if e["edge_label"] == "RELATED_TO"]
        assert len(related) == 1
        assert related[0]["properties"]["role"] == "بھائی"
        assert related[0]["from_label"] == "Person" and related[0]["to_label"] == "Person"

    async def test_relationship_to_victim_writes_related_to_only_when_victim_present(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            victim_name="مقتول",
            fir_accused=[{
                "id": "a1", "full_name": "ملزم ایک", "cnic": "00000-2000000-1",
                "relationship_to_victim": "اجنبی",
            }],
        )
        await sp.project_fir(fir)
        related = [e for e in graph_calls["edges"] if e["edge_label"] == "RELATED_TO"]
        assert len(related) == 1
        assert related[0]["properties"]["role"] == "اجنبی"

        involved = [e for e in graph_calls["edges"] if e["edge_label"] == "INVOLVED_IN"]
        assert any(e["properties"].get("role") == "victim" for e in involved)

    async def test_no_victim_name_writes_no_related_to_for_relationship_to_victim(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_accused=[{
                "id": "a1", "full_name": "ملزم ایک", "cnic": "00000-2000000-1",
                "relationship_to_victim": "اجنبی",
            }],
        )
        await sp.project_fir(fir)
        related = [e for e in graph_calls["edges"] if e["edge_label"] == "RELATED_TO"]
        assert related == []

    async def test_no_relationship_fields_writes_no_related_to(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_accused=[{"id": "a1", "full_name": "ملزم ایک", "cnic": "00000-2000000-1"}],
        )
        await sp.project_fir(fir)
        related = [e for e in graph_calls["edges"] if e["edge_label"] == "RELATED_TO"]
        assert related == []

    async def test_both_relationships_write_two_related_to_edges(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            victim_name="مقتول",
            fir_accused=[{
                "id": "a1", "full_name": "ملزم ایک", "cnic": "00000-2000000-1",
                "relationship_to_victim": "اجنبی", "relationship_to_complainant": "پڑوسی",
            }],
        )
        await sp.project_fir(fir)
        related = [e for e in graph_calls["edges"] if e["edge_label"] == "RELATED_TO"]
        assert len(related) == 2
        assert {e["properties"]["role"] for e in related} == {"اجنبی", "پڑوسی"}


class TestAssociatedWithCoMention:
    """findings.md Module 1 — ASSOCIATED_WITH{basis, confidence} between
    every pair of Person nodes (victim/complainant/accused/witnesses) that
    INVOLVED_IN the same FIR's Incident."""

    async def test_three_accused_writes_all_pairwise_edges(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            complainant_full_name=None,
            fir_accused=[
                {"id": "a1", "full_name": "ملزم ایک"},
                {"id": "a2", "full_name": "ملزم دو"},
                {"id": "a3", "full_name": "ملزم تین"},
            ],
        )
        await sp.project_fir(fir)
        assoc = [e for e in graph_calls["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
        assert len(assoc) == 3  # C(3,2) — all pairs among the 3 accused
        pairs = {frozenset({e["from_match"]["entity_id"], e["to_match"]["entity_id"]}) for e in assoc}
        assert len(pairs) == 3  # all 3 pairs distinct, no duplicate/self pairing
        for e in assoc:
            assert e["properties"]["basis"] == "co-mentioned in case fir-100-26's incident"
            assert e["confidence"] == 0.5

    async def test_one_accused_writes_no_associated_with(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            complainant_full_name=None,
            fir_accused=[{"id": "a1", "full_name": "ملزم ایک"}],
        )
        await sp.project_fir(fir)
        assoc = [e for e in graph_calls["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
        assert assoc == []

    async def test_full_roster_not_just_accused_gets_paired(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        # complainant (default from _minimal_fir) + victim + accused + witness = 4 people.
        fir = _minimal_fir(
            victim_name="مقتول",
            fir_accused=[{"id": "a1", "full_name": "ملزم ایک"}],
            fir_witness=[{"id": "w1", "full_name": "گواه ایک"}],
        )
        await sp.project_fir(fir)
        assoc = [e for e in graph_calls["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
        assert len(assoc) == 6  # C(4,2)

    async def test_officers_never_get_associated_with(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            complainant_full_name=None,
            fir_accused=[{"id": "a1", "full_name": "ملزم ایک"}],
            fir_investigating_officer=[{"id": "io1", "officer_name": "افسر ایک", "belt_no": "B-1"}],
        )
        await sp.project_fir(fir)
        assoc = [e for e in graph_calls["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
        assert all(e["from_label"] == "Person" and e["to_label"] == "Person" for e in assoc)
        # single accused + an officer, no other Person -> still zero, and never Officer-labeled
        assert assoc == []

    async def test_two_different_firs_never_get_cross_case_edges(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir1 = _minimal_fir(
            fir_id="fir-100-26", complainant_full_name=None,
            fir_accused=[{"id": "a1", "full_name": "ملزم ایک"}, {"id": "a2", "full_name": "ملزم دو"}],
        )
        fir2 = _minimal_fir(
            fir_id="fir-200-26", complainant_full_name=None,
            fir_accused=[{"id": "b1", "full_name": "ملزم تین"}, {"id": "b2", "full_name": "ملزم چار"}],
        )
        await sp.project_fir(fir1)
        await sp.project_fir(fir2)

        # Person->Case BELONGS_TO_CASE edges give a ground-truth entity_id ->
        # case_id map regardless of which resolution path (CNIC-corroborated
        # vs. gated _write_new_person) each accused went through.
        entity_ids_by_case: dict[str, set] = {}
        for e in graph_calls["edges"]:
            if e["edge_label"] == "BELONGS_TO_CASE" and e["from_label"] == "Person":
                entity_ids_by_case.setdefault(e["to_match"]["case_id"], set()).add(e["from_match"]["entity_id"])

        assoc = [e for e in graph_calls["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
        assert len(assoc) == 2  # exactly one pairwise edge per FIR (2 accused each)
        for e in assoc:
            pair = {e["from_match"]["entity_id"], e["to_match"]["entity_id"]}
            assert pair <= entity_ids_by_case["fir-100-26"] or pair <= entity_ids_by_case["fir-200-26"]


class TestZimniOfficerAndPositionTimeline:
    """Milestone C3 — fir_zimni.officer_name resolved to an Officer
    identity (reusing B2's entity_resolution.resolve_and_write("officer",
    ...) path), and fir_position rewritten to a full dated timeline."""

    async def test_zimni_officer_resolved_and_dated(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_zimni=[
                {"id": "z1", "entry_number": 1, "entry_date": "2026-02-01", "officer_name": "SI احمد"},
            ],
        )
        await sp.project_fir(fir)

        officer_calls = [c for c in fake_resolve_and_write if c["entity_type"] == "officer"]
        assert any(c["mention"]["canonical_name"] == "SI احمد" for c in officer_calls)

        occurred = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "OCCURRED_ON" and e["from_label"] == "Officer"
        ]
        assert len(occurred) == 1
        assert occurred[0]["properties"]["event_type"] == "zimni_entry"
        assert occurred[0]["to_match"] == {"date": "2026-02-01"}

    async def test_zimni_entry_with_no_officer_name_resolves_nothing(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(fir_zimni=[{"id": "z1", "entry_number": 1, "entry_date": "2026-02-01"}])
        await sp.project_fir(fir)
        assert not any(c["entity_type"] == "officer" for c in fake_resolve_and_write)

    async def test_zimni_officer_with_no_entry_date_still_resolves_officer_but_no_occurred_on(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(fir_zimni=[{"id": "z1", "entry_number": 1, "officer_name": "SI احمد"}])
        await sp.project_fir(fir)
        assert any(c["entity_type"] == "officer" for c in fake_resolve_and_write)
        assert not any(
            e["edge_label"] == "OCCURRED_ON" and e["from_label"] == "Officer" for e in graph_calls["edges"]
        )

    async def test_fir_position_rows_get_structured_record_and_dated_occurred_on(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_position=[
                {"id": "fp1", "position": "زیر تفتیش", "status_date": "2026-03-01"},
                {"id": "fp2", "position": "چالان مکمل", "status_date": "2026-06-15"},
            ],
        )
        stats = await sp.project_fir(fir)

        sr_nodes = [n for n in graph_calls["nodes"] if n["label"] == "StructuredRecord"]
        position_records = [n for n in sr_nodes if n["properties"].get("record_type") == "fir_position"]
        assert len(position_records) == 2

        occurred = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "OCCURRED_ON" and e["from_label"] == "Incident"
            and e["properties"].get("event_type") == "position"
        ]
        assert len(occurred) == 2
        dates = {e["to_match"]["date"] for e in occurred}
        assert dates == {"2026-03-01", "2026-06-15"}
        details = {e["properties"]["detail"] for e in occurred}
        assert details == {"زیر تفتیش", "چالان مکمل"}

    async def test_fir_position_row_with_no_status_date_gets_no_occurred_on(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(fir_position=[{"id": "fp1", "position": "زیر تفتیش"}])
        await sp.project_fir(fir)
        assert not any(
            e["edge_label"] == "OCCURRED_ON" and e["properties"].get("event_type") == "position"
            for e in graph_calls["edges"]
        )
        # still captured as a StructuredRecord even without a date.
        assert any(
            n["label"] == "StructuredRecord" and n["properties"].get("record_type") == "fir_position"
            for n in graph_calls["nodes"]
        )


class TestWitnessHomeJurisdiction:
    """Milestone C6 — fir_witness.police_station_of_residence_id/
    other_district -> a LOCATED_AT-style edge to the witness's home
    PoliceStation/District, reusing B1's exact identity keys."""

    async def test_police_station_of_residence_id_writes_located_at_to_police_station(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(fir_witness=[{
            "id": "w1", "full_name": "گواہ ایک", "cnic": "00000-3000000-1",
            "police_station_of_residence_id": "PS-FSD-CIVILLINES",
        }])
        await sp.project_fir(fir)

        located = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "LOCATED_AT" and e["to_label"] == "PoliceStation"
        ]
        assert len(located) == 1
        assert located[0]["to_match"] == {"station_id": "PS-FSD-CIVILLINES"}
        assert located[0]["from_label"] == "Person"

        station_nodes = [n for n in graph_calls["nodes"] if n["label"] == "PoliceStation"]
        assert any(n["match"] == {"station_id": "PS-FSD-CIVILLINES"} for n in station_nodes)

    async def test_other_district_writes_located_at_to_district_when_no_station_id(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(fir_witness=[{
            "id": "w1", "full_name": "گواہ ایک", "cnic": "00000-3000000-1",
            "other_district": "Multan",
        }])
        await sp.project_fir(fir)

        located = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "LOCATED_AT" and e["to_label"] == "District"
        ]
        assert len(located) == 1
        assert located[0]["to_match"] == {"district_id": "Multan"}

    async def test_station_id_takes_priority_over_other_district(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(fir_witness=[{
            "id": "w1", "full_name": "گواہ ایک", "cnic": "00000-3000000-1",
            "police_station_of_residence_id": "PS-FSD-CIVILLINES", "other_district": "Multan",
        }])
        await sp.project_fir(fir)

        assert any(
            e["edge_label"] == "LOCATED_AT" and e["to_label"] == "PoliceStation" for e in graph_calls["edges"]
        )
        assert not any(
            e["edge_label"] == "LOCATED_AT" and e["to_label"] == "District" for e in graph_calls["edges"]
        )

    async def test_neither_field_writes_no_home_jurisdiction_edge(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(fir_witness=[{"id": "w1", "full_name": "گواہ ایک", "cnic": "00000-3000000-1"}])
        await sp.project_fir(fir)
        assert not any(
            e["edge_label"] == "LOCATED_AT" and e["to_label"] in ("PoliceStation", "District")
            for e in graph_calls["edges"]
        )

    async def test_home_station_written_with_empty_properties_never_clobbers_filing_station(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        """A witness's home station write must never overwrite the real
        name/code B1's own _write_jurisdiction() already populated for
        this same station (write_node()'s SET-per-key semantics make an
        empty properties dict a pure no-op on the property side)."""
        fir = _minimal_fir(
            police_station={"id": "PS-1", "name": "PS Test", "code": "T1"},
            fir_witness=[{
                "id": "w1", "full_name": "گواہ ایک", "cnic": "00000-3000000-1",
                "police_station_of_residence_id": "PS-1",
            }],
        )
        await sp.project_fir(fir)
        station_writes = [n for n in graph_calls["nodes"] if n["label"] == "PoliceStation"]
        # One write with real properties (the case's own filing station),
        # one write with an empty properties dict (the witness home link).
        assert any(n["properties"].get("name") == "PS Test" for n in station_writes)
        assert any(n["properties"] == {} for n in station_writes)


class TestTypedRecoveredProperty:
    """Milestone C5 — malkhana_register.item_detail classified at write
    time; a plate/phone-shaped value resolves into Vehicle/PhoneNumber
    instead of a generic StructuredRecord."""

    async def test_plate_shaped_detail_resolves_to_vehicle(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(malkhana_register=[{"id": "m1", "item_detail": "ICT-LE-309 برآمد"}])
        stats = await sp.project_fir(fir)

        vehicle_calls = [c for c in fake_resolve_and_write if c["entity_type"] == "vehicle"]
        assert len(vehicle_calls) == 1
        assert vehicle_calls[0]["mention"]["plate"] == "ICT-LE-309"

        appears = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "APPEARS_IN" and e["properties"].get("role") == "recovered"
        ]
        assert len(appears) == 1
        assert appears[0]["properties"]["surface_text"] == "ICT-LE-309 برآمد"
        assert appears[0]["from_label"] == "Vehicle"

        # No generic StructuredRecord for this row — "instead of," not
        # "in addition to."
        assert not any(
            n["label"] == "StructuredRecord" and n["match"].get("record_id") == "malkhana_register:m1"
            for n in graph_calls["nodes"]
        )
        assert stats["structured_records"] == 0

    async def test_phone_shaped_detail_resolves_to_phone_number(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(malkhana_register=[{"id": "m1", "item_detail": "0300-1234567 نمبر برآمد"}])
        await sp.project_fir(fir)

        phone_calls = [c for c in fake_resolve_and_write if c["entity_type"] == "phone"]
        assert len(phone_calls) == 1
        assert phone_calls[0]["mention"]["phone"] == "0300-1234567"

        appears = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "APPEARS_IN" and e["properties"].get("role") == "recovered"
        ]
        assert len(appears) == 1
        assert appears[0]["from_label"] == "PhoneNumber"

    async def test_unshaped_detail_stays_a_generic_structured_record(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(malkhana_register=[{"id": "m1", "item_detail": "نقدی رقم"}])
        stats = await sp.project_fir(fir)

        assert not any(c["entity_type"] in ("vehicle", "phone") for c in fake_resolve_and_write)
        sr_nodes = [n for n in graph_calls["nodes"] if n["label"] == "StructuredRecord"]
        assert any(n["match"] == {"record_id": "malkhana_register:m1"} for n in sr_nodes)
        assert stats["structured_records"] == 1

    async def test_plate_takes_priority_when_both_would_match(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        """A detail string containing both a plate and a phone shape
        classifies as Vehicle — plate checked first, deterministic, not a
        coin-flip between the two detectors."""
        fir = _minimal_fir(malkhana_register=[
            {"id": "m1", "item_detail": "ICT-LE-309 اور رابطہ نمبر 0300-1234567"},
        ])
        await sp.project_fir(fir)
        assert any(c["entity_type"] == "vehicle" for c in fake_resolve_and_write)
        assert not any(c["entity_type"] == "phone" for c in fake_resolve_and_write)


class TestChalaanNameResolution:
    """Milestone C2 — chalaan_dispatch.accused_names/witness_names resolved
    back to this FIR's own Person nodes, reusing the same in-FIR-only
    name-matching pattern weapon_register.recovered_from already uses."""

    async def test_matched_accused_and_witness_names_get_appears_in(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_accused=[{"id": "a1", "full_name": "طارق", "cnic": "00000-2000000-1"}],
            fir_witness=[{"id": "w1", "full_name": "وقاص", "cnic": "00000-3000000-1"}],
            chalaan_dispatch=[{"id": "cd1", "accused_names": "طارق", "witness_names": "وقاص"}],
        )
        await sp.project_fir(fir)
        appears = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "APPEARS_IN" and e["from_label"] == "Person"
            and e["to_label"] == "StructuredRecord"
        ]
        assert len(appears) == 2
        roles = {e["properties"]["role"] for e in appears}
        assert roles == {"chalaan_accused", "chalaan_witness"}
        assert all(e["to_match"] == {"record_id": "chalaan_dispatch:cd1"} for e in appears)

    async def test_comma_separated_names_both_urdu_and_ascii_comma(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_accused=[
                {"id": "a1", "full_name": "طارق", "cnic": "00000-2000000-1"},
                {"id": "a2", "full_name": "عدنان", "cnic": "00000-2000000-2"},
            ],
            chalaan_dispatch=[{"id": "cd1", "accused_names": "طارق، عدنان"}],
        )
        await sp.project_fir(fir)
        appears = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "APPEARS_IN" and e["properties"].get("role") == "chalaan_accused"
        ]
        assert len(appears) == 2
        assert {e["properties"]["surface_text"] for e in appears} == {"طارق", "عدنان"}

    async def test_name_not_matching_any_in_fir_accused_is_left_unresolved(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_accused=[{"id": "a1", "full_name": "طارق", "cnic": "00000-2000000-1"}],
            chalaan_dispatch=[{"id": "cd1", "accused_names": "کوئی اور نام"}],
        )
        await sp.project_fir(fir)
        appears = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "APPEARS_IN" and e["properties"].get("role") == "chalaan_accused"
        ]
        assert appears == []

    async def test_no_chalaan_dispatch_rows_writes_no_name_links(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(
            fir_accused=[{"id": "a1", "full_name": "طارق", "cnic": "00000-2000000-1"}],
        )
        await sp.project_fir(fir)
        appears = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "APPEARS_IN"
            and e["properties"].get("role") in ("chalaan_accused", "chalaan_witness")
        ]
        assert appears == []


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


# ── findings.md T1/T2 — projection defects found against the real corpus ──


def _incident_node(graph_calls) -> dict:
    return next(n for n in graph_calls["nodes"] if n["label"] == "Incident")


def _zimni_edges(graph_calls) -> list[dict]:
    return [
        e for e in graph_calls["edges"]
        if e["edge_label"] == "OCCURRED_ON"
        and e["properties"].get("event_type") == "zimni_entry"
    ]


class TestIncidentDescriptionFromNarrative:
    """
    Regression: T1. Incident nodes carried only canonical_name, so
    Timeline Building — which reads Incident.description as its event
    text — rendered "Incident {id} (no description recorded)" for all 73
    real cases even though every FIR carries a non-empty narrative_text.
    """

    async def test_narrative_text_becomes_incident_description(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(narrative_text="مدعی نے بیان دیا کہ موٹرسائیکل چوری ہوئی۔")
        await sp.project_fir(fir)
        incident = _incident_node(graph_calls)
        assert incident["properties"]["description"] == "مدعی نے بیان دیا کہ موٹرسائیکل چوری ہوئی۔"

    async def test_narrative_is_copied_verbatim_never_summarized(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        """The whole point of a graph-derived description: no model touches it."""
        narrative = "A" * 4000
        fir = _minimal_fir(narrative_text=narrative)
        await sp.project_fir(fir)
        assert _incident_node(graph_calls)["properties"]["description"] == narrative

    async def test_absent_narrative_writes_no_description_property(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        """"Unavailable" must stay distinguishable from "recorded as blank"."""
        fir = _minimal_fir()  # no narrative_text key at all
        await sp.project_fir(fir)
        assert "description" not in _incident_node(graph_calls)["properties"]

    async def test_blank_narrative_writes_no_description_property(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(narrative_text="   ")
        await sp.project_fir(fir)
        assert "description" not in _incident_node(graph_calls)["properties"]

    async def test_canonical_name_is_unchanged_by_the_description_addition(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(narrative_text="narrative")
        await sp.project_fir(fir)
        assert _incident_node(graph_calls)["properties"]["canonical_name"] == "Incident for FIR 100/26"


class TestZimniDetailNeverStringifiesNone:
    """
    Regression: T2. Both zimni producers built detail as
    f"entry {z.get('entry_number')}", so a NULL entry_number wrote the
    literal "entry None" — 188 such edges exist in the restored graph, and
    Timeline Building renders detail verbatim to investigators.
    """

    async def test_present_entry_number_keeps_existing_representation(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(fir_zimni=[
            {"id": "z1", "entry_number": 7, "entry_date": "2026-02-01", "officer_name": "SI احمد"},
        ])
        await sp.project_fir(fir)
        edges = _zimni_edges(graph_calls)
        assert edges, "expected at least one zimni OCCURRED_ON edge"
        assert all(e["properties"]["detail"] == "entry 7" for e in edges)

    async def test_null_entry_number_omits_detail_entirely(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(fir_zimni=[
            {"id": "z1", "entry_number": None, "entry_date": "2026-02-01", "officer_name": "SI احمد"},
        ])
        await sp.project_fir(fir)
        edges = _zimni_edges(graph_calls)
        assert edges, "expected at least one zimni OCCURRED_ON edge"
        for e in edges:
            assert "detail" not in e["properties"]
            assert "None" not in str(e["properties"].get("detail", ""))

    async def test_both_producers_are_covered_incident_and_officer(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        """
        The defect existed twice (Incident-side and Officer-side zimni
        edges). Assert BOTH from_labels are exercised, so a future fix to
        only one site fails here rather than silently leaving half the
        edges defective.
        """
        fir = _minimal_fir(fir_zimni=[
            {"id": "z1", "entry_number": None, "entry_date": "2026-02-01", "officer_name": "SI احمد"},
        ])
        await sp.project_fir(fir)
        from_labels = {e["from_label"] for e in _zimni_edges(graph_calls)}
        assert from_labels == {"Incident", "Officer"}, from_labels

    async def test_entry_number_zero_is_kept_not_treated_as_missing(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        """A falsy-but-real entry number must survive — the guard is `is not None`."""
        fir = _minimal_fir(fir_zimni=[
            {"id": "z1", "entry_number": 0, "entry_date": "2026-02-01", "officer_name": "SI احمد"},
        ])
        await sp.project_fir(fir)
        assert all(e["properties"]["detail"] == "entry 0" for e in _zimni_edges(graph_calls))

    async def test_event_type_and_edge_shape_are_unchanged(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        fir = _minimal_fir(fir_zimni=[
            {"id": "z1", "entry_number": None, "entry_date": "2026-02-01", "officer_name": "SI احمد"},
        ])
        await sp.project_fir(fir)
        for e in _zimni_edges(graph_calls):
            assert e["edge_label"] == "OCCURRED_ON"
            assert e["properties"]["event_type"] == "zimni_entry"
            assert e["to_match"] == {"date": "2026-02-01"}
