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
#   - status='pending'    -> NOT the same identity yet. Surfaced
#     separately (see `unconfirmed_links` in `retrieve_graph`'s return)
#     so a cross-case answer can carry the caveat ("possibly the same
#     person, unconfirmed") instead of either silently ignoring it
#     (under-connecting — misses a repeat offender the resolver correctly
#     flagged) or silently treating it as fact (over-connecting — the
#     "single biggest way a knowledge graph fails quietly," per
#     docs/graph_schema.md).
#
#     [Milestone D2 — GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md, behind
#     src.config.FEATURE_HEDGED_PENDING_TRAVERSAL, cross-case only]
#     Pending identity is ALSO traversed, not merely surfaced as a
#     caveat — see `_expand_pending_identity()` — but every entity/chunk
#     reached only through a pending link is forced to a confidence
#     capped below verifier.py's hedge threshold (`_PENDING_HEDGE_CAP`)
#     and tagged `same_as_status="pending"` in its chunk metadata, so
#     verifier._check_hedging()'s existing mechanism (extended, not
#     duplicated, for this exact tag) refuses to deliver the answer
#     unhedged. This is D2's "disclosed hedge, not silent downweighting"
#     choice (see the plan's own §D2 reasoning) — recall goes up without
#     ever asserting an unconfirmed identity as fact. Flag OFF (default)
#     reproduces the exact prior behavior below: pending is excluded from
#     traversal, surfaced only via `unconfirmed_links`.
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
import unicodedata
from contextvars import ContextVar
from typing import Optional

from src import config
from src.extraction.structured_fields import _CNIC_RE, _PHONE_RE, _PLATE_RE
from src.graph import age_client
from src.graph.case_scope import scoped_cypher
# Cross-lingual graph name matching: the same Roman-Urdu <-> Urdu-script
# consonant-skeleton function entity_resolution.py's ingestion-time merge
# decisions already use (see its own comment block for the full
# rationale) — reused here, not reimplemented, so a Person/Officer/
# Organization node whose canonical_name ended up in one script is still
# findable as a graph-traversal seed by a query in the other script.
from src.graph.entity_resolution import _consonant_skeleton
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
    # "phone" added here (bug fix): src/graph/structured_projection.py's
    # person/officer mention writes put a phone number directly on the
    # Person node's own `phone` property — confirmed live, 98 real Person
    # nodes carry one — while a standalone PhoneNumber node (below) is
    # only ever written by the legacy, rarely-exercised admin-single-
    # file-upload NER path (1 real node in the whole graph). Without this,
    # a phone-number-anchored query could never find a seed for virtually
    # the entire real corpus.
    "Person": (("canonical_name", "cnic", "phone"), "canonical_name"),
    "Vehicle": (("plate",), "plate"),
    "PhoneNumber": (("canonical_name", "phone"), "canonical_name"),
    "Organization": (("canonical_name",), "canonical_name"),
    # Bug fix: Officer was missing from this dict entirely, despite being
    # a real, actively-written graph label (TYPE_TO_LABEL["officer"],
    # Milestone B2, keyed on belt_no) — every real FIR writes real
    # Officer nodes. An officer could never be found as a traversal seed
    # by name, belt number, or phone, on any route. Properties match what
    # a real Officer node actually carries (confirmed live this session).
    "Officer": (("canonical_name", "belt_no", "phone"), "canonical_name"),
}

# Human-readable label for a matched _SEED_LABELS id_prop, used only to
# phrase _synthetic_evidence_chunk()'s "matched via ..." clause (see
# _matched_seed_property()). Deliberately excludes "canonical_name": a
# name match needs no extra clause since the matched value already IS
# the name already shown in the sentence (surface_text) — restating it
# would be redundant, not informative, unlike a phone/CNIC/plate/belt
# number, which never otherwise appears anywhere in the synthetic text.
_MATCHED_PROPERTY_LABELS: dict[str, str] = {
    "cnic": "CNIC",
    "phone": "phone number",
    "plate": "plate number",
    "belt_no": "belt number",
}

# [Module 2 — findings.md] Properties worth surfacing in the synthetic-
# evidence sentence even when they weren't the property that matched the
# seed search — e.g. a name-seeded Officer query about their belt number
# otherwise never sees the belt number anywhere in the cited text, so the
# LLM correctly refuses to confirm a fact that's true in the graph but
# absent from what it's shown. Reuses _MATCHED_PROPERTY_LABELS for
# phrasing (single source of truth). See _synthetic_evidence_chunk().
_NOTABLE_PROPERTIES: dict[str, tuple[str, ...]] = {
    "Officer": ("belt_no", "phone"),
    "Person": ("cnic", "phone"),
    "Vehicle": ("plate",),
}


def _matched_seed_property(
    properties: dict, id_props: tuple[str, ...], candidate: str,
) -> Optional[tuple[str, str]]:
    """
    [Bug fix] Which of a seeded node's own `id_props` actually contains
    `candidate` (the same CONTAINS/lower() comparison _find_seed_nodes()'s
    Cypher WHERE clause already applied server-side to confirm a match
    exists) — and its value. First `id_props` entry that matches wins,
    same order _SEED_LABELS declares them in.

    Exists because a graph-traversal seed used to carry NO record of
    which property justified matching it at all: _synthetic_evidence_chunk()
    could say "طارق appears in fir_structured record fir-401-26" but never
    "...whose phone number is 0305-4000005" — even when the seed was found
    BY that exact phone number. A question specifically about a phone
    number then got evidence that never once mentioned a phone number,
    and the LLM (correctly, from what it could see) refused to confirm
    the claim. Confirmed live this session on the standing 0305-4000005
    cross-case repro.

    Returns None if nothing matches (should not happen for a node
    _find_seed_nodes() itself just returned — defensive, not expected).
    """
    candidate_lower = candidate.lower()
    for prop in id_props:
        value = properties.get(prop)
        if value and candidate_lower in str(value).lower():
            return prop, str(value)
    return None


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


# Interrogative/relative particles a router's LLM-based target_entity
# extraction can mistakenly glue onto a name when the name is immediately
# followed by one in the source sentence — confirmed live: "ذیشان کن
# کیسز سے منسلک ہے؟" ("which cases is Zeeshan connected to?") extracted
# target_entity="ذیشان کن" instead of "ذیشان", and that extra "کن"
# ("which") alone was enough to make seed lookup match zero nodes instead
# of the 13 real ones a clean "ذیشان" correctly finds. A real Person/
# Officer/Organization name is never a multi-word phrase ending in one of
# these, so stripping the last token when it's one of these is a safe,
# narrow recovery for this specific extraction-boundary error — not a
# general stopword filter, and not a substitute for improving the
# router's own extraction prompt (still worth doing, see router.py), just
# a deterministic backstop that doesn't depend on the LLM getting it right.
_TRAILING_ENTITY_PARTICLES = (
    "کن", "کونسا", "کونسے", "کونسی", "کس", "کیا",
    "which", "what",
)


def _strip_trailing_particle(text: str) -> Optional[str]:
    """
    If `text`'s last whitespace-separated token is a known trailing
    interrogative/relative particle, return the text with that token
    removed. None if there's nothing to strip (including single-token
    text — a bare particle with no name preceding it isn't a name at all,
    so there's nothing to fall back to).
    """
    tokens = text.strip().split()
    if len(tokens) < 2:
        return None
    if tokens[-1].lower() in _TRAILING_ENTITY_PARTICLES:
        stripped = " ".join(tokens[:-1]).strip()
        return stripped or None
    return None


def _seed_candidates(target_entity: Optional[str], query_text: str) -> list[str]:
    """
    Build the list of literal strings to look up as graph seed nodes.
    `target_entity` (the router's verbatim extraction) is always tried
    first, plus a trailing-particle-stripped variant when applicable (see
    _strip_trailing_particle's own docstring) — both are tried, not one
    in place of the other, so a correct extraction is never second-
    guessed and a corrupted one still has a real shot at matching.
    Identifier regexes (CNIC/phone/plate — reused from
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
    if target_entity:
        _add(_strip_trailing_particle(target_entity))
    for raw in (target_entity or "", query_text or ""):
        norm = normalize_urdu(raw)
        for regex in (_CNIC_RE, _PHONE_RE, _PLATE_RE):
            for m in regex.finditer(norm):
                _add(m.group(0))

    return candidates


async def _find_seed_nodes(
    candidates: list[str], case_id: Optional[str], cross_case: bool,
    jurisdiction_case_ids: Optional[list[str]] = None,
) -> list[dict]:
    """
    Resolve candidate strings to graph nodes. Within-case: seed search is
    scoped to this case's own BELONGS_TO_CASE edge (single filtered hop,
    per docs/graph_schema.md). Cross-case: deliberately unscoped — the
    case filter is ABSENT BY DESIGN here (not bypassed by a bug), gated
    only by the caller passing `cross_case=True`, so Phase 7 can attach a
    permission to this exact branch without restructuring it.

    [Milestone E1] `jurisdiction_case_ids`, when given (only meaningful
    for the cross_case branch — orchestrator.py only ever passes it there,
    already narrowed by `resolve_jurisdiction_case_ids()`'s own role gate),
    additionally restricts the cross-case MATCH to that case set — cutting
    the candidate set the traversal considers before any hop expansion
    runs, per E1's own "cut the candidate set up front" goal. `None` (the
    default, and always the case for the within-case branch) leaves the
    query exactly as before this milestone.

    [Bug fix, 2026-08-27 route sweep, BUG-3/4] Every branch below now
    excludes a merged donor node (`n.merged_into IS NULL`) and, where a
    BELONGS_TO_CASE edge is matched, a superseded one
    (`b.superseded_by IS NULL`) — the same filter this file already
    applies to SAME_AS/APPEARS_IN/ASSIGNED_TO matches elsewhere (see
    _expand_confirmed_identity, _fetch_appears_in, _fetch_officer_roles),
    just never extended to seed lookup. Confirmed live, on the exact
    component scripts/merge_confirmed_duplicate_persons.py physically
    merged: without this filter, one case's seed lookup for a single
    canonical name returned 146 matches (141 already-merged donor nodes
    plus superseded edges) instead of the 1 real survivor, diluting the
    seed set enough that the traversal's real ASSOCIATED_WITH relationship
    never made it into the final synthetic evidence — the evaluator was
    correctly rejecting what it was handed, not misjudging good evidence.
    A merge landing here (this file) as a genuine no-op for every OTHER
    consumer already routes through build_canonical_map()
    (community_detection.py) — this module's own docstring's "confirmed
    SAME_AS identity" rule already covers this data at the SEMANTIC level;
    this fix is the physical-merge counterpart of that same rule so a
    node the merge already resolved doesn't get treated as a live,
    independent seed candidate again.
    """
    if not candidates:
        return []

    seeds: list[dict] = []
    seen_ids: set[str] = set()
    for label, (id_props, _display) in _SEED_LABELS.items():
        for cand in candidates:
            where_parts = [f"toLower(n.{prop}) CONTAINS toLower($cand)" for prop in id_props]
            params: dict = {"cand": cand}

            # Cross-lingual name matching (only for labels that carry a
            # name — canonical_name is in id_props): a raw CONTAINS above
            # is script-blind, so a node whose canonical_name ended up in
            # Urdu script is invisible to an English-worded query and vice
            # versa. name_skeleton is precomputed at write time
            # (entity_resolution.resolve_and_write) via the same
            # consonant-skeleton function; matched here on equality, not
            # CONTAINS, since the skeleton is a coarse phonetic reduction
            # and a substring match on it would be too permissive.
            if "canonical_name" in id_props:
                cand_skeleton = _consonant_skeleton(cand)
                if cand_skeleton:
                    where_parts.append("n.name_skeleton = $cand_skeleton")
                    params["cand_skeleton"] = cand_skeleton

            where_clause = " OR ".join(where_parts)
            try:
                if cross_case:
                    if jurisdiction_case_ids is not None:
                        cypher = f"""
                            MATCH (n:{label})-[b:BELONGS_TO_CASE]->(c:Case)
                            WHERE ({where_clause}) AND c.case_id IN $case_ids
                              AND n.merged_into IS NULL AND b.superseded_by IS NULL
                            RETURN n
                        """
                        rows = await age_client.execute_cypher(
                            cypher, params={**params, "case_ids": jurisdiction_case_ids}, columns=["n"],
                        )
                    else:
                        cypher = f"""
                            MATCH (n:{label})
                            WHERE ({where_clause}) AND n.merged_into IS NULL
                            RETURN n
                        """
                        rows = await age_client.execute_cypher(cypher, params=params, columns=["n"])
                else:
                    # Case-scoped: routed through case_scope.scoped_cypher()
                    # so a future edit can't silently drop the case filter.
                    cypher = f"""
                        MATCH (n:{label})-[b:BELONGS_TO_CASE]->(c:Case {{case_id: $case_id}})
                        WHERE ({where_clause}) AND n.merged_into IS NULL AND b.superseded_by IS NULL
                        RETURN n
                    """
                    rows = await scoped_cypher(cypher, case_id, params=params, columns=["n"])
            except Exception as exc:
                logger.error("Graph seed lookup failed for %r (%s): %s", cand, label, exc)
                continue

            for row in rows:
                node = row.get("n")
                entity_id = _entity_id_of(node)
                if entity_id and entity_id not in seen_ids:
                    seen_ids.add(entity_id)
                    # [Bug fix] Record which id_prop/value actually matched
                    # `cand` — carried as an extra top-level key alongside
                    # this AGE-shaped node's own id/label/properties, never
                    # written into `properties` itself (that dict mirrors
                    # what's actually stored on the graph node; this is
                    # provenance about THIS seed lookup, not a node
                    # property). See _matched_seed_property()'s docstring.
                    matched = _matched_seed_property((node or {}).get("properties", {}) or {}, id_props, cand)
                    if matched is not None:
                        prop, value = matched
                        node = {**(node or {}), "_matched_property": {"property": prop, "value": value}}
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

    [Bug fix, 2026-08-27 route sweep, BUG-3/4] Same donor/superseded
    exclusion as `_find_seed_nodes` — see that function's own docstring
    for why. A case-wide enumeration is exactly as vulnerable to a merge's
    donor nodes inflating the seed set as a named-entity lookup is.
    """
    seeds: list[dict] = []
    seen_ids: set[str] = set()
    for label in _SEED_LABELS:
        try:
            rows = await scoped_cypher(
                f"MATCH (n:{label})-[b:BELONGS_TO_CASE]->(c:Case {{case_id: $case_id}}) "
                f"WHERE n.merged_into IS NULL AND b.superseded_by IS NULL RETURN n",
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
    query_text: str, min_cases: int = 2, limit: Optional[int] = None,
    jurisdiction_case_ids: Optional[list[str]] = None,
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

    [Milestone E1] `jurisdiction_case_ids`, when given, restricts the
    `BELONGS_TO_CASE` match to that case set before the recurrence count
    is even computed — the same "cut the candidate set up front" goal as
    `_find_seed_nodes`'s own `jurisdiction_case_ids` handling above.
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
            if jurisdiction_case_ids is not None:
                rows = await age_client.execute_cypher(
                    f"MATCH (n:{label})-[b:BELONGS_TO_CASE]->(c:Case) "
                    f"WHERE c.case_id IN $case_ids AND n.merged_into IS NULL AND b.superseded_by IS NULL "
                    f"RETURN n, c",
                    params={"case_ids": jurisdiction_case_ids}, columns=["n", "c"],
                )
            else:
                rows = await age_client.execute_cypher(
                    f"MATCH (n:{label})-[b:BELONGS_TO_CASE]->(c:Case) "
                    f"WHERE n.merged_into IS NULL AND b.superseded_by IS NULL "
                    f"RETURN n, c",
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


async def _both_directions(
    edge_pattern: str, where_tail: str, returns: str,
    entity_ids: set[str], columns: list[str],
) -> list[dict]:
    """
    [PERF] Run an edge expansion as TWO DIRECTED queries (outgoing, then
    incoming) and concatenate, instead of one undirected `(a)-[r]-(b)`.

    WHY, measured — this is the single largest performance defect found in
    the cross-case path, not a micro-optimization. Apache AGE compiles an
    undirected match into a Cartesian product joined by
    `(r.start_id = a.id AND r.end_id = b.id) OR (r.end_id = a.id AND
    r.start_id = b.id)`. That OR-of-AND-pairs is not satisfiable by any
    index, so the planner seq-scans EVERY vertex of EVERY label as a
    candidate for an unlabeled `b` and filters the cross product.
    EXPLAIN ANALYZE on the real graph, for ONE seed id returning ZERO
    rows: `Rows Removed by Join Filter: 11,361,240`, 12.6s.

    Measured on the live corpus (see the same-session profiling run):
      - one undirected pending-SAME_AS expansion over a real frontier:
        450s, and `retrieve_graph()` as a whole exceeded 600s with ZERO
        LLM calls involved — 599.8s of it inside 10 Cypher queries.
      - the same expansion, split into two directed queries: ~50ms.

    Deliberately NOT also label-constraining the endpoints: benchmarked
    at 50ms (unlabeled) vs 48ms (labeled `:Person` both ends), i.e. the
    DIRECTION is the entire fix, while adding a label would silently drop
    any SAME_AS/ASSOCIATED_WITH edge between non-Person entity types.
    Performance must not be bought with a semantic change.

    EQUIVALENCE, proven not assumed: verified against the live graph on
    real seeds returning 189 actual pairs — the undirected query and this
    two-directed-query union returned IDENTICAL (a_id, b_id) sets. The
    row shape is also unchanged: `a` is always the seed-side node and `b`
    the neighbour-side one in BOTH directions, exactly as the undirected
    form bound them, so every caller's (a -> b) provenance reading still
    holds with no call-site change. A pair whose two endpoints are BOTH
    in `entity_ids` still yields two rows (one per orientation), matching
    the undirected behaviour callers already dedupe against.
    """
    if not entity_ids:
        return []
    ids = list(entity_ids)
    outgoing = await age_client.execute_cypher(
        f"MATCH (a)-{edge_pattern}->(b) WHERE a.entity_id IN $ids {where_tail} RETURN {returns}",
        params={"ids": ids}, columns=columns,
    )
    incoming = await age_client.execute_cypher(
        f"MATCH (a)<-{edge_pattern}-(b) WHERE a.entity_id IN $ids {where_tail} RETURN {returns}",
        params={"ids": ids}, columns=columns,
    )
    return outgoing + incoming


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
    rows = await _both_directions(
        "[r:SAME_AS]",
        "AND r.status = 'confirmed' AND r.superseded_by IS NULL",
        "a, b",
        entity_ids,
        ["a", "b"],
    )
    out: list[tuple[str, str, dict]] = []
    for row in rows:
        from_id, to_node = _entity_id_of(row.get("a")), row.get("b")
        to_id = _entity_id_of(to_node)
        if from_id and to_id:
            out.append((from_id, to_id, to_node))
    return out


# Milestone D2: strictly below verifier.py's own hedge threshold (0.85,
# _check_hedging's `conf < 0.85` test) — anything reached only through a
# pending identity link is GUARANTEED to require a disclosed hedge, by
# construction, not by hoping the compounded confidence happens to land
# low enough. Kept as its own named constant here (not imported from
# verifier.py, which doesn't export its 0.85 as a symbol) with a comment
# on both sides pointing at the other — see verifier._check_hedging's own
# amended docstring.
_PENDING_HEDGE_CAP = 0.80


async def _expand_pending_identity(entity_ids: set[str]) -> list[tuple[str, str, dict, float, str]]:
    """
    Every (from_id, to_id, to_node, edge_confidence, basis) reachable via
    a PENDING SAME_AS edge from `entity_ids` — the Milestone D2 traversal
    path, mirroring `_expand_confirmed_identity()`'s shape exactly except
    for the status filter and the extra (confidence, basis) the caller
    needs to build a disclosed hedge. Only ever called when
    `config.FEATURE_HEDGED_PENDING_TRAVERSAL` is on (checked by the
    caller, not here, so this function stays a pure graph read).
    """
    if not entity_ids:
        return []
    # [PERF] See _both_directions() — this exact query was the 450s one.
    rows = await _both_directions(
        "[r:SAME_AS]",
        "AND r.status = 'pending' AND r.superseded_by IS NULL",
        "a, r, b",
        entity_ids,
        ["a", "r", "b"],
    )
    out: list[tuple[str, str, dict, float, str]] = []
    for row in rows:
        from_id, to_node = _entity_id_of(row.get("a")), row.get("b")
        to_id = _entity_id_of(to_node)
        if not from_id or not to_id:
            continue
        props = row.get("r", {}).get("properties", {}) or {}
        edge_confidence = min(float(props.get("confidence", 1.0)), _PENDING_HEDGE_CAP)
        out.append((from_id, to_id, to_node, edge_confidence, props.get("basis") or ""))
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
    # [PERF] See _both_directions(). The existing frozenset dedupe below
    # already collapses an (a,b)/(b,a) pair seen from either orientation,
    # so splitting the direction changes nothing about this output.
    rows = await _both_directions(
        "[r:SAME_AS]",
        "AND r.status = 'pending' AND r.superseded_by IS NULL",
        "a, r, b",
        entity_ids,
        ["a", "r", "b"],
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
    # [PERF] See _both_directions(). This one is doubly safe to split:
    # retrieve_graph()'s consumer of these rows already walks BOTH
    # orientations of every row explicitly (`((a_id, b_id, row["b"]),
    # (b_id, a_id, row["a"]))`), so it never depended on a single row
    # carrying a particular direction in the first place.
    return await _both_directions(
        f"[r:{_HOP_EDGE_TYPE}]",
        "AND r.superseded_by IS NULL",
        "a, r, b",
        entity_ids,
        ["a", "r", "b"],
    )


async def _filter_to_case(entity_ids: set[str], case_id: str) -> set[str]:
    """
    Keep only entities that BELONG_TO_CASE `case_id` — prevents a
    within-case traversal wandering into another case at any hop.

    [Bug fix, 2026-08-27 route sweep, BUG-3/4] Same donor/superseded
    exclusion as `_find_seed_nodes` — defense in depth: a merged donor
    node reached mid-traversal (its own entity_id still resolvable, just
    never meant to act as a live node again) must not re-enter the
    frontier here either, even though the fix at the seed-lookup sites
    should already keep one from ever being a starting point.
    """
    if not entity_ids:
        return set()
    rows = await scoped_cypher(
        "MATCH (n)-[b:BELONGS_TO_CASE]->(c:Case {case_id: $case_id}) "
        "WHERE n.entity_id IN $ids AND n.merged_into IS NULL AND b.superseded_by IS NULL "
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
    synthetic_by_id: dict[str, dict] = {}
    for row in appears_in_rows:
        entity_id = _entity_id_of(row.get("n"))
        edge_props = row.get("r", {}).get("properties", {}) or {}
        source_chunk_id = edge_props.get("source_chunk_id")
        if not entity_id:
            continue
        if source_chunk_id:
            row_by_chunk[source_chunk_id] = entity_id
        else:
            synthetic = _synthetic_evidence_chunk(row)
            if synthetic is not None:
                synthetic_by_id[synthetic["id"]] = synthetic
                row_by_chunk[synthetic["id"]] = entity_id

    fetched_chunks = await get_chunks_by_ids(
        [cid for cid in dict.fromkeys(row_by_chunk.keys()) if cid not in synthetic_by_id]
    )
    fetched_by_id = {c["id"]: c for c in fetched_chunks}
    fetched_by_id.update(synthetic_by_id)

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
    """
    Source-document/chunk provenance for a set of entities — never
    omitted, per architecture Figure 3.

    `case_id` (OPTIONAL MATCH so a global/case-less document still returns
    a row) rides along in the same query rather than a second per-entity
    round trip — used by _synthetic_evidence_chunk() below to give a
    structured-extraction chunk a real case_id when one exists (a
    criminal_db record genuinely has none — that silo is
    CNIC-cross-referenced, not case-anchored — so `None` there is correct,
    not a gap).

    [Bug fix] This joins BELONGS_TO_CASE off `d` (the specific Document
    this APPEARS_IN edge points to), NOT off `n` (the entity). A Document
    belongs to exactly one case (structured_projection.py's single
    BELONGS_TO_CASE write per document), so this join can never fan out.
    Joining off `n` instead — the entity — used to be wrong for any
    entity genuinely recurring across cases (the very shape XGRAPH/
    XNETWORK exist to surface): such an entity carries N BELONGS_TO_CASE
    edges, so the OPTIONAL MATCH produced N duplicate (n, r, d) rows per
    APPEARS_IN edge, one per matching case. Confirmed live: a real person
    appearing in fir-202-26 and fir-401-26 (via a shared phone number)
    had one of their two synthetic evidence chunks — the one built from
    the fir-202-26 APPEARS_IN edge — silently stamped with case_id
    "fir-401-26" in its citation metadata, because the last duplicate row
    written into the caller's chunk-id-keyed dict happened to carry the
    other case. Every cross-case citation for a multi-case entity was one
    dict-overwrite away from naming the wrong case.

    [findings.md Module 8 follow-up] Also attaches `officer_roles` (a
    list, possibly empty) — an Officer's current, non-superseded
    ASSIGNED_TO role(s) FOR THIS ROW'S OWN CASE (investigating/recording,
    structured_projection.py's `_write_investigating_officers()`/
    `_write_recording_officer()`). Deliberately fetched via a SEPARATE
    query and merged in Python (`_fetch_officer_roles()` below), NOT
    folded into this function's own MATCH as another OPTIONAL MATCH — that
    was tried first and reverted after live-verification caught the exact
    fan-out bug the "[Bug fix]" note right above already warns about, one
    hop over: `fir-401-26`'s real data has ONE officer holding BOTH the
    investigating AND recording role simultaneously (two live,
    non-superseded ASSIGNED_TO edges to the same case) — an OPTIONAL MATCH
    on that edge fans a single APPEARS_IN row into two duplicate (n, r, d)
    rows differing only in role, and the caller's synthetic-id-keyed dict
    (both rows produce the identical id, since entity_id/source_doc_id are
    unchanged) silently overwrites one role with the other, non-
    deterministically, in whatever order AGE happens to return them —
    live-confirmed by running the exact same query twice and getting
    "recording" once and "investigating" once. Aggregating in Python
    instead — one list per (entity_id, case_id), collect()-free — sidesteps
    that fan-out entirely rather than trying to get a Cypher-side
    aggregate right on the first attempt against a query shape untested
    elsewhere in this codebase.

    Returns an empty list for `officer_roles` for every non-Officer entity
    (no ASSIGNED_TO edge exists to match) and for a case-less document —
    both correctly absent, not a gap. Live-confirmed the actual reason
    this field exists at all: Local Search's own live-verification found
    the right Officer via semantic match, but the relevance evaluator then
    rejected the resulting evidence text verbatim because it "does not
    explicitly state that this individual is the investigating officer" —
    the role was real, in the graph, and simply never reached the cited
    text. See _synthetic_evidence_chunk()'s own use of this field below.
    """
    if not entity_ids:
        return []
    rows = await age_client.execute_cypher(
        # "case" is a reserved word in openCypher (the CASE WHEN
        # expression) -- confirmed live: using it as a node variable
        # name here raised a real syntax error against AGE, caught only
        # by live verification since the unit-test fake Cypher parser
        # doesn't validate real Cypher grammar. "cs" avoids the clash.
        "MATCH (n)-[r:APPEARS_IN]->(d:Document) "
        "WHERE n.entity_id IN $ids AND r.superseded_by IS NULL "
        "OPTIONAL MATCH (d)-[b:BELONGS_TO_CASE]->(cs:Case) WHERE b.superseded_by IS NULL "
        "RETURN n, r, d, cs.case_id AS case_id",
        params={"ids": list(entity_ids)},
        columns=["n", "r", "d", "case_id"],
    )
    if not rows:
        return rows

    # Copy rather than mutate in place -- consistent with this module's own
    # caution elsewhere (_find_seed_nodes()'s `{**(node or {}), ...}`
    # pattern) about not assuming the Cypher driver's returned dicts are
    # safe to mutate/shared nowhere else.
    roles_by_entity_case = await _fetch_officer_roles(entity_ids)
    return [
        {**row, "officer_roles": roles_by_entity_case.get((_entity_id_of(row.get("n")), row.get("case_id")), [])}
        for row in rows
    ]


async def _fetch_officer_roles(entity_ids: set[str]) -> dict[tuple[Optional[str], Optional[str]], list[str]]:
    """
    Every current (non-superseded) ASSIGNED_TO role for the given entities,
    keyed by (entity_id, case_id) — a clean, one-row-per-edge query with no
    fan-out risk (unlike folding this into _fetch_appears_in()'s own MATCH,
    see that function's own docstring for the live-confirmed bug this
    avoids). Non-Officer entity_ids simply match nothing here (ASSIGNED_TO
    is only ever written for Officer nodes) — cheap to include unfiltered
    rather than pre-splitting the caller's entity_ids by label.
    """
    if not entity_ids:
        return {}
    rows = await age_client.execute_cypher(
        "MATCH (n)-[ar:ASSIGNED_TO]->(cs:Case) "
        "WHERE n.entity_id IN $ids AND ar.superseded_by IS NULL "
        "RETURN n.entity_id AS entity_id, cs.case_id AS case_id, ar.role AS role",
        params={"ids": list(entity_ids)},
        columns=["entity_id", "case_id", "role"],
    )
    roles_by_entity_case: dict[tuple[Optional[str], Optional[str]], list[str]] = {}
    for row in rows:
        key = (row.get("entity_id"), row.get("case_id"))
        role = row.get("role")
        if role:
            roles_by_entity_case.setdefault(key, []).append(role)
    # Sorted, not left in whatever order AGE happened to return rows in —
    # a MATCH with no ORDER BY carries no ordering guarantee, and an
    # officer holding two roles at once (fir-401-26's real data) must
    # render identically every call, not flip depending on row-return
    # order. Cheap: at most two roles per officer today (investigating,
    # recording — structured_projection.py never writes a third kind).
    return {key: sorted(roles) for key, roles in roles_by_entity_case.items()}


def _synthetic_evidence_chunk(row: dict, matched_property: Optional[dict] = None) -> Optional[dict]:
    """
    [Bug fix] Build a synthetic evidence chunk from an APPEARS_IN row
    (n, r, d — the exact shape _fetch_appears_in() returns) whose edge has
    no source_chunk_id. Every entity written via
    src/graph/structured_projection.py (the entire real Muhafiz Data API
    sync corpus — FIRs, criminal records, everything) has this shape:
    resolve_and_write() is always called without a source_chunk_id
    there, since structured JSON records were never chunked/embedded
    into Chroma the way narrative text was — there IS no real chunk to
    look up. Both call sites that used to silently drop these rows
    (retrieve_graph()'s main evidence loop, _fetch_case_conflicts() above)
    were confirmed live to throw away real, correct graph-traversal
    results this way — "seed entity matched but no connected evidence"
    for a connection that demonstrably exists.

    Deterministic template over fields already in the SAME query result
    — never an LLM call, never fabricated text, same discipline this
    codebase's other synthesized strings already follow (e.g.
    candidate_reprioritization._why()). Returns None only when even
    source_doc_id is absent — that genuinely is "nothing to cite" and
    real APPEARS_IN edges always carry one, so this is a defensive
    fallback, not an expected path.

    The returned dict's `id` is deterministic and namespaced
    ("synthetic:...") so it can never collide with a real Chroma chunk
    id, and is merged directly into the caller's fetched_by_id lookup —
    it never goes through get_chunks_by_ids()/Chroma at all.

    `matched_property` [Bug fix]: `{"property": ..., "value": ...}` from
    _matched_seed_property() when this entity is itself a seed node
    (`None` for an entity only reached via a hop — nothing to name there,
    the hop's own via_entity provenance already covers it). Without this,
    the generated sentence never once named the identifier that actually
    justified retrieving it — "طارق appears in fir_structured record
    fir-401-26" for a query specifically about phone number
    0305-4000005, with that number appearing nowhere in the cited text.
    Confirmed live: the LLM (and citation_validator) correctly declined
    to confirm a phone-recurrence claim the graph traversal had in fact
    found, because nothing in front of it said so. See
    _MATCHED_PROPERTY_LABELS for which properties earn a clause —
    canonical_name is deliberately excluded (redundant with surface_text).

    [Module 2 — findings.md] Separately, and regardless of `matched_property`,
    this also always appends a clause for any of `_NOTABLE_PROPERTIES[label]`
    the node carries (belt_no/cnic/phone/plate) that weren't already the
    matched property — the sibling gap to the one above: even when the seed
    WAS matched by name (no match_clause at all), some OTHER property the
    user is actually asking about (e.g. belt number) was never mentioned
    anywhere in the cited text. Applied unconditionally, not gated on the
    query text mentioning that property — see findings.md Module 2's own
    "always include" recommendation.

    [findings.md Module 8 follow-up] A third, always-applied clause for an
    Officer's per-case ASSIGNED_TO role (investigating/recording) — role
    lives on the edge, not a node property, so it can't go through
    `_NOTABLE_PROPERTIES` above. Live-confirmed necessary, not theoretical:
    Local Search's own live-verification found the correct Officer via
    semantic match, but the relevance evaluator then rejected the
    resulting evidence text because it never stated the officer's role —
    the exact same "the graph knows this but the cited text doesn't say
    so" failure shape Module 2 fixed for belt/phone/plate, just for a
    property that happens to live on an edge instead of a node.
    """
    edge_props = row.get("r", {}).get("properties", {}) or {}
    source_doc_id = edge_props.get("source_doc_id")
    if not source_doc_id:
        return None

    entity_id = _entity_id_of(row.get("n"))
    doc_props = (row.get("d") or {}).get("properties", {}) or {}
    doc_type = doc_props.get("doc_type") or "record"
    filename = doc_props.get("filename") or source_doc_id
    surface_text = (
        edge_props.get("surface_text")
        or (row.get("n") or {}).get("properties", {}).get("canonical_name")
        or entity_id
    )
    match_clause = ""
    if matched_property:
        label = _MATCHED_PROPERTY_LABELS.get(matched_property.get("property") or "")
        value = matched_property.get("value")
        if label and value:
            match_clause = f", whose {label} is {value},"

    # [Module 2 — findings.md] Notable properties beyond whichever one
    # justified the seed match (if any) — always included, per findings.md's
    # own recommendation, rather than gated on query text mentioning them.
    # Applies to seed AND hop-reached rows alike (matched_property is None
    # for a hop-reached entity, so nothing is excluded there).
    matched_prop_name = (matched_property or {}).get("property")
    node_label = (row.get("n") or {}).get("label")
    node_props = (row.get("n") or {}).get("properties", {}) or {}
    notable_parts = []
    for prop in _NOTABLE_PROPERTIES.get(node_label, ()):
        if prop == matched_prop_name:
            continue  # already named in match_clause — don't duplicate
        value = node_props.get(prop)
        if value:
            notable_parts.append(f"{_MATCHED_PROPERTY_LABELS.get(prop, prop)} {value}")
    notable_clause = ""
    if notable_parts:
        joined = notable_parts[0] if len(notable_parts) == 1 else (
            ", ".join(notable_parts[:-1]) + ", and " + notable_parts[-1]
        )
        notable_clause = f", with {joined} recorded there"

    # [findings.md Module 8 follow-up] An Officer's per-case ASSIGNED_TO
    # role(s) (investigating/recording) — see _fetch_appears_in()'s own
    # docstring for why this is correlated to the specific case this row's
    # document belongs to (not looked up unscoped) and why it's a LIST:
    # the same officer can legitimately hold both roles on one case at
    # once (fir-401-26's own real data), and naming only one would be
    # either wrong or silently non-deterministic. Empty for every
    # non-Officer entity and for a case-less document; not gated on
    # `matched_property`/`_NOTABLE_PROPERTIES` like the clauses above,
    # since role isn't a node property at all — it lives on the edge.
    officer_roles = row.get("officer_roles") or []
    if not officer_roles:
        role_clause = ""
    elif len(officer_roles) == 1:
        role_clause = f", and is recorded as the {officer_roles[0]} officer for this case"
    else:
        joined_roles = ", ".join(officer_roles[:-1]) + " and " + officer_roles[-1]
        role_clause = f", and is recorded as both the {joined_roles} officer for this case"

    metadata = {"source": source_doc_id, "doc_type": doc_type, "synthetic_evidence": True}
    case_id = row.get("case_id")
    if case_id:
        # From _fetch_appears_in()'s own OPTIONAL MATCH on THIS document's
        # (not the entity's) BELONGS_TO_CASE — real, not guessed from the
        # doc_id string, and always the one case this specific document
        # belongs to even when the entity itself recurs across many.
        # Omitted (not None) for a genuinely case-less document (e.g. a
        # criminal_db record, cross-referenced by CNIC, never case-
        # anchored) rather than asserting a case that doesn't exist.
        metadata["case_id"] = case_id
    return {
        "id": f"synthetic:{entity_id}:{source_doc_id}",
        "text": f"{surface_text}{match_clause} appears in {doc_type} record {filename} ({source_doc_id}){notable_clause}{role_clause}.",
        "metadata": metadata,
    }


def _synthetic_relationship_chunk(
    from_name: str, to_name: str, edge_props: dict, case_id: Optional[str], hop: int, chain_confidence: float,
) -> Optional[dict]:
    """
    [Bug fix, 2026-08-27 route sweep, BUG-4] Build an explicit "X is
    associated with Y" evidence chunk for an ASSOCIATED_WITH hop actually
    crossed during traversal — same deterministic-template discipline as
    `_synthetic_evidence_chunk` (never an LLM call, never fabricated,
    every field sourced from this same edge's own properties).

    ROOT CAUSE THIS CLOSES: `retrieve_graph()`'s hop-expansion loop always
    correctly walked ASSOCIATED_WITH edges to grow the traversal frontier
    (hop_of/via_entity/path_confidence all track it) — but the final
    evidence chunk list was built entirely from `_fetch_appears_in()`,
    i.e. per-entity "X appears in document Y" facts. The relationship
    itself — the actual reason two entities are connected — was never
    turned into a citable sentence anywhere. Live-confirmed: a query like
    "Is X known to associate with anyone else in this case?" reached
    hop_count=1 (Y genuinely was found one hop out) and returned real
    evidence about X and real evidence about Y as two SEPARATE "appears
    in record" facts, with nothing anywhere stating they are connected —
    the evaluator correctly judged that insufficient to confirm an
    association, because nothing in the text said so. Exactly the "the
    graph knows this but the cited text doesn't say so" failure class
    _synthetic_evidence_chunk's own docstring already documents fixing
    for node properties/officer roles; this is the same fix for the
    relationship edge itself, the one thing this function's docstring
    admits was still missing.

    `edge_props["basis"]` is the edge's own honestly-worded provenance
    (structured_projection.py's _write_associated_with(): "co-mentioned
    in case <id>'s incident" — a structural fact, not a stated
    relationship, which is exactly why that write site also caps
    confidence at 0.5 rather than 1.0). Falls back to a bare "associated
    with" statement if a future writer ever omits `basis` — still true,
    just less specific, never fabricated beyond what the edge itself
    asserts.

    Returns None only when the edge carries no `source_doc_id` at all
    (mirrors `_synthetic_evidence_chunk`'s own "genuinely nothing to
    cite" fallback) — real ASSOCIATED_WITH edges always carry one.
    """
    source_doc_id = edge_props.get("source_doc_id")
    if not source_doc_id:
        return None
    basis = edge_props.get("basis")
    relation_clause = f" ({basis})" if basis else ""
    metadata = {"source": source_doc_id, "doc_type": "graph_relationship", "synthetic_evidence": True}
    if case_id:
        metadata["case_id"] = case_id
    return {
        "id": f"synthetic-rel:{from_name}:{to_name}:{source_doc_id}",
        "text": f"{from_name} is associated with {to_name}{relation_clause}.",
        "metadata": metadata,
        "rrf_score": chain_confidence,
        "hop": hop,
        "graph_confidence": chain_confidence,
        "via_entity": from_name,
    }


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


# Set when a query named a station/district that resolved to no real
# jurisdiction node, so the answer covers every jurisdiction rather than the
# one asked about. A per-request ContextVar (same mechanism and lifetime as
# current_cross_case/current_rls_active) rather than a changed return type:
# resolve_jurisdiction_case_ids()'s `Optional[list[str]]` contract is relied
# on by run_aggregate()/retrieve_graph()'s `is not None` narrowing checks,
# and a sentinel value would be filtered against as a case-id allow-list —
# silently zeroing the very query it was meant to annotate.
_JURISDICTION_UNRESOLVED: ContextVar[bool] = ContextVar(
    "jurisdiction_unresolved", default=False
)


def jurisdiction_unresolved() -> bool:
    """
    True when this request asked about a station/district that could not be
    matched, meaning the figures returned are platform-wide. Callers should
    disclose this to the user rather than presenting the result as scoped.
    """
    return _JURISDICTION_UNRESOLVED.get()


def reset_jurisdiction_unresolved() -> None:
    """Clear the flag at the start of a request, so it never leaks across turns."""
    _JURISDICTION_UNRESOLVED.set(False)


def _normalize_jurisdiction(value: Optional[str]) -> str:
    """
    [findings.md JURISDICTION] Deterministic normalization for a free-text
    jurisdiction expression, applied before any alias lookup.

    NFKC (so Urdu presentation forms and ASCII width variants compare
    equal), trim, collapse internal whitespace, casefold. Casefolding is a
    no-op on Urdu — which is exactly the point: it makes the ASCII half of
    the comparison reliable without touching the Urdu half.

    Deliberately NOT transliteration and NOT spelling correction: an alias
    either matches a jurisdiction this corpus actually stores, or the
    input stays unresolved. Guessing a district is the failure mode this
    whole layer exists to prevent.
    """
    if not value:
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


# [findings.md JURISDICTION] English -> stored-Urdu district aliases,
# keyed ONLY to the nine District nodes this corpus actually has. District
# `name` is Urdu-only and `district_id` is opaque ("DIST-04"), so no
# English query could ever match by string comparison alone — measured
# live: "Lahore" resolved to None while "لاہور" resolved to DIST-04.
#
# Scoped to real stored jurisdictions on purpose: this is not a gazetteer,
# and an alias for a district the corpus does not contain would be a
# guess, not a resolution.
_DISTRICT_ALIASES: dict[str, str] = {
    "lahore": "لاہور",
    "rawalpindi": "راولپنڈی",
    "faisalabad": "فیصل آباد",
    "islamabad": "اسلام آباد",
    "hyderabad": "حیدر آباد",
    "multan": "ملتان",
    "chiniot": "چنیوٹ",
    "karachi central": "کراچی وسطی",
    "karachi east": "کراچی ایسٹ",
}

# [findings.md JURISDICTION] Aliases that legitimately name more than one
# stored jurisdiction. "Karachi" is two real districts here (کراچی وسطی /
# کراچی ایسٹ), so resolving it to either one would silently narrow a
# supervisor's query to half the city and present it as the whole.
# Ambiguous input therefore fails through the SAME unresolved path as
# unknown input — `resolve_jurisdiction_case_ids()` does not narrow, and
# sets UNRESOLVED_JURISDICTION so the caller discloses it.
#
# Kept as a singular-resolver contract on purpose: widening
# `_resolve_district_id()` to return multiple ids is a larger shared/live
# change than this fix is scoped for.
_AMBIGUOUS_DISTRICT_ALIASES: frozenset[str] = frozenset({"karachi"})

# [findings.md JURISDICTION] English station aliases, each proven against
# exactly one stored station_id. Derived from the ASCII id itself, never
# translated from the Urdu name.
#
# "saddar" and "cyber" are deliberately ABSENT: each names two real
# stations (PS-CHN-SADDAR/PS-RWP-SADDAR, PS-ISB-CYBER/PS-RWP-CYBER), so
# they belong in the ambiguous set below, not here.
_STATION_ALIASES: dict[str, str] = {
    "model town": "PS-LHR-MODELTOWN",
    "iqbal town": "PS-LHR-IQBALTOWN",
    "barki": "PS-LHR-BARKI",
    "civil lines": "PS-FSD-CIVILLINES",
    "jhang road": "PS-FSD-JHANGROAD",
    "kotwali": "PS-FSD-KOTWALI",
    "madina town": "PS-FSD-MADINATOWN",
    "latifabad": "PS-HYD-LATIFABAD",
    "new karachi": "PS-KHI-NEWKARACHI",
    "shah faisal": "PS-KHI-SHAHFAISAL",
    "shah faisal colony": "PS-KHI-SHAHFAISAL",
    "raja bazar": "PS-RWP-RAJABAZAR",
    "waris khan": "PS-RWP-WARISKHAN",
}

_AMBIGUOUS_STATION_ALIASES: frozenset[str] = frozenset({"saddar", "cyber", "karachi"})


async def _resolve_station_id(station: str) -> Optional[str]:
    """
    Free-text station name/code (router.py's own extraction, not a
    caller-typed id) -> B1's PoliceStation.station_id, or None if nothing
    matches.

    [findings.md JURISDICTION] Lookup order is deliberate: exact canonical
    id, then exact stored name, then a proven English alias, and only then
    the original CONTAINS behaviour as a compatibility tail. The old code
    led with CONTAINS, which resolved "Karachi" to PS-KHI-NEWKARACHI purely
    because "khi" sits inside that ASCII id — an accident, not an alias.
    Ambiguous aliases are rejected before the tail can re-create it.
    """
    normalized = _normalize_jurisdiction(station)
    if not normalized:
        return None
    if normalized in _AMBIGUOUS_STATION_ALIASES:
        return None

    rows = await age_client.execute_cypher(
        "MATCH (s:PoliceStation) "
        "WHERE toLower(s.station_id) = $q OR toLower(s.name) = $q OR toLower(s.code) = $q "
        "RETURN s.station_id AS id LIMIT 1",
        params={"q": normalized}, columns=["id"],
    )
    if rows and rows[0].get("id"):
        return rows[0]["id"]

    alias = _STATION_ALIASES.get(normalized)
    if alias:
        return alias

    rows = await age_client.execute_cypher(
        "MATCH (s:PoliceStation) "
        "WHERE toLower(s.station_id) CONTAINS $q OR toLower(s.name) CONTAINS $q OR toLower(s.code) CONTAINS $q "
        "RETURN s.station_id AS id LIMIT 1",
        params={"q": normalized}, columns=["id"],
    )
    return rows[0]["id"] if rows and rows[0].get("id") else None


async def _resolve_district_id(district: str) -> Optional[str]:
    """
    Free-text district name -> B1's District.district_id, or None if
    nothing matches.

    [findings.md JURISDICTION] Same deliberate order as
    `_resolve_station_id()`. An ambiguous alias ("karachi") returns None
    rather than picking one of the two real Karachi districts.
    """
    normalized = _normalize_jurisdiction(district)
    if not normalized:
        return None
    if normalized in _AMBIGUOUS_DISTRICT_ALIASES:
        return None

    rows = await age_client.execute_cypher(
        "MATCH (d:District) "
        "WHERE toLower(d.district_id) = $q OR toLower(d.name) = $q "
        "RETURN d.district_id AS id LIMIT 1",
        params={"q": normalized}, columns=["id"],
    )
    if rows and rows[0].get("id"):
        return rows[0]["id"]

    alias = _DISTRICT_ALIASES.get(normalized)
    if alias:
        rows = await age_client.execute_cypher(
            "MATCH (d:District) WHERE d.name = $q RETURN d.district_id AS id LIMIT 1",
            params={"q": alias}, columns=["id"],
        )
        if rows and rows[0].get("id"):
            return rows[0]["id"]

    rows = await age_client.execute_cypher(
        "MATCH (d:District) "
        "WHERE toLower(d.district_id) CONTAINS $q OR toLower(d.name) CONTAINS $q "
        "RETURN d.district_id AS id LIMIT 1",
        params={"q": normalized}, columns=["id"],
    )
    return rows[0]["id"] if rows and rows[0].get("id") else None


async def resolve_jurisdiction_case_ids(
    *,
    station: Optional[str],
    district: Optional[str],
    query_text: str = "",
    user_id: Optional[str] = None,
    user_role: str = "investigator",
) -> Optional[list[str]]:
    """
    Milestone E1 — the orchestrator.py entry point B1's own
    `retrieve_jurisdiction_cases()` docstring named as "Milestone E1's
    job, out of scope here". Turns router.py's free-text `station`/
    `district` classification fields (see prompts/router.txt) into the
    case_id allow-list `retrieve_graph()`/`run_aggregate()`/
    `run_network_query()` narrow their own candidate sets to, BEFORE any
    vector/graph work runs for those routes — E1's own stated goal.

    Deliberately NOT a second gate: resolving a station/district NAME to
    B1's `PoliceStation.station_id`/`District.district_id` is a plain
    metadata lookup (no case data touched, nothing to gate). The actual
    case-enumeration call below goes through `retrieve_jurisdiction_cases()`
    unchanged — same function, same `_enforce_cross_case_role_gate()`
    call, per B1's own "second caller of it, not a second gate" precedent.
    A caller without the cross-case role gets the identical
    `PermissionError`(+audit record) `retrieve_jurisdiction_cases()`
    already raises; the orchestrator's existing per-route `except
    Exception` handlers already degrade this the same way an
    unauthorized `retrieve_graph(cross_case=True)`/`run_aggregate()` call
    already does today — no new error-handling path needed.

    Returns `None` (not `[]`) when neither `station` nor `district` was
    classified, OR when one was but resolved to no real jurisdiction node
    — `None` means "don't narrow," an empty list would incorrectly mean
    "narrow to nothing," silently zeroing out a query whose station/
    district text just didn't match (e.g. a name only present in the
    query's prose, not literally in `PoliceStation.name`).
    """
    if not station and not district:
        return None

    station_id = await _resolve_station_id(station) if station else None
    district_id = await _resolve_district_id(district) if district else None
    if not station_id and not district_id:
        # Returning None here is deliberate and unchanged ("don't narrow"
        # beats "narrow to nothing" — see this function's docstring). What
        # changed 2026-08-24 is that it is no longer SILENT.
        #
        # The resolution itself is unreliable against the live corpus, and
        # not along a clean language line: PoliceStation.station_id is
        # ASCII ("PS-LHR-MODELTOWN") while .name is Urdu, and District has
        # an opaque id ("DIST-04") with an Urdu-only name. Measured live —
        # station "Karachi" resolves (via station_id), "Lahore" does not;
        # district "لاہور" resolves, "Lahore" does not. So an English
        # jurisdiction query frequently falls through to platform-wide.
        #
        # The role gate is NOT bypassed by this: cross-case enumeration
        # still runs through retrieve_jurisdiction_cases()'s own
        # _enforce_cross_case_role_gate(), and only supervisor+ reaches
        # here at all. The harm is precision, not confidentiality — a
        # supervisor asking about one district gets platform-wide figures
        # presented as that district's. That must be disclosed, not hidden,
        # which is what UNRESOLVED_JURISDICTION exists for.
        logger.warning(
            "Milestone E1: router classified station=%r district=%r but neither "
            "resolved to a real PoliceStation/District node — NOT narrowing, so "
            "results will cover every jurisdiction. Caller should disclose this "
            "via jurisdiction_unresolved().",
            station, district,
        )
        _JURISDICTION_UNRESOLVED.set(True)
        return None

    result = await retrieve_jurisdiction_cases(
        station_id=station_id, district_id=district_id, query_text=query_text,
        user_id=user_id, user_role=user_role,
    )
    return result["case_ids"]


async def retrieve_graph(
    query_text: str,
    target_entity: Optional[str],
    case_id: Optional[str],
    cross_case: bool = False,
    max_hops: int = DEFAULT_HOPS,
    user_id: Optional[str] = None,
    user_role: str = "investigator",
    jurisdiction_case_ids: Optional[list[str]] = None,
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

    `jurisdiction_case_ids` [Milestone E1]: only meaningful when
    `cross_case=True` — narrows the cross-case seed lookup to this case
    set (see `resolve_jurisdiction_case_ids()`). `None` (the default)
    leaves cross-case seed lookup exactly as unscoped as before this
    milestone; ignored entirely on the within-case path, which is always
    scoped to `case_id` regardless.
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
        seed_nodes = await _find_seed_nodes(candidates, case_id, cross_case, jurisdiction_case_ids)
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
            seed_nodes = await _find_recurring_entities_for_query(
                query_text, min_cases=1, limit=50, jurisdiction_case_ids=jurisdiction_case_ids,
            )
        else:
            seed_nodes = await _find_recurring_entities_for_query(
                query_text, jurisdiction_case_ids=jurisdiction_case_ids,
            )
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
    # [Bug fix] Only ever set for _find_seed_nodes()'s literal-match
    # branch — _find_all_case_entities()/_find_recurring_entities_for_query()
    # seed by case membership/recurrence, not a property match, so they
    # never carry "_matched_property" and are correctly absent here.
    # Deliberately NOT propagated past hop 0 below: an entity reached via
    # a hop wasn't matched by property at all, it was reached by a graph
    # edge — via_entity already carries that provenance.
    matched_property_of: dict[str, dict] = {
        eid: n["_matched_property"]
        for n in seed_nodes
        if (eid := _entity_id_of(n)) and n.get("_matched_property")
    }

    display_name: dict[str, str] = {e["entity_id"]: e["name"] for e in seed_entities if e["entity_id"]}
    via_entity: dict[str, str] = {eid: eid for eid in display_name}  # entity_id -> seed name that led to it
    path_confidence: dict[str, float] = {eid: 1.0 for eid in display_name}
    hop_of: dict[str, int] = {eid: 0 for eid in display_name}

    frontier: set[str] = set(display_name.keys())
    visited: set[str] = set(frontier)

    # Milestone D2: entities reached ONLY through a pending (not
    # confirmed) identity link — tagged in the final chunk metadata so
    # verifier._check_hedging() disclosure applies even to a chunk whose
    # compounded confidence would otherwise land at/above the hedge
    # threshold (e.g. a single high-confidence hop from a pending link
    # whose OWN confidence was already capped). Empty and never consulted
    # when the flag is off.
    pending_identity_basis: dict[str, str] = {}
    pending_hedge_enabled = cross_case and config.FEATURE_HEDGED_PENDING_TRAVERSAL

    # [Bug fix, 2026-08-27 route sweep, BUG-4] one explicit "X is
    # associated with Y" chunk per ASSOCIATED_WITH edge actually crossed
    # — see _synthetic_relationship_chunk()'s own docstring for why this
    # exists at all (the relationship itself was never citable evidence
    # before this fix, only the two entities' separate document-mention
    # facts were). Keyed on the chunk's own id so the two directions
    # `_one_hop_neighbors()` returns for one real edge collapse to a
    # single chunk, not a duplicate.
    relationship_chunks: dict[str, dict] = {}

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

        # [Milestone E2 — GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md] The real gap
        # E2 found: _expand_confirmed_identity() is unconditional (it runs
        # regardless of cross_case, unlike _find_seed_nodes/
        # _find_recurring_entities_for_query, which are gated behind an
        # explicit cross_case=True + the role check in
        # _enforce_cross_case_role_gate). A CONFIRMED SAME_AS edge is,
        # BY DEFINITION, frequently a cross-case link (the same real person
        # recognized in two different FIRs) — so folding it into `visited`
        # unfiltered let a within-case query (cross_case=False) silently
        # pull another case's entity straight into `visited`, and from
        # there into `_fetch_appears_in(visited)` below, surfacing that
        # OTHER case's chunks without ever going through the cross-case
        # role gate. The existing per-hop guard (`_filter_to_case` on
        # `next_frontier`, below) only ever covered ordinary
        # ASSOCIATED_WITH hops — it never touched `new_identity`, since
        # `new_identity` is added to `visited`/`frontier` here, before that
        # filter runs. Case-filtering `new_identity` here, the same way
        # `next_frontier` already is, closes that: this makes "default
        # case-scoped" apply to every path that grows `visited`, not just
        # the ASSOCIATED_WITH hop path. Cross-case queries are unaffected —
        # they already went through the role gate to set cross_case=True.
        if not cross_case and case_id:
            new_identity = await _filter_to_case(new_identity, case_id)
        visited |= new_identity
        frontier |= new_identity

        if pending_hedge_enabled:
            # D2's opened traversal path — see module docstring's
            # "[Milestone D2]" note on the SAME_AS identity rule above.
            # Unlike confirmed identity, a pending link's own confidence
            # is folded INTO path_confidence (compounded, capped below
            # the hedge threshold), not carried through unchanged — it is
            # explicitly NOT a settled fact.
            pending_pairs = await _expand_pending_identity(frontier)
            new_pending: set[str] = set()
            for from_id, to_id, to_node, edge_conf, basis in pending_pairs:
                if to_id in visited or to_id in new_pending:
                    continue
                new_pending.add(to_id)
                display_name[to_id] = _display_name(to_node)
                via_entity[to_id] = via_entity.get(from_id, from_id)
                path_confidence[to_id] = min(path_confidence.get(from_id, 1.0) * edge_conf, _PENDING_HEDGE_CAP)
                hop_of[to_id] = hop_of.get(from_id, hop)
                pending_identity_basis[to_id] = basis
            visited |= new_pending
            frontier |= new_pending

        neighbor_rows = await _one_hop_neighbors(frontier)
        next_frontier: set[str] = set()
        for row in neighbor_rows:
            a_id, b_id = _entity_id_of(row.get("a")), _entity_id_of(row.get("b"))
            edge_props = row.get("r", {}).get("properties", {}) or {}
            edge_conf = float(edge_props.get("confidence", 1.0))
            for from_id, to_id, to_node in ((a_id, b_id, row.get("b")), (b_id, a_id, row.get("a"))):
                if not from_id or not to_id or from_id not in frontier or to_id in visited:
                    continue
                chain_confidence = path_confidence.get(from_id, 1.0) * edge_conf
                # [Bug fix, 2026-08-27 route sweep, BUG-4] Record the
                # relationship itself, right alongside first-discovering
                # `to_id` — gated on the SAME `to_id not in visited` check
                # as the frontier growth just above, deliberately: it both
                # (a) fixes the bug (no relationship chunk existed at all
                # for a hop that plainly happened) and (b) stays a
                # first-discovery-only event, so a later hop walking the
                # SAME edge backward (to_id's own neighbor lookup finding
                # from_id again once to_id is itself in frontier) doesn't
                # re-emit the identical fact as a second, reverse-worded
                # duplicate chunk.
                rel_chunk = _synthetic_relationship_chunk(
                    display_name.get(from_id, from_id), _display_name(to_node),
                    edge_props, case_id, hop, chain_confidence,
                )
                if rel_chunk is not None:
                    relationship_chunks.setdefault(rel_chunk["id"], rel_chunk)
                next_frontier.add(to_id)
                display_name[to_id] = _display_name(to_node)
                via_entity[to_id] = via_entity.get(from_id, from_id)
                path_confidence[to_id] = chain_confidence
                hop_of[to_id] = hop
                # D2: a hop OUT of an entity only reachable via a pending
                # identity link is itself downstream of that same
                # unconfirmed identity — the disclosure obligation
                # propagates forward, not just to the one entity that
                # crossed the pending edge directly. path_confidence
                # above already stays capped (compounding only shrinks
                # it further), this only carries the basis text along.
                if from_id in pending_identity_basis and to_id not in pending_identity_basis:
                    pending_identity_basis[to_id] = pending_identity_basis[from_id]

        if not cross_case and case_id:
            next_frontier = await _filter_to_case(next_frontier, case_id)

        visited |= next_frontier
        frontier = next_frontier

    unconfirmed_links = await _unconfirmed_same_as_links(visited) if cross_case else []

    appears_in_rows = await _fetch_appears_in(visited)
    # [Module 7 follow-up, findings.md] _fetch_appears_in() is a general,
    # case-agnostic helper — it returns EVERY APPEARS_IN edge for a given
    # entity set, each row correctly tagged with its OWN document's real
    # case_id (see that function's own "Bug fix" docstring for the
    # join-off-`d` history). That's exactly right for cross_case=True
    # (XGRAPH's whole job is citing evidence from other cases) — but for
    # a within-case query this was NEVER filtered back down to `case_id`,
    # contradicting this function's own docstring ("ignored entirely on
    # the within-case path, which is always scoped to case_id
    # regardless"). Live-confirmed: an entity that legitimately belongs
    # to the active case (a real BELONGS_TO_CASE edge, so it passes
    # _filter_to_case() and is a valid seed/frontier member) can ALSO
    # belong to a different case if the same real person/entity recurs —
    # exactly the shape XGRAPH/XAGG exist to surface — and their evidence
    # from THAT other case was leaking into a within-case answer's
    # citation list unfiltered, caught only downstream by verify_grounding()'s
    # leakage check (correctly rejecting the answer, but for a document
    # that should never have reached the prompt at all). `row["case_id"]`
    # is None for genuinely case-less content (e.g. a criminal_db record —
    # that silo is CNIC-cross-referenced, not case-anchored, per
    # _fetch_appears_in()'s own docstring) — kept, not filtered out.
    if not cross_case and case_id:
        appears_in_rows = [
            row for row in appears_in_rows
            if row.get("case_id") in (None, case_id)
        ]
    chunk_ids: list[str] = []
    row_by_chunk: dict[str, dict] = {}
    synthetic_by_id: dict[str, dict] = {}
    for row in appears_in_rows:
        entity_id = _entity_id_of(row.get("n"))
        edge_props = row.get("r", {}).get("properties", {}) or {}
        source_chunk_id = edge_props.get("source_chunk_id")
        if not entity_id:
            continue
        if not source_chunk_id:
            # [Bug fix] structured-extraction writes (src/graph/
            # structured_projection.py — the entire real Muhafiz sync
            # corpus) never carry a source_chunk_id — see
            # _synthetic_evidence_chunk()'s own docstring for the full
            # story. Confirmed live: without this, a real, existing
            # cross-case connection reported "seed entity matched but no
            # connected evidence" every time, indistinguishable from a
            # genuinely empty result.
            synthetic = _synthetic_evidence_chunk(row, matched_property=matched_property_of.get(entity_id))
            if synthetic is None:
                continue
            synthetic_by_id[synthetic["id"]] = synthetic
            source_chunk_id = synthetic["id"]
        chunk_ids.append(source_chunk_id)
        row_by_chunk[source_chunk_id] = {
            "entity_id": entity_id,
            "mention_confidence": float(edge_props.get("confidence", 1.0)),
        }

    fetched_chunks = await get_chunks_by_ids(
        [cid for cid in dict.fromkeys(chunk_ids) if cid not in synthetic_by_id]
    )
    fetched_by_id = {c["id"]: c for c in fetched_chunks}
    fetched_by_id.update(synthetic_by_id)

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
        chunk = {
            **base,
            "rrf_score": chain_confidence,
            "hop": hop_n,
            "graph_confidence": chain_confidence,
            "via_entity": display_name.get(via_entity.get(entity_id, entity_id), entity_id),
        }
        pending_basis = pending_identity_basis.get(entity_id)
        if pending_basis is not None:
            # Milestone D2: tag disclosure obligation onto the chunk
            # itself, independent of the numeric graph_confidence value —
            # verifier._check_hedging()'s extension checks this tag
            # directly rather than relying solely on the (already capped)
            # confidence number, defense-in-depth against a future edit
            # that changes how confidence compounds.
            chunk["metadata"] = {
                **(chunk.get("metadata") or {}),
                "same_as_status": "pending",
                "same_as_basis": pending_basis,
            }
        chunks.append(chunk)
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

    # [Bug fix, 2026-08-27 route sweep, BUG-4] append the explicit
    # relationship chunks collected during the hop loop above — see
    # _synthetic_relationship_chunk()'s own docstring. Appended after the
    # per-entity APPEARS_IN chunks (not interleaved) so an entity's own
    # facts are cited first and the "here's how they connect" statement
    # reads as the synthesis of those, matching how a human summary would
    # order it.
    for rel_chunk in relationship_chunks.values():
        chunks.append(rel_chunk)
        max_hop_returned = max(max_hop_returned, rel_chunk["hop"])
        weakest_confidence = min(weakest_confidence, rel_chunk["graph_confidence"])

    return {
        "chunks": chunks,
        "hop_count": max_hop_returned,
        "compounded_confidence": weakest_confidence if chunks else 1.0,
        "seed_entities": seed_entities,
        "unconfirmed_links": unconfirmed_links,
    }
