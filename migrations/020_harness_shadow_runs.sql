-- Migration 020: harness_shadow_runs — a quarantined log for shadow-mode runs
--
-- WHAT SHADOW MODE IS
-- ────────────────────
-- The agent harness (src/pipeline/harness/) is a second implementation of the
-- retrieval-and-answer path, built alongside the legacy orchestrator rather
-- than replacing it. Shadow mode runs the harness on a SAMPLE of real queries
-- AFTER the legacy path has already answered the user, and records what the
-- harness would have said. Nothing it produces is ever shown to an
-- investigator, and nothing it does can change the answer they received.
--
-- WHY THIS IS A SEPARATE TABLE, NOT A COLUMN ON pipeline_runs
-- ───────────────────────────────────────────────────────────
-- `pipeline_runs` and `pipeline_steps` are what the admin analytics screens
-- read to report route mix, verifier pass rates, latency and degradation.
-- Writing shadow rows into them would silently corrupt every one of those
-- numbers with traffic no user ever saw — a harness abstention would count
-- against the real verifier pass rate, and a shadow run's latency would enter
-- the real latency distribution. Keeping shadow results in their own table
-- means the existing dashboards keep meaning exactly what they meant before,
-- and a comparison query has to opt in by naming this table.
--
-- ROW-LEVEL SECURITY: MIRRORS pipeline_runs DELIBERATELY
-- ──────────────────────────────────────────────────────
-- This table stores a query string and a generated answer, both of which can
-- contain case content. A log table without the isolation its source data has
-- is a side channel: it would let a session scoped to Case A read text derived
-- from Case B by querying the shadow log instead of the evidence.
--
-- The policy below is character-for-character the same shape as
-- `pipeline_runs_isolation_policy`, and for the same reasons:
--   * `app.rls_active` IS DISTINCT FROM 'true' — the unscoped path (migrations,
--     admin tooling, ingestion) where no case scope has been armed at all.
--   * `app.cross_case` = 'true' — a caller that passed the cross-case role gate
--     and is legitimately reading across cases.
--   * otherwise — the row's session must belong to the armed case, with a NULL
--     case_id matching only the empty scope.
-- FORCE ROW LEVEL SECURITY is set for the same reason the other tables set it:
-- without FORCE, the table OWNER bypasses its own policy, which would make the
-- protection depend on which role happens to connect.
--
-- ON DELETE CASCADE from sessions: a deleted conversation must not leave its
-- query text and generated answers behind in a diagnostic table. `pipeline_runs`
-- predates that reasoning and uses a plain FK; new tables should not.

CREATE TABLE IF NOT EXISTS harness_shadow_runs (
    shadow_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Links a shadow run to the REAL run it shadowed, so a comparison can put
    -- the two answers side by side. Deliberately NOT a foreign key to
    -- pipeline_runs: the legacy path creates that row on a best-effort
    -- background task, so it may not exist (or may not exist YET) when the
    -- shadow run finishes. A hard FK would make a logging failure in the
    -- shadow path depend on a race in the primary path.
    run_id              uuid,

    session_id          uuid NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    user_id             uuid REFERENCES users(id),
    case_id             text,

    -- What was asked, and what each path decided to do with it.
    original_query      text,
    legacy_route        text,           -- route_query()'s answer, as legacy used it
    harness_sub_agent   text,           -- which of the seven handled it ('__direct__' if none)
    routing_basis       text,           -- classifier's stated reason, for disagreement triage

    -- What the harness produced. `harness_answer` is the full generated text:
    -- the whole point of shadow mode is being able to read it next to what the
    -- user actually got, and a truncated answer cannot be compared.
    harness_status      text,           -- ok | partial | empty | abstained | denied
    harness_answer      text,
    citation_count      integer NOT NULL DEFAULT 0,
    tools_used          text[]  NOT NULL DEFAULT '{}',
    degraded_from       text[]  NOT NULL DEFAULT '{}',
    caveats             text[]  NOT NULL DEFAULT '{}',

    -- Did the harness reach the same conclusion as the legacy path? NULL when
    -- the comparison could not be made (legacy outcome unknown at write time).
    legacy_outcome      text,
    routes_agree        boolean,

    -- Operational reality of the shadow run itself.
    duration_ms         integer,
    -- Set when the harness raised. A shadow run that crashes is a finding, not
    -- an error to swallow: it means this query shape would have failed for a
    -- real user had the harness been serving. Recorded rather than discarded.
    error               text,
    sampled_reason      text,           -- why this query was picked (rate | forced | route)

    created_at          timestamp without time zone NOT NULL DEFAULT now()
);

-- Read patterns this table is built for: "what happened recently", "how did
-- sub-agent X do", and "show me the disagreements" — the last being the whole
-- reason shadow mode exists, so it gets a partial index rather than scanning.
CREATE INDEX IF NOT EXISTS idx_harness_shadow_runs_created
    ON harness_shadow_runs (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_harness_shadow_runs_sub_agent
    ON harness_shadow_runs (harness_sub_agent, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_harness_shadow_runs_session
    ON harness_shadow_runs (session_id);

CREATE INDEX IF NOT EXISTS idx_harness_shadow_runs_disagreements
    ON harness_shadow_runs (created_at DESC)
    WHERE routes_agree IS FALSE OR error IS NOT NULL;

ALTER TABLE harness_shadow_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE harness_shadow_runs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS harness_shadow_runs_isolation_policy ON harness_shadow_runs;
CREATE POLICY harness_shadow_runs_isolation_policy ON harness_shadow_runs
    FOR ALL
    USING (
        current_setting('app.rls_active', true) IS DISTINCT FROM 'true'
        OR current_setting('app.cross_case', true) = 'true'
        OR session_id IN (
            SELECT sessions.session_id FROM sessions
            WHERE (sessions.case_id IS NULL
                   AND current_setting('app.case_id', true) = '')
               OR sessions.case_id = current_setting('app.case_id', true)
        )
    );

-- muhafiz_app is the least-privilege role the application connects as
-- (migration 015). Without these grants the shadow writer fails at runtime as
-- a permission error, which — because shadow logging is deliberately
-- fire-and-forget — would be swallowed and look like "shadow mode does
-- nothing" rather than a misconfiguration.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'muhafiz_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON harness_shadow_runs TO muhafiz_app;
    END IF;
END
$$;

COMMENT ON TABLE harness_shadow_runs IS
    'Agent-harness shadow-mode results. Never user-visible: rows record what the '
    'harness WOULD have answered for a sampled query, after the legacy path had '
    'already answered the user. Kept out of pipeline_runs so admin analytics are '
    'not polluted by traffic no user ever saw.';
