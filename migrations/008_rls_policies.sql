-- Migration 008: Row Level Security (RLS) Backstop

-- 1. Enable RLS and Force it on sensitive tables (so it applies to the table owner too)
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions FORCE ROW LEVEL SECURITY;

ALTER TABLE pipeline_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_runs FORCE ROW LEVEL SECURITY;

ALTER TABLE cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE cases FORCE ROW LEVEL SECURITY;

-- 2. Create Isolation Policies
-- These policies are fail-open ONLY IF app.rls_active is not 'true'.
-- When a request explicitly sets app.rls_active = 'true', isolation is rigidly enforced.

-- Documents: readable if RLS is inactive, OR if global, OR if cross-case is allowed, OR if case_id matches
CREATE POLICY documents_isolation_policy ON documents
FOR ALL
USING (
    current_setting('app.rls_active', true) IS DISTINCT FROM 'true'
    OR is_global = true 
    OR current_setting('app.cross_case', true) = 'true'
    OR case_id = current_setting('app.case_id', true)
);

-- Cases: readable if RLS is inactive, OR if cross-case is allowed, OR if case_id matches
CREATE POLICY cases_isolation_policy ON cases
FOR ALL
USING (
    current_setting('app.rls_active', true) IS DISTINCT FROM 'true'
    OR current_setting('app.cross_case', true) = 'true'
    OR case_id = current_setting('app.case_id', true)
);

-- Sessions: readable if RLS is inactive, OR if cross-case is allowed, OR if case_id matches
CREATE POLICY sessions_isolation_policy ON sessions
FOR ALL
USING (
    current_setting('app.rls_active', true) IS DISTINCT FROM 'true'
    OR current_setting('app.cross_case', true) = 'true'
    OR case_id = current_setting('app.case_id', true)
);

-- Pipeline runs: readable if RLS is inactive, OR if cross-case is allowed, OR if joined session's case_id matches
CREATE POLICY pipeline_runs_isolation_policy ON pipeline_runs
FOR ALL
USING (
    current_setting('app.rls_active', true) IS DISTINCT FROM 'true'
    OR current_setting('app.cross_case', true) = 'true'
    OR session_id IN (
        SELECT session_id FROM sessions 
        WHERE case_id = current_setting('app.case_id', true)
    )
);
