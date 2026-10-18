-- ============================================================
-- 055 — Identity resolution: propose, never presume
--
-- THE PROBLEM, MEASURED
--
-- Extraction writes one person row per MENTION, deliberately. The same
-- accused named in the FIR narrative and again in a zimni produced two
-- rows, so "how many accused" answered 2 where the truth was 1.
--
-- That is L4 again — the graph's 73 PART_OF edges for 19 stations — in a
-- new place. A count over mentions is not a count over people.
--
-- WHAT MAY BE MERGED AUTOMATICALLY, AND WHAT MAY NOT
--
-- CNIC is a national unique identifier and the schema already enforces
-- uq_persons_cnic. Two mentions with the same CNIC ARE one person, and
-- extraction now reuses the row. That is exact, not inferred.
--
-- Everything else is a proposal. Predicted failure F3 is a wrong merge
-- corrupting every count that touches that person, and unlike a missing
-- row a wrong merge is invisible — the count simply comes out lower and
-- looks tidier. In a corpus where 'محمد اسلم' is a common name, merging
-- on a shared name is not resolution, it is a guess that cannot be
-- distinguished from an answer.
--
-- So: name evidence generates CANDIDATES with a method and a confidence,
-- and nothing is merged until status = 'confirmed'.
--
-- COUNTS MUST DISCLOSE WHAT THEY DID NOT RESOLVE
--
-- The honest count is over confirmed clusters plus unresolved singletons,
-- reported alongside how many candidates are still unreviewed. A number
-- that hides 40 pending merges is the confident-wrong-number failure
-- wearing a cleaner shirt.
-- ============================================================

BEGIN;

-- ── Matching keys, derived not stored by hand ───────────────────────
-- name_skeleton existed as a plain column that nothing ever filled.
-- Generated from normalize_name so it cannot drift from the name.
ALTER TABLE persons DROP COLUMN IF EXISTS name_skeleton;
ALTER TABLE persons ADD COLUMN name_skeleton text
    GENERATED ALWAYS AS (normalize_name(canonical_name_ur)) STORED;
ALTER TABLE persons ADD COLUMN IF NOT EXISTS father_skeleton text
    GENERATED ALWAYS AS (normalize_name(father_name_ur)) STORED;

CREATE INDEX IF NOT EXISTS idx_persons_skeleton ON persons (name_skeleton);
CREATE INDEX IF NOT EXISTS idx_persons_father   ON persons (father_skeleton);
CREATE INDEX IF NOT EXISTS idx_persons_cnic_key ON persons (cnic);

-- ── Candidate generation ────────────────────────────────────────────
-- Re-runnable. Adds proposals, never downgrades a human decision: a link
-- already reviewed keeps its status.
CREATE OR REPLACE FUNCTION propose_identity_links()
RETURNS TABLE (method text, proposed bigint) LANGUAGE plpgsql AS $fn$
BEGIN
    -- Same CNIC. Exact, and auto-confirmed: this is an identifier, not a
    -- resemblance. Normally prevented at write time; this catches any
    -- pair that predates that rule.
    RETURN QUERY
    WITH ins AS (
        INSERT INTO person_identity_links
            (person_id_a, person_id_b, confidence, method, status)
        SELECT least(a.person_id, b.person_id), greatest(a.person_id, b.person_id),
               1.0, 'cnic_exact', 'confirmed'
        FROM persons a JOIN persons b
          ON a.cnic = b.cnic AND a.person_id < b.person_id
        WHERE a.cnic IS NOT NULL AND btrim(a.cnic) <> ''
        ON CONFLICT (person_id_a, person_id_b) DO NOTHING
        RETURNING 1
    ) SELECT 'cnic_exact'::text, count(*) FROM ins;

    -- Same name AND same father name. In Pakistani records the father name
    -- is the standard disambiguator, so this is strong — but still only a
    -- proposal, because two cousins can share both.
    RETURN QUERY
    WITH ins AS (
        INSERT INTO person_identity_links
            (person_id_a, person_id_b, confidence, method, status)
        SELECT least(a.person_id, b.person_id), greatest(a.person_id, b.person_id),
               0.85, 'name_and_father', 'proposed'
        FROM persons a JOIN persons b
          ON a.name_skeleton = b.name_skeleton
         AND a.father_skeleton = b.father_skeleton
         AND a.person_id < b.person_id
        WHERE a.name_skeleton IS NOT NULL
          AND a.father_skeleton IS NOT NULL
        ON CONFLICT (person_id_a, person_id_b) DO NOTHING
        RETURNING 1
    ) SELECT 'name_and_father'::text, count(*) FROM ins;

    -- Same name only. Deliberately low confidence. Listed so a reviewer
    -- can see it; never acted on by a count.
    RETURN QUERY
    WITH ins AS (
        INSERT INTO person_identity_links
            (person_id_a, person_id_b, confidence, method, status)
        SELECT least(a.person_id, b.person_id), greatest(a.person_id, b.person_id),
               0.40, 'name_only', 'proposed'
        FROM persons a JOIN persons b
          ON a.name_skeleton = b.name_skeleton
         AND a.person_id < b.person_id
        WHERE a.name_skeleton IS NOT NULL AND a.name_skeleton <> ''
          AND (a.father_skeleton IS NULL OR b.father_skeleton IS NULL)
        ON CONFLICT (person_id_a, person_id_b) DO NOTHING
        RETURNING 1
    ) SELECT 'name_only'::text, count(*) FROM ins;
END;
$fn$;

COMMENT ON FUNCTION propose_identity_links() IS
'Generates identity candidates with a method and confidence. Only '
'cnic_exact is auto-confirmed, because a CNIC is an identifier rather '
'than a resemblance. Everything else awaits review.';

-- ── Clusters, over CONFIRMED links only ─────────────────────────────
-- Connected components by transitive closure. A person with no confirmed
-- link is their own cluster, which is what makes an unresolved mention
-- still countable — as itself, not as nothing.
CREATE OR REPLACE VIEW person_clusters AS
WITH RECURSIVE edges AS (
    SELECT person_id_a AS a, person_id_b AS b FROM person_identity_links
     WHERE status = 'confirmed'
    UNION ALL
    SELECT person_id_b, person_id_a FROM person_identity_links
     WHERE status = 'confirmed'
),
walk AS (
    SELECT p.person_id AS member, p.person_id AS root, 0 AS depth,
           p.person_id AS seen
    FROM persons p
    UNION ALL
    SELECT e.b, w.root, w.depth + 1, w.seen || '>' || e.b
    FROM walk w JOIN edges e ON e.a = w.member
    WHERE w.depth < 8
      AND ('>' || w.seen || '>') NOT LIKE ('%>' || e.b || '>%')
)
SELECT member AS person_id, min(root) AS cluster_id
FROM walk GROUP BY member;

COMMENT ON VIEW person_clusters IS
'Maps every person mention to a cluster representing one human. Built '
'from CONFIRMED links only, so an unreviewed candidate never silently '
'changes a count.';

-- ── The countable view ──────────────────────────────────────────────
CREATE OR REPLACE VIEW persons_resolved AS
SELECT c.cluster_id                                   AS person_key,
       min(p.canonical_name_ur)                       AS name,
       max(p.father_name_ur)                          AS father_name,
       max(p.cnic)                                    AS cnic,
       count(*)                                       AS mention_count,
       array_agg(DISTINCT p.person_id)                AS mentions
FROM persons p
JOIN person_clusters c ON c.person_id = p.person_id
GROUP BY c.cluster_id;

COMMENT ON VIEW persons_resolved IS
'One row per resolved human. Count people from HERE, never from persons, '
'which holds one row per mention.';

-- ── What a count must say about itself ──────────────────────────────
CREATE OR REPLACE VIEW identity_resolution_status AS
SELECT (SELECT count(*) FROM persons)                          AS mentions,
       (SELECT count(*) FROM persons_resolved)                 AS resolved_people,
       (SELECT count(*) FROM person_identity_links
         WHERE status = 'confirmed')                           AS confirmed_links,
       (SELECT count(*) FROM person_identity_links
         WHERE status = 'proposed')                            AS pending_review,
       (SELECT count(*) FROM person_identity_links
         WHERE status = 'proposed' AND method = 'name_only')   AS pending_name_only,
       (SELECT CASE WHEN count(*) = 0 THEN 'no pending merges; a person '
                    'count is final'
               ELSE 'a person count may fall by up to ' || count(*) ||
                    ' if pending merges are confirmed, and must say so'
               END
          FROM person_identity_links WHERE status = 'proposed') AS disclosure;

COMMENT ON VIEW identity_resolution_status IS
'What any count of people is obliged to disclose: how many candidate '
'merges are unreviewed, and therefore how far the number could move.';

COMMIT;
