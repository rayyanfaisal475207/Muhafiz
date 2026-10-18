-- ============================================================
-- 067 — Two fixes to the place-conflict machinery
--
-- 1. demote_conflicting_parents() failed outright:
--      ERROR: DISTINCT is not implemented for window functions
--    count(DISTINCT ...) OVER () is not supported in Postgres. Rewritten
--    with an aggregate CTE, which is also clearer about what it counts.
--
-- 2. The sibling rule still flagged 'تھانہ' — 15 of 19 police stations
--    share that leading word because it MEANS "police station". A token
--    carried by most places of its kind is a type noun, not a name, and
--    tells us nothing about who is a sibling of whom.
--
--    Detected by measurement rather than by listing the word: a stem
--    covering the majority of its kind is generic. تھانہ is 15/19; کراچی
--    is 2/9. No Urdu word is named here, so a corpus in another language
--    gets the same protection for free.
-- ============================================================

BEGIN;

CREATE OR REPLACE VIEW place_sibling_groups AS
WITH stems AS (
    SELECT p.place_id, p.kind_raw, p.name_raw,
           split_part(btrim(p.name_raw), ' ', 1) AS stem
    FROM places p
    WHERE length(split_part(btrim(p.name_raw), ' ', 1)) >= 3
),
kind_totals AS (
    SELECT kind_raw, count(*) AS kind_total FROM stems GROUP BY kind_raw
),
grouped AS (
    SELECT s.stem, s.kind_raw, count(*) AS siblings,
           count(DISTINCT h.parent_id) AS distinct_parents,
           array_agg(s.name_raw ORDER BY s.name_raw) AS members,
           array_agg(DISTINCT pp.name_raw) FILTER (WHERE pp.name_raw IS NOT NULL)
               AS parents
    FROM stems s
    LEFT JOIN place_hierarchy h ON h.child_id = s.place_id
    LEFT JOIN places pp ON pp.place_id = h.parent_id
    GROUP BY s.stem, s.kind_raw
    HAVING count(*) > 1
)
SELECT g.*
FROM grouped g
JOIN kind_totals k ON k.kind_raw = g.kind_raw
-- A stem shared by most places of its kind is a type word, not a name.
WHERE g.siblings::numeric / greatest(k.kind_total, 1) < 0.5;

COMMENT ON VIEW place_sibling_groups IS
'Places sharing a leading name token, excluding tokens carried by most '
'places of their kind — those are type nouns ("police station"), not '
'names, and say nothing about siblinghood.';

DROP FUNCTION IF EXISTS demote_conflicting_parents(numeric);
CREATE FUNCTION demote_conflicting_parents(
    p_floor numeric DEFAULT 0.05
) RETURNS TABLE (child text, demoted_parent text, votes bigint,
                 majority_votes bigint)
LANGUAGE plpgsql AS $fn$
DECLARE r record;
BEGIN
    FOR r IN
        WITH edges AS (
            SELECT split_part(btrim(p.name_raw), ' ', 1) AS stem,
                   p.kind_raw, h.child_id, h.parent_id, h.hierarchy_raw
            FROM places p
            JOIN place_hierarchy h ON h.child_id = p.place_id
            WHERE length(split_part(btrim(p.name_raw), ' ', 1)) >= 3
              AND (split_part(btrim(p.name_raw), ' ', 1), p.kind_raw)
                  IN (SELECT stem, kind_raw FROM place_parent_conflicts)
        ),
        tally AS (
            SELECT stem, kind_raw, parent_id, count(*) AS votes
            FROM edges GROUP BY stem, kind_raw, parent_id
        ),
        top AS (
            SELECT stem, kind_raw, max(votes) AS majority
            FROM tally GROUP BY stem, kind_raw
        )
        SELECT e.child_id, e.parent_id, e.hierarchy_raw,
               t.votes, top.majority
        FROM edges e
        JOIN tally t ON t.stem = e.stem AND t.kind_raw = e.kind_raw
                    AND t.parent_id = e.parent_id
        JOIN top   ON top.stem = e.stem AND top.kind_raw = e.kind_raw
        WHERE t.votes < top.majority
    LOOP
        UPDATE place_hierarchy
           SET confidence = least(coalesce(confidence, 1.0), p_floor)
         WHERE child_id = r.child_id AND parent_id = r.parent_id
           AND hierarchy_raw = r.hierarchy_raw;

        child := (SELECT name_raw FROM places WHERE place_id = r.child_id);
        demoted_parent := (SELECT name_raw FROM places WHERE place_id = r.parent_id);
        votes := r.votes;
        majority_votes := r.majority;
        RETURN NEXT;
    END LOOP;
END;
$fn$;

COMMIT;
