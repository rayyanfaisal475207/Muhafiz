# ============================================================
# Entity-resolution continuous sampling — Ingestion Quality Control at
# Scale, Module G3 (see INGESTION_QUALITY_AT_SCALE_PLAN.md).
#
# Extends scripts/eval_entity_resolution.py's ground-truth-driven
# precision/recall idea from a manually-invoked, point-in-time script
# into a background job: periodically samples a random subset of
# recently resolved SAME_AS matches and re-checks each one's original
# scoring signal against the graph's CURRENT state, looking for signs
# that corroborating evidence has since gone inconsistent.
#
# ── HARD RULE THIS MODULE OBEYS, THE ONE TO TEST MOST AGGRESSIVELY ──────
# This module NEVER imports src.graph.versioning or the write side of
# age_client. It cannot write a SAME_AS/CITES edge, confirmed, rejected,
# or otherwise, structurally — the same "cannot do the risky thing even
# by mistake" discipline candidate_reprioritization.py established for
# D1. Its only writes are to entity_resolution_consistency_findings
# (migration 029), a plain Postgres side table that is not a graph edge
# and carries no `status` a downstream reader could mistake for a
# confirm/reject decision.
#
# ── SAMPLING POPULATION, DECIDED AGAINST THE REAL GRAPH SHAPE ───────────
# Confirmed live against muhafiz-postgres (not assumed from the plan's
# prose): entity_resolution.resolve_and_write() only ever writes a
# SAME_AS edge for TIER_FLAGGED/TIER_REVIEW decisions (see that
# function's own tier branch) — TIER_CNIC_AUTO merges directly into the
# existing node and create NO edge at all. So this module's population is
# every non-rejected SAME_AS edge: still-pending flagged_unverified/
# human_review candidates, AND already-confirmed ones (found via the
# CONFIRMED edge's own superseded predecessor, which AGE never deletes —
# `MATCH ()-[old]->() WHERE old.superseded_by = $confirmed_edge_id`,
# verified live to return the original pending edge's full scoring
# snapshot intact). Rejected edges are excluded — a human already said
# "not the same person"; re-litigating a dead candidate teaches nothing.
#
# cnic_auto merges are OUT OF SCOPE, stated honestly rather than
# papered over: versioning.write_node() MERGEs and overwrites properties
# on every write, with no per-write history log anywhere in this
# codebase — there is nothing to diff a cnic_auto merge's current CNIC
# against without building a new node-history sidelog, a real, separate
# capability this module was not sized to invent (see migration 029's
# own header for the same disclosure).
#
# ── DEGRADATION, THE MIRROR OF D1's REINFORCEMENT ───────────────────────
# candidate_reprioritization.py's _fresh_signal()/_why() already compute
# "does this candidate look MORE corroborated now than at write time" —
# reused here directly, never reimplemented. This module asks the
# opposite question: does it look LESS corroborated — a structured-id or
# shared-case signal that held at write time and no longer does, or a
# name-similarity drop of the same 0.05 magnitude D1's own reinforcement
# threshold uses (flipped sign, not a new invented number).
#
# ── EXECUTION MODEL ──────────────────────────────────────────────────────
# Same fire-and-forget-task-at-ingestion-time shape as D1
# (src/ingestion/reprioritization_bg.py) / E3
# (src/ingestion/community_refresh_bg.py) — confirmed a fourth time
# against current code that this codebase still has no standalone
# worker/cron. Unlike D1/E3, this module's sample is deliberately GLOBAL
# (not scoped to the case that was just ingested) — the population it
# audits (already-resolved matches) has no natural relationship to
# whichever case triggered this particular ingestion — so each trigger
# is throttled by SAMPLE_TRIGGER_PROBABILITY rather than running its full
# sample on every single ingest (see src/ingestion/
# entity_resolution_sampling_bg.py).
# ============================================================

from __future__ import annotations

import logging
import random
from typing import Optional

from sqlalchemy import text

from src.database.postgres import get_session
from src.graph import age_client
from src.graph.candidate_reprioritization import _fetch_cases_for, _fetch_node_by_entity_id, _fresh_signal

logger = logging.getLogger(__name__)

# How likely a single ingestion-triggered call is to actually run a
# sampling pass — this is a whole-corpus audit, not a per-case one, so it
# does not need to fire on every document (see module docstring's
# EXECUTION MODEL section). A starting point, not tuned against real
# traffic volume yet — same disclosure this plan's other starting-point
# numbers (G2's thresholds, get_staleness()'s drift %) already carry.
SAMPLE_TRIGGER_PROBABILITY = 0.2

# How many of the most-recently-touched candidates to draw the random
# sample from, and how many to actually check per pass.
RECENT_WINDOW = 500
SAMPLE_SIZE = 20

# Mirrors candidate_reprioritization._REINFORCEMENT_BONUS's own 0.05
# name-similarity delta that counts as a real move, sign-flipped for
# degradation instead of reinforcement.
NAME_SIMILARITY_DEGRADED_DELTA = -0.05


async def _recent_candidates() -> list[dict]:
    """
    Every live (superseded_by IS NULL) SAME_AS edge that is either still
    pending or already confirmed, newest first, capped at RECENT_WINDOW —
    the population random.sample() draws from. Rejected edges are
    excluded (see module docstring).
    """
    rows = await age_client.execute_cypher(
        "MATCH (a)-[r:SAME_AS]->(b) "
        "WHERE r.superseded_by IS NULL AND r.status IN ['pending', 'confirmed'] "
        "RETURN a, r, b "
        "ORDER BY r.as_of DESC "
        "LIMIT $limit",
        params={"limit": RECENT_WINDOW},
        columns=["a", "r", "b"],
    )
    return rows


async def _original_signal(edge_id: int, status: str) -> Optional[dict]:
    """
    The scoring snapshot taken at write time. For a still-pending edge,
    that's the edge's own properties. For a confirmed edge (whose own
    properties are just {tier, basis, status, reviewed_by} — see
    graph_review.py's confirm_match()), walk back to the pending edge it
    superseded, which AGE never deletes.
    """
    if status == "pending":
        return None  # caller already has the edge's own properties
    rows = await age_client.execute_cypher(
        "MATCH ()-[old:SAME_AS]->() WHERE old.superseded_by = $edge_id RETURN old",
        params={"edge_id": edge_id}, columns=["old"],
    )
    if not rows:
        return None
    return rows[0]["old"].get("properties", {})


def _is_degraded(original: dict, fresh: dict) -> Optional[str]:
    """
    Deterministic one-line reason a candidate looks LESS corroborated now
    than at write time, or None if it doesn't. A template over fields,
    never LLM narration — same discipline
    candidate_reprioritization._why() already established.
    """
    lost_structured = bool(original.get("shared_structured_id")) and not fresh["shared_structured_id"]
    lost_case = bool(original.get("shared_case")) and not fresh["shared_case"]
    orig_sim = original.get("name_similarity")
    sim_delta = fresh["name_similarity"] - orig_sim if orig_sim is not None else 0.0
    sim_degraded = sim_delta <= NAME_SIMILARITY_DEGRADED_DELTA

    reasons = []
    if lost_structured:
        reasons.append("no longer shares a structured identifier (phone/plate/father's name) it shared at write time")
    if lost_case:
        reasons.append("no longer shares a case it shared at write time")
    if sim_degraded:
        reasons.append(f"name similarity dropped to {fresh['name_similarity']:.2f} ({sim_delta:+.2f}) since write time")

    if not reasons:
        return None
    return "Corroborating evidence has become inconsistent: " + "; ".join(reasons) + "."


async def _record_finding(
    edge_id: int, tier: Optional[str], status: str, mention_id: str, candidate_id: str,
    original: dict, fresh: dict, reason: str,
) -> None:
    try:
        async with get_session() as db:
            await db.execute(
                text(
                    "INSERT INTO entity_resolution_consistency_findings "
                    "(edge_id, tier, status_at_detection, mention_entity_id, candidate_entity_id, "
                    "original_basis, original_name_similarity, original_shared_case, original_shared_structured_id, "
                    "fresh_name_similarity, fresh_shared_case, fresh_shared_structured_id, finding_reason) "
                    "VALUES (:edge_id, :tier, :status, :mention_id, :candidate_id, "
                    ":original_basis, :original_name_similarity, :original_shared_case, :original_shared_structured_id, "
                    ":fresh_name_similarity, :fresh_shared_case, :fresh_shared_structured_id, :reason) "
                    "ON CONFLICT (edge_id, acknowledged) DO UPDATE SET "
                    "fresh_name_similarity = EXCLUDED.fresh_name_similarity, "
                    "fresh_shared_case = EXCLUDED.fresh_shared_case, "
                    "fresh_shared_structured_id = EXCLUDED.fresh_shared_structured_id, "
                    "finding_reason = EXCLUDED.finding_reason, "
                    "detected_at = now() "
                    "WHERE entity_resolution_consistency_findings.acknowledged = false"
                ),
                {
                    "edge_id": edge_id, "tier": tier, "status": status,
                    "mention_id": mention_id, "candidate_id": candidate_id,
                    "original_basis": original.get("basis"),
                    "original_name_similarity": original.get("name_similarity"),
                    "original_shared_case": original.get("shared_case"),
                    "original_shared_structured_id": original.get("shared_structured_id"),
                    "fresh_name_similarity": fresh["name_similarity"],
                    "fresh_shared_case": fresh["shared_case"],
                    "fresh_shared_structured_id": fresh["shared_structured_id"],
                    "reason": reason,
                },
            )
    except Exception as exc:
        logger.error("entity_resolution_sampling: failed to record finding for edge %s: %s", edge_id, exc)


async def run_sample(sample_size: int = SAMPLE_SIZE) -> dict:
    """
    One sampling pass: draw up to `sample_size` candidates at random from
    the recent live-SAME_AS population, re-check each against the
    graph's current state, and record a finding for every one that looks
    degraded. Returns {"sampled": int, "findings": int}. Best-effort —
    a failure partway through must never raise into the ingestion run
    that triggered it (see the background wrapper).
    """
    candidates = await _recent_candidates()
    if not candidates:
        return {"sampled": 0, "findings": 0}

    sample = random.sample(candidates, k=min(sample_size, len(candidates)))

    entity_ids = sorted({row["a"]["properties"]["entity_id"] for row in sample} |
                         {row["b"]["properties"]["entity_id"] for row in sample})
    cases_by_entity = await _fetch_cases_for(entity_ids)

    findings = 0
    for row in sample:
        edge = row["r"]
        edge_id = edge["id"]
        props = edge.get("properties", {})
        status = props.get("status")
        tier = props.get("tier")
        mention_props = row["a"].get("properties", {})
        candidate_props = row["b"].get("properties", {})

        original = props if status == "pending" else await _original_signal(edge_id, status)
        if original is None:
            # A confirmed edge whose superseded predecessor could not be
            # found (shouldn't happen — AGE never deletes edges — but
            # never crash the whole pass over one unexpected row).
            continue

        # Re-fetch both nodes' CURRENT properties — they may have
        # accumulated more evidence (or, for the mention side, may still
        # be exactly what was written; a mention-tier node's own
        # canonical_name/cnic are never edited in place, but re-fetching
        # here means this check reads whatever IS there now, not a stale
        # copy from the edge's own properties).
        fresh_mention = await _fetch_node_by_entity_id(mention_props.get("entity_id"))
        fresh_candidate = await _fetch_node_by_entity_id(candidate_props.get("entity_id"))
        if not fresh_mention or not fresh_candidate:
            continue

        fresh = _fresh_signal(
            fresh_mention.get("properties", {}), fresh_candidate.get("properties", {}), cases_by_entity,
        )
        reason = _is_degraded(original, fresh)
        if reason is None:
            continue

        await _record_finding(
            edge_id, tier, status,
            mention_props.get("entity_id"), candidate_props.get("entity_id"),
            original, fresh, reason,
        )
        findings += 1

    return {"sampled": len(sample), "findings": findings}
