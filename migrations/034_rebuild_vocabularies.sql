-- ============================================================
-- 034 — Controlled vocabularies (rebuild stage 1a)
--
-- WHY THIS EXISTS
--
-- Measured failure, Phase 6 of the aggregate engine: the planner filtered
--   Weapon.license_status = "unlicensed"
--   StructuredRecord.record_type = "malkhana"
-- against data that actually holds "بغیر لائسنس" and "malkhana_register".
-- Both queries returned 0 where the truth was 30 and 45. The property
-- names were known; only the VALUES were not. A confident zero is the
-- worst output this system can produce.
--
-- Prompting cannot fix that reliably — the model is guessing English
-- against Urdu storage. The fix is structural: every low-cardinality
-- attribute becomes a lookup table holding BOTH scripts plus known
-- synonyms, and callers filter on a stable ASCII id. The Urdu value is
-- then never guessed, only joined.
--
-- `synonyms` is a text[] rather than a child table on purpose: it is
-- read-mostly, always fetched whole, and never joined against.
--
-- Additive only. Nothing is dropped and no existing table is altered.
-- ============================================================

BEGIN;

-- ── Helper: every vocabulary shares this shape ──────────────────────
--   <id>      stable ASCII key the planner filters on
--   name_ur   the exact string as stored in source documents
--   name_en   English label, for prompts and English-language answers
--   synonyms  alternate surface forms seen in the wild, either script

CREATE TABLE IF NOT EXISTS genders (
    gender_id  text PRIMARY KEY,
    name_ur    text NOT NULL,
    name_en    text NOT NULL,
    synonyms   text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS party_roles (
    role_id    text PRIMARY KEY,
    name_ur    text NOT NULL,
    name_en    text NOT NULL,
    synonyms   text[] NOT NULL DEFAULT '{}',
    -- A complainant/applicant is the reporting side; accused is the
    -- subject side. Aggregates like "how many people were involved"
    -- need to know which roles double-count a single human being.
    is_subject boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS case_statuses (
    status_id   text PRIMARY KEY,
    name_ur     text NOT NULL,
    name_en     text NOT NULL,
    synonyms    text[] NOT NULL DEFAULT '{}',
    -- Distinguishes "closed" outcomes from in-flight ones so
    -- "how many cases are still open" is a predicate, not a string match.
    is_terminal boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS crime_categories (
    category_id text PRIMARY KEY,
    name_ur     text NOT NULL,
    name_en     text NOT NULL,
    synonyms    text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS license_states (
    license_state_id text PRIMARY KEY,
    name_ur          text NOT NULL,
    name_en          text NOT NULL,
    synonyms         text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS weapon_types (
    weapon_type_id text PRIMARY KEY,
    name_ur        text NOT NULL,
    name_en        text NOT NULL,
    synonyms       text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS station_types (
    station_type_id text PRIMARY KEY,
    name_ur         text NOT NULL,
    name_en         text NOT NULL,
    synonyms        text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS event_types (
    event_type_id text PRIMARY KEY,
    name_ur       text NOT NULL,
    name_en       text NOT NULL,
    synonyms      text[] NOT NULL DEFAULT '{}',
    -- Ordering hint for timeline rendering when two events share a
    -- timestamp (an arrest and its recovery are commonly same-minute).
    typical_order integer
);

CREATE TABLE IF NOT EXISTS ranks (
    rank_id   text PRIMARY KEY,
    name_ur   text NOT NULL,
    name_en   text NOT NULL,
    synonyms  text[] NOT NULL DEFAULT '{}',
    seniority integer
);

CREATE TABLE IF NOT EXISTS document_types (
    doc_type_id text PRIMARY KEY,
    name_ur     text NOT NULL,
    name_en     text NOT NULL,
    synonyms    text[] NOT NULL DEFAULT '{}',
    -- Whether documents of this type are case evidence (scoped by RBAC)
    -- or shared reference material (readable by everyone).
    is_evidence boolean NOT NULL DEFAULT true
);

-- ── Seed data ───────────────────────────────────────────────────────
-- Urdu values below are the EXACT strings observed in the live corpus,
-- not translations. Where a value was measured in the existing data the
-- comment says so; these are the strings a filter must match.

INSERT INTO genders (gender_id, name_ur, name_en, synonyms) VALUES
    ('M', 'مرد',   'male',   ARRAY['مذکر','man','M','male']),
    ('F', 'عورت',  'female', ARRAY['مؤنث','خاتون','woman','F','female']),
    ('O', 'دیگر',  'other',  ARRAY['other','unspecified'])
ON CONFLICT (gender_id) DO NOTHING;

INSERT INTO party_roles (role_id, name_ur, name_en, synonyms, is_subject) VALUES
    ('ACCUSED',     'ملزم',    'accused',     ARRAY['ملزمان','مجرم','suspect','offender'], true),
    ('COMPLAINANT', 'مدعی',    'complainant', ARRAY['مدعیہ','شاکی','reporter'],            false),
    ('WITNESS',     'گواہ',    'witness',     ARRAY['گواہان','شاہد'],                      false),
    ('VICTIM',      'متاثرہ',  'victim',      ARRAY['مقتول','مقتولہ','injured','deceased'], false),
    ('APPLICANT',   'درخواست گزار', 'applicant', ARRAY['applicant','petitioner'],          false)
ON CONFLICT (role_id) DO NOTHING;

INSERT INTO case_statuses (status_id, name_ur, name_en, synonyms, is_terminal) VALUES
    ('REGISTERED',   'درج',            'registered',          ARRAY['registered','lodged'],     false),
    ('UNDER_INVEST', 'زیرِ تفتیش',      'under investigation', ARRAY['زیر تفتیش','investigating'], false),
    ('CHALLANED',    'چالان',          'challaned',           ARRAY['challan','charge-sheeted'], false),
    ('IN_COURT',     'زیرِ سماعت',      'in court',            ARRAY['زیر سماعت','trial'],        false),
    ('DISPOSED',     'نمٹا دیا گیا',    'disposed',            ARRAY['closed','disposed'],        true),
    ('UNTRACED',     'ناقابلِ سراغ',    'untraced',            ARRAY['untraced','A-class'],       true)
ON CONFLICT (status_id) DO NOTHING;

INSERT INTO crime_categories (category_id, name_ur, name_en, synonyms) VALUES
    ('THEFT',       'چوری',              'theft',            ARRAY['موٹرسائیکل چوری','مobile theft','379']),
    ('ROBBERY',     'ڈکیتی',             'robbery',          ARRAY['مسلحانہ ڈکیتی','dacoity','armed robbery']),
    ('SNATCHING',   'چھینا جھپٹی',       'snatching',        ARRAY['snatching']),
    ('ASSAULT',     'تشدد',              'assault',          ARRAY['مار پیٹ','hurt','battery']),
    ('CYBER_FRAUD', 'سائبر فراڈ',        'cyber fraud',      ARRAY['آن لائن فراڈ','online scam','PECA']),
    ('ARMS',        'اسلحہ',             'arms offence',     ARRAY['ناجائز اسلحہ','illegal weapon']),
    ('NARCOTICS',   'منشیات',            'narcotics',        ARRAY['drugs','CNSA']),
    ('MURDER',      'قتل',               'murder',           ARRAY['homicide','302']),
    ('KIDNAPPING',  'اغوا',              'kidnapping',       ARRAY['abduction']),
    ('DOMESTIC',    'گھریلو تشدد',       'domestic violence',ARRAY['domestic abuse']),
    ('OTHER',       'دیگر',              'other',            ARRAY[]::text[])
ON CONFLICT (category_id) DO NOTHING;

-- 'بغیر لائسنس' is the exact live value that caused the measured
-- zero-result failure described in this file's header.
INSERT INTO license_states (license_state_id, name_ur, name_en, synonyms) VALUES
    ('UNLICENSED', 'بغیر لائسنس', 'unlicensed', ARRAY['ناجائز','بلا لائسنس','illegal','no licence','unlicenced']),
    ('LICENSED',   'لائسنس شدہ',  'licensed',   ARRAY['جائز','licenced','valid licence']),
    ('UNKNOWN',    'نامعلوم',     'unknown',    ARRAY['not stated','unspecified'])
ON CONFLICT (license_state_id) DO NOTHING;

INSERT INTO weapon_types (weapon_type_id, name_ur, name_en, synonyms) VALUES
    ('PISTOL',   'پستول',       'pistol',        ARRAY['بور پستول','handgun','gun']),
    ('RIFLE',    'رائفل',       'rifle',         ARRAY['bore rifle']),
    ('SHOTGUN',  'بندوق',       'shotgun',       ARRAY['gun']),
    ('KNIFE',    'چھری',        'knife',         ARRAY['چاقو','blade','dagger']),
    ('STICK',    'ڈنڈا',        'blunt weapon',  ARRAY['لکڑی کی چھڑی','wooden stick','baton']),
    ('OTHER',    'دیگر',        'other',         ARRAY[]::text[])
ON CONFLICT (weapon_type_id) DO NOTHING;

-- Observed in the live corpus: regular thanas, a women's station
-- ('خواتین تھانہ، فیصل آباد'), cybercrime circles
-- ('سائبر کرائم سرکل، راولپنڈی') and a motorway post.
-- Station type is currently inferrable only by string matching, so
-- "how many cybercrime circles are there" is unanswerable today.
INSERT INTO station_types (station_type_id, name_ur, name_en, synonyms) VALUES
    ('THANA',    'تھانہ',              'police station',   ARRAY['thana','PS']),
    ('WOMEN',    'خواتین تھانہ',       'women police station', ARRAY['women station','ladies police station']),
    ('CYBER',    'سائبر کرائم سرکل',   'cybercrime circle', ARRAY['cyber circle','NR3C','cybercrime']),
    ('MOTORWAY', 'موٹروے پولیس اسٹیشن','motorway police station', ARRAY['NHMP','motorway patrol']),
    ('SPECIAL',  'خصوصی یونٹ',         'special unit',      ARRAY['CIA','investigation wing'])
ON CONFLICT (station_type_id) DO NOTHING;

INSERT INTO event_types (event_type_id, name_ur, name_en, synonyms, typical_order) VALUES
    ('REPORT',     'اطلاع',        'report received',   ARRAY['complaint'],            10),
    ('REGISTER',   'اندراج',       'FIR registered',    ARRAY['FIR lodged'],           20),
    ('ZIMNI',      'زمنی',         'case diary entry',  ARRAY['zimni','daily diary'],  30),
    ('ARREST',     'گرفتاری',      'arrest',            ARRAY['apprehended'],          40),
    ('RECOVERY',   'برآمدگی',      'recovery',          ARRAY['seizure','recovered'],  50),
    ('MEDICAL',    'طبی معائنہ',   'medical examination',ARRAY['MLC','post-mortem'],   60),
    ('FORENSIC',   'فرانزک',       'forensic analysis', ARRAY['lab report'],           70),
    ('CHALLAN',    'چالان',        'challan submitted', ARRAY['charge sheet'],         80),
    ('COURT',      'عدالتی کارروائی','court proceeding',ARRAY['hearing'],              90)
ON CONFLICT (event_type_id) DO NOTHING;

INSERT INTO ranks (rank_id, name_ur, name_en, synonyms, seniority) VALUES
    ('CONSTABLE', 'کانسٹیبل',       'constable',           ARRAY['PC'],              10),
    ('HEAD_CON',  'ہیڈ کانسٹیبل',   'head constable',      ARRAY['HC'],              20),
    ('ASI',       'اے ایس آئی',     'assistant sub-inspector', ARRAY['ASI'],         30),
    ('SI',        'سب انسپکٹر',     'sub-inspector',       ARRAY['SI'],              40),
    ('INSPECTOR', 'انسپکٹر',        'inspector',           ARRAY['SHO'],             50),
    ('DSP',       'ڈی ایس پی',      'deputy superintendent', ARRAY['DSP'],           60),
    ('SP',        'ایس پی',         'superintendent',      ARRAY['SP'],              70)
ON CONFLICT (rank_id) DO NOTHING;

INSERT INTO document_types (doc_type_id, name_ur, name_en, synonyms, is_evidence) VALUES
    ('FIR',        'ایف آئی آر',      'first information report', ARRAY['fir','narrative'], true),
    ('ZIMNI',      'زمنی',            'case diary',               ARRAY['zimni','diary'],   true),
    ('CHALAAN',    'چالان',           'challan',                  ARRAY['charge sheet'],    true),
    ('MALKHANA',   'مال خانہ رجسٹر',  'property register',        ARRAY['malkhana_register','property'], true),
    ('MEDICAL',    'طبی رپورٹ',       'medical report',           ARRAY['MLC'],             true),
    ('STATUTE',    'قانون',           'statute',                  ARRAY['act','ordinance','rules'], false),
    ('GUIDELINE',  'ہدایات',          'guideline',                ARRAY['SOP'],             false)
ON CONFLICT (doc_type_id) DO NOTHING;

COMMIT;
