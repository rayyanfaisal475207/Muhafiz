-- Migration 032: muhafiz_app grants for tables added by migrations 016-030
-- that migration 015's original grant list never covered.
--
-- Found live, 2 Sep 2026, verifying the agent harness end-to-end on a
-- freshly-restarted instance: Global Search failed outright with
-- asyncpg.exceptions.InsufficientPrivilegeError: permission denied for
-- table community_runs. This is the exact "Known follow-up" gap already
-- documented (but not fixed) in SCENARIO_VERIFY_LOG.md: migration 015
-- granted muhafiz_app explicit per-table DML on the schema as it existed
-- at the time, but nine tables added afterward by migrations 016-030 were
-- never added to that list. On a machine with ad-hoc/manual grants this
-- is invisible; a clean restore from a --no-privileges dump (or a fresh
-- docker-compose volume, as here) exposes it, and it fails at RUNTIME —
-- when the specific feature touching that table is first used — not at
-- startup, so /health can report "ok" while an entire capability
-- (BM25 keyword retrieval, the identity/community graph layers) is
-- silently broken underneath it.
--
-- Confirmed missing (queried live against information_schema before this
-- migration): chunk_fulltext, community_membership, community_reports,
-- community_runs, entity_resolution_consistency_findings, identity_index,
-- ingestion_run_quality, pending_candidate_priority,
-- same_as_queue_snapshot — exactly nine tables, matching the count noted
-- in the "Known follow-up" section.
--
-- Same discipline as migration 015: explicit per-table grants, not
-- ALL TABLES / default privileges, so a future new table still requires
-- an explicit decision here rather than silently inheriting access.
--
-- Idempotent: safe to re-run (GRANT is not additive-error on a role that
-- already has the privilege).

GRANT SELECT, INSERT, UPDATE, DELETE ON
    chunk_fulltext,
    community_membership,
    community_reports,
    community_runs,
    entity_resolution_consistency_findings,
    identity_index,
    ingestion_run_quality,
    pending_candidate_priority,
    same_as_queue_snapshot
TO muhafiz_app;
