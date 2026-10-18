-- Additive evidence runtime. Run as the migration owner; application roles
-- cannot migrate, validate assertions, or acquire ingestion privileges.
BEGIN;
CREATE SCHEMA evidence;
REVOKE ALL ON SCHEMA evidence FROM PUBLIC;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='muhafiz_evidence_reader') THEN
    CREATE ROLE muhafiz_evidence_reader NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='muhafiz_evidence_ingestor') THEN
    CREATE ROLE muhafiz_evidence_ingestor NOLOGIN;
  END IF;
END $$;

CREATE TABLE evidence.documents (
  document_id uuid PRIMARY KEY, source_key text NOT NULL, revision integer NOT NULL DEFAULT 1,
  title text NOT NULL DEFAULT '', content text NOT NULL, content_hash text NOT NULL,
  case_ids text[] NOT NULL DEFAULT '{}', project_id uuid, owner_id uuid, session_id uuid,
  is_global boolean NOT NULL DEFAULT false, source_metadata jsonb NOT NULL DEFAULT '{}',
  supersedes uuid REFERENCES evidence.documents, recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(source_key,content_hash), CHECK(session_id IS NULL OR owner_id IS NOT NULL),
  CHECK(NOT is_global OR (session_id IS NULL AND cardinality(case_ids)=0))
);
CREATE INDEX evidence_document_cases ON evidence.documents USING gin(case_ids);
CREATE FUNCTION evidence.preserve_source() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.content IS DISTINCT FROM OLD.content OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
 OR NEW.source_key IS DISTINCT FROM OLD.source_key OR NEW.document_id IS DISTINCT FROM OLD.document_id THEN
   RAISE EXCEPTION 'source evidence is immutable; create a new document revision';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER evidence_immutable_source BEFORE UPDATE ON evidence.documents
 FOR EACH ROW EXECUTE FUNCTION evidence.preserve_source();
CREATE TABLE evidence.spans (
  span_id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES evidence.documents,
  char_start integer NOT NULL CHECK(char_start>=0), char_end integer NOT NULL,
  text text NOT NULL, CHECK(char_end>char_start AND char_length(text)=char_end-char_start),
  UNIQUE(document_id,char_start,char_end)
);
CREATE INDEX evidence_span_text ON evidence.spans USING gin(to_tsvector('simple',text));
CREATE FUNCTION evidence.check_span() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,evidence AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM evidence.documents d WHERE d.document_id=NEW.document_id
   AND substring(d.content FROM NEW.char_start+1 FOR NEW.char_end-NEW.char_start)=NEW.text) THEN
   RAISE EXCEPTION 'span does not match its source document';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER evidence_span_source BEFORE INSERT OR UPDATE ON evidence.spans
 FOR EACH ROW EXECUTE FUNCTION evidence.check_span();
CREATE TABLE evidence.concepts (
  concept_id uuid PRIMARY KEY, label text NOT NULL, kind text NOT NULL
    CHECK(kind IN ('entity_type','property','relation','event_type')),
  meaning text NOT NULL, aliases text[] NOT NULL DEFAULT '{}',
  value_type text NOT NULL CHECK(value_type IN ('text','number','boolean','date','entity')),
  unit text, grain text NOT NULL, version integer NOT NULL DEFAULT 1,
  origin_document_id uuid NOT NULL REFERENCES evidence.documents,
  status text NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','validated','disputed','superseded')),
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE evidence.entities (
  entity_id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES evidence.documents,
  concept_id uuid NOT NULL REFERENCES evidence.concepts, label text NOT NULL,
  span_id uuid NOT NULL REFERENCES evidence.spans,
  identity_status text NOT NULL DEFAULT 'unresolved' CHECK(identity_status IN ('unresolved','confirmed')),
  canonical_id uuid, identifiers jsonb NOT NULL DEFAULT '{}',
  UNIQUE(document_id,concept_id,span_id,label)
);
CREATE TABLE evidence.concept_revisions (
  concept_id uuid NOT NULL REFERENCES evidence.concepts,version integer NOT NULL,
  document_id uuid NOT NULL REFERENCES evidence.documents,definition jsonb NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(concept_id,version)
);
CREATE FUNCTION evidence.version_concept() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,evidence AS $$
BEGIN
 IF (NEW.meaning,NEW.kind,NEW.value_type,NEW.unit,NEW.grain,NEW.aliases) IS DISTINCT FROM
    (OLD.meaning,OLD.kind,OLD.value_type,OLD.unit,OLD.grain,OLD.aliases) THEN
   INSERT INTO evidence.concept_revisions(concept_id,version,document_id,definition)
     VALUES(OLD.concept_id,OLD.version,OLD.origin_document_id,to_jsonb(OLD));
   NEW.version=OLD.version+1;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER evidence_catalog_version BEFORE UPDATE ON evidence.concepts
 FOR EACH ROW EXECUTE FUNCTION evidence.version_concept();
CREATE TABLE evidence.assertions (
  assertion_id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES evidence.documents,
  subject_id uuid NOT NULL REFERENCES evidence.entities,
  predicate_id uuid NOT NULL REFERENCES evidence.concepts,
  object_entity_id uuid REFERENCES evidence.entities,
  value_text text, value_number numeric, value_boolean boolean, value_date timestamptz,
  unit text, span_id uuid NOT NULL REFERENCES evidence.spans,
  event_time timestamptz, valid_from timestamptz, valid_to timestamptz,
  recorded_at timestamptz NOT NULL DEFAULT now(), retracted_at timestamptz,
  status text NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','validated','disputed','superseded')),
  extraction_version text NOT NULL, validation jsonb NOT NULL DEFAULT '{}',
  source_record_key text, CHECK(num_nonnulls(object_entity_id,value_text,value_number,value_boolean,value_date)=1),
  CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>valid_from)
);
CREATE INDEX evidence_assertion_query ON evidence.assertions(predicate_id,subject_id,status);
CREATE INDEX evidence_assertion_document ON evidence.assertions(document_id);
CREATE INDEX evidence_assertion_object ON evidence.assertions(object_entity_id) WHERE object_entity_id IS NOT NULL;
CREATE FUNCTION evidence.check_assertion_source() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,evidence AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM evidence.spans s WHERE s.span_id=NEW.span_id AND s.document_id=NEW.document_id)
 OR NOT EXISTS(SELECT 1 FROM evidence.entities e WHERE e.entity_id=NEW.subject_id AND e.document_id=NEW.document_id)
 OR (NEW.object_entity_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM evidence.entities e
   WHERE e.entity_id=NEW.object_entity_id AND e.document_id=NEW.document_id)) THEN
   RAISE EXCEPTION 'assertion references evidence outside its source document';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER evidence_assertion_source BEFORE INSERT OR UPDATE ON evidence.assertions
 FOR EACH ROW EXECUTE FUNCTION evidence.check_assertion_source();
CREATE FUNCTION evidence.check_entity_source() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,evidence AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM evidence.spans s WHERE s.span_id=NEW.span_id AND s.document_id=NEW.document_id) THEN
   RAISE EXCEPTION 'entity references a different source document';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER evidence_entity_source BEFORE INSERT OR UPDATE ON evidence.entities
 FOR EACH ROW EXECUTE FUNCTION evidence.check_entity_source();
CREATE TABLE evidence.identity_links (
  left_id uuid NOT NULL REFERENCES evidence.entities, right_id uuid NOT NULL REFERENCES evidence.entities,
  status text NOT NULL CHECK(status IN ('candidate','confirmed','rejected')),
  method text NOT NULL, justification jsonb NOT NULL, reviewed_by uuid,
  recorded_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(left_id,right_id), CHECK(left_id<right_id)
);
CREATE TABLE evidence.coverage (
  document_id uuid NOT NULL REFERENCES evidence.documents, concept_id uuid NOT NULL REFERENCES evidence.concepts,
  concept_version integer NOT NULL, content_hash text NOT NULL,
  status text NOT NULL CHECK(status IN ('complete','partial','failed')),
  extraction_version text NOT NULL, details jsonb NOT NULL DEFAULT '{}',
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(document_id,concept_id,concept_version,extraction_version)
);
CREATE TABLE evidence.legacy_candidates (
  candidate_id uuid PRIMARY KEY,document_id uuid NOT NULL REFERENCES evidence.documents,
  source_table text NOT NULL,source_key text NOT NULL,payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','reviewed','rejected')),
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE evidence.extraction_runs (
  extraction_id uuid PRIMARY KEY,document_id uuid NOT NULL REFERENCES evidence.documents,
  extraction_version text NOT NULL,proposed jsonb NOT NULL,review jsonb NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE evidence.ingestion_jobs (
  document_id uuid PRIMARY KEY REFERENCES evidence.documents,
  status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','complete','failed')),
  attempts integer NOT NULL DEFAULT 0, lease_until timestamptz,
  progress jsonb NOT NULL DEFAULT '{}',updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE evidence.runs (
  run_id uuid PRIMARY KEY, user_id uuid NOT NULL, session_id uuid NOT NULL,
  scope jsonb NOT NULL, question text NOT NULL, state jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','answered','partial','clarification','not_extracted','unavailable','cancelled')),
  response jsonb, progress jsonb NOT NULL DEFAULT '{}', cancel_requested boolean NOT NULL DEFAULT false,
  attempts integer NOT NULL DEFAULT 0, lease_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX evidence_run_queue ON evidence.runs(status,created_at);
CREATE UNIQUE INDEX evidence_session_active ON evidence.runs(session_id) WHERE status IN ('pending','running');
CREATE TABLE evidence.conversations (
  session_id uuid PRIMARY KEY, user_id uuid NOT NULL, state jsonb NOT NULL,
  revision integer NOT NULL DEFAULT 0, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE evidence.artifacts (
  artifact_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES evidence.runs,
  user_id uuid NOT NULL, path text NOT NULL, format text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE evidence.discovery_jobs (
  job_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES evidence.runs,
  user_id uuid NOT NULL, scope jsonb NOT NULL, terms text[] NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','complete','partial','failed','cancelled')),
  progress jsonb NOT NULL DEFAULT '{}', lease_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);

-- Transaction-local settings are installed by trusted backend code, not tools.
CREATE FUNCTION evidence.document_allowed(cases text[], project uuid, owner uuid, session uuid, global_doc boolean)
RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
 SELECT nullif(current_setting('evidence.user_id',true),'') IS NOT NULL
 AND (project IS NULL OR project::text=current_setting('evidence.project_id',true))
 AND CASE WHEN session IS NOT NULL THEN
   owner::text=current_setting('evidence.user_id',true)
   AND session::text=current_setting('evidence.session_id',true)
 ELSE (global_doc AND current_setting('evidence.include_global',true)='true')
   OR (project IS NOT NULL AND cardinality(cases)=0 AND project::text=current_setting('evidence.project_id',true))
   OR (cardinality(cases)>0 AND cases <@ ARRAY(SELECT jsonb_array_elements_text(
     COALESCE(nullif(current_setting('evidence.case_ids',true),'')::jsonb,'[]'::jsonb))))
 END
$$;
CREATE FUNCTION evidence.visible_document(id uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT EXISTS(SELECT 1 FROM evidence.documents WHERE document_id=id)
$$;

ALTER TABLE evidence.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence.documents FORCE ROW LEVEL SECURITY;
CREATE POLICY document_read ON evidence.documents FOR SELECT TO muhafiz_evidence_reader
 USING(evidence.document_allowed(case_ids,project_id,owner_id,session_id,is_global));
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['spans','entities','assertions','coverage','legacy_candidates','extraction_runs','ingestion_jobs','concept_revisions'] LOOP
   EXECUTE format('ALTER TABLE evidence.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE evidence.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY scoped_read ON evidence.%I FOR SELECT TO muhafiz_evidence_reader USING (evidence.visible_document(document_id))',t);
 END LOOP;
END $$;
ALTER TABLE evidence.concepts ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence.concepts FORCE ROW LEVEL SECURITY;
CREATE POLICY concept_read ON evidence.concepts FOR SELECT TO muhafiz_evidence_reader USING (
 evidence.visible_document(origin_document_id)
 OR EXISTS(SELECT 1 FROM evidence.assertions a WHERE a.predicate_id=concept_id)
 OR EXISTS(SELECT 1 FROM evidence.entities e WHERE e.concept_id=concepts.concept_id)
);
ALTER TABLE evidence.identity_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence.identity_links FORCE ROW LEVEL SECURITY;
CREATE POLICY identity_read ON evidence.identity_links FOR SELECT TO muhafiz_evidence_reader USING (
 EXISTS(SELECT 1 FROM evidence.entities WHERE entity_id=left_id)
 AND EXISTS(SELECT 1 FROM evidence.entities WHERE entity_id=right_id));
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['runs','conversations','artifacts','discovery_jobs'] LOOP
  EXECUTE format('ALTER TABLE evidence.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE evidence.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY owner_access ON evidence.%I TO muhafiz_evidence_reader USING (user_id::text=current_setting(''evidence.user_id'',true)) WITH CHECK (user_id::text=current_setting(''evidence.user_id'',true))',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['documents','spans','entities','assertions','coverage','concepts','identity_links','runs','conversations','artifacts','discovery_jobs','legacy_candidates','extraction_runs','ingestion_jobs','concept_revisions'] LOOP
  EXECUTE format('CREATE POLICY ingestion_access ON evidence.%I TO muhafiz_evidence_ingestor USING (true) WITH CHECK (true)',t);
 END LOOP;
END $$;

-- Independent projections, no caller-supplied graph query language.
CREATE VIEW evidence.relationships WITH (security_invoker=true) AS
 SELECT assertion_id,subject_id,predicate_id,object_entity_id,document_id,span_id,event_time,valid_from,valid_to
 FROM evidence.assertions WHERE status='validated' AND object_entity_id IS NOT NULL AND retracted_at IS NULL;
GRANT USAGE ON SCHEMA evidence TO muhafiz_evidence_reader,muhafiz_evidence_ingestor;
REVOKE ALL ON ALL TABLES IN SCHEMA evidence FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA evidence FROM PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA evidence TO muhafiz_evidence_reader;
GRANT INSERT,UPDATE ON evidence.runs,evidence.conversations,evidence.artifacts,evidence.discovery_jobs TO muhafiz_evidence_reader;
GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA evidence TO muhafiz_evidence_ingestor;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA evidence TO muhafiz_evidence_reader,muhafiz_evidence_ingestor;
COMMIT;
