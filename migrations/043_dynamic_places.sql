-- ============================================================
-- 043 — Geography as data, not as schema (supersedes 035)
--
-- WHAT WAS WRONG WITH 035
--
-- 035 built a FIXED LADDER, one table per level:
--
--     provinces -> cities -> police_ranges -> police_districts
--               -> police_divisions -> police_stations
--
-- and seeded it by hand: 5 provinces, 8 cities, 9 districts. That is the
-- authored-list mistake 039 removed from vocabularies, still sitting in
-- geography — and worse, because here the LIST OF LEVELS is welded into
-- the schema. Every one of these needs a migration:
--
--   * a country level above province (federal data, or a second country)
--   * a state/region between country and province
--   * tehsil, union council, sector, beat, picket
--   * a circle that reports to a range directly, skipping district
--   * any level a source document turns out to use that nobody predicted
--
-- A hierarchy whose DEPTH is fixed at design time cannot ingest a source
-- that disagrees about depth. It will either drop the level or lie about
-- which level a place belongs to. Both are silent.
--
-- THE FIX: ONE NODE TABLE, EDGES FOR CONTAINMENT
--
-- A place is a place. Its LEVEL is a value in a column, discovered from
-- data like every other value, and its parent is an edge. Depth is then
-- unlimited and costs nothing:
--
--     country > province > division > district > tehsil > city > ...
--
-- Adding a level is INSERTING A ROW. No migration, no downtime, no
-- schema opinion about how Pakistan is administratively divided — which
-- is not ours to hold, changes by notification, and differs between the
-- police and civil hierarchies anyway.
--
-- PARALLEL HIERARCHIES, NOT ONE TREE
--
-- 035 assumed a single tree and bolted the civil rollup on with
-- police_districts.city_id. But a station sits in a police division AND
-- in a city, and those are different hierarchies that do not nest. So
-- containment is an EDGE TABLE carrying which hierarchy it belongs to,
-- and a place may have a parent in each. 'کراچی' rolls up across
-- 'کراچی ایسٹ' and 'کراچی وسطی' by walking the civil hierarchy, without
-- either hierarchy being privileged.
--
-- NOTHING IS SEEDED
--
-- 035 seeded 9 districts to match graph district_id values. This does
-- not. Places arrive from extraction with a span, like every other fact.
-- A seeded row has no provenance and cannot be checked against a source,
-- which makes it indistinguishable from a guess.
--
-- NO HARDCODED LEVEL NAMES
--
-- kind_raw holds the level exactly as the source expressed it. What
-- levels exist is answered live by discover_values('places','kind_raw'),
-- never from a list. An unseen level lands raw and is never rejected.
-- ============================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── Orthographic normalisation ──────────────────────────────────────
-- This is NOT a vocabulary. It encodes properties of the writing system
-- (Arabic-script letters with multiple Unicode forms, diacritics that
-- carry no distinction in names, case folding) rather than anything
-- about this corpus. ک/ك and ی/ي/ى are the same letter typed
-- differently; treating them as different is a data-entry artefact, not
-- a meaningful distinction.
--
-- Shared by places and persons — persons.name_skeleton has had no
-- generator until now.
CREATE OR REPLACE FUNCTION normalize_name(p_text text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT nullif(
        btrim(
            regexp_replace(
                translate(
                    lower(unaccent(coalesce(p_text, ''))),
                    -- Arabic/Urdu presentation variants -> canonical form,
                    -- plus tatweel and the vowel marks, which are optional
                    -- in running text and inconsistently typed.
                    'كيىۍېﻻأإآةۀهٔ' || U&'\0640\064B\064C\064D\064E\064F\0650\0651\0652\0654',
                    'کییییلااات ه'  || '          '
                ),
                -- Collapse everything that is not a letter or digit in any
                -- script down to single spaces.
                '[^[:alnum:]؀-ۿ]+', ' ', 'g'
            ),
        ' '),
    '');
$fn$;

COMMENT ON FUNCTION normalize_name(text) IS
'Orthographic skeleton for matching: case-folded, diacritic-stripped, '
'Arabic-script variants unified, punctuation collapsed. Encodes the '
'writing system, not this corpus — it is not a vocabulary.';

-- ── Retire the fixed ladder ─────────────────────────────────────────
-- Dependent columns are dropped with it. Safe to do outright: every one
-- of these tables is empty except for 035's hand-written seeds, and no
-- extraction has run yet. Doing it after load would be a data migration.
ALTER TABLE addresses            DROP COLUMN IF EXISTS city_id;
ALTER TABLE addresses            DROP COLUMN IF EXISTS province_id;
ALTER TABLE rebuild_cases        DROP COLUMN IF EXISTS station_id;
ALTER TABLE officers             DROP COLUMN IF EXISTS station_id;
ALTER TABLE case_station_history DROP COLUMN IF EXISTS station_id;

DROP TABLE IF EXISTS police_stations  CASCADE;
DROP TABLE IF EXISTS police_divisions CASCADE;
DROP TABLE IF EXISTS police_districts CASCADE;
DROP TABLE IF EXISTS police_ranges    CASCADE;
DROP TABLE IF EXISTS cities           CASCADE;
DROP TABLE IF EXISTS provinces        CASCADE;

-- ── Places: every level, one table ──────────────────────────────────
CREATE TABLE places (
    place_id      bigserial PRIMARY KEY,
    -- The level, as the SOURCE expressed it: 'province', 'ضلع', 'city',
    -- 'police_station', 'tehsil', 'country'. Never validated against a
    -- list. discover_values('places','kind_raw') answers what exists.
    kind_raw      text NOT NULL,
    -- Resolved level key, filled later by a re-runnable resolver.
    -- Nullable forever: an unresolved place is still a usable place.
    kind_key      text,
    name_raw      text NOT NULL,
    name_skeleton text GENERATED ALWAYS AS (normalize_name(name_raw)) STORED,
    -- Open world. A station has a code, a city has a population, a beat
    -- has a patrol shift. Rather than a column per level-specific
    -- attribute — which reintroduces the fixed ladder through the back
    -- door — anything level-specific lands here.
    attributes    jsonb NOT NULL DEFAULT '{}',
    span_id       bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL,
    confidence    numeric,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_places_kind     ON places (kind_raw);
CREATE INDEX idx_places_skeleton ON places (name_skeleton);
CREATE INDEX idx_places_trgm     ON places USING gin (name_skeleton gin_trgm_ops);
CREATE INDEX idx_places_attrs    ON places USING gin (attributes);

COMMENT ON TABLE places IS
'Every geographic and organisational unit at any level, in one table. '
'The level is data (kind_raw), not a table name, so depth is unlimited '
'and a new level is an INSERT rather than a migration.';

-- ── Alternate names, each a fact with provenance ────────────────────
-- Not a synonyms[] column authored by hand (035 had one). Every name is
-- observed in a document and carries the span it came from, so it can be
-- checked. Urdu, English, Roman, and misspellings all land here.
CREATE TABLE place_names (
    place_name_id bigserial PRIMARY KEY,
    place_id      bigint NOT NULL REFERENCES places (place_id) ON DELETE CASCADE,
    name_raw      text NOT NULL,
    name_skeleton text GENERATED ALWAYS AS (normalize_name(name_raw)) STORED,
    script        text,
    span_id       bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL,
    confidence    numeric,
    UNIQUE (place_id, name_raw)
);

CREATE INDEX idx_place_names_skeleton ON place_names (name_skeleton);
CREATE INDEX idx_place_names_trgm     ON place_names USING gin (name_skeleton gin_trgm_ops);

COMMENT ON TABLE place_names IS
'Observed names for a place, one row per spelling or script, each with '
'the span it was read from. Replaces 035''s authored synonyms array.';

-- ── Containment: edges, in named hierarchies ────────────────────────
-- A place may sit in several hierarchies at once. A station has a police
-- parent (division) and a civil parent (city); neither is privileged and
-- neither has to exist.
CREATE TABLE place_hierarchy (
    child_id      bigint NOT NULL REFERENCES places (place_id) ON DELETE CASCADE,
    parent_id     bigint NOT NULL REFERENCES places (place_id) ON DELETE CASCADE,
    -- Which hierarchy this edge belongs to: 'police', 'civil', 'postal',
    -- whatever a source implies. Also discovered, never validated.
    hierarchy_raw text NOT NULL,
    span_id       bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL,
    confidence    numeric,
    PRIMARY KEY (child_id, parent_id, hierarchy_raw),
    CHECK (child_id <> parent_id)
);

CREATE INDEX idx_ph_parent ON place_hierarchy (parent_id, hierarchy_raw);
CREATE INDEX idx_ph_child  ON place_hierarchy (child_id, hierarchy_raw);

COMMENT ON TABLE place_hierarchy IS
'Containment as edges rather than as table structure. Supports parallel '
'hierarchies (police and civil) and unlimited depth. Adding a country '
'above every province is one INSERT per province.';

-- ── Fact tables point at a place, at whatever level is known ────────
-- No column per level. A case whose source names only a city, and a case
-- whose source names a station, both write one place_id — at the level
-- the document actually supported. Rolling up is a walk, not a join.
ALTER TABLE rebuild_cases
    ADD COLUMN station_place_id bigint REFERENCES places (place_id);
ALTER TABLE officers
    ADD COLUMN station_place_id bigint REFERENCES places (place_id);
ALTER TABLE case_station_history
    ADD COLUMN station_place_id bigint REFERENCES places (place_id);
ALTER TABLE addresses
    ADD COLUMN place_id bigint REFERENCES places (place_id);

-- case_station_history keeps its existing key (case_id, from_ts), which
-- survives the station_id drop and is the right key anyway: a case is at
-- one station from any given moment.

CREATE INDEX idx_rcases_place  ON rebuild_cases       (station_place_id);
CREATE INDEX idx_officers_place ON officers           (station_place_id);
CREATE INDEX idx_csh_place     ON case_station_history (station_place_id);
CREATE INDEX idx_addr_place    ON addresses           (place_id);

-- ── Walking up: every ancestor at any depth ─────────────────────────
-- p_hierarchy NULL walks ALL hierarchies at once, which is what a
-- question like "is this station in Karachi" needs — it does not know or
-- care whether the link is police or civil.
CREATE OR REPLACE FUNCTION place_ancestors(
    p_place_id  bigint,
    p_hierarchy text DEFAULT NULL,
    p_max_depth integer DEFAULT 12
) RETURNS TABLE (
    place_id bigint, kind_raw text, name_raw text,
    depth integer, hierarchy_raw text
) LANGUAGE sql STABLE AS $fn$
    WITH RECURSIVE up AS (
        SELECT h.parent_id, 1 AS depth, h.hierarchy_raw,
               ARRAY[h.child_id, h.parent_id] AS seen
        FROM place_hierarchy h
        WHERE h.child_id = p_place_id
          AND (p_hierarchy IS NULL OR h.hierarchy_raw = p_hierarchy)
        UNION ALL
        SELECT h.parent_id, u.depth + 1, h.hierarchy_raw,
               u.seen || h.parent_id
        FROM up u
        JOIN place_hierarchy h ON h.child_id = u.parent_id
        WHERE u.depth < p_max_depth
          AND (p_hierarchy IS NULL OR h.hierarchy_raw = p_hierarchy)
          -- Cycle guard. Administrative data really does contain loops.
          AND NOT (h.parent_id = ANY(u.seen))
    )
    SELECT DISTINCT ON (u.parent_id, u.hierarchy_raw)
           p.place_id, p.kind_raw, p.name_raw, u.depth, u.hierarchy_raw
    FROM up u JOIN places p ON p.place_id = u.parent_id
    ORDER BY u.parent_id, u.hierarchy_raw, u.depth;
$fn$;

COMMENT ON FUNCTION place_ancestors(bigint, text, integer) IS
'Every containing place at any depth, optionally within one hierarchy. '
'Cycle-guarded. Replaces the fixed province/city/district join chain.';

-- ── Walking down: the rollup set ────────────────────────────────────
-- "Cases in Karachi" = cases whose place is any descendant of Karachi,
-- however many levels down and whatever those levels are called. This is
-- the query 035 could only answer by unioning two hand-known districts.
CREATE OR REPLACE FUNCTION place_descendants(
    p_place_id  bigint,
    p_hierarchy text DEFAULT NULL,
    p_max_depth integer DEFAULT 12
) RETURNS TABLE (
    place_id bigint, kind_raw text, name_raw text,
    depth integer, hierarchy_raw text
) LANGUAGE sql STABLE AS $fn$
    WITH RECURSIVE down AS (
        SELECT h.child_id, 1 AS depth, h.hierarchy_raw,
               ARRAY[h.parent_id, h.child_id] AS seen
        FROM place_hierarchy h
        WHERE h.parent_id = p_place_id
          AND (p_hierarchy IS NULL OR h.hierarchy_raw = p_hierarchy)
        UNION ALL
        SELECT h.child_id, d.depth + 1, h.hierarchy_raw,
               d.seen || h.child_id
        FROM down d
        JOIN place_hierarchy h ON h.parent_id = d.child_id
        WHERE d.depth < p_max_depth
          AND (p_hierarchy IS NULL OR h.hierarchy_raw = p_hierarchy)
          AND NOT (h.child_id = ANY(d.seen))
    )
    SELECT DISTINCT ON (d.child_id, d.hierarchy_raw)
           p.place_id, p.kind_raw, p.name_raw, d.depth, d.hierarchy_raw
    FROM down d JOIN places p ON p.place_id = d.child_id
    ORDER BY d.child_id, d.hierarchy_raw, d.depth;
$fn$;

COMMENT ON FUNCTION place_descendants(bigint, text, integer) IS
'Every contained place at any depth. The rollup primitive: a count "in '
'Karachi" is a count over this set, with no knowledge of what levels '
'sit between.';

-- ── Name resolution, by observation not by list ─────────────────────
-- Returns CANDIDATES with a score and never picks one, for the same
-- reason find_join_paths returns candidates: a silent wrong match is a
-- confident wrong number. Exact skeleton match scores 1.0; anything else
-- is trigram similarity and is meant to be reviewed.
CREATE OR REPLACE FUNCTION resolve_place(
    p_name      text,
    p_kind      text DEFAULT NULL,
    p_min_score numeric DEFAULT 0.35,
    p_limit     integer DEFAULT 10
) RETURNS TABLE (
    place_id bigint, kind_raw text, name_raw text,
    matched_name text, score numeric, exact boolean
) LANGUAGE sql STABLE AS $fn$
    WITH target AS (SELECT normalize_name(p_name) AS skel),
    candidates AS (
        SELECT p.place_id, p.kind_raw, p.name_raw,
               p.name_raw AS matched_name, p.name_skeleton AS skel
        FROM places p
        WHERE p_kind IS NULL OR p.kind_raw = p_kind
        UNION ALL
        SELECT p.place_id, p.kind_raw, p.name_raw,
               n.name_raw, n.name_skeleton
        FROM place_names n JOIN places p ON p.place_id = n.place_id
        WHERE p_kind IS NULL OR p.kind_raw = p_kind
    )
    SELECT DISTINCT ON (c.place_id)
           c.place_id, c.kind_raw, c.name_raw, c.matched_name,
           CASE WHEN c.skel = t.skel THEN 1.0
                ELSE round(similarity(c.skel, t.skel)::numeric, 3) END,
           c.skel = t.skel
    FROM candidates c CROSS JOIN target t
    WHERE c.skel = t.skel OR similarity(c.skel, t.skel) >= p_min_score
    ORDER BY c.place_id, 5 DESC
    LIMIT p_limit;
$fn$;

COMMENT ON FUNCTION resolve_place(text, text, numeric, integer) IS
'Candidate places matching a name across every observed spelling and '
'script. Returns ranked candidates, never a single silent choice.';

-- ── Full path, for display and for citation ─────────────────────────
CREATE OR REPLACE VIEW place_full_path AS
SELECT p.place_id, p.kind_raw, p.name_raw,
       (SELECT string_agg(a.name_raw, ' > ' ORDER BY a.depth DESC)
          FROM place_ancestors(p.place_id) a)          AS ancestry,
       (SELECT count(*) FROM place_hierarchy h
         WHERE h.child_id = p.place_id)                AS parent_count
FROM places p;

COMMENT ON VIEW place_full_path IS
'Human-readable ancestry per place. parent_count > 1 means the place '
'sits in more than one hierarchy, which is expected, not an error.';

COMMIT;
