# ============================================================
# Muhafiz Data API — case provisioning (M4 of the migration,
# docs/decisions/0001-muhafiz-api-migration.md)
#
# "Case = FIR" (confirmed in the decision record): one `cases` row per FIR,
# keyed on the FIR's own id — no separate case-numbering scheme invented.
# CMS complaints and PKM applications that escalated into a real FIR
# attach to that FIR's case; everything else (unlinked CMS/PKM, criminal
# records, roznamcha) stays case-less, matching the existing
# "case_id is optional, not required" design in
# src/ingestion/service.py's ingest_documents()/ingest_file().
#
# Everything in this module is a PURE function over already-fetched
# records — no Postgres, no HTTP. scripts/sync_muhafiz_cases.py is the
# operational entry point that calls these and writes the results;
# keeping the derivation logic here, DB-free, is what makes it directly
# unit-testable against the real recorded snapshot (see
# tests/test_muhafiz_cases.py) and reusable from M9's sync script when it
# needs to decide a CMS/PKM record's case_id at ingestion time.
# ============================================================

from __future__ import annotations

from datetime import date
from typing import Optional

from src.data_gateway.muhafiz_api.models import CmsComplaint, FirRecord, PkmApplication


def case_fields_from_fir(fir: FirRecord) -> dict:
    """
    Derive a `cases` table row's fields from one FIR bundle. Returns a
    plain dict — the caller (scripts/sync_muhafiz_cases.py) is responsible
    for turning this into (or updating) a `Case` ORM row; keeping the ORM
    out of this module is what lets it be tested with no database at all.

    case_id = fir_id (not fir_display_code): fir_id is the API's own
    stable identifier ("fir-1001-26"), immune to the same display-code
    ever being reused; fir_display_code is instead recorded as fir_number,
    matching the Case model's existing field for it.
    """
    station = fir.police_station
    return {
        "case_id": fir.fir_id,
        "fir_number": fir.fir_display_code,
        "crime_category": _crime_category(fir),
        "investigation_officer": _current_investigating_officer(fir),
        "police_station": station.get("name"),
        "incident_date": _date_only(fir.raw.get("incident_datetime")),
        "investigation_status": _current_status(fir),
        "location": fir.crime_scene_location or station.get("name"),
    }


def _date_only(iso_datetime: Optional[str]) -> Optional[date]:
    """
    '2026-08-18T15:10:00Z' -> date(2026, 8, 18). Case.incident_date is a
    Date column; the API's incident_datetime is a full timestamp string.

    Bug found running the real M9 sync against a live database (never
    caught by unit tests, which only ever checked the returned value
    against a fake session that doesn't validate types): this used to
    return the sliced STRING, not an actual date object. asyncpg's DATE
    binding requires a real date/datetime.date — a plain string reached
    the driver as-is and failed with
    "AttributeError: 'str' object has no attribute 'toordinal'" on the
    very first INSERT. Mirrors scripts/load_cases.py's own
    _parse_date()'s use of date.fromisoformat() for exactly this reason.
    """
    if not iso_datetime or len(iso_datetime) < 10:
        return None
    try:
        return date.fromisoformat(iso_datetime[:10])
    except ValueError:
        return None


def _crime_category(fir: FirRecord) -> Optional[str]:
    """
    Case.crime_category is one free-text field; a real FIR can carry
    several acts (measured live: PPC + Arms Ordinance on the same FIR is
    common). Joins the distinct acts in the order they appear on
    fir_section, rather than picking just the first one, so the category
    reflects the whole FIR, not an arbitrary row.
    """
    acts = []
    for s in fir.child_rows("fir_section"):
        act = s.get("act")
        if act and act not in acts:
            acts.append(act)
    return ", ".join(acts) if acts else None


def split_crime_category(raw: Optional[str]) -> list[str]:
    """
    [Legal-code semantic layer] The reverse of `_crime_category()`'s own
    `", ".join(acts)` above — same split point, same trim, first-seen-order
    dedup preserved. Kept right beside the join it reverses so the two stay
    in sync by construction, not by convention.

    Public (unlike `_crime_category`) — reused outside this module by
    `src/pipeline/xagg.py` (real-time per-act aggregation/keyword
    filtering) and `scripts/load_legal_code_acts.py` (detecting every
    distinct act actually present in `cases.crime_category`), matching this
    module's own stated "reusable... when it needs to decide" design goal
    (see module docstring). Returns `[]` for `None`/blank input.
    """
    if not raw:
        return []
    seen: dict[str, None] = {}
    for part in raw.split(","):
        act = part.strip()
        if act:
            seen.setdefault(act, None)
    return list(seen.keys())


def _current_investigating_officer(fir: FirRecord) -> Optional[str]:
    """
    fir_investigating_officer can have more than one row over a case's
    life (an FIR can change hands). Prefers a row with no assigned_to
    (still active); falls back to the last row in the list (measured
    live: rows are not guaranteed sorted, but the last entry is the best
    available guess absent a reliable ordering field — assigned_from is
    often null too).
    """
    officers = fir.child_rows("fir_investigating_officer")
    if not officers:
        return None
    for o in officers:
        if not o.get("assigned_to") and o.get("officer_name"):
            return o["officer_name"]
    return officers[-1].get("officer_name")


def _current_status(fir: FirRecord) -> Optional[str]:
    """
    "Current position = latest row for a given fir_id" (muhafiz_schema.dbml.txt's
    own note on fir_position) — sorted by status_date when present, else
    the last row in API order. Falls back to chalaan_outcome.case_outcome
    when fir_position is entirely empty or every row's `position` is null
    (measured live: 65/94 fir_position rows have a null `position`) —
    an FIR that reached a court outcome without ever getting an explicit
    position row logged is still not status-unknown.
    """
    positions = [p for p in fir.child_rows("fir_position") if p.get("position")]
    if positions:
        dated = [p for p in positions if p.get("status_date")]
        if dated:
            return max(dated, key=lambda p: p["status_date"])["position"]
        return positions[-1]["position"]

    for outcome in fir.child_rows("chalaan_outcome"):
        if outcome.get("case_outcome"):
            return outcome["case_outcome"]

    return None


# ── cross-silo escalation matching (M6b uses the same keys for graph edges;
# this module only resolves "which case, if any" for case *provisioning*
# purposes — M9's sync script calls these to decide a rendered CMS/PKM
# Document's case_id at ingestion time) ─────────────────────────────────

def build_e_tag_index(firs: list[FirRecord]) -> dict[str, str]:
    """e_tag_number -> fir_id, for every FIR that has one set (measured
    live: 5/73). CMS complaints that escalated into an FIR carry the same
    tag as case_tag_number (confirmed in muhafiz_schema.dbml.txt, revision 11)."""
    return {fir.e_tag_number: fir.fir_id for fir in firs if fir.e_tag_number}


def build_display_code_index(firs: list[FirRecord]) -> dict[str, str]:
    """fir_display_code -> fir_id. Used to resolve PKM's forwarded_fir_number,
    which is recorded as a display code ("97/26"), not the API's fir_id slug."""
    return {fir.fir_display_code: fir.fir_id for fir in firs if fir.fir_display_code}


def resolve_cms_case_id(cms: CmsComplaint, e_tag_index: dict[str, str]) -> Optional[str]:
    if not cms.case_tag_number:
        return None
    return e_tag_index.get(cms.case_tag_number)


def resolve_pkm_case_id(pkm: PkmApplication, display_code_index: dict[str, str]) -> Optional[str]:
    """Only women_violence_report applications carry forwarded_fir_number
    (PkmApplication.forwarded_fir_number already returns None for every
    other service type)."""
    if not pkm.forwarded_fir_number:
        return None
    return display_code_index.get(pkm.forwarded_fir_number)
