# Phase 2 — Manual RLS Verification Procedure

This phase (Row-Level Security & Apache AGE isolation) could not be
verified end-to-end in the environment it was implemented in — no live
Postgres, Apache AGE, or GPU/model-server infra is available there. This
document is the procedure for whoever has real DB access to actually
confirm it. `tests/test_rls_integration.py` encodes the same four checks
as skipped-by-default pytest tests (`RUN_POSTGRES_TESTS=1` +
`TEST_DATABASE_URL` to run them for real) — this doc is the narrative
walkthrough plus the operational steps around them.

**Do this on a disposable/staging database first, not production.**
Migration 010 rewrites four existing RLS policies (`DROP POLICY` +
`CREATE POLICY` in the same statement) and adds a new one on `messages` —
low risk in isolation, but this is explicitly called out in
`solution.md` as "the single riskiest change in this plan," so treat the
first real run as a dry run.

## Prerequisites

1. A Postgres instance with the Apache AGE extension installed.
2. Every migration `001` through `010` applied, in order (`010` is new —
   `migrations/010_rls_null_case_fix.sql`).
3. **A non-superuser application role to connect as.** This is easy to
   get wrong: `FORCE ROW LEVEL SECURITY` (migrations 008/010) does not
   apply to a role with `BYPASSRLS` or `SUPERUSER` — the default
   `postgres` role has both, so testing against it will make every check
   below look like RLS isn't restricting anything, even when the policies
   are written correctly. Create a real role first:
   ```sql
   CREATE ROLE muhafiz_app LOGIN PASSWORD '...';
   GRANT SELECT, INSERT, UPDATE, DELETE ON documents, sessions, cases,
       pipeline_runs, messages TO muhafiz_app;
   -- plus whatever else the app's normal runtime role needs (case_assignments,
   -- audit_logs, etc. — these aren't RLS-protected, but the role still needs
   -- ordinary grants to read/write them).
   ```
   Connect as `muhafiz_app` for every check below, not `postgres`.

## Check 1 — general (no-case) chat session survives RLS

The direct regression test for the NULL-vs-NULL bug
(`issues.md`'s Critical "Postgres RLS policies' NULL-vs-NULL equality
silently breaks every non-case-scoped conversation" finding).

1. Start the app pointed at this database, with `muhafiz_app` as
   `DATABASE_URL`'s role.
2. Send a chat message with no active case selected (a "general" chat).
3. **Expected (post-fix):** the message sends successfully, the session
   row is created, and re-opening that same session later shows the
   message history intact.
4. **What it looked like before this phase:** the request would still
   appear to "work" from the UI's perspective in some cases, but the
   server log would show a swallowed exception around session
   creation/lookup (`orchestrator.py`'s `except Exception as exc:
   logger.error("Failed to ensure session row for...")`) on every single
   general-chat message — check the logs specifically for this even if
   the chat response itself looks fine, since the bug's failure mode is
   silent from the end-user's perspective.

## Check 2 — case-scoped rows are invisible outside their own case

1. Create two real cases (e.g. via `POST /api/cases`), `CASE-A` and
   `CASE-B`, both real rows your test user has access to.
2. Start a chat session scoped to `CASE-A`, send a message.
3. Directly query Postgres as `muhafiz_app` with
   `SET LOCAL app.rls_active = 'true'; SELECT set_config('app.case_id', 'CASE-B', true);`
   then `SELECT * FROM sessions WHERE case_id = 'CASE-A';` — **expected:
   zero rows.** Repeat with `app.case_id` set to `'CASE-A'` — **expected:
   the row is visible.**
4. Repeat the same probe against `messages` (new in this phase) and
   `pipeline_runs` — both should show the identical isolation.

## Check 3 — cross-case bypass is armed only after authorization, never before

The regression test for `issues.md`'s High "cross-case RLS bypass flag is
armed before its own role check" finding.

1. As a user with the `investigator` role (below `supervisor`), ask a
   question that the router classifies as cross-case (e.g. "has this
   phone number appeared in any other case?").
2. **Expected:** the request is denied (an abstention/safe response is
   returned; check the server log for the `PermissionError` /
   `"Unauthorized cross-case ... attempted"` warning).
3. **The actual regression check:** immediately send a SECOND, unrelated,
   case-scoped message in the same session, and confirm it still only
   returns that message's own case's data — it must not have inherited a
   still-armed cross-case bypass from the denied attempt a moment before.
   (This is hard to observe black-box; if you have log/metric access,
   confirm `current_cross_case` is never observed `True` anywhere in the
   denied request's lifetime — `tests/test_rls_integration.py::
   test_xgraph_permission_error_does_not_leave_bypass_armed_for_later_queries`
   asserts this directly against the real function, not just black-box.)
4. Now repeat as a `supervisor` (or higher) user — the same cross-case
   question should succeed, confirming the bypass still works for
   legitimately authorized callers, not just that it's now harder to
   reach.

## Check 4 — REST CRUD endpoints get a real RLS-level backstop

The regression test for `issues.md`'s Critical "Postgres RLS is never
activated for any REST CRUD endpoint" finding.

1. Pick any case-scoped endpoint, e.g. `GET /api/cases/{case_id}`.
2. As a user who is NOT assigned to `CASE-X`, confirm the app-layer check
   (`require_case_access`) still returns 403 as it did before this phase
   — that hasn't changed.
3. The actual new thing to confirm: connect directly to Postgres as
   `muhafiz_app`, run `SET LOCAL app.rls_active='true'; SELECT
   set_config('app.case_id', '', true);` (simulating "general" scope, as
   if the app-layer check had been skipped or had a bug) and attempt
   `SELECT * FROM cases WHERE case_id = 'CASE-X';` — **expected: zero
   rows**, even though nothing in this raw SQL session ever ran
   `require_case_access`. This is what "RLS is a real backstop, not just
   an app-layer check" means concretely — before this phase, this same
   probe would have returned the row, because `app.rls_active` was never
   set to `'true'` anywhere outside the chat pipeline.
4. Repeat for `sessions.py`/`attachments.py`/`admin.py`/`graph_review.py`
   — **these are intentionally NOT real per-case backstops** (see
   `src/auth/rls_context.py`'s module docstring for exactly why each one
   got the cross-case-bypass treatment instead of real case scoping).
   Confirm you understand which category each router falls into before
   concluding a gap is a regression — it may be the documented, deliberate
   scope of this phase rather than an oversight.

## What this phase does NOT cover (don't mistake these for regressions)

- **Apache AGE has no RLS at all** — case isolation for the graph is
  `src/graph/case_scope.py`'s chokepoint (a hygiene backstop against a
  future template dropping its case filter), not database-level
  defense-in-depth. See `docs/graph_schema.md`'s "Case isolation for the
  graph" section.
- **`sessions.py`/`attachments.py`/`admin.py`/`graph_review.py`/
  `projects.py`** deliberately run with the case-dimension bypassed (RLS
  armed, but not case-restrictive) — real protection there remains the
  existing application-layer ownership/role checks, unchanged by this
  phase. This was a design decision made during implementation (see the
  module's implementation report) after discovering the naive "derive
  case_id from path param, else general" design from the original plan
  would have either left these routers unprotected either way, or (for
  `admin.py` specifically) actively broken the platform-wide dashboards.
- **The graph-review queue's cross-case visibility** is unchanged and
  intentionally so — it's blocked on a product decision (`solution.md`
  §9.2), not a Phase 2 deliverable.

## Rollback

If migration 010's rewritten policies misbehave in production:

```sql
DROP POLICY IF EXISTS messages_isolation_policy ON messages;
ALTER TABLE messages DISABLE ROW LEVEL SECURITY;
DROP INDEX IF EXISTS idx_sessions_case_id;
-- then re-run migrations/008_rls_policies.sql's original four
-- CREATE POLICY statements (DROP POLICY the 010 versions first).
```

The application-code changes (`src/auth/rls_context.py`,
`src/database/postgres.py::get_session()`, the router wiring, the
`current_cross_case` reordering in `graph_retriever.py`/`xagg.py`, and
`src/graph/case_scope.py`) are a plain `git revert` of this module's
commit — no data migration involved on the code side.
