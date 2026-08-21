# ============================================================
# Graph versioning — append-only writes, lock/unlock (Phase 4.9)
#
# Every writer in this phase (4.8's entity resolution, 4.10's ingestion
# wiring, and Phase 8.2's future conflict detection) goes through this
# module rather than calling age_client.execute_cypher() directly for
# writes — it is the one place the append-only discipline (§7.4) is
# actually enforced, not a convention every call site has to remember on
# its own.
#
# Rules this module enforces:
#   1. Nodes are upserted (MERGE on a caller-given key) — a node's
#      identity isn't itself a "fact that gets superseded" the way a
#      relationship is; its properties can be refreshed in place.
#   2. Edges are NEVER mutated in place. A new edge always gets a fresh
#      `as_of`; if it supersedes an earlier edge, the earlier edge's
#      `superseded_by` is set to the new edge's id — the old edge is never
#      deleted, so "what did the system believe, and from what source, at
#      what point in time" stays answerable (§7.4).
#   3. OCCURRED_ON edges support an investigator `locked` flag. A write
#      that would supersede a LOCKED OCCURRED_ON edge is refused (logged,
#      not raised) — this is the "suppresses further automatic revision
#      until explicitly unlocked" behavior SOW Module 6 asks for.
#
# Known limitation, stated plainly: superseding an edge is two sequential
# Cypher calls (create the new edge, then mark the old one superseded),
# not one atomic transaction — acceptable for this POC's write volume and
# single-writer-at-a-time ingestion model (4.10 processes documents
# sequentially per case), flagged here rather than silently assumed safe
# at higher concurrency.
# ============================================================

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from src.graph import age_client, identity_index
from src.data_gateway import get_gateway

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _match_clause(var: str, match: dict) -> str:
    """Build `{key1: $m_key1, key2: $m_key2}` for a MATCH/MERGE pattern.

    Keys are always fixed identifiers from this codebase (entity_id,
    case_id, doc_id, ...), never raw request input — same trust model as
    age_client.py's cypher_query/graph arguments. Validated below anyway
    (A-3, defense in depth): every current caller already passes hardcoded
    literal keys, so this is a no-op today, but it closes the one place in
    this file that DIDN'T validate before Cypher interpolation while its
    sibling _build_set_clause already did — a future caller can't silently
    reintroduce an injection point the sibling function would have caught.
    """
    if not match:
        return ""
    for k in match:
        if not _VALID_KEY_RE.match(k):
            raise ValueError(f"Invalid match key for a graph write: {k!r}")
    parts = ", ".join(f"{k}: $m_{k}" for k in match)
    return f" {{{parts}}}"


def _match_params(match: dict) -> dict:
    return {f"m_{k}": v for k, v in match.items()}


_VALID_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _build_set_clause(var: str, properties: dict, param_prefix: str) -> tuple[str, dict]:
    """
    Build `var.key1 = $prefix_key1, var.key2 = $prefix_key2, ...` and the
    matching params dict.

    AGE's `SET x += $param` (map-merge from a *parameter reference*) does
    not work in this AGE version — confirmed empirically: it raises
    `FeatureNotSupportedError: SET clause expects a map` even though the
    exact same `+=` operator works fine with a literal map in the Cypher
    text. Property dicts here have per-entity-type-varying keys (CNIC on a
    Person, plate on a Vehicle, ...), so a fixed literal template isn't an
    option — this builds the equivalent individual-assignment SET clause
    per call instead, which is the form that does work.

    Keys are validated against a plain identifier pattern before being
    interpolated into the Cypher text — defense in depth. They are always
    meant to be fixed attribute names from this codebase's extractors
    (structured_fields.py, domain_entities.py), never raw request text,
    but a malformed key here would otherwise silently become a Cypher
    injection point.
    """
    assignments = []
    params = {}
    for key, value in (properties or {}).items():
        if not _VALID_KEY_RE.match(key):
            raise ValueError(f"Invalid property key for a graph write: {key!r}")
        param_name = f"{param_prefix}_{key}"
        assignments.append(f"{var}.{key} = ${param_name}")
        params[param_name] = value
    return ", ".join(assignments), params


# ── Nodes ────────────────────────────────────────────────────────────────

async def write_node(
    label: str,
    match: dict,
    properties: Optional[dict] = None,
    *,
    source_doc_id: Optional[str] = None,
    confidence: float = 1.0,
    graph: str = age_client.GRAPH_NAME,
) -> Optional[dict]:
    """
    Upsert a node: MERGE on `match` (the node's identity key, e.g.
    {"entity_id": "P-001"}), then set/refresh `properties` on it.

    Idempotent — calling this again with the same `match` updates the
    existing node rather than creating a duplicate.

    `graph` defaults to the production graph — production call sites never
    override it. scripts/eval_entity_resolution.py is the one caller that
    passes `graph="evidence_graph_eval"`, so eval fixtures never land in
    real case data (Phase 3, Module 3.1).
    """
    prop_clause, prop_params = _build_set_clause("n", properties or {}, "p")
    set_clauses = ["n.as_of = $as_of", "n.confidence = $confidence", "n.source_doc_id = $source_doc_id"]
    if prop_clause:
        set_clauses.append(prop_clause)

    cypher = f"""
        MERGE (n:{label}{_match_clause('n', match)})
        SET {", ".join(set_clauses)}
        RETURN n
    """
    params = {
        **_match_params(match),
        **prop_params,
        "as_of": _now_iso(),
        "confidence": confidence,
        "source_doc_id": source_doc_id,
    }
    rows = await age_client.execute_cypher(cypher, params=params, columns=["n"], graph=graph)
    node = rows[0]["n"] if rows else None

    if node:
        gateway = await get_gateway()
        await gateway.log_audit_event("graph_write", {
            "action": "write_node",
            "label": label,
            "match": match,
            "node_id": node["id"]
        })

        # Graph Scale & Schema Expansion, Milestone A1: maintain the
        # Postgres identity index alongside every write_node() call for an
        # identity-bearing label (see src/graph/identity_index.py). A
        # single choke point here, rather than every call site
        # remembering to do it — same reasoning this module already
        # applies to the append-only edge discipline.
        entity_id = node.get("properties", {}).get("entity_id")
        if entity_id:
            merged_properties = {**match, **(properties or {})}
            await identity_index.maintain(label, entity_id, merged_properties)
    return node


# ── Edges (append-only) ─────────────────────────────────────────────────

async def get_edge(edge_id: int, *, graph: str = age_client.GRAPH_NAME) -> Optional[dict]:
    """Fetch an edge by its AGE-internal id, for locked/superseded checks."""
    rows = await age_client.execute_cypher(
        "MATCH ()-[r]->() WHERE id(r) = $edge_id RETURN r",
        params={"edge_id": edge_id},
        columns=["r"],
        graph=graph,
    )
    return rows[0]["r"] if rows else None


async def write_edge(
    edge_label: str,
    from_label: str,
    from_match: dict,
    to_label: str,
    to_match: dict,
    properties: Optional[dict] = None,
    *,
    source_doc_id: str,
    source_chunk_id: Optional[str] = None,
    confidence: float = 1.0,
    supersedes_edge_id: Optional[int] = None,
    graph: str = age_client.GRAPH_NAME,
) -> Optional[dict]:
    """
    Append a new versioned edge from an existing `from` node to an
    existing `to` node (both matched, never implicitly created — a
    caller referencing a node that doesn't exist yet is a real bug, not
    something to paper over with an implicit MERGE).

    If `supersedes_edge_id` is given, the prior edge is marked
    `superseded_by` this new edge's id — never deleted, never mutated
    beyond that one pointer. If the prior edge is a LOCKED OCCURRED_ON
    edge, the write is refused entirely (logged, returns None) rather
    than silently overriding an investigator's lock.

    Returns the new edge (as a parsed AGE dict), or None if the write was
    refused or either endpoint node doesn't exist.

    `graph` defaults to the production graph, same as write_node() — see
    its docstring.
    """
    if supersedes_edge_id is not None:
        prior = await get_edge(supersedes_edge_id, graph=graph)
        if prior is None:
            logger.warning("write_edge: supersedes_edge_id %s not found — refusing write", supersedes_edge_id)
            return None
        if prior.get("properties", {}).get("locked"):
            logger.info(
                "write_edge: edge %s is locked — refusing to supersede it "
                "(investigator must unlock_event() first)", supersedes_edge_id,
            )
            return None
        if prior.get("properties", {}).get("superseded_by") is not None:
            # Refuse a second edge superseding the same prior edge — a
            # version chain is linear, not a fork. Without this, two
            # concurrent callers (e.g. a double-submitted review-queue
            # confirm) can each successfully supersede the same "pending"
            # edge, leaving two edges both claiming to be the current
            # replacement with no way to tell which one actually is.
            logger.info(
                "write_edge: edge %s was already superseded by %s — refusing "
                "a second supersede of the same edge",
                supersedes_edge_id, prior["properties"]["superseded_by"],
            )
            return None

    prop_clause, prop_params = _build_set_clause("r", properties or {}, "p")
    set_clauses = [
        "r.as_of = $as_of", "r.confidence = $confidence",
        "r.source_doc_id = $source_doc_id", "r.source_chunk_id = $source_chunk_id",
    ]
    if prop_clause:
        set_clauses.append(prop_clause)

    params = {
        **{f"a_{k}": v for k, v in from_match.items()},
        **{f"b_{k}": v for k, v in to_match.items()},
        **prop_params,
        "as_of": _now_iso(),
        "confidence": confidence,
        "source_doc_id": source_doc_id,
        "source_chunk_id": source_chunk_id,
    }
    # Two distinct prefixes (a_/b_), not the shared _match_clause() $m_
    # convention used by write_node() — from/to would otherwise collide
    # on a same-named key (e.g. both matched on "case_id").
    from_clause = _prefixed_match_clause(from_match, "a")
    to_clause = _prefixed_match_clause(to_match, "b")
    cypher = f"""
        MATCH (a:{from_label}{from_clause})
        MATCH (b:{to_label}{to_clause})
        CREATE (a)-[r:{edge_label}]->(b)
        SET {", ".join(set_clauses)}
        RETURN r
    """

    rows = await age_client.execute_cypher(cypher, params=params, columns=["r"], graph=graph)
    if not rows:
        logger.warning(
            "write_edge: no rows returned — one or both endpoint nodes "
            "not found (%s%s -> %s%s)", from_label, from_match, to_label, to_match,
        )
        return None
    new_edge = rows[0]["r"]

    if supersedes_edge_id is not None:
        await age_client.execute_cypher(
            "MATCH ()-[old]->() WHERE id(old) = $old_id "
            "SET old.superseded_by = $new_id RETURN old",
            params={"old_id": supersedes_edge_id, "new_id": new_edge["id"]},
            columns=["old"],
            graph=graph,
        )

    gateway = await get_gateway()
    await gateway.log_audit_event("graph_write", {
        "action": "write_edge",
        "edge_label": edge_label,
        "from_label": from_label,
        "to_label": to_label,
        "supersedes_edge_id": supersedes_edge_id,
        "new_edge_id": new_edge["id"]
    })

    return new_edge


def _prefixed_match_clause(match: dict, prefix: str) -> str:
    """See _match_clause's docstring — same A-3 defense-in-depth validation."""
    if not match:
        return ""
    for k in match:
        if not _VALID_KEY_RE.match(k):
            raise ValueError(f"Invalid match key for a graph write: {k!r}")
    parts = ", ".join(f"{k}: ${prefix}_{k}" for k in match)
    return f" {{{parts}}}"


# ── Locked/verified timeline events (OCCURRED_ON) ──────────────────────

async def lock_event(edge_id: int, locked_by: str) -> bool:
    """
    Mark an OCCURRED_ON edge as investigator-locked/verified. A lock does
    not delete or alter the edge's history — it adds a marker that
    write_edge() checks before allowing that edge to be superseded.
    """
    rows = await age_client.execute_cypher(
        "MATCH ()-[r:OCCURRED_ON]->() WHERE id(r) = $edge_id "
        "SET r.locked = true, r.locked_by = $locked_by, r.locked_at = $locked_at "
        "RETURN r",
        params={"edge_id": edge_id, "locked_by": locked_by, "locked_at": _now_iso()},
        columns=["r"],
    )
    if rows:
        gateway = await get_gateway()
        await gateway.log_audit_event("graph_write", {
            "action": "lock_event",
            "edge_id": edge_id,
            "locked_by": locked_by
        })
    return bool(rows)


async def unlock_event(edge_id: int, unlocked_by: str) -> bool:
    """Clear the investigator lock, allowing future revisions again."""
    rows = await age_client.execute_cypher(
        "MATCH ()-[r:OCCURRED_ON]->() WHERE id(r) = $edge_id "
        "SET r.locked = false, r.unlocked_by = $unlocked_by, r.unlocked_at = $unlocked_at "
        "RETURN r",
        params={"edge_id": edge_id, "unlocked_by": unlocked_by, "unlocked_at": _now_iso()},
        columns=["r"],
    )
    if rows:
        gateway = await get_gateway()
        await gateway.log_audit_event("graph_write", {
            "action": "unlock_event",
            "edge_id": edge_id,
            "unlocked_by": unlocked_by
        })
    return bool(rows)
