# ============================================================
# Ingestion circuit breaker — Ingestion Quality Control at Scale,
# Module G2 (see INGESTION_QUALITY_AT_SCALE_PLAN.md). Builds on G1's
# per-run rollup (src/graph/ingestion_quality.py, migration 028).
#
# A deterministic threshold check, run once at the end of each tracked
# ingestion run (never per-record — a single anomalous record is what the
# existing corroboration gate and review queue already handle). Compares
# the run's ambiguous-match rate and corroboration-gate rejection rate
# against a rolling baseline of recent same-source runs and, if either
# comes in well above baseline, FLAGS the run — never auto-remediates
# anything (§4 of the plan: no automatic remediation, ever). A flag is a
# fixed status distinct from plain "success", written to
# ingestion_run_quality.flagged_for_review/flagged_reason — both columns
# already exist, unused, from G1's own migration 028; this module adds no
# new migration.
#
# ── OPEN POINT 1 RESOLVED: rolling-baseline grouping is PER SOURCE ──────
# Queried the real ingestion_run_quality table before deciding (not
# guessed): every row written so far carries source IN
# ('ingest_file', 'sync_muhafiz_data'). `ingest_file` writes one row per
# single-document upload, each almost always case-scoped to a DIFFERENT
# case_id (there is no natural repeat population to baseline against per
# case). `sync_muhafiz_data` writes one row per whole `--full` sync pass
# and case_id is always NULL (a multi-case bulk run has no single case to
# attribute drift to) — confirmed live, not assumed: every
# sync_muhafiz_data row in the real table has case_id IS NULL. Grouping
# by case_id would therefore mean "almost never enough history to
# baseline against" for ingest_file and "no key to group by at all" for
# sync_muhafiz_data. `source` is the only column with a real, repeated
# population behind it today — same reasoning
# community_detection.get_staleness() applies at a single GLOBAL level
# (it has exactly one thing to compare against: the whole Person graph).
# Revisit if a genuinely distinct sync endpoint/data source is added
# later and its own runs deserve a separate baseline from
# sync_muhafiz_data's.
#
# ── OPEN POINT 2 RESOLVED: threshold, stated as a starting point ───────
# get_staleness()'s own NODE_DRIFT_WARN_PCT/EDGE_DRIFT_WARN_PCT (0.10) are
# explicitly flagged there as "not tuned against real drift-history data
# yet". This module's own thresholds get the same disclosure, not a false
# claim of having been tuned: AMBIGUOUS_RATE_WARN_PCT/
# GATE_REJECTION_RATE_WARN_PCT below are both 0.10 (an absolute
# percentage-point gap over the rolling baseline average, not a relative
# drift — a relative/fractional comparison the way get_staleness() does
# it blows up near-meaninglessly when the baseline rate itself is close
# to zero, which an ambiguous-match rate realistically can be for a
# healthy source). Revisit once enough real runs exist to see what
# genuine batch-to-batch noise looks like for each source.
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

from src.database.postgres import get_session

logger = logging.getLogger(__name__)

# How many prior FINISHED runs for the same source to average into the
# rolling baseline. Small on purpose — this is meant to track "what does
# a normal batch for this source look like lately", not a long-run
# historical average that would be slow to react to a genuine, sustained
# regression.
BASELINE_WINDOW = 10

# Fewer prior runs than this and there simply isn't a meaningful baseline
# yet — the check is skipped (not flagged, not silently treated as
# "normal") rather than comparing against 1-2 noisy data points.
MIN_BASELINE_RUNS = 3

# Starting-point thresholds — see module docstring, point 2.
AMBIGUOUS_RATE_WARN_PCT = 0.10
GATE_REJECTION_RATE_WARN_PCT = 0.10


def _ambiguous_rate(counts: dict) -> Optional[float]:
    """
    (human_review + flagged_unverified) / total resolved mentions this
    run. None (not 0.0) when the run resolved zero mentions — a
    zero-mention run has no ambiguous-rate signal at all, and reporting
    0.0 would look identical to "resolved 500 mentions, 0 were
    ambiguous", which is a completely different, much stronger signal.
    """
    total = (
        counts.get("tier_cnic_auto", 0)
        + counts.get("tier_flagged_unverified", 0)
        + counts.get("tier_human_review", 0)
        + counts.get("tier_new", 0)
    )
    if total == 0:
        return None
    ambiguous = counts.get("tier_flagged_unverified", 0) + counts.get("tier_human_review", 0)
    return ambiguous / total


def _gate_rejection_rate(counts: dict) -> Optional[float]:
    """
    corroboration_gate_rejections / tier_new — the gate only ever fires
    for a mention that would otherwise resolve to TIER_NEW (see
    structured_projection.py / ingestion_quality.record_new_tier_from_gate),
    so tier_new (not total resolved) is the correct denominator. None when
    this run produced no TIER_NEW mentions at all (nothing the gate could
    have acted on either way).
    """
    tier_new = counts.get("tier_new", 0)
    if tier_new == 0:
        return None
    return counts.get("corroboration_gate_rejections", 0) / tier_new


async def _recent_finished_runs(source: str, exclude_run_id: str, limit: int) -> list[dict]:
    async with get_session() as db:
        result = await db.execute(
            text(
                "SELECT tier_cnic_auto, tier_flagged_unverified, tier_human_review, "
                "tier_new, corroboration_gate_rejections, flagged_for_review "
                "FROM ingestion_run_quality "
                "WHERE source = :source AND run_id != :run_id AND finished_at IS NOT NULL "
                "ORDER BY finished_at DESC LIMIT :limit"
            ),
            {"source": source, "run_id": exclude_run_id, "limit": limit},
        )
        return [dict(row._mapping) for row in result.fetchall()]


async def _most_recent_finished_run(source: str, exclude_run_id: str) -> Optional[dict]:
    async with get_session() as db:
        result = await db.execute(
            text(
                "SELECT run_id, flagged_for_review, flagged_reason "
                "FROM ingestion_run_quality "
                "WHERE source = :source AND run_id != :run_id AND finished_at IS NOT NULL "
                "ORDER BY finished_at DESC LIMIT 1"
            ),
            {"source": source, "run_id": exclude_run_id},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None


async def _write_flag(run_id: str, reason: str) -> None:
    async with get_session() as db:
        await db.execute(
            text(
                "UPDATE ingestion_run_quality SET flagged_for_review = true, "
                "flagged_reason = :reason WHERE run_id = :run_id"
            ),
            {"run_id": run_id, "reason": reason},
        )


async def check_and_flag(run_id: str, source: str, counts: dict) -> dict:
    """
    The circuit breaker's one entry point — called from
    ingestion_quality.finish_run() with the just-flushed counts already
    in hand (no re-query of the row it just wrote). Returns
    {"flagged": bool, "reason": str}. Best-effort, same resilience
    contract as every other write in this module family: a failure here
    must never fail the ingestion run it rides alongside.

    Two independent ways a run gets flagged:
      1. PROPAGATION: the immediately prior finished run for this same
         source is still flagged_for_review (a human has not yet
         acknowledged it — see src/api/ingestion_quality_admin.py's
         /acknowledge action). Per the plan's own requirement ("requiring
         an explicit human acknowledgment before the next batch from the
         same source proceeds unflagged"), this run inherits the flag
         regardless of its own rates — an unacknowledged problem does not
         quietly stop applying just because the NEXT run happened to look
         fine. The inherited flag is PERSISTED onto this run's own row
         (not just returned), which means a chain of N runs created
         between the original threshold breach and the human's
         acknowledgment all end up flagged, each requiring its own look —
         acknowledging the ORIGINAL run does not retroactively clear
         later runs that already inherited the flag before the ack
         happened, only the most recently created flagged run in the
         chain gates what comes next. Confirmed live against real
         Postgres (not just unit-tested): acknowledging a stale link in
         the chain correctly does NOT unblock it — only acknowledging the
         chain's current tail does. This is intentional, not a gap: in
         real operation a human acknowledges the run they were just
         alerted about, which is always the newest one, so a multi-run
         chain only actually forms when acknowledgment is delayed across
         more than one ingestion run for that source.
      2. THRESHOLD: this run's own ambiguous-match rate or
         corroboration-gate rejection rate exceeds the rolling baseline
         average (over the last BASELINE_WINDOW same-source finished
         runs) by more than the warn threshold.
    """
    try:
        prior = await _most_recent_finished_run(source, run_id)
        if prior and prior.get("flagged_for_review"):
            reason = (
                f"prior run {prior['run_id']} for source {source!r} is still "
                "flagged and has not been acknowledged"
            )
            await _write_flag(run_id, reason)
            return {"flagged": True, "reason": reason}

        history = await _recent_finished_runs(source, run_id, BASELINE_WINDOW)
        if len(history) < MIN_BASELINE_RUNS:
            return {"flagged": False, "reason": f"insufficient baseline history for source {source!r} ({len(history)} prior run(s))"}

        baseline_ambiguous = [r for r in (_ambiguous_rate(h) for h in history) if r is not None]
        baseline_gate = [r for r in (_gate_rejection_rate(h) for h in history) if r is not None]

        current_ambiguous = _ambiguous_rate(counts)
        current_gate = _gate_rejection_rate(counts)

        reasons = []
        if current_ambiguous is not None and baseline_ambiguous:
            avg = sum(baseline_ambiguous) / len(baseline_ambiguous)
            if current_ambiguous - avg >= AMBIGUOUS_RATE_WARN_PCT:
                reasons.append(
                    f"ambiguous-match rate {current_ambiguous:.1%} vs. baseline avg {avg:.1%} "
                    f"over {len(baseline_ambiguous)} prior run(s)"
                )
        if current_gate is not None and baseline_gate:
            avg = sum(baseline_gate) / len(baseline_gate)
            if current_gate - avg >= GATE_REJECTION_RATE_WARN_PCT:
                reasons.append(
                    f"corroboration-gate rejection rate {current_gate:.1%} vs. baseline avg {avg:.1%} "
                    f"over {len(baseline_gate)} prior run(s)"
                )

        if not reasons:
            return {"flagged": False, "reason": "within baseline"}

        reason = "; ".join(reasons)
        await _write_flag(run_id, reason)
        return {"flagged": True, "reason": reason}
    except Exception as exc:
        logger.error("ingestion_circuit_breaker.check_and_flag: failed for run %s (%s): %s", run_id, source, exc)
        return {"flagged": False, "reason": "circuit breaker check failed — see logs"}
