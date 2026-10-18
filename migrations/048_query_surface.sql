-- ============================================================
-- 048 — The runtime query surface
--
-- THE POINT THIS ANSWERS
--
-- The twenty questions pulled from chat history are a SAMPLE of a space,
-- not a specification. Building handling for "count accused by gender"
-- and "cases per station" is the authored-list mistake moved up to the
-- query layer: it works for the twenty and fails on the twenty-first.
--
-- There is no finite set of questions. What there IS, is a finite and
-- knowable amount of structure — and a question is answerable when a
-- planner can find, at runtime, which part of that structure it concerns.
--
-- So nothing here knows about counting, or gender, or police stations.
-- It exposes what exists, precisely enough that a sub-agent can compose
-- a query for a question no one anticipated:
--
--     what tables exist and what ROLE does each play
--     what columns exist, of what type, how populated
--     what VALUES actually occur, in whatever script
--     how any two tables can be JOINED, ranked by meaning
--     what the answer must DISCLOSE about its own coverage
--
-- WHY THIS IS NOT JUST "SHOW THE SCHEMA"
--
-- A schema dump gives names and types. It does not say that
-- investigation_status is 71% null, that license_status holds
-- 'بغیر لائسنس' and never 'unlicensed', that doc_spans bridges every
-- table meaninglessly, or that a column with 64 distinct values in 73
-- rows must be searched rather than filtered. Those are the facts that
-- decide whether a generated query returns the truth or a plausible
-- wrong number, and every one of them is measured, not declared.
--
-- ALL DERIVED
--
-- Nothing here is authored. Roles come from foreign-key structure, value
-- spaces from the data, join costs from in-degree, coverage from null
-- rates. Add a table tomorrow and it appears, classified, with no edit.
-- ============================================================

BEGIN;

-- ── What kind of thing is each table ────────────────────────────────
-- Role decides how a planner may use a table, and it is readable from
-- structure alone:
--
--   text        carries source text        -> retrieve, never aggregate
--   provenance  anchors facts to text      -> cite, never join through
--   fact        asserts things about cases -> count and filter these
--   dimension   referenced, rarely refers  -> group and label by these
--   entity      referred to, has identity  -> join targets
--   app         operational, not evidence  -> never answer from these
CREATE OR REPLACE VIEW table_roles AS
WITH cols AS (
    SELECT c.table_name AS tn,
           count(*)                                        AS n_cols,
           bool_or(c.column_name = 'doc_id')               AS has_doc_id,
           bool_or(c.column_name = 'span_id')              AS has_span_id,
           bool_or(c.column_name = 'case_id')              AS has_case_id,
           bool_or(c.column_name IN ('content','text','chunk_text',
                                     'body','description_ur'))
                                                           AS has_text
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
       CASE
         -- Provenance first: it is the trap L12 was about. A table that
         -- many others point INTO and that itself anchors text.
         WHEN d.in_deg >= 5 AND c.has_doc_id AND NOT c.has_case_id THEN 'provenance'
         WHEN c.has_doc_id AND c.has_text                          THEN 'text'
         WHEN c.tn IN (SELECT f.table_name FROM case_fact_tables f) THEN 'fact'
         WHEN c.has_doc_id                                         THEN 'text'
         -- Referenced by others while referring to few: a lookup.
         WHEN d.in_deg >= 2 AND d.out_deg <= 1                     THEN 'dimension'
         WHEN d.in_deg >= 1                                        THEN 'entity'
         WHEN c.has_span_id                                        THEN 'fact'
         ELSE 'app'
       END AS role
FROM cols c JOIN deg d ON d.tn = c.tn;

COMMENT ON VIEW table_roles IS
'Structural classification of every table, from foreign-key shape alone. '
'Tells a planner how a table may be used: facts are counted, dimensions '
'group, text is retrieved, provenance is cited but never joined through.';

-- ── Every column, with everything a planner needs to decide ─────────
CREATE OR REPLACE VIEW column_catalog AS
SELECT c.table_name,
       c.column_name,
       c.data_type,
       c.is_nullable = 'YES'                      AS nullable,
       tr.role                                    AS table_role,
       fk.to_table                                AS references_table,
       fk.to_column                               AS references_column,
       EXISTS (SELECT 1 FROM information_schema.key_column_usage k
               JOIN information_schema.table_constraints tc
                    ON tc.constraint_name = k.constraint_name
                   AND tc.constraint_type = 'PRIMARY KEY'
               WHERE k.table_schema='public'
                 AND k.table_name = c.table_name
                 AND k.column_name = c.column_name) AS is_primary_key
FROM information_schema.columns c
JOIN information_schema.tables t
     ON t.table_schema = c.table_schema
    AND t.table_name   = c.table_name
    AND t.table_type   = 'BASE TABLE'
LEFT JOIN table_roles tr ON tr.table_name = c.table_name
LEFT JOIN LATERAL (
    SELECT r.to_table, r.to_column FROM schema_relationships r
    WHERE r.direction='child_to_parent'
      AND r.from_table = c.table_name
      AND r.from_column = c.column_name
    LIMIT 1
) fk ON true
WHERE c.table_schema = 'public';

COMMENT ON VIEW column_catalog IS
'Every column with its type, key status, foreign-key target and the role '
'of its table. The structural half of the planning surface; profiling '
'supplies the statistical half.';

-- ── Find where a term lives, by NAME and by VALUE ───────────────────
-- The capability that makes runtime query generation possible. A planner
-- holding the word 'گرفتار' or 'unlicensed' or 'Karachi' asks where it
-- occurs, and gets told — including when the stored form is in another
-- script entirely, which is the failure that started this rebuild
-- (filtering 'unlicensed' against 'بغیر لائسنس' and returning 0 where
-- the truth was 30).
--
-- Bounded deliberately: only enumerable text columns are value-scanned,
-- because scanning a free-text column is a search problem, not a lookup,
-- and belongs to the vector and full-text layers.
CREATE OR REPLACE FUNCTION search_schema(
    p_term         text,
    p_max_distinct integer DEFAULT 500,
    p_min_score    numeric DEFAULT 0.30,
    p_limit        integer DEFAULT 40
) RETURNS TABLE (
    match_kind   text,        -- 'table' | 'column' | 'value'
    table_name   text,
    column_name  text,
    value_raw    text,
    score        numeric,
    table_role   text,
    note         text
) LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_skel text := normalize_name(p_term);
    r      record;
    p      record;
BEGIN
    -- Names first: cheap, and often decisive.
    RETURN QUERY
    SELECT 'table', tr.table_name, NULL::text, NULL::text,
           round(similarity(normalize_name(tr.table_name), v_skel)::numeric, 3),
           tr.role,
           'table name resembles the term'
    FROM table_roles tr
    WHERE similarity(normalize_name(tr.table_name), v_skel) >= p_min_score;

    RETURN QUERY
    SELECT 'column', cc.table_name, cc.column_name, NULL::text,
           round(similarity(normalize_name(cc.column_name), v_skel)::numeric, 3),
           cc.table_role,
           coalesce('foreign key to ' || cc.references_table,
                    'column name resembles the term')
    FROM column_catalog cc
    WHERE similarity(normalize_name(cc.column_name), v_skel) >= p_min_score;

    -- Values: scan only enumerable text columns of tables that can carry
    -- evidence. This is what finds 'بغیر لائسنس' from 'unlicensed'
    -- once an alias exists, and finds exact stored forms always.
    FOR r IN
        SELECT cc.table_name AS tn, cc.column_name AS cn
        FROM column_catalog cc
        WHERE cc.data_type IN ('text','character varying','character')
          AND cc.table_role IN ('fact','dimension','entity')
          AND NOT cc.is_primary_key
          AND cc.references_table IS NULL
    LOOP
        BEGIN
            SELECT * INTO p FROM profile_column(r.tn, r.cn, 1, 1.01, p_max_distinct);
            CONTINUE WHEN NOT p.enumerable;

            RETURN QUERY EXECUTE format($d$
                SELECT 'value', %L, %L, v::text,
                       round(CASE WHEN normalize_name(v::text) = $1 THEN 1.0
                                  ELSE similarity(normalize_name(v::text), $1)
                             END::numeric, 3),
                       %L,
                       'stored value; filter this column by exactly this string'
                FROM (SELECT DISTINCT %I AS v FROM %I WHERE %I IS NOT NULL) s
                WHERE normalize_name(v::text) = $1
                   OR similarity(normalize_name(v::text), $1) >= $2
            $d$, r.tn, r.cn, (SELECT role FROM table_roles WHERE table_name=r.tn),
                 r.cn, r.tn, r.cn)
            USING v_skel, p_min_score;
        EXCEPTION WHEN OTHERS THEN
            CONTINUE;   -- an unscannable column must not abort the search
        END;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION search_schema(text, integer, numeric, integer) IS
'Where does this term live — as a table name, a column name, or an '
'actual stored value. Cross-script by normalisation. The primitive that '
'lets a planner bind a word from a question to real schema and real '
'values instead of guessing what the database might contain.';

-- ── A compact, machine-readable contract for one table ──────────────
-- What a sub-agent should read before writing SQL against a table.
CREATE OR REPLACE FUNCTION describe_table(p_table text)
RETURNS jsonb LANGUAGE plpgsql STABLE AS $fn$
DECLARE result jsonb; cols jsonb;
BEGIN
    SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
               'column',        cc.column_name,
               'type',          cc.data_type,
               'primary_key',   nullif(cc.is_primary_key, false),
               'references',    cc.references_table,
               'distinct',      pr.distinct_count,
               'null_rate',     pr.null_rate,
               'verdict',       pr.verdict,
               'enumerable',    nullif(pr.enumerable, false),
               'exhaustive',    nullif(pr.exhaustive, false)
           )) ORDER BY cc.column_name)
    INTO cols
    FROM column_catalog cc
    LEFT JOIN LATERAL profile_column(cc.table_name, cc.column_name) pr ON true
    WHERE cc.table_name = p_table;

    SELECT jsonb_build_object(
        'table',    p_table,
        'role',     (SELECT role FROM table_roles WHERE table_name = p_table),
        'rows',     (SELECT row_count FROM profile_column(
                        p_table, (SELECT column_name FROM column_catalog
                                   WHERE table_name = p_table LIMIT 1))),
        'columns',  cols,
        'joins_to', (SELECT jsonb_agg(DISTINCT r.to_table)
                       FROM schema_relationships r
                      WHERE r.direction='child_to_parent'
                        AND r.from_table = p_table),
        'temporal', (SELECT count(*) > 0 FROM information_schema.columns
                      WHERE table_schema='public' AND table_name = p_table
                        AND column_name = 'valid_to')
    ) INTO result;

    RETURN result;
END;
$fn$;

COMMENT ON FUNCTION describe_table(text) IS
'One table as a compact JSON contract: role, row count, every column '
'with its measured profile, join targets, and whether it is temporal. '
'What a sub-agent reads before composing SQL.';

-- ── Coverage: what an answer is obliged to disclose ─────────────────
-- Predicted failure F7, made checkable. A filter on a column that is
-- mostly null returns a small plausible number by discarding most of the
-- population invisibly. Before running a filter, a planner asks here,
-- and the answer must carry the caveat this returns.
CREATE OR REPLACE FUNCTION coverage_warning(
    p_table text, p_column text, p_threshold numeric DEFAULT 0.10
) RETURNS TABLE (
    must_disclose boolean, null_rate numeric, populated bigint,
    total bigint, message text
) LANGUAGE plpgsql STABLE AS $fn$
DECLARE p record;
BEGIN
    SELECT * INTO p FROM profile_column(p_table, p_column);
    null_rate := p.null_rate;
    populated := p.non_null_count;
    total     := p.row_count;

    IF p.row_count = 0 THEN
        must_disclose := true;
        message := format('%s holds no rows; a count here is not zero, it '
                          'is unknown', p_table);
    ELSIF p.non_null_count = 0 THEN
        must_disclose := true;
        message := format('every row of %s.%s is empty; any filter on it '
                          'matches nothing for reasons unrelated to the '
                          'question', p_table, p_column);
    ELSIF p.null_rate > p_threshold THEN
        must_disclose := true;
        message := format('%s.%s is recorded for only %s of %s rows (%s%% '
                          'missing); a filter on it silently excludes the '
                          'rest, so the answer must state this coverage',
                          p_table, p_column, p.non_null_count, p.row_count,
                          round(p.null_rate * 100, 1));
    ELSE
        must_disclose := false;
        message := format('%s.%s is recorded for %s of %s rows',
                          p_table, p_column, p.non_null_count, p.row_count);
    END IF;
    RETURN NEXT;
END;
$fn$;

COMMENT ON FUNCTION coverage_warning(text, text, numeric) IS
'Whether an answer filtered on this column must disclose its coverage. '
'Turns predicted failure F7 — plausible small numbers from sparse '
'columns — into a check a planner is required to run.';

COMMIT;
