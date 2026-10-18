-- ============================================================
-- 035 — Police and civil geography (rebuild stage 1b)
--
-- WHY THIS EXISTS
--
-- `cases.police_station` is free text with the city baked into the
-- string: 'تھانہ ماڈل ٹاؤن، لاہور'. All 73 rows are populated and there
-- are 19 distinct values, so counting STATIONS already works. What does
-- not work is anything that needs the geography inside that string:
--
--   "how many stations in Lahore"        -> substring matching
--   "how many cybercrime circles"        -> unanswerable
--   "cases in Karachi"                   -> must union کراچی ایسٹ +
--                                           کراچی وسطی, which are police
--                                           districts, not a city
--
-- The normalised model already exists — in the graph, as PoliceStation
-- (station_id 'PS-FSD-WOMEN') and District (district_id 'DIST-01'..09)
-- joined by PART_OF. It is in the wrong store. Worse, that graph holds
-- 73 PART_OF edges for 19 stations (one per case), so the natural query
--   MATCH (s:PoliceStation)-[:PART_OF]->(d:District) RETURN count(s)
-- answers 73 — the CASE count — where the truth is 19. Only an explicit
-- count(DISTINCT s.station_id) is correct.
--
-- With a primary key on station_id that class of double-count is
-- structurally impossible rather than a discipline problem.
--
-- ASSUMPTIONS (REBUILD_DESIGN.md §0; cheap to change now, expensive later)
--   A1  Full hierarchy province > range > district > division > station,
--       with range and division NULLABLE. Punjab and Karachi differ in
--       depth; a nullable chain models both without a second migration.
--   A2  Police AND civil geography are modelled separately and joined.
--       police_districts.city_id gives the civil rollup.
--
-- Additive only. `cases.police_station` is left untouched as provenance.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS provinces (
    province_id text PRIMARY KEY,          -- 'PB','SD','KP','BA','ICT','GB','AJK'
    name_ur     text NOT NULL,
    name_en     text NOT NULL,
    synonyms    text[] NOT NULL DEFAULT '{}'
);

-- Civil geography (A2). Separate from police districts on purpose:
-- 'کراچی' is a city; 'کراچی ایسٹ' and 'کراچی وسطی' are police districts
-- within it. A question about Karachi must roll up across both.
CREATE TABLE IF NOT EXISTS cities (
    city_id     text PRIMARY KEY,          -- 'CITY-KHI'
    name_ur     text NOT NULL,
    name_en     text NOT NULL,
    province_id text NOT NULL REFERENCES provinces,
    synonyms    text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS police_ranges (
    range_id    text PRIMARY KEY,
    name_ur     text NOT NULL,
    name_en     text NOT NULL,
    province_id text NOT NULL REFERENCES provinces
);

CREATE TABLE IF NOT EXISTS police_districts (
    district_id text PRIMARY KEY,          -- 'DIST-01'..'DIST-09' (as already in the graph)
    name_ur     text NOT NULL,
    name_en     text NOT NULL,
    range_id    text REFERENCES police_ranges,   -- nullable (A1)
    city_id     text REFERENCES cities,          -- civil rollup (A2)
    synonyms    text[] NOT NULL DEFAULT '{}'
);

-- Nullable level (A1): present in large metros, absent elsewhere.
CREATE TABLE IF NOT EXISTS police_divisions (
    division_id text PRIMARY KEY,
    name_ur     text NOT NULL,
    name_en     text NOT NULL,
    district_id text NOT NULL REFERENCES police_districts
);

CREATE TABLE IF NOT EXISTS police_stations (
    station_id        text PRIMARY KEY,    -- 'PS-FSD-WOMEN' (as already in the graph)
    name_ur           text NOT NULL,       -- 'خواتین تھانہ، فیصل آباد' — full, as written
    name_en           text,
    -- City stripped, so a station can be named without repeating its
    -- district in every answer: 'خواتین تھانہ'.
    short_name_ur     text,
    district_id       text NOT NULL REFERENCES police_districts,
    division_id       text REFERENCES police_divisions,
    station_type_id   text NOT NULL REFERENCES station_types,
    jurisdiction_note text
);

CREATE INDEX IF NOT EXISTS idx_police_stations_district ON police_stations (district_id);
CREATE INDEX IF NOT EXISTS idx_police_stations_type     ON police_stations (station_type_id);
CREATE INDEX IF NOT EXISTS idx_police_districts_city    ON police_districts (city_id);

-- ── Seed: provinces and cities observed in the live corpus ──────────
INSERT INTO provinces (province_id, name_ur, name_en, synonyms) VALUES
    ('PB',  'پنجاب',      'Punjab',      ARRAY['punjab']),
    ('SD',  'سندھ',       'Sindh',       ARRAY['sindh']),
    ('KP',  'خیبر پختونخوا','Khyber Pakhtunkhwa', ARRAY['KPK','NWFP']),
    ('BA',  'بلوچستان',   'Balochistan', ARRAY['balochistan']),
    ('ICT', 'اسلام آباد', 'Islamabad Capital Territory', ARRAY['Islamabad','ICT'])
ON CONFLICT (province_id) DO NOTHING;

INSERT INTO cities (city_id, name_ur, name_en, province_id, synonyms) VALUES
    ('CITY-LHR', 'لاہور',     'Lahore',      'PB',  ARRAY['lahore']),
    ('CITY-FSD', 'فیصل آباد', 'Faisalabad',  'PB',  ARRAY['faisalabad','lyallpur']),
    ('CITY-RWP', 'راولپنڈی',  'Rawalpindi',  'PB',  ARRAY['rawalpindi','pindi']),
    ('CITY-MUL', 'ملتان',     'Multan',      'PB',  ARRAY['multan']),
    ('CITY-CHW', 'چنیوٹ',     'Chiniot',     'PB',  ARRAY['chiniot']),
    ('CITY-KHI', 'کراچی',     'Karachi',     'SD',  ARRAY['karachi']),
    ('CITY-HYD', 'حیدر آباد', 'Hyderabad',   'SD',  ARRAY['hyderabad']),
    ('CITY-ISB', 'اسلام آباد','Islamabad',   'ICT', ARRAY['islamabad'])
ON CONFLICT (city_id) DO NOTHING;

-- ── Seed: the 9 police districts already present in the graph ───────
-- district_id values match the graph's District.district_id exactly, so
-- the backfill is an identity join rather than a fuzzy name match.
INSERT INTO police_districts (district_id, name_ur, name_en, city_id, synonyms) VALUES
    ('DIST-01', 'فیصل آباد',   'Faisalabad',    'CITY-FSD', ARRAY[]::text[]),
    ('DIST-02', 'کراچی وسطی',  'Karachi Central','CITY-KHI', ARRAY['central karachi']),
    ('DIST-03', 'حیدر آباد',   'Hyderabad',     'CITY-HYD', ARRAY[]::text[]),
    ('DIST-04', 'لاہور',       'Lahore',        'CITY-LHR', ARRAY[]::text[]),
    ('DIST-05', 'چنیوٹ',       'Chiniot',       'CITY-CHW', ARRAY[]::text[]),
    ('DIST-06', 'اسلام آباد',  'Islamabad',     'CITY-ISB', ARRAY[]::text[]),
    ('DIST-07', 'راولپنڈی',    'Rawalpindi',    'CITY-RWP', ARRAY[]::text[]),
    ('DIST-08', 'کراچی ایسٹ',  'Karachi East',  'CITY-KHI', ARRAY['east karachi']),
    ('DIST-09', 'ملتان',       'Multan',        'CITY-MUL', ARRAY[]::text[])
ON CONFLICT (district_id) DO NOTHING;

COMMIT;
