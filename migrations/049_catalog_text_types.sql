-- ============================================================
-- 049 — Cast catalog identifiers to text
--
-- information_schema columns are of type sql_identifier, not text. That
-- propagates silently through any view built on them, and only surfaces
-- when a function declares RETURNS TABLE (... text ...):
--
--   ERROR: Returned type information_schema.sql_identifier does not
--          match expected type text in column 2
--
-- The views themselves looked correct and queried correctly. Every
-- catalog-derived identifier is therefore cast explicitly, so anything
-- built on these views composes without each caller rediscovering this.
-- ============================================================

BEGIN;

DROP VIEW IF EXISTS column_catalog;
DROP VIEW IF EXISTS table_roles;
DROP VIEW IF EXISTS temporal_model_violations;
DROP VIEW IF EXISTS case_fact_tables;
DROP VIEW IF EXISTS schema_relationships CASCADE;

-- ── Edges, as text ──────────────────────────────────────────────────
CREATE VIEW schema_relationships AS
SELECT tc.table_name::text   AS from_table,
       kcu.column_name::text AS from_column,
       ccu.table_name::text  AS to_table,
       ccu.column_name::text AS to_column,
       'child_to_parent'     AS direction,
       tc.constraint_name::text AS constraint_name
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
SELECT ccu.table_name::text, ccu.column_name::text,
       tc.table_name::text,  kcu.column_name::text,
       'parent_to_child',
       tc.constraint_name::text
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
'Every foreign key as a directed edge, both ways, with identifiers as '
'text. This IS the join graph; nothing else enumerates it.';

-- ── Rebuilt on the cast edges ───────────────────────────────────────
CREATE VIEW table_hub_cost AS
WITH indeg AS (
    SELECT to_table AS tbl, count(DISTINCT from_table) AS in_degree
    FROM schema_relationships
    WHERE direction = 'child_to_parent'
    GROUP BY to_table
)
SELECT tbl AS table_name, in_degree,
       (in_degree * (in_degree - 1))::bigint AS spurious_pairs,
       (1 + in_degree * in_degree)::bigint   AS enter_cost
FROM indeg;

COMMENT ON VIEW table_hub_cost IS
'Cost of routing a join THROUGH each table, from how many distinct '
'tables reference it. Measured live; nothing is named or stored.';

CREATE VIEW case_fact_tables AS
SELECT DISTINCT r.from_table AS table_name
FROM schema_relationships r
WHERE r.direction = 'child_to_parent'
  AND r.to_table  = 'rebuild_cases'
  AND NOT EXISTS (
      SELECT 1 FROM information_schema.columns c
      WHERE c.table_schema = 'public'
        AND c.table_name::text = r.from_table
        AND c.column_name = 'doc_id'
  );

COMMENT ON VIEW case_fact_tables IS
'Tables that assert things about a case: a foreign key to rebuild_cases '
'and no doc_id. Discovered, so a fact table added later appears here.';

CREATE VIEW table_roles AS
WITH cols AS (
    SELECT c.table_name::text AS tn,
           count(*)                                  AS n_cols,
           bool_or(c.column_name::text = 'doc_id')   AS has_doc_id,
           bool_or(c.column_name::text = 'span_id')  AS has_span_id,
           bool_or(c.column_name::text = 'case_id')  AS has_case_id,
           bool_or(c.column_name::text IN ('content','text','chunk_text',
                                           'body','description_ur')) AS has_text
    FROM information_schema.columns c
    JOIN information_schema.tables t
         ON t.table_schema = c.table_schema
        AND t.table_name   = c.table_name
        AND t.table_type   = 'BASE TABLE'
    WHERE c.table_schema = 'public'
    GROUP BY c.table_name
),
deg AS (
    SELECT t.tn,
           coalesce((SELECT count(DISTINCT r.from_table) FROM schema_relationships r
                      WHERE r.direction='child_to_parent' AND r.to_table = t.tn), 0) AS in_deg,
           coalesce((SELECT count(DISTINCT r.to_table) FROM schema_relationships r
                      WHERE r.direction='child_to_parent' AND r.from_table = t.tn), 0) AS out_deg
    FROM cols t
)
SELECT c.tn AS table_name,
       d.in_deg, d.out_deg, c.n_cols,
       (CASE
         WHEN d.in_deg >= 5 AND c.has_doc_id AND NOT c.has_case_id THEN 'provenance'
         WHEN c.has_doc_id AND c.has_text                          THEN 'text'
         WHEN c.tn IN (SELECT f.table_name FROM case_fact_tables f) THEN 'fact'
         WHEN c.has_doc_id                                         THEN 'text'
         WHEN d.in_deg >= 2 AND d.out_deg <= 1                     THEN 'dimension'
         WHEN d.in_deg >= 1                                        THEN 'entity'
         WHEN c.has_span_id                                        THEN 'fact'
         ELSE 'app'
       END)::text AS role
FROM cols c JOIN deg d ON d.tn = c.tn;

COMMENT ON VIEW table_roles IS
'Structural classification of every table from foreign-key shape alone. '
'Facts are counted, dimensions group, text is retrieved, provenance is '
'cited but never joined through.';

CREATE VIEW column_catalog AS
SELECT c.table_name::text   AS table_name,
       c.column_name::text  AS column_name,
       c.data_type::text    AS data_type,
       c.is_nullable = 'YES' AS nullable,
       tr.role              AS table_role,
       fk.to_table          AS references_table,
       fk.to_column         AS references_column,
       EXISTS (SELECT 1 FROM information_schema.key_column_usage k
               JOIN information_schema.table_constraints tc
                    ON tc.constraint_name = k.constraint_name
                   AND tc.constraint_type = 'PRIMARY KEY'
               WHERE k.table_schema='public'
                 AND k.table_name  = c.table_name
                 AND k.column_name = c.column_name) AS is_primary_key
FROM information_schema.columns c
JOIN information_schema.tables t
     ON t.table_schema = c.table_schema
    AND t.table_name   = c.table_name
    AND t.table_type   = 'BASE TABLE'
LEFT JOIN table_roles tr ON tr.table_name = c.table_name::text
LEFT JOIN LATERAL (
    SELECT r.to_table, r.to_column FROM schema_relationships r
    WHERE r.direction='child_to_parent'
      AND r.from_table  = c.table_name::text
      AND r.from_column = c.column_name::text
    LIMIT 1
) fk ON true
WHERE c.table_schema = 'public';

COMMENT ON VIEW column_catalog IS
'Every column with type, key status, foreign-key target and table role. '
'The structural half of the planning surface.';

CREATE VIEW temporal_model_violations AS
SELECT f.table_name,
       string_agg(c.column_name::text, ', ' ORDER BY c.column_name::text) AS shadow_columns,
       'this fact table carries its own interval columns; facts_as_of() '
       'reads valid_from/valid_to and would ignore them' AS problem
FROM case_fact_tables f
JOIN information_schema.columns c
     ON c.table_schema = 'public' AND c.table_name::text = f.table_name
WHERE c.column_name::text IN ('from_ts','to_ts','effective_from','effective_to',
                              'start_ts','end_ts','valid_start','valid_end')
GROUP BY f.table_name;

COMMENT ON VIEW temporal_model_violations IS
'Should always be empty. Non-empty means a fact table has a second, '
'competing notion of time the as-of functions cannot see.';

COMMIT;
