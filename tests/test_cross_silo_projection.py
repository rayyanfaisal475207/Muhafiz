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
from src.graph import age_client, cross_silo_projection as csp, entity_resolution, versioning

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
            "source_doc_id": source_doc_id, "confidence": confidence,
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


@pytest.fixture
def fake_incident_roster(monkeypatch):
    """
    Stubs age_client.execute_cypher for
    _pair_with_existing_incident_roster()'s two reads — the only
    age_client calls this module makes: (1) "who's already INVOLVED_IN
    this Incident" and (2) "which of those is the new person already
    ASSOCIATED_WITH" (the CNIC-auto-merge dedup check). Tests set
    `.roster` (entity_ids INVOLVED_IN the Incident) and `.already_paired`
    (entity_ids to report as already-linked, default empty) to control
    each independently. Differentiated by matching each query's own
    Cypher text — the two are structurally distinct, not by convention.
    """
    state = {"roster": [], "already_paired": []}

    async def fake(cypher_query, params=None, columns=("result",), graph=None):
        if "INVOLVED_IN" in cypher_query:
            return [{"entity_id": eid} for eid in state["roster"]]
        return [{"entity_id": eid} for eid in state["already_paired"]]

    monkeypatch.setattr(age_client, "execute_cypher", fake)
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
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_incident_roster,
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

    # ── findings.md Module 1 follow-up (MODULE1_GAPS_FIX_PROMPT.md Priority 1) ──

    async def test_complainant_paired_with_existing_incident_roster(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_incident_roster,
    ):
        fake_incident_roster["roster"] = ["PERSON-EXISTING-1", "PERSON-EXISTING-2"]
        cms = CmsComplaint({
            "complaint_id": "C1", "case_tag_number": "TAG-X",
            "complainant": {"full_name": "صبا", "cnic": "00000-9000100-1"},
        })
        stats = await csp.project_cms_complaint(cms, case_id="fir-341-26")

        assert fake_resolve_and_write[0]["entity_type"] == "person"
        new_entity_id = "PERSON-0"  # fake_resolve_and_write's first-call id, CNIC present so it's used
        assoc = [e for e in graph_calls["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
        assert len(assoc) == 2  # paired with both existing roster members, never each other
        paired_ids = {e["to_match"]["entity_id"] for e in assoc}
        assert paired_ids == {"PERSON-EXISTING-1", "PERSON-EXISTING-2"}
        assert all(e["from_match"]["entity_id"] == new_entity_id for e in assoc)
        for e in assoc:
            assert e["properties"]["basis"] == "co-mentioned in case fir-341-26's incident"
            assert e["confidence"] == 0.5
            # Tagged with the CMS record's OWN doc_id, not the FIR's — so a
            # re-sync of just this record purges/re-derives just these pairs.
            assert e["source_doc_id"] == "cms/complaint/C1#structured"
        assert stats["edges_written"] >= 2

    async def test_empty_existing_roster_writes_no_associated_with(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_incident_roster,
    ):
        cms = CmsComplaint({
            "complaint_id": "C1", "case_tag_number": "TAG-X",
            "complainant": {"full_name": "صبا", "cnic": "00000-9000100-1"},
        })
        await csp.project_cms_complaint(cms, case_id="fir-341-26")
        assert not any(e["edge_label"] == "ASSOCIATED_WITH" for e in graph_calls["edges"])

    async def test_never_self_pairs_when_roster_read_echoes_the_new_person(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_incident_roster,
    ):
        # The Cypher read happens AFTER this record's own INVOLVED_IN write,
        # so in reality it would include the just-written person too — make
        # sure that never produces a self-pair.
        fake_incident_roster["roster"] = ["PERSON-0", "PERSON-EXISTING-1"]
        cms = CmsComplaint({
            "complaint_id": "C1", "case_tag_number": "TAG-X",
            "complainant": {"full_name": "صبا", "cnic": "00000-9000100-1"},
        })
        await csp.project_cms_complaint(cms, case_id="fir-341-26")
        assoc = [e for e in graph_calls["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
        assert len(assoc) == 1
        assert assoc[0]["to_match"]["entity_id"] == "PERSON-EXISTING-1"

    async def test_unlinked_complaint_never_calls_incident_roster_pairing(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_incident_roster,
    ):
        cms = CmsComplaint({"complaint_id": "C1", "case_tag_number": "TAG-X"})
        await csp.project_cms_complaint(cms, case_id=None)
        assert not any(e["edge_label"] == "ASSOCIATED_WITH" for e in graph_calls["edges"])

    async def test_cnic_auto_merge_onto_an_already_paired_person_skips_the_redundant_edge(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_incident_roster,
    ):
        """
        Live-caught regression (fir-417-26): when the CMS complainant
        CNIC-auto-merges onto a Person the FIR's own _write_associated_with()
        already paired with someone else, that pair must NOT get a second,
        redundant ASSOCIATED_WITH edge just because a cross-silo record
        happened to resolve to the same existing entity_id. A genuinely
        unpaired roster member must still get paired normally.
        """
        fake_incident_roster["roster"] = ["PERSON-ALREADY-PAIRED", "PERSON-NOT-YET-PAIRED"]
        fake_incident_roster["already_paired"] = ["PERSON-ALREADY-PAIRED"]
        cms = CmsComplaint({
            "complaint_id": "C1", "case_tag_number": "TAG-X",
            "complainant": {"full_name": "صبا", "cnic": "00000-9000100-1"},
        })
        await csp.project_cms_complaint(cms, case_id="fir-341-26")

        assoc = [e for e in graph_calls["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
        assert len(assoc) == 1
        assert assoc[0]["to_match"]["entity_id"] == "PERSON-NOT-YET-PAIRED"


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
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_incident_roster,
    ):
        pkm = PkmApplication({
            "application_id": "P1", "service_type": "women_violence_report",
            "applicant": {"full_name": "X", "cnic": "00000-1-1"},
        })
        stats = await csp.project_pkm_application(pkm, case_id="fir-97-26")
        assert stats["persons_resolved"] == 1
        involved = [e for e in graph_calls["edges"] if e["edge_label"] == "INVOLVED_IN"]
        assert any(e["properties"]["role"] == "applicant_pkm" for e in involved)

    # ── findings.md Module 1 follow-up (MODULE1_GAPS_FIX_PROMPT.md Priority 1) ──

    async def test_applicant_paired_with_existing_incident_roster(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_incident_roster,
    ):
        fake_incident_roster["roster"] = ["PERSON-EXISTING-1"]
        pkm = PkmApplication({
            "application_id": "P1", "service_type": "women_violence_report",
            "applicant": {"full_name": "X", "cnic": "00000-1-1"},
        })
        stats = await csp.project_pkm_application(pkm, case_id="fir-97-26")

        assoc = [e for e in graph_calls["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
        assert len(assoc) == 1
        assert assoc[0]["to_match"]["entity_id"] == "PERSON-EXISTING-1"
        assert assoc[0]["from_match"]["entity_id"] == "PERSON-0"
        assert assoc[0]["properties"]["basis"] == "co-mentioned in case fir-97-26's incident"
        assert assoc[0]["confidence"] == 0.5
        assert assoc[0]["source_doc_id"] == "pkm/application/P1#structured"
        assert stats["edges_written"] >= 1


class TestAssociatedWithAcrossMultipleCrossSiloRecords:
    """
    findings.md Module 1 follow-up (MODULE1_GAPS_FIX_PROMPT.md Priority 1)
    — _pair_with_existing_incident_roster() reads the CURRENT roster at
    call time rather than one frozen at some earlier point, so a second
    cross-silo record landing on the same case later pairs correctly with
    BOTH the FIR's own roster and any cross-silo record(s) already
    projected — not just with the FIR.
    """

    async def test_second_cross_silo_record_pairs_with_first_cross_silo_record_too(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_incident_roster,
    ):
        # Simulate: the FIR's own roster (1 person) plus a CMS complaint
        # already projected on this case (its complainant now INVOLVED_IN
        # the Incident too) — a PKM application projects next and should
        # pair with both.
        fake_incident_roster["roster"] = ["PERSON-FIR-1", "PERSON-CMS-COMPLAINANT"]
        pkm = PkmApplication({
            "application_id": "P1", "service_type": "women_violence_report",
            "applicant": {"full_name": "X", "cnic": "00000-1-1"},
        })
        await csp.project_pkm_application(pkm, case_id="fir-97-26")

        assoc = [e for e in graph_calls["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
        assert len(assoc) == 2
        paired_ids = {e["to_match"]["entity_id"] for e in assoc}
        assert paired_ids == {"PERSON-FIR-1", "PERSON-CMS-COMPLAINANT"}


class TestProjectPkmVehicleVerification:
    """
    REGISTERED_TO — the fifth of the five migration-005 edge types with no
    writer before this fix. Never case-scoped: resolve_pkm_case_id() only
    ever matches women_violence_report, so vehicle_verification always
    gets case_id None here, same as it does live.
    """

    def _vehicle_pkm(self, **service_overrides):
        service = {
            "vehicle_registration_no": "FSD-19-8842", "vehicle_make": "Honda",
            "vehicle_model": "CD-125", **service_overrides,
        }
        return PkmApplication({
            "application_id": "P1", "service_type": "vehicle_verification",
            "applicant": {"full_name": "خرم شہزاد", "cnic": "00000-1000018-1"},
            "vehicle_verification": service,
        })

    async def test_no_case_still_writes_a_vehicle_node(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_find_by_primary_id,
    ):
        stats = await csp.project_pkm_application(self._vehicle_pkm(), case_id=None)

        assert stats["vehicles_written"] == 1
        vehicle_nodes = [n for n in graph_calls["nodes"] if n["label"] == "Vehicle"]
        assert len(vehicle_nodes) == 1
        assert vehicle_nodes[0]["properties"]["plate"] == "FSD-19-8842"
        assert vehicle_nodes[0]["match"] == {"entity_id": "VEHICLE-FSD-19-8842"}
        # no case at all -> no person resolution attempted either, matching
        # the existing case-less-application discipline above.
        assert not fake_resolve_and_write

    async def test_no_registration_number_skips_the_vehicle_write_entirely(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_find_by_primary_id,
    ):
        pkm = self._vehicle_pkm(vehicle_registration_no=None)
        stats = await csp.project_pkm_application(pkm, case_id=None)

        assert stats["vehicles_written"] == 0
        assert not any(n["label"] == "Vehicle" for n in graph_calls["nodes"])

    async def test_applicant_cnic_matching_an_existing_person_gets_registered_to(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_find_by_primary_id,
    ):
        fake_find_by_primary_id["result"] = {"properties": {"entity_id": "PERSON-EXISTING"}}
        await csp.project_pkm_application(self._vehicle_pkm(), case_id=None)

        registered = [e for e in graph_calls["edges"] if e["edge_label"] == "REGISTERED_TO"]
        assert len(registered) == 1
        assert registered[0]["from_label"] == "Vehicle"
        assert registered[0]["from_match"] == {"entity_id": "VEHICLE-FSD-19-8842"}
        assert registered[0]["to_match"] == {"entity_id": "PERSON-EXISTING"}

    async def test_no_matching_existing_person_writes_vehicle_but_no_edge(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_find_by_primary_id,
    ):
        await csp.project_pkm_application(self._vehicle_pkm(), case_id=None)

        assert not any(e["edge_label"] == "REGISTERED_TO" for e in graph_calls["edges"])
        assert any(n["label"] == "Vehicle" for n in graph_calls["nodes"])

    async def test_non_vehicle_service_type_never_writes_a_vehicle_node(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_find_by_primary_id,
    ):
        pkm = PkmApplication({
            "application_id": "P1", "service_type": "driving_license",
            "driving_license": {"license_number": "DL-1"},
        })
        await csp.project_pkm_application(pkm, case_id=None)
        assert not any(n["label"] == "Vehicle" for n in graph_calls["nodes"])

    async def test_measured_count_against_real_snapshot(self, snapshot):
        """Locks in how many of the real PKM applications are
        vehicle_verification with a registration number — a regression
        here means the join key or fixture drifted."""
        pkms = [PkmApplication(r) for r in snapshot["endpoints"]["pkm"]]
        with_plate = [
            p for p in pkms
            if p.service_type == "vehicle_verification"
            and (p.service_record() or {}).get("vehicle_registration_no")
        ]
        assert len(with_plate) >= 1


class TestProjectPkmRelationships:
    """
    Milestone C1 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — person-relationship
    edges): RELATED_TO{role} between the two nested people on a
    tenant_registration/employee_registration application. Not observed
    populated in the live dataset (0 of 14 real PKM applications are either
    service type) — tested against a constructed fixture, per
    PkmApplication.owner/tenant/employer/employee's own docstring on the
    inferred shape.
    """

    def _keyed_find(self, by_cnic: dict):
        async def fake(label, id_key, id_value, *, graph=None):
            return by_cnic.get(id_value)
        return fake

    async def test_tenant_registration_both_cnics_resolve_writes_related_to(
        self, monkeypatch, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        monkeypatch.setattr(entity_resolution, "_find_by_primary_id", self._keyed_find({
            "00000-1-1": {"properties": {"entity_id": "PERSON-OWNER"}},
            "00000-2-2": {"properties": {"entity_id": "PERSON-TENANT"}},
        }))
        pkm = PkmApplication({
            "application_id": "P1", "service_type": "tenant_registration",
            "tenant_registration": {"property_address": "x"},
            "owner": {"cnic": "00000-1-1", "full_name": "Owner"},
            "tenant": {"cnic": "00000-2-2", "full_name": "Tenant"},
        })
        stats = await csp.project_pkm_application(pkm, case_id=None)

        related = [e for e in graph_calls["edges"] if e["edge_label"] == "RELATED_TO"]
        assert len(related) == 1
        assert related[0]["from_match"] == {"entity_id": "PERSON-OWNER"}
        assert related[0]["to_match"] == {"entity_id": "PERSON-TENANT"}
        assert related[0]["properties"]["role"] == "landlord_of"
        assert stats["edges_written"] >= 1

    async def test_employee_registration_both_cnics_resolve_writes_related_to(
        self, monkeypatch, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        monkeypatch.setattr(entity_resolution, "_find_by_primary_id", self._keyed_find({
            "00000-3-3": {"properties": {"entity_id": "PERSON-EMPLOYER"}},
            "00000-4-4": {"properties": {"entity_id": "PERSON-EMPLOYEE"}},
        }))
        pkm = PkmApplication({
            "application_id": "P2", "service_type": "employee_registration",
            "employee_registration": {},
            "employer": {"cnic": "00000-3-3", "full_name": "Employer"},
            "employee": {"cnic": "00000-4-4", "full_name": "Employee"},
        })
        await csp.project_pkm_application(pkm, case_id=None)

        related = [e for e in graph_calls["edges"] if e["edge_label"] == "RELATED_TO"]
        assert len(related) == 1
        assert related[0]["from_match"] == {"entity_id": "PERSON-EMPLOYER"}
        assert related[0]["to_match"] == {"entity_id": "PERSON-EMPLOYEE"}
        assert related[0]["properties"]["role"] == "employer_of"

    async def test_one_side_unresolved_writes_no_related_to(
        self, monkeypatch, graph_calls, no_candidates_by_default, fake_resolve_and_write,
    ):
        monkeypatch.setattr(entity_resolution, "_find_by_primary_id", self._keyed_find({
            "00000-1-1": {"properties": {"entity_id": "PERSON-OWNER"}},
            # tenant's cnic never matches an existing Person.
        }))
        pkm = PkmApplication({
            "application_id": "P1", "service_type": "tenant_registration",
            "tenant_registration": {},
            "owner": {"cnic": "00000-1-1", "full_name": "Owner"},
            "tenant": {"cnic": "00000-2-2", "full_name": "Tenant"},
        })
        await csp.project_pkm_application(pkm, case_id=None)
        assert not any(e["edge_label"] == "RELATED_TO" for e in graph_calls["edges"])

    async def test_missing_cnic_on_either_side_writes_no_related_to(
        self, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_find_by_primary_id,
    ):
        fake_find_by_primary_id["result"] = {"properties": {"entity_id": "PERSON-X"}}
        pkm = PkmApplication({
            "application_id": "P1", "service_type": "tenant_registration",
            "tenant_registration": {},
            "owner": {"full_name": "Owner"},  # no cnic
            "tenant": {"cnic": "00000-2-2", "full_name": "Tenant"},
        })
        await csp.project_pkm_application(pkm, case_id=None)
        assert not any(e["edge_label"] == "RELATED_TO" for e in graph_calls["edges"])

    async def test_measured_count_against_real_snapshot(self, snapshot):
        """0 of 14 real PKM applications are tenant_registration or
        employee_registration — locks in that this remains a
        constructed-fixture-only path, same disclosure as C4's
        cross_version test."""
        pkms = [
            r for r in snapshot["endpoints"]["pkm"]
            if r.get("service_type") in ("tenant_registration", "employee_registration")
        ]
        assert pkms == []


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
        self, snapshot, firs, graph_calls, no_candidates_by_default, fake_resolve_and_write, fake_incident_roster,
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


class TestProjectFirCrossVersions:
    """
    Milestone C4 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — cross-version
    edge). 0 populated `cross_version` rows in the live dataset
    (muhafiz_schema.dbml.txt: "NOT OBSERVED as a populated tab") — tested
    against a constructed fixture, per project_fir_cross_versions()'s own
    docstring, mirroring TestProjectFirCitations's shape but asserting the
    written-directly, no-`pending` contract instead.
    """

    async def test_writes_direct_cross_version_of_edge(self, graph_calls):
        display_code_index = {"423/26": "fir-423-26", "424/26": "fir-424-26"}
        fir = FirRecord({
            "fir_id": "fir-424-26", "fir_display_code": "424/26",
            "cross_version": [{"id": "cv1", "related_fir_display_code": "423/26", "filed_by": "accused side"}],
        })
        stats = await csp.project_fir_cross_versions(fir, display_code_index)

        assert stats["cross_version_written"] == 1
        edges = [e for e in graph_calls["edges"] if e["edge_label"] == "CROSS_VERSION_OF"]
        assert len(edges) == 1
        assert edges[0]["from_match"] == {"case_id": "fir-424-26"}
        assert edges[0]["to_match"] == {"case_id": "fir-423-26"}
        assert edges[0]["properties"]["filed_by"] == "accused side"
        # Written directly — never a `pending`/status field, unlike CITES.
        assert "status" not in edges[0]["properties"]

    async def test_self_reference_is_never_written(self, graph_calls):
        display_code_index = {"424/26": "fir-424-26"}
        fir = FirRecord({
            "fir_id": "fir-424-26", "fir_display_code": "424/26",
            "cross_version": [{"id": "cv1", "related_fir_display_code": "424/26", "filed_by": "x"}],
        })
        stats = await csp.project_fir_cross_versions(fir, display_code_index)
        assert stats["cross_version_written"] == 0
        assert not any(e["edge_label"] == "CROSS_VERSION_OF" for e in graph_calls["edges"])

    async def test_unresolvable_related_code_skipped_without_raising(self, graph_calls):
        display_code_index = {"424/26": "fir-424-26"}
        fir = FirRecord({
            "fir_id": "fir-424-26", "fir_display_code": "424/26",
            "cross_version": [{"id": "cv1", "related_fir_display_code": "999/26", "filed_by": "x"}],
        })
        stats = await csp.project_fir_cross_versions(fir, display_code_index)
        assert stats["cross_version_written"] == 0
        assert stats["errors"] == []

    async def test_missing_target_case_degrades_without_raising(self, graph_calls):
        graph_calls["missing_endpoints"] = {"fir-423-26"}
        display_code_index = {"423/26": "fir-423-26", "424/26": "fir-424-26"}
        fir = FirRecord({
            "fir_id": "fir-424-26", "fir_display_code": "424/26",
            "cross_version": [{"id": "cv1", "related_fir_display_code": "423/26", "filed_by": "x"}],
        })
        stats = await csp.project_fir_cross_versions(fir, display_code_index)
        assert stats["cross_version_written"] == 0
        assert stats["errors"] == []

    async def test_no_cross_version_rows_writes_nothing(self, graph_calls):
        fir = FirRecord({"fir_id": "fir-424-26", "fir_display_code": "424/26"})
        stats = await csp.project_fir_cross_versions(fir, {})
        assert stats["cross_version_written"] == 0
        assert not any(e["edge_label"] == "CROSS_VERSION_OF" for e in graph_calls["edges"])

    async def test_measured_count_against_real_snapshot(self, snapshot):
        """0 of 73 real FIRs carry a populated cross_version row — locks in
        that this remains a constructed-fixture-only path in the live
        dataset, same disclosure as C1's tenant/employee_registration test."""
        firs = [FirRecord(r) for r in snapshot["endpoints"]["fir"]]
        with_cross_version = [f for f in firs if f.child_rows("cross_version")]
        assert with_cross_version == []


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
