-- ============================================================
-- 038 — Dynamic, discovered vocabularies (supersedes the authored
--        seed lists in 034)
--
-- WHAT WAS WRONG WITH 034
--
-- 034 seeded closed lists I wrote by hand: 5 party roles, 5 station
-- types, 11 crime categories. That is wrong for this system. New
-- values arrive constantly — a new station type, an unseen role word,
-- a category nobody anticipated — and a closed list either rejects
-- them (FK violation on ingest) or silently drops them. An authored
-- vocabulary is stale the moment the next document arrives.
--
-- WHAT DOES NOT WORK EITHER
--
-- "Let the LLM read the Urdu" is what the system already does, and it
-- is the measured failure this whole rebuild started from: the planner
-- filtered license_status = "unlicensed" against data holding
-- "بغیر لائسنس" and returned 0 where the truth was 30. The model was
-- multilingual and still wrong, because it was GUESSING what the
-- stored value might be rather than choosing from what exists.
--
-- THE RESOLUTION: DISCOVER, DON'T AUTHOR
--
-- Vocabularies exist, but nothing hand-writes them:
--
--   observed_values   every distinct value actually present in the
--                     corpus, harvested by measurement, with frequency
--                     and first/last seen. Refreshed on every ingest.
--   value_aliases     surface forms that map to an observed value —
--                     produced by the LLM or confirmed by a user, with
--                     provenance and confidence. Learned, never authored.
--   value_clusters    optional grouping of observed values that mean
--                     the same thing ("بغیر لائسنس" / "ناجائز"),
--                     proposed by the model, confirmable by a human.
--
-- OPEN WORLD BY CONSTRUCTION
--
-- Fact tables store the RAW value as written, always, and carry a
-- NULLABLE resolved key. An unseen value can never block ingestion —
-- it lands raw, appears in observed_values on the next harvest, and
-- gets resolved later. Resolution is a re-runnable layer, not a gate.
--
-- The planner then resolves a user's phrasing against values that
-- actually exist, instead of inventing one. That is what closes the
-- zero-result failure without freezing the vocabulary.
-- ============================================================

BEGIN;

-- ── Harvested reality: what values actually occur ───────────────────
CREATE TABLE IF NOT EXISTS observed_values (
    observed_id   bigserial PRIMARY KEY,
    table_name    text NOT NULL,
    column_name   text NOT NULL,
    value_raw     text NOT NULL,          -- exactly as stored, any script
    script        text,                   -- 'arabic'|'latin'|'digit'|'mixed' (detected)
    frequency     bigint NOT NULL DEFAULT 0,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    -- Points at the cluster this value belongs to, once one is proposed.
    cluster_id    bigint,
    UNIQUE (table_name, column_name, value_raw)
);

CREATE INDEX IF NOT EXISTS idx_obsval_col     ON observed_values (table_name, column_name);
CREATE INDEX IF NOT EXISTS idx_obsval_cluster ON observed_values (cluster_id);
CREATE INDEX IF NOT EXISTS idx_obsval_freq    ON observed_values (frequency DESC);

-- ── Clusters: values that mean the same thing ───────────────────────
-- Proposed by the model over observed values, never hand-authored.
-- `status` carries the human decision; nothing is silently trusted.
CREATE TABLE IF NOT EXISTS value_clusters (
    cluster_id   bigserial PRIMARY KEY,
    table_name   text NOT NULL,
    column_name  text NOT NULL,
    -- The most frequent member, chosen by measurement, used as the label.
    canonical_value text,
    label_en     text,                     -- model-produced gloss, for prompts
    method       text NOT NULL,            -- 'embedding'|'llm'|'manual'
    confidence   numeric,
    status       text NOT NULL DEFAULT 'proposed',  -- proposed|confirmed|rejected
    reviewed_by  uuid,
    reviewed_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vclust_col    ON value_clusters (table_name, column_name);
CREATE INDEX IF NOT EXISTS idx_vclust_status ON value_clusters (status);

ALTER TABLE observed_values
    DROP CONSTRAINT IF EXISTS fk_obsval_cluster;
ALTER TABLE observed_values
    ADD CONSTRAINT fk_obsval_cluster
    FOREIGN KEY (cluster_id) REFERENCES value_clusters (cluster_id) ON DELETE SET NULL;

-- ── Aliases: how people ASK for a value ─────────────────────────────
-- "unlicensed", "ناجائز", "no licence", "bagair licence" all reaching
-- the same observed value. Rows are produced by the resolver at query
-- time and by user confirmation — never written by hand. A hit here is
-- what stops the next English guess returning zero.
CREATE TABLE IF NOT EXISTS value_aliases (
    alias_id    bigserial PRIMARY KEY,
    observed_id bigint REFERENCES observed_values (observed_id) ON DELETE CASCADE,
    cluster_id  bigint REFERENCES value_clusters (cluster_id) ON DELETE CASCADE,
    alias       text NOT NULL,
    source      text NOT NULL,             -- 'llm'|'user'|'harvested'|'transliteration'
    confidence  numeric,
    hit_count   bigint NOT NULL DEFAULT 0, -- aliases that work get reinforced
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (observed_id IS NOT NULL OR cluster_id IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_alias_target
    ON value_aliases (alias, coalesce(observed_id, -1), coalesce(cluster_id, -1));
CREATE INDEX IF NOT EXISTS idx_alias_lookup ON value_aliases (lower(alias));

-- ── Open-world fact columns ─────────────────────────────────────────
-- Fact tables keep the raw value ALWAYS, and a nullable resolved key.
-- An unseen value lands raw and is resolved later; ingestion never
-- fails because a vocabulary did not anticipate it.

ALTER TABLE weapons          ADD COLUMN IF NOT EXISTS license_state_raw text;
ALTER TABLE weapons          ADD COLUMN IF NOT EXISTS weapon_type_raw   text;
ALTER TABLE case_parties     ADD COLUMN IF NOT EXISTS role_raw          text;
ALTER TABLE rebuild_cases    ADD COLUMN IF NOT EXISTS category_raw      text;
ALTER TABLE rebuild_cases    ADD COLUMN IF NOT EXISTS status_raw        text;
ALTER TABLE police_stations  ADD COLUMN IF NOT EXISTS station_type_raw  text;
ALTER TABLE case_events      ADD COLUMN IF NOT EXISTS event_type_raw    text;
ALTER TABLE officers         ADD COLUMN IF NOT EXISTS rank_raw          text;

-- The resolved keys must not reject an unseen value, so the enum FKs
-- become advisory: nullable, and no longer NOT NULL anywhere.
ALTER TABLE case_parties    ALTER COLUMN role_id DROP NOT NULL;
ALTER TABLE case_events     ALTER COLUMN event_type_id DROP NOT NULL;
ALTER TABLE police_stations ALTER COLUMN station_type_id DROP NOT NULL;

-- ── Clear the authored seeds from 034 ───────────────────────────────
-- These tables stay as the RESOLVED-key dimension, but their contents
-- must come from discovery, not from my guesses. Emptying them now so
-- nothing downstream silently depends on a hand-written list.
-- (Rows are only re-created by the harvester + resolver.)
TRUNCATE party_roles, case_statuses, crime_categories, license_states,
         weapon_types, station_types, event_types, ranks, document_types,
         genders
    RESTART IDENTITY CASCADE;

-- ── Harvester: recompute observed_values from live data ─────────────
-- Measurement, not assertion. Called after every ingest. Adding a new
-- column to watch is one INSERT here, no schema change.
CREATE OR REPLACE FUNCTION harvest_observed_values(
    p_table text, p_column text
) RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    n integer;
BEGIN
    EXECUTE format($f$
        INSERT INTO observed_values (table_name, column_name, value_raw, script, frequency, last_seen_at)
        SELECT %L, %L, t.v,
               CASE WHEN t.v ~ '[؀-ۿ]' THEN 'arabic'
                    WHEN t.v ~ '^[0-9\s\-/]+$'   THEN 'digit'
                    WHEN t.v ~ '[A-Za-z]'        THEN 'latin'
                    ELSE 'mixed' END,
               t.n, now()
        FROM (
            SELECT %I::text AS v, count(*) AS n
            FROM %I
            WHERE %I IS NOT NULL AND btrim(%I::text) <> ''
            GROUP BY 1
        ) t
        ON CONFLICT (table_name, column_name, value_raw)
        DO UPDATE SET frequency = EXCLUDED.frequency,
                      last_seen_at = now()
    $f$, p_table, p_column, p_column, p_table, p_column, p_column);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

COMMENT ON FUNCTION harvest_observed_values(text, text) IS
'Recompute observed_values for one column by measurement. Idempotent; '
'safe to run after every ingest. Never asserts a value that is not in the data.';

COMMIT;
