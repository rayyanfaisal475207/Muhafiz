"""
M6b of the Muhafiz Data API migration (docs/decisions/0001-muhafiz-api-migration.md) —
src/graph/cross_silo_projection.py.

No real AGE — src.graph.versioning and src.graph.entity_resolution are
monkeypatched, same approach as tests/test_structured_projection.py.
"""
import json
from pathlib import Path

import pytest

from src.data_gateway.muhafiz_api.models import CmsComplaint, CriminalRecord, FirRecord, PkmApplication
from src.graph import cross_silo_projection as csp, entity_resolution, versioning

FIXTURE = Path(__file__).parent / "fixtures" / "muhafiz_api_snapshot.json"


@pytest.fixture(scope="module")
def snapshot():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def firs(snapshot):
    return [FirRecord(r) for r in snapshot["endpoints"]["fir"]]


@pytest.fixture
def graph_calls(monkeypatch):
    calls = {"nodes": [], "edges": []}

    async def fake_write_node(label, match, properties=None, *, source_doc_id=None, confidence=1.0, graph=None):
        calls["nodes"].append({"label": label, "match": match, "properties": properties or {}})
        return {"id": len(calls["nodes"]), "label": label, "properties": {**match, **(properties or {})}}

    async def fake_write_edge(edge_label, from_label, from_match, to_label, to_match,
                               properties=None, *, source_doc_id, source_chunk_id=None,
                               confidence=1.0, supersedes_edge_id=None, graph=None):
        # Simulate "endpoint doesn't exist" -> None, unless the test opts in.
        if calls.get("missing_endpoints") and to_match.get("case_id") in calls["missing_endpoints"]:
            return None
        calls["edges"].append({
            "edge_label": edge_label, "from_label": from_label, "from_match": from_match,
            "to_label": to_label, "to_match": to_match, "properties": properties or {},
        })
        return {"id": len(calls["edges"]) + 1, "label": edge_label, "properties": properties or {}}

    monkeypatch.setattr(versioning, "write_node", fake_write_node)
    monkeypatch.setattr(versioning, "write_edge", fake_write_edge)
    return calls


@pytest.fixture
def no_candidates_by_default(monkeypatch):
    async def fake(label, mention, case_id, id_key=None, *, graph=None):
        return []
    monkeypatch.setattr(entity_resolution, "_generate_candidates", fake)


@pytest.fixture
def fake_resolve_and_write(monkeypatch):
    calls = []

    async def fake(entity_type, mention, case_id, source_doc_id, source_chunk_id=None, *, graph=None):
        entity_id = f"{entity_type.upper()}-{len(calls)}"
        calls.append({"entity_type": entity_type, "mention": mention, "case_id": case_id})
        return {"entity_id": entity_id, "tier": "new", "confidence": 1.0, "basis": "test",
                "is_new_node": True, "candidates_considered": 0}

    monkeypatch.setattr(entity_resolution, "resolve_and_write", fake)
    return calls


@pytest.fixture
def fake_find_by_primary_id(monkeypatch):
    """Returns None (no existing Person) unless a test sets .result."""
    state = {"result": None}

    async def fake(label, id_key, id_value, *, graph=None):
        return state["result"]

    monkeypatch.setattr(entity_resolution, "_find_by_primary_id", fake)
    return state


# ── CMS ──────────────────────────────────────────────────────────────────

class TestProjectCmsComplaint:
    async def test_unlinked_complaint_writes_structured_record_only(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        cms = CmsComplaint({"complaint_id": "C1", "case_tag_number": "TAG-X"})
        stats = await csp.project_cms_complaint(cms, case_id=None)

        assert stats["structured_records"] == 1
        assert not any(e["edge_label"] == "BELONGS_TO_CASE" for e in graph_calls["edges"])
        assert not fake_resolve_and_write, "no case -> no person resolution attempted"
        assert stats["errors"] == []

    async def test_linked_complaint_writes_belongs_to_case_and_resolves_complainant(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        cms = CmsComplaint({
            "complaint_id": "C1", "case_tag_number": "TAG-X",
            "complainant": {"full_name": "صبا", "cnic": "00000-9000100-1"},
        })
        stats = await csp.project_cms_complaint(cms, case_id="fir-341-26")

        assert any(
            e["edge_label"] == "BELONGS_TO_CASE" and e["to_match"] == {"case_id": "fir-341-26"}
            for e in graph_calls["edges"]
        )
        assert stats["persons_resolved"] == 1
        involved = [e for e in graph_calls["edges"] if e["edge_label"] == "INVOLVED_IN"]
        assert any(e["properties"]["role"] == "complainant_cms" for e in involved)

    async def test_no_complainant_name_skips_person_resolution(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        cms = CmsComplaint({"complaint_id": "C1", "complainant": {}})
        await csp.project_cms_complaint(cms, case_id="fir-1-26")
        assert not fake_resolve_and_write


# ── PKM ──────────────────────────────────────────────────────────────────

class TestProjectPkmApplication:
    async def test_unlinked_application_writes_structured_record_only(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        pkm = PkmApplication({"application_id": "P1", "service_type": "vehicle_verification"})
        stats = await csp.project_pkm_application(pkm, case_id=None)

        assert stats["structured_records"] == 1
        assert not any(e["edge_label"] == "BELONGS_TO_CASE" for e in graph_calls["edges"])
        assert not fake_resolve_and_write

    async def test_linked_application_resolves_applicant(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        pkm = PkmApplication({
            "application_id": "P1", "service_type": "women_violence_report",
            "applicant": {"full_name": "X", "cnic": "00000-1-1"},
        })
        stats = await csp.project_pkm_application(pkm, case_id="fir-97-26")
        assert stats["persons_resolved"] == 1
        involved = [e for e in graph_calls["edges"] if e["edge_label"] == "INVOLVED_IN"]
        assert any(e["properties"]["role"] == "applicant_pkm" for e in involved)


# ── criminal records ──────────────────────────────────────────────────────

class TestProjectCriminalRecord:
    async def test_no_matching_person_still_writes_structured_record(
        self, graph_calls, fake_find_by_primary_id,
    ):
        record = CriminalRecord({"id": "CR1", "subject_cnic": "00000-9-1", "subject_full_name": "X"})
        stats = await csp.project_criminal_record(record)

        assert stats["structured_records"] == 1
        assert stats["linked_to_existing_person"] is False
        assert not any(
            e["edge_label"] == "APPEARS_IN" and e["from_label"] == "Person"
            for e in graph_calls["edges"]
        )

    async def test_matching_person_gets_direct_appears_in_edge_no_case_needed(
        self, graph_calls, fake_find_by_primary_id,
    ):
        fake_find_by_primary_id["result"] = {"properties": {"entity_id": "PERSON-EXISTING"}}
        record = CriminalRecord({"id": "CR1", "subject_cnic": "00000-9-1", "subject_full_name": "X"})

        stats = await csp.project_criminal_record(record)

        assert stats["linked_to_existing_person"] is True
        person_edges = [
            e for e in graph_calls["edges"]
            if e["edge_label"] == "APPEARS_IN" and e["from_label"] == "Person"
        ]
        assert len(person_edges) == 1
        assert person_edges[0]["from_match"] == {"entity_id": "PERSON-EXISTING"}

    async def test_no_subject_cnic_skips_person_lookup_entirely(self, graph_calls, monkeypatch):
        called = []

        async def fake_find(*args, **kwargs):
            called.append(1)
            return None
        monkeypatch.setattr(entity_resolution, "_find_by_primary_id", fake_find)

        record = CriminalRecord({"id": "CR1", "subject_full_name": "X"})  # no subject_cnic
        await csp.project_criminal_record(record)

        assert not called


# ── FIR -> FIR prose citations ────────────────────────────────────────────

class TestFindCitedDisplayCodes:
    def test_finds_a_citation_in_narrative(self):
        fir = FirRecord({
            "fir_id": "fir-424-26", "fir_display_code": "424/26",
            "narrative_text": "یہ واقعہ مقدمہ نمبر 423/26 سے متعلق ہے۔",
        })
        found = csp.find_cited_display_codes(fir, known_codes={"423/26", "424/26"})
        assert found == [("423/26", "narrative")]

    def test_never_cites_itself(self):
        fir = FirRecord({
            "fir_id": "fir-424-26", "fir_display_code": "424/26",
            "narrative_text": "مقدمہ نمبر 424/26 خود اس بیانیہ میں",
        })
        found = csp.find_cited_display_codes(fir, known_codes={"424/26"})
        assert found == []

    def test_ignores_a_code_not_in_the_known_set(self):
        """A number that merely LOOKS like NNN/YY but isn't a real FIR
        display code (e.g. a date fragment) must not be surfaced."""
        fir = FirRecord({
            "fir_id": "fir-1-26", "fir_display_code": "1/26",
            "narrative_text": "کچھ 999/99 کا ذکر جو حقیقی نہیں",
        })
        found = csp.find_cited_display_codes(fir, known_codes={"1/26"})
        assert found == []

    def test_dedupes_the_same_code_cited_twice(self):
        fir = FirRecord({
            "fir_id": "fir-2-26", "fir_display_code": "2/26",
            "narrative_text": "1/26 اور دوبارہ 1/26 کا ذکر",
        })
        found = csp.find_cited_display_codes(fir, known_codes={"1/26", "2/26"})
        assert found == [("1/26", "narrative")]

    def test_scans_zimni_and_chalaan_fields_too(self):
        fir = FirRecord({
            "fir_id": "fir-3-26", "fir_display_code": "3/26",
            "fir_zimni": [{"entry_number": 1, "entry_text": "متعلقہ مقدمہ 1/26"}],
            "chalaan_dispatch": [{"id": "cd1", "property_involved": "دیکھیں مقدمہ 2/26"}],
        })
        found = csp.find_cited_display_codes(fir, known_codes={"1/26", "2/26", "3/26"})
        assert set(found) == {("1/26", "zimni_entry_1"), ("2/26", "chalaan_property_cd1")}

    def test_measured_count_against_real_snapshot(self, firs):
        """Locks in the measured finding from the decision record: exactly
        9 FIRs cite another real FIR in prose."""
        known_codes = {f.fir_display_code for f in firs if f.fir_display_code}
        citing_firs = [f for f in firs if csp.find_cited_display_codes(f, known_codes)]
        assert len(citing_firs) == 9


class TestAgainstRealSnapshotEndToEnd:
    """
    Wires M4's resolvers (build_e_tag_index/build_display_code_index/
    resolve_cms_case_id/resolve_pkm_case_id) into M6b's projection
    functions over the full real dataset — the actual shape M9's sync
    script will run.
    """

    async def test_every_cms_and_pkm_record_projects_without_raising(
        self, snapshot, firs, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        from src.ingestion.muhafiz_cases import (
            build_e_tag_index, build_display_code_index, resolve_cms_case_id, resolve_pkm_case_id,
        )

        e_tag_index = build_e_tag_index(firs)
        display_code_index = build_display_code_index(firs)

        cms_linked = 0
        for raw in snapshot["endpoints"]["cms"]:
            cms = CmsComplaint(raw)
            case_id = resolve_cms_case_id(cms, e_tag_index)
            stats = await csp.project_cms_complaint(cms, case_id)
            assert stats["errors"] == []
            if case_id:
                cms_linked += 1
        assert cms_linked == 4  # measured

        pkm_linked = 0
        for raw in snapshot["endpoints"]["pkm"]:
            pkm = PkmApplication(raw)
            case_id = resolve_pkm_case_id(pkm, display_code_index)
            stats = await csp.project_pkm_application(pkm, case_id)
            assert stats["errors"] == []
            if case_id:
                pkm_linked += 1
        assert pkm_linked == 4  # measured

    async def test_every_criminal_record_projects_without_raising(
        self, snapshot, graph_calls, fake_find_by_primary_id,
    ):
        for raw in snapshot["endpoints"]["criminal-records"]:
            record = CriminalRecord(raw)
            stats = await csp.project_criminal_record(record)
            assert stats["errors"] == []


class TestProjectFirCitations:
    async def test_writes_pending_cites_edge(self, graph_calls):
        firs_by_code = {"423/26": "fir-423-26", "424/26": "fir-424-26"}
        fir = FirRecord({
            "fir_id": "fir-424-26", "fir_display_code": "424/26",
            "narrative_text": "متعلقہ مقدمہ 423/26",
        })
        stats = await csp.project_fir_citations(fir, firs_by_code, {"423/26", "424/26"})

        assert stats["cites_written"] == 1
        cites = [e for e in graph_calls["edges"] if e["edge_label"] == "CITES"]
        assert len(cites) == 1
        assert cites[0]["from_match"] == {"case_id": "fir-424-26"}
        assert cites[0]["to_match"] == {"case_id": "fir-423-26"}
        assert cites[0]["properties"]["status"] == "pending"

    async def test_never_writes_confirmed_or_rejected_directly(self, graph_calls):
        firs_by_code = {"1/26": "fir-1-26", "2/26": "fir-2-26"}
        fir = FirRecord({
            "fir_id": "fir-2-26", "fir_display_code": "2/26", "narrative_text": "1/26",
        })
        await csp.project_fir_citations(fir, firs_by_code, {"1/26", "2/26"})
        cites = [e for e in graph_calls["edges"] if e["edge_label"] == "CITES"]
        assert all(e["properties"]["status"] == "pending" for e in cites)

    async def test_missing_target_case_degrades_without_raising(self, graph_calls):
        """The cited FIR's Case node hasn't been M6a-projected yet —
        write_edge's own 'endpoint not found' tolerance (simulated here)
        must not raise or abort the rest of the sweep."""
        graph_calls["missing_endpoints"] = {"fir-423-26"}
        firs_by_code = {"423/26": "fir-423-26", "424/26": "fir-424-26"}
        fir = FirRecord({
            "fir_id": "fir-424-26", "fir_display_code": "424/26",
            "narrative_text": "متعلقہ مقدمہ 423/26",
        })
        stats = await csp.project_fir_citations(fir, firs_by_code, {"423/26", "424/26"})
        assert stats["cites_written"] == 0
        assert stats["errors"] == []
