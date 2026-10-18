BEGIN;
ALTER TABLE evidence.source_bindings DROP CONSTRAINT IF EXISTS source_bindings_transform_check;
ALTER TABLE evidence.source_bindings ADD CONSTRAINT source_bindings_transform_check
CHECK(transform IN ('text','number','boolean','date','entity'));
COMMIT;
