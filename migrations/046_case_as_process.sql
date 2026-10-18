-- ============================================================
-- 046 — A case is a process, not a record
--
-- WHAT THE MODEL GOT WRONG
--
-- Everything built so far treats a case as a row with fixed attributes
-- and ONE source document:
--
--     rebuild_cases.source_doc_id -> rebuild_documents.doc_id
--
-- That is false about how a case actually exists. An FIR opens a file.
-- After it come daily station diary entries (روزنامچہ) and investigation
-- reports (ضمنی), arrest and recovery memos, the challan, court orders,
-- adjournments, judgement — and after judgement, appeal. The file keeps
-- accruing documents, and it does not stop at a terminal state.
--
-- THREE CONSEQUENCES THE OLD MODEL COULD NOT HANDLE
--
-- 1. FACTS CHANGE, AND THE OLD FACT WAS NOT WRONG
--    A person recorded as a witness in the FIR can be an accused by the
--    challan. Overwriting loses that this changed and when. Keeping both
--    rows without time makes every count double. Neither is acceptable.
--
-- 2. "COMPARED TO A COUPLE OF YEARS BACK" NEEDS THE PAST STATE
--    A real question asked of this system. With only current attributes,
--    the past is unrecoverable — you can filter on registration date, but
--    you cannot ask what a case's STATUS was then, because the status
--    column holds only today's.
--
-- 3. THERE IS NO CLOSED STATE TO STORE
--    Any enum with 'closed' in it is wrong, because an appeal reopens.
--    Phase must be DERIVED from the latest activity and must always
--    admit that more can arrive.
--
-- THE FIX: BITEMPORAL ASSERTIONS
--
-- Every case fact records two different times, which are routinely
-- confused and are not the same thing:
--
--   valid_from / valid_to   when it was true IN THE WORLD
--   asserted_at             when a document SAID so
--
-- A challan filed in March stating an arrest happened in January has
-- asserted_at = March, valid_from = January. Asking "who was under
-- arrest in February" needs valid time; asking "what did we believe in
-- February" needs assertion time. Both are real questions and one column
-- cannot answer both.
--
-- A fact is never updated or deleted. It is superseded: valid_to is
-- closed and a new row opens. The old row remains queryable with its
-- span, so the history of what was believed is auditable.
--
-- WHICH TABLES GET THIS — DISCOVERED, NOT LISTED
--
-- A "case fact table" is not something to enumerate by hand. It is any
-- table carrying both case_id and span_id: something asserted about a
-- case, traceable to source text. That is read from the catalog, so a
-- fact table added later gets temporal columns by re-running this, with
-- no edit here.
-- ============================================================

BEGIN;

-- ── The document stream ─────────────────────────────────────────────
-- rebuild_documents.case_id allows many documents per case, but nothing
-- ordered them or said what part each plays in the process. And a single
-- document legitimately concerns several cases — a court order covering
-- co-accused tried together, a recovery memo listing items from three
-- FIRs — which one nullable column on the document cannot express.
CREATE TABLE case_documents (
    case_id       text NOT NULL REFERENCES rebuild_cases (case_id) ON DELETE CASCADE,
    doc_id        text NOT NULL REFERENCES rebuild_documents (doc_id) ON DELETE CASCADE,
    -- The part this document plays, exactly as the source names it:
    -- 'ابتدائی اطلاعی رپورٹ', 'ضمنی', 'روزنامچہ', 'چالان', court order.
    -- Never validated against a list; discover_values answers what exists.
    doc_role_raw  text,
    doc_role_key  text,
    -- The date the DOCUMENT bears, which is not when it was ingested.
    -- A zimni written in January and scanned in June is a January fact.
    document_date timestamptz,
    -- Position in the file. Explicit because document_date is routinely
    -- missing in this corpus, and the physical order of a file is itself
    -- evidence of sequence.
    sequence_no   integer,
    received_at   timestamptz NOT NULL DEFAULT now(),
    span_id       bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL,
    confidence    numeric,
    PRIMARY KEY (case_id, doc_id)
);

CREATE INDEX idx_casedocs_case ON case_documents (case_id, sequence_no, document_date);
CREATE INDEX idx_casedocs_doc  ON case_documents (doc_id);
CREATE INDEX idx_casedocs_role ON case_documents (doc_role_raw);

COMMENT ON TABLE case_documents IS
'The stream of documents that make up a case file, ordered, each with '
'its role in the process. A case is this stream; rebuild_cases holds '
'only what the opening document established.';

-- ── Temporal columns, applied to discovered fact tables ─────────────
-- A case fact table is any table with both case_id and span_id: asserted
-- about a case, traceable to text. Discovered from the catalog so this
-- extends itself rather than needing an edit per new table.
CREATE OR REPLACE FUNCTION apply_temporal_columns(p_schema text DEFAULT 'public')
RETURNS TABLE (table_name text, action text)
LANGUAGE plpgsql AS $fn$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.table_name AS tn
        FROM information_schema.columns c
        JOIN information_schema.tables t
             ON t.table_schema = c.table_schema
            AND t.table_name   = c.table_name
            AND t.table_type   = 'BASE TABLE'
        WHERE c.table_schema = p_schema
          AND c.column_name IN ('case_id', 'span_id')
        GROUP BY c.table_name
        HAVING count(DISTINCT c.column_name) = 2
        ORDER BY 1
    LOOP
        EXECUTE format($d$
            ALTER TABLE %I
              ADD COLUMN IF NOT EXISTS asserted_by_doc_id text
                  REFERENCES rebuild_documents (doc_id),
              ADD COLUMN IF NOT EXISTS asserted_at  timestamptz,
              ADD COLUMN IF NOT EXISTS valid_from   timestamptz,
              ADD COLUMN IF NOT EXISTS valid_to     timestamptz,
              ADD COLUMN IF NOT EXISTS superseded_by_doc_id text
                  REFERENCES rebuild_documents (doc_id)
        $d$, r.tn);

        -- Current facts are the overwhelmingly common query, so they get
        -- the index. valid_to IS NULL means "still true as far as any
        -- document says", which is the default reading of any count.
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I (case_id) WHERE valid_to IS NULL',
            'idx_' || left(r.tn, 40) || '_current', r.tn);

        table_name := r.tn; action := 'temporal columns applied';
        RETURN NEXT;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION apply_temporal_columns(text) IS
'Adds bitemporal assertion columns to every discovered case-fact table '
'(one carrying both case_id and span_id). Idempotent, and self-extending: '
're-running picks up fact tables added later.';

SELECT * FROM apply_temporal_columns();

-- ── As-of queries over any fact table ───────────────────────────────
-- Generic because the alternative is a hand-written as-of view per table,
-- which is the authored-list mistake again. Returns rows as jsonb so one
-- function serves every table shape.
--
--   -- who was recorded as accused in this case two years ago
--   SELECT facts_as_of('case_parties', now() - interval '2 years');
CREATE OR REPLACE FUNCTION facts_as_of(
    p_table text,
    p_ts    timestamptz,
    p_basis text DEFAULT 'valid'          -- 'valid' | 'asserted'
) RETURNS SETOF jsonb
LANGUAGE plpgsql STABLE AS $fn$
BEGIN
    IF p_basis NOT IN ('valid', 'asserted') THEN
        RAISE EXCEPTION
          'basis must be valid (true in the world) or asserted (believed then), got %',
          p_basis;
    END IF;

    IF p_basis = 'valid' THEN
        RETURN QUERY EXECUTE format($d$
            SELECT to_jsonb(t) FROM %I t
            WHERE (t.valid_from IS NULL OR t.valid_from <= $1)
              AND (t.valid_to   IS NULL OR t.valid_to   >  $1)
        $d$, p_table) USING p_ts;
    ELSE
        RETURN QUERY EXECUTE format($d$
            SELECT to_jsonb(t) FROM %I t
            WHERE t.asserted_at IS NULL OR t.asserted_at <= $1
        $d$, p_table) USING p_ts;
    END IF;
END;
$fn$;

COMMENT ON FUNCTION facts_as_of(text, timestamptz, text) IS
'Rows of any case-fact table as they stood at a past moment. basis '
'"valid" = true in the world then; basis "asserted" = what documents '
'said by then. These differ and both are legitimate questions.';

-- ── Case activity: documents and events on one timeline ─────────────
-- The process itself. This is what makes a case a continuation rather
-- than a row.
CREATE OR REPLACE VIEW case_activity AS
SELECT cd.case_id,
       'document'                                   AS activity_kind,
       cd.doc_id                                    AS activity_ref,
       coalesce(cd.doc_role_key, cd.doc_role_raw)   AS activity_label,
       coalesce(cd.document_date, cd.received_at)   AS occurred_at,
       cd.sequence_no,
       cd.span_id
FROM case_documents cd
UNION ALL
SELECT e.case_id,
       'event',
       e.event_id::text,
       coalesce(e.event_type_id, e.event_type_raw),
       coalesce(e.occurred_at, e.recorded_at),
       e.stage_order,
       e.span_id
FROM case_events e;

COMMENT ON VIEW case_activity IS
'Documents and events merged into one ordered stream per case. The case '
'AS a process rather than as a row.';

-- ── Derived phase, with no terminal state ───────────────────────────
-- Deliberately reports observations rather than a status word. There is
-- no 'closed': an appeal reopens a decided case, so any stored terminal
-- state is a future wrong answer. A caller wanting "open cases" must say
-- what it means by open — and the columns here let it.
CREATE OR REPLACE VIEW case_progress AS
SELECT c.case_id,
       c.registered_at,
       c.status_raw,
       (SELECT count(*) FROM case_documents d WHERE d.case_id = c.case_id) AS document_count,
       (SELECT count(*) FROM case_events   e WHERE e.case_id = c.case_id) AS event_count,
       a.last_activity_at,
       a.latest_activity_label,
       -- Elapsed time since anything happened. The honest substitute for
       -- a closed flag: a case with no activity for two years is dormant,
       -- which is an observation, not a state anyone recorded.
       CASE WHEN a.last_activity_at IS NOT NULL
            THEN (now()::date - a.last_activity_at::date) END AS days_since_activity,
       -- Coverage, so a count over this view can disclose what it knows.
       (a.last_activity_at IS NULL)                           AS no_activity_recorded
FROM rebuild_cases c
LEFT JOIN LATERAL (
    SELECT max(occurred_at) AS last_activity_at,
           (array_agg(activity_label ORDER BY occurred_at DESC NULLS LAST,
                                              sequence_no DESC NULLS LAST))[1]
               AS latest_activity_label
    FROM case_activity act WHERE act.case_id = c.case_id
) a ON true;

COMMENT ON VIEW case_progress IS
'Where a case stands, derived from its latest activity. Reports '
'observations (last activity, elapsed days) instead of a status word, '
'because there is no terminal state: an appeal reopens a decided case.';

-- ── Current facts: the default reading of any count ─────────────────
-- Counting "accused persons" must mean currently-asserted accused, not
-- every role ever recorded. One predicate, so the planner cannot forget
-- it by accident.
CREATE OR REPLACE VIEW case_parties_current AS
SELECT * FROM case_parties WHERE valid_to IS NULL;

CREATE OR REPLACE VIEW weapons_current AS
SELECT * FROM weapons WHERE valid_to IS NULL;

CREATE OR REPLACE VIEW seized_items_current AS
SELECT * FROM seized_items WHERE valid_to IS NULL;

CREATE OR REPLACE VIEW vehicles_current AS
SELECT * FROM vehicles WHERE valid_to IS NULL;

COMMENT ON VIEW case_parties_current IS
'Roles as currently asserted. A person recorded as a witness in the FIR '
'and as accused in the challan appears once here, as accused, while both '
'assertions remain in case_parties with their spans.';

-- ── Role history, because the change is itself evidence ─────────────
CREATE OR REPLACE VIEW party_role_history AS
SELECT p.case_id, p.person_id,
       coalesce(p.role_id, p.role_raw) AS role,
       p.valid_from, p.valid_to,
       p.asserted_by_doc_id, p.asserted_at, p.span_id,
       row_number() OVER (PARTITION BY p.case_id, p.person_id
                          ORDER BY p.valid_from NULLS FIRST,
                                   p.asserted_at NULLS FIRST,
                                   p.case_party_id) AS version_no,
       (p.valid_to IS NULL)                          AS is_current
FROM case_parties p;

COMMENT ON VIEW party_role_history IS
'Every role a person has held in a case, in order, with the document '
'that asserted each. A witness who becomes an accused is a finding, not '
'a correction to overwrite.';

COMMIT;
