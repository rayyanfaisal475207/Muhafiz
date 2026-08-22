# ============================================================
# Pending-candidate reprioritization — Milestone D1
# (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — queue-scale resolution).
#
# THE HARD RULE THIS MODULE OBEYS: it never calls
# src.graph.versioning.write_edge(). It has no code path that can write
# a SAME_AS/CITES edge, confirmed or rejected — only a human, through
# src/api/graph_review.py's existing confirm/reject endpoints, does that.
# This module ONLY reads AGE (via age_client / entity_resolution's own
# scoring helpers) and writes to the Postgres side table
# (pending_candidate_priority.update_priority()) — see that module's
# header for why that split makes "D1 never auto-decides" a structural
# property of the code, not a promise kept by convention.
#
# What this module DOES do, both non-destructive:
#   - REORDER: recompute each pending candidate's priority_score against
#     the graph's CURRENT state (not the state at original-match time)
#     and a deterministic one-line "why" — built from the same
#     Candidate-scoring fields entity_resolution.py already computes
#     (name_similarity, shared_case, shared_structured_id), NEVER an LLM
#     narration (Milestone D1's point 1).
#   - GROUP: connected-components clustering of pending SAME_AS
#     candidates that share an underlying signal — same target entity,
#     same mention entity, a shared structured-id value, or a shared
#     case (Milestone D1's point 2) — so a supervisor can batch-review
#     them through graph_review.py's new /queue/batches endpoints.
#   - DEPRIORITIZE (sink, never delete/reject): a candidate with no new
#     corroboration since its last scoring, old enough to call stale,
#     sinks toward the bottom of the ordered queue. This asserts nothing
#     about identity — it only reduces attention, unlike confirm/reject.
#
# EXECUTION MODEL (Milestone D1's point 3 — resolved, not left implicit):
# this codebase has no standalone scheduled worker/cron; every existing
# "background" task is a per-request `asyncio.create_task()` tied to a
# chat/ingestion request's lifecycle (src/ingestion/conflict_bg.py is the
# direct precedent this module's own background wrapper,
# src/ingestion/reprioritization_bg.py, copies). Rather than invent new
# worker infrastructure this plan never asked for, D1 uses TWO paths that
# both already fit the codebase's existing shape:
#   1. INCREMENTAL, automatic: scheduled as a fire-and-forget task right
#      after a document's graph extraction/resolution completes (same
#      call site as conflict detection), scoped to just that case's own
#      pending candidates — cheap, and it's exactly the moment "newly
#      ingested evidence" (the plan's own trigger condition) exists.
#   2. FULL SWEEP, manual: a supervisor-triggered POST endpoint
#      (graph_review.py's /queue/reprioritize) that re-scores the entire
#      queue on demand — this is what covers staleness deprioritization
#      for candidates that never see new ingestion touch them again,
#      since there is no cron to run that sweep on a schedule. Honest
#      about the gap rather than papering over it with invented infra.
# ============================================================

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.graph import age_client, pending_candidate_priority
from src.graph.entity_resolution import _has_shared_structured_id, _name_similarity

logger = logging.getLogger(__name__)

# A candidate re-scored with no new corroboration, and old enough that
# "no news" stops being "just hasn't been looked at yet" and starts being
# a real signal that nothing is going to change — sinks in the ordering.
# Not a deletion, not a rejection: purely an attention-ordering signal,
# per the plan's explicit "asserts nothing false" reasoning for why this
# one system-driven action is safe without a human.
STALE_AFTER = timedelta(days=14)

# Weights mirror entity_resolution._generate_candidates()'s own scoring
# formula exactly (name_sim*0.7 + 0.15 shared_structured + 0.15
# shared_case) — D1 reuses the SAME scoring shape investigators already
# see in the original `basis` text, rather than inventing a second,
# divergent notion of "how strong is this match". The +0.10 reinforcement
# bonus is D1's only addition: a candidate that gained NEW corroboration
# since it was first written should visibly outrank an equally-scored but
# unreinforced one in the review queue.
_REINFORCEMENT_BONUS = 0.10
_MAX_SCORE = 0.95


def _fresh_signal(mention_props: dict, candidate_props: dict, cases_by_entity: dict) -> dict:
    mention_id = mention_props.get("entity_id")
    candidate_id = candidate_props.get("entity_id")
    shared_case = bool(
        cases_by_entity.get(mention_id, set()) & cases_by_entity.get(candidate_id, set())
    )
    return {
        "name_similarity": _name_similarity(
            mention_props.get("canonical_name", ""), candidate_props.get("canonical_name", "")
        ),
        "shared_case": shared_case,
        "shared_structured_id": _has_shared_structured_id(mention_props, candidate_props),
    }


def _why(tier: Optional[str], original: dict, fresh: dict) -> str:
    """Deterministic one-line "why" — a template over scoring fields, never LLM narration. See module docstring point 1."""
    new_structured = fresh["shared_structured_id"] and not original.get("shared_structured_id")
    new_case = fresh["shared_case"] and not original.get("shared_case")
    orig_sim = original.get("name_similarity")
    sim_delta = fresh["name_similarity"] - orig_sim if orig_sim is not None else 0.0

    reinforcements = []
    if new_structured:
        reinforcements.append("a newly matching structured identifier (phone/plate/father's name)")
    if new_case:
        reinforcements.append("a newly shared case link")
    if sim_delta >= 0.05:
        reinforcements.append(f"name similarity now {fresh['name_similarity']:.2f} (+{sim_delta:.2f})")

    tier_label = tier or "original"
    if reinforcements:
        return f"Reinforced since the {tier_label} match by " + " and ".join(reinforcements) + "."
    if fresh["shared_case"] or fresh["shared_structured_id"]:
        still = []
        if fresh["shared_structured_id"]:
            still.append("a shared structured identifier")
        if fresh["shared_case"]:
            still.append("a shared case")
        return f"Still corroborated by {' and '.join(still)}; no new signal since the {tier_label} match."
    return f"No corroborating signal beyond the {tier_label} match (name similarity {fresh['name_similarity']:.2f})."


def _score(fresh: dict, reinforced: bool) -> float:
    raw = (
        fresh["name_similarity"] * 0.7
        + (0.15 if fresh["shared_structured_id"] else 0.0)
        + (0.15 if fresh["shared_case"] else 0.0)
        + (_REINFORCEMENT_BONUS if reinforced else 0.0)
    )
    return min(raw, _MAX_SCORE)


async def _fetch_node_by_entity_id(entity_id: str, *, graph: str = age_client.GRAPH_NAME) -> Optional[dict]:
    """
    Label-less lookup — the priority-index row doesn't carry the node's
    graph label (only entity_id), and entity_id is unique across labels
    (uuid-scoped per TYPE_TO_LABEL, see entity_resolution._new_entity_id).
    A handful of pending candidates at a time (queue volume, not corpus
    volume) makes this an acceptable per-pair lookup — same "dozens, not
    thousands" scale entity_resolution.py's own N+1 comment already
    accepts for candidate generation.
    """
    rows = await age_client.execute_cypher(
        "MATCH (n) WHERE n.entity_id = $id RETURN n LIMIT 1",
        params={"id": entity_id}, columns=["n"], graph=graph,
    )
    return rows[0]["n"] if rows else None


async def _fetch_cases_for(entity_ids: list[str], *, graph: str = age_client.GRAPH_NAME) -> dict[str, set]:
    if not entity_ids:
        return {}
    rows = await age_client.execute_cypher(
        "MATCH (n)-[:BELONGS_TO_CASE]->(c:Case) WHERE n.entity_id IN $ids "
        "RETURN n.entity_id AS eid, c.case_id AS cid",
        params={"ids": entity_ids}, columns=["eid", "cid"], graph=graph,
    )
    out: dict[str, set] = {}
    for row in rows:
        if row.get("eid") and row.get("cid"):
            out.setdefault(row["eid"], set()).add(row["cid"])
    return out


def _now() -> datetime:
    """A real `datetime`, not an ISO string — see pending_candidate_priority.update_priority()'s docstring for why asyncpg requires the native type here."""
    return datetime.now(timezone.utc)


class _UnionFind:
    """Plain union-find over pending SAME_AS edge_ids — Milestone D1's point 2 grouping algorithm."""

    def __init__(self, ids: list[int]):
        self._parent = {i: i for i in ids}

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)


async def reprioritize_same_as(edge_ids: list[int]) -> int:
    """
    Re-score, re-explain, and re-group the given pending SAME_AS
    edge_ids. Returns the count of rows actually updated (an edge_id
    already resolved by a human between listing and scoring is simply
    skipped — see pending_candidate_priority.update_priority()'s return
    contract).

    GROUPING (point 2): two pending SAME_AS candidates are unioned into
    the same batch when they share ANY of — the same candidate/target
    entity (b_key), the same mention entity (a_key — not produced by
    today's writer, which emits at most one pending SAME_AS per mention,
    but kept for completeness/future writers), a shared structured-id
    value between their mention nodes, or a shared case between their
    mention nodes. This is the connected-components algorithm the plan
    asked to be made concrete before D1 could be built.
    """
    if not edge_ids:
        return 0

    rows = await pending_candidate_priority.list_rows("SAME_AS")
    rows_by_id = {r["edge_id"]: r for r in rows if r["edge_id"] in set(edge_ids)}
    if not rows_by_id:
        return 0

    entity_ids = sorted({r["a_key"] for r in rows_by_id.values()} | {r["b_key"] for r in rows_by_id.values()})
    nodes: dict[str, dict] = {}
    for eid in entity_ids:
        node = await _fetch_node_by_entity_id(eid)
        if node:
            nodes[eid] = node.get("properties", {}) or {}
    cases_by_entity = await _fetch_cases_for(entity_ids)

    # ── Pass 1: score + explain each candidate against the graph's
    # CURRENT state, diffed against the ORIGINAL scoring snapshot stored
    # on the row at write time (migration 027 / ResolutionDecision) ────
    computed: dict[int, dict] = {}
    for edge_id, row in rows_by_id.items():
        mention_props = nodes.get(row["a_key"])
        candidate_props = nodes.get(row["b_key"])
        if not mention_props or not candidate_props:
            # One side no longer resolvable (shouldn't happen for a
            # pending edge — nodes are never deleted — but never crash
            # the whole sweep over one bad row).
            continue

        fresh = _fresh_signal(mention_props, candidate_props, cases_by_entity)
        original = {
            "shared_case": row.get("original_shared_case"),
            "shared_structured_id": row.get("original_shared_structured_id"),
            "name_similarity": row.get("original_name_similarity"),
        }
        reinforced = (
            (fresh["shared_structured_id"] and not original["shared_structured_id"])
            or (fresh["shared_case"] and not original["shared_case"])
            or (original["name_similarity"] is not None and fresh["name_similarity"] - original["name_similarity"] >= 0.05)
        )
        was_scored_before = row.get("last_scored_at") is not None
        stale = (
            was_scored_before
            and not reinforced
            and (datetime.now(timezone.utc) - _as_utc(row.get("created_at"))) > STALE_AFTER
        )
        computed[edge_id] = {
            "score": _score(fresh, reinforced),
            "why": _why(row.get("tier"), original, fresh),
            "deprioritized": bool(stale),
        }

    # ── Pass 2: group via connected components (point 2) ───────────
    uf = _UnionFind(list(rows_by_id.keys()))
    by_b: dict[str, list[int]] = {}
    by_a: dict[str, list[int]] = {}
    for edge_id, row in rows_by_id.items():
        by_b.setdefault(row["b_key"], []).append(edge_id)
        by_a.setdefault(row["a_key"], []).append(edge_id)
    for group in (*by_b.values(), *by_a.values()):
        for other in group[1:]:
            uf.union(group[0], other)

    # Shared structured-id value or shared case between two DIFFERENT
    # mention entities also joins their candidates into one batch — the
    # "same underlying signal, different candidate" case the plan's own
    # "shared CNIC-adjacent value / shared address / shared case" example
    # names explicitly.
    edge_ids_list = list(rows_by_id.keys())
    for i, e1 in enumerate(edge_ids_list):
        a1 = rows_by_id[e1]["a_key"]
        props1 = nodes.get(a1)
        if not props1:
            continue
        for e2 in edge_ids_list[i + 1:]:
            a2 = rows_by_id[e2]["a_key"]
            if a1 == a2:
                continue
            props2 = nodes.get(a2)
            if not props2:
                continue
            if _has_shared_structured_id(props1, props2) or (
                cases_by_entity.get(a1, set()) & cases_by_entity.get(a2, set())
            ):
                uf.union(e1, e2)

    # ── Pass 3: one write per edge, all four owned columns together ──
    updated = 0
    for edge_id, result in computed.items():
        ok = await pending_candidate_priority.update_priority(
            edge_id, priority_score=result["score"], why=result["why"],
            group_id=f"GROUP-SAME_AS-{uf.find(edge_id)}",
            deprioritized=result["deprioritized"], last_scored_at=_now(),
        )
        if ok:
            updated += 1

    return updated


def _as_utc(value) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def reprioritize_case(case_id: str) -> int:
    """
    Incremental path (execution model #1): re-score every pending SAME_AS
    candidate touching an entity that belongs to `case_id`, plus every
    pending CITES candidate touching `case_id` directly. Scheduled as a
    fire-and-forget task right after that case's document finishes graph
    extraction — see src/ingestion/reprioritization_bg.py.
    """
    same_as_rows = await pending_candidate_priority.list_rows("SAME_AS")
    if same_as_rows:
        case_entities = await _fetch_cases_for(
            list({r["a_key"] for r in same_as_rows} | {r["b_key"] for r in same_as_rows})
        )
        touching = [
            r["edge_id"] for r in same_as_rows
            if case_id in case_entities.get(r["a_key"], set()) or case_id in case_entities.get(r["b_key"], set())
        ]
        same_as_updated = await reprioritize_same_as(touching) if touching else 0
    else:
        same_as_updated = 0

    cites_ids = await pending_candidate_priority.cites_edge_ids_touching_case(case_id)
    # CITES has no per-pair Candidate-style scoring breakdown (Case<->Case
    # prose citations, not entity mentions) — its queue entry still gets
    # reordered by recency/confidence via list_rows()'s own ORDER BY;
    # only SAME_AS gets D1's re-scoring/grouping treatment, per the
    # plan's own Candidate-dataclass-specific point 1.
    logger.info(
        "reprioritize_case(%s): rescored %d SAME_AS candidates, %d CITES candidates untouched by re-scoring",
        case_id, same_as_updated, len(cites_ids),
    )
    return same_as_updated


async def reprioritize_all() -> int:
    """Full-sweep path (execution model #2) — manually triggered by a supervisor via graph_review.py's /queue/reprioritize. Re-scores the entire pending SAME_AS queue; also catches staleness for candidates no new ingestion has touched since."""
    edge_ids = await pending_candidate_priority.list_all_edge_ids("SAME_AS")
    return await reprioritize_same_as(edge_ids)
