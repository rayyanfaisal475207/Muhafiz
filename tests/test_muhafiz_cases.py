"""
M4 of the Muhafiz Data API migration (docs/decisions/0001-muhafiz-api-migration.md) —
src/ingestion/muhafiz_cases.py: pure Case-field derivation and cross-silo
escalation matching, no database involved.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from src.data_gateway.muhafiz_api.models import CmsComplaint, FirRecord, PkmApplication
from src.ingestion import muhafiz_cases as mc

FIXTURE = Path(__file__).parent / "fixtures" / "muhafiz_api_snapshot.json"


@pytest.fixture(scope="module")
def snapshot():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def firs(snapshot):
    return [FirRecord(r) for r in snapshot["endpoints"]["fir"]]


@pytest.fixture(scope="module")
def cms_complaints(snapshot):
    return [CmsComplaint(r) for r in snapshot["endpoints"]["cms"]]


@pytest.fixture(scope="module")
def pkm_applications(snapshot):
    return [PkmApplication(r) for r in snapshot["endpoints"]["pkm"]]


# ── case_fields_from_fir ─────────────────────────────────────────────────

class TestCaseFieldsFromFir:
    def test_case_id_is_the_fir_id_not_the_display_code(self):
        fir = FirRecord({"fir_id": "fir-891-24", "fir_display_code": "891/24"})
        fields = mc.case_fields_from_fir(fir)
        assert fields["case_id"] == "fir-891-24"
        assert fields["fir_number"] == "891/24"

    def test_incident_date_is_a_real_date_object_not_a_string(self):
        """Regression: a plain sliced string reached asyncpg's DATE binding
        directly and crashed on the first live INSERT
        ('str' object has no attribute 'toordinal') — never caught by
        tests until this migration was run against a real database."""
        fir = FirRecord({"fir_id": "fir-1-26", "incident_datetime": "2026-08-18T15:10:00Z"})
        fields = mc.case_fields_from_fir(fir)
        assert fields["incident_date"] == date(2026, 8, 18)
        assert isinstance(fields["incident_date"], date)

    def test_malformed_incident_datetime_gives_none_not_a_raised_error(self):
        fir = FirRecord({"fir_id": "fir-1-26", "incident_datetime": "not-a-date"})
        assert mc.case_fields_from_fir(fir)["incident_date"] is None

    def test_missing_incident_datetime_gives_none(self):
        fir = FirRecord({"fir_id": "fir-2-26"})
        assert mc.case_fields_from_fir(fir)["incident_date"] is None

    def test_crime_category_joins_distinct_acts_in_order(self):
        fir = FirRecord({
            "fir_id": "fir-3-26",
            "fir_section": [
                {"section_code": "379", "act": "PPC"},
                {"section_code": "13", "act": "Arms Ordinance 1965"},
                {"section_code": "34", "act": "PPC"},  # duplicate act, must not repeat
            ],
        })
        assert mc.case_fields_from_fir(fir)["crime_category"] == "PPC, Arms Ordinance 1965"

    def test_no_sections_gives_none_crime_category(self):
        fir = FirRecord({"fir_id": "fir-4-26"})
        assert mc.case_fields_from_fir(fir)["crime_category"] is None

    def test_officer_prefers_one_with_no_assigned_to(self):
        fir = FirRecord({
            "fir_id": "fir-5-26",
            "fir_investigating_officer": [
                {"officer_name": "Old IO", "assigned_to": "2026-01-01"},
                {"officer_name": "Current IO", "assigned_to": None},
            ],
        })
        assert mc.case_fields_from_fir(fir)["investigation_officer"] == "Current IO"

    def test_officer_falls_back_to_last_row_when_all_have_assigned_to(self):
        fir = FirRecord({
            "fir_id": "fir-6-26",
            "fir_investigating_officer": [
                {"officer_name": "First IO", "assigned_to": "2026-01-01"},
                {"officer_name": "Last IO", "assigned_to": "2026-02-01"},
            ],
        })
        assert mc.case_fields_from_fir(fir)["investigation_officer"] == "Last IO"

    def test_no_officers_gives_none(self):
        fir = FirRecord({"fir_id": "fir-7-26"})
        assert mc.case_fields_from_fir(fir)["investigation_officer"] is None

    def test_status_picks_latest_by_status_date(self):
        fir = FirRecord({
            "fir_id": "fir-8-26",
            "fir_position": [
                {"position": "زیر تفتیش", "status_date": "2026-01-01"},
                {"position": "چالان عدالت ارسال", "status_date": "2026-03-01"},
            ],
        })
        assert mc.case_fields_from_fir(fir)["investigation_status"] == "چالان عدالت ارسال"

    def test_status_skips_null_position_rows(self):
        """Measured live: 65/94 fir_position rows have position=None."""
        fir = FirRecord({
            "fir_id": "fir-9-26",
            "fir_position": [
                {"position": None, "status_date": "2026-05-01"},  # later date, but no text
                {"position": "زیر تفتیش", "status_date": "2026-01-01"},
            ],
        })
        assert mc.case_fields_from_fir(fir)["investigation_status"] == "زیر تفتیش"

    def test_status_falls_back_to_chalaan_outcome_when_no_position_rows(self):
        fir = FirRecord({
            "fir_id": "fir-10-26",
            "chalaan_outcome": [{"case_outcome": "Convicted"}],
        })
        assert mc.case_fields_from_fir(fir)["investigation_status"] == "Convicted"

    def test_status_none_when_nothing_at_all(self):
        fir = FirRecord({"fir_id": "fir-11-26"})
        assert mc.case_fields_from_fir(fir)["investigation_status"] is None

    def test_location_prefers_crime_scene_falls_back_to_station(self):
        fir = FirRecord({
            "fir_id": "fir-12-26",
            "crime_scene_location": "Model Town",
            "police_station": {"name": "PS Model Town"},
        })
        assert mc.case_fields_from_fir(fir)["location"] == "Model Town"

        fir_no_scene = FirRecord({
            "fir_id": "fir-13-26",
            "police_station": {"name": "PS Model Town"},
        })
        assert mc.case_fields_from_fir(fir_no_scene)["location"] == "PS Model Town"


# ── escalation matching ─────────────────────────────────────────────────

class TestEscalationMatching:
    def test_cms_resolves_via_matching_e_tag(self):
        firs = [FirRecord({"fir_id": "fir-1-26", "e_tag_number": "CMS-ISB-2026-0341"})]
        index = mc.build_e_tag_index(firs)
        cms = CmsComplaint({"complaint_id": "C1", "case_tag_number": "CMS-ISB-2026-0341"})
        assert mc.resolve_cms_case_id(cms, index) == "fir-1-26"

    def test_cms_no_match_returns_none(self):
        index = mc.build_e_tag_index([FirRecord({"fir_id": "fir-1-26", "e_tag_number": "TAG-A"})])
        cms = CmsComplaint({"complaint_id": "C2", "case_tag_number": "TAG-B"})
        assert mc.resolve_cms_case_id(cms, index) is None

    def test_pkm_resolves_via_forwarded_fir_number(self):
        firs = [FirRecord({"fir_id": "fir-97-26", "fir_display_code": "97/26"})]
        index = mc.build_display_code_index(firs)
        pkm = PkmApplication({
            "application_id": "P1", "service_type": "women_violence_report",
            "women_violence_report": {"forwarded_fir_number": "97/26"},
        })
        assert mc.resolve_pkm_case_id(pkm, index) == "fir-97-26"

    def test_pkm_non_women_violence_report_never_resolves(self):
        index = mc.build_display_code_index([FirRecord({"fir_id": "fir-1-26", "fir_display_code": "1/26"})])
        pkm = PkmApplication({
            "application_id": "P2", "service_type": "vehicle_verification",
            "vehicle_verification": {},
        })
        assert mc.resolve_pkm_case_id(pkm, index) is None

    def test_e_tag_index_skips_firs_with_no_tag(self):
        firs = [FirRecord({"fir_id": "fir-1-26"}), FirRecord({"fir_id": "fir-2-26", "e_tag_number": "T1"})]
        assert mc.build_e_tag_index(firs) == {"T1": "fir-2-26"}


# ── against the real snapshot ────────────────────────────────────────────

class TestAgainstRealSnapshot:
    def test_every_fir_derives_fields_without_raising(self, firs):
        for fir in firs:
            fields = mc.case_fields_from_fir(fir)
            assert fields["case_id"] == fir.fir_id

    def test_case_ids_are_unique(self, firs):
        ids = [mc.case_fields_from_fir(fir)["case_id"] for fir in firs]
        assert len(ids) == len(set(ids))

    def test_measured_escalation_counts(self, firs, cms_complaints, pkm_applications):
        """Locks in the exact counts measured live in the decision record —
        4/4 CMS, 4/8 PKM women_violence_report applications resolve to a
        real FIR. A regression here means the join keys drifted."""
        e_tag_index = mc.build_e_tag_index(firs)
        display_code_index = mc.build_display_code_index(firs)

        cms_matched = sum(1 for c in cms_complaints if mc.resolve_cms_case_id(c, e_tag_index))
        assert cms_matched == 4

        pkm_matched = sum(1 for p in pkm_applications if mc.resolve_pkm_case_id(p, display_code_index))
        assert pkm_matched == 4
