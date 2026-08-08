-- Migration 019: record when conflict detection last completed for a case
--
-- Background: `CONFLICTS_WITH` edges are the only read-time evidence about a
-- case's conflict state, and their ABSENCE is ambiguous. It means either
-- "detection ran and found nothing" or "detection never ran" — and there are
-- three independent ways to land in the second case:
--
--   1. src/ingestion/service.py schedules detection with a bare
--      asyncio.create_task() AFTER graph extraction has already written the
--      events, so there is a window in which the events are queryable and the
--      detection task has not finished.
--   2. Detection only runs on ingestion with a case_id. Events reaching the
--      graph any other way (a backfill, a manual write, an ingest where
--      case_id was omitted) never trigger it.
--   3. conflict_detection.detect_conflicts() returns early on fewer than two
--      incidents, and swallows its own fetch failure.
--
-- Without a marker, Timeline Building has to report every unflagged event as
-- ConflictState.UNKNOWN, because asserting NONE would claim a clean check the
-- system may never have performed — on what is the COMMON path for a new case.
-- That is correct but imprecise: a genuinely-checked, genuinely-clean case
-- also reads as "not checked".
--
-- This column is the missing evidence. Set to now() by conflict_bg.py when
-- detect_conflicts() RETURNS — including its early-return path, since
-- "examined the case and found fewer than two incidents" is still a completed
-- check, just one with nothing to find. NOT set when detection raises.
--
-- WRITTEN BY THE BACKGROUND TASK ITSELF, ON COMPLETION. This is what keeps the
-- race in (1) correctly represented: a query arriving while detection is still
-- in flight finds no marker yet and correctly reads UNKNOWN. Writing it at
-- schedule time instead would assert the check had happened before it had.
--
-- NULLABLE, and NULL is meaningful: it is exactly "no completed detection on
-- record", which is the state every pre-existing case is genuinely in. There is
-- deliberately no backfill — inventing a timestamp for cases that were never
-- checked would manufacture the false all-clear this whole column exists to
-- prevent.

ALTER TABLE cases
    ADD COLUMN IF NOT EXISTS conflicts_checked_at TIMESTAMP NULL;

COMMENT ON COLUMN cases.conflicts_checked_at IS
    'When case-level conflict detection last COMPLETED for this case, written by '
    'the background task on return (including its <2-incident early return). NULL '
    'means no completed detection is on record - which is NOT the same as "no '
    'conflicts found". Read by the Timeline Building sub-agent to decide between '
    'ConflictState.NONE and ConflictState.UNKNOWN.';
