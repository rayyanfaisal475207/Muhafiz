BEGIN;
-- Preserve original evidence while denying all reader paths after revocation.
ALTER POLICY document_read ON evidence.documents TO muhafiz_evidence_reader
USING (evidence.document_allowed(case_ids,project_id,owner_id,session_id,is_global)
       AND NOT source_metadata @> '{"attachment_revoked":true}'::jsonb);
COMMIT;
