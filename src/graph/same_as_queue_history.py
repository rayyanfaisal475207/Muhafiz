# ============================================================
# Pending SAME_AS review-queue backlog — snapshot history
# (GRAPH_QUALITY_VISIBILITY_FIX_PROMPT.md, Feature A; migration 030).
#
# WHY THIS EXISTS: src/api/graph_review.py's GET /stats already computes
# a live tier x status snapshot of every SAME_AS edge, but it's read-time
# only — nothing persists it, so "is the pending backlog actually
# shrinking" cannot be answered without manually diffing two runs of that
# query by hand. This module writes the SAME shape into
# same_as_queue_snapshot (migration 030), turning one point-in-time read
# into a queryable time series.
#
# EXECUTION MODEL: this codebase has no standalone scheduled worker/cron
# — see src/graph/candidate_reprioritization.py's own module docstring,
# which resolved this exact question for Milestone D1 and is the
# precedent this module follows, not a new decision. The primary path is
# the on-demand script (scripts/snapshot_same_as_queue.py) — a human or
# an external scheduler (Windows Task Scheduler, cron outside this repo)
# invokes it periodically. No incremental/piggyback path is added here;
# build one later only if the on-demand script alone proves insufficient.
#
# READ-ONLY AGAINST AGE, WRITE-ONLY TO ITS OWN POSTGRES TABLE: same "adds
# visibility, never a new judgment call" discipline ingestion_quality.py's
# own header states for Module G1. This module never touches a SAME_AS
# edge, never calls versioning.write_edge, and is structurally incapable
# of confirming/rejecting anything.
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

from src.database.postgres import get_session
from src.graph import age_client

logger = logging.getLogger(__name__)


async def _fetch_edges() -> list[dict]:
    """
    Every non-superseded SAME_AS edge, with the DISTINCT case_id(s)
    either endpoint belongs to.

    Deliberately NOT src/api/graph_review.py::list_pending()'s own plain
    `OPTIONAL MATCH (a)-[:BELONGS_TO_CASE]->(ca:Case)` join, unaggregated
    — confirmed live against this graph that some Person nodes carry many
    (measured: 131) separate, non-superseded BELONGS_TO_CASE edges to the
    SAME case (a leftover from the pre-d5fa333 replay bug: every mention
    resolution wrote a fresh membership edge, never deduped or
    superseded). An unaggregated join multiplies every SAME_AS row by
    that count — measured live: 2,583 real edges turned into 10,975
    joined rows, one single edge alone appearing 131 times. `collect
    (DISTINCT ...)` inside a `WITH` between the two OPTIONAL MATCHes
    collapses each side's case ids to a set BEFORE the second join can
    multiply against it, so this returns exactly one row per SAME_AS
    edge regardless of how many redundant BELONGS_TO_CASE edges either
    endpoint carries. (list_pending() has the same latent multiplication
    — flagged separately, out of scope for this module to fix.)
    """
    rows = await age_client.execute_cypher(
        "MATCH (a)-[r:SAME_AS]->(b) "
        "WHERE r.superseded_by IS NULL "
        "OPTIONAL MATCH (a)-[:BELONGS_TO_CASE]->(ca:Case) "
        "WITH r, b, collect(DISTINCT ca.case_id) AS a_case_ids "
        "OPTIONAL MATCH (b)-[:BELONGS_TO_CASE]->(cb:Case) "
        "WITH r, a_case_ids, collect(DISTINCT cb.case_id) AS b_case_ids "
        "RETURN r, a_case_ids, b_case_ids",
        columns=["r", "a_case_ids", "b_case_ids"],
    )
    return rows


def _aggregate(rows: list[dict]) -> dict[Optional[str], dict[tuple[str, str], int]]:
    """
    case_id -> (tier, status) -> count, plus a `None` key for the global
    (cross-case) rollup — every edge counts toward both its own case_id(s)
    row(s) AND the global row, matching /stats' existing "one number for
    the whole queue" semantics while still giving per-case breakdowns.

    An edge whose two endpoints belong to DIFFERENT cases (a genuine
    cross-case candidate — the P-006 repeat-offender shape) counts toward
    BOTH case rows, not neither and not an arbitrary pick of one — it is
    really sitting in both cases' queues from a reviewer's point of view.
    """
    by_scope: dict[Optional[str], dict[tuple[str, str], int]] = {None: {}}
    for row in rows:
        props = row["r"].get("properties", {})
        tier = props.get("tier") or "unknown"
        status = props.get("status") or "unknown"
        key = (tier, status)

        by_scope[None][key] = by_scope[None].get(key, 0) + 1

        case_ids = set(row.get("a_case_ids") or []) | set(row.get("b_case_ids") or [])
        case_ids.discard(None)
        for case_id in case_ids:
            bucket = by_scope.setdefault(case_id, {})
            bucket[key] = bucket.get(key, 0) + 1

    return by_scope


async def write_snapshot() -> int:
    """
    Compute the current tier x status breakdown, globally and per case,
    and insert one row per (case_id, tier, status) tuple — including the
    case_id IS NULL global rollup — all stamped with the same
    snapshot_at. Returns the number of rows written.

    Best-effort on the write side, same resilience contract as
    ingestion_quality.start_run(): a failure to persist the snapshot must
    never be treated as "the backlog is empty" by a caller — it raises,
    so scripts/snapshot_same_as_queue.py's own exit code reflects a real
    failure rather than silently writing nothing.
    """
    rows = await _fetch_edges()
    by_scope = _aggregate(rows)

    insert_rows = [
        {"case_id": case_id, "tier": tier, "status": status, "edge_count": count}
        for case_id, buckets in by_scope.items()
        for (tier, status), count in buckets.items()
    ]

    if not insert_rows:
        return 0

    async with get_session() as db:
        await db.execute(
            text(
                "INSERT INTO same_as_queue_snapshot (case_id, tier, status, edge_count) "
                "VALUES (:case_id, :tier, :status, :edge_count)"
            ),
            insert_rows,
        )

    return len(insert_rows)


async def read_history(case_id: Optional[str] = None, days: int = 30) -> list[dict]:
    """
    Snapshot rows for the last `days` days, most recent first — the read
    shape GET /queue/history serves. `case_id=None` returns the global
    rollup history (case_id IS NULL rows only), matching write_snapshot()'s
    own convention that NULL is the global scope, never "any case".
    """
    async with get_session() as db:
        result = await db.execute(
            text(
                "SELECT snapshot_at, case_id, tier, status, edge_count "
                "FROM same_as_queue_snapshot "
                "WHERE snapshot_at > now() - make_interval(days => :days) "
                "  AND case_id IS NOT DISTINCT FROM :case_id "
                "ORDER BY snapshot_at DESC"
            ),
            {"days": days, "case_id": case_id},
        )
        return [dict(row._mapping) for row in result]
