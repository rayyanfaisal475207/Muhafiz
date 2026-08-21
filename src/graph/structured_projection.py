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
#
# Every write here is source_doc_id-tagged to the synthetic "#structured"
# Document, so provenance ("what does the system believe, from where") is
# never lost — same append-only discipline as _run_graph_extraction().
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

from src import config
from src.data_gateway.muhafiz_api.models import FirRecord
from src.graph import age_client, entity_resolution, versioning

logger = logging.getLogger(__name__)

_STRUCTURED_RECORD_TABLES = (
    "fir_section", "malkhana_register", "chalaan_dispatch",
    "chalaan_outcome", "fir_zimni_index",
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
    *, graph: str,
) -> dict:
    """
    Bypasses entity_resolution.resolve_and_write() entirely — used when
    the corroboration gate refuses to let a no-CNIC structured mention
    risk a name-fallback SAME_AS candidate at all. Mirrors
    resolve_and_write()'s own TIER_NEW write path exactly (new entity_id,
    Person node, BELONGS_TO_CASE, APPEARS_IN — no SAME_AS, since nothing
    corroborated a link to write).
    """
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
        return await _write_new_person(mention, case_id, source_doc_id, source_chunk_id, graph=graph)
    if await _has_corroboration(mention, case_id, graph=graph):
        return await entity_resolution.resolve_and_write(
            "person", mention, case_id, source_doc_id, source_chunk_id, graph=graph,
        )
    return await _write_new_person(mention, case_id, source_doc_id, source_chunk_id, graph=graph)


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


# ── main entry point ─────────────────────────────────────────────────────

async def project_fir(fir: FirRecord, *, graph: str = age_client.GRAPH_NAME) -> dict:
    stats = {
        "persons_resolved": 0, "weapons_written": 0, "structured_records": 0,
        "edges_written": 0, "errors": [],
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
        await versioning.write_node(
            "Incident", {"entity_id": incident_id},
            {"canonical_name": f"Incident for FIR {fir.fir_display_code or fir.fir_id}"},
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

        await _write_complainant(fir, case_id, doc_id, incident_id, graph, stats)
        await _write_accused(fir, case_id, doc_id, incident_id, graph, stats, accused_by_name)
        await _write_witnesses(fir, case_id, doc_id, incident_id, graph, stats)
        await _write_weapons(fir, case_id, doc_id, graph, stats, accused_by_name)
        await _write_structured_records(fir, case_id, doc_id, graph, stats)

    except Exception as exc:
        logger.error("Structured projection failed for %s: %s", fir.fir_id, exc)
        stats["errors"].append(str(exc))

    return stats


# ── people ────────────────────────────────────────────────────────────────

async def _write_complainant(fir, case_id, doc_id, incident_id, graph, stats) -> None:
    mention = _complainant_mention(fir)
    if not mention:
        return
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
    except Exception as exc:
        logger.warning("Complainant write failed for %s: %s", fir.fir_id, exc)
        stats["errors"].append(f"complainant: {exc}")


async def _write_accused(fir, case_id, doc_id, incident_id, graph, stats, accused_by_name: dict) -> None:
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
        except Exception as exc:
            logger.warning("Accused write failed for %s in %s: %s", a.get("full_name"), fir.fir_id, exc)
            stats["errors"].append(f"accused[{a.get('id')}]: {exc}")


async def _write_witnesses(fir, case_id, doc_id, incident_id, graph, stats) -> None:
    for w in fir.child_rows("fir_witness"):
        mention = _person_mention(w)
        if not mention:
            continue
        try:
            resolution = await resolve_structured_person(mention, case_id, doc_id, graph=graph)
            stats["persons_resolved"] += 1
            await versioning.write_edge(
                "INVOLVED_IN", "Person", {"entity_id": resolution["entity_id"]},
                "Incident", {"entity_id": incident_id}, {"role": "witness"},
                source_doc_id=doc_id, confidence=1.0, graph=graph,
            )
            stats["edges_written"] += 1
            if mention.get("address_text"):
                await _write_located_at(resolution["entity_id"], mention["address_text"], doc_id, graph, stats)
        except Exception as exc:
            logger.warning("Witness write failed for %s in %s: %s", w.get("full_name"), fir.fir_id, exc)
            stats["errors"].append(f"witness[{w.get('id')}]: {exc}")


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

async def _write_structured_records(fir, case_id, doc_id, graph, stats) -> None:
    for table in _STRUCTURED_RECORD_TABLES:
        for row in fir.child_rows(table):
            row_id = row.get("id")
            if not row_id:
                continue
            try:
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
            except Exception as exc:
                logger.warning("StructuredRecord write failed for %s in %s: %s", table, fir.fir_id, exc)
                stats["errors"].append(f"structured_record[{table}:{row_id}]: {exc}")


# ── timeline (OCCURRED_ON from typed timestamps) ─────────────────────────

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
                {"event_type": "zimni_entry", "detail": f"entry {z.get('entry_number')}"},
                doc_id, graph, stats,
            )
    for cd in fir.child_rows("chalaan_dispatch"):
        if cd.get("dispatch_datetime"):
            await _write_occurred_on_edge(
                "Incident", incident_id, cd["dispatch_datetime"], {"event_type": "chalaan_dispatch"},
                doc_id, graph, stats,
            )
