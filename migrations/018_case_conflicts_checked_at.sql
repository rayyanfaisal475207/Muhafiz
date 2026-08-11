-- Migration 018: cases.conflicts_checked_at — a completion marker for
-- background conflict detection (agent-harness reconciliation, Unit 6).
--
-- WHY THIS EXISTS. src/graph/conflict_detection.py::detect_conflicts()
-- writes CONFLICTS_WITH edges when it finds a contradiction, but nothing
-- ever recorded that a case's incidents were CHECKED at all. The absence of
-- a CONFLICTS_WITH edge is ambiguous between two very different facts:
--
--   * detection ran and found nothing         -> a real "clean" result
--   * detection never ran, or hasn't finished  -> nothing is known yet
--
-- and prior to this migration nothing distinguished them at read time. Three
-- independent, real (not hypothetical) ways a case can have zero conflict
-- edges without having been checked:
--
--   1. THE RACE. src/ingestion/service.py schedules detection with a bare
--      asyncio.create_task() immediately after graph extraction writes the
--      case's Incident nodes. Between that write and the task finishing (an
--      LLM call — seconds), the incidents are queryable and no conflict
--      edges exist yet.
--   2. Detection only ever triggers on an ingestion path that supplies a
--      case_id. Incidents that reach the graph any other way (a backfill, a
--      manual write) never trigger it at all.
--   3. detect_conflicts() legitimately returns early on fewer than two
--      incidents (nothing to compare) — a completed check with nothing to
--      find, distinct from case 1/2 above where nothing was attempted.
--
-- conflicts_checked_at is written by src/ingestion/conflict_bg.py's
-- background task ONLY after detect_conflicts() returns without raising —
-- i.e. a genuinely completed check (including the fewer-than-two-incidents
-- early return, which is a real "checked, nothing to compare" outcome).
-- conflict_detection.py's own internal fetch failure was changed from a
-- silently-swallowed `return` to a `raise` as part of this same fix, so a
-- failed check no longer looks identical to a completed one from this
-- marker's perspective — see that file's own comment.
--
-- src/pipeline/harness/agents/timeline_building.py reads this to decide
-- whether an Incident absent from CONFLICTS_WITH may honestly render as
-- ConflictState.NONE ("checked, clean") rather than UNKNOWN ("not known to
-- have been checked") — asserting NONE without this marker would be exactly
-- the false all-clear ConflictState's own three-state design (RESOLVED-5)
-- exists to prevent.
--
-- Nullable, no default: NULL is the correct starting state for every
-- existing case (none of them have a marker recorded — this migration adds
-- the column, it does not retroactively assert anything about them).

ALTER TABLE cases
    ADD COLUMN IF NOT EXISTS conflicts_checked_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN cases.conflicts_checked_at IS
    'When conflict detection last COMPLETED for this case (never merely scheduled). NULL = not known to have been checked. Written by src/ingestion/conflict_bg.py on a genuinely completed detect_conflicts() run.';
