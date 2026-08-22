# ============================================================
# Ingestion run quality tracking — Ingestion Quality Control at Scale,
# Module G1 (see INGESTION_QUALITY_AT_SCALE_PLAN.md).
#
# Pure counting of decisions entity_resolution.py and
# structured_projection.py already make deterministically elsewhere —
# this module adds VISIBILITY into an ingestion run's outcome, never a
# new judgment call. No LLM anywhere in this file, no writes to any
# graph edge/node, structurally incapable of confirming/rejecting an
# entity match (same "cannot do the risky thing even by mistake"
# discipline candidate_reprioritization.py already established for D1).
#
# Same contextvar idiom src/database/postgres.py already uses
# (current_case_id/current_cross_case/current_rls_active) — a mutable
# per-run accumulator, set at the start of one ingestion run
# (ingest_file()'s per-document call, or one whole
# scripts/sync_muhafiz_data.py --full pass) and read/flushed at the end.
# asyncio.create_task() copies the current context at spawn time, so a
# fire-and-forget background task started INSIDE a tracked run's own
# call tree still shares the same accumulator object (dict mutation is
# visible across the copy) — confirmed by this module's own tests, not
# assumed from asyncio's documented behavior alone.
#
# record_tier()/record_new_tier_from_gate()/record_extraction_error() are
# no-ops when no run is currently tracked (the overwhelming majority
# of call sites in this codebase — every existing caller of
# entity_resolution.resolve_and_write()/structured_projection.py's
# corroboration gate that predates this module keeps behaving exactly as
# before; opting a call site INTO tracking is what start_run()/track_run()
# do, not a default every ingestion path now carries).
# ============================================================

from __future__ import annotations

import contextvars
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from sqlalchemy import text

from src.database.postgres import get_session

logger = logging.getLogger(__name__)

# entity_resolution.py's own tier constants -> this table's column names.
# Not re-derived from entity_resolution.py by import (this module must
# stay a leaf dependency graph.entity_resolution.py can safely import
# FROM, not the reverse) — the four literal tier strings are
# entity_resolution.TIER_CNIC_AUTO/TIER_FLAGGED/TIER_REVIEW/TIER_NEW's
# own values, confirmed against that module before writing this.
_TIER_COLUMN = {
    "cnic_auto": "tier_cnic_auto",
    "flagged_unverified": "tier_flagged_unverified",
    "human_review": "tier_human_review",
    "new": "tier_new",
}

_EMPTY_COUNTS = {
    "tier_cnic_auto": 0,
    "tier_flagged_unverified": 0,
    "tier_human_review": 0,
    "tier_new": 0,
    "corroboration_gate_rejections": 0,
    "extraction_errors": 0,
}

_current_run: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "_ingestion_quality_current_run", default=None
)


async def start_run(run_id: str, source: str, case_id: Optional[str] = None) -> None:
    """
    Begin tracking one ingestion run. Sets the per-run accumulator (a
    fresh dict every call — never shared/reused across runs, even
    same-source ones) and inserts the row's starting state, so a run
    that crashes mid-flight still leaves a "started, never finished"
    row behind as its own honest failure signal, rather than silently
    losing that run's counts entirely.

    Best-effort, same resilience contract as identity_index.maintain():
    a failure to write the starting row must never fail the ingestion
    it rides alongside — logged and swallowed. The in-memory accumulator
    is still set regardless, so record_*() calls during this run are
    never lost even if the starting INSERT itself failed; only
    finish_run()'s own write is then at risk, and that failure is
    handled the same way.
    """
    _current_run.set(dict(_EMPTY_COUNTS))
    try:
        async with get_session() as db:
            await db.execute(
                text(
                    "INSERT INTO ingestion_run_quality (run_id, source, case_id) "
                    "VALUES (:run_id, :source, :case_id) "
                    "ON CONFLICT (run_id) DO NOTHING"
                ),
                {"run_id": run_id, "source": source, "case_id": case_id},
            )
    except Exception as exc:
        logger.error("ingestion_quality.start_run: failed to write starting row for %s: %s", run_id, exc)


def record_tier(tier: str) -> None:
    """
    Called from entity_resolution.resolve_and_write()'s own return
    chokepoint — the ONE place every person/vehicle/phone/officer
    resolution in this codebase produces a final tier decision. A
    no-op when no run is currently tracked, and a no-op for a tier
    string this module doesn't recognize (defends against a future new
    tier being added to entity_resolution.py without this table's
    schema being extended in lockstep — undercounts rather than raises).
    """
    acc = _current_run.get()
    if acc is None:
        return
    column = _TIER_COLUMN.get(tier)
    if column:
        acc[column] += 1


def record_new_tier_from_gate(gated: bool) -> None:
    """
    structured_projection.py's _write_new_person() path — the OTHER
    place a mention resolves to TIER_NEW, outside
    entity_resolution.resolve_and_write() entirely (see that module's
    own docstring: the corroboration gate bypasses resolve_and_write()
    on purpose, to never even risk a name-fallback SAME_AS candidate).
    `gated=True` when this call is specifically the corroboration
    gate's own refusal outcome (a real candidate existed and was
    refused) — kept as a SEPARATE call from record_tier("new") so a
    future caller of record_tier() can never accidentally double-count
    a gate refusal as this module's own tier_new bucket; call sites
    call whichever of the two matches what actually happened.
    """
    acc = _current_run.get()
    if acc is None:
        return
    acc["tier_new"] += 1
    if gated:
        acc["corroboration_gate_rejections"] += 1


def record_extraction_error() -> None:
    """A record that failed extraction/projection entirely during this run — not a resolution-tier outcome, a hard failure."""
    acc = _current_run.get()
    if acc is None:
        return
    acc["extraction_errors"] += 1


async def finish_run(run_id: str, *, source: Optional[str] = None) -> Optional[dict]:
    """
    Flush the accumulated counts to the run's row and clear the
    contextvar. Returns the flushed counts dict, or None if no run was
    being tracked (a caller invoking finish_run() without a matching
    start_run() — a caller bug, reported via the return value rather
    than raised, so a defensive/optional call site doesn't need its own
    try/except just to be safe).

    Best-effort, same resilience contract as start_run(): a failure to
    persist the flush is logged and swallowed, never propagated to fail
    the ingestion run itself.

    `source` — [Ingestion Quality Control at Scale, Module G2] when
    given, this is the one chokepoint the circuit breaker
    (src/graph/ingestion_circuit_breaker.py) reads from: it already has
    the just-flushed counts in hand here, so the threshold check runs
    against them directly rather than re-querying Postgres for the row
    that was just written. Optional and additive — a caller that omits
    it (there are none left in this codebase, but a future one could
    exist) simply gets G1's original flush-only behavior, no circuit
    breaker check. The returned dict gains "flagged_for_review"/
    "flagged_reason" keys only when `source` is given.
    """
    acc = _current_run.get()
    if acc is None:
        return None
    try:
        async with get_session() as db:
            await db.execute(
                text(
                    "UPDATE ingestion_run_quality SET finished_at = now(), "
                    "tier_cnic_auto = :tier_cnic_auto, "
                    "tier_flagged_unverified = :tier_flagged_unverified, "
                    "tier_human_review = :tier_human_review, "
                    "tier_new = :tier_new, "
                    "corroboration_gate_rejections = :corroboration_gate_rejections, "
                    "extraction_errors = :extraction_errors "
                    "WHERE run_id = :run_id"
                ),
                {**acc, "run_id": run_id},
            )
    except Exception as exc:
        logger.error("ingestion_quality.finish_run: failed to flush counts for %s: %s", run_id, exc)
    finally:
        _current_run.set(None)

    if source is not None:
        from src.graph import ingestion_circuit_breaker
        decision = await ingestion_circuit_breaker.check_and_flag(run_id, source, acc)
        acc = {**acc, "flagged_for_review": decision["flagged"], "flagged_reason": decision["reason"]}

    return acc


@asynccontextmanager
async def track_run(run_id: str, source: str, case_id: Optional[str] = None) -> AsyncIterator[None]:
    """
    Convenience wrapper for the common case: `async with track_run(...):`
    around one ingestion run's body. Always calls finish_run() on the
    way out, success or exception, so a run that raises partway through
    still leaves its partial counts recorded rather than losing them
    silently.
    """
    await start_run(run_id, source, case_id=case_id)
    try:
        yield
    finally:
        await finish_run(run_id, source=source)
