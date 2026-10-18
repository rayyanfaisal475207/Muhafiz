BEGIN;
CREATE TABLE IF NOT EXISTS evidence.projections (
  cache_key text PRIMARY KEY,
  user_id uuid NOT NULL,
  session_id uuid NOT NULL,
  scope_fingerprint text NOT NULL,
  source_snapshot text NOT NULL,
  source_documents uuid[] NOT NULL DEFAULT '{}',
  projection_version text NOT NULL,
  result jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT now()+interval '30 minutes'
);
ALTER TABLE evidence.projections ADD COLUMN IF NOT EXISTS source_documents uuid[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS evidence_projection_expiry ON evidence.projections(expires_at);
ALTER TABLE evidence.projections ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence.projections FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS projection_access ON evidence.projections;
CREATE POLICY projection_access ON evidence.projections TO muhafiz_evidence_reader
USING(user_id::text=current_setting('evidence.user_id',true)
  AND session_id::text=current_setting('evidence.session_id',true)
  AND scope_fingerprint=current_setting('evidence.scope_fingerprint',true)
  AND NOT EXISTS(SELECT 1 FROM unnest(source_documents) doc_id WHERE NOT evidence.visible_document(doc_id)))
WITH CHECK(user_id::text=current_setting('evidence.user_id',true)
  AND session_id::text=current_setting('evidence.session_id',true)
  AND scope_fingerprint=current_setting('evidence.scope_fingerprint',true));
DROP POLICY IF EXISTS projection_ingestion ON evidence.projections;
CREATE POLICY projection_ingestion ON evidence.projections TO muhafiz_evidence_ingestor
USING(true) WITH CHECK(true);
REVOKE ALL ON evidence.projections FROM PUBLIC;
GRANT SELECT,INSERT,DELETE ON evidence.projections TO muhafiz_evidence_reader;
GRANT SELECT,INSERT,UPDATE,DELETE ON evidence.projections TO muhafiz_evidence_ingestor;
COMMIT;
