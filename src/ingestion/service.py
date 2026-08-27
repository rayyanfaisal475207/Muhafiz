# ============================================================
# Ingestion Service — Loads, Chunks, Embeds, and Stores
#
# This service is the entry point for Milestone 2.
# It takes files from data/documents/, processes them,
# pushes them to ChromaDB, and logs them to the SQLite DB.
# ============================================================

import asyncio
import logging
from pathlib import Path
from typing import Optional

from src import config
from src.ingestion.loader_router import route_and_load
from src.ingestion.text_normalizer import normalize_whitespace, normalize_urdu
from src.ingestion.script_detector import is_roman_urdu
from src.ingestion.chunker import chunk_documents
from src.retrieval.embedder import embed_texts
from src.retrieval.vector_store import upsert_documents
from src.ingestion.conflict_bg import _run_conflict_detection_bg
from src.ingestion.reprioritization_bg import _run_reprioritization_bg
from src.ingestion.community_refresh_bg import _run_community_refresh_bg
from src.ingestion.entity_resolution_sampling_bg import _run_entity_resolution_sampling_bg
from src.ingestion.entity_embedding_refresh_bg import _run_entity_embedding_refresh_bg

logger = logging.getLogger(__name__)


# ── Phase 4.10: post-chunking graph extraction/resolution ──────────────

# NER mention type -> entity_resolution entity_type. "weapon" is
# deliberately absent — weapons have no cross-document identity key
# (nothing analogous to CNIC/plate), so they're written as simple graph
# nodes without going through entity resolution at all (see
# _write_unresolved_mention below).
#
# "incident" belongs here too, and its absence was a real bug (found in
# a retrieval-quality audit, not by design): entity_resolution.resolve_and_write
# is the ONLY place that writes an Incident's OCCURRED_ON edge to a Date
# node (see its `if entity_type == "incident" and mention.get("date")`
# branch) — domain_entities.py has extracted "incident" mentions with a
# "date" attribute since Phase 4.6, but every one of them was routed
# through _write_unresolved_mention() instead (no id_key in
# TYPE_PRIMARY_ID_KEY needed it to be there for CNIC-style resolution;
# it was simply missing from this set), which never writes OCCURRED_ON at
# all. The result: NO document, old or newly ingested, ever got a
# timeline edge for its incidents — not a partial/older-documents gap,
# a total one. Confirmed by grepping every write_edge() call site in this
# codebase for "OCCURRED_ON": only entity_resolution.py's dead branch
# had it.
_RESOLVABLE_MENTION_TYPES = {"person", "location", "organization", "vehicle", "incident"}


async def _write_unresolved_mention(label: str, mention_text: str, case_id: str, doc_id: str,
                                     chunk_id: str, confidence: float) -> None:
    """Write a mention type with no cross-document identity (currently: weapon)
    as its own node every time — no resolution, no dedup across mentions.
    """
    from src.graph import versioning
    import uuid as _uuid

    entity_id = f"{label.upper()}-{_uuid.uuid4().hex[:10]}"
    await versioning.write_node(
        label, {"entity_id": entity_id}, {"canonical_name": mention_text},
        source_doc_id=doc_id, confidence=confidence,
    )
    await versioning.write_edge(
        "BELONGS_TO_CASE", label, {"entity_id": entity_id}, "Case", {"case_id": case_id},
        {}, source_doc_id=doc_id, source_chunk_id=chunk_id, confidence=1.0,
    )
    await versioning.write_edge(
        "APPEARS_IN", label, {"entity_id": entity_id}, "Document", {"doc_id": doc_id},
        {"surface_text": mention_text}, source_doc_id=doc_id, source_chunk_id=chunk_id,
        confidence=confidence,
    )


def _attach_chunk_identifiers(mention: dict, chunk_text: str, person_mentions_in_chunk: int) -> dict:
    """
    Best-effort attach a CNIC found in the same chunk to a person mention.

    Deliberate simplification, stated plainly: this does not do proximity/
    field-based linking between a specific name and a specific CNIC — it
    only fires when the chunk contains exactly one CNIC and exactly one
    person mention, the common case for this corpus's compact structured
    header blocks (a witness/complainant block naming one person next to
    their one CNIC). A chunk naming multiple people near one CNIC is left
    unlinked rather than guessed at — an incorrect CNIC attachment is far
    more damaging than a missed one, since CNIC is the primary resolution
    key (§7.3).
    """
    from src.extraction import structured_fields as sf

    if person_mentions_in_chunk != 1:
        return mention
    cnics = sf.extract_cnics(chunk_text)
    if len(cnics) == 1:
        mention = {**mention, "cnic": cnics[0].normalized}
    return mention


def _document_cnic_for_name(name: str, name_cnics: dict[str, str]) -> Optional[str]:
    """
    Look up a CNIC recorded elsewhere in THIS document for a name that is
    a whitespace-bounded prefix of `name`, or vice versa — the common
    narrative pattern where a person's given name alone ("فیصل") recurs
    later in the same document after an earlier full identification line
    ("فیصل ولد محمد رمضان، شناختی کارڈ ..."). Token-based, not a
    substring/similarity score, so "فیصل" matches "فیصل ولد محمد رمضان"
    (the longer name's tokens start with the shorter one's) but never an
    unrelated word that merely shares a character prefix ("فیصلآباد").

    Refuses to guess unless exactly ONE distinct CNIC is reachable this
    way. If the document names two different people who happen to share
    a given name ("فیصل" and "فیصل احمد" with different CNICs), both are
    reachable and this returns None rather than picking one — same "an
    incorrect CNIC attachment is far more damaging than a missed one"
    rule _attach_chunk_identifiers already applies to the single-CNIC-
    per-chunk case.
    """
    name_tokens = normalize_urdu(name).split()
    if not name_tokens:
        return None

    found: set[str] = set()
    for other_name, other_cnic in name_cnics.items():
        if other_name == name:
            continue
        other_tokens = normalize_urdu(other_name).split()
        if not other_tokens:
            continue
        shorter, longer = (
            (name_tokens, other_tokens) if len(name_tokens) <= len(other_tokens)
            else (other_tokens, name_tokens)
        )
        if longer[: len(shorter)] == shorter:
            found.add(other_cnic)

    return next(iter(found)) if len(found) == 1 else None


async def _extract_and_write_relationships(
    relationship_extraction, versioning, text: str, persons: dict[str, str],
    doc_id: str, chunk_id: str, written_pairs: set[frozenset], stats: dict,
) -> None:
    """
    Shared by both the within-chunk pass and the adjacent-chunk-pair pass
    in _run_graph_extraction() below — same extract-then-write shape,
    parameterized on which text/person-set to run it against. `persons`
    maps canonical_name -> entity_id (as resolved_persons is built above);
    `written_pairs` is the caller's whole-document dedup set (a pair
    already written once for this document, by either pass, is skipped
    the second time it's proposed).
    """
    if len(persons) < 2:
        return
    try:
        relationships = await relationship_extraction.extract_relationships(text, list(persons.keys()))
    except Exception as exc:
        logger.warning("relationship_extraction failed for chunk %s: %s", chunk_id, exc)
        stats["errors"].append(f"relationship_extraction[{chunk_id}]: {exc}")
        return

    for rel in relationships:
        entity_a = persons.get(rel["person_a"])
        entity_b = persons.get(rel["person_b"])
        if not entity_a or not entity_b or entity_a == entity_b:
            continue
        pair_key = frozenset({entity_a, entity_b})
        if pair_key in written_pairs:
            continue
        try:
            await versioning.write_edge(
                "ASSOCIATED_WITH", "Person", {"entity_id": entity_a},
                "Person", {"entity_id": entity_b},
                {"basis": rel["basis"]},
                source_doc_id=doc_id, source_chunk_id=chunk_id,
                confidence=rel["confidence"],
            )
            written_pairs.add(pair_key)
            stats["relationships_written"] += 1
        except Exception as exc:
            logger.warning(
                "ASSOCIATED_WITH write failed for %s<->%s in %s: %s",
                rel["person_a"], rel["person_b"], chunk_id, exc,
            )
            stats["errors"].append(f"associated_with[{chunk_id}]: {exc}")


PROJECTION_COMPLETE_PROPERTY = "projection_complete"


async def _graph_projection_complete(doc_id: str) -> bool:
    """
    [findings.md legacy re-ingestion] Has THIS exact chunk-document already
    been projected all the way through, successfully?

    Deliberately keyed on the full content-derived `doc_id` (e.g.
    "psrms_fir_fir-1001-26#narrative_c8bf2613"), not on the FIR id: a
    changed narrative yields a different chunk hash and so must remain
    eligible for extraction.

    Existence of the Document node is NOT the test. That node is written at
    the START of `_run_graph_extraction()`, so it only proves projection
    began — every stage after it catches its own exception and continues.
    Skipping on existence alone would make a half-projected document
    permanently unrecoverable. Only the explicit completion marker, written
    last and only when nothing errored, means "done".

    Historical documents predate the marker and therefore read as
    incomplete, which is the safe answer: they stay eligible rather than
    being retroactively assumed good.
    """
    from src.graph import age_client

    try:
        rows = await age_client.execute_cypher(
            "MATCH (d:Document {doc_id: $doc_id}) "
            f"RETURN d.{PROJECTION_COMPLETE_PROPERTY} AS complete LIMIT 1",
            params={"doc_id": doc_id},
            columns=["complete"],
        )
    except Exception as exc:
        # Fail OPEN on a lookup error: re-extracting costs time, but
        # wrongly skipping would silently drop a document's graph state.
        logger.warning("Projection-completion lookup failed for %s: %s", doc_id, exc)
        return False

    if not rows:
        return False
    return rows[0].get("complete") is True


async def _mark_graph_projection_complete(doc_id: str, stats: dict) -> None:
    """
    Stamp the completion marker — the LAST thing a successful projection
    does, and only when `stats["errors"]` is empty.

    If the stamp itself fails the run is NOT durably complete, so the
    failure is appended to `stats["errors"]` and the marker stays absent:
    the next replay re-projects rather than trusting a half-written state.
    """
    from src.graph import versioning

    try:
        await versioning.write_node(
            "Document",
            {"doc_id": doc_id},
            {PROJECTION_COMPLETE_PROPERTY: True},
            source_doc_id=doc_id,
        )
    except Exception as exc:
        logger.error("Failed to mark graph projection complete for %s: %s", doc_id, exc)
        stats["errors"].append(f"projection_complete_marker: {exc}")


async def _run_graph_extraction(
    source_name: str, documents: list, chunks: list, case_id: str, doc_id: str,
) -> dict:
    """
    Phase 4.3-4.9: structured-field extraction, doc-type classification,
    NER, domain-entity extraction, and entity resolution, all case_id-
    scoped and written through versioning.py.

    Every step here is independently resilient to its own LLM/parse
    failures already (see structured_fields.py/doc_classifier.py/ner.py/
    domain_entities.py/entity_resolution.py's own docstrings) — this
    function's own try/except is a second layer, so an unexpected error
    ANYWHERE in this step degrades to "this document has no graph data,"
    never to "ingestion of this document failed." The caller (ingest_file)
    already got its chunks embedded and stored before this runs.
    """
    from src.extraction import structured_fields as sf
    from src.extraction import doc_classifier, ner, domain_entities, relationship_extraction
    from src.graph import entity_resolution, versioning

    stats = {
        "doc_type": None, "entities_resolved": 0, "entities_unresolved": 0,
        "relationships_written": 0, "errors": [],
    }

    try:
        full_text = "\n".join(d.text for d in documents)

        # Endpoints every entity/document edge in this step needs to
        # already exist — write_edge() never implicitly creates a node.
        await versioning.write_node("Case", {"case_id": case_id}, {}, source_doc_id=doc_id)
        await versioning.write_node(
            "Document", {"doc_id": doc_id}, {"filename": source_name},
            source_doc_id=doc_id,
        )
        await versioning.write_edge(
            "BELONGS_TO_CASE", "Document", {"doc_id": doc_id}, "Case", {"case_id": case_id},
            {}, source_doc_id=doc_id, confidence=1.0,
        )

        # 4.3 — regex-only, no LLM dependency, always attempted.
        try:
            fields = sf.extract_all(full_text)
        except Exception as exc:
            logger.warning("structured_fields extraction failed for %s: %s", source_name, exc)
            fields = {}
            stats["errors"].append(f"structured_fields: {exc}")

        # 2026-08-04: `fields["phones"]` was computed above and then never
        # used anywhere in this function — a real audit finding, not a
        # design choice (entity_resolution.TYPE_TO_LABEL/graph_retriever.py/
        # xagg.py all already treat "has this phone number recurred across
        # cases" as a supported query; no extractor ever produced a
        # PhoneNumber node to make that true). Phones are regex-extracted
        # from the whole document, not per-chunk like NER/domain_entities
        # mentions, so they get their own small resolve_and_write loop here
        # rather than joining the per-chunk `all_mentions` loop below —
        # source_chunk_id stays None, same as the Case/Document nodes above
        # that also aren't tied to one chunk.
        for phone in {p.normalized for p in fields.get("phones", [])}:
            try:
                await entity_resolution.resolve_and_write(
                    "phone",
                    {"canonical_name": phone, "phone": phone, "extraction_confidence": 1.0},
                    case_id, doc_id,
                )
                stats["entities_resolved"] += 1
            except Exception as exc:
                logger.warning("PhoneNumber graph write failed for %r in %s: %s", phone, source_name, exc)
                stats["errors"].append(f"phone_write: {exc}")

        # 2026-08-04: same dead-code pattern as phones above, for vehicle
        # PLATES specifically. domain_entities.py's per-chunk LLM pass is
        # still the only source of full vehicle *descriptions*
        # (make/model/color), and is the right tool for that — but the
        # plate itself, the identifier XAGG's "recurring vehicle across
        # cases" aggregate actually keys on, is a fixed structured format
        # (like CNIC/phone) that a regex already extracts reliably. Relying
        # solely on the LLM to both notice a vehicle mention AND transcribe
        # its plate correctly means one bad LLM reply for a chunk (confirmed
        # live: the local model sometimes answers this prompt conversationally
        # instead of with JSON, same failure class documain_entities.py's
        # retry fix addresses but doesn't fully eliminate) silently loses
        # the vehicle for cross-case recurrence purposes even when the plate
        # was sitting in the text in an obviously regex-matchable format.
        # Writing plate-identified Vehicle nodes directly guarantees XAGG's
        # Vehicle-recurrence path has data to work with independent of LLM
        # extraction quality; the LLM-found vehicles (with fuller
        # descriptions) still merge into the same node via
        # TYPE_PRIMARY_ID_KEY["vehicle"]="plate"'s exact-match auto-merge
        # when both are present.
        for plate in {p.normalized for p in fields.get("plates", [])}:
            try:
                await entity_resolution.resolve_and_write(
                    "vehicle",
                    {"canonical_name": plate, "plate": plate, "extraction_confidence": 1.0},
                    case_id, doc_id,
                )
                stats["entities_resolved"] += 1
            except Exception as exc:
                logger.warning("Vehicle graph write failed for plate %r in %s: %s", plate, source_name, exc)
                stats["errors"].append(f"vehicle_plate_write: {exc}")

        # 4.4 — doc-type classification. Its own LLM-failure handling
        # returns None rather than raising; still guarded here.
        try:
            classification = await doc_classifier.classify_document(full_text)
        except Exception as exc:
            logger.warning("doc_classifier failed for %s: %s", source_name, exc)
            classification = None
            stats["errors"].append(f"doc_classifier: {exc}")

        if classification:
            # M7 (Muhafiz Data API migration, docs/decisions/0001-muhafiz-api-migration.md):
            # doc_classifier.classify_document() now returns doc_type=None
            # (rather than the whole result being discarded) when the LLM
            # names an out-of-vocabulary type, specifically so
            # date_registered still gets written. write_node() sets every
            # listed property unconditionally, though — including it here
            # as a literal None would MERGE null onto doc_type and could
            # clobber a real value from an earlier successful
            # classification on a re-run. Omitted entirely, not written as
            # null, when doc_type is None.
            doc_properties = {
                "date_registered": classification.get("date_registered"),
                "date_registered_confidence": classification.get("date_registered_confidence"),
            }
            if classification["doc_type"] is not None:
                doc_properties["doc_type"] = classification["doc_type"]
            await versioning.write_node(
                "Document", {"doc_id": doc_id}, doc_properties,
                source_doc_id=doc_id, confidence=classification["confidence"],
            )
            stats["doc_type"] = classification["doc_type"]

        # 4.5/4.6 — per-chunk NER + domain-entity extraction, then 4.8
        # resolution for every mention with a cross-document identity.
        #
        # `written_pairs` is scoped to this WHOLE document (not reset per
        # chunk): the within-chunk pass and the adjacent-chunk-pair pass
        # below can both propose the same real-world pair (e.g. two people
        # who co-occur inside chunk N, and are then paired again in the
        # chunk-N/chunk-N+1 window) — this dedups within one ingestion run
        # so the same relationship isn't written twice as separate
        # ASSOCIATED_WITH edges. `prev_chunk` carries the previous
        # iteration's text/resolved-persons forward for that adjacent-pair
        # pass — see its own comment below.
        written_pairs: set[frozenset] = set()
        prev_chunk: Optional[dict] = None
        # [findings.md Module 11] canonical_name -> entity_id for every
        # PERSON resolved anywhere in THIS document so far (unlike
        # `resolved_persons` below, deliberately NOT reset per chunk).
        # entity_resolution.resolve_and_write()'s name-fallback tiers
        # mint a brand-new node for every mention with no CNIC, by design
        # (architecture §7.3 — a name match alone must never auto-merge
        # ACROSS documents/cases). That design is correct for its stated
        # purpose, but it was also firing for the SAME literal string
        # repeated many times within one document's own narrative prose —
        # live-confirmed: one document minted 692 Person nodes for 8
        # distinct strings (231/138/92/47/46/46/46/46 repeats). An exact-
        # string repeat within one ingestion run is a categorically safer
        # case than a cross-document name match — it's the same sentence-
        # level narrative referring back to a person it already named, not
        # two independent documents that merely happen to share a name —
        # so it's collapsed here, at the one point that already knows it's
        # within a single document, rather than loosening
        # resolve_and_write()'s own cross-document matching at all.
        document_resolved_persons: dict[str, str] = {}
        # name -> cnic, for every person mention in THIS document whose
        # CNIC was DIRECTLY found by _attach_chunk_identifiers (never a
        # hoisted/propagated one — see _document_cnic_for_name's own
        # docstring for why only direct observations seed this map).
        # Lets a later mention of the same person by given name alone
        # ("فیصل") resolve via TIER_CNIC_AUTO against the CNIC an earlier
        # chunk's identification line already attached to the full name
        # ("فیصل ولد محمد رمضان"), instead of minting a name-fallback
        # duplicate that only a pending SAME_AS (and later, a human or a
        # collapse script) would ever link back.
        document_person_cnics: dict[str, str] = {}
        for chunk in chunks:
            chunk_id = chunk.doc_id  # the CHUNK's own id (parent doc_id lives in chunk.metadata)
            try:
                ner_mentions = await ner.extract_entities(chunk.text, source_chunk_id=chunk_id)
            except Exception as exc:
                logger.warning("NER failed for chunk %s: %s", chunk_id, exc)
                ner_mentions = []
                stats["errors"].append(f"ner[{chunk_id}]: {exc}")

            try:
                domain_mentions = await domain_entities.extract_domain_entities(
                    chunk.text, source_chunk_id=chunk_id
                )
            except Exception as exc:
                logger.warning("domain_entities failed for chunk %s: %s", chunk_id, exc)
                domain_mentions = []
                stats["errors"].append(f"domain_entities[{chunk_id}]: {exc}")

            all_mentions = ner_mentions + domain_mentions
            person_count = sum(1 for m in all_mentions if m["type"] == "person")

            # canonical_name -> entity_id for every person resolved in THIS
            # chunk, so the relationship pass below (after this loop) can
            # map the names it's given back to the graph nodes to connect.
            resolved_persons: dict[str, str] = {}

            for m in all_mentions:
                mention_type = m["type"]
                mention_dict = {"canonical_name": m["text"], **(m.get("attributes") or {})}
                if mention_type == "person":
                    mention_dict = _attach_chunk_identifiers(mention_dict, chunk.text, person_count)
                    if mention_dict.get("cnic"):
                        # Directly observed — seed the document-wide map
                        # (first observation wins; do not let a later,
                        # possibly OCR-noisier chunk overwrite it).
                        document_person_cnics.setdefault(m["text"], mention_dict["cnic"])
                    elif m["text"] not in document_resolved_persons:
                        # No CNIC on this mention itself — see if this
                        # document already resolved a name-related mention
                        # (e.g. the given name alone, vs. an earlier full
                        # "X ولد Y" identification line) with one.
                        hoisted = _document_cnic_for_name(m["text"], document_person_cnics)
                        if hoisted:
                            mention_dict = {**mention_dict, "cnic": hoisted}
                mention_dict["extraction_confidence"] = m.get("confidence", 1.0)

                try:
                    if mention_type == "person" and m["text"] in document_resolved_persons:
                        # [findings.md Module 11] Exact-string repeat of a
                        # person already resolved earlier in this same
                        # document — reuse that entity_id instead of
                        # minting a fresh node, a fresh BELONGS_TO_CASE
                        # edge, and a fresh (near-certain-to-be-a-
                        # duplicate) pending SAME_AS proposal. Still
                        # records THIS occurrence's own provenance
                        # (APPEARS_IN, this chunk's surface_text/
                        # confidence) — same edge shape
                        # resolve_and_write() itself writes — only the
                        # node-minting/candidate-search/SAME_AS-proposal
                        # steps are skipped, not provenance for this
                        # specific mention.
                        entity_id = document_resolved_persons[m["text"]]
                        if mention_dict.get("cnic"):
                            # [Faisal-in-doc gap] This exact name was
                            # already resolved from an earlier mention
                            # that carried no CNIC (e.g. a bare given-name
                            # occurrence before the narrative's own
                            # identification line). THIS occurrence
                            # directly carries one — backfill it onto the
                            # already-created node rather than losing it,
                            # so a later document in this case that finds
                            # the same CNIC can still match via
                            # TIER_CNIC_AUTO instead of minting yet
                            # another duplicate. Idempotent MERGE/SET —
                            # harmless to repeat if already backfilled.
                            await versioning.write_node(
                                entity_resolution.TYPE_TO_LABEL["person"], {"entity_id": entity_id},
                                {"cnic": mention_dict["cnic"]},
                                source_doc_id=doc_id, confidence=mention_dict.get("extraction_confidence", 1.0),
                            )
                        await versioning.write_edge(
                            "APPEARS_IN",
                            entity_resolution.TYPE_TO_LABEL["person"], {"entity_id": entity_id},
                            "Document", {"doc_id": doc_id},
                            {"surface_text": mention_dict.get("canonical_name", "")},
                            source_doc_id=doc_id, source_chunk_id=chunk_id,
                            confidence=mention_dict.get("extraction_confidence", 1.0),
                        )
                        resolved_persons[m["text"]] = entity_id
                        stats["entities_resolved"] += 1
                    elif mention_type in _RESOLVABLE_MENTION_TYPES:
                        resolution = await entity_resolution.resolve_and_write(
                            mention_type, mention_dict, case_id, doc_id, chunk_id,
                        )
                        stats["entities_resolved"] += 1
                        if mention_type == "person":
                            resolved_persons[m["text"]] = resolution["entity_id"]
                            document_resolved_persons[m["text"]] = resolution["entity_id"]
                    else:
                        label = entity_resolution.TYPE_TO_LABEL.get(mention_type, mention_type.capitalize())
                        await _write_unresolved_mention(
                            label, m["text"], case_id, doc_id, chunk_id, m.get("confidence", 1.0),
                        )
                        stats["entities_unresolved"] += 1
                except Exception as exc:
                    logger.warning("Graph write failed for mention %r in %s: %s", m["text"], chunk_id, exc)
                    stats["errors"].append(f"write[{chunk_id}]: {exc}")

            # 4.6 (completing the gap docs/graph_schema.md always described
            # this phase as covering, but never implemented — see
            # src/extraction/relationship_extraction.py's module docstring):
            # person-to-person ASSOCIATED_WITH edges, extracted from this
            # same chunk's text over the people just resolved above. Only
            # meaningful with 2+ distinct people in the chunk.
            await _extract_and_write_relationships(
                relationship_extraction, versioning, chunk.text, resolved_persons,
                doc_id, chunk_id, written_pairs, stats,
            )

            # 2026-08-06 (Priority 4 of OPEN_GAPS_FIX_PROMPT.md): a
            # relationship stated across two ADJACENT chunks of the same
            # document was never found — extract_relationships only ever
            # saw one chunk's text and that chunk's own resolved people, so
            # "Person A ... [chunk boundary] ... his son Person B" (a real,
            # observed shape: FIR narrative text chunked mid-sentence or
            # between adjacent paragraphs) silently produced no edge, not
            # because no relationship was stated, but because neither
            # single-chunk call ever saw both names at once. Deliberately
            # windowed to ADJACENT pairs only, not a document-level pass
            # over the full concatenated text: CHUNK_SIZE (512 chars) keeps
            # a two-chunk window well inside relationship_extraction.py's
            # existing _MAX_CHARS=3000 cap with no prompt/cap rework
            # needed, and costs exactly one extra LLM call per adjacent
            # pair (bounded, O(chunks), not the O(chunks^2) a naive
            # all-pairs sweep or the much bigger single-call-per-document
            # a full-text pass would cost). A relationship stated more than
            # one chunk apart is still missed — a real, smaller residual
            # limitation of this fix, not a claim of full document-level
            # coverage.
            if prev_chunk is not None:
                combined_persons = {**prev_chunk["persons"], **resolved_persons}
                if len(combined_persons) >= 2:
                    combined_text = prev_chunk["text"] + "\n" + chunk.text
                    await _extract_and_write_relationships(
                        relationship_extraction, versioning, combined_text, combined_persons,
                        doc_id, chunk_id, written_pairs, stats,
                    )
            prev_chunk = {"text": chunk.text, "persons": resolved_persons}

    except Exception as exc:
        logger.error("Graph extraction failed for %s (case %s): %s", source_name, case_id, exc)
        stats["errors"].append(str(exc))

    # [findings.md legacy re-ingestion] Completion marker, written LAST and
    # only on a clean run. Every stage above records its own failure into
    # stats["errors"] and continues, so an empty list is what "every stage
    # either succeeded or had nothing to do" actually looks like here.
    # Anything else leaves the marker unset and the document replayable.
    if not stats["errors"]:
        await _mark_graph_projection_complete(doc_id, stats)

    return stats


async def ingest_directory(dir_path: Path = None, project_id: str = None, is_global: bool = True, case_id: str = None) -> dict:
    """
    Ingest all supported files in a directory.
    If no dir_path provided, uses config.DOCUMENTS_DIR.

    `is_global` defaults to True here (unlike ingest_file's own default of
    False): a directory-wide bulk ingest is the shared knowledge-base corpus
    case (e.g. Phase 0.8's re-ingest of config.DOCUMENTS_DIR), not a
    project-scoped upload — those go through ingest_file() directly with an
    explicit project_id. Previously this had no is_global param at all and
    silently ingested everything as is_global=False, making every
    directory-ingested document invisible to non-project chat retrieval
    (the orchestrator's isolation filter only matches is_global=True or a
    matching project_id).

    `case_id` defaults to None — a bulk directory ingest is the shared/global
    corpus path (the pre-Phase-1 96-document corpus has no case), not a
    single case's evidence upload. Pass it explicitly only when every file
    in the directory genuinely belongs to one case.

    Returns:
        dict: Summary of ingestion (files processed, chunks added).
    """
    if dir_path is None:
        dir_path = config.DOCUMENTS_DIR

    if not dir_path.exists():
        logger.error("Directory not found: %s", dir_path)
        return {"error": "Directory not found"}

    all_files = [f for f in dir_path.iterdir() if f.is_file() and f.name != "README.txt"]
    if not all_files:
        logger.info("No files to ingest in %s", dir_path)
        return {"status": "success", "files_processed": 0, "chunks_added": 0}

    logger.info("Starting ingestion of %d files from %s", len(all_files), dir_path)
    total_chunks = 0

    for file_path in all_files:
        stats = await ingest_file(file_path, project_id=project_id, is_global=is_global, case_id=case_id)
        total_chunks += stats.get("chunks_added", 0)

    return {
        "status": "success",
        "files_processed": len(all_files),
        "chunks_added": total_chunks
    }


async def ingest_documents(
    documents: list,
    source_name: str,
    project_id: str = None,
    is_global: bool = False,
    source_type: str = None,
    category: str = None,
    case_id: str = None,
    doc_type: str = None,
    run_graph_extraction: bool = True,
) -> dict:
    """
    Ingest an already-loaded list of `Document` objects (src/ingestion/document.py)
    into the SHARED knowledge base: normalize, chunk, embed, store, then
    (case_id permitting) run graph extraction. This is everything `ingest_file()`
    used to do past the load step — split out (Milestone A, M2 of the Muhafiz
    Data API migration, see docs/decisions/0001-muhafiz-api-migration.md) so a
    non-file source can call it directly.

    `Document` (src/ingestion/document.py) has no filesystem coupling, so
    nothing below this point ever touches `file_path` — only `documents` and
    the plain `source_name` string used for logging, the Document graph
    node's `filename` property, and the Postgres `documents.filename` column.

    IMPORTANT — bypassing route_and_load() also bypasses every check in
    src/ingestion/validation.py (size limits, magic-byte sniffing, zip-bomb
    guard — see that module's own "single chokepoint" docstring). A caller
    that builds `documents` itself (not via `ingest_file()`) is responsible
    for its own equivalent guard against pathological input; none is added
    here, since what "pathological" means is source-specific (e.g. a REST
    source cares about record count, not file size).

    `is_global`, `source_type`/`category`, and `case_id` — see `ingest_file()`'s
    docstring; identical semantics, just no longer described as being about a
    "file".

    `doc_type`, unlike the rest, has no source-agnostic default: `ingest_file()`
    passes the file's extension here (a historical quirk — this ends up on
    Chroma's `doc_type` metadata key, distinct from the LLM-classified
    `doc_type` `_run_graph_extraction()` separately writes onto the graph's
    Document node; the two share a name but not a meaning, a pre-existing
    wart out of scope for this refactor to fix). A non-file caller should
    pass whatever `doc_type` is meaningful for its own records, or omit it.

    `run_graph_extraction` (default True, unchanged behavior for every
    existing caller) — added M9 of the Muhafiz Data API migration
    (docs/decisions/0001-muhafiz-api-migration.md) so a caller whose
    records already have a BETTER, deterministic graph writer can skip
    `_run_graph_extraction()`'s LLM/NER pass over the same text entirely,
    still with `case_id` scoping chunks for retrieval as normal.
    `scripts/sync_muhafiz_data.py` passes False for every Muhafiz Data
    API record: `src/graph/structured_projection.py`/`cross_silo_projection.py`
    already extract these people/weapons/timelines from ground-truth
    structured fields, so running NER/LLM guesswork over the SAME
    narrative text a second time would be pure waste (cost, latency) and
    would tag the graph with a second, hashed/sanitized family of
    `source_doc_id`s that a re-sync's idempotency purge (which targets
    only the clean, unhashed ids the structured-projection modules write)
    would never clean up — duplicating on every `--full` re-run exactly
    the class of bug this migration's idempotency work exists to close.
    """
    try:
        # Normalize before it reaches the chunker.
        #
        # Urdu normalization runs FIRST: it removes diacritics and
        # tatweel, so it changes offsets, and everything downstream
        # (chunk boundaries, stored text, embeddings, BM25 terms) must
        # see the same canonical form. Whitespace collapsing then cleans
        # up Docling's markdown table column padding.
        for doc in documents:
            doc.text = normalize_whitespace(normalize_urdu(doc.text))

        # Chunk
        chunks = chunk_documents(
            documents,
            chunk_size=config.CHUNK_SIZE,
            chunk_overlap=config.CHUNK_OVERLAP,
            case_id=case_id,
            project_id=project_id,
        )
        if not chunks:
            logger.warning("No chunks generated for %s", source_name)
            return {"chunks_added": 0, "error": "The file produced no text chunks."}

        # Tag every chunk so the vector store writes the right document row
        for chunk in chunks:
            if project_id:
                chunk.metadata["project_id"] = project_id
            if case_id:
                chunk.metadata["case_id"] = case_id
            chunk.metadata["is_global"] = is_global
            if doc_type is not None:
                chunk.metadata["doc_type"] = doc_type
            if source_type:
                chunk.metadata["source_type"] = source_type
            if category:
                chunk.metadata["category"] = category
            # Phase 2.6: per-chunk, not per-document. A bilingual FIR can
            # have an English body and a Roman-Urdu witness statement;
            # tagging the whole file one way would mislabel one of them,
            # and the Phase 9 eval slices by chunk anyway.
            chunk.metadata["is_roman_urdu"] = is_roman_urdu(chunk.text)

        # Embed
        texts_to_embed = [c.text for c in chunks]
        embeddings = await embed_texts(texts_to_embed, task_type="RETRIEVAL_DOCUMENT")

        if len(embeddings) != len(chunks):
            logger.error("Mismatch: %d chunks vs %d embeddings", len(chunks), len(embeddings))
            return {"chunks_added": 0}

        # Save to Chroma + Postgres
        ids = [c.doc_id for c in chunks]
        metadatas = [c.metadata for c in chunks]
        await upsert_documents(
            ids=ids,
            texts=texts_to_embed,
            embeddings=embeddings,
            metadatas=metadatas
        )

        # Record the chunk count on the document row so the dashboard's
        # "chunks per document" breakdown is accurate without counting 88k rows.
        doc_id = chunks[0].metadata.get("doc_id") if chunks else None
        if doc_id:
            try:
                from src.data_gateway import get_gateway
                gateway = await get_gateway()
                await gateway.log_document(
                    doc_id=str(doc_id),
                    filename=source_name,
                    doc_type=doc_type,
                    chunk_count=len(chunks),
                    is_global=is_global,
                    case_id=case_id,
                )
            except Exception as exc:
                logger.warning("Could not update document record for %s: %s", source_name, exc)

        logger.info("Successfully ingested %d chunks from %s", len(chunks), source_name)

        # Phase 4.10: graph extraction/resolution, case_id-scoped. Runs
        # AFTER the chunks are already embedded and stored above — a
        # graph-extraction failure must never take down the (already
        # succeeded) retrieval-facing part of ingestion. Skipped entirely
        # when there's no case_id, matching every other case_id-optional
        # branch in this function: graph writes need BELONGS_TO_CASE's
        # target, and the pre-Phase-1 global corpus has no case to attach to.
        # Also skipped when run_graph_extraction=False (M9) — see this
        # function's own docstring for why a caller with a better,
        # deterministic graph writer opts out here.
        graph_stats = None
        # [findings.md legacy re-ingestion] Replay gate. This chunk's
        # content-derived doc_id is stable, so a document already projected
        # to completion has nothing left to contribute — and re-running the
        # NER/LLM pass would mint fresh random Person entity_ids
        # (entity_resolution._new_entity_id) for every CNIC-less mention,
        # adding duplicate nodes plus their APPEARS_IN/BELONGS_TO_CASE and
        # a new batch of pending SAME_AS candidates on every replay.
        #
        # Checked BEFORE the extraction call so the expensive,
        # non-deterministic work never runs, not after.
        if case_id and doc_id and run_graph_extraction and await _graph_projection_complete(str(doc_id)):
            logger.info(
                "Graph extraction skipped for %s: chunk %s already projected.",
                source_name, doc_id,
            )
            run_graph_extraction = False
            graph_stats = {"skipped": "already_projected"}

        if case_id and doc_id and run_graph_extraction:
            # [Ingestion Quality Control at Scale, Module G1] One
            # tracked run per document — entity_resolution.py's own
            # resolve_and_write() chokepoint (and
            # structured_projection.py's corroboration-gate path, for
            # callers that go through that module instead) record into
            # whichever run is active on this contextvar; nothing here
            # decides anything new, only counts what already happened.
            from src.graph import ingestion_quality
            try:
                async with ingestion_quality.track_run(f"ingest-{doc_id}", "ingest_file", case_id=case_id):
                    graph_stats = await _run_graph_extraction(source_name, documents, chunks, case_id, str(doc_id))
            except Exception as exc:
                logger.error("Graph extraction step raised unexpectedly for %s: %s", source_name, exc)
                graph_stats = {"errors": [str(exc)]}

            # Phase 8.2: Run case-level conflict detection in the background
            asyncio.create_task(_run_conflict_detection_bg(case_id, str(doc_id)))

            # Milestone D1 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — pending-
            # candidate reprioritization): re-score this case's own pending
            # SAME_AS/CITES candidates now that new evidence just landed —
            # the incremental half of D1's execution model, same
            # fire-and-forget shape as conflict detection above.
            asyncio.create_task(_run_reprioritization_bg(case_id))

            # Milestone E3 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md —
            # incremental community refresh): reuses D1's execution model
            # (point 4 of E3's resolved open points) — check whether the
            # whole-graph community partition has drifted enough to be
            # worth a full recompute, and only actually re-run
            # detect_communities()/summarize_communities() when it has.
            # Not case-scoped (unlike D1/conflict detection above) —
            # community detection clusters the whole Person graph, so
            # there's no case-specific slice to pass in.
            asyncio.create_task(_run_community_refresh_bg())

            # findings.md Module 8 (Local Search) — entity description
            # embeddings kept fresh alongside community detection above,
            # same fire-and-forget shape and same "not case-scoped, a new/
            # changed entity anywhere should be embedded" reasoning. Unlike
            # community detection this is a plain incremental diff, not a
            # staleness-gated recompute — see
            # src/graph/entity_embedding_refresh.py's own module docstring.
            asyncio.create_task(_run_entity_embedding_refresh_bg())

            # Ingestion Quality Control at Scale, Module G3 — continuous
            # sampling-based re-verification of already-resolved SAME_AS
            # matches (see src/graph/entity_resolution_sampling.py's
            # module docstring). Same fire-and-forget shape as the three
            # tasks above; deliberately a whole-corpus sample, not scoped
            # to this case, self-throttled internally.
            asyncio.create_task(_run_entity_resolution_sampling_bg())

        # Module 4.3: total_pages must always mean the PDF's true page count
        # (doc.num_pages(), carried in pdf_loader's per-Document metadata),
        # never silently shrink to "however many pages survived". Other
        # loaders never had a "true" page count distinct from their output,
        # so len(documents) is still the right value there — same as before.
        raw_total_pages = documents[0].metadata.get("total_pages") if documents else None
        total_pages = raw_total_pages if raw_total_pages is not None else len(documents)
        dropped_pages = documents[0].metadata.get("dropped_pages", []) if documents else []

        return {
            "doc_id": str(doc_id) if doc_id else None,
            "chunks_added": len(chunks),
            "char_count": sum(len(d.text) for d in documents),
            "ocr_pages": sum(1 for d in documents if d.metadata.get("extraction_method") == "vision_llm"),
            "total_pages": total_pages,
            "pages_ingested": len(documents),
            "dropped_pages": dropped_pages,
            "effective_from": documents[0].metadata.get("effective_from") if documents else None,
            "effective_to": documents[0].metadata.get("effective_to") if documents else None,
            "graph": graph_stats,
        }

    except Exception as exc:
        logger.error("Failed to ingest %s: %s", source_name, exc)
        return {"chunks_added": 0, "error": str(exc)}


async def ingest_file(
    file_path: Path,
    project_id: str = None,
    is_global: bool = False,
    source_type: str = None,
    category: str = None,
    case_id: str = None,
) -> dict:
    """
    Ingest a single file into the SHARED knowledge base.

    1. Load + validate text (via loader_router — see its own docstring for
       the size/magic-byte/zip-bomb checks this enforces before anything
       below runs)
    2. Delegate everything else — chunk, embed, store, graph extraction —
       to ingest_documents() (see its docstring for the full pipeline)

    `is_global=True` marks the document as part of the shared knowledge base —
    that is what admin uploads produce. Chat attachments never reach this
    function: their text is injected into a single conversation and is never
    embedded or indexed.

    `source_type` ('scraped' | 'synthetic') and `category` are optional —
    Phase 4 dataset-manifest tags, carried into every chunk's Chroma
    metadata for provenance filtering. Uploads without a manifest entry
    (e.g. ad-hoc admin uploads) simply omit them.

    `case_id` is optional, not required, at this layer: the pre-existing
    96-document corpus was ingested before Case existed and has no case to
    attach to, and ad-hoc admin uploads to the shared knowledge base aren't
    case evidence either. A caller that IS uploading evidence for a specific
    investigation (the case-scoped upload path Phase 1.8's UI drives)
    should always pass it — enforcing that as a hard requirement belongs at
    that call site (or a future dedicated evidence-upload endpoint), not
    here, where it would also break the existing global-corpus re-ingest.
    """
    logger.info("Ingesting file: %s", file_path.name)
    try:
        # Load — offloaded to a thread: a scanned PDF can hit the
        # vision-fallback retry loop's blocking time.sleep(120) x10, which
        # would otherwise freeze the event loop for up to 20 minutes.
        documents = await asyncio.to_thread(route_and_load, file_path)
        if not documents:
            logger.warning("No content extracted from %s", file_path.name)
            return {"chunks_added": 0, "error": "No text could be extracted from this file."}
    except Exception as exc:
        logger.error("Failed to ingest %s: %s", file_path.name, exc)
        return {"chunks_added": 0, "error": str(exc)}

    return await ingest_documents(
        documents,
        source_name=file_path.name,
        project_id=project_id,
        is_global=is_global,
        source_type=source_type,
        category=category,
        case_id=case_id,
        doc_type=file_path.suffix.lower().lstrip("."),
    )
