"""Recover facts from the structured source, without inventing their meaning.

WHY THIS EXISTS

The extracted schema lost information the source system always had, and
re-deriving it with a model reintroduced errors the source never made.
Measured against `tests/fixtures/muhafiz_api_snapshot.json`:

    SOURCE                              EXTRACTED LEGACY
    weapon_register: 32 rows            weapons: 96 rows
    caliber_or_bore: "30 بور"           quantity: 30        <- the BORE
    quantity: "1 عدد + 6 گولیاں"         (free text, read as a number)
    report_datetime: 73/73              reported_at: 0/73
    complainant_cnic: 71/73             894 unresolved person rows
    police_station: {id: "PS-ISB-CYBER"} matched by name

The source keeps bore and quantity in SEPARATE FIELDS. Nothing had to
infer that a pistol's calibre was a count; the flattening did it.

So these facts are imported, not discovered. No model runs here.

WHAT IS AND IS NOT VALIDATED

Only fields whose meaning is established by the source contract below
become `status='validated'` assertions. Everything else is preserved as a
candidate for review. A field being present, or numeric-looking, is not
evidence that anyone knows what it means.

Two consequences worth stating because both were live defects:

  * `quantity` is TEXT. "1 عدد + 6 گولیاں" means "1 item plus 6 bullets"
    and is not a number in any unit. It is never summed. A concept with
    value_type number would make `SUM(quantity)` compile and return the
    meaningless 972 the legacy table produces today.

  * A weapon_register ROW is a register entry, not a physical weapon.
    Counting rows answers "how many register entries", and the two are
    different questions. The concept says so in its grain.

TIME IS NOT ONE THING

The source carries `report_datetime`, `incident_datetime`, `created_at`
and `station_departure_datetime`. None of them is a registration time,
and there is no registration field in this snapshot. They are imported as
distinct concepts with distinct meanings; "FIRs registered this month"
remains unanswerable until a registration time exists, which is a correct
outcome rather than a gap to paper over with the nearest timestamp.

Offsets are preserved as given. Every value in this snapshot carries `Z`.

PROVENANCE

Each record is stored as its canonical JSON serialization, and every
assertion points at the exact character range within that serialization
which carries its value. `source_record_key` records endpoint, primary
key and JSON pointer, so a claim can be traced to one field of one
record of one endpoint.

IDEMPOTENCE

Document, span, entity and assertion ids are derived from content, so a
re-import of an unchanged snapshot writes nothing new. A changed record
produces a new document revision that supersedes the old one; the earlier
assertions remain, and are not silently rewritten.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from src import config

from .ingestion import put_document, put_span, stable_id

# The extraction version recorded on assertions from this path. Distinct
# from the model extractor's version so the two can never be confused when
# coverage or accuracy is reviewed.
STRUCTURED_VERSION = "structured-source-v1"


@dataclass(frozen=True)
class FieldContract:
    """One source field whose meaning is established, not guessed."""

    pointer: str                 # JSON pointer within the record
    label: str
    meaning: str
    value_type: str              # text | number | boolean | date | entity
    grain: str
    unit: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationContract:
    """A relationship the SOURCE states, not one inferred from proximity."""

    label: str
    meaning: str
    grain: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntityContract:
    """A thing the source identifies with a stable key."""

    label: str
    meaning: str
    id_pointer: str              # the stable source identifier
    name_pointer: str
    grain: str
    identifier_name: str         # what that identifier IS


# ── The contract ────────────────────────────────────────────────────────
#
# Deliberately small. A field appears here only when its meaning is
# readable from the source structure itself. Everything omitted is kept as
# a candidate, which is the honest state for a field nobody has verified.

FIR_ENTITY = EntityContract(
    label="FIR",
    meaning="one First Information Report as recorded by the source system",
    id_pointer="/fir_id", name_pointer="/fir_display_code",
    grain="one FIR record", identifier_name="fir_id")

STATION_ENTITY = EntityContract(
    label="police station",
    meaning="a police station as identified by the source system",
    id_pointer="/police_station/id", name_pointer="/police_station/name",
    grain="one police station", identifier_name="police_station_id")

DISTRICT_ENTITY = EntityContract(
    label="district",
    meaning="the district a police station belongs to in the source system",
    id_pointer="/police_station/district/id",
    name_pointer="/police_station/district/name",
    grain="one district", identifier_name="district_id")

# A person is identified by CNIC, which the source supplies for every
# accused row and 71 of 73 complainants. MEASURED: 164 distinct CNICs
# across 198 person rows, 31 of them recurring across records.
#
# The extracted legacy schema holds 894 person rows for the same 73 cases
# with 746 merges pending, because it matched on names -- one name
# (ذیشان) appears 46 times and no rule can say how many people that is.
# A stable identifier removes the question rather than answering it.
PERSON_ENTITY = EntityContract(
    label="person",
    meaning="an individual identified by the CNIC recorded in the source",
    id_pointer="/cnic", name_pointer="/full_name",
    grain="one person, identified by CNIC", identifier_name="cnic")

# Roles are SEPARATE relations. A complainant and an accused are not the
# same relationship to a case, and collapsing them into "person involved"
# would make "who was accused" unanswerable.
COMPLAINANT_OF = RelationContract(
    label="complainant of",
    meaning="the person recorded as the complainant on this FIR",
    grain="one complainant per FIR record")

ACCUSED_IN = RelationContract(
    label="accused in",
    meaning="a person recorded in this FIR's accused list. A record of "
            "accusation as entered by the source, not a finding of guilt.",
    grain="one accused entry on one FIR")

REGISTERED_AT_STATION = RelationContract(
    label="recorded at police station",
    meaning="the police station this FIR is recorded under in the source. "
            "Jurisdiction, which is not the same as where the incident "
            "occurred.",
    grain="one station per FIR record")

STATION_IN_DISTRICT = RelationContract(
    label="station in district",
    meaning="the district a police station belongs to, as the source "
            "structures it. Administrative containment, not incident "
            "location.",
    grain="one district per police station")


# NO SYNONYM LISTS LIVE HERE.
#
# Each contract used to carry `aliases=("nominated", "نامزد", ...)` so a
# question could reach a concept by matching a word someone had typed next
# to it. That is a translation dictionary in the live path: it is stale the
# day it is written, it covers only the terms whoever wrote it thought of,
# and "vehicle theft" failed because nobody had thought of those two.
#
# The reading of a question is now done per request, and every form it
# proposes is checked against the records before anything searches for it
# (see QueryUnderstanding and EvidenceStore.ground_forms). A concept's
# `meaning` stays, because it states what a SOURCE FIELD is -- that
# report_datetime is NOT a registration time is a fact about the data, not
# a synonym for a question word.

FIR_FIELDS: tuple[FieldContract, ...] = (
    FieldContract("/fir_display_code", "FIR number",
                  "the FIR's display number as recorded",
                  "text", "one FIR record"),
    # Reporting time. NOT registration time -- the source has no
    # registration field, and substituting this one would answer a
    # different question than the one asked.
    FieldContract("/report_datetime", "reported at",
                  "the time the report was made, as supplied by the source. "
                  "This is NOT the registration time; the source carries no "
                  "registration timestamp.",
                  "date", "one FIR record"),
    FieldContract("/incident_datetime", "incident time",
                  "the time the incident is recorded to have occurred",
                  "date", "one FIR record"),
    # /created_at and /updated_at are DELIBERATELY NOT CONTRACTED.
    #
    # MEASURED: asked "When was FIR 1001/26 registered?", the planner
    # matched "registered" to the concept "record created at" and
    # answered with the source system's row timestamp. The concept's
    # meaning said "a system timestamp, not a fact about the incident"
    # and it was selected anyway -- a meaning written for a reader does
    # not stop a matcher.
    #
    # That is the one failure this contract exists to prevent: the
    # snapshot has no registration field, so "when was it registered"
    # must go unanswered. A bookkeeping timestamp about the ROW is not
    # evidence about the CASE, so it is not offered as an answer to any
    # question. It stays on the document as provenance, where it remains
    # available for audit but cannot be returned as a fact.
    FieldContract("/station_departure_datetime", "station departure time",
                  "time of departure from the station, where recorded",
                  "date", "one FIR record"),
    FieldContract("/reporting_delay_reason", "reporting delay reason",
                  "the stated reason for a delay in reporting, where given",
                  "text", "one FIR record"),
)

# Child rows. The grain wording is load-bearing: it is what stops a row
# count being reported as a count of physical objects.
WEAPON_FIELDS: tuple[FieldContract, ...] = (
    FieldContract("/item_detail", "weapon register item detail",
                  "the item as described in the weapon register entry",
                  "text", "one weapon register ENTRY, which may describe an "
                          "item together with its ammunition"),
    FieldContract("/caliber_or_bore", "calibre or bore",
                  "the calibre or bore as recorded, e.g. '30 بور'. A "
                  "descriptive label, never a count.",
                  "text", "one weapon register entry"),
    # TEXT, emphatically. Measured values include "1 عدد + 6 گولیاں".
    FieldContract("/quantity", "quantity as recorded",
                  "the quantity exactly as written in the register, which is "
                  "free text such as '1 عدد + 6 گولیاں' (one item plus six "
                  "rounds). It is not a number and must never be summed.",
                  "text", "one weapon register entry"),
    FieldContract("/license_status", "licence status",
                  "the licence status recorded for the item", "text",
                  "one weapon register entry"),
    FieldContract("/recovered_from", "recovered from",
                  "from whom or where the item was recorded as recovered",
                  "text", "one weapon register entry"),
)

# ── Other endpoints ─────────────────────────────────────────────────────

CRIMINAL_RECORD_ENTITY = EntityContract(
    label="criminal record",
    meaning="one prior criminal record entry as held by the source system",
    id_pointer="/id", name_pointer="/id",
    grain="one criminal record entry", identifier_name="criminal_record_id")

CRIMINAL_FIELDS: tuple[FieldContract, ...] = (
    FieldContract("/offense_summary", "offence summary",
                  "the offence as summarised in the prior record", "text",
                  "one criminal record entry"),
    FieldContract("/conviction_status", "conviction status",
                  "the conviction status as recorded in the prior record",
                  "text", "one criminal record entry"),
    # Free text naming an FIR and station. NOT a foreign key: it is a
    # human-written reference and resolving it would be a guess.
    FieldContract("/source_case_ref", "source case reference (as written)",
                  "the case this record refers to, exactly as written in the "
                  "source. Free text, not a resolved link to an FIR.",
                  "text", "one criminal record entry"),
)

ROZNAMCHA_ENTITY = EntityContract(
    label="roznamcha entry",
    meaning="one daily diary (roznamcha) entry recorded at a police station",
    id_pointer="/id", name_pointer="/entry_number",
    grain="one roznamcha entry", identifier_name="roznamcha_id")

ROZNAMCHA_FIELDS: tuple[FieldContract, ...] = (
    FieldContract("/entry_text", "roznamcha entry text",
                  "the diary entry as written", "text", "one roznamcha entry"),
    FieldContract("/entry_date", "roznamcha entry date",
                  "the date of the diary entry as recorded", "date",
                  "one roznamcha entry"),
    FieldContract("/entry_number", "roznamcha entry number",
                  "the entry's number within the station's diary", "text",
                  "one roznamcha entry"),
)

CMS_ENTITY = EntityContract(
    label="CMS complaint",
    meaning="one complaint submitted through the complaint management system",
    id_pointer="/complaint_id", name_pointer="/case_tag_number",
    grain="one CMS complaint", identifier_name="complaint_id")

CMS_FIELDS: tuple[FieldContract, ...] = (
    FieldContract("/one_line_summary", "complaint summary",
                  "the complaint's one-line summary as submitted", "text",
                  "one CMS complaint"),
    FieldContract("/submitted_at", "submitted at",
                  "when the complaint was submitted, as supplied. A "
                  "submission time, not an FIR registration time.",
                  "date", "one CMS complaint"),
    FieldContract("/method", "submission method",
                  "how the complaint was submitted, as recorded", "text",
                  "one CMS complaint"),
    FieldContract("/case_tag_number", "case tag number",
                  "the complaint's tag number as assigned", "text",
                  "one CMS complaint"),
)

PKM_ENTITY = EntityContract(
    label="PKM application",
    meaning="one citizen service application in the PKM system",
    id_pointer="/application_id", name_pointer="/service_type",
    grain="one PKM application", identifier_name="application_id")

PKM_FIELDS: tuple[FieldContract, ...] = (
    FieldContract("/service_type", "service type",
                  "the kind of service applied for, as recorded", "text",
                  "one PKM application"),
    FieldContract("/status", "application status",
                  "the application's status as recorded", "text",
                  "one PKM application"),
    FieldContract("/submitted_at", "submitted at",
                  "when the application was submitted, as supplied", "date",
                  "one PKM application"),
)

SUBJECT_OF_RECORD = RelationContract(
    label="subject of record",
    meaning="the person this record is about, identified by CNIC",
    grain="one subject per record")


ACCUSED_FIELDS: tuple[FieldContract, ...] = (
    # MEASURED: asked "is he nominated in any cases", the system answered
    # "the term 'nominated' is not defined in the catalog" -- an honest
    # refusal, and a real gap. نامزد is standard Pakistani police usage for
    # a person named as an accused in the FIR itself, and the source
    # records it on every accused row. It had no concept, so a routine
    # question about it could not be asked.
    FieldContract("/accused_type", "accused type",
                  "how this person entered the case, as recorded on the "
                  "accused row. The recorded value is usually 'نامزد' "
                  "(nominated -- named as an accused in the FIR itself). "
                  "MEASURED: 87 of 94 accused rows carry 'نامزد' and four "
                  "instead carry statute section numbers, which is a fault "
                  "in the source, not a type. The value is imported exactly "
                  "as recorded and is never cleaned up.",
                  "text", "one accused entry on one FIR"),
    FieldContract("/arrest_status", "arrest status",
                  "the arrest position of this accused as recorded, free "
                  "text such as 'زیر تفتیش' (under investigation), "
                  "'گرفتار' (arrested) or 'مفرور' (absconding). It is a "
                  "recorded status, not a controlled code, so values vary "
                  "in wording and must not be counted as categories.",
                  "text", "one accused entry on one FIR"),
    FieldContract("/arrested_date", "arrest date",
                  "the date this accused is recorded as arrested, where "
                  "recorded", "date", "one accused entry on one FIR"),
)


SECTION_FIELDS: tuple[FieldContract, ...] = (
    FieldContract("/section_code", "statute section code",
                  "the section code applied, as recorded", "text",
                  "one section applied to one FIR"),
    FieldContract("/act", "statute act",
                  "the act the section belongs to, as recorded", "text",
                  "one section applied to one FIR"),
    FieldContract("/action_date", "section action date",
                  "the date this section action was recorded", "date",
                  "one section applied to one FIR"),
)


ALL_FIELDS: tuple[FieldContract, ...] = (
    FIR_FIELDS + WEAPON_FIELDS + CRIMINAL_FIELDS + ROZNAMCHA_FIELDS
    + CMS_FIELDS + PKM_FIELDS + SECTION_FIELDS + ACCUSED_FIELDS)


def glossary() -> list[dict]:
    """What each contracted source field means, for a reader of the raw JSON.

    A source span is the canonical JSON of a record, so anything reading
    it sees `report_datetime` as a bare key with a timestamp beside it and
    no statement of what it means. That is how "When was FIR 1001/26
    registered?" was answered "registered on 2024-09-25": the meaning that
    says report_datetime is NOT a registration time lives on the concept,
    and the narrative path never sees concepts.

    These meanings are already written and already correct. This makes
    them available to whatever is reading the JSON.
    """
    seen: dict[str, dict] = {}
    for contract in ALL_FIELDS:
        field = contract.pointer.rsplit("/", 1)[-1]
        if field not in seen:
            seen[field] = {"field": field, "means": contract.label,
                           "definition": contract.meaning}
    return sorted(seen.values(), key=lambda entry: entry["field"])


# The export stringified Python None. MEASURED: criminal-records
# `external_record_ref` and several pkm fields hold the four characters
# "None" rather than a null. Treating that as a value would assert the
# literal text "None" as a fact about a record.
ABSENT_LITERALS = ("None", "null", "NULL", "N/A", "-")


def is_absent(value: Any) -> bool:
    """Whether a source field carries no value, including stringified nulls."""
    if value in (None, "", [], {}):
        return True
    return isinstance(value, str) and value.strip() in ABSENT_LITERALS


def pointer_value(record: dict, pointer: str) -> Any:
    """Resolve a JSON pointer, returning None rather than raising."""
    node: Any = record
    for part in pointer.strip("/").split("/"):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def canonical(record: dict) -> str:
    """The exact serialization every span for this record points into.

    Sorted keys and fixed separators so the same record always produces
    the same text, which is what makes document and span ids stable
    across re-imports.
    """
    return json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1)


def locate_value(text: str, pointer: str, value: Any) -> tuple[int, int] | None:
    """The character range in `text` carrying `value` for `pointer`.

    Finds the field's key first and takes the value that follows it, so a
    value that also appears elsewhere in the record cannot be cited from
    the wrong place.
    """
    key = pointer.rstrip("/").split("/")[-1]
    rendered = json.dumps(value, ensure_ascii=False)
    at = 0
    while True:
        k = text.find(f'"{key}"', at)
        if k < 0:
            return None
        v = text.find(rendered, k)
        if v >= 0 and v - k < 400:
            return v, v + len(rendered)
        at = k + 1


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_time(raw: Any) -> tuple[datetime | None, str]:
    """A source timestamp, with how it was interpreted stated alongside.

    Three outcomes, kept distinct because they mean different things:

      instant     an offset was supplied and is preserved as given.
      date_only   "2024-09-25" -- a calendar date with no time and no
                  offset. It is a real fact and is kept, but it is NOT an
                  instant. Recorded at midnight with that interpretation
                  written into the assertion, so a later question about
                  "that morning" cannot silently rest on an assumed hour.
      unusable    a naive date-time like "2024-09-25T17:25:00" with no
                  offset. Rejected. Assuming a timezone here moves an
                  event across a day boundary, which is exactly what a
                  monthly count would then get wrong.

    Measured: every `report_datetime` and `incident_datetime` in this
    snapshot carries `Z`; `action_date` values are date-only.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "absent"
    text = raw.strip()
    if _DATE_ONLY.match(text):
        try:
            midnight = datetime.fromisoformat(text)
        except ValueError:
            return None, "unusable"
        # A bare calendar date on a Pakistani police record is a LOCAL
        # date. Anchoring it to the configured zone is a decision, so it
        # is made once, here, and written into the assertion's
        # interpretation -- never left naive for a database session to
        # decide, which is how "2024-09-25" became 2024-09-24 19:00Z.
        return midnight.replace(tzinfo=ZoneInfo(config.EVIDENCE_TIMEZONE)), "date_only"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, "unusable"
    if parsed.tzinfo is None:
        return None, "unusable"
    return parsed, "instant"


@dataclass
class ImportReport:
    documents: int = 0
    concepts: int = 0
    entities: int = 0
    assertions: int = 0
    candidates: int = 0
    identity_links: int = 0
    coverage: int = 0
    skipped_absent: int = 0
    unparsed_time: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"documents": self.documents, "concepts": self.concepts,
                "entities": self.entities, "assertions": self.assertions,
                "candidates": self.candidates,
                "identity_links": self.identity_links,
                "coverage": self.coverage,
                "skipped_absent": self.skipped_absent,
                "unparsed_time": self.unparsed_time[:10],
                "notes": self.notes[:10]}


async def ensure_concept(conn, contract: FieldContract, doc_id: UUID) -> UUID:
    """One catalog concept per contracted field, created once."""
    concept_id = stable_id("concept", STRUCTURED_VERSION, contract.pointer,
                           contract.label)
    await conn.execute(
        """INSERT INTO evidence.concepts(concept_id,label,kind,meaning,aliases,
             value_type,unit,grain,version,origin_document_id,status)
           VALUES($1,$2,'property',$3,$4,$5,$6,$7,1,$8,'validated')
           ON CONFLICT(concept_id) DO NOTHING""",
        concept_id, contract.label, contract.meaning,
        list(contract.aliases), contract.value_type, contract.unit,
        contract.grain, doc_id)
    return concept_id


async def ensure_entity(conn, spec: EntityContract, record: dict,
                        doc_id: UUID, span_id: UUID) -> UUID | None:
    """An entity keyed on the SOURCE's identifier, never on its name.

    Identity from a stable key is a fact the source asserts. Identity
    from a matching name is a guess, and this system already carries 894
    person rows for 73 cases because that guess was made repeatedly.
    """
    key = pointer_value(record, spec.id_pointer)
    if key in (None, ""):
        return None
    name = pointer_value(record, spec.name_pointer) or str(key)
    concept_id = stable_id("concept", STRUCTURED_VERSION, "entity", spec.label)
    await conn.execute(
        """INSERT INTO evidence.concepts(concept_id,label,kind,meaning,aliases,
             value_type,unit,grain,version,origin_document_id,status)
           VALUES($1,$2,'entity_type',$3,'{}','entity',NULL,$4,1,$5,'validated')
           ON CONFLICT(concept_id) DO NOTHING""",
        concept_id, spec.label, spec.meaning, spec.grain, doc_id)
    # Document-scoped, because evidence.check_assertion_source() requires
    # an assertion's subject and object to belong to its own document.
    # That invariant is right: an entity row is an OBSERVATION of a thing
    # in one source record. Sameness across records is a separate claim,
    # recorded in evidence.identity_links where it can be reviewed.
    entity_id = stable_id("entity", spec.label, str(key), str(doc_id))
    await conn.execute(
        """INSERT INTO evidence.entities(entity_id,document_id,concept_id,label,
             span_id,identity_status,identifiers)
           VALUES($1,$2,$3,$4,$5,'confirmed',$6)
           ON CONFLICT(entity_id) DO NOTHING""",
        entity_id, doc_id, concept_id, str(name), span_id,
        {spec.identifier_name: str(key)})
    return entity_id


async def ensure_relation(conn, spec: RelationContract, doc_id: UUID) -> UUID:
    """A catalog concept for a relationship the source states."""
    concept_id = stable_id("concept", STRUCTURED_VERSION, "relation", spec.label)
    await conn.execute(
        """INSERT INTO evidence.concepts(concept_id,label,kind,meaning,aliases,
             value_type,unit,grain,version,origin_document_id,status)
           VALUES($1,$2,'relation',$3,$4,'entity',NULL,$5,1,$6,'validated')
           ON CONFLICT(concept_id) DO NOTHING""",
        concept_id, spec.label, spec.meaning, list(spec.aliases),
        spec.grain, doc_id)
    return concept_id


async def put_relation(conn, *, doc_id: UUID, subject_id: UUID,
                       predicate_id: UUID, object_id: UUID, span_id: UUID,
                       record_key: str, report: ImportReport) -> None:
    """One stated relationship between two identified things."""
    assertion_id = stable_id("assertion", STRUCTURED_VERSION, record_key,
                             str(predicate_id), str(object_id))
    await conn.execute(
        """INSERT INTO evidence.assertions(assertion_id,document_id,subject_id,
             predicate_id,object_entity_id,span_id,status,extraction_version,
             validation,source_record_key)
           VALUES($1,$2,$3,$4,$5,$6,'validated',$7,$8,$9)
           ON CONFLICT(assertion_id) DO NOTHING""",
        assertion_id, doc_id, subject_id, predicate_id, object_id, span_id,
        STRUCTURED_VERSION,
        {"source": "structured", "verified": "relationship stated by source"},
        record_key)
    report.assertions += 1


async def put_assertion(conn, *, doc_id: UUID, subject_id: UUID,
                        predicate_id: UUID, contract: FieldContract,
                        value: Any, span_id: UUID, record_key: str,
                        report: ImportReport) -> bool:
    """One validated fact, or nothing.

    A contracted date that cannot be parsed with its offset is NOT
    written as a validated assertion. It is reported instead, because a
    timestamp guessed into existence is worse than a missing one.
    """
    columns = {"value_text": None, "value_number": None,
               "value_boolean": None, "value_date": None}
    interpretation = "exact source field"
    if contract.value_type == "date":
        parsed, how = parse_time(value)
        if parsed is None:
            report.unparsed_time.append(f"{record_key}: {value!r} ({how})")
            return False
        # Written as the aware instant parse_time returned. Stripping the
        # offset here made the stored value depend on the DATABASE SESSION
        # timezone: with the session on Asia/Karachi, report_datetime
        # "2024-09-25T17:25:00Z" was read back as 12:25Z -- five hours
        # early, and disagreeing with the same event's timestamp from the
        # other ingestion path, which kept its offset.
        columns["value_date"] = parsed
        event_time = parsed
        interpretation = ("offset preserved as supplied" if how == "instant"
                          else "calendar date only; no time or timezone was "
                               "supplied and none was assumed")
    else:
        columns["value_text"] = str(value)
        event_time = None

    assertion_id = stable_id("assertion", STRUCTURED_VERSION, record_key,
                             str(predicate_id), str(value))
    await conn.execute(
        """INSERT INTO evidence.assertions(assertion_id,document_id,subject_id,
             predicate_id,value_text,value_number,value_boolean,value_date,unit,
             span_id,event_time,status,extraction_version,validation,
             source_record_key)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'validated',$12,$13,$14)
           ON CONFLICT(assertion_id) DO NOTHING""",
        assertion_id, doc_id, subject_id, predicate_id,
        columns["value_text"], columns["value_number"],
        columns["value_boolean"], columns["value_date"], contract.unit,
        span_id, event_time, STRUCTURED_VERSION,
        {"source": "structured", "pointer": contract.pointer,
         "verified": interpretation, "raw": str(value)},
        record_key)
    report.assertions += 1
    return True


async def ensure_person(conn, row: dict, doc_id: UUID, span_id: UUID,
                        report: ImportReport) -> UUID | None:
    """A person entity, keyed on CNIC, or nothing.

    A row without a CNIC does NOT become an entity here. Two people with
    the same name are two people until something says otherwise, and the
    894-row legacy person table is what happens when that rule is relaxed.
    The row is preserved as a candidate for review instead.
    """
    cnic = (row.get("cnic") or "").strip()
    name = (row.get("full_name") or "").strip()
    if not cnic:
        report.notes.append(f"person without CNIC kept as candidate: {name!r}")
        return None
    concept_id = stable_id("concept", STRUCTURED_VERSION, "entity",
                           PERSON_ENTITY.label)
    await conn.execute(
        """INSERT INTO evidence.concepts(concept_id,label,kind,meaning,aliases,
             value_type,unit,grain,version,origin_document_id,status)
           VALUES($1,$2,'entity_type',$3,'{}','entity',NULL,$4,1,$5,'validated')
           ON CONFLICT(concept_id) DO NOTHING""",
        concept_id, PERSON_ENTITY.label, PERSON_ENTITY.meaning,
        PERSON_ENTITY.grain, doc_id)
    entity_id = stable_id("entity", PERSON_ENTITY.label, cnic, str(doc_id))
    await conn.execute(
        """INSERT INTO evidence.entities(entity_id,document_id,concept_id,label,
             span_id,identity_status,identifiers)
           VALUES($1,$2,$3,$4,$5,'confirmed',$6)
           ON CONFLICT(entity_id) DO NOTHING""",
        entity_id, doc_id, concept_id, name or cnic, span_id,
        {"cnic": cnic})
    report.entities += 1
    return entity_id


async def link_identity(conn, *, entity_id: UUID, key: str, kind: str,
                        identifier_name: str, report: ImportReport) -> None:
    """Record that two document-local entities are the same thing.

    The claim is made ONLY from a stable source identifier -- a CNIC, a
    police_station_id -- never from a matching name. MEASURED: the
    extracted legacy store holds 894 person rows for 73 cases because
    names were matched; one name appears 46 times and no rule can say how
    many people that is.

    So `method` names the identifier that justifies the link, and a
    reviewer can see exactly what it rests on. A person with no CNIC gets
    no link at all and stays a separate, unresolved observation.
    """
    first = await conn.fetchval(
        """SELECT entity_id FROM evidence.entities
           WHERE identifiers->>$1 = $2 AND entity_id <> $3
           ORDER BY entity_id LIMIT 1""", identifier_name, key, entity_id)
    if not first:
        return
    left, right = sorted([first, entity_id], key=str)
    await conn.execute(
        """INSERT INTO evidence.identity_links(left_id,right_id,status,method,
             justification)
           VALUES($1,$2,'confirmed',$3,$4)
           ON CONFLICT(left_id,right_id) DO NOTHING""",
        left, right, f"source_identifier:{identifier_name}",
        {"identifier": identifier_name, "value": key, "kind": kind,
         "basis": "the source system records the same identifier on both"})
    report.identity_links += 1


async def mark_covered(conn, *, doc_id: UUID, concept_id: UUID,
                       content_hash: str, present: bool,
                       report: ImportReport) -> None:
    """Record that this document was completely checked for this concept.

    A structured import can make this claim where an extractor cannot: it
    reads a named field of a parsed record, so "the field is absent" is a
    finding, not a failure to look. `present` distinguishes the two and is
    kept in details, so a later question can tell "no value recorded" from
    "not yet examined".

    Coverage is keyed by content_hash and concept version, so a changed
    record or a changed concept definition invalidates it automatically
    rather than leaving a stale completeness certificate behind.
    """
    # The version coverage is recorded against is the concept's CURRENT
    # version, read here rather than assumed.
    #
    # MEASURED: this wrote a literal 1. Coverage is matched on
    # concept_version, so the moment a concept reached version 2 its
    # coverage could never match again and every question about it
    # answered "Documents checked: 0/73" -- a complete corpus reporting
    # itself unexamined. It stayed hidden while every concept sat at
    # version 1, and surfaced the first time one of them changed.
    await conn.execute(
        """INSERT INTO evidence.coverage(document_id,concept_id,concept_version,
             content_hash,status,extraction_version,details)
           SELECT $1,$2,k.version,$3,'complete',$4,$5
           FROM evidence.concepts k WHERE k.concept_id=$2
           ON CONFLICT(document_id,concept_id,concept_version,extraction_version)
           DO UPDATE SET status='complete', content_hash=EXCLUDED.content_hash,
                         details=EXCLUDED.details, recorded_at=now()""",
        doc_id, concept_id, content_hash, STRUCTURED_VERSION,
        {"method": "structured source field",
         "value_present": present,
         "basis": "the named field was read from the parsed source record"})
    report.coverage += 1


async def put_candidate(conn, *, doc_id: UUID, record_key: str,
                        source_table: str, payload: dict,
                        report: ImportReport) -> None:
    """A field whose meaning is not established. Preserved, not trusted."""
    candidate_id = stable_id("candidate", STRUCTURED_VERSION, record_key)
    await conn.execute(
        """INSERT INTO evidence.legacy_candidates(candidate_id,document_id,
             source_table,source_key,payload,status)
           VALUES($1,$2,$3,$4,$5,'candidate')
           ON CONFLICT(candidate_id) DO NOTHING""",
        candidate_id, doc_id, source_table, record_key, payload)
    report.candidates += 1
