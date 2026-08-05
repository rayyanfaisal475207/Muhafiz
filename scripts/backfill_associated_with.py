# ============================================================
# One-off backfill: regenerate ASSOCIATED_WITH edges using the fixed
# src/extraction/relationship_extraction.py (see git history — "Fix
# relationship_extraction.py's missing JSON-retry"). That fix only
# changes behavior for FUTURE extraction calls; this script re-runs
# extraction against chunks that were already ingested before the fix
# landed, using the persons ALREADY resolved for each chunk (via existing
# APPEARS_IN edges) rather than re-running NER/entity_resolution —
# deliberately, since entity_resolution.py's name-fallback tier mints a
# new node per mention rather than merging (see docs/graph_schema.md),
# so re-running resolution here would create MORE duplicate Person nodes,
# not fix anything. This only adds ASSOCIATED_WITH edges between people
# who already exist in the graph.
#
# Precedented in this repo's own history: commit 3fb031d ("Fix router
# misclassification and XAGG cross-case aggregate pipeline") did the same
# kind of one-time "backfilled graph extraction... with the fixed
# pipeline" operation.
#
# Run AFTER scripts/cleanup_implausible_person_nodes.py --apply — this
# script reads whichever Person nodes currently exist per chunk, so
# running it before cleanup would extract relationships involving the
# form-label-noise nodes cleanup was meant to remove.
#
# Run: python scripts/backfill_associated_with.py
# ============================================================

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.extraction import relationship_extraction
from src.graph import age_client, versioning
from src.retrieval.vector_store import get_chunks_by_ids


async def _fetch_chunk_person_groups() -> dict[str, dict[str, str]]:
    """chunk_id -> {entity_id: canonical_name} for every Person's non-superseded APPEARS_IN edge."""
    rows = await age_client.execute_cypher(
        "MATCH (p:Person)-[r:APPEARS_IN]->() WHERE r.superseded_by IS NULL "
        "RETURN p.entity_id AS entity_id, p.canonical_name AS name, r.source_chunk_id AS chunk_id",
        columns=["entity_id", "name", "chunk_id"],
    )
    groups: dict[str, dict[str, str]] = defaultdict(dict)
    for row in rows:
        if row["chunk_id"] and row["entity_id"] and row["name"]:
            groups[row["chunk_id"]][row["entity_id"]] = row["name"]
    return groups


async def main() -> None:
    groups = await _fetch_chunk_person_groups()
    multi_person_chunks = {cid: g for cid, g in groups.items() if len(g) >= 2}
    print(f"Chunks with 2+ resolved persons: {len(multi_person_chunks)} (out of {len(groups)} chunks with any resolved person)")

    if not multi_person_chunks:
        print("Nothing to backfill.")
        return

    chunk_texts = {c["id"]: c["text"] for c in await get_chunks_by_ids(list(multi_person_chunks.keys()))}
    print(f"Fetched text for {len(chunk_texts)}/{len(multi_person_chunks)} chunks (missing ones were never in Chroma or since deleted).")

    processed = 0
    relationships_found = 0
    edges_written = 0
    errors = 0

    for chunk_id, entity_by_id in multi_person_chunks.items():
        text = chunk_texts.get(chunk_id)
        if not text:
            continue
        processed += 1

        # name -> entity_id, first-seen-wins if two nodes share a name —
        # same behavior src/ingestion/service.py's own resolved_persons
        # dict has during normal ingestion (a dict keyed by text).
        name_to_entity: dict[str, str] = {}
        for entity_id, name in entity_by_id.items():
            name_to_entity.setdefault(name, entity_id)
        distinct_names = list(name_to_entity.keys())

        try:
            relationships = await relationship_extraction.extract_relationships(text, distinct_names)
        except Exception as exc:
            print(f"  [{chunk_id}] extraction failed: {exc}")
            errors += 1
            continue

        for rel in relationships:
            relationships_found += 1
            entity_a = name_to_entity.get(rel["person_a"])
            entity_b = name_to_entity.get(rel["person_b"])
            if not entity_a or not entity_b or entity_a == entity_b:
                continue
            # source_doc_id isn't tracked per-group here (would need a
            # second query joining back to Document) — versioning.write_edge
            # requires it for provenance, so derive it from any one of this
            # chunk's Person->Document APPEARS_IN edges directly.
            doc_rows = await age_client.execute_cypher(
                "MATCH (p:Person {entity_id: $eid})-[r:APPEARS_IN]->(d) "
                "WHERE r.source_chunk_id = $chunk_id AND r.superseded_by IS NULL "
                "RETURN r.source_doc_id AS doc_id LIMIT 1",
                params={"eid": entity_a, "chunk_id": chunk_id}, columns=["doc_id"],
            )
            source_doc_id = doc_rows[0]["doc_id"] if doc_rows else "unknown"
            try:
                await versioning.write_edge(
                    "ASSOCIATED_WITH", "Person", {"entity_id": entity_a},
                    "Person", {"entity_id": entity_b},
                    {"basis": rel["basis"]},
                    source_doc_id=source_doc_id, source_chunk_id=chunk_id,
                    confidence=rel["confidence"],
                )
                edges_written += 1
            except Exception as exc:
                print(f"  [{chunk_id}] write failed for {rel['person_a']}<->{rel['person_b']}: {exc}")
                errors += 1

    print(
        f"\nDone. Chunks processed: {processed} | relationships found: {relationships_found} | "
        f"edges written: {edges_written} | errors: {errors}"
    )

    final = await age_client.execute_cypher(
        "MATCH (:Person)-[r:ASSOCIATED_WITH]->(:Person) WHERE r.superseded_by IS NULL RETURN count(r) AS c",
        columns=["c"],
    )
    print(f"Total ASSOCIATED_WITH edges in graph now: {final[0]['c']}")


if __name__ == "__main__":
    asyncio.run(main())
