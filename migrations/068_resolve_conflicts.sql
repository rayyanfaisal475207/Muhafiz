-- ============================================================
-- 068 — Demote a losing reading; never break a tie by guessing
--
-- Two defects in 067:
--
-- 1. OUT parameter `votes` shadowed the column of the same name:
--      ERROR: column reference "votes" is ambiguous
--
-- 2. More important, and not a syntax problem. The real conflict is:
--
--      کراچی ایسٹ -> کراچی ڈویژن    1 vote
--      کراچی وسطی -> کراچی ساؤتھ    1 vote
--
--    A 1-1 tie has no majority. Demoting either side would be picking a
--    winner by nothing at all, and then presenting the rollup that follows
--    as fact. That is precisely the confident-wrong-number failure, moved
--    into the repair tool.
--
--    So: demote only where a majority actually exists. A tie is reported
--    as needing review and both edges are marked contested, so a rollup
--    can see the group is unsettled rather than silently counting half.
-- ============================================================

BEGIN;

ALTER TABLE place_hierarchy ADD COLUMN IF NOT EXISTS contested boolean;

COMMENT ON COLUMN place_hierarchy.contested IS
'True when sibling places disagree about this parent and no majority '
'settles it. A rollup crossing a contested edge is incomplete and must '
'say so rather than return a tidy number.';

DROP FUNCTION IF EXISTS demote_conflicting_parents(numeric);

CREATE FUNCTION resolve_place_conflicts(p_floor numeric DEFAULT 0.05)
RETURNS TABLE (child_name text, parent_name text, action text, detail text)
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
                  IN (SELECT c.stem, c.kind_raw FROM place_parent_conflicts c)
        ),
        tally AS (
            SELECT e.stem, e.kind_raw, e.parent_id, count(*) AS n_votes
            FROM edges e GROUP BY e.stem, e.kind_raw, e.parent_id
        ),
        top AS (
            SELECT t.stem, t.kind_raw, max(t.n_votes) AS majority,
                   count(*) FILTER (WHERE t.n_votes =
                       (SELECT max(t2.n_votes) FROM tally t2
                         WHERE t2.stem = t.stem
                           AND t2.kind_raw = t.kind_raw)) AS n_leaders
            FROM tally t GROUP BY t.stem, t.kind_raw
        )
        SELECT e.child_id, e.parent_id, e.hierarchy_raw,
               t.n_votes, top.majority, top.n_leaders
        FROM edges e
        JOIN tally t ON t.stem = e.stem AND t.kind_raw = e.kind_raw
                    AND t.parent_id = e.parent_id
        JOIN top ON top.stem = e.stem AND top.kind_raw = e.kind_raw
    LOOP
        child_name := (SELECT name_raw FROM places WHERE place_id = r.child_id);
        parent_name := (SELECT name_raw FROM places WHERE place_id = r.parent_id);

        IF r.n_leaders > 1 THEN
            -- No majority. Mark both sides contested and leave the data
            -- alone: a tie is a question for a person, not for a tiebreak
            -- rule invented here.
            UPDATE place_hierarchy SET contested = true
             WHERE child_id = r.child_id AND parent_id = r.parent_id
               AND hierarchy_raw = r.hierarchy_raw;
            action := 'contested';
            detail := format('%s-way tie at %s vote(s); needs review',
                             r.n_leaders, r.n_votes);
        ELSIF r.n_votes < r.majority THEN
            UPDATE place_hierarchy
               SET confidence = least(coalesce(confidence, 1.0), p_floor),
                   contested = true
             WHERE child_id = r.child_id AND parent_id = r.parent_id
               AND hierarchy_raw = r.hierarchy_raw;
            action := 'demoted';
            detail := format('%s vote(s) against a majority of %s',
                             r.n_votes, r.majority);
        ELSE
            action := 'kept';
            detail := format('majority reading, %s vote(s)', r.n_votes);
        END IF;
        RETURN NEXT;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION resolve_place_conflicts(numeric) IS
'Settles sibling parent disagreements where a majority exists, and marks '
'ties contested instead of breaking them. Never deletes an edge: a wrong '
'answer stays auditable with the source that produced it.';

COMMIT;
