# ============================================================
# Cross-silo graph linking — M6b of the Muhafiz Data API migration
# (docs/decisions/0001-muhafiz-api-migration.md)
#
# M6a (structured_projection.py) is strictly single-FIR and deterministic.
# This module is the other half: confidence-scored or cross-record joins,
# split out deliberately because they carry a different risk profile and
# review discipline than M6a's ground-truth writes.
#
# TWO KINDS OF LINK HERE, TWO DIFFERENT BARS:
#
#   1. EXACT-KEY JOINS (written directly, not pending) — CMS<->FIR via
#      case_tag_number == e_tag_number, PKM<->FIR via
#      forwarded_fir_number == fir_display_code (src/ingestion/muhafiz_cases.py,
#      M4, reused here rather than re-implemented), and criminal records
#      linked to an EXISTING Person by subject_cnic (never the documented
#      but measured-broken criminal_record_ref soft-reference), and
#      REGISTERED_TO for a PKM vehicle_verification application's Vehicle
#      linked to an EXISTING Person by applicant_cnic, same soft-reference
#      discipline. These are exact string-equality joins on API-supplied
#      keys — no heuristic, no confidence score, written as real edges
#      immediately. Milestone C1 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md —
#      person-relationship edges) adds RELATED_TO{role} for PKM
#      tenant_registration (owner/tenant) and employee_registration
#      (employer/employee) applications, same soft-reference-by-CNIC
#      discipline as REGISTERED_TO above — see _write_pkm_relationship().
#
#   2. FIR->FIR PROSE CITATIONS (written as `CITES`, always `pending`) —
#      a regex hit against free text carries the same false-positive risk
#      profile as entity_resolution.py's name-based SAME_AS candidates, so
#      it gets the SAME human-confirmation bar, not a lower one just
#      because the source is prose instead of a name match. `CITES` is a
#      distinct edge type from `SAME_AS` (Case/Incident identity is not
#      Person/Vehicle identity — see docs/graph_schema.md) — reviewed
#      through the parallel queue in src/api/graph_review.py's
#      /citations endpoints, added alongside the existing /pending
#      SAME_AS queue rather than merged into it.
#
# ORDERING DEPENDENCY: every function here writes edges whose OTHER
# endpoint is a Case/Person node that M6a's project_fir() creates. If this
# module runs before M6a has projected the relevant FIR(s),
# versioning.write_edge() degrades to "endpoint not found -> None +
# warning" (its own documented behavior) rather than raising — the edge
# is simply not written that run. M9's sync script must project every FIR
# (M6a) before or together with cross-silo linking (M6b) for these joins
# to land on the first pass; a stale/partial graph self-heals on the next
# full re-projection once both sides exist.
# ============================================================

from __future__ import annotations

import logging
import re
from typing import Optional

from src.data_gateway.muhafiz_api.models import CmsComplaint, CriminalRecord, FirRecord, PkmApplication
from src.graph import age_client, entity_resolution, versioning
from src.graph.structured_projection import resolve_structured_person

logger = logging.getLogger(__name__)


def _doc_id_for_cms(cms: CmsComplaint) -> str:
    return f"cms/complaint/{cms.complaint_id}#structured"


def _doc_id_for_pkm(pkm: PkmApplication) -> str:
    return f"pkm/application/{pkm.application_id}#structured"


def _vehicle_entity_id(plate: str) -> str:
    """Deterministic, MERGE-safe id — a vehicle's plate is itself the
    natural key (same convention as Person's cnic/Weapon's per-FIR id),
    so re-syncing the same application never mints a duplicate node."""
    return f"VEHICLE-{plate}"


def _doc_id_for_criminal_record(record: CriminalRecord) -> str:
    return f"criminal_db/criminal_record/{record.record_id}#structured"


def _incident_entity_id_for_case(case_id: str) -> str:
    """
    Mirrors structured_projection._incident_entity_id() exactly (same
    deterministic formula, same FIR-id-keyed Incident) — recomputed here
    from just the resolved case_id (== fir_id under "Case = FIR") rather
    than imported, since this module only ever has the case_id, not a
    FirRecord, at the point it needs this.
    """
    return f"INCIDENT-FIR-{case_id}"


# ── CMS ──────────────────────────────────────────────────────────────────

async def project_cms_complaint(
    cms: CmsComplaint, case_id: Optional[str], *, graph: str = age_client.GRAPH_NAME,
) -> dict:
    """
    `case_id` is the result of src.ingestion.muhafiz_cases.resolve_cms_case_id()
    — None for an unlinked complaint (measured live: 0 of 4 in this
    dataset are unlinked, but the general case exists). An unlinked
    complaint still gets its StructuredRecord node (complainant CNIC kept
    as a plain property, soft-reference style — the same pattern
    muhafiz_schema.dbml.txt itself uses throughout); it just never gets a
    BELONGS_TO_CASE edge or complainant Person resolution, since both
    require a real Case to attach to (entity_resolution.py's case-scoped
    candidate generation raises on a falsy case_id — see
    src/graph/case_scope.py's own structural guard — so this is a
    deliberate skip, not an oversight).
    """
    stats = {"structured_records": 0, "edges_written": 0, "persons_resolved": 0, "errors": []}
    doc_id = _doc_id_for_cms(cms)
    record_id = f"cms_complaint:{cms.complaint_id}"

    try:
        await versioning.write_node(
            "Document", {"doc_id": doc_id}, {"filename": cms.complaint_id, "doc_type": "cms_structured"},
            source_doc_id=doc_id, graph=graph,
        )
        properties = {
            "record_type": "cms_complaint",
            "case_tag_number": cms.case_tag_number,
            "police_station_name": cms.raw.get("police_station_name"),
            "police_station_code": cms.raw.get("police_station_code"),
            "submitted_at": cms.raw.get("submitted_at"),
            "method": cms.raw.get("method"),
            "language": cms.raw.get("language"),
            "complainant_cnic": cms.complainant_cnic,
        }
        properties = {k: v for k, v in properties.items() if v is not None}
        await versioning.write_node(
            "StructuredRecord", {"record_id": record_id}, properties, source_doc_id=doc_id, graph=graph,
        )
        await versioning.write_edge(
            "APPEARS_IN", "StructuredRecord", {"record_id": record_id}, "Document", {"doc_id": doc_id},
            {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        stats["structured_records"] += 1
        stats["edges_written"] += 1

        if case_id:
            edge = await versioning.write_edge(
                "BELONGS_TO_CASE", "StructuredRecord", {"record_id": record_id}, "Case", {"case_id": case_id},
                {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
            )
            if edge:
                stats["edges_written"] += 1

            complainant_name = cms.complainant.get("full_name")
            if complainant_name:
                mention = {"canonical_name": complainant_name, "extraction_confidence": 1.0}
                if cms.complainant_cnic:
                    mention["cnic"] = cms.complainant_cnic
                if cms.complainant.get("phone"):
                    mention["phone"] = cms.complainant["phone"]
                resolution = await resolve_structured_person(mention, case_id, doc_id, graph=graph)
                stats["persons_resolved"] += 1
                involved = await versioning.write_edge(
                    "INVOLVED_IN", "Person", {"entity_id": resolution["entity_id"]},
                    "Incident", {"entity_id": _incident_entity_id_for_case(case_id)},
                    {"role": "complainant_cms"}, source_doc_id=doc_id, confidence=1.0, graph=graph,
                )
                if involved:
                    stats["edges_written"] += 1
    except Exception as exc:
        logger.warning("CMS projection failed for %s: %s", cms.complaint_id, exc)
        stats["errors"].append(str(exc))

    return stats


# ── PKM ──────────────────────────────────────────────────────────────────

async def project_pkm_application(
    pkm: PkmApplication, case_id: Optional[str], *, graph: str = age_client.GRAPH_NAME,
) -> dict:
    """Same shape as project_cms_complaint(); case_id from
    src.ingestion.muhafiz_cases.resolve_pkm_case_id() (women_violence_report
    applications only — every other service type always resolves to None,
    per that function's own docstring)."""
    stats = {"structured_records": 0, "edges_written": 0, "persons_resolved": 0, "vehicles_written": 0, "errors": []}
    doc_id = _doc_id_for_pkm(pkm)
    record_id = f"pkm_application:{pkm.application_id}"

    try:
        await versioning.write_node(
            "Document", {"doc_id": doc_id}, {"filename": pkm.application_id, "doc_type": "pkm_structured"},
            source_doc_id=doc_id, graph=graph,
        )
        properties = {
            "record_type": "pkm_application",
            "service_type": pkm.service_type,
            "submitted_at": pkm.raw.get("submitted_at"),
            "status": pkm.raw.get("status"),
            "applicant_cnic": pkm.applicant_cnic,
        }
        properties = {k: v for k, v in properties.items() if v is not None}
        await versioning.write_node(
            "StructuredRecord", {"record_id": record_id}, properties, source_doc_id=doc_id, graph=graph,
        )
        await versioning.write_edge(
            "APPEARS_IN", "StructuredRecord", {"record_id": record_id}, "Document", {"doc_id": doc_id},
            {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        stats["structured_records"] += 1
        stats["edges_written"] += 1

        if case_id:
            edge = await versioning.write_edge(
                "BELONGS_TO_CASE", "StructuredRecord", {"record_id": record_id}, "Case", {"case_id": case_id},
                {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
            )
            if edge:
                stats["edges_written"] += 1

            applicant_name = pkm.applicant.get("full_name")
            if applicant_name:
                mention = {"canonical_name": applicant_name, "extraction_confidence": 1.0}
                if pkm.applicant_cnic:
                    mention["cnic"] = pkm.applicant_cnic
                if pkm.applicant.get("phone"):
                    mention["phone"] = pkm.applicant["phone"]
                if pkm.applicant.get("address_text"):
                    mention["address_text"] = pkm.applicant["address_text"]
                resolution = await resolve_structured_person(mention, case_id, doc_id, graph=graph)
                stats["persons_resolved"] += 1
                involved = await versioning.write_edge(
                    "INVOLVED_IN", "Person", {"entity_id": resolution["entity_id"]},
                    "Incident", {"entity_id": _incident_entity_id_for_case(case_id)},
                    {"role": "applicant_pkm"}, source_doc_id=doc_id, confidence=1.0, graph=graph,
                )
                if involved:
                    stats["edges_written"] += 1

        if pkm.service_type == "vehicle_verification":
            await _write_pkm_vehicle(pkm, doc_id, graph, stats)
        elif pkm.service_type == "tenant_registration":
            await _write_pkm_relationship(pkm, pkm.owner, pkm.tenant, "landlord_of", doc_id, graph, stats)
        elif pkm.service_type == "employee_registration":
            await _write_pkm_relationship(pkm, pkm.employer, pkm.employee, "employer_of", doc_id, graph, stats)
    except Exception as exc:
        logger.warning("PKM projection failed for %s: %s", pkm.application_id, exc)
        stats["errors"].append(str(exc))

    return stats


async def _write_pkm_relationship(
    pkm: PkmApplication, from_person: dict, to_person: dict, role: str,
    doc_id: str, graph: str, stats: dict,
) -> None:
    """
    Milestone C1 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — person-relationship
    edges): RELATED_TO{role} between the two nested people on a
    tenant_registration (owner/tenant) or employee_registration
    (employer/employee) application.

    Never case-scoped (same reasoning as _write_pkm_vehicle above — these
    service types don't resolve to a Case via resolve_pkm_case_id()), so
    neither person is minted here — only linked when BOTH sides already
    resolve to an EXISTING Person node by CNIC, the identical soft-reference
    discipline project_criminal_record()'s subject_cnic lookup and
    _write_pkm_vehicle()'s applicant_cnic lookup already use. Minting a
    fresh caseless Person here would bypass entity_resolution's
    corroboration gate entirely (there is no case to corroborate against),
    so a PKM-only owner/tenant/employer/employee with no CNIC match to an
    existing Person is simply not linked — consistent with, not a
    weakening of, this module's existing case-less-record precedent.
    """
    from_cnic, to_cnic = from_person.get("cnic"), to_person.get("cnic")
    if not from_cnic or not to_cnic:
        return
    from_existing = await entity_resolution._find_by_primary_id("Person", "cnic", from_cnic, graph=graph)
    to_existing = await entity_resolution._find_by_primary_id("Person", "cnic", to_cnic, graph=graph)
    if not from_existing or not to_existing:
        return
    edge = await versioning.write_edge(
        "RELATED_TO", "Person", {"entity_id": from_existing["properties"].get("entity_id")},
        "Person", {"entity_id": to_existing["properties"].get("entity_id")},
        {"role": role}, source_doc_id=doc_id, confidence=1.0, graph=graph,
    )
    if edge:
        stats["edges_written"] += 1


async def _write_pkm_vehicle(pkm: PkmApplication, doc_id: str, graph: str, stats: dict) -> None:
    """
    REGISTERED_TO — the fifth of the five edge types declared in migration
    005 with zero writers before this migration (structured_projection.py's
    module docstring names OWNS/INVOLVED_IN/PART_OF/LOCATED_AT as the other
    four; this one was explicitly deferred here since it needs PKM's
    vehicle_verification, a cross-silo record with no FIR of its own).

    Never case-scoped — vehicle_verification applications never resolve to
    a case (src.ingestion.muhafiz_cases.resolve_pkm_case_id() only ever
    matches women_violence_report; this service type always gets case_id
    None), so the Vehicle node is written unconditionally here rather than
    gated behind `if case_id:` the way applicant Person resolution is
    above. The vehicle's plate is its own natural key (MERGE-safe via
    _vehicle_entity_id, mirroring Person's cnic/Weapon's per-FIR id), so a
    re-sync never mints a duplicate.

    REGISTERED_TO only gets written when the applicant's CNIC already
    resolves to an EXISTING Person node — same soft-reference discipline
    as project_criminal_record()'s subject_cnic lookup just below: no
    Person is minted here just to hang an edge off it (that would bypass
    the corroboration gate entirely for a mention with no case context to
    corroborate against).
    """
    service = pkm.service_record() or {}
    plate = service.get("vehicle_registration_no")
    if not plate:
        return

    entity_id = _vehicle_entity_id(plate)
    properties = {
        "plate": plate,
        "canonical_name": plate,
        "make": service.get("vehicle_make"),
        "model": service.get("vehicle_model"),
        "chassis_no": service.get("vehicle_chassis_no"),
        "engine_no": service.get("vehicle_engine_no"),
        "verification_result": service.get("verification_result"),
    }
    properties = {k: v for k, v in properties.items() if v is not None}
    await versioning.write_node("Vehicle", {"entity_id": entity_id}, properties, source_doc_id=doc_id, graph=graph)
    stats["vehicles_written"] += 1
    edge = await versioning.write_edge(
        "APPEARS_IN", "Vehicle", {"entity_id": entity_id}, "Document", {"doc_id": doc_id},
        {"surface_text": plate}, source_doc_id=doc_id, confidence=1.0, graph=graph,
    )
    if edge:
        stats["edges_written"] += 1

    applicant_cnic = pkm.applicant_cnic
    if applicant_cnic:
        existing = await entity_resolution._find_by_primary_id("Person", "cnic", applicant_cnic, graph=graph)
        if existing:
            person_entity_id = existing["properties"].get("entity_id")
            registered = await versioning.write_edge(
                "REGISTERED_TO", "Vehicle", {"entity_id": entity_id}, "Person", {"entity_id": person_entity_id},
                {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
            )
            if registered:
                stats["edges_written"] += 1


# ── criminal records ──────────────────────────────────────────────────────

async def project_criminal_record(
    record: CriminalRecord, *, graph: str = age_client.GRAPH_NAME,
) -> dict:
    """
    Never case-scoped (criminal_db isn't part of any Case in this design —
    a person's history spans cases by definition). Links to an EXISTING
    Person node by subject_cnic via a direct entity_resolution._find_by_primary_id()
    lookup — never entity_resolution.resolve_and_write() (which requires
    a case_id this record doesn't have) and never the documented
    criminal_record_ref soft-reference (measured live: 0 of 6 populated
    refs on this dataset actually match an external_record_ref — see the
    decision record). No Person node is minted for a criminal record with
    no matching existing Person; the record's subject_cnic stays a plain
    property either way, soft-reference style.
    """
    stats = {"structured_records": 0, "edges_written": 0, "linked_to_existing_person": False, "errors": []}
    doc_id = _doc_id_for_criminal_record(record)
    record_id = f"criminal_record:{record.record_id}"

    try:
        await versioning.write_node(
            "Document", {"doc_id": doc_id}, {"filename": record.record_id, "doc_type": "criminal_record_structured"},
            source_doc_id=doc_id, graph=graph,
        )
        properties = {
            "record_type": "criminal_record",
            "subject_cnic": record.subject_cnic,
            "subject_full_name": record.raw.get("subject_full_name"),
            "offense_summary": record.raw.get("offense_summary"),
            "conviction_status": record.raw.get("conviction_status"),
            "source_case_ref": record.raw.get("source_case_ref"),
        }
        properties = {k: v for k, v in properties.items() if v is not None}
        await versioning.write_node(
            "StructuredRecord", {"record_id": record_id}, properties, source_doc_id=doc_id, graph=graph,
        )
        await versioning.write_edge(
            "APPEARS_IN", "StructuredRecord", {"record_id": record_id}, "Document", {"doc_id": doc_id},
            {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        stats["structured_records"] += 1
        stats["edges_written"] += 1

        if record.subject_cnic:
            existing = await entity_resolution._find_by_primary_id(
                "Person", "cnic", record.subject_cnic, graph=graph,
            )
            if existing:
                person_entity_id = existing["properties"].get("entity_id")
                edge = await versioning.write_edge(
                    "APPEARS_IN", "Person", {"entity_id": person_entity_id}, "Document", {"doc_id": doc_id},
                    {"surface_text": record.raw.get("subject_full_name", "")},
                    source_doc_id=doc_id, confidence=1.0, graph=graph,
                )
                if edge:
                    stats["linked_to_existing_person"] = True
                    stats["edges_written"] += 1
    except Exception as exc:
        logger.warning("Criminal record projection failed for %s: %s", record.record_id, exc)
        stats["errors"].append(str(exc))

    return stats


# ── FIR -> FIR prose citations (CITES, always pending) ───────────────────

# Matches "NNN/YY"-shaped tokens — the real fir_display_code format
# (structured_fields._FIR_RE, by contrast, matches the synthetic corpus's
# FIR-YYYY-CAT-NNN shape and would find nothing here; deliberately not
# reused for that reason).
_FIR_CODE_RE = re.compile(r"\d{1,4}\s*/\s*\d{2}")


def find_cited_display_codes(fir: FirRecord, known_codes: set[str]) -> list[tuple[str, str]]:
    """
    Scans every free-text field this FIR carries for a token matching
    another REAL fir_display_code (never itself). Returns
    [(cited_display_code, basis_label), ...], each code appearing once
    (first field it was found in). Pure function — no I/O — so it's
    directly unit-testable and reusable from a future eval/report without
    touching the graph.
    """
    own_code = (fir.fir_display_code or "").replace(" ", "")
    blobs: list[tuple[str, Optional[str]]] = [("narrative", fir.narrative_text)]
    for p in fir.child_rows("fir_position"):
        blobs.append((f"position_remarks_{p.get('id')}", p.get("remarks")))
    for z in fir.child_rows("fir_zimni"):
        blobs.append((f"zimni_entry_{z.get('entry_number')}", z.get("entry_text")))
    for cd in fir.child_rows("chalaan_dispatch"):
        blobs.append((f"chalaan_property_{cd.get('id')}", cd.get("property_involved")))

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, text in blobs:
        if not text:
            continue
        for raw_match in _FIR_CODE_RE.findall(text):
            code = raw_match.replace(" ", "")
            if code == own_code or code in seen or code not in known_codes:
                continue
            seen.add(code)
            found.append((code, label))
    return found


async def project_fir_citations(
    fir: FirRecord, display_code_index: dict[str, str], known_codes: set[str],
    *, graph: str = age_client.GRAPH_NAME,
) -> dict:
    """
    Writes a `CITES{status: "pending"}` Case->Case edge for every prose
    citation found — never a direct/confirmed edge (see module docstring:
    same human-confirmation bar as a name-based SAME_AS candidate).
    Reviewed through src.api.graph_review's /citations endpoints.
    """
    stats = {"cites_written": 0, "errors": []}
    doc_id = f"psrms/fir/{fir.fir_id}#structured"

    for cited_code, basis_label in find_cited_display_codes(fir, known_codes):
        cited_fir_id = display_code_index.get(cited_code)
        if not cited_fir_id or cited_fir_id == fir.fir_id:
            continue
        try:
            edge = await versioning.write_edge(
                "CITES", "Case", {"case_id": fir.fir_id}, "Case", {"case_id": cited_fir_id},
                {
                    "status": "pending",
                    "basis": f"FIR {cited_code} referenced in {basis_label}",
                },
                source_doc_id=doc_id, confidence=0.6, graph=graph,
            )
            if edge:
                stats["cites_written"] += 1
        except Exception as exc:
            logger.warning("CITES write failed for %s -> %s: %s", fir.fir_id, cited_fir_id, exc)
            stats["errors"].append(f"cites[{cited_code}]: {exc}")

    return stats
