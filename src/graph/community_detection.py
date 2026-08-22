# ============================================================
# Community detection — GraphRAG-inspired layer, Section 2 (additive,
# read-only against the Apache AGE graph; writes only to the new Postgres
# side tables from migration 016). See docs/AUDIT_FINDINGS_2026-08-04.md /
# FIX_PLAN_2026-08-04.md for the verified graph state this builds on.
#
# Does NOT touch age_client.py's connection handling, entity_resolution.py's
# resolution logic, or versioning.py's append-only edge model — this module
# only reads the graph (via age_client.execute_cypher, the same interface
# every other read-side module uses) and writes a derived, recomputable
# snapshot to Postgres.
#
# ── Why a projected "shared-case" edge, not ASSOCIATED_WITH alone ──
# ASSOCIATED_WITH (the real extracted person-to-person relationship) is,
# live-checked against the current graph, only ~17 edges across ~1000
# Person nodes — far too sparse to cluster on by itself. BELONGS_TO_CASE
# (~1700 edges) is dense but bipartite (entity->Case, not entity->entity).
# This module projects BELONGS_TO_CASE into a derived Person<->Person
# "shares N cases with" edge — the same underlying join xagg.py's
# _top_recurring_nodes() already does for single-node recurrence counts,
# generalized here to pairs. Real ASSOCIATED_WITH edges are weighted higher
# than derived shared-case edges, since they're extracted facts, not
# co-occurrence inference.
#
# ── Why canonicalize through SAME_AS before clustering ──
# entity_resolution.py mints a new Person node for every name-fallback
# mention rather than physically merging (see docs/graph_schema.md's
# "Entity resolution & canonicalization" section) — so a real-world person
# can be split across several graph nodes linked by *confirmed*,
# non-superseded SAME_AS edges. Clustering on the un-collapsed graph would
# fragment one person into multiple community members. This module
# collapses confirmed SAME_AS components to a single canonical id
# (read-time only — no write to SAME_AS or versioning.py) before building
# the community graph.
#
# ── Storage ──
# community_id is a derived analytical snapshot, not a document-sourced
# fact, so it is NOT written back as an AGE node property through
# versioning.write_node() (which would require fabricating a fake
# source_doc_id). It lives in Postgres (migration 016), replaced wholesale
# each run: deleting the prior community_runs row cascades to
# community_membership and community_reports, so "latest run only" is
# enforced by the FK, not by manual TRUNCATE bookkeeping.
# ============================================================

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

import networkx as nx
from networkx.algorithms.community import louvain_communities
from sqlalchemy import text

from src.database.postgres import get_session
from src.graph import age_client

logger = logging.getLogger(__name__)

# Edge weight components — ASSOCIATED_WITH is an extracted fact, so it
# outweighs the derived shared-case co-occurrence signal per shared case.
_ASSOCIATED_WITH_BASE_WEIGHT = 3.0
_SHARED_CASE_WEIGHT_PER_CASE = 1.0

# ── Non-name filter (narrow, additive guard — not an ner.py fix) ──────────
# Live-discovered running this module against the real graph: one document
# batch (the 13 CASE-B0-* cases, "complex"/"retrofitted" rendering tier per
# data/memory/case_index.csv) averages ~70 Person nodes/case vs. ~7 for
# every other case — sampling confirmed the excess is case-diary/General
# Diary form-field labels ("Date", "District", "Under Section", "Roznamcha",
# "House No", "Entry No") mistagged as Person entities, the same failure
# *class* the 2026-08-04 audit's Finding B-4 caught two live instances of,
# just systemic here rather than isolated.
#
# This is community_detection.py's own concern, not ner.py's: clustering on
# un-filtered nodes would produce a fluent-sounding, confidently-written
# LLM summary describing a fake "network" of form labels — worse than not
# having the feature, for a tool whose whole premise is grounded answers.
# Root-causing WHY this document tier's extraction goes wrong belongs to
# Section 1's extraction pipeline (src/extraction/ner.py), not here — this
# is a narrow, conservative guard scoped to keep obviously-non-name nodes
# out of the community graph, not a fix to the extraction pipeline itself.
#
# Two rules, both derived from what was actually observed in the sample
# (not a generic stoplist guessed in the abstract):
#   1. A canonical_name with no space (a single token) is dropped — every
#      real person mention sampled was 2+ tokens (e.g. "Irfan Mirza",
#      "Inspector Fariha Saeed"); every single-token mention sampled was
#      noise ("Date", "No", "Total", "The", "Mulzim"). This trades a little
#      recall (a bare-surname fragment like "Saeed" already covered by
#      "Fariha Saeed"/"Inspector Fariha Saeed" mentions of the same person)
#      for a large precision gain.
#   2. An exact-match blocklist for the multi-token administrative/
#      procedural phrases sampled (form-section headers, role titles with
#      no name attached, station/place names extracted as Person). Exact
#      match only — "Inspector Fariha Saeed" is NOT dropped by the
#      "Investigating Officer" entry below, since it's a different string
#      that happens to contain a real name.
_NON_NAME_PHRASES = {
    "under sections", "under section", "general diary", "house no",
    "golra road", "action taken", "thumb impression",
    "complainant station house officer", "bhara kahu police station",
    "investigating officer", "entry dates", "entry no",
    "judicial magistrate", "progress summary", "unknown mulzim",
    "bhara kahu", "statements recorded", "evidence collected",
    "next steps", "current status", "under investigation", "shahzad town",
    # Third pass — found running scripts/cleanup_implausible_person_nodes.py
    # and scripts/backfill_associated_with.py against the live graph:
    # crime-category/status phrases and vehicle model names, both a
    # different shape than the form-label/station noise the first two
    # passes targeted. Same "growing blocklist, not a generalizable fix"
    # limitation already flagged twice — accepted here for the same
    # reason (cheap, targets what's actually been observed), not treated
    # as a closed problem.
    "active leads being pursued", "cyber cell", "cyber fraud", "fraud cell",
    "honda civic car", "house theft", "online scam", "pending trial",
    "roznamcha no", "suzuki alto car", "toyota corolla", "toyota corolla car",
    "applicant",
    "خلاف قانونی کارروائی", "شناخت ابھی تک", "مبینہ مجرم", "مجرم قرار",
    "میرے ساتھ", "میرے گھر",
}

# Location-suffix substring check — same principle as the "police station"
# substring check below, generalized: a capitalized run ending in a
# road/highway/market word is a place, not a person, regardless of which
# specific road. Directly observed: "Main Road", "Margalla Road", "Murree
# Road", "Nilore Road", "Srinagar Highway", "Jinnah Super Market" all
# slipped through the exact-phrase blocklist since each is a different
# specific place — the shared structural marker (the suffix word) is what
# generalizes, not any one of these strings.
_LOCATION_SUFFIX_WORDS = ("road", "highway", "market")

# A second pass, added after re-sampling a post-filter community and
# finding two more failure shapes the exact-phrase blocklist above can't
# generalize to (a growing blocklist alone doesn't scale to unseen document
# tiers — flagged explicitly for the Stage 3 closeout, not treated as
# solved):
#   - an org/station name mistagged as Person ("Shahzad Town Police
#     Station", "Town Police Station") — substring match on "police
#     station", not exact match, since the station name prefix varies
#   - a self-introduction pattern over-capturing the whole sentence
#     instead of just the name ("Tahira Chaudhry D/O Rashid Mirza,
#     resident of House No. 81, Street 25, Sector D-12, Islamabad") — a
#     real name in this corpus was never observed exceeding ~40 characters,
#     so an unusually long canonical_name is treated as a probable
#     extraction over-capture, not a name to cluster on
_MAX_PLAUSIBLE_NAME_LENGTH = 45

# ── Fourth-round audit (2026-08-06) ────────────────────────────────────────
# A full audit (not another one-off patch — see the fix commit this section
# belongs to) looked for a STRUCTURAL signal common to all three prior
# blocklist rounds instead of adding a fourth category of specific words.
# Live-sampling the current graph (161 Person nodes) found 10 nodes across
# 7 distinct name strings that the exact-phrase/suffix/length checks above
# all miss, e.g. "Inspector Fariha Saeed Bhara" (1 occurrence) alongside
# the correctly-extracted "Inspector Fariha Saeed" (7 occurrences) — traced
# to a document-rendering artifact: a table/field boundary collapsing with
# no separating punctuation, so an adjacent field's text (a station-name
# fragment, or a form-label bleeding across a blank line) gets appended
# directly onto a real name with nothing but whitespace between them.
#
# Two structural checks below close this shape WITHOUT enumerating station
# names or form labels (the set of possible trailing fragments is exactly
# this dataset's own station/label list, unbounded in principle — the same
# scaling problem each prior round hit):
#
#   1. Structural-artifact characters. A real extracted person name never
#      contains a newline, carriage return, a markdown table pipe, or a
#      parenthesis — every one of these is a direct signature of a
#      rendering/table boundary bleeding into the captured span
#      ("Inspector Tariq Khan\n\nStatus", "Yusra Nawaz\n\nApplicant",
#      "Inspector (Golra)", all live-observed). Purely per-string, no
#      corpus context needed.
#   2. Prefix contamination (_compute_prefix_contaminated_names below).
#      A candidate name is a contaminated superstring of a real name, not
#      a real longer name in its own right, when dropping its last token
#      yields a shorter string that (a) is itself a canonical_name actually
#      present in the corpus and (b) occurs strictly more often than the
#      candidate. A person's real, distinct longer name essentially never
#      coincides with an unrelated shorter name that's already
#      independently common in the same corpus purely by chance — trading
#      a theoretical false positive for closing the whole category, the
#      same precision-over-recall posture the checks above already take.
_STRUCTURAL_ARTIFACT_CHARS = ("\n", "\r", "|", "(", ")")


def _compute_prefix_contaminated_names(person_names: dict[str, str]) -> set[str]:
    """
    Corpus-frequency-driven — see the "Fourth-round audit" note above.
    Returns the set of raw (untrimmed) canonical_name strings judged a
    contaminated superstring of some other, more common name already
    present in this same person_names population.
    """
    counts = Counter(n for n in person_names.values() if n)
    contaminated: set[str] = set()
    for name, count in counts.items():
        tokens = name.split()
        if len(tokens) < 3:
            continue
        prefix = " ".join(tokens[:-1])
        if counts.get(prefix, 0) > count:
            contaminated.add(name)
    return contaminated


async def fetch_known_police_stations() -> set[str]:
    """
    Real police_station values from the `cases` table, lowercased — a
    genuinely data-driven check rather than another guessed string.
    Live-discovered why this matters: "Industrial Area" and "Lohi Bher"
    both leaked into stored community summaries as if they were person
    names (community_reports "0010"/"0022") — both turned out to be exact,
    real police_station values, not coincidental phrase collisions. A
    hardcoded blocklist entry only catches names already seen; querying
    the actual station list catches every station name this dataset has,
    present or future, without needing to spot each one manually first.
    """
    async with get_session() as db:
        res = await db.execute(text("SELECT DISTINCT police_station FROM cases WHERE police_station IS NOT NULL"))
        return {row[0].strip().lower() for row in res.fetchall() if row[0]}


def _is_plausible_person_name(name: Optional[str], known_stations: Optional[set[str]] = None) -> bool:
    if not name or not name.strip():
        return False
    stripped = name.strip()
    normalized = stripped.lower()
    if normalized in _NON_NAME_PHRASES:
        return False
    if known_stations and normalized in known_stations:
        return False
    if any(ch in stripped for ch in _STRUCTURAL_ARTIFACT_CHARS):
        return False
    if " " not in stripped:
        return False
    if "police station" in normalized:
        return False
    if any(normalized.endswith(suffix) or f" {suffix} " in f" {normalized} " for suffix in _LOCATION_SUFFIX_WORDS):
        return False
    if len(stripped) > _MAX_PLAUSIBLE_NAME_LENGTH:
        return False
    return True


async def fetch_person_names() -> dict[str, str]:
    rows = await age_client.execute_cypher(
        "MATCH (p:Person) RETURN p.entity_id AS entity_id, p.canonical_name AS name",
        columns=["entity_id", "name"],
    )
    return {r["entity_id"]: r["name"] for r in rows if r["entity_id"]}

# A "community" worth summarizing needs at least 2 members — a singleton
# isn't a network. Singletons are still recorded in community_membership
# (each gets its own community_id) for a complete partition, but are
# skipped by the summarization job (community_summarization.py).
MIN_MEMBERS_FOR_SUMMARY = 2


async def estimate_distinct_person_count(member_entity_ids: list[str], person_names: dict[str, str]) -> int:
    """
    Raw member_count over-counts real people: entity_resolution.py mints a
    new node for every name-fallback mention rather than physically
    merging (see docs/graph_schema.md's "Entity resolution &
    canonicalization"), and confirmed SAME_AS collapse (canon(), used
    building the community graph itself) only catches PENDING mentions
    that have already been reviewed/confirmed — an unresolved but
    obviously-the-same-person pending pair still counts as two separate
    community members. A few small communities are really one person's
    unresolved duplicate mentions, not a network of size 2+.

    Greedy single-linkage grouping by name similarity (deliberately not a
    real merge decision, no graph write — read-only estimation for the
    MIN_MEMBERS_FOR_SUMMARY gate only). Reuses entity_resolution.py's own
    name-similarity function and near-exact threshold rather than
    reimplementing name comparison — imported directly (entity_resolution.py
    itself is left untouched, per this section's own constraint) since
    community_detection.py is a new, independent caller of an existing
    private helper, not a reason to widen that module's public surface.
    """
    from src.graph.entity_resolution import NEAR_EXACT_NAME, _name_similarity

    names = [person_names.get(eid, eid) for eid in member_entity_ids]
    representatives: list[str] = []
    for name in names:
        if not any(_name_similarity(name, rep) >= NEAR_EXACT_NAME for rep in representatives):
            representatives.append(name)
    return len(representatives)


# ── Graph reads ──────────────────────────────────────────────────────────

async def fetch_confirmed_same_as() -> list[tuple[str, str]]:
    rows = await age_client.execute_cypher(
        "MATCH (a:Person)-[r:SAME_AS]->(b:Person) "
        "WHERE r.superseded_by IS NULL AND r.status = 'confirmed' "
        "RETURN a.entity_id AS from_id, b.entity_id AS to_id",
        columns=["from_id", "to_id"],
    )
    return [(r["from_id"], r["to_id"]) for r in rows if r["from_id"] and r["to_id"]]


async def _fetch_associated_with() -> list[tuple[str, str, float, Optional[str]]]:
    rows = await age_client.execute_cypher(
        "MATCH (p1:Person)-[r:ASSOCIATED_WITH]->(p2:Person) "
        "WHERE r.superseded_by IS NULL "
        "RETURN p1.entity_id AS p1_id, p2.entity_id AS p2_id, r.confidence AS confidence, r.basis AS basis",
        columns=["p1_id", "p2_id", "confidence", "basis"],
    )
    return [
        (r["p1_id"], r["p2_id"], float(r["confidence"]) if r["confidence"] is not None else 1.0, r.get("basis"))
        for r in rows if r["p1_id"] and r["p2_id"]
    ]


async def _fetch_shared_case_pairs() -> list[tuple[str, str, int]]:
    rows = await age_client.execute_cypher(
        "MATCH (p1:Person)-[b1:BELONGS_TO_CASE]->(c:Case)<-[b2:BELONGS_TO_CASE]-(p2:Person) "
        "WHERE b1.superseded_by IS NULL AND b2.superseded_by IS NULL AND p1.entity_id < p2.entity_id "
        "RETURN p1.entity_id AS p1_id, p2.entity_id AS p2_id, count(DISTINCT c.case_id) AS shared_cases",
        columns=["p1_id", "p2_id", "shared_cases"],
    )
    return [(r["p1_id"], r["p2_id"], int(r["shared_cases"])) for r in rows if r["p1_id"] and r["p2_id"]]


async def fetch_person_case_membership() -> list[tuple[str, str]]:
    """Every (person entity_id, case_id) pair — used to attach case_ids to communities after canonicalization."""
    rows = await age_client.execute_cypher(
        "MATCH (p:Person)-[b:BELONGS_TO_CASE]->(c:Case) "
        "WHERE b.superseded_by IS NULL "
        "RETURN p.entity_id AS entity_id, c.case_id AS case_id",
        columns=["entity_id", "case_id"],
    )
    return [(r["entity_id"], r["case_id"]) for r in rows if r["entity_id"] and r["case_id"]]


def build_canonical_map(same_as_pairs: list[tuple[str, str]]) -> dict[str, str]:
    """
    Collapse confirmed SAME_AS components to one canonical id per component
    — the lexicographically smallest entity_id in the component, for a
    deterministic choice independent of write order. Ids not involved in
    any confirmed SAME_AS edge map to themselves.
    """
    g = nx.Graph()
    g.add_edges_from(same_as_pairs)
    canonical: dict[str, str] = {}
    for component in nx.connected_components(g):
        rep = min(component)
        for node in component:
            canonical[node] = rep
    return canonical


def canon(canonical_map: dict[str, str], entity_id: str) -> str:
    return canonical_map.get(entity_id, entity_id)


# ── Core detection ──────────────────────────────────────────────────────

async def detect_communities() -> dict:
    """
    Run one full community-detection pass: export the Person subgraph from
    AGE, canonicalize through confirmed SAME_AS links, project a
    shared-case co-occurrence signal alongside real ASSOCIATED_WITH edges,
    run Louvain, and replace the stored partition in Postgres.

    Returns a summary dict — {run_id, node_count, edge_count,
    community_count, communities: [{community_id, member_entity_ids,
    case_ids, member_count}]} — the same shape community_summarization.py
    consumes next.
    """
    same_as_pairs = await fetch_confirmed_same_as()
    canonical_map = build_canonical_map(same_as_pairs)

    person_names = await fetch_person_names()
    known_stations = await fetch_known_police_stations()
    contaminated_names = _compute_prefix_contaminated_names(person_names)
    implausible_ids = {
        eid for eid, name in person_names.items()
        if not _is_plausible_person_name(name, known_stations) or name in contaminated_names
    }
    if implausible_ids:
        logger.info(
            "Community detection: excluding %d/%d Person nodes with implausible names "
            "(form-field/administrative-text extraction noise, not real names) before clustering.",
            len(implausible_ids), len(person_names),
        )

    associated_with = await _fetch_associated_with()
    shared_case = await _fetch_shared_case_pairs()
    person_cases = await fetch_person_case_membership()

    # Raw, pre-filter counts — captured before the implausible-name
    # filtering below reassigns these variables. Persisted alongside the
    # post-filter node_count/edge_count so
    # scripts/check_community_staleness.py has a genuinely comparable
    # baseline against the live graph's own raw counts, rather than
    # diffing filtered-vs-raw quantities (an apples-to-oranges comparison
    # that would always show large fake "drift").
    raw_node_count = len(person_names)
    raw_edge_count = len(associated_with) + len(person_cases)

    associated_with = [
        (p1, p2, c, basis) for p1, p2, c, basis in associated_with
        if p1 not in implausible_ids and p2 not in implausible_ids
    ]
    shared_case = [
        (p1, p2, n) for p1, p2, n in shared_case
        if p1 not in implausible_ids and p2 not in implausible_ids
    ]
    person_cases = [
        (eid, case_id) for eid, case_id in person_cases if eid not in implausible_ids
    ]

    g = nx.Graph()

    for p1, p2, confidence, _basis in associated_with:
        c1, c2 = canon(canonical_map, p1), canon(canonical_map, p2)
        if c1 == c2:
            continue  # collapsed to the same canonical person — not a real edge
        w = g[c1][c2]["weight"] if g.has_edge(c1, c2) else 0.0
        g.add_edge(c1, c2, weight=w + _ASSOCIATED_WITH_BASE_WEIGHT * confidence)

    for p1, p2, shared_cases in shared_case:
        c1, c2 = canon(canonical_map, p1), canon(canonical_map, p2)
        if c1 == c2:
            continue
        w = g[c1][c2]["weight"] if g.has_edge(c1, c2) else 0.0
        g.add_edge(c1, c2, weight=w + _SHARED_CASE_WEIGHT_PER_CASE * shared_cases)

    node_count = g.number_of_nodes()
    edge_count = g.number_of_edges()

    if node_count == 0:
        logger.warning("Community detection: no connected Person pairs found — nothing to cluster.")
        partition: list[set[str]] = []
    else:
        partition = louvain_communities(g, weight="weight", seed=42)

    # Canonical-person -> case_ids, aggregated from every mention (pre- and
    # post-canonicalization ids alike) so a community's case list reflects
    # every document/case any of its collapsed mentions touched.
    canon_case_ids: dict[str, set[str]] = defaultdict(set)
    for entity_id, case_id in person_cases:
        canon_case_ids[canon(canonical_map, entity_id)].add(case_id)

    run_id = f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    run_date = datetime.now(timezone.utc).strftime("%Y%m%d")

    communities = []
    for idx, members in enumerate(partition):
        community_id = f"C-{run_date}-{idx:04d}"
        case_ids = sorted(set().union(*(canon_case_ids.get(m, set()) for m in members)))
        member_list = sorted(members)
        distinct_count = await estimate_distinct_person_count(member_list, person_names)
        communities.append({
            "community_id": community_id,
            "member_entity_ids": member_list,
            "case_ids": case_ids,
            "member_count": len(members),
            "distinct_member_count": distinct_count,
        })

    await _persist(run_id, node_count, edge_count, communities, raw_node_count, raw_edge_count)

    return {
        "run_id": run_id,
        "node_count": node_count,
        "edge_count": edge_count,
        "community_count": len(communities),
        "communities": communities,
    }


async def _persist(
    run_id: str, node_count: int, edge_count: int, communities: list[dict],
    raw_node_count: int, raw_edge_count: int,
) -> None:
    async with get_session() as db:
        # Deleting every prior run cascades to community_membership and
        # community_reports (both FK ON DELETE CASCADE to community_runs)
        # — this is what enforces "latest run only" without separate
        # TRUNCATE statements on three tables.
        await db.execute(text("DELETE FROM community_runs"))
        await db.execute(
            text(
                "INSERT INTO community_runs "
                "(run_id, node_count, edge_count, community_count, algorithm, raw_node_count, raw_edge_count) "
                "VALUES (:run_id, :node_count, :edge_count, :community_count, 'louvain', :raw_node_count, :raw_edge_count)"
            ),
            {
                "run_id": run_id, "node_count": node_count, "edge_count": edge_count,
                "community_count": len(communities),
                "raw_node_count": raw_node_count, "raw_edge_count": raw_edge_count,
            },
        )
        for c in communities:
            for entity_id in c["member_entity_ids"]:
                await db.execute(
                    text(
                        "INSERT INTO community_membership (entity_id, community_id, level, run_id) "
                        "VALUES (:entity_id, :community_id, 0, :run_id)"
                    ),
                    {"entity_id": entity_id, "community_id": c["community_id"], "run_id": run_id},
                )
    logger.info(
        "Community detection run %s: %d nodes, %d edges, %d communities.",
        run_id, node_count, edge_count, len(communities),
    )


async def get_associated_with_context(
    member_entity_ids: set[str],
    *,
    canonical_map: Optional[dict[str, str]] = None,
    names: Optional[dict[str, str]] = None,
) -> list[dict]:
    """
    Real ASSOCIATED_WITH edges (with their `basis` text) whose canonical
    endpoints both fall within a given community's member set — supplies
    community_summarization.py with actual extracted-relationship context,
    not just co-membership.

    `canonical_map`/`names` are optional run-wide data a caller iterating
    over many communities in one run can compute ONCE and pass in, instead
    of this function re-deriving them (a fresh SAME_AS/fetch_person_names
    read) on every single call — this was called once per community with
    no caching, the more significant instance of the same per-run-data-
    refetched-per-community inefficiency already fixed in
    community_summarization.py's own loop. Still self-sufficient when
    omitted (re-derives both, as before) — no existing caller breaks.
    _fetch_associated_with() itself is deliberately NOT hoistable the same
    way (it's genuinely per-call-relevant, filtered per member set), so
    it's always freshly fetched here.
    """
    if canonical_map is None:
        same_as_pairs = await fetch_confirmed_same_as()
        canonical_map = build_canonical_map(same_as_pairs)
    if names is None:
        names = await fetch_person_names()
    associated_with = await _fetch_associated_with()

    context = []
    for p1, p2, confidence, basis in associated_with:
        c1, c2 = canon(canonical_map, p1), canon(canonical_map, p2)
        if c1 in member_entity_ids and c2 in member_entity_ids and c1 != c2:
            context.append({
                "person_1": names.get(p1, p1),
                "person_2": names.get(p2, p2),
                "confidence": confidence,
                "basis": basis,
            })
    return context


async def get_latest_run() -> Optional[dict]:
    """The most recent community_runs row, or None if detection has never run."""
    async with get_session() as db:
        res = await db.execute(text(
            "SELECT run_id, computed_at, node_count, edge_count, community_count, algorithm, "
            "raw_node_count, raw_edge_count "
            "FROM community_runs ORDER BY computed_at DESC LIMIT 1"
        ))
        row = res.mappings().first()
        return dict(row) if row else None


# ============================================================
# [Milestone E3 — GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md] Staleness check,
# moved here from scripts/check_community_staleness.py (which now just
# calls this and prints it — same logic, one home, not a duplicate).
#
# Genuinely incremental community DETECTION (only re-clustering the
# subgraph that actually changed, rather than the full Louvain pass
# above) is a real algorithmic undertaking of its own — out of scope
# here, named honestly rather than half-built. What E3 actually delivers
# is moving the TRIGGER from "a human remembers to run
# scripts/check_community_staleness.py, then remembers to run
# detect_communities()/summarize_communities() by hand" to "checked
# automatically after ingestion, using this exact same drift heuristic,
# and re-run automatically when it fires" — see
# src/ingestion/community_refresh_bg.py. detect_communities() itself is
# still a full recompute every time it actually runs; E3 changes WHEN it
# runs, not what it computes.
# ============================================================

# Not tuned against real drift-history data yet (this is still the first
# version of this heuristic) — starting point thresholds, documented as
# such, same disclosure the standalone script always carried.
NODE_DRIFT_WARN_PCT = 0.10
EDGE_DRIFT_WARN_PCT = 0.10


async def _current_raw_node_count() -> int:
    rows = await age_client.execute_cypher(
        "MATCH (p:Person) RETURN count(p) AS c", columns=["c"],
    )
    return int(rows[0]["c"]) if rows else 0


async def _current_raw_edge_count() -> int:
    # ASSOCIATED_WITH + BELONGS_TO_CASE (Person only) — the same two edge
    # sources detect_communities() itself measures into
    # raw_node_count/raw_edge_count above, for a genuinely comparable
    # baseline.
    rows = await age_client.execute_cypher(
        "MATCH (:Person)-[r:ASSOCIATED_WITH]->(:Person) WHERE r.superseded_by IS NULL "
        "RETURN count(r) AS c",
        columns=["c"],
    )
    associated_with = int(rows[0]["c"]) if rows else 0
    rows2 = await age_client.execute_cypher(
        "MATCH (:Person)-[r:BELONGS_TO_CASE]->(:Case) WHERE r.superseded_by IS NULL "
        "RETURN count(r) AS c",
        columns=["c"],
    )
    belongs_to_case = int(rows2[0]["c"]) if rows2 else 0
    return associated_with + belongs_to_case


async def get_staleness() -> dict:
    """
    Compare the live graph's current raw Person node/edge counts against
    the raw counts recorded at the last community_runs row, and report
    whether drift has crossed NODE_DRIFT_WARN_PCT/EDGE_DRIFT_WARN_PCT.

    Returns {"stale": bool, "reason": str, "last_run_id": Optional[str],
    "node_drift": Optional[float], "edge_drift": Optional[float],
    "current_raw_nodes": int, "current_raw_edges": int,
    "prior_raw_nodes": Optional[int], "prior_raw_edges": Optional[int]}.

    `stale=True` with `last_run_id=None` means detection has never run at
    all — there is no partition to be stale, but there is also nothing
    for a query to draw on, so this is reported as "needs a run" the same
    way an out-of-date partition is, not as a separate third state a
    caller would have to special-case.
    """
    last_run = await get_latest_run()
    current_raw_nodes = await _current_raw_node_count()
    current_raw_edges = await _current_raw_edge_count()

    if last_run is None:
        return {
            "stale": True, "reason": "no community detection run found yet",
            "last_run_id": None, "node_drift": None, "edge_drift": None,
            "current_raw_nodes": current_raw_nodes, "current_raw_edges": current_raw_edges,
            "prior_raw_nodes": None, "prior_raw_edges": None,
        }

    prior_raw_nodes = last_run.get("raw_node_count")
    prior_raw_edges = last_run.get("raw_edge_count")
    if prior_raw_nodes is None or prior_raw_edges is None:
        # A run from before migration 017 has no raw counts to compare
        # against — fail toward "recommend a re-run" rather than
        # crashing on a None/int comparison or silently skipping.
        return {
            "stale": True, "reason": "prior run predates raw-count tracking (migration 017)",
            "last_run_id": last_run["run_id"], "node_drift": None, "edge_drift": None,
            "current_raw_nodes": current_raw_nodes, "current_raw_edges": current_raw_edges,
            "prior_raw_nodes": None, "prior_raw_edges": None,
        }

    node_drift = abs(current_raw_nodes - prior_raw_nodes) / max(prior_raw_nodes, 1)
    edge_drift = abs(current_raw_edges - prior_raw_edges) / max(prior_raw_edges, 1)
    stale = node_drift >= NODE_DRIFT_WARN_PCT or edge_drift >= EDGE_DRIFT_WARN_PCT

    return {
        "stale": stale,
        "reason": f"node drift {node_drift:.1%}, edge drift {edge_drift:.1%}" if stale else "within threshold",
        "last_run_id": last_run["run_id"],
        "node_drift": node_drift, "edge_drift": edge_drift,
        "current_raw_nodes": current_raw_nodes, "current_raw_edges": current_raw_edges,
        "prior_raw_nodes": prior_raw_nodes, "prior_raw_edges": prior_raw_edges,
    }
