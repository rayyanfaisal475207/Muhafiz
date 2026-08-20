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
#      but measured-broken criminal_record_ref soft-reference). These are
#      exact string-equality joins on API-supplied keys — no heuristic,
#      no confidence score, written as real edges immediately.
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
    stats = {"structured_records": 0, "edges_written": 0, "persons_resolved": 0, "errors": []}
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
    except Exception as exc:
        logger.warning("PKM projection failed for %s: %s", pkm.application_id, exc)
        stats["errors"].append(str(exc))

    return stats


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
