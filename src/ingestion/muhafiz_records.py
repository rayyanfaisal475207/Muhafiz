# ============================================================
# Muhafiz Data API — record → Document rendering (M3 of the migration,
# docs/decisions/0001-muhafiz-api-migration.md)
#
# Turns REST records (src/data_gateway/muhafiz_api/models.py) into the same
# Document objects (src/ingestion/document.py) file loaders produce, so
# existing ingestion (chunk/embed/store — src/ingestion/service.py's
# ingest_documents(), M2) needs zero changes to consume them.
#
# WHAT GETS EMBEDDED, AND WHAT DOESN'T:
# Only genuine free text is chunked and embedded here — narrative_text,
# zimni entry_text, fir_position remarks, chalaan free-text fields,
# cms.one_line_summary, PKM incident/loss descriptions, roznamcha
# entry_text. Everything else (accused/witness/section/weapon rows,
# timestamps, station/district) is STRUCTURED data with its own identity
# fields — it belongs on graph nodes as ground truth
# (src/graph/structured_projection.py, M6a/M6b), not re-flattened into
# prose and handed to an LLM to re-extract. Duplicating a CNIC into
# embedded text would just recreate the extraction-guesswork problem this
# migration exists to eliminate.
#
# A short STRUCTURED HEADER (station, district, sections, key dates) is
# prepended to each embedded chunk anyway — not for re-extraction, but
# because semantic + BM25 retrieval need SOME lexical surface for
# identifiers, and this dataset's identifiers (a station name, a section
# number, an FIR display code) are exactly the tokens investigators query on.
#
# STABLE `source` IDS:
# Document.doc_id is seeded on metadata["source"] (see document.py's
# _generate_id() docstring) — re-fetching the same record from the API and
# re-rendering it must produce the SAME source string every time, or every
# sync run mints new chunk ids and orphans the graph's APPEARS_IN edges
# (constraint 3 in the decision record). Source strings here are therefore
# always "{silo}/{table}/{record_id}#{field}", built only from fields the
# API guarantees are stable identifiers (fir_id, complaint_id,
# application_id, roznamcha id) — never from content.
#
# CASE SCOPING IS NOT THIS MODULE'S JOB: a caller passes case_id to
# ingest_documents() itself (Case = FIR, confirmed in the decision record —
# so a whole FIR's rendered Documents share fir_id as case_id; CMS/PKM/
# roznamcha records not linked to a FIR are ingested with case_id=None).
# ============================================================

from __future__ import annotations

from typing import Optional

from src.data_gateway.muhafiz_api.models import (
    CmsComplaint,
    PkmApplication,
    FirRecord,
    RoznamchaEntry,
)
from src.ingestion.document import Document

SOURCE_SYSTEM = "muhafiz_api"


def _meta(
    record_type: str, external_id: str, source: str, *,
    station_code: Optional[str] = None, district: Optional[str] = None,
    content_provenance: Optional[str] = None, record_date: Optional[str] = None,
) -> dict:
    """
    Common metadata every rendered Document carries. `source` here is the
    stable source string described in the module docstring — NOT the
    record's own `source` field (its real/synthetic origin tag, which is
    instead carried as `content_provenance` to avoid colliding with the
    existing chunk-metadata key `source`, which means "which file/record
    did this chunk come from" everywhere else in the pipeline).

    `record_date` — added M8 (docs/decisions/0001-muhafiz-api-migration.md)
    — an ISO date/timestamp string genuinely relevant to this record
    (FIR incident date, CMS/PKM submission date, roznamcha entry date).
    Lets src/retrieval/reranker.py's recency boost read a real field
    instead of regexing a filename — API-sourced `source` strings (e.g.
    "psrms/fir/fir-1001-26#narrative") carry no year at all, unlike the
    synthetic corpus's filenames.
    """
    return {
        "source": source,
        "record_type": record_type,
        "source_system": SOURCE_SYSTEM,
        "external_id": external_id,
        "station_code": station_code,
        "district": district,
        "content_provenance": content_provenance,
        "record_date": record_date,
    }


def _station_district(station: dict) -> tuple[Optional[str], Optional[str]]:
    if not station:
        return None, None
    district = station.get("district")
    district_name = district.get("name") if isinstance(district, dict) else district
    return station.get("code"), district_name


# ── FIR ──────────────────────────────────────────────────────────────────

def _fir_header(fir: FirRecord) -> str:
    """
    Compact structured line prepended to every FIR-derived chunk: station,
    district, FIR display code, sections, incident date. Not itself a
    source of graph ground truth (M6a writes those fields directly) —
    purely a lexical anchor for semantic/BM25 retrieval.
    """
    station = fir.police_station
    station_name = station.get("name") or ""
    district = station.get("district")
    district_name = district.get("name") if isinstance(district, dict) else (district or "")
    sections = ", ".join(
        f"{s.get('section_code')} {s.get('act')}".strip()
        for s in fir.child_rows("fir_section") if s.get("section_code")
    )
    parts = [f"FIR {fir.fir_display_code or fir.fir_id}", station_name, district_name]
    if sections:
        parts.append(f"Sections: {sections}")
    if fir.raw.get("incident_datetime"):
        parts.append(f"Incident: {fir.raw['incident_datetime']}")
    return " | ".join(p for p in parts if p)


def render_fir(fir: FirRecord) -> list[Document]:
    """
    One Document per genuine free-text field on this FIR bundle: the main
    narrative, crime-scene description, reporting-delay reason, every
    zimni entry with actual content, and the free-text tails on
    fir_position/chalaan_dispatch/chalaan_outcome. A null/blank field
    produces no Document — an FIR with an empty narrative simply
    contributes fewer chunks, never a placeholder one.
    """
    station_code, district = _station_district(fir.police_station)
    header = _fir_header(fir)
    docs: list[Document] = []

    def _add(field_label: str, text: Optional[str], suffix: str) -> None:
        if not text or not text.strip():
            return
        docs.append(Document(
            text=f"{header}\n{field_label}: {text.strip()}",
            metadata=_meta(
                "fir_narrative", fir.fir_id,
                source=f"psrms/fir/{fir.fir_id}#{suffix}",
                station_code=station_code, district=district,
                content_provenance=fir.source,
                record_date=fir.raw.get("incident_datetime"),
            ),
        ))

    _add("Narrative", fir.narrative_text, "narrative")
    _add("Crime scene", fir.crime_scene_location, "crime_scene")
    _add("Reporting delay reason", fir.raw.get("reporting_delay_reason"), "delay_reason")

    for z in fir.child_rows("fir_zimni"):
        row_id = z.get("id") or z.get("entry_number")
        _add(f"Zimni entry {z.get('entry_number')}", z.get("entry_text"), f"zimni_{row_id}")

    for p in fir.child_rows("fir_position"):
        _add("Position remarks", p.get("remarks"), f"position_{p.get('id')}")
        _add("Pending challan objections", p.get("pending_challan_objections"), f"position_obj_{p.get('id')}")

    for cd in fir.child_rows("chalaan_dispatch"):
        _add("Forensic sample note", cd.get("forensic_sample_note"), f"chalaan_dispatch_{cd.get('id')}")
        _add("Property involved", cd.get("property_involved"), f"chalaan_property_{cd.get('id')}")

    for co in fir.child_rows("chalaan_outcome"):
        _add("Court order detail", co.get("court_order_detail"), f"chalaan_outcome_{co.get('id')}")

    return docs


# ── CMS ──────────────────────────────────────────────────────────────────

def render_cms(cms: CmsComplaint) -> list[Document]:
    """One Document per complaint's one_line_summary — the only free text CMS carries."""
    if not cms.one_line_summary or not cms.one_line_summary.strip():
        return []
    header_parts = [f"Complaint {cms.complaint_id}"]
    if cms.case_tag_number:
        header_parts.append(f"Tag: {cms.case_tag_number}")
    if cms.raw.get("police_station_name"):
        header_parts.append(cms.raw["police_station_name"])
    header = " | ".join(header_parts)
    return [Document(
        text=f"{header}\nSummary: {cms.one_line_summary.strip()}",
        metadata=_meta(
            "cms_complaint", cms.complaint_id,
            source=f"cms/complaint/{cms.complaint_id}#summary",
            station_code=cms.raw.get("police_station_code"),
            content_provenance=cms.complainant.get("source"),
            record_date=cms.raw.get("submitted_at"),
        ),
    )]


# ── PKM ──────────────────────────────────────────────────────────────────

# Only these two of the seven service types carry free text worth
# embedding (measured live — see decision record). The other five
# (character_certificate, driving_license, tenant_registration,
# employee_registration, vehicle_verification) are entirely structured
# fields, handled by M6b's cross-silo linking instead.
_PKM_FREE_TEXT_FIELDS = {
    "loss_report": ("lost_item_description", "Lost item"),
    "women_violence_report": ("incident_description", "Incident"),
}


def render_pkm(pkm: PkmApplication) -> list[Document]:
    record = pkm.service_record()
    if not record:
        return []
    field, label = _PKM_FREE_TEXT_FIELDS.get(record["service_type"], (None, None))
    if not field or not record.get(field) or not record[field].strip():
        return []
    header = f"PKM application {pkm.application_id} | {record['service_type']}"
    return [Document(
        text=f"{header}\n{label}: {record[field].strip()}",
        metadata=_meta(
            "pkm_application", pkm.application_id,
            source=f"pkm/application/{pkm.application_id}#{field}",
            content_provenance=pkm.applicant.get("source"),
            record_date=pkm.raw.get("submitted_at"),
        ),
    )]


# ── Roznamcha ────────────────────────────────────────────────────────────

def render_roznamcha(entry: RoznamchaEntry) -> list[Document]:
    """
    Station-scoped, no case: roznamcha entries stay case-less under
    "Case = FIR" (confirmed in the decision record — an inferred FIR link
    by date/station proximity is exactly the kind of unstated inference
    this platform's SAME_AS/pending discipline refuses to make elsewhere).
    """
    if not entry.entry_text or not entry.entry_text.strip():
        return []
    header = f"Roznamcha {entry.entry_id}"
    if entry.entry_date:
        header += f" | {entry.entry_date}"
    return [Document(
        text=f"{header}\n{entry.entry_text.strip()}",
        metadata=_meta(
            "roznamcha_entry", entry.entry_id,
            source=f"psrms/roznamcha/{entry.entry_id}",
            station_code=entry.police_station_id,
            record_date=entry.entry_date,
        ),
    )]
