"""Import structured source records as catalog-backed, cited assertions.

Runs no model. Every fact comes from a field whose meaning is established
in `src/evidence/structured.py`; everything else is preserved as a
candidate for review.

    python scripts/evidence_structured_import.py --dry-run
    python scripts/evidence_structured_import.py --target-env .env.evidence-test
    python scripts/evidence_structured_import.py --target-env .env.evidence-test --verify

`--target-env` names the file holding the destination credentials, so an
import cannot reach the live store by omission. There is no default
target: the destination is always stated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from dotenv import dotenv_values, load_dotenv  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.evidence.ingestion import (  # noqa: E402
    put_document, put_span, stable_id, text_hash,
)  # noqa: E402
from src.evidence.structured import (  # noqa: E402
    ACCUSED_IN, CMS_ENTITY, CMS_FIELDS, COMPLAINANT_OF, CRIMINAL_FIELDS,
    CRIMINAL_RECORD_ENTITY, DISTRICT_ENTITY, FIR_ENTITY, FIR_FIELDS,
    PERSON_ENTITY, PKM_ENTITY, PKM_FIELDS, REGISTERED_AT_STATION,
    ROZNAMCHA_ENTITY, ROZNAMCHA_FIELDS, STATION_IN_DISTRICT,
    SUBJECT_OF_RECORD, is_absent,
    REGISTERED_AT_STATION, SECTION_FIELDS, STATION_ENTITY, STATION_IN_DISTRICT,
    STRUCTURED_VERSION, WEAPON_FIELDS, ACCUSED_FIELDS, ImportReport, canonical, ensure_concept,
    ensure_entity, ensure_person, ensure_relation, link_identity,
    locate_value, pointer_value,
    mark_covered, put_assertion, put_candidate, put_relation,
)

SNAPSHOT = Path("tests/fixtures/muhafiz_api_snapshot.json")

# Populated from the snapshot before import; used only to confirm that a
# structurally derived case id names a FIR that actually exists.
KNOWN_FIR_IDS: set[str] = set()

# Child collections whose rows are imported with their own contracts.
CHILDREN = (("fir_section", SECTION_FIELDS, "/id"),
            ("weapon_register", WEAPON_FIELDS, "/id"),
            # The accused rows' own fields. Asserted on the FIR, like the
            # weapon register: combined with the `accused in` relation this
            # answers "cases where X is accused AND was nominated", which
            # is what "is he nominated in any cases" asks.
            ("fir_accused", ACCUSED_FIELDS, "/id"))

# Present in the record, meaning not established by this contract. Kept as
# candidates so nothing is lost and nothing is asserted.
UNCONTRACTED = ("chalaan_dispatch", "chalaan_outcome", "crime_scene_description",
                "crime_scene_location", "crime_scene_beat_number",
                "crime_scene_direction", "crime_scene_distance_km",
                "malkhana_register", "fir_position", "fir_witness",
                "e_tag_number", "cross_version", "form_type")


async def import_fir(conn, record: dict, report: ImportReport) -> None:
    fir_id = pointer_value(record, "/fir_id")
    if not fir_id:
        report.notes.append("a FIR record has no fir_id; skipped")
        return

    text = canonical(record)
    doc_id = await put_document(
        conn, source_key=f"muhafiz-api/fir/{fir_id}", text=text,
        title=f"FIR {pointer_value(record, '/fir_display_code') or fir_id}",
        case_ids=[str(fir_id)], is_global=False,
        metadata={"endpoint": "fir", "primary_key": str(fir_id),
                  "source": "muhafiz_api_snapshot",
                  "serialization": "json/sorted/indent1",
                  # Provenance, not evidence. Kept here rather than as a
                  # queryable concept so an audit can still see when the
                  # source row was written, while no question about the
                  # CASE can be answered with a timestamp about the ROW.
                  "source_row_created_at": pointer_value(record, "/created_at"),
                  "source_row_updated_at": pointer_value(record, "/updated_at")})
    report.documents += 1

    async def span_for(pointer: str, value) -> str | None:
        extent = locate_value(text, pointer, value)
        if not extent:
            return None
        return await put_span(conn, doc_id, text, extent[0], extent[1])

    root_span = await span_for("/fir_id", fir_id)
    if root_span is None:
        report.notes.append(f"{fir_id}: could not locate its own identifier")
        return

    subject = await ensure_entity(conn, FIR_ENTITY, record, doc_id, root_span)
    if subject is None:
        return
    report.entities += 1

    # Station and district are SEPARATE things. Jurisdiction is not the
    # same relation as geographic containment, and collapsing them is how
    # "cases in Lahore" quietly becomes "cases at a Lahore station".
    station_id = district_id = None
    for spec in (STATION_ENTITY, DISTRICT_ENTITY):
        value = pointer_value(record, spec.id_pointer)
        if value in (None, ""):
            continue
        span = await span_for(spec.id_pointer, value)
        if not span:
            continue
        made = await ensure_entity(conn, spec, record, doc_id, span)
        if made:
            report.entities += 1
            await link_identity(conn, entity_id=made, key=str(value),
                                kind=spec.label,
                                identifier_name=spec.identifier_name,
                                report=report)
            if spec is STATION_ENTITY:
                station_id, station_span = made, span
            else:
                district_id, district_span = made, span

    # Jurisdiction and containment are recorded as two DIFFERENT relations.
    # "cases in Lahore" and "cases at a Lahore station" are different
    # questions, and one relation for both would make them indistinguishable.
    if station_id:
        await put_relation(
            conn, doc_id=doc_id, subject_id=subject,
            predicate_id=await ensure_relation(conn, REGISTERED_AT_STATION, doc_id),
            object_id=station_id, span_id=station_span,
            record_key=f"fir/{fir_id}#/police_station/id", report=report)
        if district_id:
            await put_relation(
                conn, doc_id=doc_id, subject_id=station_id,
                predicate_id=await ensure_relation(conn, STATION_IN_DISTRICT, doc_id),
                object_id=district_id, span_id=district_span,
                record_key=f"fir/{fir_id}#/police_station/district/id",
                report=report)

    # ── People, keyed on CNIC ───────────────────────────────────────────
    complainant = {"cnic": record.get("complainant_cnic"),
                   "full_name": record.get("complainant_full_name")}
    if complainant["cnic"]:
        span = await span_for("/complainant_cnic", complainant["cnic"])
        if span:
            person = await ensure_person(conn, complainant, doc_id, span, report)
            if person:
                await link_identity(conn, entity_id=person,
                                    key=str(complainant["cnic"]).strip(),
                                    kind="person", identifier_name="cnic",
                                    report=report)
                await put_relation(
                    conn, doc_id=doc_id, subject_id=subject,
                    predicate_id=await ensure_relation(conn, COMPLAINANT_OF, doc_id),
                    object_id=person, span_id=span,
                    record_key=f"fir/{fir_id}#/complainant_cnic", report=report)
    elif complainant["full_name"]:
        await put_candidate(conn, doc_id=doc_id,
                            record_key=f"fir/{fir_id}#/complainant_full_name",
                            source_table="muhafiz-api/fir/complainant",
                            payload={"reason": "no CNIC; identity unresolved",
                                     **complainant}, report=report)

    for row in (record.get("fir_accused") or []):
        if not isinstance(row, dict):
            continue
        if not (row.get("cnic") or "").strip():
            await put_candidate(
                conn, doc_id=doc_id,
                record_key=f"fir/{fir_id}/fir_accused/{row.get('id')}",
                source_table="muhafiz-api/fir/fir_accused",
                payload={"reason": "no CNIC; identity unresolved", **row},
                report=report)
            continue
        span = await span_for("/cnic", row["cnic"])
        if not span:
            continue
        person = await ensure_person(conn, row, doc_id, span, report)
        if person:
            await link_identity(conn, entity_id=person,
                                key=str(row["cnic"]).strip(), kind="person",
                                identifier_name="cnic", report=report)
            await put_relation(
                conn, doc_id=doc_id, subject_id=subject,
                predicate_id=await ensure_relation(conn, ACCUSED_IN, doc_id),
                object_id=person, span_id=span,
                record_key=f"fir/{fir_id}/fir_accused/{row.get('id')}#/cnic",
                report=report)

    digest = text_hash(text)
    for contract in FIR_FIELDS:
        value = pointer_value(record, contract.pointer)
        predicate_for_coverage = await ensure_concept(conn, contract, doc_id)
        # Recorded whether or not a value is present: reading a named field
        # of a parsed record and finding it empty IS a complete check.
        await mark_covered(conn, doc_id=doc_id,
                           concept_id=predicate_for_coverage,
                           content_hash=digest, present=not is_absent(value),
                           report=report)
        if is_absent(value):
            report.skipped_absent += 1
            continue
        span = await span_for(contract.pointer, value)
        if span is None:
            report.notes.append(f"{fir_id}{contract.pointer}: value not "
                                f"locatable in its own serialization")
            continue
        predicate = await ensure_concept(conn, contract, doc_id)
        await put_assertion(conn, doc_id=doc_id, subject_id=subject,
                            predicate_id=predicate, contract=contract,
                            value=value, span_id=span,
                            record_key=f"fir/{fir_id}#{contract.pointer}",
                            report=report)

    for collection, contracts, key_pointer in CHILDREN:
        rows = pointer_value(record, f"/{collection}") or []
        # Coverage for a child collection is a property of the PARENT
        # record: the list was read in full, so "this FIR has no weapon
        # register entries" is a finding rather than an unchecked gap.
        for contract in contracts:
            await mark_covered(
                conn, doc_id=doc_id,
                concept_id=await ensure_concept(conn, contract, doc_id),
                content_hash=digest,
                present=any(not is_absent(pointer_value(r, contract.pointer))
                            for r in rows if isinstance(r, dict)),
                report=report)
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_key = pointer_value(row, key_pointer) or "row"
            for contract in contracts:
                value = pointer_value(row, contract.pointer)
                if value in (None, "", [], {}):
                    report.skipped_absent += 1
                    continue
                # The child row is serialized inside the parent, so its
                # span still points into the parent's exact text.
                span = await span_for(contract.pointer, value)
                if span is None:
                    continue
                predicate = await ensure_concept(conn, contract, doc_id)
                await put_assertion(
                    conn, doc_id=doc_id, subject_id=subject,
                    predicate_id=predicate, contract=contract, value=value,
                    span_id=span,
                    record_key=f"fir/{fir_id}/{collection}/{row_key}"
                               f"#{contract.pointer}",
                    report=report)

    # ── Coverage for entity and relation concepts ───────────────────────
    #
    # MEASURED: the runtime asked "how many FIRs were reported in June
    # 2026?" and reported "Documents checked: 0/73" even though every
    # `reported at` field was covered. A plan naming a subject_type
    # requires coverage for that ENTITY concept too, and the import only
    # recorded it for properties.
    #
    # The import can make the claim honestly: it reads a parsed record in
    # full, so which entities and relationships that record establishes is
    # determined, not sampled.
    for spec in (FIR_ENTITY, STATION_ENTITY, DISTRICT_ENTITY, PERSON_ENTITY):
        await mark_covered(
            conn, doc_id=doc_id,
            concept_id=stable_id("concept", STRUCTURED_VERSION, "entity", spec.label),
            content_hash=digest,
            present=not is_absent(pointer_value(record, spec.id_pointer)),
            report=report)
    for relation in (REGISTERED_AT_STATION, STATION_IN_DISTRICT,
                     COMPLAINANT_OF, ACCUSED_IN):
        await mark_covered(
            conn, doc_id=doc_id,
            concept_id=stable_id("concept", STRUCTURED_VERSION, "relation", relation.label),
            content_hash=digest, present=True, report=report)

    for name in UNCONTRACTED:
        value = record.get(name)
        if value in (None, "", [], {}):
            continue
        await put_candidate(conn, doc_id=doc_id,
                            record_key=f"fir/{fir_id}#/{name}",
                            source_table=f"muhafiz-api/fir/{name}",
                            payload={"field": name, "value": value},
                            report=report)


# endpoint -> (entity contract, field contracts, person pointer or None,
#              uncontracted fields kept as candidates)
ENDPOINTS = {
  "criminal-records": (CRIMINAL_RECORD_ENTITY, CRIMINAL_FIELDS,
                       ("/subject_cnic", "/subject_full_name"),
                       ("external_record_ref",)),
  "roznamcha":        (ROZNAMCHA_ENTITY, ROZNAMCHA_FIELDS, None, ()),
  "cms":              (CMS_ENTITY, CMS_FIELDS,
                       ("/complainant/cnic", "/complainant/full_name"),
                       ("language", "police_station_code",
                        "police_station_name", "complainant_person_id")),
  "pkm":              (PKM_ENTITY, PKM_FIELDS,
                       ("/applicant/cnic", "/applicant/full_name"),
                       ("language", "certificate_number", "police_station_id",
                        "driving_license", "character_certificate",
                        "loss_report", "tenant_registration",
                        "vehicle_verification", "employee_registration",
                        "women_violence_report")),
}


async def import_record(conn, endpoint: str, record: dict,
                        report: ImportReport) -> None:
    """One record of a non-FIR endpoint, under the same contract discipline."""
    spec, fields, person_ptrs, uncontracted = ENDPOINTS[endpoint]
    key = pointer_value(record, spec.id_pointer)
    if is_absent(key):
        report.notes.append(f"{endpoint}: record without {spec.id_pointer}")
        return

    text = canonical(record)
    # ── Authorization scope, derived only where the SOURCE establishes it ─
    #
    # A document with no case_ids, not global and not session-owned is
    # visible to nobody: evidence.document_allowed() has no branch that
    # admits it. MEASURED after the first import: 125 of 198 documents
    # were unreachable by any user.
    #
    # roznamcha primary keys carry their FIR structurally --
    # "rz-fir-891-24" -> "fir-891-24" -- and 73 of 74 resolve to a known
    # FIR. That is a property of the key, not an interpretation of the
    # entry text, so it is safe to derive.
    #
    # cms, pkm and criminal-records have no such linkage. Their scope is
    # an AUTHORIZATION DECISION, not a derivation: criminal history made
    # global would be readable by every authenticated user. They stay
    # unreachable until that decision is made, and the import reports it.
    case_ids: list[str] = []
    if endpoint == "roznamcha" and str(key).startswith("rz-"):
        candidate = str(key)[3:]
        if candidate in KNOWN_FIR_IDS:
            case_ids = [candidate]
    # ── Force-wide records ───────────────────────────────────────────────
    #
    # criminal-records, cms and pkm carry no case linkage the source
    # establishes, so they cannot be case-scoped without inventing one.
    #
    # DECISION (operator, 24 September 2026): this deployment is used only
    # by police -- investigators, DIG/IG level -- with no civilian or
    # other-government access. These records are therefore force-wide
    # reference material for any AUTHENTICATED user of this system.
    #
    # What that does and does not mean, stated because it is a widening:
    #   * document_allowed() still requires an authenticated evidence.user_id.
    #     An unauthenticated caller sees nothing.
    #   * It DOES bypass case-level scoping: a user restricted to one case
    #     will also see these records. That is the accepted trade, not an
    #     oversight. If per-case restriction is ever required for them, the
    #     source must supply the linkage -- it cannot be derived here.
    force_wide = endpoint in {"criminal-records", "cms", "pkm"}
    doc_id = await put_document(
        conn, source_key=f"muhafiz-api/{endpoint}/{key}", text=text,
        title=f"{spec.label} {key}", case_ids=case_ids, is_global=force_wide,
        metadata={"endpoint": endpoint, "primary_key": str(key),
                  "source": "muhafiz_api_snapshot",
                  "serialization": "json/sorted/indent1"})
    report.documents += 1

    async def span_for(pointer: str, value):
        extent = locate_value(text, pointer, value)
        return await put_span(conn, doc_id, text, extent[0], extent[1]) if extent else None

    root_span = await span_for(spec.id_pointer, key)
    if root_span is None:
        report.notes.append(f"{endpoint}/{key}: identifier not locatable")
        return
    subject = await ensure_entity(conn, spec, record, doc_id, root_span)
    if subject is None:
        return
    report.entities += 1
    await link_identity(conn, entity_id=subject, key=str(key),
                        kind=spec.label, identifier_name=spec.identifier_name,
                        report=report)

    digest = text_hash(text)
    for contract in fields:
        value = pointer_value(record, contract.pointer)
        predicate_for_coverage = await ensure_concept(conn, contract, doc_id)
        await mark_covered(conn, doc_id=doc_id,
                           concept_id=predicate_for_coverage,
                           content_hash=digest, present=not is_absent(value),
                           report=report)
        if is_absent(value):
            report.skipped_absent += 1
            continue
        span = await span_for(contract.pointer, value)
        if span is None:
            continue
        predicate = await ensure_concept(conn, contract, doc_id)
        await put_assertion(conn, doc_id=doc_id, subject_id=subject,
                            predicate_id=predicate, contract=contract,
                            value=value, span_id=span,
                            record_key=f"{endpoint}/{key}#{contract.pointer}",
                            report=report)

    # The person this record is about, keyed on CNIC where the source has one.
    if person_ptrs:
        cnic_ptr, name_ptr = person_ptrs
        cnic = pointer_value(record, cnic_ptr)
        name = pointer_value(record, name_ptr)
        if not is_absent(cnic):
            span = await span_for(cnic_ptr, cnic)
            if span:
                person = await ensure_person(
                    conn, {"cnic": cnic, "full_name": name}, doc_id, span, report)
                if person:
                    await link_identity(conn, entity_id=person,
                                        key=str(cnic).strip(), kind="person",
                                        identifier_name="cnic", report=report)
                    await put_relation(
                        conn, doc_id=doc_id, subject_id=subject,
                        predicate_id=await ensure_relation(conn, SUBJECT_OF_RECORD, doc_id),
                        object_id=person, span_id=span,
                        record_key=f"{endpoint}/{key}#{cnic_ptr}", report=report)
        elif not is_absent(name):
            await put_candidate(conn, doc_id=doc_id,
                                record_key=f"{endpoint}/{key}#{name_ptr}",
                                source_table=f"muhafiz-api/{endpoint}/person",
                                payload={"reason": "no CNIC; identity unresolved",
                                         "name": name}, report=report)

    await mark_covered(
        conn, doc_id=doc_id,
        concept_id=stable_id("concept", STRUCTURED_VERSION, "entity", spec.label),
        content_hash=digest, present=True, report=report)
    if person_ptrs:
        await mark_covered(
            conn, doc_id=doc_id,
            concept_id=stable_id("concept", STRUCTURED_VERSION, "entity",
                                 PERSON_ENTITY.label),
            content_hash=digest,
            present=not is_absent(pointer_value(record, person_ptrs[0])),
            report=report)
        await mark_covered(
            conn, doc_id=doc_id,
            concept_id=stable_id("concept", STRUCTURED_VERSION, "relation",
                                 SUBJECT_OF_RECORD.label),
            content_hash=digest, present=True, report=report)

    for name in uncontracted:
        value = record.get(name)
        if is_absent(value):
            continue
        await put_candidate(conn, doc_id=doc_id,
                            record_key=f"{endpoint}/{key}#/{name}",
                            source_table=f"muhafiz-api/{endpoint}/{name}",
                            payload={"field": name, "value": value},
                            report=report)


async def verify(conn) -> dict:
    """Independent recount from the store, compared with the raw snapshot."""
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    rows = snap["endpoints"]["fir"]
    raw_reported = sum(1 for r in rows if r.get("report_datetime"))
    raw_weapons = sum(len(r.get("weapon_register") or []) for r in rows)

    got_reported = await conn.fetchval(
        """SELECT count(*) FROM evidence.assertions a
           JOIN evidence.concepts c ON c.concept_id=a.predicate_id
           WHERE c.label='reported at' AND a.status='validated'""")
    got_weapon_rows = await conn.fetchval(
        """SELECT count(*) FROM evidence.assertions a
           JOIN evidence.concepts c ON c.concept_id=a.predicate_id
           WHERE c.label='weapon register item detail'""")
    numeric_on_text = await conn.fetchval(
        """SELECT count(*) FROM evidence.assertions a
           JOIN evidence.concepts c ON c.concept_id=a.predicate_id
           WHERE c.label='quantity as recorded' AND a.value_number IS NOT NULL""")
    return {"report_datetime_raw": raw_reported,
            "report_datetime_imported": got_reported,
            "weapon_register_rows_raw": raw_weapons,
            "weapon_register_rows_imported": got_weapon_rows,
            "quantity_stored_as_number": numeric_on_text}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target-env", required=True,
                    help="env file holding EVIDENCE_INGEST_DATABASE_URL")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    load_dotenv(".env")
    target = dotenv_values(args.target_env)
    dsn = target.get("EVIDENCE_INGEST_DATABASE_URL")
    if not dsn:
        raise SystemExit(f"{args.target_env} has no EVIDENCE_INGEST_DATABASE_URL")

    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    records = snapshot["endpoints"]["fir"]
    KNOWN_FIR_IDS.update(str(r["fir_id"]) for r in records if r.get("fir_id"))
    if args.limit:
        records = records[:args.limit]

    # Say where this is going, without printing the credential.
    from urllib.parse import urlsplit
    where = urlsplit(dsn)
    print(f"target database: {where.path.lstrip('/')}  "
          f"({len(records)} FIR records, snapshot {snapshot['fetched_at']})")
    if args.dry_run:
        print("dry run — nothing written")
        return

    report = ImportReport()
    conn = await asyncpg.connect(dsn)
    # The same JSON codec the runtime's pool installs. Without it a dict
    # bound to a jsonb column raises "expected str, got dict", and the
    # import would otherwise need to pre-serialize -- which would diverge
    # from how the runtime reads these columns back.
    for name in ("json", "jsonb"):
        await conn.set_type_codec(
            name, encoder=lambda v: json.dumps(v, default=str),
            decoder=json.loads, schema="pg_catalog")
    try:
        for record in records:
            async with conn.transaction():
                await import_fir(conn, record, report)
        for endpoint in ENDPOINTS:
            rows = snapshot["endpoints"].get(endpoint) or []
            if args.limit:
                rows = rows[:args.limit]
            for record in rows:
                async with conn.transaction():
                    await import_record(conn, endpoint, record, report)
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        if args.verify:
            print("\nverification against the raw snapshot:")
            print(json.dumps(await verify(conn), ensure_ascii=False, indent=2))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
