BEGIN;
ALTER TABLE evidence.discovery_jobs ADD COLUMN failed_attempts integer NOT NULL DEFAULT 0 CHECK(failed_attempts>=0);
COMMIT;
