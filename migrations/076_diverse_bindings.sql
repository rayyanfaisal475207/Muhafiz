-- ============================================================
-- 076 — One kind of evidence must not crowd out the others
--
-- MEASURED: bind_terms('weapon') returned three rows, all of them
-- `contains` matches inside case_events.event_type_raw:
--
--   'arrest and weapon seizure'  1.000
--   'weapon_recovery'            1.000
--   'weapon seizure'             1.000
--
-- The `weapons` TABLE also scores 1.000 — and never appeared, because the
-- per-term limit was filled by three near-identical containment hits
-- before the table match was reached.
--
-- The agent then answered "how many cases have a weapon" from
-- seized_items.category: 4, where the truth is 32.
--
-- THE RULE
--
-- Keep the best of EACH match kind before taking more of any one kind. A
-- table match, a value match and a containment match are different KINDS
-- of evidence about the same word, and the one that matters most varies by
-- question. Three phrasings of the same containment are one piece of
-- evidence repeated; a table name is another piece entirely.
--
-- This is the same mistake as ranking a stored value against a
-- similar-sounding identifier: treating incomparable evidence as points on
-- one scale. Here it truncated rather than mis-ranked.
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
    WITH raw AS (
        SELECT DISTINCT ON (t.term, s.table_name, s.column_name,
                            coalesce(s.value_raw, ''))
               t.term, s.match_kind, s.table_name, s.column_name,
               s.value_raw, s.score, s.table_role
        FROM unnest(p_terms) AS t(term)
        CROSS JOIN LATERAL search_schema(t.term, 5000, p_min_score, 20) s
        ORDER BY t.term, s.table_name, s.column_name,
                 coalesce(s.value_raw, ''), s.score DESC
    ),
    -- Rank WITHIN each kind first, so every kind gets a seat.
    per_kind AS (
        SELECT r.*,
               row_number() OVER (PARTITION BY r.term, r.match_kind
                                  ORDER BY r.score DESC, r.table_name)
                   AS kind_rank
        FROM raw r
    ),
    hits AS (
        SELECT p.*,
               row_number() OVER (
                   PARTITION BY p.term
                   -- One from each kind, then a second from each, and so
                   -- on. A kind with many near-identical hits contributes
                   -- its best before contributing its second.
                   ORDER BY p.kind_rank,
                            p.score DESC,
                            CASE p.match_kind
                                 WHEN 'value'        THEN 0
                                 WHEN 'table'        THEN 1
                                 WHEN 'column'       THEN 2
                                 WHEN 'contains'     THEN 3
                                 ELSE 4 END,
                            p.table_name)             AS rn,
               max(p.score) OVER (PARTITION BY p.term) AS best,
               nth_value(p.score, 2) OVER (
                   PARTITION BY p.term ORDER BY p.score DESC
                   ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
               )                                       AS runner_up
        FROM per_kind p
    )
    SELECT term, rn::integer, match_kind, table_name, column_name,
           value_raw, score, table_role,
           (runner_up IS NOT NULL AND best > 0
            AND runner_up >= best * 0.90)              AS ambiguous
    FROM hits
    WHERE rn <= p_per_term
    ORDER BY term, rn;
$fn$;

COMMENT ON FUNCTION bind_terms(text[], numeric, integer) IS
'Binds terms to schema and values, keeping the best of EACH match kind '
'before taking more of any one. Three phrasings of one containment are '
'one piece of evidence repeated; a table name is a different kind, and '
'truncating it away cost a correct answer.';

COMMIT;
