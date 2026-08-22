# ============================================================
# Pending-candidate priority index — Postgres-backed shadow table for
# every PENDING SAME_AS/CITES edge (Graph Scale & Schema Expansion,
# Milestone D1). See migrations/027_pending_candidate_priority.sql's
# header for the full design rationale — same "side table alongside
# AGE, never a replacement" discipline as identity_index.py (A1).
#
# Two callers, two different levels of trust:
#   - src/graph/versioning.py's write_edge() is the ONLY thing that
#     inserts or deletes rows here — a row's existence always mirrors a
#     real pending edge in AGE (inserted the moment one is written,
#     deleted the moment a human confirms/rejects it). This module never
#     decides that on its own.
#   - src/graph/candidate_reprioritization.py (Milestone D1's re-scoring
#     job) is the ONLY thing that UPDATEs priority_score/why/group_id/
#     deprioritized on an existing row. It never inserts, deletes, or
#     touches any other column — structurally incapable of asserting a
#     confirm/reject decision, because it never calls write_edge().
# ============================================================

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from src.database.postgres import get_session

logger = logging.getLogger(__name__)

# Edge labels this index tracks. CROSS_VERSION_OF is deliberately absent —
# C4 writes it directly (status is never 'pending'), so it never belongs
# in a pending-candidate queue at all.
TRACKED_EDGE_LABELS = ("SAME_AS", "CITES")


async def maintain_pending(
    edge_id: int,
    edge_label: str,
    a_key: str,
    b_key: str,
    *,
    tier: Optional[str],
    confidence: Optional[float],
    basis: Optional[str],
    name_similarity: Optional[float],
    shared_case: Optional[bool],
    shared_structured_id: Optional[bool],
    source_doc_id: Optional[str],
) -> None:
    """
    Insert the row for a freshly-written PENDING edge. A no-op for any
    edge_label outside TRACKED_EDGE_LABELS. Same resilience contract as
    identity_index.maintain(): failure here must never fail the graph
    write it rides alongside (versioning.write_edge already returned
    successfully by the time this runs) — logged and swallowed; the
    queue simply misses this one candidate until the next successful
    write for it, same safety net identity_index leans on.
    """
    if edge_label not in TRACKED_EDGE_LABELS:
        return
    try:
        async with get_session() as db:
            await db.execute(
                text(
                    """
                    INSERT INTO pending_candidate_priority
                        (edge_id, edge_label, tier, a_key, b_key,
                         original_confidence, original_basis,
                         original_name_similarity, original_shared_case,
                         original_shared_structured_id, priority_score,
                         source_doc_id)
                    VALUES
                        (:edge_id, :edge_label, :tier, :a_key, :b_key,
                         :confidence, :basis, :name_similarity, :shared_case,
                         :shared_structured_id, :priority_score,
                         :source_doc_id)
                    ON CONFLICT (edge_id) DO NOTHING
                    """
                ),
                {
                    "edge_id": edge_id, "edge_label": edge_label, "tier": tier,
                    "a_key": a_key, "b_key": b_key, "confidence": confidence,
                    "basis": basis, "name_similarity": name_similarity,
                    "shared_case": shared_case, "shared_structured_id": shared_structured_id,
                    # A separate bound parameter, not a second use of
                    # :confidence inside COALESCE(...) — asyncpg infers a
                    # single PostgreSQL type per parameter NUMBER across
                    # the whole statement, and a literal `0` in a COALESCE
                    # alongside a `double precision` column made it guess
                    # `integer` for both uses, raising AmbiguousParameterError
                    # (confirmed live: "inconsistent types deduced for
                    # parameter $6 integer versus double precision").
                    "priority_score": float(confidence) if confidence is not None else 0.0,
                    "source_doc_id": source_doc_id,
                },
            )
    except Exception as exc:
        logger.warning(
            "pending_candidate_priority.maintain_pending: failed to index "
            "edge %s (%s) — queue will miss this candidate until the next "
            "successful write (%s)", edge_id, edge_label, exc,
        )


async def resolve_pending(edge_id: int) -> None:
    """
    Remove the row for an edge that just got confirmed/rejected by a
    human (versioning.write_edge()'s supersedes_edge_id path). A no-op
    if no row existed (e.g. a CROSS_VERSION_OF or already-superseded
    edge) — DELETE ... WHERE matching nothing is not an error.
    """
    try:
        async with get_session() as db:
            await db.execute(
                text("DELETE FROM pending_candidate_priority WHERE edge_id = :edge_id"),
                {"edge_id": edge_id},
            )
    except Exception as exc:
        logger.warning(
            "pending_candidate_priority.resolve_pending: failed to clear "
            "edge %s (%s) — a stale row may linger until the next "
            "reprioritization sweep drops it", edge_id, exc,
        )


async def update_priority(
    edge_id: int, *, priority_score: float, why: str, group_id: Optional[str],
    deprioritized: bool, last_scored_at: datetime,
) -> bool:
    """
    The ONLY write candidate_reprioritization.py performs. Updates the
    four re-scoring-owned columns on an existing row; never touches
    edge_id/edge_label/tier/a_key/b_key/original_* (those are the
    write-time snapshot, immutable after insert) and never inserts a
    row that doesn't already exist (a re-score with nothing to score
    against is a caller bug, not a reason to invent a queue entry).

    Returns True if a row was updated, False if edge_id had no row
    (already resolved by a human, or never a tracked edge_label) —
    callers should skip a candidate rather than treat this as fatal.

    `last_scored_at` must be a real `datetime` (candidate_reprioritization
    passes `datetime.now(timezone.utc)` directly), NOT an ISO string —
    asyncpg's prepared-statement binding infers a strict PostgreSQL type
    per parameter from the query itself and rejects a plain str for a
    timestamptz column outright, even wrapped in an explicit SQL `CAST`
    (confirmed live: `DataError: expected a datetime.date or
    datetime.datetime instance, got 'str'`, unchanged by adding the
    CAST). Every other timestamp this codebase writes through raw SQL
    (e.g. identity_index.maintain()'s `updated_at`) instead lets
    Postgres's own `now()` compute the value server-side — not usable
    here since `last_scored_at` is caller-supplied, not "now" by
    definition (candidate_reprioritization stamps it once and reuses it
    across several update_priority() calls in the same sweep).
    """
    try:
        async with get_session() as db:
            result = await db.execute(
                text(
                    """
                    UPDATE pending_candidate_priority
                    SET priority_score = :priority_score, why = :why,
                        group_id = :group_id, deprioritized = :deprioritized,
                        last_scored_at = :last_scored_at
                    WHERE edge_id = :edge_id
                    """
                ),
                {
                    "edge_id": edge_id, "priority_score": priority_score, "why": why,
                    "group_id": group_id, "deprioritized": deprioritized,
                    "last_scored_at": last_scored_at,
                },
            )
            return result.rowcount > 0
    except Exception as exc:
        logger.warning(
            "pending_candidate_priority.update_priority: failed for edge %s (%s)",
            edge_id, exc,
        )
        return False


async def list_rows(edge_label: str, *, include_deprioritized: bool = True) -> list[dict]:
    """
    Every tracked row for `edge_label`, ordered by priority_score
    descending (deprioritized rows sink to the bottom regardless of their
    stale score — "sink, not delete" per the plan). This is the Postgres
    index scan that replaces the AGE `WHERE r.status = 'pending'` full
    label scan on graph_review.py's new /queue read path.
    """
    try:
        async with get_session() as db:
            clause = "" if include_deprioritized else "AND deprioritized = false"
            res = await db.execute(
                text(
                    f"""
                    SELECT edge_id, tier, a_key, b_key, original_confidence,
                           original_basis, priority_score, why, group_id,
                           deprioritized, last_scored_at, created_at, source_doc_id
                    FROM pending_candidate_priority
                    WHERE edge_label = :edge_label {clause}
                    ORDER BY deprioritized ASC, priority_score DESC, created_at DESC
                    """
                ),
                {"edge_label": edge_label},
            )
            return [dict(row._mapping) for row in res.fetchall()]
    except Exception as exc:
        logger.warning(
            "pending_candidate_priority.list_rows: failed for %s (%s) — "
            "caller falls back to unordered AGE listing", edge_label, exc,
        )
        return []


async def list_all_edge_ids(edge_label: str) -> list[int]:
    """Every currently-pending edge_id for `edge_label` — the re-scoring job's own work list, so it never has to fall back to an AGE scan to find its candidates."""
    try:
        async with get_session() as db:
            res = await db.execute(
                text("SELECT edge_id FROM pending_candidate_priority WHERE edge_label = :edge_label"),
                {"edge_label": edge_label},
            )
            return [row[0] for row in res.fetchall()]
    except Exception as exc:
        logger.warning("pending_candidate_priority.list_all_edge_ids: failed for %s (%s)", edge_label, exc)
        return []


async def cites_edge_ids_touching_case(case_id: str) -> list[int]:
    """
    CITES rows whose a_key/b_key (both case_ids for this edge_label,
    unlike SAME_AS's entity_id keys) is `case_id` — a direct Postgres
    filter, safe because CITES's keys ARE case_ids. SAME_AS has no
    equivalent cheap filter (its a_key/b_key are entity_ids, not
    case_ids) — candidate_reprioritization.py's incremental path instead
    fetches the case's own entity set from AGE and filters
    list_rows("SAME_AS") in Python against that set.
    """
    try:
        async with get_session() as db:
            res = await db.execute(
                text(
                    """
                    SELECT edge_id FROM pending_candidate_priority
                    WHERE edge_label = 'CITES' AND (a_key = :case_id OR b_key = :case_id)
                    """
                ),
                {"case_id": case_id},
            )
            return [row[0] for row in res.fetchall()]
    except Exception as exc:
        logger.warning("pending_candidate_priority.cites_edge_ids_touching_case: failed for %s (%s)", case_id, exc)
        return []
