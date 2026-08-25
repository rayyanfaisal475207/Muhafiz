# ============================================================
# Structured graph projection — M6a of the Muhafiz Data API migration
# (docs/decisions/0001-muhafiz-api-migration.md)
#
# A peer to src/ingestion/service.py's _run_graph_extraction(), not a
# replacement: _run_graph_extraction() runs LLM/regex extraction over
# free TEXT (still needed for M3's rendered narrative Documents — the
# genuine free-text fields). This module writes STRUCTURED fields
# directly to the graph as ground truth — no NER, no LLM guessing,
# because the API already supplies these as typed, identified data.
# Fixes the chunk-heuristic gap that made CNIC-auto-merge nearly
# unreachable for multi-person structured records
# (_attach_chunk_identifiers requires "exactly one CNIC and one person
# per chunk" — a structured FIR block with a complainant + 3 accused + 2
# witnesses never satisfies that).
#
# SCOPE: strictly single-FIR, deterministic writes only. Cross-silo
# linking (CMS/PKM/criminal-record joins) and confidence-scored prose
# parsing (FIR->FIR citations) are M6b's job, not this module's — kept
# separate because they carry a different risk profile (heuristic vs.
# ground truth) and therefore a different review discipline.
#
# WHAT THIS WRITES:
#   - Case, Document (one synthetic "#structured" Document per FIR,
#     representing the whole structured payload — there is no single file
#     these facts came from, unlike M3's per-field narrative Documents)
#   - Person nodes for complainant/accused/witness, via entity_resolution
#     with the REAL cnic in the mention dict (constraint: this is what
#     makes CNIC auto-merge actually fire) — see resolve_structured_person()
#     below for the corroboration gate applied when no CNIC is present.
#   - Weapon nodes (weapon_register), matched to an accused's Person node
#     via OWNS ONLY when recovered_from resolves to a named accused WITHIN
#     THE SAME FIR (measured: 30/31 resolvable in-FIR; matching globally
#     would be wrong given this dataset's name collisions).
#   - StructuredRecord nodes for typed rows with no identity of their own:
#     fir_section, malkhana_register, chalaan_dispatch, chalaan_outcome,
#     fir_zimni_index. Implements the label declared in
#     migrations/005_age_graph.sql and documented in docs/graph_schema.md
#     since the graph's original design, never written by any code path
#     until now.
#   - INVOLVED_IN (Person -> Incident, role=complainant|accused|witness),
#     PART_OF (Incident -> Case), LOCATED_AT (Person -> Address, from
#     address_text) — three of the five edge types declared in migration
#     005 with zero writers anywhere in the codebase before this module.
#     OWNS is the fourth (see Weapon above); REGISTERED_TO is the fifth,
#     written by M6b's cross_silo_projection.py instead (needs PKM's
#     vehicle_verification, a cross-silo join this module never sees).
#   - OCCURRED_ON (Incident -> Date), from TYPED timestamps
#     (incident_datetime, fir_zimni.entry_date, fir_accused.arrested_date,
#     chalaan_dispatch.dispatch_datetime) — deterministic, no LLM date
#     parsing, unlike entity_resolution.py's own domain_entities-fed path.
#   - PoliceStation/District nodes + Case-[FILED_AT]->PoliceStation-
#     [PART_OF]->District (Milestone B1, GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md
#     — jurisdiction graph nodes), from `FirRecord.police_station`'s nested
#     {id, name, code, district} object. Written unconditionally alongside
#     every other structural write below, so re-syncing every existing FIR
#     (M9's --full sync) backfills this relationship for every pre-existing
#     Case, not just newly-ingested ones — write_node()'s MERGE and the
#     purge-by-source-doc-id-prefix idempotency scripts/sync_muhafiz_data.py
#     already relies on (see its own module docstring) make this additive,
#     never destructive, on a second run.
#   - Officer nodes (Milestone B2, GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md —
#     officer identity resolution), resolved by belt_no through
#     entity_resolution.py's normal tiering (same exact-match discipline
#     CNIC/plate/phone already get), from BOTH investigating officers
#     (fir_investigating_officer child rows) and the single recording
#     officer (FirRecord.recording_officer_* fields) —
#     Officer-[ASSIGNED_TO {role, assigned_from, assigned_to}]->Case,
#     replacing the collapsed "current officer" string with full,
#     append-only assignment history (a supersession chain across
#     investigating-officer reassignments — see _write_investigating_
#     officers() below).
#   - RELATED_TO {role} edges between Person nodes (Milestone C1,
#     GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — person-relationship edges),
#     from fir_accused.relationship_to_victim/relationship_to_complainant.
#     `role` carries the raw relationship text as recorded (e.g. "اجنبی",
#     "بھائی") — same "structured field, no heuristic" tier as OWNS/OCCURRED_ON
#     above, written directly with confidence=1.0, no SAME_AS/pending step.
#     Direction is accused -> victim/complainant (the edge describes the
#     accused's relationship to the other party). victim_name has no
#     structured identity of its own on the schema (no CNIC, no separate
#     victim table) — a single bare-name Person is resolved for it via
#     the same resolve_structured_person() corroboration-gate discipline
#     every other no-CNIC structured mention in this module already goes
#     through (see _write_victim() below), rather than inventing a second,
#     looser path just because this is the one caller with nothing but a
#     name.
#   - chalaan_dispatch.accused_names/witness_names resolved back to this
#     FIR's own already-written Person nodes (Milestone C2,
#     GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — chalaan name resolution), via
#     APPEARS_IN{role: "chalaan_accused"|"chalaan_witness"} edges. Reuses
#     the EXACT SAME in-FIR-only name-matching pattern
#     weapon_register.recovered_from already uses (accused_by_name, built
#     during _write_accused()) — plus a parallel witness_by_name built
#     during _write_witnesses() — never a global name lookup across the
#     whole graph (see _write_chalaan_name_links() below).
#   - malkhana_register.item_detail classified at write time (Milestone
#     C5, GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — typed recovered
#     property): a value shaped like a vehicle plate or phone number
#     resolves into the existing Vehicle/PhoneNumber node type
#     (APPEARS_IN{role: "recovered"}) via entity_resolution.resolve_and_write(),
#     instead of a generic StructuredRecord; everything else (cash,
#     generic exhibits) stays a StructuredRecord, unchanged. Reuses
#     structured_fields.py's existing plate/phone shape detectors, not new
#     pattern matchers (see _classify_and_write_malkhana_item() below).
#   - fir_witness.police_station_of_residence_id/other_district resolved
#     to a LOCATED_AT-style edge to the witness's HOME PoliceStation/
#     District (Milestone C6, GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md —
#     witness home jurisdiction), distinct from the case's own filing
#     jurisdiction (FILED_AT, B1). Reuses B1's exact PoliceStation/
#     District identity keys (station_id/district_id) so this MERGEs onto
#     the same jurisdiction nodes B1 already writes (see
#     _write_witness_home_jurisdiction() below).
#   - fir_zimni.officer_name resolved to an Officer identity (Milestone C3,
#     GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — zimni officer and position
#     timeline), reusing B2's entity_resolution.resolve_and_write("officer", ...)
#     path directly (see _write_zimni_officers() below) — plus fir_position
#     rewritten from "latest row only" to a full dated OCCURRED_ON timeline
#     (one edge per row with a status_date, same idiom as every other
#     multi-dated-event source in this module).
#   - ASSOCIATED_WITH{basis, confidence} between every pair of Person nodes
#     (victim/complainant/accused/witnesses — never Officer) that
#     INVOLVED_IN the same Incident (findings.md Module 1 — this was the
#     ONLY edge type src/retrieval/graph_retriever.py's multi-hop
#     traversal ever follows between two different entities, and this
#     module never wrote it: live count was 0 across the whole real graph,
#     making every GRAPH/GRAPH_HYBRID/XGRAPH/XNETWORK query permanently
#     hop_count=0 in production). Deliberately labeled "co-mentioned",
#     confidence 0.5 — well under the 1.0 used for directly-stated
#     structured fields elsewhere in this module — since sharing an
#     Incident is a structural fact, not a stated relationship (see
#     _write_associated_with() below).
#
# Every write here is source_doc_id-tagged to the synthetic "#structured"
# Document, so provenance ("what does the system believe, from where") is
# never lost — same append-only discipline as _run_graph_extraction().
# ============================================================

from __future__ import annotations

import itertools
import logging
import re
from typing import Optional

from src import config
from src.data_gateway.muhafiz_api.models import FirRecord
from src.extraction import structured_fields
from src.graph import age_client, entity_resolution, ingestion_quality, versioning

logger = logging.getLogger(__name__)

_STRUCTURED_RECORD_TABLES = (
    "fir_section", "malkhana_register", "chalaan_dispatch",
    "chalaan_outcome", "fir_zimni_index",
    # Milestone C3 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — zimni officer and
    # position timeline): fir_position's non-dated fields (prosecutor_name,
    # cross_certificate_ref, pending_challan_objections, remarks) get the
    # same full-field StructuredRecord capture every other typed row with
    # no identity of its own already gets here — the dated TIMELINE itself
    # (the actual C3 requirement) is the separate OCCURRED_ON writes in
    # _write_occurred_on() below, for rows that carry a status_date.
    "fir_position",
)


def _doc_id_for(fir: FirRecord) -> str:
    """
    The synthetic Document id every structured write on this FIR is
    tagged to. Deliberately distinct from M3's per-field narrative
    Document ids (psrms/fir/{fir_id}#narrative, #zimni_5, ...) — there is
    no single free-text field these structured facts "came from"; they
    came from the API response as a whole.
    """
    return f"psrms/fir/{fir.fir_id}#structured"


def _station_identity(fir: FirRecord, station: dict) -> Optional[tuple[str, dict]]:
    """
    (match_key, properties) for `fir.police_station`, or None when this FIR
    carries no station data at all (measured: every FIR in the recorded
    snapshot has one, but the API's own docs don't guarantee it, and a
    Case with genuinely no station on file must not fabricate one).

    Keyed on `fir.police_station_id` (the FIR's own FK — the canonical
    identifier per `FirRecord`'s docstring) first, falling back to the
    nested object's own `id`, then to `name` as a last resort — mirrors
    `_incident_entity_id()`'s own "deterministic match key, not a random
    uuid" requirement: re-projecting the same FIR must MERGE onto the same
    PoliceStation node, not mint a new one, and two different FIRs at the
    same real station must land on ONE shared node, not one each.
    """
    station_id = fir.police_station_id or station.get("id")
    name = station.get("name")
    key = station_id or name
    if not key:
        return None
    return key, {"name": name, "code": station.get("code")}


def _district_identity(station: dict) -> Optional[tuple[str, dict]]:
    """
    (match_key, properties) for a station's nested `district` — a dict
    ({id, name, province}, per the live API shape) or a bare string
    (`muhafiz_records.py`'s `_station_district()` already tolerates both).
    None when the station carries no district at all.
    """
    district = station.get("district")
    if isinstance(district, dict):
        key = district.get("id") or district.get("name")
        if not key:
            return None
        return key, {"name": district.get("name"), "province": district.get("province")}
    if district:
        return district, {"name": district}
    return None


async def _write_jurisdiction(fir: FirRecord, case_id: str, doc_id: str, graph: str, stats: dict) -> None:
    """
    Case-[FILED_AT]->PoliceStation-[PART_OF]->District — see this module's
    docstring's B1 entry.

    Access control is deliberately NOT this function's concern: it only
    writes jurisdiction METADATA (which station/district a case belongs
    to). Station/district-SCOPED TRAVERSAL — "every case filed at this
    station" — is a broader enumeration capability that goes through
    `src/retrieval/graph_retriever.py`'s `retrieve_jurisdiction_cases()`,
    which reuses the exact same cross-case role gate `retrieve_graph()`
    already enforces (see that module's own comment) rather than this
    module inventing a second one.
    """
    station = fir.police_station or {}
    station_identity = _station_identity(fir, station)
    if not station_identity:
        return
    try:
        station_key, station_props = station_identity
        await versioning.write_node(
            "PoliceStation", {"station_id": station_key}, station_props,
            source_doc_id=doc_id, graph=graph,
        )
        await versioning.write_edge(
            "FILED_AT", "Case", {"case_id": case_id}, "PoliceStation", {"station_id": station_key},
            {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        stats["edges_written"] += 1

        district_identity = _district_identity(station)
        if district_identity:
            district_key, district_props = district_identity
            await versioning.write_node(
                "District", {"district_id": district_key}, district_props,
                source_doc_id=doc_id, graph=graph,
            )
            await versioning.write_edge(
                "PART_OF", "PoliceStation", {"station_id": station_key}, "District", {"district_id": district_key},
                {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
            )
            stats["edges_written"] += 1
    except Exception as exc:
        logger.warning("Jurisdiction write failed for %s: %s", fir.fir_id, exc)
        stats["errors"].append(f"jurisdiction: {exc}")


def _incident_entity_id(fir: FirRecord) -> str:
    """
    Deterministic (not uuid4-random, unlike the free-text extraction
    path's domain_entities-derived incidents) so re-projecting the same
    FIR MERGEs onto the same Incident node instead of minting a duplicate
    every run — write_node() MERGEs on its match dict, so this is what
    makes M6a idempotent on re-sync (M9).
    """
    return f"INCIDENT-FIR-{fir.fir_id}"


# ── the corroboration gate (structured-record name-fallback only) ──────

async def _has_corroboration(mention: dict, case_id: str, *, graph: str) -> bool:
    """
    Cheap pre-check specific to a structured-record person mention with NO
    cnic (measured live: 4/37 witnesses). Reuses entity_resolution's own
    candidate generation — shared_case/shared_structured_id are already
    computed there — rather than duplicating its scoring; this only asks
    "does the best candidate have ANY corroboration beyond name alone"
    before allowing the full resolve_and_write() call (and therefore a
    possible SAME_AS write) to run at all.

    A CNIC-present mention never calls this — entity_resolution.py's
    exact-match hard block already fully protects that case regardless of
    name similarity (see that module's own module docstring).
    """
    candidates = await entity_resolution._generate_candidates(
        "Person", mention, case_id, id_key="cnic", graph=graph,
    )
    if not candidates:
        return False
    best = candidates[0]
    if best.name_similarity < entity_resolution.REVIEW_FLOOR:
        return False
    if best.shared_case or best.shared_structured_id:
        return True
    return _matching_address(mention, best.node)


def _matching_address(mention: dict, node: dict) -> bool:
    m = (mention.get("address_text") or "").strip()
    c = (node.get("properties", {}) or {}).get("address_text", "")
    c = (c or "").strip()
    return bool(m) and bool(c) and m == c


async def _write_new_person(
    mention: dict, case_id: str, source_doc_id: str, source_chunk_id: Optional[str] = None,
    *, graph: str, gated: bool = False,
) -> dict:
    """
    Bypasses entity_resolution.resolve_and_write() entirely — used when
    the corroboration gate refuses to let a no-CNIC structured mention
    risk a name-fallback SAME_AS candidate at all. Mirrors
    resolve_and_write()'s own TIER_NEW write path exactly (new entity_id,
    Person node, BELONGS_TO_CASE, APPEARS_IN — no SAME_AS, since nothing
    corroborated a link to write).

    `gated` [Ingestion Quality Control at Scale, Module G1]: True only
    when THIS call is the corroboration gate's own refusal outcome (a
    real candidate existed and was declined) — see
    resolve_structured_person()'s own call sites for which branch passes
    which value. False for the other route into this function (name-
    fallback resolution administratively disabled via
    config.ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED) — that is not
    the gate intervening, so it must not be counted as a rejection.
    """
    if graph == entity_resolution._PRODUCTION_GRAPH:
        ingestion_quality.record_new_tier_from_gate(gated)
    entity_id = entity_resolution._new_entity_id("person")
    node_properties = {k: v for k, v in mention.items() if v is not None}
    await versioning.write_node(
        "Person", {"entity_id": entity_id}, node_properties,
        source_doc_id=source_doc_id, confidence=1.0, graph=graph,
    )
    await versioning.write_edge(
        "BELONGS_TO_CASE", "Person", {"entity_id": entity_id}, "Case", {"case_id": case_id},
        {}, source_doc_id=source_doc_id, source_chunk_id=source_chunk_id, confidence=1.0, graph=graph,
    )
    await versioning.write_edge(
        "APPEARS_IN", "Person", {"entity_id": entity_id}, "Document", {"doc_id": source_doc_id},
        {"surface_text": mention.get("canonical_name", "")},
        source_doc_id=source_doc_id, source_chunk_id=source_chunk_id,
        confidence=mention.get("extraction_confidence", 1.0), graph=graph,
    )
    return {
        "entity_id": entity_id, "tier": "new", "confidence": 1.0,
        "basis": "structured mention, no cnic, no corroborating match (gate)",
        "is_new_node": True, "candidates_considered": 0,
    }


async def resolve_structured_person(
    mention: dict, case_id: str, source_doc_id: str, source_chunk_id: Optional[str] = None,
    *, graph: str = age_client.GRAPH_NAME,
) -> dict:
    """
    The entry point every person write in this module goes through.

      - mention has a cnic -> straight to entity_resolution.resolve_and_write().
        The exact-match hard block there already fully protects this case;
        no gate needed.
      - mention has NO cnic, gate disabled
        (config.ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED=False) ->
        _write_new_person(), never risk a SAME_AS at all.
      - mention has NO cnic, gate enabled (default) -> _has_corroboration()
        decides; corroborated -> resolve_and_write() (its own normal
        scoring/tiering runs from there); uncorroborated -> _write_new_person().
    """
    if mention.get("cnic"):
        return await entity_resolution.resolve_and_write(
            "person", mention, case_id, source_doc_id, source_chunk_id, graph=graph,
        )
    if not config.ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED:
        # Not a gate refusal — name-fallback resolution is administratively
        # off, so there was never a candidate for the gate to weigh in on.
        return await _write_new_person(mention, case_id, source_doc_id, source_chunk_id, graph=graph, gated=False)
    if await _has_corroboration(mention, case_id, graph=graph):
        return await entity_resolution.resolve_and_write(
            "person", mention, case_id, source_doc_id, source_chunk_id, graph=graph,
        )
    # The gate actually ran and declined — Module G1's corroboration_gate_rejections count.
    return await _write_new_person(mention, case_id, source_doc_id, source_chunk_id, graph=graph, gated=True)


# ── person mention builders ──────────────────────────────────────────────

def _person_mention(raw: dict, *, name_key: str = "full_name") -> Optional[dict]:
    name = raw.get(name_key)
    if not name:
        return None
    mention = {"canonical_name": name, "extraction_confidence": 1.0}
    for src_key, dst_key in (
        ("cnic", "cnic"), ("father_name", "father_name"),
        ("address_text", "address_text"), ("phone", "phone"),
    ):
        val = raw.get(src_key)
        if val:
            mention[dst_key] = val
    return mention


def _victim_mention(fir: FirRecord) -> Optional[dict]:
    """
    `FirRecord.raw["victim_name"]` — a bare name, nothing else (no CNIC, no
    father_name/address on the schema for a victim; muhafiz_schema.dbml.txt
    flags the field itself as "NOT OBSERVED... added on direct instruction
    despite this. No injury or relationship field added alongside it").
    Measured live: 9/73 FIRs carry one.
    """
    name = fir.raw.get("victim_name")
    if not name:
        return None
    return {"canonical_name": name, "extraction_confidence": 1.0}


def _complainant_mention(fir: FirRecord) -> Optional[dict]:
    name = fir.raw.get("complainant_full_name")
    if not name:
        return None
    mention = {"canonical_name": name, "extraction_confidence": 1.0}
    if fir.complainant_cnic:
        mention["cnic"] = fir.complainant_cnic
    if fir.raw.get("complainant_father_name"):
        mention["father_name"] = fir.raw["complainant_father_name"]
    if fir.raw.get("complainant_address"):
        mention["address_text"] = fir.raw["complainant_address"]
    if fir.raw.get("complainant_phone"):
        mention["phone"] = fir.raw["complainant_phone"]
    return mention


# ── officers (Milestone B2) ──────────────────────────────────────────────

def _officer_mention(
    name: Optional[str], belt_no: Optional[str],
    designation: Optional[str] = None, phone: Optional[str] = None,
) -> Optional[dict]:
    if not name:
        return None
    mention = {"canonical_name": name, "extraction_confidence": 1.0}
    if belt_no:
        mention["belt_no"] = belt_no
    if designation:
        mention["designation"] = designation
    if phone:
        mention["phone"] = phone
    return mention


async def _write_investigating_officers(fir, case_id, doc_id, graph, stats) -> None:
    """
    fir_investigating_officer can have more than one row over a case's
    life (muhafiz_cases.py's own `_current_investigating_officer()`
    comment) — measured live: `fir-205-26` has two (belt 1854L from
    2026-02-15, superseded by belt GEN-0105 from 2026-06-22). Sorted by
    `assigned_from` (rows with none sort last — same "assigned_from is
    often null too" tolerance `muhafiz_cases.py` already documents) and
    written as a SUPERSESSION CHAIN: each later officer's edge supersedes
    the previous one via `versioning.write_edge()`'s existing mechanism.
    The prior edge is never deleted — only marked `superseded_by` — so the
    full history stays queryable (§7-B's ">1 row wherever the source data
    shows a reassignment" requirement) while the one edge with no
    `superseded_by` is unambiguously "who is investigating this case now."
    """
    rows = fir.child_rows("fir_investigating_officer")
    rows_sorted = sorted(rows, key=lambda r: (r.get("assigned_from") is None, r.get("assigned_from") or ""))

    previous_edge_id: Optional[int] = None
    for row in rows_sorted:
        mention = _officer_mention(row.get("officer_name"), row.get("belt_no"), row.get("designation"))
        if not mention:
            continue
        try:
            resolution = await entity_resolution.resolve_and_write(
                "officer", mention, case_id, doc_id, graph=graph,
            )
            stats["officers_resolved"] += 1
            edge = await versioning.write_edge(
                "ASSIGNED_TO", "Officer", {"entity_id": resolution["entity_id"]}, "Case", {"case_id": case_id},
                {
                    "role": "investigating",
                    "assigned_from": row.get("assigned_from"),
                    "assigned_to": row.get("assigned_to"),
                },
                source_doc_id=doc_id, confidence=1.0,
                supersedes_edge_id=previous_edge_id, graph=graph,
            )
            stats["edges_written"] += 1
            if edge:
                previous_edge_id = edge["id"]
        except Exception as exc:
            logger.warning(
                "Investigating officer write failed for %s in %s: %s", row.get("officer_name"), fir.fir_id, exc,
            )
            stats["errors"].append(f"investigating_officer[{row.get('id')}]: {exc}")


async def _write_recording_officer(fir, case_id, doc_id, graph, stats) -> None:
    """
    A single officer per FIR, no history rows in the source data (unlike
    investigating officers above) — one ASSIGNED_TO edge, nothing to
    supersede against. `assigned_from` is the FIR's own `report_datetime`
    (when the FIR was recorded), the closest available meaning to "when
    this assignment began" for a role the source data models as a single
    point-in-time fact rather than a dated series.
    """
    mention = _officer_mention(
        fir.raw.get("recording_officer_name"),
        fir.raw.get("recording_officer_belt_no"),
        fir.raw.get("recording_officer_designation"),
        fir.raw.get("recording_officer_phone"),
    )
    if not mention:
        return
    try:
        resolution = await entity_resolution.resolve_and_write(
            "officer", mention, case_id, doc_id, graph=graph,
        )
        stats["officers_resolved"] += 1
        await versioning.write_edge(
            "ASSIGNED_TO", "Officer", {"entity_id": resolution["entity_id"]}, "Case", {"case_id": case_id},
            {
                "role": "recording",
                "assigned_from": fir.raw.get("report_datetime"),
                "assigned_to": None,
            },
            source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        stats["edges_written"] += 1
    except Exception as exc:
        logger.warning("Recording officer write failed for %s: %s", fir.fir_id, exc)
        stats["errors"].append(f"recording_officer: {exc}")


async def _write_zimni_officers(fir, case_id, doc_id, graph, stats) -> None:
    """
    Milestone C3 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — zimni officer and
    position timeline): each fir_zimni entry's officer_name resolved to an
    Officer identity, reusing B2's entity_resolution.resolve_and_write("officer", ...)
    path DIRECTLY — the same call `_write_investigating_officers()`/
    `_write_recording_officer()` above already make, not a parallel
    officer-matching mechanism. fir_zimni rows carry no belt_no (only
    fir_investigating_officer/recording_officer_* do), so this always
    resolves through entity_resolution's ordinary name-fallback tiering —
    the same path any belt_no-less officer mention already takes.

    An Officer-[OCCURRED_ON{event_type: "zimni_entry", detail}]->Date edge
    is written alongside the Incident's own zimni OCCURRED_ON edge
    (_write_occurred_on() above, same date, same event_type) — the same
    idiom OCCURRED_ON already uses for "multiple dated events, this time
    from a different from_label" (Person's arrest date is the existing
    precedent), giving "which officer recorded which dated entry" without
    a new edge label.
    """
    for z in fir.child_rows("fir_zimni"):
        mention = _officer_mention(z.get("officer_name"), None)
        if not mention:
            continue
        try:
            resolution = await entity_resolution.resolve_and_write(
                "officer", mention, case_id, doc_id, graph=graph,
            )
            stats["officers_resolved"] += 1
            if z.get("entry_date"):
                await _write_occurred_on_edge(
                    "Officer", resolution["entity_id"], z["entry_date"],
                    _zimni_edge_properties(z),
                    doc_id, graph, stats,
                )
        except Exception as exc:
            logger.warning(
                "Zimni officer write failed for %s in %s: %s", z.get("officer_name"), fir.fir_id, exc,
            )
            stats["errors"].append(f"zimni_officer[{z.get('id')}]: {exc}")


async def _write_officers(fir, case_id, doc_id, graph, stats) -> None:
    await _write_investigating_officers(fir, case_id, doc_id, graph, stats)
    await _write_recording_officer(fir, case_id, doc_id, graph, stats)
    await _write_zimni_officers(fir, case_id, doc_id, graph, stats)


# ── main entry point ─────────────────────────────────────────────────────

async def project_fir(fir: FirRecord, *, graph: str = age_client.GRAPH_NAME) -> dict:
    stats = {
        "persons_resolved": 0, "weapons_written": 0, "structured_records": 0,
        "officers_resolved": 0, "edges_written": 0, "errors": [],
    }
    case_id = fir.fir_id
    doc_id = _doc_id_for(fir)
    incident_id = _incident_entity_id(fir)

    try:
        await versioning.write_node("Case", {"case_id": case_id}, {}, source_doc_id=doc_id, graph=graph)
        await versioning.write_node(
            "Document", {"doc_id": doc_id},
            {"filename": fir.fir_id, "doc_type": "fir_structured"},
            source_doc_id=doc_id, graph=graph,
        )
        await versioning.write_edge(
            "BELONGS_TO_CASE", "Document", {"doc_id": doc_id}, "Case", {"case_id": case_id},
            {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
        )

        # Incident node + PART_OF (Incident -> Case) — one of the two
        # previously-dead structural edges this module writes unconditionally.
        #
        # [findings.md T1] `description` carries the FIR's own authoritative
        # narrative verbatim — it is NOT generated, summarized, or inferred.
        # Timeline Building reads Incident.description as its event text and
        # documents it as "DETERMINISTIC, GRAPH-DERIVED, NEVER model-written"
        # precisely so a timeline cannot fabricate what happened; leaving the
        # property unset made every real event render the bare placeholder
        # "Incident {id} (no description recorded)" instead.
        #
        # Optional by the same convention the rest of this module uses (see
        # the `v is None: continue` filters at :337 and :1116, and the
        # `{"event_type": "incident"}` edge at :1167 that simply omits an
        # absent key): a FIR with no narrative writes no `description` at
        # all rather than an empty string or a placeholder, so "unavailable"
        # stays distinguishable from "recorded as blank".
        incident_properties = {
            "canonical_name": f"Incident for FIR {fir.fir_display_code or fir.fir_id}",
        }
        if (fir.narrative_text or "").strip():
            incident_properties["description"] = fir.narrative_text
        await versioning.write_node(
            "Incident", {"entity_id": incident_id}, incident_properties,
            source_doc_id=doc_id, graph=graph,
        )
        await versioning.write_edge(
            "BELONGS_TO_CASE", "Incident", {"entity_id": incident_id}, "Case", {"case_id": case_id},
            {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        await versioning.write_edge(
            "PART_OF", "Incident", {"entity_id": incident_id}, "Case", {"case_id": case_id},
            {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        stats["edges_written"] += 3

        await _write_jurisdiction(fir, case_id, doc_id, graph, stats)
        await _write_occurred_on(fir, incident_id, doc_id, graph, stats)

        accused_by_name: dict[str, str] = {}  # in-FIR only, for weapon matching below
        witness_by_name: dict[str, str] = {}  # in-FIR only, for chalaan name resolution (C2) below

        # Victim/complainant resolved before accused so their entity_ids
        # are available for _write_accused's RELATED_TO writes (Milestone
        # C1) — order matters here, unlike the rest of this function.
        victim_entity_id = await _write_victim(fir, case_id, doc_id, incident_id, graph, stats)
        complainant_entity_id = await _write_complainant(fir, case_id, doc_id, incident_id, graph, stats)
        await _write_accused(
            fir, case_id, doc_id, incident_id, graph, stats, accused_by_name,
            complainant_entity_id, victim_entity_id,
        )
        await _write_witnesses(fir, case_id, doc_id, incident_id, graph, stats, witness_by_name)
        await _write_associated_with(
            case_id, doc_id, graph, stats,
            victim_entity_id, complainant_entity_id, accused_by_name, witness_by_name,
        )
        await _write_weapons(fir, case_id, doc_id, graph, stats, accused_by_name)
        await _write_structured_records(fir, case_id, doc_id, graph, stats, accused_by_name, witness_by_name)
        await _write_officers(fir, case_id, doc_id, graph, stats)

    except Exception as exc:
        logger.error("Structured projection failed for %s: %s", fir.fir_id, exc)
        stats["errors"].append(str(exc))

    return stats


# ── people ────────────────────────────────────────────────────────────────

async def _write_victim(fir, case_id, doc_id, incident_id, graph, stats) -> Optional[str]:
    """
    Milestone C1: a Person node for `FirRecord.victim_name`, resolved
    through the same corroboration-gated resolve_structured_person() path
    as complainant/accused/witness — a bare name with no CNIC otherwise
    gets the identical no-CNIC treatment every other structured mention in
    this module gets. Returns the resolved entity_id, or None when this
    FIR carries no victim_name at all (the majority case, measured live:
    9/73). This entity_id is what fir_accused.relationship_to_victim's
    RELATED_TO edges (_write_accused below) attach to.
    """
    mention = _victim_mention(fir)
    if not mention:
        return None
    try:
        resolution = await resolve_structured_person(mention, case_id, doc_id, graph=graph)
        stats["persons_resolved"] += 1
        await versioning.write_edge(
            "INVOLVED_IN", "Person", {"entity_id": resolution["entity_id"]},
            "Incident", {"entity_id": incident_id}, {"role": "victim"},
            source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        stats["edges_written"] += 1
        return resolution["entity_id"]
    except Exception as exc:
        logger.warning("Victim write failed for %s: %s", fir.fir_id, exc)
        stats["errors"].append(f"victim: {exc}")
        return None


async def _write_complainant(fir, case_id, doc_id, incident_id, graph, stats) -> Optional[str]:
    mention = _complainant_mention(fir)
    if not mention:
        return None
    try:
        resolution = await resolve_structured_person(mention, case_id, doc_id, graph=graph)
        stats["persons_resolved"] += 1
        await versioning.write_edge(
            "INVOLVED_IN", "Person", {"entity_id": resolution["entity_id"]},
            "Incident", {"entity_id": incident_id}, {"role": "complainant"},
            source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        stats["edges_written"] += 1
        if mention.get("address_text"):
            await _write_located_at(resolution["entity_id"], mention["address_text"], doc_id, graph, stats)
        return resolution["entity_id"]
    except Exception as exc:
        logger.warning("Complainant write failed for %s: %s", fir.fir_id, exc)
        stats["errors"].append(f"complainant: {exc}")
        return None


async def _write_related_to(
    from_entity_id: str, to_entity_id: str, role: str, doc_id: str, graph: str, stats: dict,
) -> None:
    """
    Milestone C1: RELATED_TO {role} — `role` is the raw
    relationship_to_victim/relationship_to_complainant text as recorded on
    the accused row (e.g. "اجنبی", "بھائی"). Direction is always
    accused -> victim/complainant, describing the accused's relationship
    to the other party. Written directly, confidence=1.0, no SAME_AS/
    pending step — same "structured field, no heuristic" tier as OWNS/
    OCCURRED_ON elsewhere in this module.
    """
    await versioning.write_edge(
        "RELATED_TO", "Person", {"entity_id": from_entity_id}, "Person", {"entity_id": to_entity_id},
        {"role": role}, source_doc_id=doc_id, confidence=1.0, graph=graph,
    )
    stats["edges_written"] += 1


async def _write_accused(
    fir, case_id, doc_id, incident_id, graph, stats, accused_by_name: dict,
    complainant_entity_id: Optional[str] = None, victim_entity_id: Optional[str] = None,
) -> None:
    for a in fir.child_rows("fir_accused"):
        mention = _person_mention(a)
        if not mention:
            continue
        try:
            resolution = await resolve_structured_person(mention, case_id, doc_id, graph=graph)
            stats["persons_resolved"] += 1
            accused_by_name[a["full_name"]] = resolution["entity_id"]
            await versioning.write_edge(
                "INVOLVED_IN", "Person", {"entity_id": resolution["entity_id"]},
                "Incident", {"entity_id": incident_id},
                {"role": "accused", "arrest_status": a.get("arrest_status") or ""},
                source_doc_id=doc_id, confidence=1.0, graph=graph,
            )
            stats["edges_written"] += 1
            if mention.get("address_text"):
                await _write_located_at(resolution["entity_id"], mention["address_text"], doc_id, graph, stats)
            if a.get("arrested_date"):
                await _write_occurred_on_edge(
                    "Person", resolution["entity_id"], a["arrested_date"],
                    {"event_type": "arrest"}, doc_id, graph, stats,
                )
            if victim_entity_id and a.get("relationship_to_victim"):
                await _write_related_to(
                    resolution["entity_id"], victim_entity_id, a["relationship_to_victim"],
                    doc_id, graph, stats,
                )
            if complainant_entity_id and a.get("relationship_to_complainant"):
                await _write_related_to(
                    resolution["entity_id"], complainant_entity_id, a["relationship_to_complainant"],
                    doc_id, graph, stats,
                )
        except Exception as exc:
            logger.warning("Accused write failed for %s in %s: %s", a.get("full_name"), fir.fir_id, exc)
            stats["errors"].append(f"accused[{a.get('id')}]: {exc}")


async def _write_witnesses(fir, case_id, doc_id, incident_id, graph, stats, witness_by_name: dict) -> None:
    for w in fir.child_rows("fir_witness"):
        mention = _person_mention(w)
        if not mention:
            continue
        try:
            resolution = await resolve_structured_person(mention, case_id, doc_id, graph=graph)
            stats["persons_resolved"] += 1
            witness_by_name[w["full_name"]] = resolution["entity_id"]
            await versioning.write_edge(
                "INVOLVED_IN", "Person", {"entity_id": resolution["entity_id"]},
                "Incident", {"entity_id": incident_id}, {"role": "witness"},
                source_doc_id=doc_id, confidence=1.0, graph=graph,
            )
            stats["edges_written"] += 1
            if mention.get("address_text"):
                await _write_located_at(resolution["entity_id"], mention["address_text"], doc_id, graph, stats)
            await _write_witness_home_jurisdiction(resolution["entity_id"], w, doc_id, graph, stats)
        except Exception as exc:
            logger.warning("Witness write failed for %s in %s: %s", w.get("full_name"), fir.fir_id, exc)
            stats["errors"].append(f"witness[{w.get('id')}]: {exc}")


async def _write_associated_with(
    case_id: str, doc_id: str, graph: str, stats: dict,
    victim_entity_id: Optional[str], complainant_entity_id: Optional[str],
    accused_by_name: dict, witness_by_name: dict,
) -> None:
    """
    findings.md Module 1 — ASSOCIATED_WITH{basis, confidence} between every
    pair of Person nodes that INVOLVED_IN this FIR's Incident (victim,
    complainant, accused, witnesses — everyone _write_victim/
    _write_complainant/_write_accused/_write_witnesses just wrote an
    INVOLVED_IN edge for). This was the only edge type
    graph_retriever.py's multi-hop traversal ever follows between two
    different entities, and this module never wrote it — live count was 0
    across the whole real graph, so every GRAPH/GRAPH_HYBRID/XGRAPH/
    XNETWORK query was permanently hop_count=0 in production.

    Deliberately NOT Officer<->Person — an investigating officer isn't a
    co-conspirator-style associate of the accused. Officers never reach
    this function's roster: they're linked via ASSIGNED_TO -> Case
    (_write_officers), never INVOLVED_IN -> Incident, so no separate
    filter is needed to keep them out.

    `basis` is honestly "co-mentioned in case <id>'s incident", not "known
    associate" — this is a structural fact (two people appear in the same
    FIR), not a stated relationship — hence confidence 0.5, well under the
    1.0 used for directly-stated structured fields elsewhere in this
    module (INVOLVED_IN, RELATED_TO, OWNS). Scoped entirely to this one
    FIR's own roster — never mixes people across two different cases.

    One edge per pair (not both directions) is enough:
    graph_retriever._one_hop_neighbors() matches `(a)-[r:ASSOCIATED_WITH]-(b)`
    with no arrow, i.e. undirected.
    """
    roster = {victim_entity_id, complainant_entity_id, *accused_by_name.values(), *witness_by_name.values()}
    roster.discard(None)
    people = sorted(roster)
    for entity_a, entity_b in itertools.combinations(people, 2):
        try:
            await versioning.write_edge(
                "ASSOCIATED_WITH", "Person", {"entity_id": entity_a}, "Person", {"entity_id": entity_b},
                {"basis": f"co-mentioned in case {case_id}'s incident"},
                source_doc_id=doc_id, confidence=0.5, graph=graph,
            )
            stats["edges_written"] += 1
        except Exception as exc:
            logger.warning(
                "ASSOCIATED_WITH write failed for %s<->%s in %s: %s", entity_a, entity_b, case_id, exc,
            )
            stats["errors"].append(f"associated_with[{entity_a}<->{entity_b}]: {exc}")


async def _write_witness_home_jurisdiction(
    witness_entity_id: str, w: dict, doc_id: str, graph: str, stats: dict,
) -> None:
    """
    Milestone C6 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — witness home
    jurisdiction): `fir_witness.police_station_of_residence_id`/
    `other_district` -> a LOCATED_AT-style edge to the witness's HOME
    PoliceStation/District — distinct from the case's own filing
    jurisdiction (Case-[FILED_AT]->PoliceStation, B1) — a witness's home
    station/district is not the case's station/district.

    `police_station_of_residence_id` is a BARE id string (verified live
    against `tests/fixtures/muhafiz_api_snapshot.json` before assuming its
    shape from the field name alone — e.g. "PS-FSD-CIVILLINES" — never a
    nested object the way `FirRecord.police_station` is), and it matches
    B1's own `PoliceStation.station_id` values exactly. This MUST MERGE
    onto the SAME node B1 already writes, not a second, parallel
    station/district node set — so this reuses B1's exact identity key
    (`{"station_id": ...}`), never a locally-invented one. Written with
    empty properties (`write_node()`'s SET clause is then a no-op on the
    property side — see its own docstring) so a witness-only reference to
    a station never overwrites a real name/code B1 already populated from
    that station's own filing-side data.

    `other_district` (a bare district name, per the schema — never
    observed populated live, 0/37 in the recorded snapshot) is the
    fallback when no `police_station_of_residence_id` is on file: a
    direct Person->District edge, keyed the same id-or-name way B1's own
    `_district_identity()` already tolerates for a bare-string district.
    The two fields are mutually exclusive in this design (station takes
    priority when present) — a witness's home station's own district
    (via PoliceStation-[PART_OF]->District) is NOT independently derived
    here, since this module only has a bare station id for the witness's
    home station, not the nested {id, name, district} object
    `_write_jurisdiction()` has for the case's OWN station; a PART_OF edge
    for that station still lands correctly if the same station also
    happens to be some FIR's filing station (B1's own MERGE-safe write
    covers that case) — this function's honest scope is the witness link
    itself, not fabricating a district it wasn't given.
    """
    station_id = w.get("police_station_of_residence_id")
    if station_id:
        await versioning.write_node("PoliceStation", {"station_id": station_id}, {}, source_doc_id=doc_id, graph=graph)
        edge = await versioning.write_edge(
            "LOCATED_AT", "Person", {"entity_id": witness_entity_id}, "PoliceStation", {"station_id": station_id},
            {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        if edge:
            stats["edges_written"] += 1
        return

    other_district = w.get("other_district")
    if other_district:
        await versioning.write_node(
            "District", {"district_id": other_district}, {"name": other_district},
            source_doc_id=doc_id, graph=graph,
        )
        edge = await versioning.write_edge(
            "LOCATED_AT", "Person", {"entity_id": witness_entity_id}, "District", {"district_id": other_district},
            {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        if edge:
            stats["edges_written"] += 1


async def _write_located_at(person_entity_id: str, address_text: str, doc_id: str, graph: str, stats: dict) -> None:
    await versioning.write_node(
        "Address", {"text": address_text}, {"normalized_text": address_text.strip()},
        source_doc_id=doc_id, graph=graph,
    )
    await versioning.write_edge(
        "LOCATED_AT", "Person", {"entity_id": person_entity_id}, "Address", {"text": address_text},
        {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
    )
    stats["edges_written"] += 1


# ── weapons ───────────────────────────────────────────────────────────────

async def _write_weapons(fir, case_id, doc_id, graph, stats, accused_by_name: dict) -> None:
    for w in fir.child_rows("weapon_register"):
        detail = w.get("item_detail")
        if not detail:
            continue
        try:
            entity_id = f"WEAPON-{w.get('id') or w.get('sr_no')}-{fir.fir_id}"
            await versioning.write_node(
                "Weapon", {"entity_id": entity_id},
                {
                    "canonical_name": detail,
                    "caliber_or_bore": w.get("caliber_or_bore"),
                    "license_status": w.get("license_status"),
                    "condition": w.get("condition"),
                },
                source_doc_id=doc_id, graph=graph,
            )
            await versioning.write_edge(
                "BELONGS_TO_CASE", "Weapon", {"entity_id": entity_id}, "Case", {"case_id": case_id},
                {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
            )
            await versioning.write_edge(
                "APPEARS_IN", "Weapon", {"entity_id": entity_id}, "Document", {"doc_id": doc_id},
                {"surface_text": detail}, source_doc_id=doc_id, confidence=1.0, graph=graph,
            )
            stats["weapons_written"] += 1
            stats["edges_written"] += 2

            # Matched to an accused ONLY within this same FIR (measured
            # 30/31 resolvable in-FIR; recovered_from is a bare name with
            # no cross-FIR identity key, so a global match would risk the
            # same name-collision problem the corroboration gate exists
            # to guard against elsewhere in this module).
            recovered_from = w.get("recovered_from")
            accused_entity_id = accused_by_name.get(recovered_from) if recovered_from else None
            if accused_entity_id:
                await versioning.write_edge(
                    "OWNS", "Person", {"entity_id": accused_entity_id}, "Weapon", {"entity_id": entity_id},
                    {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
                )
                stats["edges_written"] += 1
        except Exception as exc:
            logger.warning("Weapon write failed in %s: %s", fir.fir_id, exc)
            stats["errors"].append(f"weapon[{w.get('id')}]: {exc}")


# ── structured records (typed rows with no identity of their own) ───────

# Milestone C2 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — chalaan name
# resolution): splits chalaan_dispatch.accused_names/witness_names on the
# same Urdu-or-ASCII comma boundary structured_fields.py already splits
# section-reference lists on ("، U+060C" is the corpus convention, ASCII
# comma also observed) — measured live: "محمد عدنان, عمران قریشی" mixes
# both. Not imported from structured_fields.py (that module's own split
# regex is scoped to a different field, section references), but the same
# two-character class.
_NAME_LIST_SPLIT_RE = re.compile(r"[,،]")


def _split_name_list(text: Optional[str]) -> list[str]:
    if not text:
        return []
    return [p.strip() for p in _NAME_LIST_SPLIT_RE.split(text) if p.strip()]


async def _write_chalaan_name_links(
    row: dict, record_id: str, doc_id: str, graph: str, stats: dict,
    accused_by_name: dict, witness_by_name: dict,
) -> None:
    """
    Milestone C2: resolves chalaan_dispatch.accused_names/witness_names
    back to the FIR's OWN already-written Person nodes — reusing the exact
    same in-FIR-only name-matching pattern weapon_register.recovered_from
    already uses (_write_weapons's accused_by_name dict above), not a new
    global-matching mechanism. Global matching here would reintroduce the
    same name-collision risk the corroboration gate exists to guard
    against elsewhere in this module: a name with no match in THIS FIR's
    own accused/witness dicts is simply left unresolved, never looked up
    across the whole graph.

    APPEARS_IN (Person -> StructuredRecord) is the edge — the same label
    already used for "this entity appears in this record/document"
    elsewhere in this module (Weapon/StructuredRecord -> Document), reused
    here for a chalaan_dispatch record naming a person, rather than a new
    edge label for what is the same relationship shape.
    """
    for name in _split_name_list(row.get("accused_names")):
        entity_id = accused_by_name.get(name)
        if not entity_id:
            continue
        edge = await versioning.write_edge(
            "APPEARS_IN", "Person", {"entity_id": entity_id}, "StructuredRecord", {"record_id": record_id},
            {"role": "chalaan_accused", "surface_text": name},
            source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        if edge:
            stats["edges_written"] += 1

    for name in _split_name_list(row.get("witness_names")):
        entity_id = witness_by_name.get(name)
        if not entity_id:
            continue
        edge = await versioning.write_edge(
            "APPEARS_IN", "Person", {"entity_id": entity_id}, "StructuredRecord", {"record_id": record_id},
            {"role": "chalaan_witness", "surface_text": name},
            source_doc_id=doc_id, confidence=1.0, graph=graph,
        )
        if edge:
            stats["edges_written"] += 1


# Milestone C5 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — typed recovered
# property): reuses structured_fields.py's existing plate/phone shape
# detectors (extract_plates()/extract_phones(), built on that module's own
# _PLATE_RE/_PHONE_RE) rather than writing new pattern matchers — the
# same "normalize_urdu() first, tolerate OCR-noise separators" detection
# ingestion/service.py already relies on for free-text plate/phone
# extraction.
async def _classify_and_write_malkhana_item(
    row: dict, case_id: str, doc_id: str, graph: str, stats: dict,
) -> bool:
    """
    malkhana_register.item_detail classified at write time. A value
    shaped like a vehicle plate or phone number resolves into the
    existing Vehicle/PhoneNumber node type via
    entity_resolution.resolve_and_write() — the EXACT SAME call
    src/ingestion/service.py already makes for free-text-extracted
    plates/phones (TYPE_PRIMARY_ID_KEY's cnic_auto-equivalent exact-match
    tier on "plate"/"phone"), so this correctly MERGEs onto whichever
    Vehicle/PhoneNumber node already carries that value — including one
    written by a completely different path (PKM's REGISTERED_TO vehicle,
    or a plate/phone regex-extracted from free narrative text elsewhere)
    — rather than a second, parallel node-minting scheme. One additional
    explicit APPEARS_IN{role: "recovered", surface_text} edge is written
    on top of resolve_and_write()'s own generic (role-less) APPEARS_IN,
    to carry the specific "recovered in this malkhana entry" fact the
    generic edge doesn't.

    Returns True when item_detail classified (caller skips the generic
    StructuredRecord write for this row — "instead of," not "in addition
    to," per the plan) — everything else (cash, generic exhibits) falls
    through to the unchanged StructuredRecord path.
    """
    detail = row.get("item_detail")
    if not detail:
        return False

    plates = structured_fields.extract_plates(detail)
    if plates:
        entity_type, label, id_key, value = "vehicle", "Vehicle", "plate", plates[0].normalized
    else:
        phones = structured_fields.extract_phones(detail)
        if not phones:
            return False
        entity_type, label, id_key, value = "phone", "PhoneNumber", "phone", phones[0].normalized

    resolution = await entity_resolution.resolve_and_write(
        entity_type, {"canonical_name": value, id_key: value, "extraction_confidence": 1.0},
        case_id, doc_id, graph=graph,
    )
    edge = await versioning.write_edge(
        "APPEARS_IN", label, {"entity_id": resolution["entity_id"]}, "Document", {"doc_id": doc_id},
        {"role": "recovered", "surface_text": detail},
        source_doc_id=doc_id, confidence=1.0, graph=graph,
    )
    if edge:
        stats["edges_written"] += 1
    return True


async def _write_structured_records(
    fir, case_id, doc_id, graph, stats,
    accused_by_name: Optional[dict] = None, witness_by_name: Optional[dict] = None,
) -> None:
    for table in _STRUCTURED_RECORD_TABLES:
        for row in fir.child_rows(table):
            row_id = row.get("id")
            if not row_id:
                continue
            try:
                if table == "malkhana_register" and await _classify_and_write_malkhana_item(
                    row, case_id, doc_id, graph, stats,
                ):
                    continue

                record_id = f"{table}:{row_id}"
                properties = {"record_type": table}
                for k, v in row.items():
                    if k in ("id", "fir_id") or v is None:
                        continue
                    properties[k] = v
                await versioning.write_node(
                    "StructuredRecord", {"record_id": record_id}, properties,
                    source_doc_id=doc_id, graph=graph,
                )
                await versioning.write_edge(
                    "BELONGS_TO_CASE", "StructuredRecord", {"record_id": record_id},
                    "Case", {"case_id": case_id}, {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
                )
                await versioning.write_edge(
                    "APPEARS_IN", "StructuredRecord", {"record_id": record_id},
                    "Document", {"doc_id": doc_id}, {}, source_doc_id=doc_id, confidence=1.0, graph=graph,
                )
                stats["structured_records"] += 1
                stats["edges_written"] += 2

                if table == "chalaan_dispatch":
                    await _write_chalaan_name_links(
                        row, record_id, doc_id, graph, stats,
                        accused_by_name or {}, witness_by_name or {},
                    )
            except Exception as exc:
                logger.warning("StructuredRecord write failed for %s in %s: %s", table, fir.fir_id, exc)
                stats["errors"].append(f"structured_record[{table}:{row_id}]: {exc}")


# ── timeline (OCCURRED_ON from typed timestamps) ─────────────────────────

def _zimni_edge_properties(zimni_row: dict) -> dict:
    """
    OCCURRED_ON properties for one fir_zimni row.

    [findings.md T2] `detail` used to be built as
    `f"entry {z.get('entry_number')}"` at BOTH zimni producer sites. When
    entry_number is absent the f-string stringified Python's None, writing
    the literal "entry None" onto the edge — 188 such edges exist in the
    graph the dump restored. Timeline Building renders `detail` verbatim
    (`f": {detail}" if detail else ""`), so an investigator was shown
    "zimni_entry: entry None" as if it were recorded case data.

    Omitting the key entirely (rather than substituting another
    placeholder) is what the rest of this module already does for absent
    values — see the `v is None: continue` filters at :337/:1116 and the
    `{"event_type": "incident"}` edge that simply carries no `detail`. It
    also needs no downstream change: Timeline Building's truthiness check
    already renders a missing detail correctly.

    Shared by both producers so the two cannot drift apart again — the
    original defect existed twice because the same expression was written
    out twice.
    """
    properties = {"event_type": "zimni_entry"}
    entry_number = zimni_row.get("entry_number")
    if entry_number is not None:
        properties["detail"] = f"entry {entry_number}"
    return properties


async def _write_occurred_on_edge(
    from_label: str, from_entity_id: str, date_value: str, edge_properties: dict,
    doc_id: str, graph: str, stats: dict,
) -> None:
    date_str = str(date_value)[:10]  # date or full ISO timestamp -> date-only, matching entity_resolution's own Date node key
    if len(date_str) != 10:
        return
    # Every current caller passes an entity_id-keyed node (Incident or
    # Person) — not generalized to Case (case_id-keyed) since nothing
    # here calls it with one.
    await versioning.write_node("Date", {"date": date_str}, {}, source_doc_id=doc_id, graph=graph)
    await versioning.write_edge(
        "OCCURRED_ON", from_label, {"entity_id": from_entity_id}, "Date", {"date": date_str},
        edge_properties, source_doc_id=doc_id, confidence=1.0, graph=graph,
    )
    stats["edges_written"] += 1


async def _write_occurred_on(fir, incident_id, doc_id, graph, stats) -> None:
    if fir.raw.get("incident_datetime"):
        await _write_occurred_on_edge(
            "Incident", incident_id, fir.raw["incident_datetime"], {"event_type": "incident"},
            doc_id, graph, stats,
        )
    for z in fir.child_rows("fir_zimni"):
        if z.get("entry_date"):
            await _write_occurred_on_edge(
                "Incident", incident_id, z["entry_date"],
                _zimni_edge_properties(z),
                doc_id, graph, stats,
            )
    for cd in fir.child_rows("chalaan_dispatch"):
        if cd.get("dispatch_datetime"):
            await _write_occurred_on_edge(
                "Incident", incident_id, cd["dispatch_datetime"], {"event_type": "chalaan_dispatch"},
                doc_id, graph, stats,
            )
    # Milestone C3: fir_position rewritten from "latest row only"
    # (muhafiz_cases.py's _current_status(), which stays as-is — that's a
    # separate Postgres Case.investigation_status column, out of scope
    # here, same "graph-side addition, source column untouched" precedent
    # B2 set for investigation_officer) to a full dated timeline — every
    # row with a status_date gets its own OCCURRED_ON edge, consistent
    # with how zimni/chalaan_dispatch entries above already handle
    # multiple dated events per Incident, rather than collapsing to one.
    for p in fir.child_rows("fir_position"):
        if p.get("status_date"):
            await _write_occurred_on_edge(
                "Incident", incident_id, p["status_date"],
                {"event_type": "position", "detail": p.get("position") or ""},
                doc_id, graph, stats,
            )
