-- Additive workflow-v2 catalog. Apply to an isolated database before rollout.
BEGIN;
CREATE TABLE evidence.source_profiles (
  profile_id uuid PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES evidence.documents,
  source_namespace text NOT NULL,
  schema_fingerprint text NOT NULL,
  profile jsonb NOT NULL,
  proposed jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','validated','rejected','superseded')),
  review jsonb NOT NULL DEFAULT '{}',
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(document_id,source_namespace,schema_fingerprint)
);
CREATE TABLE evidence.source_bindings (
  binding_id uuid PRIMARY KEY,
  profile_id uuid NOT NULL REFERENCES evidence.source_profiles,
  document_id uuid NOT NULL REFERENCES evidence.documents,
  source_namespace text NOT NULL,
  schema_fingerprint text NOT NULL,
  pointer text NOT NULL,
  concept_id uuid NOT NULL REFERENCES evidence.concepts,
  concept_version integer NOT NULL,
  subject_type uuid NOT NULL REFERENCES evidence.concepts,
  transform text NOT NULL CHECK(transform IN ('text','number','boolean','date')),
  status text NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','validated','rejected','superseded')),
  evidence jsonb NOT NULL,
  review jsonb NOT NULL DEFAULT '{}',
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(profile_id,pointer,concept_id,concept_version)
);
CREATE INDEX evidence_binding_schema ON evidence.source_bindings(source_namespace,schema_fingerprint,status);
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['source_profiles','source_bindings'] LOOP
    EXECUTE format('ALTER TABLE evidence.%I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('ALTER TABLE evidence.%I FORCE ROW LEVEL SECURITY',t);
    EXECUTE format('CREATE POLICY scoped_read ON evidence.%I FOR SELECT TO muhafiz_evidence_reader USING(evidence.visible_document(document_id))',t);
    EXECUTE format('CREATE POLICY ingestion_access ON evidence.%I TO muhafiz_evidence_ingestor USING(true) WITH CHECK(true)',t);
    EXECUTE format('GRANT SELECT ON evidence.%I TO muhafiz_evidence_reader',t);
    EXECUTE format('GRANT SELECT,INSERT,UPDATE ON evidence.%I TO muhafiz_evidence_ingestor',t);
  END LOOP;
END $$;
REVOKE ALL ON evidence.source_profiles,evidence.source_bindings FROM PUBLIC;
COMMIT;
