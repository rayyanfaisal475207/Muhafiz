# ============================================================
# Ingestion Service — Loads, Chunks, Embeds, and Stores
#
# This service is the entry point for Milestone 2.
# It takes files from data/documents/, processes them,
# pushes them to ChromaDB, and logs them to the SQLite DB.
# ============================================================

import logging
from pathlib import Path

from src import config
from src.ingestion.loader_router import route_and_load
from src.ingestion.text_normalizer import normalize_whitespace, normalize_urdu
from src.ingestion.script_detector import is_roman_urdu
from src.ingestion.chunker import chunk_documents
from src.retrieval.embedder import embed_texts
from src.retrieval.vector_store import upsert_documents
from src.database.pipeline_logger import log_ingested_chunk
from src.ingestion.conflict_bg import _run_conflict_detection_bg

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


async def _run_graph_extraction(
    file_path: Path, documents: list, chunks: list, case_id: str, doc_id: str,
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
            "Document", {"doc_id": doc_id}, {"filename": file_path.name},
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
            logger.warning("structured_fields extraction failed for %s: %s", file_path.name, exc)
            fields = {}
            stats["errors"].append(f"structured_fields: {exc}")

        # 4.4 — doc-type classification. Its own LLM-failure handling
        # returns None rather than raising; still guarded here.
        try:
            classification = await doc_classifier.classify_document(full_text)
        except Exception as exc:
            logger.warning("doc_classifier failed for %s: %s", file_path.name, exc)
            classification = None
            stats["errors"].append(f"doc_classifier: {exc}")

        if classification:
            await versioning.write_node(
                "Document", {"doc_id": doc_id},
                {"doc_type": classification["doc_type"], "date_registered": classification.get("date_registered")},
                source_doc_id=doc_id, confidence=classification["confidence"],
            )
            stats["doc_type"] = classification["doc_type"]

        # 4.5/4.6 — per-chunk NER + domain-entity extraction, then 4.8
        # resolution for every mention with a cross-document identity.
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
                mention_dict["extraction_confidence"] = m.get("confidence", 1.0)

                try:
                    if mention_type in _RESOLVABLE_MENTION_TYPES:
                        resolution = await entity_resolution.resolve_and_write(
                            mention_type, mention_dict, case_id, doc_id, chunk_id,
                        )
                        stats["entities_resolved"] += 1
                        if mention_type == "person":
                            resolved_persons[m["text"]] = resolution["entity_id"]
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
            if len(resolved_persons) >= 2:
                try:
                    relationships = await relationship_extraction.extract_relationships(
                        chunk.text, list(resolved_persons.keys())
                    )
                except Exception as exc:
                    logger.warning("relationship_extraction failed for chunk %s: %s", chunk_id, exc)
                    relationships = []
                    stats["errors"].append(f"relationship_extraction[{chunk_id}]: {exc}")

                for rel in relationships:
                    entity_a = resolved_persons.get(rel["person_a"])
                    entity_b = resolved_persons.get(rel["person_b"])
                    if not entity_a or not entity_b or entity_a == entity_b:
                        continue
                    try:
                        await versioning.write_edge(
                            "ASSOCIATED_WITH", "Person", {"entity_id": entity_a},
                            "Person", {"entity_id": entity_b},
                            {"basis": rel["basis"]},
                            source_doc_id=doc_id, source_chunk_id=chunk_id,
                            confidence=rel["confidence"],
                        )
                        stats["relationships_written"] += 1
                    except Exception as exc:
                        logger.warning(
                            "ASSOCIATED_WITH write failed for %s<->%s in %s: %s",
                            rel["person_a"], rel["person_b"], chunk_id, exc,
                        )
                        stats["errors"].append(f"associated_with[{chunk_id}]: {exc}")

    except Exception as exc:
        logger.error("Graph extraction failed for %s (case %s): %s", file_path.name, case_id, exc)
        stats["errors"].append(str(exc))

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

    1. Load text (via loader_router)
    2. Chunk text
    3. Embed chunks
    4. Save chunks to ChromaDB (the same collection retrieval reads)
    5. Return stats, including the doc_id, so the caller can track the job

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
        # 1. Load
        documents = route_and_load(file_path)
        if not documents:
            logger.warning("No content extracted from %s", file_path.name)
            return {"chunks_added": 0, "error": "No text could be extracted from this file."}

        # 1b. Normalize before it reaches the chunker.
        #
        # Urdu normalization runs FIRST: it removes diacritics and
        # tatweel, so it changes offsets, and everything downstream
        # (chunk boundaries, stored text, embeddings, BM25 terms) must
        # see the same canonical form. Whitespace collapsing then cleans
        # up Docling's markdown table column padding.
        for doc in documents:
            doc.text = normalize_whitespace(normalize_urdu(doc.text))

        # 2. Chunk
        chunks = chunk_documents(
            documents,
            chunk_size=config.CHUNK_SIZE,
            chunk_overlap=config.CHUNK_OVERLAP,
            case_id=case_id,
            project_id=project_id,
        )
        if not chunks:
            logger.warning("No chunks generated for %s", file_path.name)
            return {"chunks_added": 0, "error": "The file produced no text chunks."}

        # Tag every chunk so the vector store writes the right document row
        for chunk in chunks:
            if project_id:
                chunk.metadata["project_id"] = project_id
            if case_id:
                chunk.metadata["case_id"] = case_id
            chunk.metadata["is_global"] = is_global
            chunk.metadata["doc_type"] = file_path.suffix.lower().lstrip(".")
            if source_type:
                chunk.metadata["source_type"] = source_type
            if category:
                chunk.metadata["category"] = category
            # Phase 2.6: per-chunk, not per-document. A bilingual FIR can
            # have an English body and a Roman-Urdu witness statement;
            # tagging the whole file one way would mislabel one of them,
            # and the Phase 9 eval slices by chunk anyway.
            chunk.metadata["is_roman_urdu"] = is_roman_urdu(chunk.text)

        # 3. Embed
        # Extract text for embedding
        texts_to_embed = [c.text for c in chunks]
        embeddings = await embed_texts(texts_to_embed, task_type="RETRIEVAL_DOCUMENT")

        if len(embeddings) != len(chunks):
            logger.error("Mismatch: %d chunks vs %d embeddings", len(chunks), len(embeddings))
            return {"chunks_added": 0}

        # Attach embeddings to metadata for vector_store to pick up
        # Note: In our current vector_store.py we might just pass text, let's see.
        # Actually ChromaDB can generate embeddings itself if we pass an embedding function,
        # but we do it manually to use Gemini. We'll pass embeddings to upsert_documents.

        # 4. Save to PostgreSQL
        ids = [c.doc_id for c in chunks]
        metadatas = [c.metadata for c in chunks]
        await upsert_documents(
            ids=ids,
            texts=texts_to_embed,
            embeddings=embeddings,
            metadatas=metadatas
        )

        # 5. Log to SQLite
        # ext = file_path.suffix.lower().lstrip(".")
        # for c, emb in zip(chunks, embeddings):
        #     log_ingested_chunk(
        #         chunk_id=c.doc_id,
        #         source_file=file_path.name,
        #         source_path=str(file_path),
        #         file_type=ext,
        #         chunk_index=c.metadata.get("chunk_index", 0),
        #         chunk_total=c.metadata.get("chunk_total", 1),
        #         chunk_text=c.text,
        #         embedding_model=config.GEMINI_EMBEDDING_MODEL,
        #         embedding_dims=len(emb)
        #     )

        # Record the chunk count on the document row so the dashboard's
        # "chunks per document" breakdown is accurate without counting 88k rows.
        doc_id = chunks[0].metadata.get("doc_id") if chunks else None
        if doc_id:
            try:
                from src.data_gateway import get_gateway
                gateway = await get_gateway()
                await gateway.log_document(
                    doc_id=str(doc_id),
                    filename=file_path.name,
                    doc_type=file_path.suffix.lower().lstrip("."),
                    chunk_count=len(chunks),
                    is_global=is_global,
                    case_id=case_id,
                )
            except Exception as exc:
                logger.warning("Could not update document record for %s: %s", file_path.name, exc)

        logger.info("Successfully ingested %d chunks from %s", len(chunks), file_path.name)

        # Phase 4.10: graph extraction/resolution, case_id-scoped. Runs
        # AFTER the chunks are already embedded and stored above — a
        # graph-extraction failure must never take down the (already
        # succeeded) retrieval-facing part of ingestion. Skipped entirely
        # when there's no case_id, matching every other case_id-optional
        # branch in this function: graph writes need BELONGS_TO_CASE's
        # target, and the pre-Phase-1 global corpus has no case to attach to.
        graph_stats = None
        if case_id and doc_id:
            try:
                graph_stats = await _run_graph_extraction(file_path, documents, chunks, case_id, str(doc_id))
            except Exception as exc:
                logger.error("Graph extraction step raised unexpectedly for %s: %s", file_path.name, exc)
                graph_stats = {"errors": [str(exc)]}
            
            # Phase 8.2: Run case-level conflict detection in the background
            import asyncio
            asyncio.create_task(_run_conflict_detection_bg(case_id, str(doc_id)))

        return {
            "doc_id": str(doc_id) if doc_id else None,
            "chunks_added": len(chunks),
            "char_count": sum(len(d.text) for d in documents),
            "ocr_pages": sum(1 for d in documents if d.metadata.get("extraction_method") == "vision_llm"),
            "total_pages": len(documents),
            "effective_from": documents[0].metadata.get("effective_from") if documents else None,
            "effective_to": documents[0].metadata.get("effective_to") if documents else None,
            "graph": graph_stats,
        }

    except Exception as exc:
        logger.error("Failed to ingest %s: %s", file_path.name, exc)
        return {"chunks_added": 0, "error": str(exc)}
