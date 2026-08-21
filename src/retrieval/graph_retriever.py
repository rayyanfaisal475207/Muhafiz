# ============================================================
# Graph retriever — Phase 5.2.
#
# Traverses `evidence_graph` (Apache AGE) via src/graph/age_client's
# execute_cypher() to answer entity+relationship queries, capped at 2-3
# hops, and returns chunk-shaped dicts the rest of the retrieval stack
# (bm25_retriever / reranker / cross_reranker / evaluator /
# _format_documents_for_prompt) already consumes — no special-casing
# needed downstream. Never touches age_client's connection/pool/LOAD
# machinery directly; every graph read goes through execute_cypher().
#
# ╔═══════════════════════════════════════════════════════════════════╗
# ║ THE ONE RULE THIS MODULE OWNS: SAME_AS IDENTITY IS CONFIRMED-ONLY. ║
# ╚═══════════════════════════════════════════════════════════════════╝
# Entity resolution (Phase 4, docs/graph_schema.md "Entity resolution &
# canonicalization") never physically merges two AGE nodes on a
# probabilistic match — a name-fallback match creates a SEPARATE node
# linked by a SAME_AS edge with status in {pending, confirmed, rejected}.
# "All mentions of this real-world person" is therefore NOT "one graph
# node" — it is "this node plus every node reachable via a CONFIRMED
# SAME_AS edge." This module follows that rule exactly once, in
# `_expand_confirmed_identity()` below:
#   - status='confirmed'  -> same identity. Traverse through it freely.
#   - status='pending'    -> NOT the same identity yet. Never traversed
#     as identity, but surfaced separately (see `unconfirmed_links` in
#     `retrieve_graph`'s return) so a cross-case answer can carry the
#     caveat ("possibly the same person, unconfirmed") instead of either
#     silently ignoring it (under-connecting — misses a repeat offender
#     the resolver correctly flagged) or silently treating it as fact
#     (over-connecting — the "single biggest way a knowledge graph fails
#     quietly," per docs/graph_schema.md).
#   - status='rejected'   -> confirmed NOT the same identity. Never
#     traversed, never surfaced.
# Get this wrong in either direction and this is exactly the failure mode
# the whole project exists to avoid — see the P-006 (flagged_unverified,
# must surface WITH caveat) and ADDR-002 (shared address, must NOT be
# surfaced as a relationship) fixtures in tests/test_graph_retriever.py.
#
# ── Why LOCATED_AT/OWNS/REGISTERED_TO never expand the traversal ──
# ASSOCIATED_WITH is the only edge type this module follows to connect
# two DIFFERENT real-world entities — it is the one edge type
# docs/graph_schema.md itself describes as "Generic person-to-person
# relationship... e.g. co-accused" (i.e. an actual relationship).
# LOCATED_AT/OWNS/REGISTERED_TO are attribute edges of a single entity
# (their address, their vehicle) — fetched for display context on a node
# already reached, but never used to hop onward to a THIRD entity.
# Concretely: if two unrelated people share an address, following
# LOCATED_AT outward from Person A to the shared Address node and back
# would present Person B as "connected" to Person A, which is exactly
# the ADDR-002 negative test this module must not fail.
#
# ── M8 of the Muhafiz Data API migration (docs/decisions/0001-muhafiz-api-migration.md):
# the same decision, made explicitly, for the edge types M6a/M6b added ──
#   - INVOLVED_IN (Person -> Incident, role=complainant|accused|witness) —
#     NOT traversed. Same reasoning as LOCATED_AT: it is an attribute edge
#     of one entity's role in an incident, not a stated relationship
#     between two entities. Two different accused both INVOLVED_IN the
#     same Incident would otherwise appear "connected" by hopping through
#     the Incident node — the exact ADDR-002 failure shape, just with
#     Incident standing in for Address. A genuine co-accused relationship
#     is what ASSOCIATED_WITH already exists to capture (extracted with
#     an LLM-judged basis, not implied by shared incident membership).
#   - PART_OF (Incident -> Case) — NOT traversed. Purely structural
#     (mirrors BELONGS_TO_CASE), never a hop between two different
#     real-world entities in the first place.
#   - CITES (Case -> Case, src/graph/cross_silo_projection.py) — NOT
#     traversed by this module at all, confirmed or not. This module's
#     traversal is entity-centric (Person/Vehicle/PhoneNumber/Organization
#     seeds hopping via ASSOCIATED_WITH); CITES is a case-level edge with
#     no entity-level analogue here. A future case-level traversal
#     feature built on CITES would need its own confirmed-only discipline
#     (status='confirmed' only — never 'pending' — exactly the SAME_AS
#     rule above, generalized) rather than reusing this module's
#     entity-hop machinery as-is.
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

from src.extraction.structured_fields import _CNIC_RE, _PHONE_RE, _PLATE_RE
from src.graph import age_client
from src.graph.case_scope import scoped_cypher
from src.ingestion.text_normalizer import normalize_urdu
from src.retrieval.vector_store import get_chunks_by_ids
from src.data_gateway import get_gateway
from src.database.postgres import current_cross_case, current_rls_active

logger = logging.getLogger(__name__)

MAX_HOPS = 3
DEFAULT_HOPS = 2

# label -> (identifier properties checked with exact/CONTAINS match, display-name property)
#
# PhoneNumber/Organization fixed in M8 of the Muhafiz Data API migration
# (docs/decisions/0001-muhafiz-api-migration.md): this table looked up
# `number`/`name`, but no writer anywhere in the codebase has ever set
# either property — `_run_graph_extraction()`'s phone-writing loop
# (src/ingestion/service.py) writes {"canonical_name": phone, "phone": phone},
# and every organization write (NER/domain_entities via
# entity_resolution.resolve_and_write) uses `canonical_name` only. The seed
# lookup for these two labels could therefore never match a real node —
# confirmed live during this migration's investigation, not previously
# known. `id_props` now matches what is actually written; the unused
# second element (display-name) is left as `canonical_name` for
# consistency, though nothing currently reads it (see the `_display`
# variable below, intentionally unused — `_display_name()` already has
# its own independent canonical_name-first priority list).
_SEED_LABELS: dict[str, tuple[tuple[str, ...], str]] = {
    "Person": (("canonical_name", "cnic"), "canonical_name"),
    "Vehicle": (("plate",), "plate"),
    "PhoneNumber": (("canonical_name", "phone"), "canonical_name"),
    "Organization": (("canonical_name",), "canonical_name"),
}

# The only edge type used to hop between two DIFFERENT entities — see
# module docstring for why LOCATED_AT/OWNS/REGISTERED_TO are excluded.
_HOP_EDGE_TYPE = "ASSOCIATED_WITH"

# Keyword hints for picking which label(s) a cross-case "has any X recurred"
# query is about ("has any phone number been used across multiple cases?"),
# mirroring src/pipeline/xagg.py's keyword dispatch (kept local rather than
# imported to avoid a retrieval->pipeline dependency).
_LABEL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "PhoneNumber": ("phone", "number", "فون", "نمبر"),
    "Vehicle": ("vehicle", "car", "motorcycle", "plate", "گاڑی"),
    # "لوگ" ("people", the common everyday Urdu word — substring-matches
    # "لوگوں" too, see _matches_any's `kw in lowered`) was missing here even
    # though "افراد" (a more formal synonym) was present — a query using the
    # everyday word matched nothing at all and silently returned an empty
    # seed set instead of falling through to this label.
    "Person": ("person", "people", "suspect", "offender", "شخص", "افراد", "لوگ"),
    "Organization": ("organization", "gang", "group", "ring", "گروہ"),
}

# Distinguishes "has X recurred across cases" (the narrower question
# _find_recurring_entities_for_query's default min_cases=2 answers) from
# "list/enumerate every X mentioned across the cases" (the broader
# question actually being asked when there's no specific instance in
# mind and no recurrence claim at all — "list of all people mentioned in
# the cases"/"مقدمات میں مذکور تمام لوگوں کی فہرست"). Checked in
# retrieve_graph()'s cross-case, no-entity branch to pick min_cases=1
# (every instance, not just recurring ones) for this shape of question.
_ENUMERATION_KEYWORDS = ("list", "every", "all", "تمام", "فہرست")


def _display_name(node: Optional[dict]) -> str:
    if not node:
        return ""
    props = node.get("properties", {}) or {}
    for key in ("canonical_name", "plate", "number", "name"):
        if props.get(key):
            return props[key]
    return props.get("entity_id", "")


def _entity_id_of(node: Optional[dict]) -> Optional[str]:
    if not node:
        return None
    return (node.get("properties", {}) or {}).get("entity_id")


def _seed_candidates(target_entity: Optional[str], query_text: str) -> list[str]:
    """
    Build the list of literal strings to look up as graph seed nodes.
    `target_entity` (the router's verbatim extraction) is always tried
    first; identifier regexes (CNIC/phone/plate — reused from
    structured_fields.py, never re-copied) additionally scan both
    `target_entity` and the raw query text, since the router may name an
    entity in prose ("the suspect's phone") while the actual dialable
    number only appears elsewhere in the query.
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(text: Optional[str]) -> None:
        if text and text.strip() and text.strip() not in seen:
            seen.add(text.strip())
            candidates.append(text.strip())

    _add(target_entity)
    for raw in (target_entity or "", query_text or ""):
        norm = normalize_urdu(raw)
        for regex in (_CNIC_RE, _PHONE_RE, _PLATE_RE):
            for m in regex.finditer(norm):
                _add(m.group(0))

    return candidates


async def _find_seed_nodes(
    candidates: list[str], case_id: Optional[str], cross_case: bool
) -> list[dict]:
    """
    Resolve candidate strings to graph nodes. Within-case: seed search is
    scoped to this case's own BELONGS_TO_CASE edge (single filtered hop,
    per docs/graph_schema.md). Cross-case: deliberately unscoped — the
    case filter is ABSENT BY DESIGN here (not bypassed by a bug), gated
    only by the caller passing `cross_case=True`, so Phase 7 can attach a
    permission to this exact branch without restructuring it.
    """
    if not candidates:
        return []

    seeds: list[dict] = []
    seen_ids: set[str] = set()
    for label, (id_props, _display) in _SEED_LABELS.items():
        for cand in candidates:
            where_parts = [f"toLower(n.{prop}) CONTAINS toLower($cand)" for prop in id_props]
            where_clause = " OR ".join(where_parts)
            try:
                if cross_case:
                    cypher = f"""
                        MATCH (n:{label})
                        WHERE {where_clause}
                        RETURN n
                    """
                    rows = await age_client.execute_cypher(cypher, params={"cand": cand}, columns=["n"])
                else:
                    # Case-scoped: routed through case_scope.scoped_cypher()
                    # so a future edit can't silently drop the case filter.
                    cypher = f"""
                        MATCH (n:{label})-[:BELONGS_TO_CASE]->(c:Case {{case_id: $case_id}})
                        WHERE {where_clause}
                        RETURN n
                    """
                    rows = await scoped_cypher(cypher, case_id, params={"cand": cand}, columns=["n"])
            except Exception as exc:
                logger.error("Graph seed lookup failed for %r (%s): %s", cand, label, exc)
                continue

            for row in rows:
                node = row.get("n")
                entity_id = _entity_id_of(node)
                if entity_id and entity_id not in seen_ids:
                    seen_ids.add(entity_id)
                    seeds.append(node)

    return seeds


async def _find_all_case_entities(case_id: str) -> list[dict]:
    """
    Every Person/Vehicle/PhoneNumber/Organization node belonging to this
    case — the seed set for a case-wide enumeration query that names no
    specific entity to traverse from ("how many accused are involved in
    CASE-009", "who are the witnesses in FIR-2026-ARMS-001"). Bounded to
    one case's own (small) entity set via BELONGS_TO_CASE, same scoping
    guarantee as the literal-match branch in `_find_seed_nodes`.
    """
    seeds: list[dict] = []
    seen_ids: set[str] = set()
    for label in _SEED_LABELS:
        try:
            rows = await scoped_cypher(
                f"MATCH (n:{label})-[:BELONGS_TO_CASE]->(c:Case {{case_id: $case_id}}) RETURN n",
                case_id,
                columns=["n"],
            )
        except Exception as exc:
            logger.error("Case-wide seed lookup failed for label %s, case %r: %s", label, case_id, exc)
            continue
        for row in rows:
            node = row.get("n")
            entity_id = _entity_id_of(node)
            if entity_id and entity_id not in seen_ids:
                seen_ids.add(entity_id)
                seeds.append(node)
    return seeds


async def _find_recurring_entities_for_query(
    query_text: str, min_cases: int = 2, limit: Optional[int] = None
) -> list[dict]:
    """
    Cross-case fallback seed set for either of two related but different
    questions that name no specific instance, distinguished by `min_cases`:
      - min_cases=2 (default): "has ANY X recurred across cases?" ("has any
        phone number been used across multiple cyber fraud cases?", "کیا
        کوئی فون نمبر متعدد ... مقدمات میں استعمال ہوا ہے؟") — the
        recurrence itself IS the answer.
      - min_cases=1: "list/enumerate every X across the cases" ("list of
        all people mentioned in the cases"/"مقدمات میں مذکور تمام لوگوں کی
        فہرست") — every instance, not just recurring ones. See
        retrieve_graph()'s `_ENUMERATION_KEYWORDS` check for which callers
        pass this.
    Picks the label(s) the query text hints at via `_LABEL_KEYWORDS`. If
    nothing hints at a type (e.g. a query about a shared attribute like an
    address, not an entity type this graph tracks), this deliberately
    returns empty rather than scanning every label — a graph-wide,
    unscoped "anything anywhere" seed set would inject irrelevant
    cross-case noise into an otherwise-unrelated query (the ADDR-002
    negative test this project's own eval set guards against: a shared
    address must never be presented as a relationship).

    `limit`, when set, caps the returned seed count (kept deterministic —
    sorted by case-recurrence count descending, so the most
    cross-case-relevant entities survive the cut, not an arbitrary
    Cypher row order) — real-corpus enumeration ("list everyone") could
    otherwise return an unbounded wall of names and, downstream, trigger a
    hop-traversal/chunk-fetch pass over every one of them.
    """
    lowered = (query_text or "").lower()
    labels = [label for label, kws in _LABEL_KEYWORDS.items() if any(kw in lowered for kw in kws)]
    if not labels:
        return []

    seeds: list[dict] = []
    seen_ids: set[str] = set()
    seed_case_counts: dict[str, int] = {}
    for label in labels:
        try:
            rows = await age_client.execute_cypher(
                f"MATCH (n:{label})-[:BELONGS_TO_CASE]->(c:Case) RETURN n, c",
                columns=["n", "c"],
            )
        except Exception as exc:
            logger.error("Cross-case recurrence lookup failed for label %s: %s", label, exc)
            continue
        cases_by_entity: dict[str, set[str]] = {}
        node_by_entity: dict[str, dict] = {}
        for row in rows:
            node = row.get("n")
            entity_id = _entity_id_of(node)
            case_props = (row.get("c") or {}).get("properties", {}) or {}
            case_id = case_props.get("case_id")
            if not entity_id or not case_id:
                continue
            cases_by_entity.setdefault(entity_id, set()).add(case_id)
            node_by_entity[entity_id] = node
        for entity_id, cases in cases_by_entity.items():
            if len(cases) >= min_cases and entity_id not in seen_ids:
                seen_ids.add(entity_id)
                seed_case_counts[entity_id] = len(cases)
                seeds.append(node_by_entity[entity_id])

    if limit is not None and len(seeds) > limit:
        seeds.sort(key=lambda n: seed_case_counts.get(_entity_id_of(n), 0), reverse=True)
        logger.warning(
            "Cross-case enumeration for %r returned %d entities, capped to %d",
            query_text[:60], len(seeds), limit,
        )
        seeds = seeds[:limit]

    return seeds


async def _expand_confirmed_identity(entity_ids: set[str]) -> list[tuple[str, str, dict]]:
    """
    Every (from_id, to_id, to_node) reachable via a CONFIRMED SAME_AS edge
    from `entity_ids` — see module docstring for why only `confirmed`
    counts as identity. Returns pairs (not just a set) so the caller can
    propagate via_entity/path_confidence/hop provenance from the correct
    source node, not an arbitrary one.
    """
    if not entity_ids:
        return []
    rows = await age_client.execute_cypher(
        "MATCH (a)-[r:SAME_AS]-(b) "
        "WHERE a.entity_id IN $ids AND r.status = 'confirmed' AND r.superseded_by IS NULL "
        "RETURN a, b",
        params={"ids": list(entity_ids)},
        columns=["a", "b"],
    )
    out: list[tuple[str, str, dict]] = []
    for row in rows:
        from_id, to_node = _entity_id_of(row.get("a")), row.get("b")
        to_id = _entity_id_of(to_node)
        if from_id and to_id:
            out.append((from_id, to_id, to_node))
    return out


async def _unconfirmed_same_as_links(entity_ids: set[str]) -> list[dict]:
    """
    Pending SAME_AS edges touching any traversed entity — never followed
    as identity, but surfaced so the answer can carry the caveat (the
    P-006 flagship requirement) instead of silently dropping the signal
    the resolver already flagged.
    """
    if not entity_ids:
        return []
    rows = await age_client.execute_cypher(
        "MATCH (a)-[r:SAME_AS]-(b) "
        "WHERE a.entity_id IN $ids AND r.status = 'pending' AND r.superseded_by IS NULL "
        "RETURN a, r, b",
        params={"ids": list(entity_ids)},
        columns=["a", "r", "b"],
    )
    seen_pairs: set[frozenset] = set()
    out: list[dict] = []
    for row in rows:
        a_id, b_id = _entity_id_of(row.get("a")), _entity_id_of(row.get("b"))
        if not a_id or not b_id:
            continue
        pair = frozenset({a_id, b_id})
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        props = row.get("r", {}).get("properties", {}) or {}
        out.append({
            "entity": _display_name(row.get("a")),
            "candidate": _display_name(row.get("b")),
            "tier": props.get("tier"),
            "confidence": props.get("confidence"),
            "status": "pending",
            # WHY the resolver flagged these two as possibly the same person
            # (e.g. "matched on near-identical name + shared case"). Without it
            # a reviewer sees two names and a number with no stated reason, and
            # the caveat text downstream has nothing to explain itself with —
            # which is the difference between a reviewable lead and an
            # unexplained assertion.
            "basis": props.get("basis"),
        })
    return out


async def _one_hop_neighbors(entity_ids: set[str]) -> list[dict]:
    """One ASSOCIATED_WITH hop out from `entity_ids` — the only relationship-expansion edge (see module docstring)."""
    if not entity_ids:
        return []
    rows = await age_client.execute_cypher(
        f"MATCH (a)-[r:{_HOP_EDGE_TYPE}]-(b) "
        "WHERE a.entity_id IN $ids AND r.superseded_by IS NULL "
        "RETURN a, r, b",
        params={"ids": list(entity_ids)},
        columns=["a", "r", "b"],
    )
    return rows


async def _filter_to_case(entity_ids: set[str], case_id: str) -> set[str]:
    """Keep only entities that BELONG_TO_CASE `case_id` — prevents a within-case traversal wandering into another case at any hop."""
    if not entity_ids:
        return set()
    rows = await scoped_cypher(
        "MATCH (n)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id}) "
        "WHERE n.entity_id IN $ids "
        "RETURN n",
        case_id,
        params={"ids": list(entity_ids)},
        columns=["n"],
    )
    return {eid for eid in (_entity_id_of(row.get("n")) for row in rows) if eid}


async def _fetch_case_conflicts(case_id: str) -> list[dict]:
    """
    Pre-computed CONFLICTS_WITH edges for this case (written by background
    conflict detection, src/graph/conflict_detection.py) plus the source
    chunks for both sides of each conflict, tagged with
    `metadata.conflict_basis` — surfaces the already-computed contradiction
    instead of relying on the generator to re-derive it from raw text alone.
    Independent of seed/target_entity matching: conflicts are case-scoped,
    not entity-scoped, so this runs even when the query's target_entity
    doesn't resolve to a graph node (e.g. a case/FIR number rather than a
    person/vehicle/phone/org).
    """
    rows = await scoped_cypher(
        "MATCH (a:Incident)-[r:CONFLICTS_WITH]->(b:Incident)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id}) "
        "RETURN a, r, b",
        case_id,
        columns=["a", "r", "b"],
    )
    if not rows:
        return []

    entity_ids: set[str] = set()
    basis_by_entity: dict[str, list[str]] = {}
    confidence_by_entity: dict[str, float] = {}
    for row in rows:
        a_id, b_id = _entity_id_of(row.get("a")), _entity_id_of(row.get("b"))
        if not a_id or not b_id:
            continue
        edge_props = row.get("r", {}).get("properties", {}) or {}
        basis = edge_props.get("basis") or "Contradiction detected between these two incidents."
        confidence = float(edge_props.get("confidence", 1.0))
        for eid in (a_id, b_id):
            entity_ids.add(eid)
            basis_by_entity.setdefault(eid, []).append(basis)
            confidence_by_entity[eid] = min(confidence_by_entity.get(eid, 1.0), confidence)

    appears_in_rows = await _fetch_appears_in(entity_ids)
    row_by_chunk: dict[str, str] = {}  # chunk_id -> entity_id
    for row in appears_in_rows:
        entity_id = _entity_id_of(row.get("n"))
        edge_props = row.get("r", {}).get("properties", {}) or {}
        source_chunk_id = edge_props.get("source_chunk_id")
        if entity_id and source_chunk_id:
            row_by_chunk[source_chunk_id] = entity_id

    fetched_chunks = await get_chunks_by_ids(list(dict.fromkeys(row_by_chunk.keys())))
    fetched_by_id = {c["id"]: c for c in fetched_chunks}

    out: list[dict] = []
    for chunk_id, entity_id in row_by_chunk.items():
        base = fetched_by_id.get(chunk_id)
        if base is None:
            continue
        confidence = confidence_by_entity.get(entity_id, 1.0)
        basis_text = "; ".join(dict.fromkeys(basis_by_entity.get(entity_id, [])))
        metadata = {**(base.get("metadata") or {}), "conflict_basis": basis_text}
        out.append({
            **base,
            "metadata": metadata,
            "rrf_score": confidence,
            "hop": 0,
            "graph_confidence": confidence,
            "via_entity": "conflict_detection",
        })
    return out


async def _fetch_appears_in(entity_ids: set[str]) -> list[dict]:
    """Source-document/chunk provenance for a set of entities — never omitted, per architecture Figure 3."""
    if not entity_ids:
        return []
    rows = await age_client.execute_cypher(
        "MATCH (n)-[r:APPEARS_IN]->(d:Document) "
        "WHERE n.entity_id IN $ids AND r.superseded_by IS NULL "
        "RETURN n, r, d",
        params={"ids": list(entity_ids)},
        columns=["n", "r", "d"],
    )
    return rows


def _compounded_confidence(edge_confidences: list[float]) -> float:
    """Simple product across a chain — a 3-hop chain of 0.9-confidence edges compounds to ~0.73, per the spec's own example."""
    result = 1.0
    for c in edge_confidences:
        result *= float(c)
    return result


# Every caller that needs the cross-case gate — retrieve_graph(cross_case=
# True) and retrieve_jurisdiction_cases() below — checks against this SAME
# tuple, imported nowhere else. cross_case_linkage.py's own module docstring
# names the identical three roles for xgraph_tool/xnetwork_tool (design
# §4.3) — this constant is this module's single source of truth for them,
# not a second copy that could drift.
CROSS_CASE_ROLES = ("supervisor", "station-admin", "platform-admin")


async def _enforce_cross_case_role_gate(
    *, user_id: Optional[str], user_role: str, case_id: Optional[str],
    target_entity: Optional[str], query_text: str,
) -> None:
    """
    THE cross-case role gate — the one this module's own docstring, this
    file's `retrieve_graph()`, and SUBAGENT_INTERFACES.md's "do not add a
    third gate" warning all refer to. Raises `PermissionError` (after
    writing an `authorization_violation` audit record) for any role not in
    `CROSS_CASE_ROLES`; returns normally (after writing a
    `graph_traversal_cross_case` audit record) for an authorized caller.

    Milestone B1 (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — jurisdiction graph
    nodes): station/district-scoped traversal ("every case filed at this
    station") is a BROADER enumeration capability than a single cross-case
    link hop, even though it only reads jurisdiction metadata — so it must
    reuse this exact function, not get a looser tier of its own or a
    second, parallel check that could drift out of sync with it. Extracted
    out of `retrieve_graph()`'s own inline body (which used to be the only
    caller) specifically so `retrieve_jurisdiction_cases()` below calls the
    IDENTICAL code path — trace both call sites in review, not just this
    docstring's claim, to confirm there is only one gate.
    """
    if user_role in CROSS_CASE_ROLES:
        # Audit log this high-risk data-exposure capability
        try:
            gateway = await get_gateway()
            await gateway.log_audit_event(
                event_type="graph_traversal_cross_case",
                user_id=user_id,
                case_id=case_id,
                details={"target_entity": target_entity, "query": query_text},
            )
        except Exception as e:
            logger.error("Failed to audit log cross-case traversal: %s", e)
        return

    logger.warning("Unauthorized cross-case graph traversal attempted by %s (user_id: %s)", user_role, user_id)
    try:
        gateway = await get_gateway()
        await gateway.log_audit_event(
            event_type="authorization_violation",
            user_id=user_id,
            case_id=case_id,
            details={"target_entity": target_entity, "query": query_text, "role": user_role},
        )
    except Exception as e:
        logger.error("Failed to audit log unauthorized cross-case traversal: %s", e)
    raise PermissionError("Cross-case graph traversal requires supervisor role or higher.")


async def retrieve_jurisdiction_cases(
    *,
    station_id: Optional[str] = None,
    district_id: Optional[str] = None,
    query_text: str = "",
    user_id: Optional[str] = None,
    user_role: str = "investigator",
    limit: int = 200,
) -> dict:
    """
    Milestone B1 — station/district-scoped case enumeration: "every case
    filed at this station/district", via the Case-[FILED_AT]->PoliceStation
    -[PART_OF]->District structure `src/graph/structured_projection.py`
    writes. Exactly one of `station_id`/`district_id` is expected (both
    given narrows to their intersection; neither given is a caller bug —
    raises ValueError rather than silently enumerating every case in the
    graph, which this function's name promises is jurisdiction-SCOPED).

    ACCESS CONTROL — see this module's own comment on `retrieve_graph()`'s
    cross-case branch and `_enforce_cross_case_role_gate()`'s docstring:
    this calls that SAME function, not a reimplementation. A denial raises
    PermissionError and writes the identical `authorization_violation`
    audit record the cross-case link-hop gate already writes — there is
    exactly one gate in this codebase for "enumerate/traverse across case
    boundaries," and this is a second caller of it, not a second gate.

    Returns {"case_ids": [...], "station_id", "district_id"} — deliberately
    NOT chunk-shaped like `retrieve_graph()`'s return: this is a metadata
    enumeration (which cases exist in this jurisdiction), not an
    entity/evidence traversal, so there is no chunk/hop/confidence
    provenance to report. Wiring this into a harness tool/sub-agent (query-
    scope preclassification) is Milestone E1's job, out of scope here.
    """
    # Role gate first, always — even a malformed call (see the ValueError
    # below) must not tell an unauthorized caller anything before the
    # authorization check runs.
    await _enforce_cross_case_role_gate(
        user_id=user_id, user_role=user_role, case_id=None,
        target_entity=station_id or district_id, query_text=query_text,
    )

    if not station_id and not district_id:
        raise ValueError("retrieve_jurisdiction_cases requires station_id and/or district_id.")

    if station_id and district_id:
        cypher = (
            "MATCH (c:Case)-[:FILED_AT]->(s:PoliceStation)-[:PART_OF]->(d:District) "
            "WHERE s.station_id = $station_id AND d.district_id = $district_id "
            "RETURN DISTINCT c.case_id AS case_id LIMIT $limit"
        )
        params = {"station_id": station_id, "district_id": district_id, "limit": limit}
    elif station_id:
        cypher = (
            "MATCH (c:Case)-[:FILED_AT]->(s:PoliceStation) "
            "WHERE s.station_id = $station_id "
            "RETURN DISTINCT c.case_id AS case_id LIMIT $limit"
        )
        params = {"station_id": station_id, "limit": limit}
    else:
        cypher = (
            "MATCH (c:Case)-[:FILED_AT]->(:PoliceStation)-[:PART_OF]->(d:District) "
            "WHERE d.district_id = $district_id "
            "RETURN DISTINCT c.case_id AS case_id LIMIT $limit"
        )
        params = {"district_id": district_id, "limit": limit}

    rows = await age_client.execute_cypher(cypher, params=params, columns=["case_id"])
    return {
        "case_ids": [r["case_id"] for r in rows if r.get("case_id")],
        "station_id": station_id,
        "district_id": district_id,
    }


async def retrieve_graph(
    query_text: str,
    target_entity: Optional[str],
    case_id: Optional[str],
    cross_case: bool = False,
    max_hops: int = DEFAULT_HOPS,
    user_id: Optional[str] = None,
    user_role: str = "investigator",
) -> dict:
    """
    Traverse evidence_graph for `target_entity` (seeded within `case_id`,
    or cross-case if `cross_case=True`), capped at `max_hops` (2-3)
    ASSOCIATED_WITH hops, and return chunk-shaped dicts with per-chunk
    hop/confidence provenance.

    Returns:
        {
            "chunks": [{"id", "text", "metadata", "rrf_score",
                        "hop", "graph_confidence", "via_entity"}, ...],
            "hop_count": int,             # deepest hop actually returned
            "compounded_confidence": float,  # weakest chain among results
            "seed_entities": [{"entity_id", "type", "name"}, ...],
            "unconfirmed_links": [...],   # pending SAME_AS caveats
        }
    """
    empty_result = {
        "chunks": [], "hop_count": 0, "compounded_confidence": 1.0,
        "seed_entities": [], "unconfirmed_links": [],
    }

    if cross_case:
        # _enforce_cross_case_role_gate() raises PermissionError (after
        # writing its own authorization_violation audit record) on denial —
        # execution never reaches the RLS-arming below for an unauthorized
        # caller. See that function's own docstring for why this is the ONE
        # place the role check lives, reused (not reimplemented) by every
        # cross-case-shaped caller, jurisdiction-scoped traversal included.
        await _enforce_cross_case_role_gate(
            user_id=user_id, user_role=user_role, case_id=case_id,
            target_entity=target_entity, query_text=query_text,
        )

        # Phase 2: arm the Postgres RLS cross-case bypass ONLY now that
        # the role check above has actually passed — this is the fix
        # for issues.md's High "cross-case RLS bypass flag is armed
        # before its own role check" finding. It used to be armed by
        # the orchestrator the instant the router classified the query
        # as cross-case, before this function's role check ever ran,
        # and was never reset on a PermissionError above. Arming it
        # here instead of there means an unauthorized caller never
        # arms it at all — there's no window to close, because it's
        # never opened for them in the first place.
        # Also self-arm rls_active here (security-review addendum):
        # this used to rely entirely on the caller (chat_endpoint's
        # set_case_scope()) having already armed it, a convention
        # enforced only by docstring — a future second caller of
        # retrieve_graph() that forgets to arm RLS upstream would
        # otherwise run with app.rls_active never set, which migration
        # 010's policies treat as "RLS fully inactive" (fail-open).
        current_rls_active.set(True)
        current_cross_case.set(True)

    if not cross_case and not case_id:
        # A within-case graph query with no active case has nothing to
        # scope to — fail closed rather than search unscoped (would leak
        # every case's entities into a query that never asked for that).
        logger.warning("retrieve_graph called with cross_case=False and no case_id — returning empty.")
        return empty_result

    max_hops = max(1, min(max_hops, MAX_HOPS))

    conflict_chunks: list[dict] = []
    if not cross_case and case_id:
        conflict_chunks = await _fetch_case_conflicts(case_id)

    candidates = _seed_candidates(target_entity, query_text)
    if candidates:
        seed_nodes = await _find_seed_nodes(candidates, case_id, cross_case)
    elif not cross_case and case_id:
        # No literal name/CNIC/phone/plate anywhere in the query — this is a
        # case-wide enumeration question ("how many accused are involved in
        # CASE-009", "who are the witnesses in FIR-2026-ARMS-001"), not a
        # single-entity lookup. Seed from every entity in the case instead
        # of returning empty just because nothing was named.
        seed_nodes = await _find_all_case_entities(case_id)
    elif cross_case:
        # Same gap, cross-case flavor, but two different questions share
        # this "no entity named" shape: "has ANY phone number recurred
        # across cases" (the recurrence itself is the answer) vs. "list
        # every person mentioned across the cases" (enumeration — every
        # instance, not just recurring ones). _ENUMERATION_KEYWORDS picks
        # which one this is; min_cases=1 with a cap is the enumeration
        # case, the untouched default (min_cases=2, no cap) is recurrence.
        lowered_query = (query_text or "").lower()
        if any(kw in lowered_query for kw in _ENUMERATION_KEYWORDS):
            seed_nodes = await _find_recurring_entities_for_query(query_text, min_cases=1, limit=50)
        else:
            seed_nodes = await _find_recurring_entities_for_query(query_text)
    else:
        seed_nodes = []
    if not seed_nodes:
        if not conflict_chunks:
            return empty_result
        # No entity/relationship seed matched (e.g. target_entity was a case
        # or FIR number, not a person/vehicle/phone/org), but this case has
        # pre-computed conflicts — surface those rather than an empty result.
        return {
            "chunks": conflict_chunks,
            "hop_count": 0,
            "compounded_confidence": min((c["graph_confidence"] for c in conflict_chunks), default=1.0),
            "seed_entities": [],
            "unconfirmed_links": [],
        }

    seed_entities = [
        {"entity_id": _entity_id_of(n), "type": (n or {}).get("label"), "name": _display_name(n)}
        for n in seed_nodes
    ]

    display_name: dict[str, str] = {e["entity_id"]: e["name"] for e in seed_entities if e["entity_id"]}
    via_entity: dict[str, str] = {eid: eid for eid in display_name}  # entity_id -> seed name that led to it
    path_confidence: dict[str, float] = {eid: 1.0 for eid in display_name}
    hop_of: dict[str, int] = {eid: 0 for eid in display_name}

    frontier: set[str] = set(display_name.keys())
    visited: set[str] = set(frontier)

    for hop in range(1, max_hops + 1):
        if not frontier:
            break

        # Confirmed-SAME_AS identity fold happens every hop layer (not
        # just once) so a chain that reaches a new mention node also
        # picks up ITS confirmed canonical identity before the next hop.
        # Confirmed identity is a settled fact, not a probabilistic edge,
        # so it carries the source's own path_confidence/hop forward
        # unchanged rather than compounding it further.
        identity_pairs = await _expand_confirmed_identity(frontier)
        new_identity: set[str] = set()
        for from_id, to_id, to_node in identity_pairs:
            if to_id in visited or to_id in new_identity:
                continue
            new_identity.add(to_id)
            display_name[to_id] = _display_name(to_node)
            via_entity[to_id] = via_entity.get(from_id, from_id)
            path_confidence[to_id] = path_confidence.get(from_id, 1.0)
            hop_of[to_id] = hop_of.get(from_id, hop)
        visited |= new_identity
        frontier |= new_identity

        neighbor_rows = await _one_hop_neighbors(frontier)
        next_frontier: set[str] = set()
        for row in neighbor_rows:
            a_id, b_id = _entity_id_of(row.get("a")), _entity_id_of(row.get("b"))
            edge_conf = float((row.get("r", {}).get("properties", {}) or {}).get("confidence", 1.0))
            for from_id, to_id, to_node in ((a_id, b_id, row.get("b")), (b_id, a_id, row.get("a"))):
                if not from_id or not to_id or from_id not in frontier or to_id in visited:
                    continue
                next_frontier.add(to_id)
                display_name[to_id] = _display_name(to_node)
                via_entity[to_id] = via_entity.get(from_id, from_id)
                path_confidence[to_id] = path_confidence.get(from_id, 1.0) * edge_conf
                hop_of[to_id] = hop

        if not cross_case and case_id:
            next_frontier = await _filter_to_case(next_frontier, case_id)

        visited |= next_frontier
        frontier = next_frontier

    unconfirmed_links = await _unconfirmed_same_as_links(visited) if cross_case else []

    appears_in_rows = await _fetch_appears_in(visited)
    chunk_ids: list[str] = []
    row_by_chunk: dict[str, dict] = {}
    for row in appears_in_rows:
        entity_id = _entity_id_of(row.get("n"))
        edge_props = row.get("r", {}).get("properties", {}) or {}
        source_chunk_id = edge_props.get("source_chunk_id")
        if not entity_id or not source_chunk_id:
            continue
        chunk_ids.append(source_chunk_id)
        row_by_chunk[source_chunk_id] = {
            "entity_id": entity_id,
            "mention_confidence": float(edge_props.get("confidence", 1.0)),
        }

    fetched_chunks = await get_chunks_by_ids(list(dict.fromkeys(chunk_ids)))
    fetched_by_id = {c["id"]: c for c in fetched_chunks}

    chunks: list[dict] = []
    max_hop_returned = 0
    weakest_confidence = 1.0
    for chunk_id, info in row_by_chunk.items():
        base = fetched_by_id.get(chunk_id)
        if base is None:
            # "Never hand graph results to the generator without their
            # supporting chunk" — a hop whose document was never ingested
            # (or since deleted from Chroma) is dropped, not passed
            # through text-less.
            continue
        entity_id = info["entity_id"]
        hop_n = hop_of.get(entity_id, 0)
        chain_confidence = path_confidence.get(entity_id, 1.0) * info["mention_confidence"]
        chunks.append({
            **base,
            "rrf_score": chain_confidence,
            "hop": hop_n,
            "graph_confidence": chain_confidence,
            "via_entity": display_name.get(via_entity.get(entity_id, entity_id), entity_id),
        })
        max_hop_returned = max(max_hop_returned, hop_n)
        weakest_confidence = min(weakest_confidence, chain_confidence)

    if conflict_chunks:
        chunks_by_id = {c["id"]: c for c in chunks}
        for cc in conflict_chunks:
            existing = chunks_by_id.get(cc["id"])
            if existing is not None:
                existing["metadata"] = {**(existing.get("metadata") or {}), "conflict_basis": cc["metadata"]["conflict_basis"]}
            else:
                chunks.append(cc)
                weakest_confidence = min(weakest_confidence, cc["graph_confidence"])

    return {
        "chunks": chunks,
        "hop_count": max_hop_returned,
        "compounded_confidence": weakest_confidence if chunks else 1.0,
        "seed_entities": seed_entities,
        "unconfirmed_links": unconfirmed_links,
    }
