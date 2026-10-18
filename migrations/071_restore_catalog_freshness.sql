-- ============================================================
-- 071 — Restore catalog_freshness
--
-- 060 rebuilt column_catalog with DROP ... CASCADE, which silently took
-- catalog_freshness with it. Nothing referenced the view until the new
-- /api/ask/health endpoint did, and it returned 500 — the first request
-- ever made against it.
--
-- A CASCADE removes whatever depends on the thing being dropped, and the
-- list is not printed unless you ask. Everything else 060 destroyed was
-- recreated in the same migration; this one was missed because no test
-- touched it.
-- ============================================================

BEGIN;

CREATE OR REPLACE VIEW catalog_freshness AS
SELECT (SELECT count(*) FROM schema_relationships)                AS cached_edges,
       (SELECT count(*) FROM pg_constraint con
          JOIN pg_class c ON c.oid = con.conrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE con.contype='f' AND n.nspname='public') * 2        AS live_edges,
       (SELECT count(*) FROM column_catalog)                      AS cached_columns,
       (SELECT count(*) FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname='public' AND c.relkind='r'
           AND a.attnum > 0 AND NOT a.attisdropped)               AS live_columns;

COMMENT ON VIEW catalog_freshness IS
'Cached catalog size against live catalog size. A mismatch means a '
'migration ran without refresh_catalog(), so the planner is reasoning '
'about a schema that no longer exists.';

COMMIT;
