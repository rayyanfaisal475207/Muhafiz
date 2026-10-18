-- ============================================================
-- 042 — Ranked join paths (fixes the defect recorded as L12)
--
-- WHAT WENT WRONG IN 041
--
-- find_join_path('weapons','cities') returned, correctly and uselessly:
--
--     weapons -> doc_spans -> addresses -> cities
--
-- Structurally valid, semantically wrong. Every fact table carries
-- span_id -> doc_spans, so the provenance anchor is a hub that connects
-- everything to everything in two hops. The join runs, returns rows, and
-- counts weapons by the city of whatever address happened to share a
-- span. A confident wrong number — the one output we said is
-- unacceptable.
--
-- The path a human means is longer:
--
--     weapons -> rebuild_cases -> police_stations
--             -> police_districts -> cities
--
-- Breadth-first will never choose it while a three-hop path exists.
--
-- WHY NOT JUST EXCLUDE doc_spans
--
-- Because naming tables to avoid IS the authored-list mistake, one layer
-- up. It would be wrong the moment a new provenance table appears, and
-- it would also forbid the legitimate question "which spans support this
-- weapon", which genuinely does traverse doc_spans.
--
-- THE FIX: COST THE HOPS, DISCOVERED FROM STRUCTURE
--
-- Hub-ness is a measurable property, not an opinion. Measured live:
--
--     doc_spans          14 referencing tables
--     rebuild_cases       9
--     users               8
--     police_stations     3
--     cities              2
--
-- A table referenced by N others sits between N*(N-1) ordered pairs of
-- tables that it does not actually relate. That count IS the number of
-- spurious connections it offers, so the cost of routing THROUGH a table
-- is proportional to it. That is a derivation, not a tuning constant:
-- entering a table costs 1 + in_degree^2.
--
-- Nothing is named, nothing is stored, and a provenance table invented
-- next year is penalised automatically the moment enough tables
-- reference it.
--
-- AND: RETURN CANDIDATES, NOT AN ANSWER
--
-- Cost ranking is a heuristic and will sometimes be wrong. So the
-- function returns the top N paths with their costs and hub exposure,
-- and the planner chooses on semantics. A single answer hides the choice;
-- a ranked list makes it reviewable.
-- ============================================================

BEGIN;

-- ── Hub cost, measured live ─────────────────────────────────────────
-- in_degree is the number of DISTINCT tables holding a foreign key into
-- this one. Two columns from the same table (a case referencing both a
-- registering and a transferring station) are one relationship, not two.
CREATE OR REPLACE VIEW table_hub_cost AS
WITH indeg AS (
    SELECT to_table AS tbl, count(DISTINCT from_table) AS in_degree
    FROM schema_relationships
    WHERE direction = 'child_to_parent'
    GROUP BY to_table
)
SELECT
    tbl                                   AS table_name,
    in_degree,
    -- Ordered pairs this table offers a spurious two-hop bridge between.
    (in_degree * (in_degree - 1))::bigint AS spurious_pairs,
    (1 + in_degree * in_degree)::bigint    AS enter_cost
FROM indeg;

COMMENT ON VIEW table_hub_cost IS
'Cost of routing a join THROUGH each table, derived from how many '
'distinct tables reference it. A provenance hub is expensive because it '
'bridges pairs of tables it does not actually relate. Measured live from '
'foreign keys; nothing is named or stored.';

-- ── Ranked path search ──────────────────────────────────────────────
-- Same breadth-first walk as 041, but every partial path carries its
-- accumulated cost, and results are ordered by cost before hops. Returns
-- up to p_limit candidates so the caller can choose on meaning.
CREATE OR REPLACE FUNCTION find_join_paths(
    p_from      text,
    p_to        text,
    p_max_depth integer DEFAULT 6,
    p_limit     integer DEFAULT 5
) RETURNS TABLE (
    rank        integer,
    path_tables text[],
    join_steps  text[],
    hops        integer,
    path_cost   bigint,
    max_hub     integer,
    hub_table   text
) LANGUAGE sql STABLE AS $fn$
    -- Paths accumulate as delimited TEXT: a recursive CTE cannot infer a
    -- collation for an empty text[] seed, but text accepts COLLATE.
    -- Split back into arrays at the end.
    WITH RECURSIVE search AS (
        SELECT (p_from COLLATE "default")  AS tables,
               ''::text COLLATE "default"  AS steps,
               (p_from COLLATE "default")  AS current,
               0                           AS depth,
               0::bigint                   AS cost,
               0::bigint                   AS worst_hub,
               ''::text COLLATE "default"  AS worst_tbl
        UNION ALL
        SELECT s.tables || '>' || r.to_table,
               CASE WHEN s.steps = '' THEN '' ELSE s.steps || '>' END
                 || format('%s.%s = %s.%s',
                           r.from_table, r.from_column,
                           r.to_table,   r.to_column),
               r.to_table,
               s.depth + 1,
               s.cost + coalesce(h.enter_cost, 1),
               -- Track the single most generic table crossed, so a caller
               -- can see WHY a path was expensive rather than only that
               -- it was.
               greatest(s.worst_hub, coalesce(h.in_degree, 0)),
               CASE WHEN coalesce(h.in_degree, 0) > s.worst_hub
                    THEN r.to_table ELSE s.worst_tbl END
        FROM search s
        JOIN schema_relationships r ON r.from_table = s.current
        LEFT JOIN table_hub_cost h  ON h.table_name = r.to_table
        WHERE s.depth < p_max_depth
          -- Cycle guard: never revisit a table already on this path.
          AND ('>' || s.tables || '>') NOT LIKE ('%>' || r.to_table || '>%')
          AND s.current <> p_to
    ),
    hits AS (
        SELECT DISTINCT ON (tables)
               tables, steps, depth, cost, worst_hub, worst_tbl
        FROM search
        WHERE current = p_to
    )
    SELECT (row_number() OVER (ORDER BY cost, depth, tables))::integer,
           string_to_array(tables, '>'),
           CASE WHEN steps = '' THEN ARRAY[]::text[]
                ELSE string_to_array(steps, '>') END,
           depth,
           cost,
           worst_hub::integer,
           nullif(worst_tbl, '')
    FROM hits
    ORDER BY cost, depth, tables
    LIMIT p_limit;
$fn$;

COMMENT ON FUNCTION find_join_paths(text, text, integer, integer) IS
'Top-N foreign-key paths between two tables, ranked by accumulated hub '
'cost rather than hop count, so a short route through a provenance '
'anchor loses to a longer meaningful one. Returns candidates, not an '
'answer: the planner picks on semantics and can see max_hub/hub_table '
'to explain the ranking.';

-- ── Compatibility wrapper ───────────────────────────────────────────
-- 041 shipped a singular find_join_path that returned the shortest path.
-- Same signature, but now the CHEAPEST path. Callers that want to choose
-- should use find_join_paths.
DROP FUNCTION IF EXISTS find_join_path(text, text, integer);

CREATE FUNCTION find_join_path(
    p_from      text,
    p_to        text,
    p_max_depth integer DEFAULT 6
) RETURNS TABLE (
    path_tables text[],
    join_steps  text[],
    hops        integer
) LANGUAGE sql STABLE AS $fn$
    SELECT path_tables, join_steps, hops
    FROM find_join_paths(p_from, p_to, p_max_depth, 1);
$fn$;

COMMENT ON FUNCTION find_join_path(text, text, integer) IS
'Single best join path by hub cost. Thin wrapper over find_join_paths; '
'prefer the plural, which exposes the alternatives that were rejected.';

COMMIT;
