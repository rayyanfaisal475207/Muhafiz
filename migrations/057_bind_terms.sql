-- ============================================================
-- 057 — Bind many terms in one round trip
--
-- WHY
--
-- The cheap gate binds every candidate term in a question against real
-- schema and real stored values, and decides from the RESULT rather than
-- from a guess about what kind of question it is. There is no taxonomy of
-- question types to enumerate — there are millions, which is exactly why
-- classification by category was the wrong shape.
--
-- One term costs ~26 ms. A ten-word question yields ~27 candidate n-grams,
-- which is ~700 ms of round trips for a step that is supposed to be
-- cheaper than an LLM call. So all terms bind in a single statement.
--
-- WHY N-GRAMS AND NOT A KEYWORD LIST
--
-- Every 1-, 2- and 3-word window of the question is offered. Words that
-- mean nothing to this database simply fail to bind — "how", "many",
-- "please" return no rows and cost nothing extra. That removes the need
-- for a stopword list, which would be an authored vocabulary and would be
-- wrong in Urdu, in Roman Urdu, and in the mixed script this corpus
-- actually uses.
--
-- WHAT THE CALLER DOES WITH IT
--
--   zero bindings, no unknown domain term  -> answer from the prompt
--   zero bindings, unknown domain term     -> world knowledge, web
--   one or more bindings                   -> plan against the data
--
-- and the ambiguity flags below decide whether one LLM call is enough or
-- the expensive loop is warranted.
-- ============================================================

BEGIN;

CREATE OR REPLACE FUNCTION bind_terms(
    p_terms     text[],
    p_min_score numeric DEFAULT 0.45,
    p_per_term  integer DEFAULT 4
) RETURNS TABLE (
    term        text,
    rank        integer,
    match_kind  text,
    table_name  text,
    column_name text,
    value_raw   text,
    score       numeric,
    table_role  text,
    ambiguous   boolean
) LANGUAGE sql STABLE AS $fn$
    WITH hits AS (
        SELECT t.term,
               s.match_kind, s.table_name, s.column_name, s.value_raw,
               s.score, s.table_role,
               row_number() OVER (PARTITION BY t.term
                                  ORDER BY s.score DESC,
                                           -- An exact stored VALUE outranks a
                                           -- column whose NAME merely looks
                                           -- similar: the data is stronger
                                           -- evidence than the label.
                                           CASE s.match_kind
                                                WHEN 'value'  THEN 0
                                                WHEN 'column' THEN 1
                                                ELSE 2 END,
                                           s.table_name) AS rn,
               count(*) OVER (PARTITION BY t.term)        AS n_hits,
               max(s.score) OVER (PARTITION BY t.term)    AS best,
               -- Second-best score, to detect a near tie.
               nth_value(s.score, 2) OVER (
                   PARTITION BY t.term ORDER BY s.score DESC
                   ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
               )                                          AS runner_up
        FROM unnest(p_terms) AS t(term)
        CROSS JOIN LATERAL search_schema(t.term, 500, p_min_score, 20) s
    )
    SELECT term, rn::integer, match_kind, table_name, column_name,
           value_raw, score, table_role,
           -- Two readings within 10% of each other is the signal that one
           -- LLM call cannot safely choose, and the loop should run.
           (runner_up IS NOT NULL AND best > 0
            AND runner_up >= best * 0.90)               AS ambiguous
    FROM hits
    WHERE rn <= p_per_term
    ORDER BY term, rn;
$fn$;

COMMENT ON FUNCTION bind_terms(text[], numeric, integer) IS
'Binds many candidate terms to real tables, columns and stored values in '
'one round trip. Returns the top matches per term plus an ambiguity flag. '
'The basis of the cheap gate: what a question is ABOUT is discovered, '
'never classified from a list of question types.';

-- ── Is this question expressible at all ─────────────────────────────
-- Given the tables a question touched, can they actually be joined? A
-- planner asks before writing SQL rather than generating a join that
-- cannot exist.
CREATE OR REPLACE FUNCTION plan_feasibility(p_tables text[])
RETURNS TABLE (
    from_table text, to_table text, hops integer,
    path_cost bigint, best_path text, second_cost bigint, close_call boolean
) LANGUAGE sql STABLE AS $fn$
    SELECT a.t, b.t, p.hops, p.path_cost,
           array_to_string(p.path_tables, ' -> '),
           q.path_cost,
           -- Two routes of nearly equal cost mean two different questions
           -- (the city of the case address vs the city of the station).
           -- Deterministic signal to escalate.
           (q.path_cost IS NOT NULL AND p.path_cost > 0
            AND q.path_cost <= p.path_cost * 1.10)
    FROM unnest(p_tables) AS a(t)
    CROSS JOIN unnest(p_tables) AS b(t)
    LEFT JOIN LATERAL (
        SELECT * FROM find_join_paths(a.t, b.t, 6, 1)
    ) p ON true
    LEFT JOIN LATERAL (
        SELECT path_cost FROM find_join_paths(a.t, b.t, 6, 2) OFFSET 1 LIMIT 1
    ) q ON true
    WHERE a.t < b.t;
$fn$;

COMMENT ON FUNCTION plan_feasibility(text[]) IS
'Whether the tables a question touches can be joined, at what cost, and '
'whether a second route is nearly as cheap — which means the question has '
'two readings and one LLM call should not choose between them.';

COMMIT;
