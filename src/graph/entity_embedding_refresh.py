# ============================================================
# Entity embedding refresh — GraphRAG-inspired Local Search layer,
# findings.md Module 8.
#
# Additive, read-only against the Apache AGE graph (same interface every
# other read-side module uses — age_client.execute_cypher()); writes only
# to the new Chroma collection in src/retrieval/entity_vector_store.py.
# Mirrors community_detection.py's/community_summarization.py's own split
# (graph reads live in src/graph/, the Chroma wrapper lives in
# src/retrieval/) rather than merging the two.
#
# UNLIKE COMMUNITY DETECTION: community detection is a whole-graph Louvain
# recompute, expensive enough to need a staleness/drift heuristic
# (community_detection.get_staleness(), the 10%-drift check) before it's
# worth re-running. Entity embedding is cheap and purely incremental — no
# LLM call, no graph algorithm, just string formatting + an embedding call
# per entity — so no drift heuristic is needed here: every run does a plain
# diff against what's already in the entity-description collection (new
# entity_id, or an existing one whose description text changed) and only
# pays the embedding cost for the delta.
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

from src.graph import age_client
from src.retrieval.entity_vector_store import (
    delete_entities,
    get_all_entity_documents,
    upsert_entities,
)
from src.retrieval.graph_retriever import (
    _MATCHED_PROPERTY_LABELS,
    _NOTABLE_PROPERTIES,
    _SEED_LABELS,
)

logger = logging.getLogger(__name__)

# Officer is handled separately (needs its own ASSIGNED_TO-role join) — see
# _fetch_candidate_entities() below.
_PLAIN_LABELS: tuple[str, ...] = tuple(label for label in _SEED_LABELS if label != "Officer")


def _describe_entity(label: str, canonical_name: str, properties: dict, role: Optional[str] = None) -> str:
    """
    The text actually embedded. Reuses graph_retriever.py's own
    _NOTABLE_PROPERTIES/_MATCHED_PROPERTY_LABELS as the single source of
    truth for which properties matter and how they're phrased — the same
    table findings.md Module 2 built for the synthetic-evidence sentence,
    not a second, drifting copy of that judgment.

    `designation` (police rank — SI/ASI/Inspector) is included for Officer
    even though it is not itself in _NOTABLE_PROPERTIES (that table drives
    graph_retriever.py's own citation-facing sentence, a separate concern
    from what's worth embedding for semantic matching) — real signal for
    "who is this officer" queries.

    `role` — an Officer's per-case ASSIGNED_TO.role ("investigating" /
    "recording"), when known — is the one piece of data that actually makes
    "investigating officer" semantically distinct from "recording officer"
    for the SAME node. This is Local Search's own addition beyond raw node
    properties, since role lives on the edge, never the node, and
    retrieve_graph() never surfaces it either (see findings.md Module 8's
    "Officer role" finding). None when this entity carries no live
    ASSIGNED_TO edge, or for every non-Officer label.
    """
    parts = [f"{label} {canonical_name}"]
    for prop in _NOTABLE_PROPERTIES.get(label, ()):
        value = properties.get(prop)
        if value:
            parts.append(f"{_MATCHED_PROPERTY_LABELS.get(prop, prop)} {value}")
    designation = properties.get("designation")
    if designation:
        parts.append(f"designation {designation}")
    if role:
        parts.append(f"{role} officer for this case")
    return ", ".join(parts) + "."


async def _fetch_candidate_entities() -> list[dict]:
    """
    Every live entity across _SEED_LABELS, its owning case_id (each node
    belongs to exactly one case via BELONGS_TO_CASE — entity resolution
    mints a new node per case-mention, never merges across cases outside a
    confirmed SAME_AS edge), plus an Officer's current (non-superseded)
    ASSIGNED_TO role when one exists.

    Returns [{entity_id, label, case_id, canonical_name, description_text}, ...].
    Entities with no BELONGS_TO_CASE edge or no entity_id are skipped —
    can't be embedded scoped to a case, which this collection requires by
    design (see entity_vector_store.py's module docstring).
    """
    candidates: list[dict] = []

    for label in _PLAIN_LABELS:
        try:
            rows = await age_client.execute_cypher(
                f"MATCH (n:{label})-[:BELONGS_TO_CASE]->(c:Case) RETURN n, c.case_id AS case_id",
                columns=["n", "case_id"],
            )
        except Exception as exc:
            logger.error("Entity embedding refresh: candidate fetch failed for label %s: %s", label, exc)
            continue
        for row in rows:
            node = row.get("n") or {}
            props = node.get("properties", {}) or {}
            entity_id = props.get("entity_id")
            case_id = row.get("case_id")
            canonical_name = props.get("canonical_name")
            if not entity_id or not case_id or not canonical_name:
                continue
            candidates.append({
                "entity_id": entity_id,
                "label": label,
                "case_id": case_id,
                "canonical_name": canonical_name,
                "description_text": _describe_entity(label, canonical_name, props),
            })

    try:
        officer_rows = await age_client.execute_cypher(
            "MATCH (n:Officer)-[:BELONGS_TO_CASE]->(c:Case) "
            "OPTIONAL MATCH (n)-[r:ASSIGNED_TO]->(c) WHERE r.superseded_by IS NULL "
            "RETURN n, c.case_id AS case_id, r.role AS role",
            columns=["n", "case_id", "role"],
        )
    except Exception as exc:
        logger.error("Entity embedding refresh: candidate fetch failed for label Officer: %s", exc)
        officer_rows = []
    for row in officer_rows:
        node = row.get("n") or {}
        props = node.get("properties", {}) or {}
        entity_id = props.get("entity_id")
        case_id = row.get("case_id")
        canonical_name = props.get("canonical_name")
        if not entity_id or not case_id or not canonical_name:
            continue
        candidates.append({
            "entity_id": entity_id,
            "label": "Officer",
            "case_id": case_id,
            "canonical_name": canonical_name,
            "description_text": _describe_entity("Officer", canonical_name, props, role=row.get("role")),
        })

    return candidates


async def refresh_entity_embeddings() -> dict:
    """
    Plain incremental diff, no staleness heuristic (see module docstring):
    fetch every live candidate entity, compare its freshly-computed
    description text against what's already embedded, upsert only new/
    changed entity_ids, and prune ids that are embedded but no longer
    present in the graph at all (same "a Chroma collection only ever
    upserts — nothing prunes on its own" discipline
    community_vector_store.clear_all_reports()'s own docstring documents as
    a REAL confirmed bug class for the sibling community-report collection;
    not repeating it here).

    Returns {"scanned": int, "upserted": int, "deleted": int}.
    """
    candidates = await _fetch_candidate_entities()
    fresh_by_id = {c["entity_id"]: c for c in candidates}

    existing_documents = await get_all_entity_documents()

    to_upsert = [
        c for entity_id, c in fresh_by_id.items()
        if existing_documents.get(entity_id) != c["description_text"]
    ]
    stale_ids = [entity_id for entity_id in existing_documents if entity_id not in fresh_by_id]

    if to_upsert:
        await upsert_entities(to_upsert)
    if stale_ids:
        await delete_entities(stale_ids)

    logger.info(
        "Entity embedding refresh: %d scanned, %d upserted, %d deleted.",
        len(candidates), len(to_upsert), len(stale_ids),
    )
    return {"scanned": len(candidates), "upserted": len(to_upsert), "deleted": len(stale_ids)}
