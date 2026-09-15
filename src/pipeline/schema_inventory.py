# ============================================================
# Record inventory — [Gold-QA fix — Module 151]
#
# PURPOSE:
# The one place that answers "what fields do our records actually have?"
# for the grounding verifier. A generated answer may state that the
# platform's records have NO field for something — no interview-statement
# text on a witness record (KB2), no chain-of-custody field on a
# women-violence report (KB5), no inquest / post-mortem record anywhere
# (KB9). Nothing in a document corpus can cite an absence, so the LLM
# judge rejects every such claim as "unsupported" (MODULE151_RESULT.md
# §1). The claim is about the SHAPE of the database, and the shape is a
# fact this module can check directly.
#
# TWO SOURCES, UNIONED — AND WHY NEITHER ALONE IS ENOUGH:
#
#   1. DECLARED — `DECLARED_RECORD_FAMILIES` below: every record family the
#      platform receives from the Muhafiz Data API, with the columns each
#      row carries, exactly as the API returns them. The authority for a
#      register's shape is the register: Module 95 established (xagg.py,
#      `_WEAPON_REGISTER_FIELDS`) that the graph is a PROJECTION which
#      drops columns the register does carry — deriving "no such field"
#      from the graph alone would report absences that are false of the
#      records an SHO actually holds. `tests/test_schema_inventory.py` pins
#      every observed family here to the recorded API snapshot
#      (`tests/fixtures/muhafiz_api_snapshot.json`) and to Module 95's own
#      constant, so the build fails if the API's shape drifts from this
#      list rather than the verifier silently grounding a stale claim.
#
#   2. LIVE GRAPH — `keys(n)` per node label and per
#      `StructuredRecord.record_type`, read from AGE at verify time (cached
#      for `_CACHE_TTL_S`). Anything the platform has ADDED on top of the
#      API's rows (Module 22's report timestamps, Module 95's zimni counts,
#      cross-silo properties) lives only here. It can only ever ADD fields
#      to the declared inventory, i.e. only ever make an absence claim
#      HARDER to confirm. If the graph is unreachable the declared
#      inventory is used alone and the fact is logged.
#
# WHAT THE INVENTORY IS USED FOR, AND THE DIRECTION OF EVERY ERROR:
# `fields_serving()` is the generalisation of Module 95's
# `_missing_custody_controls()` — "which columns, if any, serve this
# concept?" as a substring scan over normalised identifiers. A NON-EMPTY
# result REFUTES a claimed absence (the field exists: fabricated
# negative, rejection stands). An EMPTY result is one of two conditions
# the verifier requires before it treats an absence as grounded (see
# verifier.py's Module 151 block for the other). Every degraded path —
# unknown scope, no usable keyword, graph down — makes confirmation
# harder, never easier.
# ============================================================

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# ── 1. Declared record families ───────────────────────────────────────────
#
# Keys are the family names the schema-claim classifier is allowed to name
# as a claim's SCOPE (prompts/schema_claim.txt lists the same names).
# Values are the row's columns as the Muhafiz Data API returns them
# (`tests/fixtures/muhafiz_api_snapshot.json`); the four PKM service tables
# that snapshot carries only as null sub-objects are taken from
# muhafiz_schema.dbml.txt and marked below.
DECLARED_RECORD_FAMILIES: dict[str, tuple[str, ...]] = {
    # psrms.fir — the FIR itself. The four legacy crime_scene_* columns are
    # still returned alongside the merged one (models.py's header).
    "fir": (
        "fir_id", "fir_display_code", "serial_number", "e_tag_number", "form_type",
        "police_station_id", "police_station", "report_datetime",
        "station_departure_datetime", "incident_datetime",
        "complainant_full_name", "complainant_father_name", "complainant_address",
        "complainant_cnic", "complainant_phone", "victim_name",
        "crime_scene_location", "crime_scene_description", "crime_scene_distance_km",
        "crime_scene_direction", "crime_scene_beat_number", "reporting_delay_reason",
        "recording_officer_name", "recording_officer_designation",
        "recording_officer_belt_no", "recording_officer_phone", "narrative_text",
        "source", "created_at", "updated_at",
    ),
    "fir_section": (
        "id", "fir_id", "section_code", "act", "action", "action_date",
        "zimni_index_no", "amendment_datetime", "remarks", "updated_at",
    ),
    "fir_accused": (
        "id", "fir_id", "full_name", "father_name", "spouse_name", "cnic", "address_text",
        "gender", "nationality", "occupation", "education", "age", "physical_description",
        "relationship_to_victim", "relationship_to_complainant", "arrest_status",
        "custody_position", "nominated_date", "arrested_date", "arresting_officer_name",
        "other_arresting_officer_name", "warrant_date", "criminal_record_ref",
        "accused_type", "sections_applied", "updated_at",
    ),
    # psrms.fir_witness — identity and contact only. The DBML's own note:
    # "NO statement content field of any kind, and none should ever be
    # added: witness statement content is handled by the courts / law
    # department, not stored in PSRMS." That sentence is KB2's gold.
    "fir_witness": (
        "id", "fir_id", "full_name", "father_or_spouse_name", "cnic", "address_text",
        "police_station_of_residence_id", "other_district", "nationality", "gender",
        "phone", "age", "relationship_to_complainant", "guardian_cnic", "witness_type",
        "ethnicity", "religion", "updated_at",
    ),
    "fir_investigating_officer": (
        "id", "fir_id", "officer_name", "designation", "belt_no",
        "assigned_from", "assigned_to", "updated_at",
    ),
    "fir_position": (
        "id", "fir_id", "position", "status_date", "prosecutor_name",
        "cross_certificate_ref", "pending_challan_objections", "remarks", "updated_at",
    ),
    "fir_zimni": (
        "id", "fir_id", "entry_number", "entry_date", "officer_name",
        "entry_text", "entry_type", "updated_at",
    ),
    "fir_zimni_index": (
        "id", "fir_id", "sr_no", "zimni_report_date", "officer_name", "updated_at",
    ),
    "chalaan_dispatch": (
        "id", "fir_id", "dispatch_datetime", "sections_and_accused_chalaaned",
        "accused_names", "witness_names", "custody_classification",
        "property_involved", "forensic_sample_note", "updated_at",
    ),
    "chalaan_outcome": (
        "id", "fir_id", "challan_reached_court_date", "case_outcome",
        "court_order_detail", "updated_at",
    ),
    "malkhana_register": (
        "id", "fir_display_code", "sr_no", "reg_no", "date_entered",
        "item_detail", "quantity", "condition", "updated_at",
    ),
    # Identical to xagg.py's `_WEAPON_REGISTER_FIELDS` (Module 95); a test
    # asserts the two stay equal rather than importing xagg here.
    "weapon_register": (
        "id", "fir_display_code", "sr_no", "item_detail", "caliber_or_bore",
        "quantity", "license_status", "recovered_from", "date_entered",
        "condition", "updated_at",
    ),
    "roznamcha": (
        "id", "police_station_id", "entry_date", "entry_number", "entry_text", "updated_at",
    ),
    "cms_complaint": (
        "complaint_id", "case_tag_number", "police_station_name", "police_station_code",
        "complainant_person_id", "complainant", "one_line_summary", "submitted_at",
        "method", "language", "updated_at",
    ),
    # The person object nested on a CMS complaint / PKM application.
    "person": (
        "id", "cnic", "full_name", "father_name", "address_text", "phone",
        "source", "updated_at",
    ),
    "pkm_application": (
        "application_id", "service_type", "applicant_person_id", "applicant",
        "police_station_id", "police_station", "submitted_at", "status",
        "certificate_number", "language", "updated_at",
        "character_certificate", "driving_license", "tenant_registration",
        "employee_registration", "vehicle_verification", "loss_report",
        "women_violence_report",
    ),
    "women_violence_report": (
        "application_id", "incident_description", "written_by",
        "assigned_women_station_id", "assigned_women_station", "assigned_io_name",
        "assigned_io_badge_no", "forwarded_fir_number", "updated_at",
    ),
    "driving_license": (
        "application_id", "license_category", "dob", "blood_group", "application_ref_no",
        "photograph_ref", "is_renewal_or_duplicate", "previous_license_no", "reason",
        "updated_at",
    ),
    "vehicle_verification": (
        "application_id", "vehicle_registration_no", "vehicle_make", "vehicle_model",
        "vehicle_chassis_no", "vehicle_engine_no", "verification_result",
        "cross_checked_against_psrms", "updated_at",
    ),
    # DBML-declared only (null on every snapshot record) — muhafiz_schema.dbml.txt.
    "character_certificate": (
        "application_id", "passport_no", "nationality", "purpose", "destination_country",
        "photograph_ref", "signature_ref", "issuing_officer_name",
    ),
    "tenant_registration": (
        "application_id", "owner_person_id", "tenant_person_id", "property_address",
        "rent_agreement_ref", "tenant_photograph_ref", "registration_date",
    ),
    "employee_registration": (
        "application_id", "employer_person_id", "employee_person_id",
        "employee_photograph_ref", "affidavit_ref",
    ),
    "loss_report": (
        "application_id", "lost_item_description", "approx_loss_date", "approx_loss_location",
    ),
    "criminal_record": (
        "id", "subject_cnic", "subject_full_name", "external_record_ref",
        "offense_summary", "source_case_ref", "conviction_status", "updated_at",
    ),
    "police_station": ("id", "name", "code", "district"),
    "district": ("id", "name", "province"),
}

# Families the API snapshot observes with rows (pinned by the test); the
# rest are DBML-declared.
SNAPSHOT_OBSERVED_FAMILIES: tuple[str, ...] = (
    "fir", "fir_section", "fir_accused", "fir_witness", "fir_investigating_officer",
    "fir_position", "fir_zimni", "fir_zimni_index", "chalaan_dispatch", "chalaan_outcome",
    "malkhana_register", "weapon_register", "roznamcha", "cms_complaint", "person",
    "pkm_application", "women_violence_report", "driving_license", "vehicle_verification",
    "criminal_record", "police_station",
)

# One line per family for the classifier prompt — what the family IS, so a
# claim about "witness records" or "women-violence reports" resolves to the
# right scope. Kept short: this is rendered into every classifier call.
FAMILY_DESCRIPTIONS: dict[str, str] = {
    "fir": "the FIR (case) record itself — complainant, victim name, scene, timestamps, recording officer, narrative",
    "fir_section": "sections of law applied to an FIR",
    "fir_accused": "an accused person on an FIR",
    "fir_witness": "a witness on an FIR (identity and contact only)",
    "fir_investigating_officer": "an investigating officer assigned to an FIR",
    "fir_position": "an FIR's case-position / court-stage entry",
    "fir_zimni": "a daily-diary (zimni) investigation entry",
    "fir_zimni_index": "the zimni tracking index (serial, date, officer)",
    "chalaan_dispatch": "a challan (charge sheet) dispatch to court",
    "chalaan_outcome": "a challan's court outcome",
    "malkhana_register": "the malkhana (property) register — recovered / deposited items",
    "weapon_register": "the weapon register — recovered weapons and ammunition",
    "roznamcha": "a station daily-register (roznamcha) entry",
    "cms_complaint": "a walk-in / online CMS complaint",
    "person": "a person object nested on a CMS complaint or PKM application",
    "pkm_application": "a citizen-service (PKM) application; the service-specific sub-record is one of the families below",
    "women_violence_report": "a women-violence report filed as a PKM application",
    "driving_license": "a driving-licence application",
    "vehicle_verification": "a vehicle-verification application",
    "character_certificate": "a character-certificate application",
    "tenant_registration": "a tenant-registration application",
    "employee_registration": "an employee-registration application",
    "loss_report": "a loss-report application",
    "criminal_record": "a criminal-record entry",
    "police_station": "a police station",
    "district": "a district",
}

ANY_SCOPE = "any"

# ── 2. Live graph ─────────────────────────────────────────────────────────
#
# Which declared family each graph label's properties are unioned into.
# Person is deliberately fanned into every family whose rows become Person
# nodes: a witness IS a Person node in the graph, so any property the graph
# holds on a Person is, for the purpose of refuting "witness records have no
# field for X", a field witness records have.
GRAPH_LABEL_FAMILIES: dict[str, tuple[str, ...]] = {
    "Incident": ("fir",),
    "Case": ("fir",),
    "Person": ("person", "fir_witness", "fir_accused", "fir"),
    "Officer": ("fir_investigating_officer", "fir"),
    "Weapon": ("weapon_register",),
    "Vehicle": ("vehicle_verification",),
    "PhoneNumber": ("person",),
    "Address": ("person",),
    "PoliceStation": ("police_station",),
    "District": ("district",),
    "Document": ("document",),
}

# `StructuredRecord.record_type` values map to the family of the same name.
_CACHE_TTL_S = 300.0


@dataclass
class RecordInventory:
    """Field names per record family, plus where they came from."""

    families: dict[str, set[str]] = field(default_factory=dict)
    graph_labels: set[str] = field(default_factory=set)
    record_types: set[str] = field(default_factory=set)
    source: str = "declared"

    @property
    def field_count(self) -> int:
        return sum(len(v) for v in self.families.values())

    def scope_fields(self, scope: Optional[str]) -> Optional[dict[str, set[str]]]:
        """The families a claim's scope covers, or None when the scope names
        a family this inventory does not know — an unknown scope can confirm
        nothing."""
        if not scope or scope.strip().lower() == ANY_SCOPE:
            return dict(self.families)
        key = scope.strip().lower()
        if key in self.families:
            return {key: self.families[key]}
        return None

    def render(self) -> str:
        """The inventory as the classifier prompt shows it."""
        lines = []
        for fam in sorted(self.families):
            desc = FAMILY_DESCRIPTIONS.get(fam, "")
            head = f"- {fam}" + (f" ({desc})" if desc else "")
            lines.append(f"{head}: {', '.join(sorted(self.families[fam]))}")
        return "\n".join(lines)


def declared_inventory() -> RecordInventory:
    inv = RecordInventory(
        families={k: set(v) for k, v in DECLARED_RECORD_FAMILIES.items()},
        source="declared",
    )
    return inv


_cache: dict[str, object] = {"at": 0.0, "inv": None}


async def _read_graph_keys() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(`{label: keys}`, `{record_type: keys}`) off the live graph."""
    from src.graph import age_client  # local import: keeps this module importable without asyncpg

    by_label: dict[str, set[str]] = {}
    rows = await age_client.execute_cypher(
        "MATCH (n) UNWIND keys(n) AS k RETURN DISTINCT labels(n) AS l, k",
        columns=["l", "k"],
    )
    for r in rows or []:
        labs = r.get("l")
        lab = labs[0] if isinstance(labs, list) and labs else labs
        key = r.get("k")
        if lab and key:
            by_label.setdefault(str(lab), set()).add(str(key))

    by_type: dict[str, set[str]] = {}
    rows = await age_client.execute_cypher(
        "MATCH (n:StructuredRecord) UNWIND keys(n) AS k RETURN DISTINCT n.record_type AS t, k",
        columns=["t", "k"],
    )
    for r in rows or []:
        t, key = r.get("t"), r.get("k")
        if t and key:
            by_type.setdefault(str(t), set()).add(str(key))
    return by_label, by_type


async def live_inventory(*, ttl_s: float = _CACHE_TTL_S) -> RecordInventory:
    """Declared families unioned with the live graph's property keys.

    Cached for `ttl_s`; on any graph failure returns the declared inventory
    alone (logged) — which can only make an absence claim harder to confirm,
    never easier, because the graph only ever ADDS fields.
    """
    now = time.monotonic()
    cached = _cache.get("inv")
    if cached is not None and now - float(_cache["at"]) < ttl_s:  # type: ignore[arg-type]
        return cached  # type: ignore[return-value]

    inv = declared_inventory()
    try:
        by_label, by_type = await _read_graph_keys()
    except Exception as exc:  # noqa: BLE001 — degrade to declared-only
        logger.warning(
            "Schema inventory: live graph read failed (%s: %s) — using the declared "
            "record inventory alone (%d families, %d fields).",
            type(exc).__name__, exc, len(inv.families), inv.field_count,
        )
        inv.source = "declared-only (graph unavailable)"
        _cache["inv"], _cache["at"] = inv, now
        return inv

    for label, keys in by_label.items():
        inv.graph_labels.add(label)
        for fam in GRAPH_LABEL_FAMILIES.get(label, ()):
            inv.families.setdefault(fam, set()).update(keys)
    for rtype, keys in by_type.items():
        inv.record_types.add(rtype)
        inv.families.setdefault(rtype, set()).update(keys)
    inv.source = "declared+graph"
    logger.info(
        "Schema inventory: %d families, %d fields (declared API shape + live graph: "
        "%d labels, %d record types).",
        len(inv.families), inv.field_count, len(inv.graph_labels), len(inv.record_types),
    )
    _cache["inv"], _cache["at"] = inv, now
    return inv


def reset_cache() -> None:
    _cache["inv"], _cache["at"] = None, 0.0


# ── 3. Concept-to-field matching ──────────────────────────────────────────

_NORM_RE = re.compile(r"[^a-z0-9]+")

# Generic words a concept keyword list must not be reduced to: "text" would
# match narrative_text / entry_text / address_text and "date" every *_date
# column, refuting or — worse, if they were the only keyword — confirming
# nothing meaningful. Dropping them can only remove a keyword; a claim left
# with NO usable keyword is unconfirmable (the judge's verdict stands).
KEYWORD_STOPLIST: frozenset[str] = frozenset({
    "field", "fields", "record", "records", "column", "columns", "table", "data",
    "text", "info", "information", "detail", "details", "note", "notes", "entry",
    "entries", "status", "date", "datetime", "time", "type", "name", "names", "number",
    "id", "ref", "file", "files", "value", "values", "flag", "system", "schema",
    "register", "report", "reports", "log", "logs", "case", "cases", "fir", "firs",
    "police", "our", "the", "any", "none", "no", "not",
})
_MIN_KEYWORD_LEN = 3  # "age" must stay usable; a short keyword can only over-REFUTE, which is the safe direction


def normalise_identifier(s: str) -> str:
    return _NORM_RE.sub("", (s or "").lower())


_COMPOUND_RE = re.compile(r"[\s_\-/]+")


def _is_single_stem(raw: str) -> bool:
    """One word — no space, underscore, hyphen or slash inside it."""
    return len([p for p in _COMPOUND_RE.split(raw.strip()) if p]) == 1


def usable_keywords(keywords: Iterable[str]) -> list[str]:
    """Keywords after normalisation, stoplist and length floor; order kept."""
    out: list[str] = []
    for kw in keywords or ():
        raw = str(kw or "").strip().lower()
        if not raw or raw in KEYWORD_STOPLIST:
            continue
        norm = normalise_identifier(raw)
        if len(norm) < _MIN_KEYWORD_LEN or norm in KEYWORD_STOPLIST:
            continue
        if norm not in out:
            out.append(norm)
    return out


def has_single_stem_keyword(keywords: Iterable[str]) -> bool:
    """
    [Measured live, MODULE151_RESULT.md §6] A keyword list made ONLY of
    invented compounds — "investigative_team", "expedited_time",
    "magistrate_referral" — matches no column by construction, so it can
    never refute anything, and a claim carrying such a list would be
    "confirmed" for free. A concept the inventory can actually check is one
    a column name could contain as ONE stem (inquest, custody, statement,
    age). At least one such usable stem is required; otherwise the claim is
    unconfirmable and the judge's rejection stands.
    """
    for kw in keywords or ():
        raw = str(kw or "").strip().lower()
        if not raw or not _is_single_stem(raw):
            continue
        if usable_keywords([raw]):
            return True
    return False


def fields_serving(
    keywords: Iterable[str],
    scope: Optional[str],
    inventory: RecordInventory,
) -> Optional[list[str]]:
    """
    Every `family.field` (and every family NAME) in `scope` that contains
    one of `keywords` — the generalisation of Module 95's
    `_missing_custody_controls()` set difference, in the refuting direction.

    Returns None when the question cannot be asked at all: an unknown
    scope, no usable keyword, or no SINGLE-STEM keyword (see
    `has_single_stem_keyword()`). Callers must treat None as "cannot
    confirm", never as "confirmed absent".
    """
    fams = inventory.scope_fields(scope)
    if fams is None:
        return None
    kws = usable_keywords(keywords)
    if not kws or not has_single_stem_keyword(keywords):
        return None
    hits: list[str] = []
    for fam in sorted(fams):
        fam_norm = normalise_identifier(fam)
        if any(kw in fam_norm for kw in kws):
            hits.append(fam)
        for fld in sorted(fams[fam]):
            fld_norm = normalise_identifier(fld)
            if any(kw in fld_norm for kw in kws):
                hits.append(f"{fam}.{fld}")
    return hits
