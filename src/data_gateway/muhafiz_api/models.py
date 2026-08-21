# ============================================================
# Muhafiz Data API — typed record wrappers
#
# WHY THIN WRAPPERS OVER RAW DICTS, NOT FULL DATACLASSES PER FIELD:
# Measured against the live API (see docs/decisions/0001-muhafiz-api-migration.md
# for the full measurement), the response already diverges from
# muhafiz_schema.dbml.txt in ways a strict schema would break on:
#   - fir.crime_scene_location (the DBML's "merged in revision 11" field) is
#     returned ALONGSIDE the four legacy columns it supposedly replaced
#     (crime_scene_description/_distance_km/_direction/_beat_number), all
#     null. Both shapes must be tolerated.
#   - fir_id / police_station_id are slugs ("fir-1001-26", "PS-ISB-CYBER"),
#     not the UUIDs the DBML declares. Never parse these as UUID.
#   - Many fields are null far more often than the schema implies
#     (fir_position.position 65/94 null, fir_zimni.entry_type 188/259 null,
#     fir_witness.witness_type 0/37 populated at all).
# A dataclass-per-field model would need updating every time the API's
# actual shape drifts further from the DBML, and would silently drop
# unknown fields. Instead: `raw` is always the full parsed dict (nothing is
# lost), and each wrapper exposes read-only properties for the fields the
# ingestion pipeline (src/ingestion/muhafiz_records.py, M3) and graph
# projection (src/graph/structured_projection.py, M6a/M6b) actually use —
# every property tolerates a missing/null key.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


def _get(raw: dict, *keys: str) -> Any:
    """First non-None value found walking `keys` in order, else None."""
    for k in keys:
        v = raw.get(k)
        if v is not None:
            return v
    return None


@dataclass(frozen=True)
class FirRecord:
    """
    One FIR bundle — the FIR itself plus its child-table arrays, exactly as
    /fir and /fir/{id} return it (API_CONSUMER_GUIDE.md's "complete FIR case
    objects"). Child tables are left as raw list[dict] on `raw` rather than
    wrapped further; M3/M6a read them directly by key, since their shapes
    are simple flat rows already.
    """

    raw: dict

    @property
    def fir_id(self) -> str:
        return self.raw["fir_id"]

    @property
    def fir_display_code(self) -> Optional[str]:
        """e.g. "891/24" — the human FIR number, globally unique (verified)."""
        return self.raw.get("fir_display_code")

    @property
    def police_station_id(self) -> Optional[str]:
        return self.raw.get("police_station_id")

    @property
    def police_station(self) -> dict:
        """Nested {id, name, code, district} object, may be absent."""
        return self.raw.get("police_station") or {}

    @property
    def crime_scene_location(self) -> Optional[str]:
        """
        The revision-11 merged field. Falls back to the legacy
        crime_scene_description column if the merged field is null and the
        legacy one isn't — belt-and-braces for whichever shape a given
        record happens to carry (both have been observed live, always
        null together in this dataset, but not guaranteed to stay that way).
        """
        return _get(self.raw, "crime_scene_location", "crime_scene_description")

    @property
    def narrative_text(self) -> Optional[str]:
        return self.raw.get("narrative_text")

    @property
    def complainant_cnic(self) -> Optional[str]:
        return self.raw.get("complainant_cnic")

    @property
    def e_tag_number(self) -> Optional[str]:
        """Soft-ref to cms.complaint.case_tag_number when this FIR escalated from a complaint."""
        return self.raw.get("e_tag_number")

    @property
    def updated_at(self) -> Optional[str]:
        return self.raw.get("updated_at")

    @property
    def source(self) -> Optional[str]:
        """'synthetic' | 'real' | None — provenance tag, see decision record §synthetic-provenance."""
        return self.raw.get("source")

    def child_rows(self, table: str) -> list[dict]:
        """
        One of the 12 documented child arrays (fir_section, fir_accused,
        fir_witness, fir_investigating_officer, fir_position, fir_zimni,
        fir_zimni_index, chalaan_dispatch, chalaan_outcome, cross_version,
        malkhana_register, weapon_register). Always a list, possibly empty
        — API_CONSUMER_GUIDE.md: "An empty array means no matching records
        exist for that FIR," never a missing key in practice, but `.get`
        with a default guards the case anyway.
        """
        return self.raw.get(table) or []


@dataclass(frozen=True)
class CmsComplaint:
    raw: dict

    @property
    def complaint_id(self) -> str:
        return self.raw["complaint_id"]

    @property
    def case_tag_number(self) -> Optional[str]:
        return self.raw.get("case_tag_number")

    @property
    def complainant(self) -> dict:
        """Nested person object when complainant_person_id matched person.id."""
        return self.raw.get("complainant") or {}

    @property
    def complainant_cnic(self) -> Optional[str]:
        return self.complainant.get("cnic")

    @property
    def one_line_summary(self) -> Optional[str]:
        return self.raw.get("one_line_summary")


@dataclass(frozen=True)
class PkmApplication:
    raw: dict

    _SERVICE_KEYS = (
        "character_certificate", "driving_license", "employee_registration",
        "loss_report", "tenant_registration", "vehicle_verification",
        "women_violence_report",
    )

    @property
    def application_id(self) -> str:
        return self.raw["application_id"]

    @property
    def service_type(self) -> Optional[str]:
        return self.raw.get("service_type")

    @property
    def applicant(self) -> dict:
        return self.raw.get("applicant") or {}

    @property
    def applicant_cnic(self) -> Optional[str]:
        return self.applicant.get("cnic")

    def service_record(self) -> Optional[dict]:
        """
        Whichever of the 7 service-specific nested objects is non-null for
        this application (API_CONSUMER_GUIDE.md: "unavailable service
        records set to null"). Returns None if none are populated.
        """
        for key in self._SERVICE_KEYS:
            val = self.raw.get(key)
            if val:
                return {"service_type": key, **val}
        return None

    @property
    def forwarded_fir_number(self) -> Optional[str]:
        """Only present on women_violence_report applications."""
        rec = self.service_record()
        if rec and rec.get("service_type") == "women_violence_report":
            return rec.get("forwarded_fir_number")
        return None

    # Milestone C1 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — person-relationship
    # edges): nested person objects for tenant_registration/employee_registration
    # applications. Not observed populated in the live dataset (0 of 14 real
    # PKM applications are either service type — measured against
    # tests/fixtures/muhafiz_api_snapshot.json), so this shape is inferred by
    # direct analogy to `applicant` above (the one nested-person enrichment
    # confirmed live, on every application regardless of service type) and
    # API_CONSUMER_GUIDE.md's own text ("It also includes available
    # applicant, police-station, employee, tenant... references" — listed as
    # its own top-level references, the same tier as `applicant`/
    # `police_station`, not nested inside the service-specific sub-object).
    # Flagged here the same way the schema itself flags victim_name/
    # cross_version as inferred-not-confirmed, rather than silently assumed.
    @property
    def owner(self) -> dict:
        return self.raw.get("owner") or {}

    @property
    def tenant(self) -> dict:
        return self.raw.get("tenant") or {}

    @property
    def employer(self) -> dict:
        return self.raw.get("employer") or {}

    @property
    def employee(self) -> dict:
        return self.raw.get("employee") or {}


@dataclass(frozen=True)
class CriminalRecord:
    raw: dict

    @property
    def record_id(self) -> str:
        return self.raw["id"]

    @property
    def subject_cnic(self) -> Optional[str]:
        return self.raw.get("subject_cnic")

    @property
    def external_record_ref(self) -> Optional[str]:
        """
        Documented as the join key back to fir_accused.criminal_record_ref.
        Measured against the live data: 0 of 6 populated accused refs
        actually match an external_record_ref. Do not rely on this join —
        link criminal records to FIRs by subject_cnic instead (see decision
        record). Kept here only because the field exists on the wire.
        """
        return self.raw.get("external_record_ref")


@dataclass(frozen=True)
class RoznamchaEntry:
    raw: dict

    @property
    def entry_id(self) -> str:
        return self.raw["id"]

    @property
    def police_station_id(self) -> Optional[str]:
        return self.raw.get("police_station_id")

    @property
    def entry_text(self) -> Optional[str]:
        return self.raw.get("entry_text")

    @property
    def entry_date(self) -> Optional[str]:
        return self.raw.get("entry_date")
