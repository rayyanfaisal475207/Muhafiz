# -*- coding: utf-8 -*-
"""
[Gold-QA fix — Module 178] LLM-generated graph queries as a BOUNDED, VISIBLE
FALLBACK for a cross-case question that no aggregate answers and no plan
composes.

THE DEFECT. `xagg.py`'s aggregate kinds are enumerated, not general: a
question of a shape nobody wrote gets nothing. Module 145 (semantic
dispatch) and Module 158 (semantic sub-agent selection) fixed REACHING what
exists; Modules 144 and 79 let what exists COMPOSE; nothing answers what was
never written. Q2 — "is there anyone who used one of our citizen services
who also turns out to be under investigation for a crime?" — has a real,
one-row answer (Module 161: one CNIC joins the citizen-services silo to the
accused roster) and still routes XGRAPH -> Cross-Case Linkage -> "no
cross-case connections were found", three of three, because the aggregate
that computes it (`applicant_accused_overlap`) is reachable only from its
own description's wording (row 175).

WHY THIS IS A FALLBACK AND NOT THE PRIMARY PATH (tracker row 178). A fixed
query plus fixed arithmetic can be read by a supervisor after the fact; a
generated query differs every time and can run clean and return a
plausible wrong number. For a system whose figures go into case files that
is the failure mode to fear, so this layer is the LAST resort, its output
is marked unverified, and its query is shown verbatim.

THE FIVE BOUNDS — each is pinned by a test in tests/test_llm_query_fallback.py:

  1. POSITION — `applies()` is true only when keyword dispatch
     (`xagg.resolves_to_specific_aggregate`), Module 145's semantic dispatch
     (folded into the same predicate), sub-agent selection (the route's
     DEFAULT sub-agent was chosen, i.e. no trigger, no plan, no semantic
     selection) and Module 79's plan matching have ALL missed, AND the
     sub-agent that ran came back ABSTAINED/EMPTY with no infrastructure
     error. Only the XGRAPH/XNETWORK routes: XAGG's chain ends in a
     catch-all that always answers something, so "no aggregate answers" is
     never true there by construction (a wrong generic answer is rows
     173/174's defect, one layer up). 0 of the 32 gold questions satisfy
     the predicate.
  2. READ-ONLY, LEAST PRIVILEGE — the statement runs on the
     `muhafiz_mcp_readonly` role (`MCP_DATABASE_URL`, migrations 009 + 033),
     never the app role; it is rejected before the database if it contains
     any of CREATE/SET/DELETE/MERGE/REMOVE/DROP/CALL/DETACH/LOAD, and — the
     stronger guard — every identifier in it must be on an ALLOW-LIST
     (clause keywords, known labels, edge types, property keys, functions,
     or a variable the query itself bound). It runs inside a READ ONLY
     transaction that is ALWAYS rolled back, never committed. That last
     bound is not belt-and-braces: measured live on AGE 1.5.0 during this
     module, a SELECT-only role AND a READ ONLY transaction both let
     `SET`/`REMOVE`/`DETACH DELETE` through (only CREATE/MERGE are refused);
     the rollback is the one backstop that held. See MODULE178_RESULT.md §3
     and tracker row 183.
  3. SCOPED — the same supervisor-or-above role gate every aggregate goes
     through (`run_aggregate()`), applied BEFORE generation; and the same
     `jurisdiction_case_ids` allow-list, applied by INJECTING a
     `BELONGS_TO_CASE -> Case.case_id IN $case_ids` constraint onto every
     node variable the query binds — not by trusting the model to write a
     WHERE clause. An investigator is DENIED before any model call.
  4. BOUNDED — `LIMIT` clamped to `LLM_QUERY_FALLBACK_ROW_CAP` (200),
     `statement_timeout` = `LLM_QUERY_FALLBACK_TIMEOUT_S` (15 s), exactly
     one generation attempt: a query that fails to parse, validate or run
     is an abstention WITH the query and the error shown — never a retry,
     never a silent fallback to something else.
  5. VISIBLE — the served answer carries the generated query verbatim in
     `caveats` (rendered as italic lines under the answer and in the
     "what I checked" panel), status PARTIAL with `tools_used=["LLM_QUERY"]`
     ("generated graph query (unverified)"), and a `citation_validator`
     event whose detail says "unverified" — the exact signal
     MessageBubble.tsx already renders as a warning pill. The prose still
     goes through `verify_grounding()` against the returned rows; if it
     fails, the deterministic row rendering is served instead of the prose.

Feature flag: `config.LLM_QUERY_FALLBACK_ENABLED`, default OFF — see the
config comment and MODULE178_RESULT.md §6 for the measurement that decides.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import asyncpg

from src import config
from src.llm.client import call_llm
from src.pipeline.json_extract import extract_json

logger = logging.getLogger(__name__)

GRAPH_NAME = "evidence_graph"
SOURCE_TOOL = "LLM_QUERY"

# ═══════════════════════════════════════════════════════════════════════
# The schema the model is given. A condensed, live-measured extract of
# docs/graph_schema.md (labels, the properties that are actually populated,
# edge types with their real endpoints) — tests/test_llm_query_fallback.py
# pins every label and edge type here to that document. The AGE-dialect
# rules at the bottom are the ones this codebase has hit for real: Module
# 89 (INVOLVED_IN runs Person->Incident, not Person->Case) and xagg.py's
# `WHERE NOT (w)-[:R]->(:Label)` rejection.
# ═══════════════════════════════════════════════════════════════════════
KNOWN_LABELS: frozenset[str] = frozenset({
    "Case", "Person", "Officer", "Incident", "Document", "StructuredRecord",
    "Address", "Weapon", "Vehicle", "PhoneNumber", "Organization",
    "PoliceStation", "District", "Date",
})

KNOWN_EDGE_TYPES: frozenset[str] = frozenset({
    "BELONGS_TO_CASE", "APPEARS_IN", "ASSOCIATED_WITH", "SAME_AS", "OWNS",
    "REGISTERED_TO", "LOCATED_AT", "INVOLVED_IN", "PART_OF", "FILED_AT",
    "ASSIGNED_TO", "RELATED_TO", "CROSS_VERSION_OF", "OCCURRED_ON",
    "CONFLICTS_WITH", "CITES",
})

# Property keys measured on the live graph (Module 178 §2, schema probe).
# A generated query that names a property outside this set is rejected:
# an invented property compares as NULL everywhere, which is precisely
# the "runs clean, returns a plausible wrong number" failure this layer
# must not produce.
KNOWN_PROPERTIES: frozenset[str] = frozenset({
    # shared provenance
    "as_of", "source_doc_id", "source_chunk_id", "confidence",
    "extraction_confidence", "superseded_by", "entity_id", "canonical_name",
    "name_skeleton", "surface_text", "basis", "status", "tier", "role",
    "merged_into", "merged_at", "updated_at",
    # Case / Document / Incident
    "case_id", "doc_id", "doc_type", "filename", "description",
    "report_datetime", "incident_datetime", "station_departure_datetime",
    "reporting_delay_reason", "zimni_entry_count", "zimni_typed_count",
    # Person / Officer
    "cnic", "father_name", "address_text", "phone", "gender", "age",
    "belt_no", "designation",
    # Vehicle / Weapon / PhoneNumber / Address
    "plate", "make", "model", "chassis_no", "engine_no",
    "verification_result", "license_status", "caliber_or_bore", "condition",
    "number", "text", "normalized_text",
    # PoliceStation / District / Date
    "station_id", "name", "code", "district_id", "province", "date",
    # StructuredRecord
    "record_id", "record_type", "applicant_cnic", "complainant_cnic",
    "service_type", "submitted_at", "subject_cnic", "subject_full_name",
    "conviction_status", "act", "section_code", "action", "action_date",
    "status_date", "sr_no", "officer_name", "fir_display_code", "quantity",
    "date_entered", "item_detail", "position", "accused_names",
    "witness_names", "property_involved", "remarks", "reg_no",
    "offense_summary", "source_case_ref", "court_order_detail", "language",
    "police_station_name", "police_station_code", "case_tag_number",
    "method", "forensic_sample_note", "sections_and_accused_chalaaned",
    "zimni_index_no", "custody_classification", "amendment_datetime",
    "case_outcome", "zimni_report_date", "dispatch_datetime",
    "challan_reached_court_date",
    # edge properties
    "arrest_status", "assigned_from", "assigned_to", "event_type", "detail",
    "locked", "locked_by", "locked_at", "char_span",
})

# Which properties exist on WHICH label (same probe). `validate_query()`
# checks `var.prop` against the label `var` is bound to, so
# `incident.case_id` — the exact miss the first live Q2 run produced
# (Incident has no case_id; the column came back null and the narration
# read the null as "nobody found") — is rejected before it can run.
_COMMON = {"as_of", "source_doc_id", "confidence", "entity_id", "canonical_name",
           "extraction_confidence", "source_chunk_id"}
PROPERTIES_BY_LABEL: dict[str, frozenset[str]] = {
    "Case": frozenset(_COMMON | {"case_id"}),
    "Person": frozenset(_COMMON | {"cnic", "father_name", "address_text", "phone", "gender", "age",
                                   "name_skeleton", "merged_into", "merged_at"}),
    "Officer": frozenset(_COMMON | {"belt_no", "designation", "phone", "name_skeleton"}),
    "Incident": frozenset(_COMMON | {"description", "report_datetime", "incident_datetime",
                                     "station_departure_datetime", "reporting_delay_reason",
                                     "zimni_entry_count", "zimni_typed_count"}),
    "Document": frozenset(_COMMON | {"doc_id", "doc_type", "filename"}),
    "StructuredRecord": frozenset(_COMMON | {
        "record_id", "record_type", "updated_at", "applicant_cnic", "complainant_cnic",
        "service_type", "submitted_at", "status", "subject_cnic", "subject_full_name",
        "conviction_status", "act", "section_code", "action", "action_date", "status_date",
        "sr_no", "officer_name", "fir_display_code", "quantity", "date_entered", "item_detail",
        "position", "accused_names", "witness_names", "property_involved", "remarks", "reg_no",
        "offense_summary", "source_case_ref", "court_order_detail", "language",
        "police_station_name", "police_station_code", "case_tag_number", "method",
        "forensic_sample_note", "sections_and_accused_chalaaned", "zimni_index_no",
        "custody_classification", "amendment_datetime", "case_outcome", "zimni_report_date",
        "dispatch_datetime", "challan_reached_court_date", "condition"}),
    "Address": frozenset(_COMMON | {"text", "normalized_text"}),
    "Weapon": frozenset(_COMMON | {"license_status", "caliber_or_bore", "condition"}),
    "Vehicle": frozenset(_COMMON | {"plate", "make", "model", "chassis_no", "engine_no",
                                    "verification_result"}),
    "PhoneNumber": frozenset(_COMMON | {"number", "phone"}),
    "Organization": frozenset(_COMMON | {"name", "description"}),
    "PoliceStation": frozenset(_COMMON | {"station_id", "name", "code"}),
    "District": frozenset(_COMMON | {"district_id", "name", "province"}),
    "Date": frozenset(_COMMON | {"date"}),
}
EDGE_PROPERTIES: frozenset[str] = frozenset({
    "as_of", "source_doc_id", "source_chunk_id", "confidence", "superseded_by", "surface_text",
    "role", "arrest_status", "assigned_from", "assigned_to", "basis", "status", "tier",
    "event_type", "detail", "locked", "locked_by", "locked_at", "char_span",
})

# Every (edge type, from label, to label) triple that exists in the live
# graph (Module 178 §2 census, 2026-09-15). A pattern whose direction or
# endpoints are not in this table matches nothing — it does not error, it
# "runs clean" and returns empty or null rows, which is the failure this
# layer must not serve. The first live Q2 run wrote
# `(sr:StructuredRecord)-[:APPEARS_IN]->(p:Person)` (the real edge runs
# Person -> StructuredRecord) and served "one record, no details".
KNOWN_ENDPOINTS: frozenset[tuple[str, str, str]] = frozenset({
    ("APPEARS_IN", "Address", "Document"), ("APPEARS_IN", "StructuredRecord", "Document"),
    ("APPEARS_IN", "Person", "Document"), ("APPEARS_IN", "Officer", "Document"),
    ("APPEARS_IN", "Person", "StructuredRecord"), ("APPEARS_IN", "Weapon", "Document"),
    ("APPEARS_IN", "Vehicle", "Document"), ("APPEARS_IN", "PhoneNumber", "Document"),
    ("ASSIGNED_TO", "Officer", "Case"), ("ASSOCIATED_WITH", "Person", "Person"),
    ("BELONGS_TO_CASE", "Address", "Case"), ("BELONGS_TO_CASE", "StructuredRecord", "Case"),
    ("BELONGS_TO_CASE", "Person", "Case"), ("BELONGS_TO_CASE", "Officer", "Case"),
    ("BELONGS_TO_CASE", "Document", "Case"), ("BELONGS_TO_CASE", "Incident", "Case"),
    ("BELONGS_TO_CASE", "Weapon", "Case"), ("BELONGS_TO_CASE", "PhoneNumber", "Case"),
    ("BELONGS_TO_CASE", "Vehicle", "Case"), ("BELONGS_TO_CASE", "Organization", "Case"),
    ("CITES", "Case", "Case"), ("CROSS_VERSION_OF", "Case", "Case"),
    ("FILED_AT", "Case", "PoliceStation"), ("INVOLVED_IN", "Person", "Incident"),
    ("LOCATED_AT", "Person", "Address"), ("LOCATED_AT", "Person", "PoliceStation"),
    ("LOCATED_AT", "Person", "District"),
    ("OCCURRED_ON", "Incident", "Date"), ("OCCURRED_ON", "Person", "Date"), ("OCCURRED_ON", "Officer", "Date"),
    ("OWNS", "Person", "Weapon"), ("OWNS", "Person", "Vehicle"), ("OWNS", "Person", "PhoneNumber"),
    ("PART_OF", "Incident", "Case"), ("PART_OF", "PoliceStation", "District"),
    ("REGISTERED_TO", "Vehicle", "Person"), ("RELATED_TO", "Person", "Person"),
    ("SAME_AS", "Address", "Address"), ("SAME_AS", "Person", "Person"), ("SAME_AS", "Officer", "Officer"),
    ("SAME_AS", "Vehicle", "Vehicle"), ("SAME_AS", "PhoneNumber", "PhoneNumber"),
    ("CONFLICTS_WITH", "Document", "Document"), ("CONFLICTS_WITH", "Incident", "Incident"),
    ("CONFLICTS_WITH", "Document", "Incident"), ("CONFLICTS_WITH", "Incident", "Document"),
})

# Enumerated property values (same census). A literal compared to one of
# these keys that is not a real value is an invented filter — again a
# query that runs clean and returns nothing. `arrest_status` and the
# like are free Urdu text and are not checked.
KNOWN_VALUES: dict[str, frozenset[str]] = {
    "role": frozenset({"accused", "complainant", "victim", "witness", "applicant_pkm", "complainant_cms",
                       "chalaan_accused", "chalaan_witness", "investigating", "recording"}),
    "record_type": frozenset({"fir_section", "fir_zimni_index", "malkhana_register", "chalaan_dispatch",
                              "chalaan_outcome", "fir_position", "pkm_application", "cms_complaint",
                              "criminal_record"}),
    "event_type": frozenset({"incident", "arrest", "zimni_entry", "position", "chalaan_dispatch"}),
}

# Labels whose nodes carry a BELONGS_TO_CASE edge (measured: every label
# below except Vehicle/Organization has them today; those two are scoped
# the same way and simply match nothing under a scoped caller — the
# conservative direction). Case is scoped on its own case_id. Date,
# District and PoliceStation are reference nodes with no case of their own
# and are left unscoped: a bare date string or a station name is not a
# case's row.
CASE_CARRYING_LABELS: frozenset[str] = frozenset({
    "Person", "Officer", "Incident", "Document", "StructuredRecord",
    "Address", "Weapon", "Vehicle", "PhoneNumber", "Organization",
})
REFERENCE_LABELS: frozenset[str] = frozenset({"Date", "District", "PoliceStation"})

FORBIDDEN_KEYWORDS: frozenset[str] = frozenset({
    "CREATE", "SET", "DELETE", "MERGE", "REMOVE", "DROP", "CALL", "DETACH",
    "LOAD", "FOREACH", "ALTER", "GRANT", "INSERT", "UPDATE", "TRUNCATE",
    "COPY", "EXECUTE",
})

ALLOWED_KEYWORDS: frozenset[str] = frozenset({
    "MATCH", "OPTIONAL", "WHERE", "WITH", "RETURN", "ORDER", "BY", "ASC",
    "DESC", "LIMIT", "SKIP", "UNWIND", "AS", "DISTINCT", "AND", "OR", "NOT",
    "XOR", "IN", "IS", "NULL", "TRUE", "FALSE", "STARTS", "ENDS", "CONTAINS",
    "CASE", "WHEN", "THEN", "ELSE", "END",
})

ALLOWED_FUNCTIONS: frozenset[str] = frozenset({
    "count", "collect", "sum", "avg", "min", "max", "size", "tolower",
    "toupper", "tostring", "tointeger", "tofloat", "coalesce", "head",
    "last", "labels", "type", "id", "startnode", "endnode", "length", "trim",
    "substring", "split", "left", "right", "replace", "abs", "round", "keys",
    "exists", "nodes", "relationships", "ceil", "floor", "properties",
})

SCHEMA_SUMMARY = """Graph: Apache AGE graph `evidence_graph` (Cypher via AGE 1.5). One graph for a police
caseload: a Case is one FIR. Labels, with the properties that are actually populated:

Case {case_id (e.g. "fir-620-26"), as_of, source_doc_id}
Person {canonical_name (Urdu name), cnic (e.g. "00000-1000055-1", present on ~45%), father_name,
        address_text, phone, gender, age, entity_id, name_skeleton, merged_into (set on duplicate nodes
        merged into another Person — exclude `WHERE p.merged_into IS NULL` when counting people)}
Officer {canonical_name, belt_no, designation, phone, entity_id}
Incident {entity_id ("INCIDENT-FIR-<fir>"), description, report_datetime, incident_datetime,
          reporting_delay_reason, zimni_entry_count} — one per Case (the FIR's event). An Incident has
          NO case_id: to name its case, add (i)-[:PART_OF]->(c:Case) and return c.case_id.
Document {doc_id, doc_type, filename}
StructuredRecord {record_id, record_type, ...} — typed rows. record_type values and their extra keys:
    "fir_section" {act, section_code, action, action_date}
    "fir_zimni_index" {zimni_report_date, officer_name, zimni_index_no}
    "malkhana_register" {item_detail, quantity, condition, date_entered, sr_no}
    "chalaan_dispatch" {accused_names, witness_names, dispatch_datetime, challan_reached_court_date}
    "chalaan_outcome" {case_outcome, court_order_detail}
    "criminal_record" {subject_full_name, subject_cnic, conviction_status (English text: "Under trial",
                       "Convicted, on bail pending appeal", ...), offense_summary, source_case_ref}
    "fir_position" {position, status_date}
    "pkm_application" {applicant_cnic, service_type, submitted_at, status}   — citizen service (Khidmat Markaz)
    "cms_complaint" {complainant_cnic, police_station_name, case_tag_number} — citizen walk-in complaint
Weapon {canonical_name, license_status, caliber_or_bore}
Vehicle {plate, make, model}   PhoneNumber {number}   Address {text, normalized_text, canonical_name}
PoliceStation {station_id, name, code}   District {district_id, name, province}   Date {date "YYYY-MM-DD"}

Edges (direction matters — these are the ONLY directions that exist):
(Person|Officer|Incident|Document|StructuredRecord|Address|Weapon|PhoneNumber)-[:BELONGS_TO_CASE]->(Case)
(Person)-[:INVOLVED_IN {role: "accused"|"complainant"|"witness"|"applicant_pkm"|"complainant_cms",
                        arrest_status}]->(Incident)      — NOT to Case; reach the case via
(Incident)-[:PART_OF]->(Case)
(Person|Officer|Address|Weapon|Vehicle|PhoneNumber|StructuredRecord)-[:APPEARS_IN]->(Document)
(Person)-[:APPEARS_IN {role: "chalaan_accused"|"chalaan_witness"}]->(StructuredRecord)
(Officer)-[:ASSIGNED_TO {role: "investigating"|"recording", superseded_by}]->(Case)
(Case)-[:FILED_AT]->(PoliceStation)     (PoliceStation)-[:PART_OF]->(District)
(Person)-[:ASSOCIATED_WITH {basis}]->(Person)     (Person)-[:RELATED_TO {role}]->(Person)
(Person|Officer|Address)-[:SAME_AS {tier, status}]->(same label)   — identity candidates, not facts
(Person)-[:OWNS]->(Weapon)     (Vehicle)-[:REGISTERED_TO]->(Person)
(Person)-[:LOCATED_AT]->(Address|PoliceStation)
(Incident|Person|Officer)-[:OCCURRED_ON {event_type: "incident"|"arrest"|"zimni_entry"|"position"|...}]->(Date)
(Case)-[:CITES {status}]->(Case)

Facts about the data: names are Urdu script and single given names repeat across unrelated people —
join people on `cnic`, never on a name. "Under investigation" / "accused in a case" =
(Person)-[:INVOLVED_IN {role:"accused"}]->(Incident)-[:PART_OF]->(Case), returning c.case_id. "Convicted" lives on StructuredRecord
record_type "criminal_record" (conviction_status CONTAINS 'Convicted'; join subject_cnic to Person.cnic);
"fugitive"/"absconding" is the INVOLVED_IN edge's arrest_status, Urdu free text (e.g. contains 'مفرور'). Citizen services are StructuredRecord record_type
"pkm_application" (applicant_cnic) and "cms_complaint" (complainant_cnic); most of those records have
no Person node of their own, so join their CNIC property to Person.cnic.
"""

AGE_RULES = """Apache AGE Cypher rules (violations are rejected or error out — there is no retry):
- ONE statement, starting with MATCH. No semicolons, no comments, no parameters ($x), no backticks.
- Read-only: never CREATE, SET, DELETE, MERGE, REMOVE, DROP, CALL, DETACH, LOAD, FOREACH.
- Every node in a pattern must carry a label: (p:Person), never (p) or () — a variable bound earlier
  may be reused without a label.
- Use only the labels, edge types and property names listed above. Do not invent properties.
- No `WHERE NOT (a)-[:R]->(b)` pattern predicates and no EXISTS { } subqueries — AGE rejects them.
  To express "has no X", count with OPTIONAL MATCH and test the collected value, or subtract counts.
- No `RETURN *`, no map projections, no list comprehensions, no CASE inside aggregations.
- Give EVERY item in RETURN an alias with AS (e.g. `RETURN p.canonical_name AS name`).
- Return properties, not whole nodes; include the case_id where a case is involved. `case_id` exists
  ONLY on Case: from an Incident reach it with (i:Incident)-[:PART_OF]->(c:Case) and return c.case_id;
  from a Person/record with (x)-[:BELONGS_TO_CASE]->(c:Case). Every property you read must be listed
  under that node's label above.
- Prefer count(DISTINCT x). Keep it small: LIMIT 50 unless the question needs every row.
Answer with ONLY a JSON object: {"cypher": "<the statement>", "reasoning": "<one sentence>"}"""

GENERATION_SYSTEM_PROMPT = (
    "You write ONE read-only Cypher query against a police evidence graph, to answer an "
    "investigator's cross-case question. You do not answer the question yourself; you write the "
    "query whose rows answer it.\n\n" + SCHEMA_SUMMARY + "\n" + AGE_RULES
)

NARRATION_SYSTEM_PROMPT = """You answer a police investigator's cross-case question from the rows a graph query returned.
The rows are given as [Document 1]. Rules:
- Use ONLY the rows. Every number, name, CNIC and case id you write must appear in the rows verbatim,
  and every sentence that states a fact ends with the citation [Document 1].
- A row IS a match: if there is one row, one person/record was found — say so and name it. Never turn
  a row into "nobody was found" because a column is null; say that column was not returned.
- If there are no rows, say plainly that the query found no matching records — do not speculate.
- Answer in the language of the question (Urdu question -> Urdu answer; English -> English).
- Be brief: state the finding, then the supporting rows (name, CNIC, case id) as a short list.
- Do not mention the query, the database, or that you are a model."""


# ═══════════════════════════════════════════════════════════════════════
# Validation — allow-list tokenizer
# ═══════════════════════════════════════════════════════════════════════
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


class QueryRejected(ValueError):
    """The generated text failed validation; `.reason` is user-facing.
    `.model` is set when the rejection happened after a model answered."""

    def __init__(self, reason: str, model: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.model = model


@dataclass
class ValidatedQuery:
    cypher: str
    node_vars: dict[str, str] = field(default_factory=dict)   # var -> label
    columns: list[str] = field(default_factory=list)


def _blank_strings(text: str) -> str:
    """Replace the CONTENT of every string literal with spaces (same length,
    quotes kept) so offsets are preserved and identifier scanning never
    enters a literal."""
    return _STRING.sub(lambda m: "'" + " " * (m.end() - m.start() - 2) + "'", text)


def validate_query(cypher: str) -> ValidatedQuery:
    """
    Reject anything that is not a plain read over the known schema.

    Order: cheap structural refusals first (empty, too long, forbidden
    characters, forbidden keywords), then the allow-list pass over every
    identifier outside string literals, classified by its context:
    a label/edge type after ':' in a pattern, a property after '.', a map
    key before ':' inside '{}', a function before '(', a clause keyword, or
    a variable the query bound in a pattern / with AS. Anything else is an
    unknown identifier and the query is rejected — a blacklist can only
    name the writes we thought of; an allow-list names the reads we allow.
    """
    text = (cypher or "").strip().rstrip(";").strip()
    if not text:
        raise QueryRejected("the model returned no query")
    if len(text) > 2000:
        raise QueryRejected("the generated query is longer than 2000 characters")
    for bad, why in ((";", "a semicolon"), ("$", "a parameter or dollar-quote"),
                     ("`", "a backtick"), ("//", "a comment"), ("/*", "a comment")):
        if bad in text:
            raise QueryRejected(f"the generated query contains {why}")

    blanked = _blank_strings(text)
    upper_tokens = {m.group(0).upper() for m in _IDENT.finditer(blanked)}
    forbidden = sorted(upper_tokens & FORBIDDEN_KEYWORDS)
    if forbidden:
        raise QueryRejected(f"the generated query contains a write keyword: {', '.join(forbidden)}")
    if not re.match(r"(?i)^\s*(OPTIONAL\s+)?MATCH\b", blanked):
        raise QueryRejected("the generated query does not start with MATCH")
    if len(re.findall(r"(?i)\bRETURN\b", blanked)) != 1:
        raise QueryRejected("the generated query must contain exactly one RETURN")

    node_vars: dict[str, str] = {}
    edge_vars: set[str] = set()
    bound: set[str] = set()
    # Pass 1: pattern bindings and labels.
    for m in re.finditer(r"\(\s*([A-Za-z_][A-Za-z0-9_]*)?\s*(?::\s*([A-Za-z_][A-Za-z0-9_]*))?", blanked):
        var, label = m.group(1), m.group(2)
        # A '(' that is a function call or a grouping paren, not a node
        # pattern: `count(p)` / `(a AND b)` — those have a preceding
        # identifier or no ':' and are handled by the general pass below.
        prev = blanked[: m.start()].rstrip()
        prev_ident = _IDENT.findall(prev[-40:])
        if prev and (prev[-1].isalnum() or prev[-1] == "_") and (
            not prev_ident or prev_ident[-1].upper() not in ALLOWED_KEYWORDS
        ):
            continue  # function-call parenthesis, e.g. count(p)
        if label is None and var is not None and var.upper() in ALLOWED_KEYWORDS:
            continue  # `(NOT x)` style grouping
        if label is not None:
            if label not in KNOWN_LABELS:
                raise QueryRejected(f"unknown node label: {label}")
            if var is not None:
                if var in node_vars and node_vars[var] != label:
                    raise QueryRejected(f"variable {var} is bound to two labels")
                node_vars[var] = label
                bound.add(var)
        elif var is not None and var not in bound and var not in node_vars:
            # Unlabelled node variable: allowed only if bound earlier.
            if var.lower() not in ALLOWED_FUNCTIONS and var.upper() not in ALLOWED_KEYWORDS:
                raise QueryRejected(f"node variable {var} has no label")
    for m in re.finditer(r"\[\s*([A-Za-z_][A-Za-z0-9_]*)?\s*(?::\s*([A-Za-z_][A-Za-z0-9_|\s]*))?", blanked):
        var, types = m.group(1), m.group(2)
        if types:
            for t in re.split(r"\s*\|\s*", types.strip()):
                t = t.strip()
                if t and t not in KNOWN_EDGE_TYPES:
                    raise QueryRejected(f"unknown relationship type: {t}")
        if var:
            bound.add(var)
            edge_vars.add(var)
    for m in re.finditer(r"(?i)\bAS\s+([A-Za-z_][A-Za-z0-9_]*)", blanked):
        bound.add(m.group(1))

    # Pass 2: every identifier, classified by context.
    for m in _IDENT.finditer(blanked):
        tok = m.group(0)
        before = blanked[: m.start()].rstrip()
        after = blanked[m.end():].lstrip()
        if before.endswith("."):
            if tok not in KNOWN_PROPERTIES:
                raise QueryRejected(f"unknown property: {tok}")
            owner_m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", before[:-1])
            owner = owner_m.group(1) if owner_m else None
            if owner in node_vars and tok not in PROPERTIES_BY_LABEL[node_vars[owner]]:
                raise QueryRejected(f"{node_vars[owner]} has no property {tok} (asked as {owner}.{tok})")
            if owner in edge_vars and owner not in node_vars and tok not in EDGE_PROPERTIES:
                raise QueryRejected(f"relationships have no property {tok} (asked as {owner}.{tok})")
            continue
        if before.endswith(":"):
            continue  # label / edge type — checked in pass 1
        if after.startswith(":") and not after.startswith("::"):
            # `{role: 'accused'}` map key, or a pattern variable before its label
            if before.endswith("{") or before.endswith(","):
                if tok not in KNOWN_PROPERTIES:
                    raise QueryRejected(f"unknown property: {tok}")
            continue
        if tok.upper() in ALLOWED_KEYWORDS:
            continue
        if after.startswith("("):
            if tok.lower() not in ALLOWED_FUNCTIONS:
                raise QueryRejected(f"function not allowed: {tok}")
            continue
        if tok in bound:
            continue
        raise QueryRejected(f"unknown identifier: {tok}")

    _check_edge_endpoints(blanked, node_vars)
    _check_enumerated_values(text)
    return ValidatedQuery(cypher=text, node_vars=node_vars, columns=return_columns(text))


_NODE = r"\(\s*([A-Za-z_][A-Za-z0-9_]*)?\s*(?::\s*([A-Za-z_][A-Za-z0-9_]*))?[^()]*\)"
_EDGE = r"\[[^\]]*?:\s*([A-Za-z_][A-Za-z0-9_|\s]*?)(?:\s*\*[^\]]*)?(?:\s*\{[^}]*\})?\s*\]"
_PATTERN_STEP = re.compile(_NODE + r"\s*(<-|-)\s*" + _EDGE + r"\s*(->|-)\s*(?=" + _NODE + ")")


def _check_edge_endpoints(blanked: str, node_vars: dict[str, str]) -> None:
    """Every directed, typed hop `(a:L1)-[:T]->(b:L2)` must be a triple
    the graph actually holds (KNOWN_ENDPOINTS). Undirected hops and
    variable-length hops are checked in both directions / skipped."""
    pos = 0
    while True:
        m = _PATTERN_STEP.search(blanked, pos)
        if not m:
            return
        # The right-hand node is captured inside the lookahead (groups 6-7)
        # so a chain (a)-[:X]->(b)-[:Y]->(c) checks both hops.
        left_var, left_label, arrow_in, types, arrow_out, right_var, right_label = m.groups()
        pos = m.end()
        left = left_label or node_vars.get(left_var or "")
        rght = right_label or node_vars.get(right_var or "")
        if not left or not rght or "*" in m.group(0):
            continue
        for t in re.split(r"\s*\|\s*", types.strip()):
            t = t.strip()
            if not t:
                continue
            if arrow_in == "<-" and arrow_out == "-":
                src, dst, directed = rght, left, True
            elif arrow_out == "->" and arrow_in == "-":
                src, dst, directed = left, rght, True
            else:
                src, dst, directed = left, rght, False
            stated = (t, src, dst) in KNOWN_ENDPOINTS
            reverse = (t, dst, src) in KNOWN_ENDPOINTS
            if not (stated or (not directed and reverse)):
                raise QueryRejected(
                    f"no {t} edge runs {src} -> {dst}"
                    + (f" (it runs {dst} -> {src})" if reverse else "")
                )


_ENUM_LITERAL = re.compile(
    r"\b(role|record_type|event_type)\s*(?::|=|<>|!=)\s*'([^']*)'"
)
_ENUM_IN = re.compile(r"\.(role|record_type|event_type)\s+IN\s*\[([^\]]*)\]", re.IGNORECASE)


def _check_enumerated_values(text: str) -> None:
    # RELATED_TO.role is free Urdu text ("بھائی"); only Latin-script
    # literals are checked against the enumerations.
    def _bad(key: str, value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z_ ]+", value)) and value not in KNOWN_VALUES[key]

    for m in _ENUM_LITERAL.finditer(text):
        key, value = m.group(1), m.group(2)
        if _bad(key, value):
            raise QueryRejected(f"{key} has no value '{value}'")
    for m in _ENUM_IN.finditer(text):
        key = m.group(1)
        for lit in re.findall(r"'([^']*)'", m.group(2)):
            if _bad(key, lit):
                raise QueryRejected(f"{key} has no value '{lit}'")


# ═══════════════════════════════════════════════════════════════════════
# Rewrites: RETURN columns, row cap, case-scope injection
# ═══════════════════════════════════════════════════════════════════════
def _top_level_split(text: str, sep: str = ",") -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _return_tail(cypher: str) -> tuple[int, str, str]:
    """(index of RETURN, the projection text, the trailer ORDER/SKIP/LIMIT)."""
    blanked = _blank_strings(cypher)
    m = list(re.finditer(r"(?i)\bRETURN\b", blanked))[-1]
    tail = cypher[m.end():]
    trailer = re.search(r"(?i)\b(ORDER\s+BY|SKIP|LIMIT)\b", _blank_strings(tail))
    if trailer:
        return m.start(), tail[: trailer.start()], tail[trailer.start():]
    return m.start(), tail, ""


def return_columns(cypher: str) -> list[str]:
    """Column names for AGE's static `AS (c1 agtype, ...)` list: the AS
    alias of each top-level RETURN item, else a sanitised expression
    (`p.cnic` -> `p_cnic`), else `col<i>`; de-duplicated."""
    _, projection, _ = _return_tail(cypher)
    projection = re.sub(r"(?i)^\s*DISTINCT\b", "", projection)
    cols: list[str] = []
    for i, item in enumerate(_top_level_split(projection)):
        item = item.strip()
        alias = re.search(r"(?i)\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", item)
        if alias:
            name = alias.group(1)
        else:
            name = re.sub(r"[^A-Za-z0-9_]+", "_", item).strip("_") or f"col{i}"
        name = name[:60].lower()
        if name in cols or not re.match(r"^[a-z_]", name):
            name = f"col{i}"
        cols.append(name)
    return cols


def apply_row_cap(cypher: str, cap: int) -> str:
    """Clamp a trailing LIMIT to `cap`, or append one."""
    blanked = _blank_strings(cypher)
    m = re.search(r"(?i)\bLIMIT\s+(\d+)\s*$", blanked)
    if m:
        n = int(m.group(1))
        if n <= cap:
            return cypher
        return cypher[: m.start()] + f"LIMIT {cap}"
    return cypher.rstrip() + f" LIMIT {cap}"


def inject_case_scope(cypher: str, node_vars: dict[str, str]) -> str:
    """
    Bound 3. Rewrite the validated query so every node variable it binds is
    constrained to the caller's case allow-list, bound as `$case_ids`:

        (v:<case-carrying label>)  ->  MATCH (v)-[:BELONGS_TO_CASE]->(_sN:Case)
                                          WHERE _sN.case_id IN $case_ids
        (v:Case)                   ->  MATCH (v:Case) WHERE v.case_id IN $case_ids

    inserted immediately before the WITH / RETURN that closes the segment
    the variable was introduced in, so a variable is scoped where it is
    bound. Anonymous labelled nodes `(:Label)` are first given a variable
    so they are scoped too. Reference labels (Date, District,
    PoliceStation) are left alone. OPTIONAL MATCH is refused under scoping
    (a MATCH on a null-bound optional variable is not well defined), so
    the fail-closed outcome is an abstention rather than an unscoped row.
    """
    if re.search(r"(?i)\bOPTIONAL\s+MATCH\b", _blank_strings(cypher)):
        raise QueryRejected("OPTIONAL MATCH cannot be case-scoped for this caller")

    # Name anonymous labelled nodes.
    counter = [0]

    def _name(m: re.Match) -> str:
        counter[0] += 1
        return f"(_anon{counter[0]}:{m.group(1)}"

    cypher = re.sub(r"\(\s*:\s*([A-Za-z_][A-Za-z0-9_]*)", _name, cypher)
    node_vars = dict(node_vars)
    for m in re.finditer(r"\((_anon\d+):([A-Za-z_][A-Za-z0-9_]*)", cypher):
        node_vars[m.group(1)] = m.group(2)

    # Segment boundaries: top-level WITH / RETURN outside strings.
    blanked = _blank_strings(cypher)
    boundaries = [m for m in re.finditer(r"(?i)\b(WITH|RETURN)\b", blanked)]
    out, pos, seen, n = [], 0, set(), 0
    for b in boundaries:
        segment = cypher[pos:b.start()]
        seg_blank = blanked[pos:b.start()]
        clauses = []
        for m in re.finditer(r"\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_]*)", seg_blank):
            var, label = m.group(1), m.group(2)
            if var in seen:
                continue
            seen.add(var)
            if label in CASE_CARRYING_LABELS:
                n += 1
                clauses.append(
                    f" MATCH ({var})-[:BELONGS_TO_CASE]->(_s{n}:Case) WHERE _s{n}.case_id IN $case_ids"
                )
            elif label == "Case":
                clauses.append(f" MATCH ({var}:Case) WHERE {var}.case_id IN $case_ids")
        out.append(segment.rstrip() + "".join(clauses) + " ")
        pos = b.start()
    out.append(cypher[pos:])
    return "".join(out)


# ═══════════════════════════════════════════════════════════════════════
# Execution — read-only role, READ ONLY transaction, always rolled back
# ═══════════════════════════════════════════════════════════════════════
_pool: Optional[asyncpg.Pool] = None


def _readonly_dsn() -> str:
    raw = (config.MCP_DATABASE_URL or "").strip()
    return raw.replace("postgresql+asyncpg://", "postgresql://")


async def get_readonly_pool() -> asyncpg.Pool:
    """A pool on `MCP_DATABASE_URL` (the `muhafiz_mcp_readonly` role). No
    fallback to `DATABASE_URL`: an unconfigured role is an abstention, never
    a run on the app role."""
    global _pool
    if _pool is None:
        dsn = _readonly_dsn()
        if not dsn:
            raise RuntimeError(
                "MCP_DATABASE_URL is not configured — the generated-query fallback "
                "runs only on the least-privilege muhafiz_mcp_readonly role."
            )
        _pool = await asyncpg.create_pool(dsn, min_size=0, max_size=2, statement_cache_size=0)
    return _pool


async def close_readonly_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def execute_readonly(
    cypher: str,
    params: Optional[dict],
    columns: list[str],
    *,
    timeout_s: Optional[float] = None,
    graph: str = GRAPH_NAME,
) -> list[dict]:
    """
    Run one validated statement on the read-only role inside a READ ONLY
    transaction with a statement timeout, and ROLL BACK unconditionally.
    The rollback is the load-bearing bound (module docstring, bound 2).
    """
    from src.graph.age_client import _load_age, _parse_agtype  # same AGE lifecycle, no pool sharing

    if "$cypher$" in cypher:
        raise QueryRejected("the generated query contains a dollar-quote")
    col_list = ", ".join(f"{c} agtype" for c in columns)
    sql = f"SELECT * FROM cypher('{graph}', $cypher${cypher}$cypher$, $1::agtype) AS ({col_list})"
    timeout_ms = int((timeout_s if timeout_s is not None else config.LLM_QUERY_FALLBACK_TIMEOUT_S) * 1000)
    pool = await get_readonly_pool()
    async with pool.acquire() as conn:
        await _load_age(conn)
        tr = conn.transaction(readonly=True)
        await tr.start()
        try:
            await conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            rows = await conn.fetch(sql, json.dumps(params or {}))
        finally:
            await tr.rollback()
    return [{c: _parse_agtype(r[c]) for c in columns} for r in rows]


# ═══════════════════════════════════════════════════════════════════════
# Model calls — one attempt each, model recorded
# ═══════════════════════════════════════════════════════════════════════
class _ModelRecorder(logging.Handler):
    """Captures src.llm.client's own fallback log line so a caller can
    record WHICH model answered — call_llm() returns a bare string."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.fell_back = False
        self.detail = ""

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "Falling back to" in msg or "Failing over" in msg:
            self.fell_back = True
            self.detail = msg[:200]


async def _call_recorded(system_prompt: str, user_message: str, **kw: Any) -> tuple[str, str]:
    rec = _ModelRecorder()
    llm_logger = logging.getLogger("src.llm.client")
    llm_logger.addHandler(rec)
    try:
        text = await call_llm(system_prompt, user_message, **kw)
    finally:
        llm_logger.removeHandler(rec)
    if rec.fell_back or not config.LOCAL_LLM_URL:
        provider = config.LLM_PROVIDER
        model = config.GROQ_MODEL if provider == "groq" else config.GEMINI_MODEL
        return text, f"cloud:{provider}:{model}"
    return text, f"local:{config.LOCAL_LLM_MODEL}"


# Token budgets. Both the local Qwen3-14B and the Groq gpt-oss-120b
# fallback are reasoning models that spend their budget on a hidden trace
# before the JSON starts; measured live on Q3 (MODULE178_RESULT.md §4),
# 1,200 local / 600 cloud came back EMPTY from both, three of three. The
# floor src/llm/client.py settled on for this model is 2,000; a query over
# a two-record join needs more thinking than a router verdict.
GENERATION_MAX_TOKENS = 3000
GENERATION_CLOUD_MAX_TOKENS = 1500


async def generate_query(question: str) -> tuple[str, str, str]:
    """One generation attempt. Returns (cypher, reasoning, model)."""
    raw, model = await _call_recorded(
        GENERATION_SYSTEM_PROMPT, question, temperature=0.0,
        max_tokens=GENERATION_MAX_TOKENS, cloud_max_tokens=GENERATION_CLOUD_MAX_TOKENS,
    )
    try:
        obj = extract_json(raw)
    except Exception as exc:  # noqa: BLE001 — any parse failure is an abstention
        raise QueryRejected(f"the model did not return a query object ({exc})", model) from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("cypher"), str):
        raise QueryRejected("the model did not return a query object", model)
    return obj["cypher"].strip(), str(obj.get("reasoning") or "")[:300], model


def render_rows(rows: list[dict], *, max_rows: int = 50) -> str:
    """Deterministic, bounded rendering of the returned rows — what the
    narration is verified against and what is served if it fails."""
    if not rows:
        return "The generated query returned no rows."
    lines = [f"{len(rows)} row(s) returned" + (f"; first {max_rows} shown" if len(rows) > max_rows else "") + ":"]
    for r in rows[:max_rows]:
        lines.append(json.dumps(r, ensure_ascii=False, default=str))
    return "\n".join(lines)


_CASE_ID_RE = re.compile(r"\bfir-\d+-\d+\b")


def _case_ids_in(text: str) -> list[str]:
    return sorted(set(_CASE_ID_RE.findall(text)))


# ═══════════════════════════════════════════════════════════════════════
# The supervisor-facing predicate and the attempt
# ═══════════════════════════════════════════════════════════════════════
FALLBACK_ROUTES: frozenset[str] = frozenset({"XGRAPH", "XNETWORK"})


def dispatch_layers_missed(route_result: dict, query_text: str) -> bool:
    """
    True iff every dispatch layer above this one missed for `query_text`:
    keyword dispatch and Module 145's semantic dispatch (both inside
    `xagg.resolves_to_specific_aggregate`), the Meta-Analysis trigger list,
    and Module 79's decomposition plans. Pure apart from the semantic
    lookup, which reads a decision the router already prepared.
    """
    from src.pipeline import xagg
    from src.pipeline.harness import supervisor as sup
    from src.pipeline.harness.agents.meta_analysis import _match_decomposition_plan

    if xagg.resolves_to_specific_aggregate(query_text):
        return False
    if any(pat.search(query_text) for pat in sup._META_ANALYSIS_TRIGGER_PATTERNS):
        return False
    if _match_decomposition_plan(query_text) is not None:
        return False
    return True


def applies(
    route_result: dict,
    query_text: str,
    sub_agent_name: str,
    result: Any,
    *,
    allow_meta_analysis: bool = True,
    enabled: Optional[bool] = None,
) -> bool:
    """
    Bound 1 — the ONE statement of when the fallback may fire. See the
    module docstring. `result` is the SubAgentResult the selected sub-agent
    returned.
    """
    from src.pipeline.harness import supervisor as sup
    from src.pipeline.harness.types import SubAgentStatus

    if not (config.LLM_QUERY_FALLBACK_ENABLED if enabled is None else enabled):
        return False
    if not allow_meta_analysis or not query_text:
        return False  # never for a nested sub-query
    route = str(route_result.get("route") or "").upper()
    if route not in FALLBACK_ROUTES:
        return False
    if str(route_result.get("output_format") or "chat").lower() != "chat":
        return False
    if str(route_result.get("case_scope") or "within_case").lower() != "cross_case":
        return False
    # Sub-agent selection must have fallen through to the route's default —
    # a trigger, a plan, Global Search or (Module 158) a semantic selection
    # is a selection HIT, not a miss.
    if sub_agent_name != sup._ROUTE_TO_SUBAGENT.get(route):
        return False
    if sub_agent_name != sup.CROSS_CASE_LINKAGE:
        return False
    if result is None or result.status not in (SubAgentStatus.ABSTAINED, SubAgentStatus.EMPTY):
        return False
    if getattr(result, "error", None) is not None:
        return False  # an infrastructure failure or a timeout is not a dispatch miss
    return dispatch_layers_missed(route_result, query_text)


@dataclass
class FallbackOutcome:
    """Everything a live run or a test wants to know about one attempt."""
    fired: bool
    served: bool
    query: Optional[str] = None
    scoped_query: Optional[str] = None
    rows: Optional[list[dict]] = None
    model: Optional[str] = None
    narration_model: Optional[str] = None
    verifier: Optional[dict] = None
    reason: Optional[str] = None
    elapsed_s: float = 0.0


UNVERIFIED_CAVEAT = (
    "UNVERIFIED: this answer comes from a graph query the language model wrote for this "
    "question, not from an audited aggregate. Read the query and the rows before relying on any "
    "figure in it."
)


async def attempt(
    agent_input: Any,
    route_result: dict,
    prior_result: Any,
    *,
    emit: Optional[Callable[[Any], None]] = None,
    gateway: Any = None,
) -> tuple[Any, FallbackOutcome]:
    """
    Run the fallback for a question `applies()` accepted. Returns
    (SubAgentResult to serve, outcome). Every failure path returns an
    ABSTAINED result that SHOWS the generated query and the error — never a
    retry, never a silent fallback to the prior result's wording.
    """
    from src.pipeline.harness.types import (
        CROSS_CASE_ROLES, PipelineEvent, SubAgentResult, SubAgentStatus, ToolError,
    )
    from src.pipeline.verifier import verify_grounding

    started = time.monotonic()
    emit = emit or (lambda _e: None)
    question = agent_input.query_text
    caller = agent_input.execution.caller
    out = FallbackOutcome(fired=True, served=False)

    def _abstain(reason: str, *, query: Optional[str] = None) -> Any:
        out.reason = reason
        out.elapsed_s = round(time.monotonic() - started, 1)
        caveats = [f"Generated-query fallback could not answer this: {reason}"]
        if query:
            caveats.append(f"Generated graph query (not run or failed): {query}")
        emit(PipelineEvent(step="llm_query_fallback", status="error", detail=reason[:200],
                           query=query, model=out.model))
        logger.info("LLM-QUERY-FALLBACK abstained: %s | q=%s", reason[:160], question[:100])
        return SubAgentResult(status=SubAgentStatus.ABSTAINED, answer_text=None, caveats=caveats,
                             degraded_from=["XGRAPH", "XNETWORK"])

    # ── Bound 3a: the same role gate every aggregate goes through, BEFORE
    #    any model call and before any Cypher. An investigator is denied
    #    here even though Cross-Case Linkage would already have denied. ──
    if caller.role not in CROSS_CASE_ROLES:
        out.reason = "denied"
        out.elapsed_s = round(time.monotonic() - started, 1)
        emit(PipelineEvent(step="llm_query_fallback", status="skipped", detail="denied: cross-case role required"))
        if gateway is not None:
            try:
                await gateway.log_audit_event(
                    event_type="authorization_violation", user_id=caller.user_id, case_id=None,
                    details={"query": question, "role": caller.role.value, "route": "LLM_QUERY"},
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("LLM-QUERY-FALLBACK: audit write failed: %s", exc)
        return (
            SubAgentResult(
                status=SubAgentStatus.DENIED,
                error=ToolError(kind="permission_denied",
                                message="Cross-case generated queries require supervisor role or higher."),
                caveats=["Cross-case access was denied for your role."],
            ),
            out,
        )

    # ── Bound 3b: the same jurisdiction allow-list every aggregate takes,
    #    resolved the way orchestrator.py resolves it (role-gated). ──
    jurisdiction_case_ids: Optional[list[str]] = None
    station, district = route_result.get("station"), route_result.get("district")
    if station or district:
        try:
            from src.retrieval.graph_retriever import resolve_jurisdiction_case_ids
            jurisdiction_case_ids = await resolve_jurisdiction_case_ids(
                station=station, district=district, query_text=question,
                user_id=caller.user_id, user_role=caller.role.value,
            )
        except Exception as exc:  # noqa: BLE001
            return _abstain(f"jurisdiction could not be resolved ({type(exc).__name__})"), out

    emit(PipelineEvent(step="llm_query_fallback", status="active",
                       detail="Every aggregate and plan missed — asking the model for one read-only graph query"))

    # ── Generation: ONE attempt (bound 4). ──
    try:
        cypher, reasoning, model = await generate_query(question)
    except QueryRejected as exc:
        out.model = exc.model
        return _abstain(exc.reason), out
    except Exception as exc:  # noqa: BLE001
        return _abstain(f"query generation failed ({type(exc).__name__}: {str(exc)[:120]})"), out
    out.query, out.model = cypher, model

    # ── Bound 2: validate; bound 3: scope; bound 4: cap. ──
    try:
        validated = validate_query(cypher)
        scoped = validated.cypher
        params: dict = {}
        if jurisdiction_case_ids is not None:
            scoped = inject_case_scope(scoped, validated.node_vars)
            params = {"case_ids": list(jurisdiction_case_ids)}
        scoped = apply_row_cap(scoped, config.LLM_QUERY_FALLBACK_ROW_CAP)
    except QueryRejected as exc:
        return _abstain(f"rejected before the database — {exc.reason}", query=cypher), out
    out.scoped_query = scoped

    # ── Execute on the read-only role, READ ONLY, rolled back, timed. ──
    try:
        rows = await execute_readonly(scoped, params, validated.columns)
    except QueryRejected as exc:
        return _abstain(f"rejected before the database — {exc.reason}", query=cypher), out
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
        return _abstain(f"the query failed to run ({type(exc).__name__}: {msg})", query=scoped), out
    out.rows = rows
    logger.info("LLM-QUERY-FALLBACK ran: %d row(s) model=%s | %s", len(rows), model, scoped[:300])
    emit(PipelineEvent(step="llm_query_fallback", status="done",
                       detail=f"generated query returned {len(rows)} row(s)",
                       query=scoped, rows=len(rows), model=model, reasoning=reasoning))

    # ── Narrate, then verify against the rows (bound 5). ──
    rendered = render_rows(rows)
    chunks = [{"id": "llm-query-rows", "text": rendered,
               "metadata": {"case_id": "cross_case", "source_file": "generated graph query"}}]
    cross_case_ids = _case_ids_in(rendered)
    narration, verification = None, None
    try:
        narration, out.narration_model = await _call_recorded(
            NARRATION_SYSTEM_PROMPT,
            f"Question: {question}\n\n[Document 1]\n{rendered}",
            temperature=0.0, max_tokens=2000, cloud_max_tokens=800,
        )
        verification = await verify_grounding(
            answer=narration, cited_chunks=chunks, case_id="cross_case", cross_case_ids=cross_case_ids,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM-QUERY-FALLBACK narration/verification failed: %s", exc)
        verification = {"grounded": False, "off_topic": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
    out.verifier = verification
    grounded = bool(verification.get("grounded")) and not verification.get("off_topic")
    answer_text = narration.strip() if (grounded and narration and narration.strip()) else rendered

    query_caveat = f"Generated graph query (run as-is on a read-only role): {scoped}"
    caveats = [UNVERIFIED_CAVEAT, query_caveat]
    if not grounded:
        caveats.append(
            "The model's wording of these rows did not pass grounding verification, so the raw rows "
            "are shown instead" + (f" ({verification.get('reason')})" if verification.get("reason") else "") + "."
        )
    emit(PipelineEvent(step="citation_validator", status="done",
                       detail="unverified: figures come from a model-generated graph query, not an audited aggregate"))
    out.served, out.elapsed_s = True, round(time.monotonic() - started, 1)
    return (
        SubAgentResult(
            status=SubAgentStatus.PARTIAL,
            answer_text=answer_text,
            tools_used=[SOURCE_TOOL],
            degraded_from=["XGRAPH", "XNETWORK"],
            caveats=caveats,
        ),
        out,
    )
