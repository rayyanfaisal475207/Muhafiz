-- ============================================================
-- 066 — Only flag conflicts that are about the SAME place
--
-- 065's sibling check caught the real error and two false positives:
--
--   تھانہ  15 stations -> 8 parents   'تھانہ' means "police station". The
--                                     stations are in different cities and
--                                     SHOULD have different parents.
--   سائبر   2 circles  -> 2 parents   Islamabad and Rawalpindi are simply
--                                     different places.
--   کراچی   2 districts-> 2 parents   کراچی ڈویژن vs کراچی ساؤتھ — both
--                                     contain 'کراچی', so these are two
--                                     READINGS OF ONE PLACE. Real conflict.
--
-- The signal is in the parents, not the children. Competing parents that
-- share a leading token are rival answers to one question; parents that do
-- not are just different places, which is the normal case.
--
-- This keeps the rule derived — no place name, and no list of generic
-- words like 'تھانہ', appears anywhere.
-- ============================================================

BEGIN;

CREATE OR REPLACE VIEW place_parent_conflicts AS
SELECT g.stem, g.kind_raw, g.siblings, g.distinct_parents,
       g.members, g.parents,
       'siblings resolved to different readings of the same parent place; '
       'at least one lookup is wrong and a rollup would be incomplete'
           AS problem
FROM place_sibling_groups g
WHERE g.distinct_parents > 1
  AND (SELECT count(DISTINCT split_part(btrim(x), ' ', 1))
         FROM unnest(g.parents) AS x
        WHERE x IS NOT NULL) < g.distinct_parents;

COMMENT ON VIEW place_parent_conflicts IS
'Should be empty. Flags only groups whose competing parents are readings '
'of the SAME place; different parents for genuinely different places are '
'normal and are not reported.';

COMMIT;
