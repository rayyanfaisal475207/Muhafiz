-- Migration 010: Fix RLS NULL-vs-NULL bug, cover `messages`, index the
-- messages policy's correlated subquery.
--
-- Background (see issues.md's Critical "Postgres RLS policies' NULL-vs-NULL
-- equality silently breaks every non-case-scoped conversation" finding):
-- migration 008's policies compared `case_id = current_setting('app.case_id', true)`.
-- For a general (no-case) row, `case_id` is SQL NULL; if the application
-- never called `SET LOCAL`/`set_config` for `app.case_id` either (or a
-- future caller forgets to), `current_setting(...)` also returns NULL —
-- and `NULL = NULL` evaluates to NULL, not TRUE, in three-valued logic.
-- The row becomes invisible, and since these are FOR ALL policies with no
-- separate WITH CHECK, INSERTing such a row is rejected too.
--
-- Fix: the application (src/database/postgres.py::get_session(), Phase 2)
-- now ALWAYS calls set_config('app.case_id', ...) with the empty string
-- for "no case", never leaving it unset. These policies are rewritten to
-- compare against '' explicitly as a real, comparable value instead of
-- relying on NULL semantics on either side of the comparison.

-- 1. Rewrite documents_isolation_policy
DROP POLICY IF EXISTS documents_isolation_policy ON documents;
CREATE POLICY documents_isolation_policy ON documents
FOR ALL
USING (
    current_setting('app.rls_active', true) IS DISTINCT FROM 'true'
    OR is_global = true
    OR current_setting('app.cross_case', true) = 'true'
    OR (case_id IS NULL AND current_setting('app.case_id', true) = '')
    OR case_id = current_setting('app.case_id', true)
);

-- 2. Rewrite cases_isolation_policy
DROP POLICY IF EXISTS cases_isolation_policy ON cases;
CREATE POLICY cases_isolation_policy ON cases
FOR ALL
USING (
    current_setting('app.rls_active', true) IS DISTINCT FROM 'true'
    OR current_setting('app.cross_case', true) = 'true'
    OR (case_id IS NULL AND current_setting('app.case_id', true) = '')
    OR case_id = current_setting('app.case_id', true)
);

-- 3. Rewrite sessions_isolation_policy
DROP POLICY IF EXISTS sessions_isolation_policy ON sessions;
CREATE POLICY sessions_isolation_policy ON sessions
FOR ALL
USING (
    current_setting('app.rls_active', true) IS DISTINCT FROM 'true'
    OR current_setting('app.cross_case', true) = 'true'
    OR (case_id IS NULL AND current_setting('app.case_id', true) = '')
    OR case_id = current_setting('app.case_id', true)
);

-- 4. Rewrite pipeline_runs_isolation_policy
DROP POLICY IF EXISTS pipeline_runs_isolation_policy ON pipeline_runs;
CREATE POLICY pipeline_runs_isolation_policy ON pipeline_runs
FOR ALL
USING (
    current_setting('app.rls_active', true) IS DISTINCT FROM 'true'
    OR current_setting('app.cross_case', true) = 'true'
    OR session_id IN (
        SELECT session_id FROM sessions
        WHERE (case_id IS NULL AND current_setting('app.case_id', true) = '')
           OR case_id = current_setting('app.case_id', true)
    )
);

-- 5. New: cover `messages` — the table actually holding chat content.
-- Migration 008 protected `sessions` but never its content-bearing child
-- table (issues.md's Medium "`messages` — the table actually holding
-- case-sensitive chat content — is not covered by RLS at all" finding).
-- Same join-through-sessions pattern as pipeline_runs above.
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages FORCE ROW LEVEL SECURITY;

CREATE POLICY messages_isolation_policy ON messages
FOR ALL
USING (
    current_setting('app.rls_active', true) IS DISTINCT FROM 'true'
    OR current_setting('app.cross_case', true) = 'true'
    OR session_id IN (
        SELECT session_id FROM sessions
        WHERE (case_id IS NULL AND current_setting('app.case_id', true) = '')
           OR case_id = current_setting('app.case_id', true)
    )
);

-- 6. Index to keep the messages/pipeline_runs policies' correlated
-- subquery (`SELECT session_id FROM sessions WHERE case_id = ...`) from
-- becoming a sequential scan of `sessions` on every message/run read as
-- history accumulates (solution.md §2.2 blast-radius note).
CREATE INDEX IF NOT EXISTS idx_sessions_case_id ON sessions (case_id);
