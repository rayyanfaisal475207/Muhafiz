-- ============================================================
-- 041 — Dynamic relationship discovery (no hardcoded join paths)
--
-- THE PROBLEM
--
-- A person belongs to a case; a case belongs to a station; a station
-- belongs to a division, district, city and province. Chain those and
-- the number of usable paths explodes combinatorially. Writing them
-- down means:
--
--   * a path map that is wrong the moment a table is added
--   * questions that fail because nobody anticipated that particular
--     hop ("weapons by province" needs
--     weapons -> cases -> stations -> districts -> cities -> provinces,
--     five joins nobody wrote)
--   * the same brittleness as an authored vocabulary, one layer up
--
-- THE FIX
--
-- The join graph already exists — it IS the foreign keys. Postgres
-- knows every edge. So the planner reads the edges at query time and
-- finds the path itself. Nothing is enumerated, nothing is stored, and
-- adding a table extends the graph automatically.
--
-- This is the same principle as 039 applied to relationships rather
-- than values: DISCOVER, never author.
--
-- The path search is breadth-first over foreign keys, so it returns the
-- SHORTEST join path. Depth is bounded so a pathological schema cannot
-- hang the planner.
-- ============================================================

BEGIN;

-- ── The edges: every foreign key, live ──────────────────────────────
-- Direction matters for join semantics, so both directions are emitted:
-- 'child_to_parent' (many->one) and 'parent_to_child' (one->many). A
-- planner counting DISTINCT needs to know which way it traversed.
CREATE OR REPLACE VIEW schema_relationships AS
SELECT
    tc.table_name        AS from_table,
    kcu.column_name      AS from_column,
    ccu.table_name       AS to_table,
    ccu.column_name      AS to_column,
    'child_to_parent'    AS direction,
    tc.constraint_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
     ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema    = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
     ON tc.constraint_name = ccu.constraint_name
    AND tc.table_schema    = ccu.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_schema    = 'public'
UNION ALL
SELECT
    ccu.table_name, ccu.column_name,
    tc.table_name,  kcu.column_name,
    'parent_to_child',
    tc.constraint_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
     ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema    = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
     ON tc.constraint_name = ccu.constraint_name
    AND tc.table_schema    = ccu.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_schema    = 'public';

COMMENT ON VIEW schema_relationships IS
'Every foreign key as a directed edge, both ways. This IS the join '
'graph; nothing else enumerates it.';

-- ── Path finding: shortest join path between any two tables ─────────
-- Breadth-first over schema_relationships. Returns the path as an
-- ordered array of tables plus the join predicates to apply, so a
-- planner can build SQL without knowing the schema in advance.
--
--   SELECT * FROM find_join_path('weapons','provinces');
--     -> weapons -> rebuild_cases -> police_stations
--        -> police_districts -> cities -> provinces
CREATE OR REPLACE FUNCTION find_join_path(
    p_from      text,
    p_to        text,
    p_max_depth integer DEFAULT 6
) RETURNS TABLE (
    path_tables text[],
    join_steps  text[],
    hops        integer
) LANGUAGE sql STABLE AS $$
    -- Paths accumulate as delimited TEXT rather than arrays: a recursive
    -- CTE cannot infer a collation for an empty text[] seed, and text
    -- accepts an explicit COLLATE. Split back into arrays at the end.
    WITH RECURSIVE search AS (
        SELECT (p_from COLLATE "default")   AS tables,
               ''::text COLLATE "default"   AS steps,
               (p_from COLLATE "default")   AS current,
               0                            AS depth
        UNION ALL
        SELECT s.tables || '>' || r.to_table,
               CASE WHEN s.steps = '' THEN '' ELSE s.steps || '>' END
                 || format('%s.%s = %s.%s',
                           r.from_table, r.from_column,
                           r.to_table,   r.to_column),
               r.to_table,
               s.depth + 1
        FROM search s
        JOIN schema_relationships r ON r.from_table = s.current
        WHERE s.depth < p_max_depth
          -- Cycle guard: never revisit a table already on this path.
          AND ('>' || s.tables || '>') NOT LIKE ('%>' || r.to_table || '>%')
          AND s.current <> p_to
    )
    SELECT string_to_array(tables, '>'),
           CASE WHEN steps = '' THEN ARRAY[]::text[]
                ELSE string_to_array(steps, '>') END,
           depth
    FROM search
    WHERE current = p_to
    ORDER BY depth
    LIMIT 1;
$$;

COMMENT ON FUNCTION find_join_path(text, text, integer) IS
'Shortest foreign-key path between two tables, discovered live by '
'breadth-first search. Returns the table sequence and the join '
'predicates. Depth-bounded and cycle-guarded.';

-- ── What can this table reach? ──────────────────────────────────────
-- Lets the planner answer "is this question even expressible" before
-- it writes SQL, instead of generating a join that cannot exist.
CREATE OR REPLACE FUNCTION reachable_tables(
    p_from      text,
    p_max_depth integer DEFAULT 4
) RETURNS TABLE (table_name text, hops integer)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE walk AS (
        SELECT (p_from COLLATE "default") AS t,
               0                          AS depth,
               (p_from COLLATE "default") AS seen
        UNION ALL
        SELECT r.to_table, w.depth + 1, w.seen || '>' || r.to_table
        FROM walk w
        JOIN schema_relationships r ON r.from_table = w.t
        WHERE w.depth < p_max_depth
          AND ('>' || w.seen || '>') NOT LIKE ('%>' || r.to_table || '>%')
    )
    SELECT t, min(depth)::integer
    FROM walk
    WHERE t <> p_from
    GROUP BY t
    ORDER BY 2, 1;
$$;

COMMENT ON FUNCTION reachable_tables(text, integer) IS
'Every table reachable from a starting table within N foreign-key hops, '
'with the minimum hop count. Used to test expressibility before '
'generating SQL.';

COMMIT;
