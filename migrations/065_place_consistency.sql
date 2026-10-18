-- ============================================================
-- 065 — Siblings must agree about their parent
--
-- THE MEASURED FAILURE
--
-- World-knowledge lookups resolved each orphan place independently, and
-- each answer looked good on its own:
--
--   کراچی وسطی -> کراچی ڈویژن   confidence 1.00   correct
--   کراچی ایسٹ -> کراچی ساؤتھ   confidence 0.90   WRONG
--
-- Karachi East is a district OF Karachi Division, not of Karachi South.
-- The model misread a snippet that listed "DC East" under a heading
-- mentioning South Karachi, and reported 0.90 — above the 0.85 bar.
--
-- The damage is precise: the two districts that "cases in Karachi" must
-- roll up ACROSS now sit under different parents, so the rollup they exist
-- to enable is broken. A per-item confidence score cannot catch this,
-- because each answer is individually plausible. Only the SET is wrong.
--
-- THE STRUCTURAL CHECK
--
-- Places whose names share a leading word are siblings — کراچی ایسٹ and
-- کراچی وسطی both begin with کراچی. Siblings of the same kind should
-- resolve to the same parent. When they do not, at least one lookup is
-- wrong, and which one is not knowable from confidence alone.
--
-- Derived, not listed: no city, district or division name appears here.
-- The rule is about shared name prefixes, which is computed from the data.
--
-- WHAT HAPPENS TO A DISAGREEMENT
--
-- It is reported, and the minority edge is demoted rather than deleted:
-- its confidence drops below any usable bar so no rollup counts through
-- it, while the row and its source URL remain for review. Deleting
-- evidence of a wrong answer would hide the fact that it was ever made.
-- ============================================================

BEGIN;

-- ── Sibling groups, by shared leading name token ────────────────────
CREATE OR REPLACE VIEW place_sibling_groups AS
SELECT split_part(btrim(p.name_raw), ' ', 1) AS stem,
       p.kind_raw,
       count(*)                              AS siblings,
       count(DISTINCT h.parent_id)           AS distinct_parents,
       array_agg(p.name_raw ORDER BY p.name_raw) AS members,
       array_agg(DISTINCT pp.name_raw)       AS parents
FROM places p
LEFT JOIN place_hierarchy h ON h.child_id = p.place_id
LEFT JOIN places pp ON pp.place_id = h.parent_id
WHERE length(split_part(btrim(p.name_raw), ' ', 1)) >= 3
GROUP BY 1, 2
HAVING count(*) > 1;

COMMENT ON VIEW place_sibling_groups IS
'Places sharing a leading name token, which makes them siblings. '
'distinct_parents > 1 means independently-resolved lookups disagree about '
'where the group belongs.';

-- ── Disagreements ───────────────────────────────────────────────────
CREATE OR REPLACE VIEW place_parent_conflicts AS
SELECT stem, kind_raw, siblings, distinct_parents, members, parents,
       'siblings resolved to different parents; at least one lookup is '
       'wrong and a rollup across this group would be incomplete' AS problem
FROM place_sibling_groups
WHERE distinct_parents > 1;

COMMENT ON VIEW place_parent_conflicts IS
'Should be empty. Non-empty means a rollup over that group counts only '
'part of it — the silent half-answer that per-item confidence cannot '
'detect.';

-- ── Demote the minority reading ─────────────────────────────────────
-- Not a delete. The row and its source URL stay visible for review; only
-- its confidence falls, so nothing counts through it.
CREATE OR REPLACE FUNCTION demote_conflicting_parents(
    p_floor numeric DEFAULT 0.05
) RETURNS TABLE (child text, demoted_parent text, kept_parent text)
LANGUAGE plpgsql AS $fn$
DECLARE r record;
BEGIN
    FOR r IN
        WITH grp AS (
            SELECT split_part(btrim(p.name_raw), ' ', 1) AS stem,
                   p.kind_raw, h.child_id, h.parent_id, h.hierarchy_raw,
                   h.confidence,
                   count(*) OVER (PARTITION BY split_part(btrim(p.name_raw),' ',1),
                                               p.kind_raw, h.parent_id) AS votes
            FROM places p
            JOIN place_hierarchy h ON h.child_id = p.place_id
            WHERE length(split_part(btrim(p.name_raw), ' ', 1)) >= 3
        ),
        ranked AS (
            SELECT g.*,
                   max(g.votes) OVER (PARTITION BY g.stem, g.kind_raw) AS top_votes,
                   count(DISTINCT g.parent_id)
                       OVER (PARTITION BY g.stem, g.kind_raw)          AS n_parents
            FROM grp g
        )
        SELECT * FROM ranked
        WHERE n_parents > 1 AND votes < top_votes
    LOOP
        UPDATE place_hierarchy
           SET confidence = least(coalesce(confidence, 1.0), p_floor)
         WHERE child_id = r.child_id AND parent_id = r.parent_id
           AND hierarchy_raw = r.hierarchy_raw;

        child := (SELECT name_raw FROM places WHERE place_id = r.child_id);
        demoted_parent := (SELECT name_raw FROM places WHERE place_id = r.parent_id);
        kept_parent := NULL;
        RETURN NEXT;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION demote_conflicting_parents(numeric) IS
'Lowers the confidence of sibling parent edges that disagree with the '
'majority of their group, so no rollup counts through a contested edge. '
'The row is kept with its source so the wrong answer stays auditable.';

COMMIT;
