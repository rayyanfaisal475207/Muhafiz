-- ============================================================
-- 060 — Two defects found by testing the binder
--
-- DEFECT 1: GENERATED COLUMNS WERE SEARCHED AS IF THEY HELD FACTS
--
--   bind_terms('Karachi') ->
--     cross_script  places.name_raw       'کراچی ایسٹ'  0.267
--     cross_script  places.name_skeleton  'کراچی ایسٹ'  0.267   <- same fact
--
-- name_skeleton is GENERATED ALWAYS AS normalize_name(name_raw). It is a
-- derivation of the column beside it, so matching both reports one piece
-- of evidence twice — and doubling the evidence for a value is exactly
-- how a planner ends up confident about the wrong one.
--
-- Detected structurally: pg_attribute.attgenerated marks them. No list.
--
-- DEFECT 2: 'کراچی' DOES NOT EXIST, SO A KARACHI QUESTION SPLITS
--
-- Ingestion read the header field, which names the POLICE DISTRICT:
--
--   کراچی ایسٹ    kind_raw = 'city'
--   کراچی وسطی    kind_raw = 'city'
--   کراچی         -- absent
--
-- So "cases in Karachi" binds to two places and counts one of them, or
-- flags ambiguous and stops. Neither is the answer. This is precisely
-- what migration 035's comment predicted before any data was loaded.
--
-- WHAT CAN BE DERIVED, AND WHAT CANNOT
--
-- One thing IS derivable: 'کراچی ایسٹ' contains 'کراچی'. Name containment
-- between two places is a containment CANDIDATE — structural, computed
-- from the data, no gazetteer.
--
-- What containment cannot tell you is whether the shared prefix means
-- administrative containment or coincidence. 'نیو کراچی' contains 'کراچی'
-- too, and is a different place. So containment PROPOSES; it never
-- decides — the same discipline as person identity in 055.
--
-- Confirmation is world knowledge: whether Karachi East is part of
-- Karachi is a public fact about Pakistan, not a fact about this corpus.
-- A web lookup settles it, and settling it changes no case fact — it only
-- stops a count from being silently halved.
-- ============================================================

BEGIN;

-- ── Generated columns are derivations, not evidence ─────────────────
DROP MATERIALIZED VIEW IF EXISTS column_catalog CASCADE;

CREATE MATERIALIZED VIEW column_catalog AS
SELECT c.relname::text                        AS table_name,
       a.attname::text                        AS column_name,
       format_type(a.atttypid, a.atttypmod)   AS data_type,
       CASE WHEN a.atttypid IN ('text'::regtype, 'varchar'::regtype,
                                'bpchar'::regtype) THEN true ELSE false END
                                              AS is_text,
       NOT a.attnotnull                       AS nullable,
       a.attnum                               AS ordinal,
       -- A generated column restates the column it derives from, so it is
       -- never independent evidence.
       (a.attgenerated <> '')                 AS is_generated,
       fk.to_table                            AS references_table,
       fk.to_column                           AS references_column,
       coalesce(pk.is_pk, false)              AS is_primary_key
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN LATERAL (
    SELECT r.to_table, r.to_column FROM schema_relationships r
    WHERE r.direction = 'child_to_parent'
      AND r.from_table = c.relname::text
      AND r.from_column = a.attname::text
    LIMIT 1
) fk ON true
LEFT JOIN LATERAL (
    SELECT true AS is_pk FROM pg_constraint con
    WHERE con.conrelid = c.oid AND con.contype = 'p'
      AND a.attnum = ANY(con.conkey)
    LIMIT 1
) pk ON true
WHERE c.relkind = 'r';

CREATE INDEX idx_colcat_table ON column_catalog (table_name);
CREATE INDEX idx_colcat_col   ON column_catalog (column_name);

COMMENT ON MATERIALIZED VIEW column_catalog IS
'Every column with type, key status, foreign-key target and whether it is '
'GENERATED. A generated column restates its source and is excluded from '
'value search, so one fact is not counted as two pieces of evidence.';

-- The CASCADE also dropped everything built on column_catalog. Rebuilt
-- here unchanged, in dependency order.
CREATE OR REPLACE VIEW case_fact_tables AS
SELECT DISTINCT r.from_table AS table_name
FROM schema_relationships r
WHERE r.direction = 'child_to_parent'
  AND r.to_table  = 'rebuild_cases'
  AND NOT EXISTS (
      SELECT 1 FROM column_catalog c
      WHERE c.table_name = r.from_table AND c.column_name = 'doc_id');

CREATE MATERIALIZED VIEW evidence_tables AS
WITH RECURSIVE walk AS (
    SELECT c.table_name AS tbl, c.table_name AS origin, 0 AS depth,
           c.table_name AS seen
    FROM (SELECT DISTINCT table_name FROM column_catalog) c
    UNION ALL
    SELECT r.to_table, w.origin, w.depth + 1, w.seen || '>' || r.to_table
    FROM walk w
    JOIN schema_relationships r
         ON r.from_table = w.tbl AND r.direction = 'child_to_parent'
    WHERE w.depth < 4
      AND ('>' || w.seen || '>') NOT LIKE ('%>' || r.to_table || '>%')
)
SELECT origin AS table_name, min(depth) AS hops_to_provenance
FROM walk WHERE tbl = 'doc_spans' GROUP BY origin;

CREATE UNIQUE INDEX idx_evtables ON evidence_tables (table_name);

CREATE OR REPLACE VIEW temporal_model_violations AS
SELECT f.table_name,
       string_agg(c.column_name, ', ' ORDER BY c.column_name) AS shadow_columns,
       'this fact table carries its own interval columns; facts_as_of() '
       'reads valid_from/valid_to and would ignore them' AS problem
FROM case_fact_tables f
JOIN column_catalog c ON c.table_name = f.table_name
WHERE c.column_name IN ('from_ts','to_ts','effective_from','effective_to',
                        'start_ts','end_ts','valid_start','valid_end')
GROUP BY f.table_name;

CREATE MATERIALIZED VIEW table_roles AS
WITH cols AS (
    SELECT cc.table_name AS tn,
           count(*)                                   AS n_cols,
           bool_or(cc.column_name = 'doc_id')         AS has_doc_id,
           bool_or(cc.column_name = 'span_id')        AS has_span_id,
           bool_or(cc.column_name = 'case_id')        AS has_case_id,
           bool_or(cc.column_name IN ('content','text','chunk_text',
                                      'body','description_ur')) AS has_text
    FROM column_catalog cc GROUP BY cc.table_name
),
deg AS (
    SELECT t.tn,
           coalesce((SELECT count(DISTINCT r.from_table) FROM schema_relationships r
                      WHERE r.direction='child_to_parent' AND r.to_table = t.tn),0) AS in_deg,
           coalesce((SELECT count(DISTINCT r.to_table) FROM schema_relationships r
                      WHERE r.direction='child_to_parent' AND r.from_table = t.tn),0) AS out_deg
    FROM cols t
),
facts AS (SELECT table_name FROM case_fact_tables)
SELECT c.tn AS table_name, d.in_deg, d.out_deg, c.n_cols,
       (CASE
         WHEN d.in_deg >= 5 AND c.has_doc_id AND NOT c.has_case_id THEN 'provenance'
         WHEN c.has_doc_id AND c.has_text                          THEN 'text'
         WHEN c.tn IN (SELECT table_name FROM facts)               THEN 'fact'
         WHEN c.has_doc_id                                         THEN 'text'
         WHEN d.in_deg >= 2 AND d.out_deg <= 1                     THEN 'dimension'
         WHEN d.in_deg >= 1                                        THEN 'entity'
         WHEN c.has_span_id                                        THEN 'fact'
         ELSE 'app'
       END)::text AS role,
       (c.tn = 'doc_spans'
        OR EXISTS (SELECT 1 FROM evidence_tables e WHERE e.table_name = c.tn))
                                                                   AS is_evidence
FROM cols c JOIN deg d ON d.tn = c.tn;

CREATE UNIQUE INDEX idx_troles_table ON table_roles (table_name);

-- ── Containment candidates between places ───────────────────────────
-- Structural only: one place's normalised name appears inside another's.
-- Emphatically a CANDIDATE — 'نیو کراچی' contains 'کراچی' and is a
-- different place — so this produces rows to confirm, never edges.
CREATE OR REPLACE VIEW place_containment_candidates AS
SELECT child.place_id    AS child_id,
       child.name_raw    AS child_name,
       child.kind_raw    AS child_kind,
       parent.place_id   AS parent_id,
       parent.name_raw   AS parent_name,
       parent.kind_raw   AS parent_kind,
       -- How much of the child's name the parent accounts for. A near-1
       -- ratio means the child is mostly the parent's name plus a
       -- qualifier, which is the administrative-subdivision shape.
       round(length(parent.name_skeleton)::numeric
             / nullif(length(child.name_skeleton), 0), 3) AS name_overlap,
       -- Where the parent name sits. A LEADING match ('کراچی ایسٹ') is the
       -- subdivision pattern; a trailing one ('نیو کراچی') is more often a
       -- distinct locality named after it. Reported, not acted on.
       (position(parent.name_skeleton in child.name_skeleton) = 1) AS parent_leads,
       EXISTS (SELECT 1 FROM place_hierarchy h
                WHERE h.child_id = child.place_id
                  AND h.parent_id = parent.place_id)      AS already_linked
FROM places child
JOIN places parent
  ON child.place_id <> parent.place_id
 AND parent.name_skeleton IS NOT NULL
 AND child.name_skeleton IS NOT NULL
 AND length(parent.name_skeleton) >= 3
 AND length(parent.name_skeleton) < length(child.name_skeleton)
 AND position(parent.name_skeleton in child.name_skeleton) > 0;

COMMENT ON VIEW place_containment_candidates IS
'Places whose name contains another place''s name: a containment '
'CANDIDATE, derived from data with no gazetteer. Name containment cannot '
'distinguish administrative subdivision from coincidence, so every row '
'here needs confirmation before becoming a place_hierarchy edge.';

COMMIT;
