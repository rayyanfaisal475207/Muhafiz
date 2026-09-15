-- Migration 033: READ-ONLY AGE graph grants for the least-privilege
-- muhafiz_mcp_readonly role (migration 009), for Module 178's LLM-generated
-- graph-query fallback (src/pipeline/llm_query_fallback.py).
--
-- WHY. Module 178 lets the language model write ONE Cypher statement for a
-- cross-case question that no aggregate answers and no plan composes, and
-- runs it. A generated query is untrusted text: the keyword filter in
-- llm_query_fallback.py rejects CREATE/SET/DELETE/MERGE/REMOVE/DROP/CALL
-- before anything reaches the database, but a filter is a guess about the
-- text and a role grant is a fact about the connection. The query therefore
-- runs on muhafiz_mcp_readonly, which migration 009 left with SELECT on
-- exactly one relational table and NO access to the graph schemas at all --
-- so, until this migration, a generated query could not read the graph
-- either. This grants the role what a read needs and nothing a write does.
--
-- WHAT IS GRANTED. USAGE on ag_catalog (the agtype type and the cypher()
-- function live there) and on evidence_graph (every vertex/edge label is a
-- real table in this schema); SELECT on all tables in both. Deliberately
-- NOT: INSERT/UPDATE/DELETE on anything, TRUNCATE, any sequence privilege
-- (a write needs the label sequences; a read does not), evidence_graph_eval
-- (the evaluation graph is not a production surface), and nothing new in
-- public -- migration 009's per-table discipline for the relational schema
-- is unchanged and police_reference_data stays the only relational table
-- this role can see.
--
-- ALL TABLES + ALTER DEFAULT PRIVILEGES, SELECT only: the same reasoning as
-- migration 031's block comment -- AGE creates a label's table lazily at
-- first write, so a per-table list here would silently exclude every label
-- added after this file was written. The default-privileges clause covers
-- those future labels for SELECT and for nothing else.
--
-- The role's password and MCP_DATABASE_URL are migration 009's manual steps
-- and are unchanged. `LOAD 'age'` is superuser-only; the connection relies
-- on shared_preload_libraries = 'age' exactly as muhafiz_app does
-- (src/graph/age_client.py::_load_age()).
--
-- Verified live (Module 178 result, section 3): under this role a
-- MATCH ... RETURN runs; CREATE, SET, DELETE, MERGE and REMOVE each fail
-- with "permission denied for table ..." -- the write is refused by the
-- role even if the text filter had let it through.
--
-- Idempotent: safe to re-run.

GRANT USAGE ON SCHEMA ag_catalog TO muhafiz_mcp_readonly;
GRANT USAGE ON SCHEMA evidence_graph TO muhafiz_mcp_readonly;

GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO muhafiz_mcp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA evidence_graph TO muhafiz_mcp_readonly;

ALTER DEFAULT PRIVILEGES IN SCHEMA evidence_graph
    GRANT SELECT ON TABLES TO muhafiz_mcp_readonly;

-- Default privileges are per CREATING role: the clause above covers labels
-- the migration runner (postgres) creates; labels AGE creates lazily at
-- runtime are owned by the application's own connection role (muhafiz_app,
-- migration 015), so that role's defaults must carry the grant too, or a
-- label first written by the app is invisible to this role.
ALTER DEFAULT PRIVILEGES FOR ROLE muhafiz_app IN SCHEMA evidence_graph
    GRANT SELECT ON TABLES TO muhafiz_mcp_readonly;
