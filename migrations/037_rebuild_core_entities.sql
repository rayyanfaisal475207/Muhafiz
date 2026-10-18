-- ============================================================
-- 037 — Cases, persons, parties, evidence, statute, events
--        (rebuild stage 4 schema; the load itself comes later)
--
-- WHY THIS EXISTS
--
-- This is the table that makes counting a query instead of a traversal:
--
--   SELECT count(DISTINCT person_id) FROM case_parties WHERE role_id='WITNESS';
--
-- Today the same answer requires a graph traversal whose natural form
-- double-counts (73 PART_OF edges for 19 stations), or a semantic search
-- over ~10 chunks per case. Neither is reliable, and neither is cheap.
--
-- DESIGN NOTES WORTH KEEPING
--
--   * fir_serial / fir_year are split out. '201/26' as text cannot be
--     sorted, ranged, or grouped by year.
--   * age_at_incident lives on case_parties, NOT on persons. A person's
--     stated age differs per case; putting it on the person is precisely
--     how age filters go wrong.
--   * Reporting delay becomes (reported_at - incident_at), a column
--     subtraction rather than a derived graph property.
--   * Every fact row carries span_id, so a SQL-computed number can cite
--     the sentence it came from.
--
-- Additive only, rebuild_* prefix where a name would collide with the
-- live schema. Nothing existing is dropped or altered.
-- ============================================================

BEGIN;

-- ── Addresses ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS addresses (
    address_id  bigserial PRIMARY KEY,
    raw_text_ur text NOT NULL,             -- 'موضع برکی، لاہور' — as written
    locality_ur text,
    city_id     text REFERENCES cities,
    province_id text REFERENCES provinces,
    span_id     bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_addresses_city ON addresses (city_id);

-- ── Cases ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rebuild_cases (
    case_id       text PRIMARY KEY,        -- 'fir-201-26'
    fir_number    text NOT NULL,           -- '201/26' as written
    fir_serial    integer,                 -- 201  — sortable
    fir_year      integer,                 -- 2026 — filterable
    station_id    text REFERENCES police_stations,   -- current station (A3)
    category_id   text REFERENCES crime_categories,
    status_id     text REFERENCES case_statuses,
    incident_at   timestamptz,             -- when it happened
    reported_at   timestamptz,             -- when it was reported
    registered_at timestamptz,             -- when the FIR was cut
    location_text text,
    address_id    bigint REFERENCES addresses,
    description   text,
    source_doc_id text REFERENCES rebuild_documents (doc_id),
    span_id       bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rcases_station  ON rebuild_cases (station_id);
CREATE INDEX IF NOT EXISTS idx_rcases_year     ON rebuild_cases (fir_year);
CREATE INDEX IF NOT EXISTS idx_rcases_category ON rebuild_cases (category_id);
CREATE INDEX IF NOT EXISTS idx_rcases_status   ON rebuild_cases (status_id);
CREATE INDEX IF NOT EXISTS idx_rcases_incident ON rebuild_cases (incident_at);

-- A3: a case can move between stations; a column cannot represent that.
CREATE TABLE IF NOT EXISTS case_station_history (
    case_id    text NOT NULL REFERENCES rebuild_cases (case_id) ON DELETE CASCADE,
    station_id text NOT NULL REFERENCES police_stations,
    from_ts    timestamptz NOT NULL,
    to_ts      timestamptz,
    reason     text,
    span_id    bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL,
    PRIMARY KEY (case_id, from_ts)
);

-- ── Persons ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS persons (
    person_id         text PRIMARY KEY,    -- 'PERSON-1b360367f6' (matches graph ids)
    canonical_name_ur text NOT NULL,
    canonical_name_en text,
    -- Consonant skeleton: the cross-script match key. The same human
    -- appears as 'چوہدری بشیر احمد' and a Roman transliteration across
    -- documents; this is what makes them joinable.
    name_skeleton     text,
    father_name_ur    text,
    cnic              text,                -- strongest identity key when present
    gender_id         text REFERENCES genders,
    date_of_birth     date,
    address_id        bigint REFERENCES addresses,
    span_id           bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- CNIC is unique only where present; most records do not carry one.
CREATE UNIQUE INDEX IF NOT EXISTS uq_persons_cnic
    ON persons (cnic) WHERE cnic IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_persons_skeleton ON persons (name_skeleton);
CREATE INDEX IF NOT EXISTS idx_persons_gender   ON persons (gender_id);

-- Identity resolution as data, not as graph edges. The graph projection
-- reads this table; it is no longer the source of truth.
CREATE TABLE IF NOT EXISTS person_identity_links (
    person_id_a text NOT NULL REFERENCES persons (person_id) ON DELETE CASCADE,
    person_id_b text NOT NULL REFERENCES persons (person_id) ON DELETE CASCADE,
    confidence  numeric NOT NULL,
    method      text NOT NULL,             -- 'cnic'|'skeleton'|'phone'|'manual'
    status      text NOT NULL DEFAULT 'proposed',  -- proposed|confirmed|rejected
    reviewed_by uuid,
    reviewed_at timestamptz,
    PRIMARY KEY (person_id_a, person_id_b),
    CHECK (person_id_a < person_id_b)      -- one row per pair, not two
);

CREATE INDEX IF NOT EXISTS idx_pil_status ON person_identity_links (status);

-- ── Case parties — the keystone table ───────────────────────────────
CREATE TABLE IF NOT EXISTS case_parties (
    case_party_id     bigserial PRIMARY KEY,
    case_id           text NOT NULL REFERENCES rebuild_cases (case_id) ON DELETE CASCADE,
    person_id         text NOT NULL REFERENCES persons (person_id),
    role_id           text NOT NULL REFERENCES party_roles,
    is_primary        boolean NOT NULL DEFAULT false,
    age_at_incident   integer,             -- per-case, not per-person
    stated_occupation text,
    span_id           bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL,
    UNIQUE (case_id, person_id, role_id)   -- one person, one role, one case
);

CREATE INDEX IF NOT EXISTS idx_cparties_case   ON case_parties (case_id);
CREATE INDEX IF NOT EXISTS idx_cparties_person ON case_parties (person_id);
CREATE INDEX IF NOT EXISTS idx_cparties_role   ON case_parties (role_id);
CREATE INDEX IF NOT EXISTS idx_cparties_age    ON case_parties (age_at_incident);

-- ── Evidence ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS weapons (
    weapon_id        bigserial PRIMARY KEY,
    case_id          text NOT NULL REFERENCES rebuild_cases (case_id) ON DELETE CASCADE,
    weapon_type_id   text REFERENCES weapon_types,
    caliber_or_bore  text,                 -- '30 بور'
    license_state_id text REFERENCES license_states,
    quantity         integer,
    ammunition_count integer,
    recovered_at     timestamptz,
    span_id          bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_weapons_case    ON weapons (case_id);
CREATE INDEX IF NOT EXISTS idx_weapons_license ON weapons (license_state_id);
CREATE INDEX IF NOT EXISTS idx_weapons_caliber ON weapons (caliber_or_bore);

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id      bigserial PRIMARY KEY,
    case_id         text NOT NULL REFERENCES rebuild_cases (case_id) ON DELETE CASCADE,
    make            text,
    model           text,
    registration_no text,
    colour          text,
    span_id         bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_vehicles_case ON vehicles (case_id);
CREATE INDEX IF NOT EXISTS idx_vehicles_reg  ON vehicles (registration_no);

CREATE TABLE IF NOT EXISTS phone_numbers (
    phone_id  bigserial PRIMARY KEY,
    msisdn    text NOT NULL,
    person_id text REFERENCES persons (person_id),
    case_id   text REFERENCES rebuild_cases (case_id) ON DELETE CASCADE,
    span_id   bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_phones_msisdn ON phone_numbers (msisdn);
CREATE INDEX IF NOT EXISTS idx_phones_case   ON phone_numbers (case_id);

CREATE TABLE IF NOT EXISTS seized_items (
    item_id        bigserial PRIMARY KEY,
    case_id        text NOT NULL REFERENCES rebuild_cases (case_id) ON DELETE CASCADE,
    description_ur text,
    category       text,
    quantity       integer,
    seized_at      timestamptz,
    span_id        bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_seized_case ON seized_items (case_id);

-- ── Statute ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS acts (
    act_id     text PRIMARY KEY,           -- 'PPC','PECA','CRPC'
    name_ur    text,
    name_en    text NOT NULL,
    short_code text,
    year       integer,
    synonyms   text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS statute_sections (
    section_id      text PRIMARY KEY,      -- 'PPC-379'
    act_id          text NOT NULL REFERENCES acts,
    section_no      text NOT NULL,         -- '379'
    title_en        text,
    title_ur        text,
    is_cognizable   boolean,
    is_bailable     boolean,
    punishment_text text,
    UNIQUE (act_id, section_no)
);

CREATE TABLE IF NOT EXISTS case_statutes (
    case_id    text NOT NULL REFERENCES rebuild_cases (case_id) ON DELETE CASCADE,
    section_id text NOT NULL REFERENCES statute_sections,
    stage      text NOT NULL DEFAULT 'fir',  -- fir|challan|court
    applied_at timestamptz,
    span_id    bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL,
    PRIMARY KEY (case_id, section_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_cstatutes_section ON case_statutes (section_id);

-- ── Officers and the timeline ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS officers (
    officer_id text PRIMARY KEY,
    name_ur    text NOT NULL,
    name_en    text,
    belt_no    text,                       -- 'GEN-0101'
    rank_id    text REFERENCES ranks,
    station_id text REFERENCES police_stations,
    phone      text,
    span_id    bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_officers_station ON officers (station_id);
CREATE INDEX IF NOT EXISTS idx_officers_belt    ON officers (belt_no);

CREATE TABLE IF NOT EXISTS case_officers (
    case_id    text NOT NULL REFERENCES rebuild_cases (case_id) ON DELETE CASCADE,
    officer_id text NOT NULL REFERENCES officers (officer_id),
    role       text NOT NULL,              -- 'IO'|'SHO'|'RECORDER'
    from_ts    timestamptz,
    to_ts      timestamptz,
    PRIMARY KEY (case_id, officer_id, role)
);

CREATE TABLE IF NOT EXISTS case_events (
    event_id       bigserial PRIMARY KEY,
    case_id        text NOT NULL REFERENCES rebuild_cases (case_id) ON DELETE CASCADE,
    event_type_id  text NOT NULL REFERENCES event_types,
    occurred_at    timestamptz,
    recorded_at    timestamptz,
    officer_id     text REFERENCES officers (officer_id),
    description_ur text,
    sequence_no    integer,                -- zimni entry number
    span_id        bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_cevents_case ON case_events (case_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_cevents_type ON case_events (event_type_id);

COMMIT;
