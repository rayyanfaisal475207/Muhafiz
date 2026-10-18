-- ============================================================
-- 040 — Per-person event pathways (resolves open question A4)
--
-- THE PROBLEM A4 RAISED
--
-- `cases.investigation_status` holds sentences, not statuses:
--
--   'ملزم گرفتار، بعد ازاں ضمانت پر رہا، سماعت جاری'
--     (accused arrested, later released on bail, hearing ongoing)
--
-- That is three events in one string, and it describes what happened to
-- a PERSON, not to the case. Two accused in the same case routinely
-- follow different paths: one is arrested then bailed, another is never
-- arrested and goes straight to hearing. A case-level status column
-- cannot represent that, and neither can a per-case enum.
--
-- WHAT THIS ENABLES
--
--   "how many individuals were arrested, then bailed, and are now
--    under prosecution"
--
-- That is a question about an ORDERED SEQUENCE of events belonging to
-- one person in one case. It needs events attached to the person and
-- ordered, not a status string parsed at query time.
--
-- NO HARDCODED EVENT TYPES
--
-- `event_type_raw` stores the phrase exactly as written, always. The
-- resolved `event_type_key` is nullable and filled by the extractor
-- later. An unseen kind of event lands raw and is never rejected.
-- Discovery of what event types exist is done live by
-- discover_values('case_events','event_type_raw'), never from a list.
-- ============================================================

BEGIN;

-- ── Events belong to a person, when they are about a person ─────────
-- Nullable: registration and filing are case-level, arrest and bail are
-- person-level. One table, one ordering, both kinds.
ALTER TABLE case_events ADD COLUMN IF NOT EXISTS person_id text
    REFERENCES persons (person_id) ON DELETE SET NULL;

-- Where this event was asserted. A status sentence yields several
-- events from one span; keeping the span lets every derived event be
-- traced to the exact words behind it.
ALTER TABLE case_events ADD COLUMN IF NOT EXISTS derived_from text;
    -- 'status_string' | 'zimni' | 'chalaan' | 'explicit'

-- Ordering within a person's journey. `occurred_at` is frequently NULL
-- in this corpus (real FIRs carry 'تاریخ و وقت: -'), so a stated order
-- must survive missing dates.
ALTER TABLE case_events ADD COLUMN IF NOT EXISTS stage_order integer;

-- Confidence, because events derived from a sentence are inferred, not
-- recorded. A pathway query must be able to exclude weak inferences.
ALTER TABLE case_events ADD COLUMN IF NOT EXISTS confidence numeric;

CREATE INDEX IF NOT EXISTS idx_cevents_person
    ON case_events (person_id, case_id, stage_order);
CREATE INDEX IF NOT EXISTS idx_cevents_raw
    ON case_events (event_type_raw);

-- ── The timeline: one ordered row per person-event ──────────────────
-- Ordering rule, in priority order: explicit stage_order, then
-- occurred_at, then recorded_at, then insertion order. NULLS LAST so a
-- dated event always precedes an undated one rather than sorting first.
CREATE OR REPLACE VIEW person_case_timeline AS
SELECT
    e.case_id,
    e.person_id,
    e.event_id,
    coalesce(e.event_type_id, e.event_type_raw) AS event_type,
    e.event_type_raw,
    e.occurred_at,
    e.confidence,
    e.span_id,
    row_number() OVER (
        PARTITION BY e.case_id, e.person_id
        ORDER BY e.stage_order NULLS LAST,
                 e.occurred_at NULLS LAST,
                 e.recorded_at NULLS LAST,
                 e.event_id
    ) AS step_no
FROM case_events e
WHERE e.person_id IS NOT NULL;

COMMENT ON VIEW person_case_timeline IS
'One ordered row per person-event. step_no is the position in that '
'person''s journey through this case, tolerant of missing dates.';

-- ── Pathway: the ordered sequence, as an array per person ───────────
-- A pathway query then becomes array containment rather than string
-- matching over a status sentence:
--
--   -- arrested, then bailed, then still in prosecution
--   SELECT count(*) FROM person_case_pathway
--   WHERE pathway @> ARRAY['ARREST','BAIL']
--     AND current_stage = 'COURT';
--
--   -- never arrested, straight to hearing
--   SELECT count(*) FROM person_case_pathway
--   WHERE NOT (pathway @> ARRAY['ARREST'])
--     AND pathway @> ARRAY['COURT'];
CREATE OR REPLACE VIEW person_case_pathway AS
SELECT
    case_id,
    person_id,
    array_agg(event_type ORDER BY step_no)  AS pathway,
    (array_agg(event_type ORDER BY step_no))[array_length(array_agg(event_type), 1)]
                                            AS current_stage,
    count(*)                                AS step_count,
    min(occurred_at)                        AS first_event_at,
    max(occurred_at)                        AS last_event_at,
    min(confidence)                         AS weakest_confidence
FROM person_case_timeline
GROUP BY case_id, person_id;

COMMENT ON VIEW person_case_pathway IS
'Ordered event sequence per person per case, as an array. Supports '
'"arrested then bailed then prosecuted" as array containment plus a '
'current-stage test, instead of parsing a status sentence.';

-- ── Case-level derived status ───────────────────────────────────────
-- Replaces the free-text status column as the thing queries filter on,
-- WITHOUT deleting it: `rebuild_cases.status_raw` keeps the original
-- sentence for provenance and display.
CREATE OR REPLACE VIEW case_derived_status AS
SELECT
    c.case_id,
    c.status_raw,
    (SELECT coalesce(e.event_type_id, e.event_type_raw)
       FROM case_events e
      WHERE e.case_id = c.case_id
      ORDER BY e.stage_order NULLS LAST,
               e.occurred_at NULLS LAST,
               e.event_id DESC
      LIMIT 1)                                   AS latest_event,
    (SELECT count(DISTINCT e.person_id) FROM case_events e
      WHERE e.case_id = c.case_id AND e.person_id IS NOT NULL) AS persons_with_events,
    (SELECT count(*) FROM case_events e WHERE e.case_id = c.case_id) AS event_count
FROM rebuild_cases c;

COMMENT ON VIEW case_derived_status IS
'Case status derived from its latest event rather than from a parsed '
'sentence. status_raw is retained for provenance and display.';

COMMIT;
