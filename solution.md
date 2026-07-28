# Muhafiz — Implementation Plan

**Status: PLAN ONLY. No code has been written, edited, or fixed in producing this document.**
**Source:** `issues.md` (125 findings, 2026-07-27 audit) plus the graph-contamination issue described separately by the user, which is now folded in as issue **[Critical] Apache AGE graph contains synthetic eval-harness fixtures permanently written into real cases** (already added to `issues.md` §3, confirmed here against live code).

---

## 0. How to read this plan

- Phases are ordered **dependency first, severity second** — see the rationale note at the top of each phase.
- Every module lists the exact `issues.md` finding titles it addresses, so nothing goes missing between audit and implementation.
- "Verification" is honest about what can and cannot be checked without live Postgres/Apache AGE/GPU infra. Where I could read code but not execute it, I say so.
- §9 at the bottom lists **disagreements** with `issues.md` — findings I think are wrong, overstated, or need a product decision rather than a code fix.
- §10 lists what I recommend **not** fixing right now, and why.
- §11 gives rough relative effort per phase.

---

## Phase 0 — Foundations: trust the tools before using them

**Why first:** every later phase will be judged by "does the test suite still pass" and "does the app still boot." Right now neither claim is reliable — CI is red, and a missing env var silently degrades the whole app instead of refusing to start. Fixing this first means every subsequent phase gets an honest signal.

### Module 0.1 — Resolve the one failing CI test

- **Issues addressed:** *The test suite currently has one failing, stale regression test on `main` (HEAD) — CI is red right now, not hypothetically.*
- **Files/functions:** `tests/test_orchestrator.py:700-731` (`test_rag_retry_exhausted_gemini_fallback_is_verified`), `src/pipeline/orchestrator.py:1409-1436` (retry-exhaustion branch).
- **Approach:** I read both sides. `orchestrator.py`'s own comment at lines 1412-1421 states plainly that the automatic Gemini web-search fallback on RAG retry-exhaustion was **deliberately removed** ("removed by design (scope change, not a bug fix)") in favor of abstaining (`_SAFE_RESPONSE`), because web search should only ever be user-invoked (`enable_web_search`) or router-classified (`WEB` route) — never a silent reactive fallback. The test still asserts the old behavior (a verified Gemini answer). **The code is correct; the test is stale.** Fix: delete `test_rag_retry_exhausted_gemini_fallback_is_verified` and replace it with a new regression test, e.g. `test_rag_retry_exhausted_abstains_without_web_fallback`, asserting: no `call_gemini_with_search` call occurs, no `citation_validator` event is emitted, and the streamed response equals `_SAFE_RESPONSE`. This preserves the *intent* of the original regression test (lock in intentional behavior) without asserting removed functionality.
- **Blast radius/risk:** None — test-only change. Zero production code touched.
- **Verification:** `python -m pytest tests/test_orchestrator.py -k retry_exhausted -q` must pass; then full suite (`python -m pytest tests/ --continue-on-collection-errors`) should report 0 failures.
- **Migration/infra needs:** None.
- **Rollback:** Revert the test file; trivial.

### Module 0.2 — Fail-fast startup configuration

- **Issues addressed:** *`JWT_SECRET_KEY` has an insecure hardcoded default and is never validated at startup*; *No enforcement that `DATABASE_URL` is actually configured — the app silently boots into a non-functional "legacy SQLite mode"*; *`validate_config()`'s scope is narrow*; *Cookie `Secure` flag fails open to insecure whenever `ENVIRONMENT` is unset or doesn't exactly match `"development"`*.
- **Files/functions:** `src/config.py` (`validate_config()` lines 155-189, `JWT_SECRET_KEY` line 193, `ENVIRONMENT` line 128, `AIR_GAP_MODE` line 101), `src/main.py:91-138` (startup sequence), `src/auth/routes.py:94,120` (`is_secure`).
- **Approach:**
  1. Add a `CRITICAL_ERRORS` category to `validate_config()`, separate from the existing soft `errors` (warnings): `JWT_SECRET_KEY == "your-secret-key-for-dev"` in anything other than `ENVIRONMENT == "development"`; `ENVIRONMENT` not in the enum `{"development", "staging", "production"}` (currently a free string compared exact-match against `"development"` — replace the `!=` check in `auth/routes.py` with `config.ENVIRONMENT == "production"` deployments explicitly opting into insecure cookies only via `"development"`, and validate the enum itself at startup).
  2. In `src/main.py`, if `is_postgres_configured()` is `False`, do **not** silently fall back to `init_db()`'s legacy SQLite schema — log a clear CRITICAL and refuse to serve traffic beyond `/health` (or exit, if a hard `REQUIRE_POSTGRES=true` flag is set, defaulting true). The legacy SQLite path predates the entire case/auth/RBAC model per the audit; keeping it as a silent fallback is the actual bug, not a feature to preserve.
  3. `validate_config()`'s new critical-error branch should be raised (`SystemExit` or a raised exception caught once in `main.py`'s startup) rather than only logged, when `ENVIRONMENT == "production"`. In development, keep it a warning so local onboarding isn't broken.
- **Blast radius/risk:** A misconfigured production deployment that's currently "working" (accidentally, with a public JWT secret) will now refuse to boot until fixed — this is the intended, correct behavior change, but it's a breaking change for any existing prod deployment that hasn't set these vars. Sequence this module's merge with an operational heads-up, not a silent deploy.
- **Verification:** New unit tests in `tests/test_config.py` (new file) asserting: `validate_config()` flags the default JWT secret when `ENVIRONMENT=production`; flags an invalid `ENVIRONMENT` value; startup raises when Postgres isn't configured and `REQUIRE_POSTGRES` isn't explicitly disabled. These are pure Python, no live infra needed.
- **Migration/infra needs:** None for code; ops must ensure `JWT_SECRET_KEY`, `DATABASE_URL`, `ENVIRONMENT=production` are actually set before this lands in any real deployment (see Module 0.3, which makes this discoverable).
- **Rollback:** Revert `validate_config()`/`main.py`/`auth/routes.py` changes; the old (unsafe) fail-open behavior returns immediately.

### Module 0.3 — `.env.example` and config-doc sync

- **Issues addressed:** *`.env.example` is missing roughly a dozen settings `src/config.py` actually reads, and several shared values have silently drifted to different defaults.*
- **Files/functions:** `.env.example` (79 lines) vs. `src/config.py` (198 lines).
- **Approach:** Add every variable `config.py` reads that `.env.example` omits (`JWT_SECRET_KEY`, `ENVIRONMENT`, `AIR_GAP_MODE`, `WEB_ALLOWED_DOMAINS`, `LOCAL_GEN_LLM_*`, `LOCAL_LLM_API_KEY`, `MODEL_SERVER_BASE_URL`, `EMBEDDINGS_URL`, `RERANKER_URL`, `DB_PATH`), and correct the drifted defaults (`EMBEDDING_PROVIDER=e5` not `gemini`; `GEMINI_MODEL=gemini-2.5-flash`; `LOCAL_LLM_MODEL` to match whatever Module 0.2 settles on as the documented model) so the template matches reality and Module 0.2's new fail-fast checks don't surprise a fresh clone.
- **Blast radius/risk:** None — documentation-only file.
- **Verification:** Manual diff of every `os.getenv(...)` call site in `config.py` against `.env.example` keys; a small script (throwaway, not committed) can grep both and diff the key sets.
- **Migration/infra needs:** None.
- **Rollback:** Trivial.

---

## Phase 1 — Independent Critical security fixes (no architectural dependencies)

**Why here, before the RLS redesign:** these are all Critical-severity, but each is a narrow, self-contained fix with no dependency on the access-control redesign in Phase 2. Landing them now closes several fast attack surfaces while the harder RLS design work (which needs more careful review) is still in progress.

### Module 1.1 — XLSX formula/CSV injection

- **Issues addressed:** *Formula/CSV injection (CWE-1236) in generated XLSX exports via unsanitized evidence/LLM-supplied cell content.*
- **Files/functions:** `src/generation/xlsx_builder.py:22-29` (`build_xlsx`); contrast the already-correct pattern in `src/generation/pdf_builder.py`.
- **Approach:** Before writing any cell value, if it's a string and starts with `=`, `+`, `-`, or `@` (or a tab/CR that could re-trigger the same after leading whitespace), prefix it with a single leading `'` (apostrophe) — Excel's standard "force text" escape — or prepend a single quote only where OWASP's CWE-1236 guidance recommends (leading apostrophe is the standard mitigation; do this centrally in one helper, not per call site, so future export formats reuse it). Apply it uniformly to every cell derived from evidence/LLM content, not just specific columns, since the audit confirmed the payload is the LLM's `final_response`, itself built from case-evidence.
- **Blast radius/risk:** Low — purely additive escaping of specific leading characters. Risk is a legitimate cell value that happens to start with `=`/`+`/`-`/`@` (e.g., a negative number formatted as text) rendering with a visible leading apostrophe in Excel; acceptable and matches industry-standard mitigation.
- **Verification:** New unit test in `tests/test_xlsx_builder.py` (new or extended file): build an XLSX from a payload containing `=HYPERLINK("http://x","y")`, open the resulting file with `openpyxl` (already a likely dependency, or `pandas.read_excel`), assert the cell's raw string is stored with a leading `'` / as text, not as a formula. Fully testable without live infra.
- **Migration/infra needs:** None.
- **Rollback:** Revert the sanitization helper; trivial, no data model change.

### Module 1.2 — MCP least-privilege + dead-code + injection cleanup

- **Issues addressed:** *MCP Postgres server connects with the same superuser DB role as the entire application — no least-privilege scoping for the SQL route*; *Admin `mcp-demo` endpoint builds SQL via string concatenation against a superuser database connection*; *`src/mcp/config.py` + `src/mcp/server.js` are a second, entirely unreferenced MCP-Postgres server implementation*; *`mcp-servers/package.json` declares three unused npm dependencies and omits the one package actually spawned at runtime*; *`execute_query`'s `params` argument is declared but never used, actively misleading given the adjacent injection-prone code path.*
- **Files/functions:** `src/mcp/client.py:12-47` (`execute_query`, `node_db_url`), `src/mcp/config.py` (whole file, dead), `src/mcp/server.js` (whole file, dead), `mcp-servers/package.json`, `src/api/admin.py:397-416` (`mcp_demo`).
- **Approach:**
  1. Provision a least-privilege Postgres role (e.g. `muhafiz_mcp_readonly`) with `SELECT`-only grants on `police_reference_data` (the table the SQL route is actually meant to expose) — a new migration `009_mcp_readonly_role.sql`. Point `src/mcp/client.py`'s `node_db_url` at this role's connection string (a new `MCP_DATABASE_URL` env var, falling back to `DATABASE_URL` with a startup warning if unset, so this doesn't hard-break existing deployments on day one).
  2. Delete `src/mcp/config.py` and `src/mcp/server.js` outright (confirmed zero live references) rather than leaving them as a "second implementation someone might trust."
  3. Trim `mcp-servers/package.json` to declare only `@modelcontextprotocol/server-postgres` (the package actually spawned via `npx -y` in `client.py`) with a pinned version, and add a lockfile so it's not fetched ad hoc from the registry on every invocation.
  4. Rewrite `mcp_demo` in `src/api/admin.py` to use SQLAlchemy's parameterized `.ilike()` (the same safe pattern `direct_backend.py::query_police_reference_data` already uses) instead of manual quote-doubling string concatenation.
  5. Either implement `execute_query`'s declared-but-unused `params` argument for real parameter binding, or remove it from the signature — whichever happens, it must stop being a dead parameter that invites someone to believe a future `mcp_demo` fix is parameterized when it silently isn't.
- **Blast radius/risk:** Medium. Repointing the MCP connection to a new role can break the SQL route if the new role's grants are too narrow (e.g., missing a table the route legitimately needs) or if `MCP_DATABASE_URL` isn't set in an environment that previously worked by accident. Sequence: create the role and env var with a safe fallback to the superuser URL *with a loud warning*, verify the SQL route still works end-to-end, then remove the fallback in a follow-up once confirmed.
- **Verification:** Cannot fully verify the least-privilege role grants without a live Postgres instance to run `GRANT`/`REVOKE` against and confirm `execute_query` still succeeds for legitimate reference-data queries and fails for e.g. `DROP TABLE users`. Write the migration and a manual test script (`scripts/verify_mcp_role.py`, run against a real DB) that asserts the new role can `SELECT` from `police_reference_data` and gets `permission denied` against `users`/`audit_logs`. The dead-code deletion and `mcp_demo` parameterization ARE fully testable without infra (unit test asserting the built SQL uses bind params, not string interpolation).
- **Migration/infra needs:** New Postgres role + migration `009_mcp_readonly_role.sql`; new `MCP_DATABASE_URL` env var (add to `.env.example` per Module 0.3).
- **Rollback:** Point `MCP_DATABASE_URL` back at the superuser `DATABASE_URL` (config-only revert); the dead-code deletion is a plain file revert if for some reason `server.js` turns out to be needed (it shouldn't be — zero references confirmed by repo-wide grep).

### Module 1.3 — Auth/registration hardening

- **Issues addressed:** *No password strength or minimum-length validation on registration*; *No rate limiting on any endpoint besides `/api/auth/register` and `/api/auth/login`*; *`CORS_ORIGINS` is hardcoded rather than environment-configurable.*
- **Files/functions:** `src/auth/routes.py:27-30` (`UserCreate.password`), `:47-49,74-76` (existing rate limits), `src/config.py:195` (`CORS_ORIGINS`).
- **Approach:** Add a Pydantic validator on `UserCreate.password` requiring a minimum length (12+, matching common guidance for a system with no MFA) — no complexity-class requirements beyond length, per current NIST guidance, to avoid encouraging predictable substitutions. Extend `@limiter.limit(...)` to `/api/chat`, `/api/admin/kb/upload`, `/api/attachments`, case creation, and case-assignment endpoints, with per-endpoint-appropriate limits (chat can be more generous than upload). Change `CORS_ORIGINS` to `os.getenv("CORS_ORIGINS", "http://localhost:5173,...").split(",")`.
- **Blast radius/risk:** Low. Rate limits sized too aggressively could throttle legitimate heavy chat use — pick generous defaults (e.g. 60/min for chat) and make them env-overridable. Password minimum length could reject existing short passwords on next password-change flow only (not retroactively — do not force-expire existing sessions/passwords as part of this fix, that's out of scope).
- **Verification:** Unit tests: `UserCreate(password="short")` raises a validation error; `UserCreate(password="a-real-passphrase-1234")` succeeds. Rate-limit tests already exist as a pattern in the codebase for the two decorated endpoints — mirror that pattern for the newly-decorated ones (`pytest` + `slowapi`'s test helpers, no live infra).
- **Migration/infra needs:** `CORS_ORIGINS` needs to be added to `.env.example` (Module 0.3).
- **Rollback:** Revert the validator/decorators/env read; independent, no data implications.

---

## Phase 2 — Row-Level Security & Apache AGE isolation: the target design

**Why here, as one coherent module:** `issues.md` is explicit that four findings — RLS never activated outside chat; the NULL-vs-NULL bug; the graph having no RLS equivalent; the cross-case bypass armed before its own role check — are one broken security layer, not four bugs. I designed the target state first, then the migration path to it, per the request. This phase has no code dependency on Phase 1, but it's sequenced after it because it's the largest, riskiest single change in this plan and deserves the most isolated review window.

### 2.0 — What's broken, precisely (confirmed by reading the code, not just the audit)

- `src/database/postgres.py:46-48` declares three `contextvars`: `current_case_id`, `current_cross_case`, `current_rls_active`. They are read in exactly one place — `get_session()` at lines 99-116 — and **set** in exactly one place in the entire codebase: `src/pipeline/orchestrator.py:187,189,429`. I confirmed this with a repo-wide grep; there is no second setter anywhere, including `direct_backend.py`, whose ~70 methods (confirmed: every one goes through `async with get_session() as db:`) never touch these vars.
- `migrations/008_rls_policies.sql` policies read `current_setting('app.case_id', true)`. For a general (no-case) chat, `current_case_id.set(case_id)` in `orchestrator.py:188-189` only fires `if case_id:` — so `app.case_id` is never set, `current_setting(...)` returns SQL NULL, and `case_id = NULL` is NULL (not TRUE) in three-valued logic. Every `sessions`/`cases`/`pipeline_runs` row with a NULL `case_id` becomes invisible under RLS, and since these are `FOR ALL` policies with no separate `WITH CHECK`, **inserting** such a row is rejected too.
- `messages` (the table holding actual chat content) has no RLS policy at all — only its parent `sessions` does.
- Apache AGE's vertex/edge labels are real Postgres tables (confirmed: `migrations/005_age_graph.sql` uses `create_vlabel`/`create_elabel`, which are ordinary catalog-backed tables under `ag_catalog`), but migration 008 never mentions them — no RLS, no policy, nothing. Case-scoping for the graph is entirely up to each of the ~15 hand-written Cypher templates individually remembering to filter by `case_id`.
- `orchestrator.py:428-429` sets `current_cross_case.set(True)` the instant the router classifies a query as `cross_case`/`XGRAPH`/`XAGG` — **before** the actual role gate, which lives inside `retrieve_graph()`/`run_aggregate()` and is only reached later (confirmed: `retrieve_graph(...)` call at line 995, wrapped in the `try` at 994-1100; a `PermissionError` from the role check is caught at the `except Exception as e:` on line 1094, which does **not** reset `current_cross_case`). Any `asyncio.create_task` spawned later in the same request (e.g. `update_project_memory` at line 1405, or the background conflict-detection task in ingestion) snapshots this still-armed context.

### Module 2.1 — Target design

The design principle: **RLS context (`case_id`, `cross_case`, `rls_active`) must be derived once, at the single chokepoint every DB-backed request already passes through (`get_session()`'s caller), from the authenticated user + the resource being accessed — never left to each pipeline or route handler to remember to set.**

1. **Move context-setting out of `orchestrator.py` and into a request-scoped dependency.** Add a FastAPI dependency (e.g. `src/auth/rls_context.py::apply_rls_context`) that every authenticated router depends on (via a shared `APIRouter(dependencies=[...])` or a dependency added to the existing `get_current_user` chain). It sets `current_rls_active.set(True)` unconditionally for every authenticated request (not just chat), and derives `case_id`/`cross_case` from the resolved route:
   - For endpoints scoped to one `case_id` path/query param (`cases.py`, `case_assignments.py`, `graph_review.py`'s per-case actions, chat's `case_id`), set `current_case_id` to that value.
   - For a **general** (no case) chat or a **general** (no case) REST call, set a sentinel that the fixed RLS policy (below) recognizes as "no case scope, general access" — NOT Postgres NULL compared against NULL. Concretely: change the policy predicate from `case_id = current_setting('app.case_id', true)` to `current_setting('app.case_id', true) = '' AND case_id IS NULL OR case_id = current_setting('app.case_id', true)`, and always `SET LOCAL app.case_id = ''` (empty string, never leave it unset) when there is no case — this makes the "no case" state an explicit, comparable value instead of relying on SQL NULL semantics. This is the direct fix for the NULL-vs-NULL bug, generalized to every table, not just patched in `orchestrator.py`.
   - `current_cross_case` is set **only after** the caller's role/authorization for cross-case access has already been confirmed — see Module 2.2 for the orchestrator-side reordering this requires.
2. **Cover `messages`.** Add an RLS policy on `messages` that mirrors `pipeline_runs`' pattern: `session_id IN (SELECT session_id FROM sessions WHERE <same case predicate>)`. This is a subquery-per-row-check; see the performance note in Blast Radius below.
3. **AGE has no native RLS**, and per `age_client.py`'s own docstring, the Cypher query text is always a fixed template — parameters are the only bindable value — so a Postgres RLS policy on AGE's underlying catalog tables isn't practically usable the way it is for `documents`/`sessions` (AGE manages those tables itself and matching its internal row shape to a case-scoping predicate is not a supported, documented AGE interface). Instead, build a **single enforcement point analogous to RLS**: every Cypher template that should be case-scoped must go through a new helper — `src/graph/case_scope.py::scoped_cypher(cypher, case_id, cross_case)` — that (a) asserts every template passed through it contains a `$case_id` placeholder actually referenced in its `WHERE`/pattern, failing loudly (raise, not log) if a template is registered without one, and (b) is the only sanctioned way `entity_resolution.py`/`graph_retriever.py`/`versioning.py` issue a case-scoped query. This converts "15 templates that individually happen to remember" into "one chokepoint that refuses to run an unscoped template," which is the closest structural equivalent to RLS that AGE's actual capabilities allow. It is **not** database-level defense-in-depth (a bug in `case_scope.py` itself is still a single point of failure) — that limitation should be stated plainly in `docs/graph_schema.md`, not implied to be solved.

### Module 2.2 — Migration path

1. New migration `010_rls_null_case_fix.sql`: rewrite `documents_isolation_policy`, `cases_isolation_policy`, `sessions_isolation_policy`, `pipeline_runs_isolation_policy` to use the explicit-empty-string comparison from 2.1 instead of relying on NULL semantics; add `messages_isolation_policy`.
2. `src/database/postgres.py::get_session()`: always `SET LOCAL app.case_id = ''` when there's no case (never leave it unset), so the policy's `current_setting(...) = ''` branch is reliable rather than depending on whether `set_config` was called at all this transaction.
3. New `src/auth/rls_context.py` dependency (2.1.1); wire it into `src/main.py`'s router registration for every router currently missing it (`cases.py`, `sessions.py`, `attachments.py`, `admin.py`, `case_assignments.py`, `graph_review.py`, `projects.py`) and into the chat endpoint (replacing, not duplicating, `orchestrator.py`'s current direct `.set()` calls — `orchestrator.py` should stop setting these vars itself once the dependency does it upstream of `process_query()`).
4. `orchestrator.py:428-429`: move `current_cross_case.set(True)` from immediately after router classification to immediately after the role check inside `retrieve_graph()`/`run_aggregate()` succeeds (i.e., pass a callback/flag the role check sets, or restructure so the bypass is armed by the function that just confirmed the caller is allowed to use it, not by the router's classification). On the `except PermissionError` path, `current_cross_case` must never have been set in the first place (not "set then reset" — reordering removes the window entirely, which is safer than a `finally: current_cross_case.set(False)` that could itself be skipped by an unexpected exception path).
5. `src/graph/case_scope.py` (new, 2.1.3) + register every existing Cypher template in `entity_resolution.py`, `graph_retriever.py`, `versioning.py`, `graph_review.py` through it. This is a mechanical pass, not a logic change, since the audit already confirmed current templates are correctly scoped — the point is making future drift impossible to ship silently.

### Blast radius/risk (whole phase)

- **This is the single riskiest change in this plan.** It changes what every authenticated request is allowed to see at the database level. Sequencing: land Module 2.1's policy rewrite and `get_session()` change first, behind a feature flag if the team has one, or in a maintenance window with `pytest` run against a real Postgres instance before merge — this cannot be safely reasoned about from source alone (see Verification).
- The `messages` subquery policy adds a per-row correlated subquery to every `messages` read; if `sessions` grows large and lacks an index on `(session_id, case_id)`, this could be a real latency regression. Add that index in the same migration.
- Moving `current_cross_case` activation later in `orchestrator.py` requires care: `retrieve_graph()`/`run_aggregate()` currently raise `PermissionError` as their *authorization* signal — the reordering needs the bypass-arming code to run in the narrow window between "role check passed" and "the actual cross-case Postgres/AGE queries execute," not accidentally after them (which would defeat the bypass's purpose) or with a race if any of this becomes concurrent later.
- Any code path that calls `process_query()` directly (bypassing `chat_endpoint`) — the audit specifically flags this as an existing gap — must also go through the new dependency or set context itself; document this requirement prominently in `orchestrator.py`'s docstring.

### Verification

- **Cannot be verified without a live Postgres instance with RLS actually enabled** (none available in this environment, per the audit's own repeated caveat) — the NULL-vs-NULL fix, the `messages` policy, and the cross-case reordering are all "correct by SQL semantics and code trace" but need to be run against real Postgres to confirm no policy typo breaks a previously-working query.
- Concrete test plan for whoever has DB access: (1) a general (no `case_id`) chat session — send a message, confirm the session row is created and re-readable, confirm no swallowed exception in logs; (2) a case-scoped chat — confirm cross-case rows are invisible; (3) an XGRAPH/XAGG query from a role that should be denied — confirm `PermissionError` fires and a *subsequent, unrelated* query in the same request cannot see cross-case data (this directly tests the reordering fix); (4) every REST CRUD endpoint (`cases.py` etc.) — confirm a user without case access gets a real 403/empty result at the RLS level even if the application-layer check were hypothetically removed (this is the actual regression test for "RLS never activated outside chat").
- Write these as `tests/test_rls_integration.py`, marked `@pytest.mark.requires_postgres`, skipped in the default CI run (which apparently has no live Postgres — confirm this from `.github/workflows/ci.yml` during implementation) but runnable by anyone with a real instance.

### Migration/infra needs

- New SQL migration `010_rls_null_case_fix.sql` (schema change, requires `scripts/apply_migration.py` or equivalent to run against every environment, including any already-deployed instance).
- No data backfill needed — this changes policy predicates and context-setting code, not stored data.

### Rollback

- The migration should be additive/replacing policies (`DROP POLICY ... ; CREATE POLICY ...` in the same file) so a rollback migration (`DROP POLICY` back to 008's originals) is straightforward if the new predicates misbehave in production. The `current_cross_case` reordering and the new `rls_context.py` dependency are pure application code — revert the commit.

---

## Phase 3 — Graph contamination: eval/production isolation and purge

**Why here, right after the RLS/AGE design:** the fix depends on the same architectural question Phase 2 just answered — "how do we give AGE a real isolation boundary" — and reuses the `graph` parameter `age_client.execute_cypher()` already exposes (confirmed: `execute_cypher(..., graph: str = GRAPH_NAME)` — the graph name is a real, already-plumbed parameter, just never varied from the one hardcoded `"evidence_graph"` default).

### 3.0 — A correction to the user's own description of this issue

Reading `scripts/eval_entity_resolution.py` directly (not just `issues.md`'s description), the problem is **worse than "synthetic fixtures coexist with real data written into real cases."** Line 184 of the script runs, unconditionally, on every invocation:

```python
await age_client.execute_cypher("MATCH (n) DETACH DELETE n", columns=["result"])
```

This is a **full wipe of every node and edge in the entire `evidence_graph`** — not a scoped delete of prior eval fixtures. If this script were ever run against the same graph that also holds real ingested case data, it would delete **all real graph data**, then repopulate the graph with bare `Case` nodes (`{"case_id": ...}`, no other fields) and the synthetic fixtures. The fact that real, fully-populated entities coexist with `EVAL-*` fixtures today means either (a) the script has never actually been run against this specific graph instance since real ingestion started, and the contamination dates from a run that happened before or between real ingestion batches, or (b) it's been run multiple times and real data was re-ingested after each wipe. Either way, **this script is a live landmine, not just a source of stale contamination** — anyone running it today "just to check resolution quality" destroys the production graph. I'm flagging this because it changes the fix's priority and shape: isolating the eval harness from production data is not optional cleanup, it's closing a one-command way to destroy all case intelligence data. The rest of this section's design accounts for this.

### Module 3.1 — Eval harness isolation

- **Issues addressed:** *The Apache AGE graph contains synthetic eval-harness test fixtures permanently written into real cases, indistinguishable from real evidence at the entity level* (plus the wipe-blast-radius correction above, which I'm treating as part of the same finding, not a new one).
- **Files/functions:** `scripts/eval_entity_resolution.py` (whole file), `src/graph/age_client.py:59,136-192` (`GRAPH_NAME`, `execute_cypher`), `src/graph/versioning.py` (`write_node`/`write_edge` — currently no `graph` parameter), `src/graph/entity_resolution.py::resolve_and_write` (currently no `graph` parameter), new migration for a second AGE graph.
- **Approach:**
  1. New migration `011_age_eval_graph.sql`, mirroring `005_age_graph.sql`'s pattern exactly: `create_graph('evidence_graph_eval')` plus the same vlabel/elabel pre-creation loop, for a **second, physically separate AGE graph** dedicated to eval/dev fixtures.
  2. Add a `graph: str = age_client.GRAPH_NAME` parameter threaded through `versioning.write_node`, `versioning.write_edge`, `versioning.get_edge`, and `entity_resolution.resolve_and_write` (all of which currently call `age_client.execute_cypher()` without ever overriding its default) — every one of these already only takes a Cypher template + params, so adding a pass-through `graph` argument is a small, mechanical signature change, not a rewrite.
  3. `scripts/eval_entity_resolution.py` sets a module-level `EVAL_GRAPH = "evidence_graph_eval"` and passes `graph=EVAL_GRAPH` on every `versioning`/`entity_resolution` call it makes (roughly a dozen call sites in `resolve_roster`/`evaluate`). The destructive `MATCH (n) DETACH DELETE n` wipe also gets `graph=EVAL_GRAPH` explicitly — it now only ever wipes the eval graph, never `evidence_graph`.
  4. Additionally, as a second, independent guard (defense in depth, matching the spirit of this whole audit): add a hard runtime check at the top of `evaluate()` — refuse to run at all unless `graph=EVAL_GRAPH` resolves to a graph name containing `eval` (a literal string check, deliberately simple/unmissable) — so a future edit that accidentally removes the `graph=` argument from one call site fails loudly instead of silently falling back to the production default.
  5. `eval_entity_resolution.py`'s synthetic `case_id` values should also stop reusing real `CASE-002..CASE-020`/`CASE-DRY-001` — switch the roster's case references to clearly synthetic values (e.g. `EVAL-CASE-014` instead of `CASE-014`) if/when the roster CSV (`data/memory/entity_roster.csv`) is regenerated. This is optional given the graph-namespace fix already provides full isolation, but it removes the last cosmetic overlap with real case IDs and makes any accidental cross-graph query immediately obvious by ID alone. Flagging as optional, not required, since it touches a data file rather than code and isn't necessary for correctness once (1)-(4) land.
- **Blast radius/risk:** Low for the code change itself (additive parameter, defaulted to current behavior for every *production* call site — nothing in ingestion, entity resolution at query time, or graph retrieval passes a non-default `graph`, so production behavior is unchanged). The risk is entirely in **not** landing this before anyone runs the eval script again — treat this as urgent within the plan even though it's architecturally "just" an isolation fix.
- **Verification:** Fully testable with a live Postgres+AGE instance, not without one: create both graphs, run `eval_entity_resolution.py`, confirm (a) `evidence_graph` node/edge counts are unchanged before/after the run, (b) `evidence_graph_eval` contains the fixtures, (c) the hard runtime guard actually raises if `graph=` is (hypothetically) omitted from a call site during a future edit. Without live infra, the parameter-threading itself can be unit-tested by mocking `age_client.execute_cypher` and asserting every call from the eval script passes `graph="evidence_graph_eval"`.
- **Migration/infra needs:** Migration `011_age_eval_graph.sql` (new AGE graph + labels — same one-time setup cost as the original `005`).
- **Rollback:** Drop the `evidence_graph_eval` graph and revert the parameter-threading commit; production code paths are untouched since they never pass `graph=` and keep defaulting to `evidence_graph`.

### Module 3.2 — Purge existing contamination

- **Issues addressed:** same finding as 3.1 (the cleanup half).
- **Files/functions:** new one-off script `scripts/purge_eval_contamination.py`.
- **Approach:**
  1. **Reconcile the 33-vs-26 `Person` discrepancy first**, before deleting anything. Run both counting queries the two audit passes apparently used (my best reconstruction, to be confirmed against the live graph): `MATCH (p:Person) WHERE p.source_chunk_id IS NULL RETURN count(p)` vs. `MATCH (p:Person) WHERE p.source_doc_id STARTS WITH 'EVAL-' RETURN count(p)`. If these disagree, it means either (a) some real `Person` nodes also lack a `source_chunk_id` for an unrelated reason (worth knowing regardless — it would mean the "no `source_chunk_id` ⇒ never citable" safety property the audit relies on for "this can't leak into chat answers" has other, non-eval causes too, which is itself worth a follow-up), or (b) some `EVAL-*` fixtures do have a `source_chunk_id` for some reason. **Do not delete anything until this is understood** — the purge script's identification query must be the `source_doc_id` prefix match (`EVAL-P-*`, `EVAL-NV-*`, `EVAL-CP-*`, `EVAL-DRY-*`, and the generic `EVAL-` prefix the script actually generates: `f"EVAL-{row['entity_id']}-{case_id}-{i}"`), which is a precise, deliberate marker — not the `source_chunk_id IS NULL` proxy, which is what produced the discrepancy in the first place.
  2. The purge script deletes, in order (respecting edge-then-node ordering so nothing is orphaned mid-delete): all edges where either endpoint's `source_doc_id` starts with `EVAL-`, or the edge's own `source_doc_id` starts with `EVAL-`; then all nodes (across every label — `Document`, `Person`, `Vehicle`, `PhoneNumber`, `Organization`, `Address`, and any others the reconciliation step turns up) whose `source_doc_id` starts with `EVAL-`. Do **not** delete the `Case` nodes the fixtures were attached to (`CASE-002`..`CASE-020`) — those are real cases and must survive; only their spurious `BELONGS_TO_CASE`/`SAME_AS` edges to fixture entities are removed.
  3. Run it as a dry-run first (`MATCH ... RETURN count(*)`, no `DELETE`) and print the exact counts against the audit's numbers (72 `Document`, 26-or-33 `Person`, 8 `Vehicle`, 11 `PhoneNumber`, 6 `Organization`, 10 `Address`, 144 `BELONGS_TO_CASE`, 88 `SAME_AS`) before doing the real delete, so any mismatch from the audit's snapshot (data may have changed since 2026-07-27) is caught before an irreversible write.
- **Blast radius/risk:** High if the identification query is wrong (real evidence could be deleted). This is why the reconciliation step is a hard prerequisite, not a nice-to-have, and why the script must dry-run first. Low risk to real data once the prefix-based query is confirmed correct, since `EVAL-` is a distinctive, deliberately-chosen marker with no plausible collision against real ingested `source_doc_id` values (real ones are actual filenames/paths).
- **Verification:** Cannot be verified without a live graph to run the dry-run/reconciliation queries against. Once run, verification is: re-run the audit's original counting queries and confirm zero `EVAL-*`-prefixed nodes/edges remain, and spot-check a handful of the 18 real cases (`CASE-002`..`CASE-020`) to confirm their genuinely-real entities (ones with a populated `source_chunk_id`) are untouched.
- **Migration/infra needs:** One-time live-graph script run, not a schema migration. Take a full AGE/Postgres backup immediately before running it (standard practice for any irreversible bulk delete, doubly so here given Module 3.0's discovery that this general class of script can wipe the whole graph).
- **Rollback:** Only via restoring the pre-purge backup — Cypher `DELETE` is not otherwise reversible. This is exactly why the dry-run + backup-first sequencing above is non-negotiable, not a suggestion.

---

## Phase 4 — Ingestion & retrieval data integrity

**Why here:** independent of the RLS/graph work, but several of these Critical/High findings (case-scoped retrieval unreachable, doc_id collisions) share the same root cause — `case_id`/`project_id`/`is_global` handling drifted across `ingestion/document.py`, `retrieval/vector_store.py`, and `pipeline/orchestrator.py` without a single source of truth — so they're grouped into one phase and, within it, one shared design decision.

### Module 4.1 — Case-scoped ID generation and retrieval-filter consistency

- **Issues addressed:** *`doc_id`/chunk-id collisions across *different cases* silently overwrite one case's evidence with another's in Chroma*; *Postgres `insert_documents` uses `ON CONFLICT (doc_id) DO NOTHING`, causing permanent cross-store metadata divergence on re-ingest under a different case*; *Case-scoped retrieval can be structurally unreachable for evidence with no `project_id` set*.
- **Files/functions:** `src/ingestion/document.py:53-68` (`Document._generate_id`), `src/ingestion/chunker.py:169-188` (chunk id), `src/data_gateway/direct_backend.py:612-625` (`insert_documents`), `src/retrieval/vector_store.py:207-272` (`_build_where`), `src/pipeline/orchestrator.py:872-874,1248-1250` (`where_clause` construction).
- **Approach:**
  1. **`_generate_id()`**: add `case_id`/`project_id` into the hash seed: `seed = f"{case_id or project_id or 'global'}::{source}::{page}::{text[:200]}"`. This directly fixes the cross-case collision — two cases ingesting `scan001.pdf` now hash to different IDs. Chunk IDs (`f"{doc_id}_c{i}"`) automatically inherit the fix since they derive from `doc_id`.
  2. **`insert_documents`'s `ON CONFLICT (doc_id) DO NOTHING`**: change to `ON CONFLICT (doc_id) DO UPDATE SET case_id = EXCLUDED.case_id, project_id = EXCLUDED.project_id, is_global = EXCLUDED.is_global, doc_type = EXCLUDED.doc_type` so Postgres and Chroma (which already unconditionally overwrites via `upsert()`) agree on which case a re-ingested `doc_id` belongs to, instead of Postgres silently keeping stale ownership forever. With fix (1) in place, a same-`doc_id` conflict now only occurs for a genuine re-ingest of the *same* document under the *same* case (the intended case-preserving hash key), so "last write wins" is the correct, intended behavior here — not a cross-case leak anymore.
  3. **Retrieval filter unreachability**: replace both `where_clause = {"project_id": project_id} if project_id else {"is_global": True}` call sites with logic that reflects what's actually documented: a case-scoped query should filter on `case_id` alone (when present), only falling back to `is_global: True` when there is genuinely no `project_id` **and** no `case_id` (a true "general knowledge base" query). Concretely: `where_clause = {}`; `if project_id: where_clause["project_id"] = project_id`; `if case_id: where_clause["case_id"] = case_id`; `if not project_id and not case_id: where_clause["is_global"] = True`. This removes the incorrect unconditional `is_global: True` ANDed against a real case_id.
- **Blast radius/risk:** **This changes existing `doc_id` values for all future ingests** — any code or data that stores/compares `doc_id` values by exact string outside the write path itself (I did not find any in the audited scope, but this needs a repo-wide grep during implementation, not just my read) would break. Existing already-ingested documents keep their old (collision-prone) IDs unless explicitly re-ingested — this fix prevents *new* collisions, it does not retroactively fix documents already silently overwritten. A companion one-off audit query (`scripts/find_doc_id_collisions.py`) should be run against the live Postgres+Chroma data to find any `doc_id` that currently has multiple distinct `case_id`s in its Chroma chunk history (not directly recoverable from Postgres alone, since Chroma's history isn't versioned) — flag these to a human for manual investigation/re-ingestion, since automatically guessing which case's data is "correct" isn't safe.
- **Verification:** New unit tests in `tests/test_ingestion_document.py` (new or extended): two `Document` objects with identical filename/page/text but different `case_id` produce different `doc_id`s. New test for `_build_where`: a call with `case_id="CASE-1"` and no `project_id` produces `{"case_id": "CASE-1"}`, not an `is_global` AND. Fully testable without live infra. The `ON CONFLICT DO UPDATE` change needs a live Postgres to verify the actual SQL behavior (`INSERT ... ON CONFLICT DO UPDATE` syntax correctness) but is a standard, low-risk SQL pattern.
- **Migration/infra needs:** No schema migration (same columns, different conflict-resolution SQL and different application-computed ID). Operationally: run the collision-finder audit script once against production data before/alongside this deploy.
- **Rollback:** Revert the three files; existing already-ingested data is unaffected either way (this only changes IDs for new writes going forward).

### Module 4.2 — Ingestion transactional consistency

- **Issues addressed:** *No transaction or rollback across Postgres and ChromaDB during ingestion — orphaned `documents` rows with zero retrievable chunks*; *Chroma dimension-mismatch failures are loud (as documented) but still trigger orphaned Postgres rows, and compound silently across an entire batch ingest after a provider switch*; *No runtime guard against silently writing wrong-dimension embeddings into a freshly-created, empty Chroma collection.*
- **Files/functions:** `src/retrieval/vector_store.py:284-354` (`upsert_documents`), `src/ingestion/service.py:408-413,480-482`, `src/retrieval/embedder.py`, `src/config.py` (new setting).
- **Approach:**
  1. Reorder `upsert_documents`: attempt the Chroma write **first**; only call `gateway.insert_documents(...)` (Postgres) after Chroma succeeds. If Chroma fails, raise before any Postgres write happens — this is a cheap reordering that eliminates the orphan-row class entirely without needing real cross-database distributed-transaction machinery (which Postgres+Chroma can't do natively anyway).
  2. Add `EXPECTED_EMBEDDING_DIM` to `config.py`, derived from `EMBEDDING_PROVIDER` (e5=1024, gemini=3072, openai=1536 — confirm exact values against each provider's actual output during implementation). In `vector_store.upsert()`, before writing, assert the first embedding's length matches `EXPECTED_EMBEDDING_DIM`; raise a clear, named exception (`EmbeddingDimensionMismatch`) rather than letting Chroma's own error surface first (or silently succeeding on an empty collection, which is the specific gap this finding identifies).
  3. `ingest_directory`'s per-file loop already catches each file's failure independently (this is correct behavior once (1)/(2) prevent orphans) — no change needed there beyond ensuring the new `EmbeddingDimensionMismatch` is caught and logged with enough detail (filename, expected vs. actual dim) that a whole-batch dimension mismatch is diagnosable from one log line instead of N identical-looking failures.
- **Blast radius/risk:** Reordering Chroma-then-Postgres changes failure semantics slightly: a Chroma success followed by a Postgres failure (e.g., a `case_id` foreign-key violation) now leaves an orphaned Chroma chunk with no Postgres row — the inverse orphan of today's bug, but arguably safer (a chunk with no `documents` row is invisible to any UI that lists documents by querying Postgres first, whereas today's orphan *looks* successfully ingested). Mitigate by wrapping the Postgres insert in a try/except that, on failure, deletes the just-written Chroma chunks by their ids (compensating action) before re-raising — this makes the whole operation effectively atomic from the caller's perspective, in either failure direction.
- **Verification:** Unit test with a fake Chroma client that raises on `upsert()` — assert no Postgres row was written. Unit test with a fake gateway that raises on `insert_documents()` — assert the compensating Chroma delete was called. Dimension-mismatch: unit test asserting `EmbeddingDimensionMismatch` raises before any write when embeddings don't match `EXPECTED_EMBEDDING_DIM`. All fully testable with mocks, no live infra needed.
- **Migration/infra needs:** New `EXPECTED_EMBEDDING_DIM` config value (add to `.env.example`, Module 0.3).
- **Rollback:** Revert the reordering/compensating-delete logic; independent of schema.

### Module 4.3 — Ingestion metadata correctness sweep

- **Issues addressed:** *`total_pages` means two different things depending on where it's read, silently masking pages that fail both OCR and the vision fallback*; *Admin KB upload silently overwrites an existing file of the same name on disk, permanently destroying the original evidentiary file*; *`date_registered` is simply the first date-shaped substring found anywhere in the document, not necessarily the actual registration date* (grouped here rather than Phase 6 since it's the same subsystem/file family); *Excel loader's NaN-cleanup also blanks any genuine cell whose actual content is the literal string `"nan"`*; *PDF export escapes XML metacharacters for paragraphs/headings but not for table headers/row cells.*
- **Files/functions:** `src/ingestion/service.py:473` (`total_pages`), `src/ingestion/loaders/pdf_loader.py:205-256` (`_load_scanned_page_with_vision`), `src/api/admin.py:264-269` (KB upload write), `src/extraction/doc_classifier.py:85-86` (`date_registered`), `src/ingestion/loaders/excel_loader.py:152-155`, `src/generation/pdf_builder.py:59-71` (`Table`).
- **Approach:**
  1. `total_pages`: keep the true page count from `doc.num_pages()` under its existing name, and add a **new** key `pages_ingested: len(documents)` alongside it, plus a new `dropped_pages: list[int]` populated whenever `_load_scanned_page_with_vision` returns `[]` for a page (currently silent). Never let one key's meaning shift based on outcome.
  2. Admin KB upload: before `dest.write_bytes(contents)`, check `dest.exists()`; if so, write to a disambiguated path (`filename__2.ext`, incrementing) instead of overwriting, and surface the renamed filename back to the caller so they know a collision occurred.
  3. `date_registered`: change `classify_document` to prefer a date near a recognized label (regex for "date registered"/"dated"/"FIR date" etc. within N characters of a date match, reusing `extract_dates()`'s existing offsets) before falling back to `dates[0]`; if no labeled date is found, keep the current first-date fallback but tag the field with a new `date_registered_confidence: "labeled"|"unlabeled_fallback"` so downstream graph timeline consumers can distinguish a confident date from a guess.
  4. Excel loader: replace the blanket `df.astype(str).replace("nan", "")` with `df.where(pd.notna(df), "")` applied before the `astype(str)` cast, so only genuinely-missing (NaN) cells are blanked, not cells whose real content happens to be the string `"nan"`.
  5. PDF table cells: run the same XML-escaping helper `Paragraph()` calls already use over each table cell string before constructing `Table(table_data)`, for consistency (currently a latent trap, not an active bug, per the audit — this closes it defensively before any future change wraps cells in `Paragraph()`).
- **Blast radius/risk:** Low across the board — these are additive/corrective changes to metadata and string handling, not architectural. The `date_registered` labeled-date heuristic could occasionally pick a worse date than the current first-date default on documents with unusual layouts — mitigate by keeping the fallback and only using the labeled match when found with reasonable confidence (a nearby explicit label word), and by exposing the confidence tag rather than silently trusting either heuristic.
- **Verification:** Each of the five is independently unit-testable with fixture inputs (a fake multi-page PDF result with a dropped page; two upload calls with the same filename; a document with a labeled "Date Registered:" field vs. one without; an Excel sheet with a real `"nan"` string cell; a PDF table cell containing `<` / `&`). None require live infra.
- **Migration/infra needs:** None.
- **Rollback:** Each is an independent, revertible file change.

---

## Phase 5 — Authorization/RBAC application-layer hardening

**Why here, after Phase 2:** these are endpoint-level authorization gaps that exist regardless of RLS — RLS is a backstop, not a replacement for these checks (per the audit's own framing) — but designing them now, with Phase 2's target model already settled, avoids building an app-layer check today that contradicts the RLS design landing next month.

### Module 5.1 — Case mutation and assignment scoping (join these two — same data model)

- **Issues addressed:** *Any user assigned to a case, at any role, can permanently delete or edit the case record*; *Case-assignment routes are gated only by a global role, not by per-case or per-station access.*
- **Files/functions:** `src/api/cases.py:15-19,110-132` (`require_case_access`, `update_case`, `delete_case`), `src/data_gateway/direct_backend.py:516-525` (`check_case_access`), `src/api/case_assignments.py:24-58`, `src/database/models.py` (`CaseAssignment.role`, `Case.police_station`, `User` — **confirmed the `User` model currently has no station/police-station field at all**, only `Case.police_station` exists).
- **Approach:**
  1. `check_case_access` currently only checks *row existence* in `case_assignments`, ignoring the row's own `role` column (which reuses the same global `role_enum`: `investigator`/`supervisor`/`station-admin`/`platform-admin`). Add a `min_role` parameter to `check_case_access`, and require `min_role="supervisor"` for `update_case`/`delete_case` specifically (read/list stays at the current "any assignment" threshold — this finding is about *destructive* operations only, per its own framing). This requires no schema change, since the column already exists and is populated (default `"investigator"`) — it's a pure logic fix: currently-assigned investigators keep read access, lose destructive-write access unless their per-case assignment role is `supervisor`+.
  2. For `case_assignments.py`'s station-scoping: **this needs a schema addition first** — I confirmed `User` has no station field to scope against (only `Case.police_station` exists). Add `User.police_station` (new nullable column, migration `012_user_station.sql`), then change `assign_user`/`unassign_user`/`list_assignments` to additionally require, for callers below `platform-admin`, that `current_user.police_station == case.police_station` (fetch the case row to compare). `platform-admin` keeps unrestricted access, matching its existing system-wide role elsewhere.
- **Blast radius/risk:** Item 1 could break an existing workflow if investigators are currently relied upon to edit/delete cases day-to-day (plausible, since that's the reported bug) — this is a genuine behavior change the team needs to sign off on, not just a bug fix; some currently-working case edits by investigators will start returning 403 the day this ships. Item 2 depends on backfilling `User.police_station` for existing users — until that backfill runs, every existing `station-admin` has `police_station IS NULL`, which under a naive equality check would deny them access to every case (since `NULL == 'some_station'` is never true) — the migration must ship with a backfill step (even a manual one, driven by existing `case_assignments`/`Case.police_station` data to infer each admin's station) or a temporary "if `police_station IS NULL`, fall back to old unrestricted behavior with a loud warning log" bridge, removed once backfilled.
- **Verification:** Unit tests with a mocked gateway: an `investigator`-role case assignment gets 403 on `PUT`/`DELETE /api/cases/{id}`; a `supervisor`-role assignment succeeds. Station-scoping: a `station-admin` with a mismatched `police_station` gets 403 on assignment routes; a matching one succeeds; `platform-admin` always succeeds. Fully testable without live infra using the existing test patterns for these routes (check `tests/test_api_cases.py` / equivalent for the current mocking approach).
- **Migration/infra needs:** New migration `012_user_station.sql` (add `police_station` to `users`); a data backfill (manual or scripted, environment-specific — cannot be automated confidently without knowing the real station/user mapping, which lives outside this codebase).
- **Rollback:** Drop the new column (or leave it, unused) and revert the route logic; case-assignment access reverts to today's (looser) behavior.

### Module 5.2 — Graph-review queue hardening

- **Issues addressed:** *Graph-review confirm/reject actions accept a client-supplied `reviewed_by` value and stamp identity decisions with an unverifiable attribution*; *Graph-review confirm/reject actions are never written to the audit log at all*; *`graph_review.py`'s `list_pending` endpoint accepts a `case_id` query parameter that is silently ignored*; *Graph-review queue and confirm/reject bypass `case_assignments` entirely for any global supervisor-or-above* (see §9 disagreement below — I'm not recommending a code fix for this last one without a product decision).
- **Files/functions:** `src/api/graph_review.py` (whole file — `ReviewAction.reviewed_by`, `confirm_match`, `reject_match`, `list_pending`).
- **Approach:**
  1. `reviewed_by`: remove it from the client-supplied `ReviewAction` request model entirely; derive it server-side from the already-authenticated `admin: User` dependency (`reviewed_by=str(admin.id)` or `admin.username`, whichever the rest of the codebase's audit trail convention uses — check `log_audit_event` call sites for the established identity-field convention). This is a pure request-model change, not a new dependency.
  2. Add `gateway.log_audit_event("graph_review_confirm"/"graph_review_reject", {...})` calls to `confirm_match`/`reject_match`, matching the pattern every other mutating admin/case action already uses elsewhere in the codebase.
  3. `list_pending`'s ignored `case_id` param: either wire it into the underlying query as a real filter (straightforward — the query already has access to `case_id` on the entities/edges it returns) or, if cross-case visibility is confirmed as the deliberate design (see §9), change the parameter's default/response to make that explicit — e.g. return `case_id_filter_applied: false` in the response envelope, or reject the parameter with a 400 rather than silently ignoring caller intent. I recommend actually implementing the filter (it's cheap and strictly more useful) rather than the 400-rejection alternative, but this is a small enough judgment call to flag rather than force.
- **Blast radius/risk:** Low. `reviewed_by` and audit-logging additions are purely additive. The `case_id` filter, if implemented, changes `list_pending`'s response shape for any existing caller passing `case_id` today (they'd start getting filtered results instead of the full queue) — check the admin frontend's `ReviewQueuePage.tsx` doesn't rely on the current no-op behavior before wiring the filter through.
- **Verification:** Unit tests: `confirm_match`/`reject_match` write an audit log entry with the authenticated user's id, not a client-supplied string, even if the request body includes a spoofed `reviewed_by`. `list_pending?case_id=X` returns only entities/edges touching case X once wired.
- **Migration/infra needs:** None (no schema change — `reviewed_by` is presumably already a string column on the edge/audit record).
- **Rollback:** Revert the file; independent.

### Module 5.3 — Session/attachment ownership gaps

- **Issues addressed:** *Chat attachment upload has no session-ownership check, unlike the sibling list/delete endpoints in the same file*; *Session message writes (`save_history`) are not ownership-checked, asymmetric with reads/deletes in the same module*; *Falsy-ownership checks silently skip authorization for rows with a missing owner.*
- **Files/functions:** `src/api/attachments.py:59-138` (`upload_attachment`), `:141-161` (`list_attachments`/`delete_attachment`), `src/memory/conversation.py:113-160` (`load_history`/`save_history`/`delete_history`).
- **Approach:**
  1. `upload_attachment`: add the same ownership check `list_attachments`/`delete_attachment` already use — fetch the session, and if it has an owner, require `session["user_id"] == current_user.id`.
  2. `save_history`: add the identical check `load_history`/`delete_history` already perform (`if session_obj["user_id"] and session_obj["user_id"] != user_id: raise`/`return`) — currently the only one of the three with no check at all.
  3. **Falsy-ownership default**: change every one of these conditional checks (`if session.get("user_id") and session["user_id"] != current_user.id`) from "skip the check when `user_id` is falsy" to "deny by default when `user_id` is missing, unless the caller is the one who just created this ownerless row in the same request" — concretely, invert to `if session.get("user_id") not in (None, current_user.id): raise 403` combined with `if session.get("user_id") is None: raise 403` (deny), *except* the specific, already-audited legitimate case of a brand-new session being created by its own creator in the same call — which doesn't hit this check at all since it's a fresh insert, not a lookup. This closes the "fail-open on missing owner" gap the audit flagged as latent-but-wrong-default.
- **Blast radius/risk:** Low-medium. If any current legitimate flow relies on ownerless sessions being freely writable (the audit notes `main.py`'s own comments acknowledge these have existed historically) — auditing every current call site for a still-relevant ownerless-session flow is needed before flipping "skip check" to "deny" (do a repo-wide grep for any code that intentionally creates a `user_id=None` session/attachment/message row, to confirm none is a legitimate current feature this would break).
- **Verification:** Unit tests: uploading an attachment to another user's session returns 403; `save_history` on another user's session returns 403/raises; a session with `user_id=None` denies a third party but is confirmed (via the repo grep above) to have no legitimate current writer either, so denying it outright is safe.
- **Migration/infra needs:** None.
- **Rollback:** Revert; independent per-file changes.

### Module 5.4 — Generated-file download scoping

- **Issues addressed:** *Generated-file download bypasses case-level access control for `station-admin` accounts.*
- **Files/functions:** `src/main.py:281-323` (`download_file`), `src/data_gateway/direct_backend.py:234-266` (`generated_files` schema).
- **Approach:** `generated_files` has no `case_id` column, so there's nothing to scope against today even if the blanket `station-admin` role check were removed. Two-part fix: (1) add a `case_id` column to `generated_files` (nullable — not every generated file is case-derived) populated at generation time from whatever context produced the file (the RAG answer's `case_id`, if any); (2) change `download_file`'s check from a blanket `role in ("station-admin", "platform-admin")` bypass to: owner OR `platform-admin` OR (`station-admin` AND `case_id` matches one of their `case_assignments`, when the file has a `case_id`; unrestricted only when the file has no `case_id`, i.e. wasn't case-derived).
- **Blast radius/risk:** Medium — requires a schema migration and backfill decision for existing rows (existing `generated_files` rows have no way to retroactively determine their `case_id` unless it's derivable from the associated `pipeline_runs`/session; if not cleanly derivable, leave existing rows' `case_id` NULL, meaning they keep today's blanket `station-admin` access — only *new* files get properly scoped, which is an acceptable, explicitly-stated limitation rather than a silent gap).
- **Verification:** Unit test: a `station-admin` with no case-assignment for a file's `case_id` gets 403; one with the matching assignment succeeds; a `platform-admin` always succeeds; a file with `case_id IS NULL` remains accessible to any `station-admin`/`platform-admin` (documented legacy behavior).
- **Migration/infra needs:** Migration `013_generated_files_case_id.sql`; no automatic backfill (see above).
- **Rollback:** Drop the column or ignore it; revert the route logic to the prior blanket check.

---

## Phase 6 — LLM pipeline correctness

**Why here:** independent of everything above; grouped as one phase because most of it touches the same handful of files (`evaluator.py`, `verifier.py`, `router.py`, `query_expander.py`, `sql_extractor.py`) and should land together to avoid re-touching them repeatedly.

### Module 6.1 — Shared, safe JSON extraction (replaces 3 duplicated buggy implementations + 2 missing ones)

- **Issues addressed:** *Greedy `_extract_json` regex — duplicated in three files — re-implements a pattern this codebase already diagnosed and fixed as broken*; *Thinking-trace JSON-parsing fix applied inconsistently — several identical call sites skipped.*
- **Files/functions:** `src/pipeline/file_structurer.py:13-69` (the correct implementation, to be extracted, not rewritten), `src/pipeline/evaluator.py:41-50,107`, `src/pipeline/verifier.py:61-78,288`, `src/pipeline/router.py:60-67`, `src/pipeline/query_expander.py:80-93`, `src/pipeline/sql_extractor.py:22-31`.
- **Approach:** Move `file_structurer.py`'s existing `_extract_json` (the `<think>`-tag strip + fenced-block check + string-aware brace-depth scan — already correct, confirmed by direct read) verbatim into a new shared module, `src/pipeline/json_extract.py`, as the single `extract_json(response: str) -> dict` (or `-> Any`, to also support the list-returning callers below). Update every call site:
  - `evaluator.py`/`verifier.py`: replace their local `_extract_json` (the greedy `r"\{.*\}"` regex) with the shared import.
  - `router.py`: replace its inline `re.search(r'\{.*\}', ...)` block with the shared import.
  - `query_expander.py`: replace its markdown-fence-only strip with the shared import (it needs the list-returning case — the shared function should return whatever `json.loads` produces, list or dict, and let each caller validate the shape it expects, same as `query_expander.py` already does after parsing).
  - `sql_extractor.py`: replace its hardcoded ``` ```json ``` prefix/suffix strip with the shared import.
- **Blast radius/risk:** Low — this is strictly a robustness improvement (the shared function is a superset of what each broken version already tried to do: whole-response parse, then fenced-block, then brace-scan). The only behavior change is that previously-failing parses (preamble-contaminated JSON) now succeed, which is the intended fix, not a risk. No case where the new function is stricter than any of the old ones.
- **Verification:** Move `file_structurer.py`'s existing tests (if any target `_extract_json` directly) to target the new shared module; add cases for each of the five call sites' actual historical failure mode (a `<think>...</think>` preamble before a JSON array, for `query_expander.py`; a bare ` ```json ` fence with no closing brace-scan needed, for `sql_extractor.py`). Fully unit-testable, no live infra.
- **Migration/infra needs:** None.
- **Rollback:** Revert the five call sites back to their local implementations; the shared module itself is inert if unused.

### Module 6.2 — RAG route exception guard

- **Issues addressed:** *RAG route's final generation + verifier call is the one dispatch branch with no exception guard.*
- **Files/functions:** `src/pipeline/orchestrator.py:1349-1401` (RAG's `call_llm()`/`verify_grounding()`).
- **Approach:** Wrap the generation + verification block in the same `try/except` pattern every other route branch already uses, degrading to `_SAFE_RESPONSE`/`_ABSTENTION_RESPONSE` on any exception (context-window overflow, provider error, a bug inside `verify_grounding()`), rather than letting it propagate uncaught out of `process_query()` into `main.py`'s bare `{"detail": str(e)}` SSE error event.
- **Blast radius/risk:** Low — purely additive error handling matching an established, already-proven-safe pattern used by every sibling branch. No behavior change on the success path.
- **Verification:** Unit test: mock `call_llm`/`verify_grounding` to raise inside the RAG branch, assert the pipeline yields `_SAFE_RESPONSE`/`_ABSTENTION_RESPONSE` and a `response`/`done` event instead of propagating the exception — mirror the existing test pattern for the other routes' equivalent failure-mode tests.
- **Migration/infra needs:** None.
- **Rollback:** Revert; independent.

### Module 6.3 — max_tokens / context-window safety

- **Issues addressed:** *Raising evaluator/verifier `max_tokens` from 800 to 2000 risks local-model context-window overflow*; *`max_tokens` increase on evaluator/verifier applies uniformly to the cloud fallback too, on every call including every RAG retry.*
- **Files/functions:** `src/pipeline/evaluator.py:103`, `src/pipeline/verifier.py:284`, contrast `src/pipeline/file_structurer.py:131-136`'s documented ~4096-token local-model ceiling.
- **Approach:** Make `max_tokens` local-vs-cloud aware rather than a single constant: keep `2000` for cloud providers (Gemini/Groq, which have ample context budget and where 2000 was presumably raised for legitimate output-quality reasons), but cap the **local**-model call at a value that leaves real input headroom given the ~4096-token ceiling `file_structurer.py` already documents empirically (e.g., 800-1000, matching the pre-existing value that was known to fit) — determine the exact safe number from the same empirical source `file_structurer.py`'s comment cites, not a guess. This requires threading a `is_local: bool` (or checking `llm_mode`) into the `max_tokens` selection at both call sites.
- **Blast radius/risk:** Low. Reverting the local-model max_tokens closer to its previous, known-safe value undoes whatever output-quality improvement motivated the original 800→2000 change for local — if that change was deliberately made to fix truncated evaluator/verifier JSON on the local model specifically, this fix could reintroduce that original problem. **Check the commit that introduced the 800→2000 change (`git log -p -- src/pipeline/evaluator.py`) for its stated rationale before picking the new local-model number** — if truncation was the original motivation, the real fix is trimming the *input* (fewer/shorter chunks in the prompt) rather than raising the *output* budget, since the latter is what risks the overflow this finding warns about.
- **Verification:** Cannot be verified without access to the actual local model server to confirm its real context window (the audit itself couldn't verify this from the repo). Verification here is a code-level guard (assert/log a warning if the computed prompt token estimate + max_tokens exceeds a configured `LOCAL_MODEL_CONTEXT_WINDOW`), not a live-tested fix.
- **Migration/infra needs:** New `LOCAL_MODEL_CONTEXT_WINDOW` config value (add to `.env.example`).
- **Rollback:** Revert to the flat `2000` for both; independent.

### Module 6.4 — Streaming/rotation robustness

- **Issues addressed:** *`_stream_local` never got the empty/whitespace-content robustness fix `_call_local` just received*; *`key_manager.py`'s key rotation has no coordination across concurrent rate-limit failures.*
- **Files/functions:** `src/llm/client.py:224-276` (`_call_local`/`_stream_local`), `src/llm/key_manager.py:33-46` (`rotate_key`).
- **Approach:**
  1. Port `_call_local`'s `if not content or not content.strip(): raise ...` (triggering the existing cloud-fallback path) into `_stream_local`'s equivalent point — after the stream completes, check whether any non-whitespace content was actually yielded; if not, raise the same exception `stream_llm()`'s existing exception handler already knows how to catch and fall back on.
  2. `rotate_key`: change from an unconditional index-increment to a **compare-and-swap** on the index — `rotate_key(expected_current_index)` only advances if the current index still equals what the caller observed before it hit the rate limit; if another coroutine already rotated past it, this call is a no-op (the key has already been rotated by someone else, no need to advance further). This requires threading the index the caller last used through the rate-limit-handling call site, not just calling `rotate_key()` blind.
- **Blast radius/risk:** Low. (1) is a direct, low-risk port of an already-shipped, already-tested fix to its sibling function. (2) changes concurrent behavior under load — needs a concurrency test (multiple simulated coroutines hitting a rate limit simultaneously) to confirm the CAS logic doesn't itself deadlock or under-rotate when truly all keys are exhausted.
- **Verification:** (1): unit test with a fake streaming client yielding only whitespace deltas, assert the cloud fallback fires. (2): unit test simulating N concurrent `rotate_key` calls all observing the same starting index, assert the index advances by exactly 1 (not N), and a follow-up single call from a stale-index caller after that is a no-op. Both fully testable without live infra (pure async logic + mocks).
- **Migration/infra needs:** None.
- **Rollback:** Revert either independently.

### Module 6.5 — Extraction regex robustness

- **Issues addressed:** *CNIC/phone/plate/FIR extraction regexes are strict-separator-only, silently under-matching exactly the OCR'd/scanned documents where entity resolution needs them most.*
- **Files/functions:** `src/extraction/structured_fields.py:53-65` (`_CNIC_RE`, `_PHONE_RE`, `_PLATE_RE`, `_FIR_RE`), `normalize_urdu()`.
- **Approach:** Add a punctuation-normalization pass before matching (not into `normalize_urdu()`, which has a different, documented job) — collapse runs of whitespace/en-dash/em-dash/non-breaking-hyphen around digit groups to a plain ASCII hyphen, and make the hyphen itself optional-but-anchored in each regex (e.g. `\b\d{5}-?\d{7}-?\d\b` plus a normalization pre-pass so `03211234567`/`+92 321 1234567`/CNIC-without-hyphens are all caught). Make `_PLATE_RE` case-insensitive (`re.IGNORECASE`).
- **Blast radius/risk:** Loosening these regexes increases false-positive risk (matching an unrelated 13-digit sequence as a CNIC) — the audit already flags this as a pre-existing risk even with the strict version. Mitigate by keeping the digit-count/grouping structure exact (only the separator becomes optional, not the digit pattern), which doesn't meaningfully widen the false-positive surface beyond what already exists.
- **Verification:** Unit tests with the specific OCR-realistic variants named in the audit (`03211234567`, `+92 321 1234567`, spaced/dashless CNIC, lowercase plate) — assert they now match; assert existing correctly-matching fixtures still match unchanged. Fully unit-testable.
- **Migration/infra needs:** None.
- **Rollback:** Revert the regex/normalization changes; independent.

---

## Phase 7 — Ingestion performance & safety

### Module 7.1 — Blocking vision-OCR retry loop

- **Issues addressed:** *A blocking `time.sleep(120)` retried up to 10 times runs inside an `async def` ingestion function with no executor offload — a single bad file can freeze the entire server.*
- **Files/functions:** `src/ingestion/loaders/pdf_loader.py:186-256` (`_load_scanned_page_with_vision`), `load_pdf`, `src/ingestion/loader_router.py:85`, `src/ingestion/service.py:348` (`ingest_file`).
- **Approach:** Wrap the entire synchronous `load_pdf`/vision-fallback call chain in `await asyncio.to_thread(...)` at the point `ingest_file` invokes it (line 348) — the simplest fix that doesn't require converting every loader to native async. Confirm `time.sleep(120)` itself doesn't need to become `asyncio.sleep` (it's already off the event loop once the whole chain runs in a thread pool worker).
- **Blast radius/risk:** Low — moving synchronous, CPU/IO-blocking work into a thread pool is a standard, safe pattern that doesn't change the loader's own logic at all. The only operational consideration: the default `asyncio.to_thread` pool size is unbounded by default in modern Python but shares the process's thread pool executor — a burst of simultaneous bad-scan ingests could still exhaust threads (not the event loop) under extreme load; acceptable, since that's a vastly better failure mode than freezing every other request.
- **Verification:** Integration test: kick off a simulated slow/rate-limited ingest (mock the vision call to sleep) and confirm a concurrent `/health` or mock chat request still responds promptly during the ingest — this is testable without live infra using `asyncio` mocks and a test client, no real Postgres/AGE/GPU needed.
- **Migration/infra needs:** None.
- **Rollback:** Revert the `to_thread` wrapper; independent single-line-ish change.

### Module 7.2 — Upload validation and size caps

- **Issues addressed:** *No MIME/magic-byte file-type validation anywhere in the upload-to-ingestion path*; *No file-size cap exists inside the shared ingestion/loader code path — the 50MB limit lives only on the single admin HTTP upload endpoint.*
- **Files/functions:** `src/ingestion/loader_router.py:55-90`, `src/api/admin.py:249-262`, `src/ingestion/service.py:260-306` (`ingest_directory`).
- **Approach:** Add a shared `src/ingestion/validation.py::validate_file(path)` used by every ingestion entry point (the admin upload endpoint, `ingest_directory`, any future case-evidence endpoint) that checks: (a) file size against `config.MAX_UPLOAD_SIZE_MB` (currently admin-endpoint-only), (b) magic-byte/content-type sniffing (e.g. via `python-magic` or a minimal hand-rolled signature check for PDF/DOCX/XLSX/common image formats) matches the claimed extension, rejecting a mismatch before any loader touches the bytes, (c) for `.docx`/`.xlsx` (zip-based), a decompression-ratio guard (reject if uncompressed size would exceed e.g. 100x the compressed size or a hard cap) before full decompression.
- **Blast radius/risk:** Low-medium — a legitimate file with an unusual but valid internal structure could be incorrectly rejected by an overly strict magic-byte check; use permissive, well-tested signature libraries rather than hand-rolled byte matching where possible, and log-don't-silently-drop any rejection so false positives are diagnosable.
- **Verification:** Unit tests: a `.pdf`-named file with non-PDF magic bytes is rejected; a zip crafted with an extreme compression ratio (a small test fixture, safely constructed, not a real "zip bomb" download) is rejected before full decompression; a file exceeding the size cap is rejected at every entry point, not just the admin endpoint. Fully testable without live infra.
- **Migration/infra needs:** Possibly a new dependency (`python-magic` or equivalent) — check licensing/OS-dependency implications (on Windows, `python-magic` needs `libmagic` bindings — confirm this doesn't complicate the dev setup described in the README, or use a pure-Python magic-byte table instead to avoid the native-dependency question entirely).
- **Rollback:** Revert the shared validator and its call sites; independent.

### Module 7.3 — Entity-resolution N+1 batching

- **Issues addressed:** *N+1 Cypher round-trips in entity resolution's candidate generation — one query per surviving candidate, per extracted mention, during ingestion.*
- **Files/functions:** `src/graph/entity_resolution.py:171-213` (`_generate_candidates`, the serial `await _shares_case(entity_id, case_id)` inside the loop).
- **Approach:** Batch the per-candidate `_shares_case` checks into a single Cypher query taking a list of candidate entity ids and returning which ones share the case (`WHERE id(n) IN $candidate_ids AND ...`), replacing N serial round-trips with one. This is a mechanical query-shape change, not a logic change — the resolution decision itself (which candidates pass) is unchanged.
- **Blast radius/risk:** Low — same result set, fewer round trips. Slight risk if the candidate list can be very large (unlikely given the audit's own note that this is bounded by a similarity floor of 0.40 and "dozens" of candidates, not thousands) — cap the batch size defensively if it ever exceeds a few hundred.
- **Verification:** Unit test with a mocked `age_client.execute_cypher` counting call invocations before/after — assert it drops from O(candidates) to O(1) per mention. Fully testable with mocks.
- **Migration/infra needs:** None.
- **Rollback:** Revert; independent.

### Module 7.4 — Analytics indexes, pagination, and bounded caches

- **Issues addressed:** *Analytics queries against `pipeline_runs`/`pipeline_steps`/`error_logs` are unbounded and the underlying tables have zero indexes*; *Audit Logs page (admin) has no pagination or size limit at all; several sibling pages are capped at a fixed 100 rows with no "load more" or true-total indicator*; *Unbounded, process-lifetime in-memory vision-OCR cache*; *`docx_loader._iter_blocks` is O(n²).*
- **Files/functions:** `src/database/models.py` (add `Index()` declarations), `src/data_gateway/direct_backend.py:948-973,804-817` (`get_runs_since`/`get_step_latencies_since`/`get_errors_since`), `src/ingestion/loaders/image_loader.py:21` (`_vision_cache`), `src/ingestion/loaders/docx_loader.py:134-171`.
- **Approach:**
  1. Add indexes on `pipeline_runs(created_at)`, `pipeline_steps(run_id, created_at)`, `error_logs(created_at)` (exact columns to confirm against actual query `WHERE`/`ORDER BY` clauses during implementation) via a new migration.
  2. Add `limit`/`offset` parameters (with sane defaults) to `get_runs_since`/`get_step_latencies_since`/`get_errors_since`, and thread real pagination controls through `AuditLogPage.tsx` and the other fixed-100-row admin pages, replacing the implicit "first 100, no more" behavior with an actual "load more"/page control and a true total count.
  3. Bound `_vision_cache` with a simple LRU (e.g. `functools.lru_cache`-style wrapper or a small hand-rolled bounded dict, capped at a few hundred entries — vision OCR results are small strings, so the cap is about count, not byte size).
  4. `docx_loader._iter_blocks`: replace the O(n²) rescan with a single linear pass building an index once (a one-time dict/lookup keyed by whatever the current rescan is searching for) — mechanical algorithmic fix, no behavior change.
- **Blast radius/risk:** Low across all four — indexes are additive and safe (verify they don't slow down writes meaningfully on these already-append-heavy tables, but read-heavy analytics tables benefit far more than the write cost). Pagination changes the admin pages' data-fetch contract — coordinate with Phase 9's admin frontend work so the two aren't implemented against different assumptions about the response shape.
- **Verification:** Indexes: `EXPLAIN ANALYZE` before/after on a live Postgres instance with realistic data volume — cannot be meaningfully verified without one (an empty/small dev DB won't show the difference). Pagination/cache/O(n²): fully unit-testable without live infra.
- **Migration/infra needs:** New migration for indexes.
- **Rollback:** Drop indexes; revert pagination/cache/loader changes independently.

---

## Phase 8 — Frontend security & state hygiene (main chat app)

**Why here:** independent of all backend phases; the case-switch/citation-panel leak is Critical and should not wait on any backend work, but frontend and backend can proceed in parallel once Phase 2's design (which determines whether the frontend needs to pass anything new for case-scoping) is settled — hence sequenced after backend phases only for review-bandwidth reasons, not a real code dependency.

### Module 8.1 — Cross-case data leak on case/session switch

- **Issues addressed:** *Switching the active case in the chat UI does not scope the visible conversation — stale case content stays on screen and can bleed into new messages*; *Citation panel is never cleared on session/case navigation — cross-case evidence bleeds visibly into the UI.*
- **Files/functions:** `frontend/src/components/layout/Sidebar.tsx:195-201` (Case `<select>` `onChange`), `frontend/src/pages/ChatPage.tsx:20-40,59-71` (`activeSource`, session-restore effect).
- **Approach:** Change the Case (and Project) `<select>`'s `onChange` to `navigate('/', { state: { fresh: true } })` — reusing the existing "New Chat" button's mechanism — instead of a bare `navigate('/')`, so the session-restore effect's existing `location.state.fresh === true` check correctly skips restoring the previous case's last session. Add `activeSource: null` to whatever the same effect already resets (or a new `useEffect` keyed on `caseId`/`sessionId` change) so the citation panel is cleared on the same navigation, not just on explicit close.
- **Blast radius/risk:** Low — this reuses an already-existing, already-working code path (`fresh: true`) rather than inventing a new one. Confirm no other current caller of the Case/Project `onChange` relies on the "keep last session" behavior being preserved across a case switch (unlikely, since that's the bug).
- **Verification:** Manual browser test (per this codebase's own UI-testing convention): switch case, confirm the composer/chat area clears rather than showing the prior case's messages; open a citation, switch case, confirm the citation panel is gone. Also confirm via a component test (if the frontend has React Testing Library set up — check `frontend/` for existing test tooling) that the `fresh` state is passed on the select's `onChange`.
- **Migration/infra needs:** None.
- **Rollback:** Revert; independent, no backend coordination needed.

### Module 8.2 — Store hygiene and streaming robustness

- **Issues addressed:** *No store reset on logout — stale user/case/session state can persist into the next login on a shared workstation*; *Rapid double-send can permanently orphan a "streaming" assistant message*; *SSE stream has no client-side stall timeout or reconnect strategy.*
- **Files/functions:** `frontend/src/store/authStore.ts:81-89` (`logout()`), `chatStore`/`caseStore`/`projectStore`/`sessionStore` (module-level singletons), `frontend/src/store/chatStore.ts:255-404` (`sendMessage`), `frontend/src/lib/api.ts:40-117` (`streamChat`).
- **Approach:**
  1. `logout()`: call each store's own reset (add a `reset()` action to `chatStore`/`caseStore`/`projectStore`/`sessionStore` if they don't already have one) and clear the unscoped `LAST_SESSION_KEY` from `localStorage`.
  2. `sendMessage`: before aborting a prior in-flight controller for a new send, explicitly mark that prior message's `isStreaming = false` (with whatever partial content/an "interrupted" marker it had) instead of leaving it in a permanent streaming state when the abort's `catch` exits early.
  3. `streamChat`: add a stall timeout (e.g., reset a timer on every received chunk; if no chunk arrives within N seconds, abort and surface a "connection seems stalled" state) — no reconnect logic is being added here (that's a larger scope decision, see §10 deferred list), just detection so the UI stops looking falsely "still working" forever.
- **Blast radius/risk:** Low for (1)/(2). (3)'s stall timeout needs a sensible default (long enough not to false-positive on a genuinely slow LLM response, e.g. 60-90s) — pick this value with awareness of the pipeline's own realistic worst-case latency (multiple LLM calls + retries) so it doesn't fire on legitimately slow-but-working responses.
- **Verification:** All three are unit/component-testable in the frontend's existing test setup (mock the store, mock the fetch stream, simulate double-send and a stalled stream). No backend/live infra needed.
- **Migration/infra needs:** None.
- **Rollback:** Revert independently; no backend coordination.

### Module 8.3 — Swallowed frontend errors

- **Issues addressed:** *Sidebar session delete/rename/export failures are swallowed with only `console.error`*; *`caseStore`/`projectStore`/`sessionStore` fetch errors and loading states are never rendered anywhere in the UI*; *Malformed SSE event chunks are dropped with zero logging, not even a console warning.*
- **Files/functions:** `frontend/src/components/layout/Sidebar.tsx:76-95,108-149`, `frontend/src/lib/api.ts:97-116`.
- **Approach:** Surface each store's existing `error`/`isLoading` fields (already present per the audit, just unconsumed) in the Sidebar UI — a small inline error indicator is enough, not a redesign. Replace Sidebar's silent `console.error` catches with a lightweight toast/inline failure message. Change the SSE parser's empty `catch` blocks to at least `console.warn` with the offending raw chunk, so a production debugging session has something to go on.
- **Blast radius/risk:** Low — purely additive UI feedback, no behavior change to the underlying data flow.
- **Verification:** Component tests forcing a fetch rejection and asserting an error UI element renders; a malformed SSE chunk fixture logs a console warning.
- **Migration/infra needs:** None.
- **Rollback:** Revert independently.

### Module 8.4 — Deferred: markdown rendering and responsive layout

See §10 (deliberately deferred) — *Assistant responses are rendered with a hand-rolled partial "markdown" parser* and *Two-column chat layout and fixed sidebar have no mobile/tablet breakpoints* are real Medium findings but represent meaningfully larger, separately-scoped work (a sanitized markdown renderer needs an XSS review given untrusted-content findings elsewhere in this same audit; a responsive redesign needs a product decision on tablet/field-officer support). Not included as a module here — see §10 for the full reasoning.

---

## Phase 9 — Admin frontend fixes

### Module 9.1 — Audit Logs page hardening

- **Issues addressed:** *Admin Dashboard's Audit Logs page: the frontend's role gate is looser than the backend's* (see §9 disagreement/decision below); *Audit Logs page doesn't clear stale results on a failed fetch*; *Audit Logs filters fire a full backend request on every keystroke, no debounce*; *Audit log `details` blob is rendered raw into the admin DOM with no field-level redaction*; *Audit Logs page has no date-range filter*; *`AuditLogPage.tsx`'s raw `fetch()` calls omit explicit `credentials`/CSRF handling.*
- **Files/functions:** `admin-frontend/src/pages/AuditLogPage.tsx` (whole file), `admin-frontend/src/App.tsx:55`, `admin-frontend/src/components/Sidebar.tsx:63-67`, `src/api/admin.py:214-221`.
- **Approach:** (Role-gate fix depends on the product decision in §9 — implementing it here assuming the decision lands as "match the backend, `platform-admin`-only," since that's the narrower, safer default until told otherwise.) Remove the Audit Logs nav link/route for `supervisor`/`station-admin` in the frontend to match the backend's actual restriction — this alone fixes the "broken 403 experience," independent of which direction the eventual product decision goes (widening backend access later is a much smaller, safer follow-up than leaving the current mismatch). Additionally: clear `logs` state on fetch failure (don't leave stale rows under the error banner); debounce the text filters (~300ms); redact or truncate high-risk fields from the raw `details` JSON dump (at minimum, don't render nested PII blobs unredacted — check `log_audit_event` call sites for which fields are actually sensitive before designing the redaction list); add the same `RangePicker` component `DashboardPage.tsx`/`ErrorsPage.tsx` already use, plus real pagination (coordinate with Phase 7.4's backend pagination work); switch the raw `fetch()` calls to the shared axios instance (`admin-frontend/src/api.ts`) used by every other page, picking up `withCredentials: true` for free.
- **Blast radius/risk:** Low-medium. Removing the nav link for `supervisor`/`station-admin` is a visible behavior change for whoever currently (mis)uses it, but it's currently broken for them anyway (per the audit) — this is a strict improvement, not a new restriction, pending the §9 decision on whether to instead widen backend access.
- **Verification:** Component tests for each of the six behaviors (nav link visibility per role, stale-clear on failure, debounce timing, redaction of a known-sensitive field, date-range filter present, network calls including credentials). Fully testable in the frontend's existing test setup.
- **Migration/infra needs:** None beyond Phase 7.4's pagination backend work, which this depends on for the "true pagination" half specifically (the rest is independent).
- **Rollback:** Revert independently; the nav-link change can be reverted trivially if the product decision goes the other way.

### Module 9.2 — Confirmation dialogs and attribution

- **Issues addressed:** *Case-assignment removal and entity-match confirm/reject have no confirmation dialog before an irreversible action fires.*
- **Files/functions:** `admin-frontend/src/pages/CaseManagementPage.tsx:113-122` (`handleUnassign`), `admin-frontend/src/pages/ReviewQueuePage.tsx:72-85` (`act('confirm'|'reject')`), and — coordinating with backend Module 5.2 — the admin frontend's hardcoded `reviewed_by: "admin"` literal.
- **Approach:** Add `window.confirm(...)` to both handlers, matching the existing pattern already used by KB-document-delete and generated-file-delete in the same app. Remove the frontend's hardcoded `reviewed_by: "admin"` from the confirm/reject request body entirely, since backend Module 5.2 stops accepting/trusting a client-supplied value anyway.
- **Blast radius/risk:** Low — purely additive friction on two destructive actions, matching an already-established in-app pattern.
- **Verification:** Component test: clicking unassign/confirm/reject without confirming the dialog performs no network call; confirming it does.
- **Migration/infra needs:** None (coordinate merge order with backend Module 5.2 so the frontend doesn't send a now-ignored field indefinitely, though sending an extra ignored field is harmless if sequencing slips).
- **Rollback:** Revert independently.

### Module 9.3 — CSS drift and accessibility sweep

- **Issues addressed:** *Seven of thirteen admin pages reference CSS classes and custom properties that don't exist in the loaded stylesheet*; *Filter/tier/range-picker buttons across the admin app convey selected state by color alone, with no `aria-pressed`/`aria-selected`*; *Form labels are not programmatically associated with their inputs* (both apps); *Workspace/Case `<select>` dropdowns have no associated accessible label*; *Case/Project creation modals have no dialog semantics, focus trap, or Escape-to-close handling*; *Several icon-only buttons rely only on `title`, not `aria-label`*; *Login/Case-Assignment forms have unassociated label/input pairs (admin frontend)*; *One hardcoded Tailwind color class breaks the app's single-source-of-truth design-token system*; *Branding inconsistency between Login and Register pages.*
- **Files/functions:** the seven admin pages listed in the audit (`CaseManagementPage.tsx`, `EntityEvalPage.tsx`, `GeneratedFilesPage.tsx`, `McpCallLogPage.tsx`, `ProfilePage.tsx`, `RunHistoryPage.tsx`, `UsersPage.tsx`), `admin-frontend/src/components/common.tsx:53-72`, main-app `LoginPage.tsx`/`RegisterPage.tsx`/`CaseSettingsModal.tsx`/`ProjectSettingsModal.tsx`/`Sidebar.tsx`, `admin-frontend/src/pages/LoginPage.tsx`, `CaseManagementPage.tsx`, `SettingsPage.tsx:125`.
- **Approach:** This is a mechanical sweep, not a design decision: replace each nonexistent class/variable reference with its real equivalent (the audit already lists the exact wrong→right mapping for every one — `.page-subtitle`→`.page-sub`, `var(--gold)`→(confirm real token), etc.); add `htmlFor`/`id` pairs to every listed unassociated label/input; add `aria-label` to the listed `<select>`s and icon-only buttons; add `role="dialog"`/`aria-modal`/`aria-labelledby`/focus-trap/Escape-handling to the two creation modals (a small shared hook/wrapper, e.g. `useModalA11y()`, applied to both, is cleaner than duplicating the logic); add `aria-pressed`/`aria-selected` to the filter/range-picker buttons; replace `text-green-600` with `var(--success)`; add the shared `LogoLockup` to `RegisterPage.tsx`.
- **Blast radius/risk:** Very low — every change is either fixing an already-broken visual (the CSS class drift genuinely renders wrong/invisible today, so there's no working behavior to regress) or purely additive ARIA attributes.
- **Verification:** Visual regression (manual, screenshot the seven pages before/after) for the CSS fixes; automated accessibility testing (`axe-core` or equivalent, if not already in the frontend's toolchain — worth adding given how many findings this sweep addresses) for the ARIA/label fixes going forward, not just this one pass.
- **Migration/infra needs:** Possibly adding an accessibility-testing dependency (`@axe-core/react` or `jest-axe`) — small, optional but recommended addition alongside this module.
- **Rollback:** Revert independently, file by file.

### Module 9.4 — Remaining admin-page error/loading-state gaps

- **Issues addressed:** *Admin Errors page has no `.catch()` on its data fetch*; *Admin Dashboard loading indicator only guards the very first load*; *Knowledge Base page (admin) load and delete have no error handling.*
- **Files/functions:** `admin-frontend/src/pages/ErrorsPage.tsx:51-80`, `admin-frontend/src/pages/DashboardPage.tsx:110-116`, `admin-frontend/src/pages/KnowledgeBasePage.tsx:58-113`.
- **Approach:** Add `.catch()` to `ErrorsPage.tsx`'s `Promise.all([...])` chain, setting a visible error state instead of silently rendering "0 errors." Change `DashboardPage.tsx`'s loading guard from `if (loading && !usage)` to something that also shows a (lighter-weight, e.g. a small inline spinner rather than the full first-load skeleton) loading indicator on subsequent range changes. Add error handling to `KnowledgeBasePage.tsx`'s `refresh`/`remove` so a failed load doesn't hang on "Loading…" forever and a failed delete gives visible feedback.
- **Blast radius/risk:** Very low — additive error/loading UI only.
- **Verification:** Component tests forcing each fetch to reject and asserting the corresponding error state renders.
- **Migration/infra needs:** None.
- **Rollback:** Revert independently.

---

## Phase 10 — Dead code, contract drift, and cleanup

**Why last-but-one:** genuinely zero risk and zero urgency — nothing depends on these, and nothing else depends on them either. Sequenced late so review bandwidth goes to the security/correctness work first.

### Module 10.1 — Remove confirmed-dead code

- **Issues addressed:** *`_use_local()` in the LLM client is explicitly self-documented as dead code and left in place*; *`prompts/citation_validator.txt` is an orphaned prompt file with no live reader*; *`src/pipeline/query_constructor.py` has zero callers anywhere in the codebase*; *`App.css` and default Vite-scaffold assets are unused in both frontends*; *Several exported utility functions in the main frontend's `lib/utils.ts` are unused*; *`ProtectedRoute`'s `useEffect` is a no-op left over from a refactor*; *SQLite chunk-logging import is live but its only call site is fully commented out.*
- **Files/functions:** `src/llm/client.py:88-89`, `prompts/citation_validator.txt`, `src/pipeline/query_constructor.py` + `prompts/search_query_constructor.txt`, `frontend/src/App.css` + `admin-frontend/src/App.css` + unreferenced assets, `frontend/src/lib/utils.ts` (4 named functions), `frontend/src/components/auth/ProtectedRoute.tsx:8-12`, `src/ingestion/service.py:19,415-428`.
- **Approach:** Delete each, having re-confirmed zero references via repo-wide grep immediately before deletion (the audit already did this once; re-verify at implementation time in case anything changed in the interim). For `service.py:415-428`'s commented-out block: either delete the dead import + comment block outright, or if `pipeline_logger`'s chunk-logging table is actually wanted, un-comment and finish wiring it — this is the one item in this module that's a real product/data question rather than pure deletion (flagging it rather than assuming "delete" is correct).
- **Blast radius/risk:** Essentially none for the deletions (confirmed zero live references). The one open question (`service.py`'s commented block) needs a one-line answer from the team before either path is taken.
- **Verification:** `git grep` for each symbol/path immediately before deleting, confirming no new reference was added since the audit; full test suite run after removal.
- **Migration/infra needs:** None.
- **Rollback:** `git revert`; trivial.

### Module 10.2 — `DataGateway` Protocol reconciliation

- **Issues addressed:** *`get_cases()`'s typed Protocol signature is misleading and would crash a caller who trusts it*; *`DataGateway` Protocol (`base.py`) is substantially out of sync with the real `DirectGateway` implementation*; *`base.py` declares `log_generated_file` twice, with conflicting return-type annotations*; *`DataGateway` Protocol interface drift* (cross-referenced, same underlying issue).
- **Files/functions:** `src/data_gateway/base.py` (whole file) vs. `src/data_gateway/direct_backend.py`.
- **Approach:** Regenerate `base.py`'s Protocol to match `direct_backend.py`'s real signatures exactly — add the six missing methods (`check_case_access`, `get_case_assignments`, `assign_user_to_case`, `unassign_user_from_case`, `log_step`, `table_exists`), fix `create_session`/`get_ingested_files_summary`'s mismatched signatures, fix `get_cases()`'s signature to require `user_id`/`user_role` (matching reality, so a caller coding against the Protocol gets a type error instead of a runtime crash), and remove the duplicate `log_generated_file` declaration, keeping the one matching the real `-> str` return type.
- **Blast radius/risk:** None at runtime (Protocols are structural/type-checking only in Python, not enforced at runtime) — this is purely a static-analysis correctness fix. If the project runs `mypy`/`pyright` in CI (currently it doesn't, per the audit's CI findings — see Module 12.4), this fix becomes actually enforced going forward.
- **Verification:** If a type checker is added (Module 12.4), run it and confirm `base.py` and `direct_backend.py` now agree; otherwise, manual side-by-side diff of every method signature.
- **Migration/infra needs:** None.
- **Rollback:** Revert; independent.

### Module 10.3 — MCP scaffolding cleanup (cross-reference)

Already covered by Module 1.2 (*`src/mcp/config.py` + `server.js`*, *`mcp-servers/package.json`*) — not repeated here to avoid double-counting; flagging the cross-reference per the "group by file" instruction, since this is squarely a Phase 10-style dead-code item that I sequenced into Phase 1 instead because it shares a file (`src/mcp/client.py`, `admin.py::mcp_demo`) with a Critical security fix that needed to land early. No separate module needed.

---

## Phase 11 — Documentation & deployment risk

### Module 11.1 — Remaining documentation drift

- **Issues addressed:** *README's claim about `MEMORY_BACKEND` and `src/memory/conversation.py` being dead JSON-file code is itself stale and inaccurate*; *`embed_text()`/`embed_texts()` docstrings claim a 3072-dimensional return, contradicting the actual default provider*; *Verifier module's top-of-file docstring is stale relative to its own current code*; *`requirements.txt`'s header comment still names the project "TaxIQ"*; *`admin-frontend/README.md` is the entirely unmodified Vite scaffold README.*
- **Files/functions:** `README.md:446`, `src/retrieval/embedder.py:36-48`, `src/pipeline/verifier.py:10-11`, `requirements.txt:2`, `admin-frontend/README.md`.
- **Approach:** Correct each stale claim to match current code (already fully diagnosed by the audit — this is a direct-fix list, not a design question). Write a minimal real `admin-frontend/README.md` (what it is, how to run it against the backend, matching the main `frontend/README.md`'s level of detail if one exists).
- **Blast radius/risk:** None — documentation only.
- **Verification:** Manual proofreading against current code; no automated test applicable.
- **Migration/infra needs:** None.
- **Rollback:** Trivial.

### Module 11.2 — Migration/startup drift guard

- **Issues addressed:** *Alembic and the plain-SQL `migrations/` chain have drifted further than the README discloses, and `create_all()` runs unconditionally on every startup regardless of migration state.*
- **Files/functions:** `alembic/versions/*`, `migrations/*.sql`, `src/main.py:91-127` (`init_postgres`/`create_all`), `src/database/models.py:56` (`role_enum`, `create_type=False`).
- **Approach:** I'm **not** recommending reconciling Alembic's history to match the plain-SQL migrations' actual current schema in this pass — rewriting migration history that some environments may have already partially applied is higher-risk than the problem it solves (see §10, deferred). Instead, add a narrow, low-risk startup guard: before calling `Base.metadata.create_all()`, check whether the `user_role` Postgres enum type exists (a single, cheap query); if not, log a clear, specific CRITICAL ("required enum type `user_role` missing — run `migrations/006_rbac.sql` before starting the app") instead of letting `create_all()` fail with a misleading "PostgreSQL unreachable" warning that points at the wrong subsystem.
- **Blast radius/risk:** Low — this is a diagnostic improvement (better error message, same failure point), not a schema change.
- **Verification:** Cannot be verified without a live Postgres instance to actually trigger the missing-enum condition (start against a fresh DB with only Alembic's head applied, no plain-SQL migrations run, confirm the new message appears instead of the generic connectivity warning).
- **Migration/infra needs:** None (no schema change, just startup-sequence code).
- **Rollback:** Revert the guard; independent.

### Module 11.3 — Dependency pinning

- **Issues addressed:** *All Python dependencies are floor-pinned (`>=`) only — no upper bounds, no lockfile.*
- **Files/functions:** `requirements.txt`.
- **Approach:** Generate a lockfile (`pip freeze > requirements.lock.txt` from a known-working environment, or migrate to `pip-tools`/`poetry` if the team wants a more maintainable long-term solution than a raw freeze — flagging this as a choice for the team, not deciding it here) and document that `requirements.txt` remains the floor-pinned intent file while the lockfile is what CI/deployment actually installs from.
- **Blast radius/risk:** Low to adopt, but note explicitly: pinning to whatever versions happen to be installed *right now* does not mean those versions are individually verified compatible with everything else — this fix makes builds reproducible, it does not itself audit for latent version-mismatch bugs. That's a separate, larger effort out of scope here.
- **Verification:** CI installs from the lockfile and the full test suite still passes.
- **Migration/infra needs:** Update `.github/workflows/ci.yml` to install from the new lockfile.
- **Rollback:** Revert to `requirements.txt`-only installs.

### Module 11.4 — CI hardening (scoped narrowly)

- **Issues addressed:** *CI runs `pytest` and frontend builds only — no linting, type-checking, or dependency/security scanning of any kind*; *No coverage measurement tooling exists anywhere in the project.*
- **Files/functions:** `.github/workflows/ci.yml`, `requirements.txt`/`pytest.ini`.
- **Approach:** Add `pytest-cov` and a `--cov` flag to the existing `pytest` CI step (report-only, not a hard coverage gate, to avoid blocking merges on a threshold nobody's agreed to yet) — this alone makes every subsystem's real test coverage visible without requiring anyone to manually infer it, as the audit itself had to. Add a dependency-vulnerability scan (`pip-audit` for Python, `npm audit` for both frontends) as a non-blocking CI step (report findings, don't fail the build yet — that's a follow-up once the team has triaged whatever it finds). I'm explicitly **not** proposing adding `mypy`/`ruff`/ESLint-as-a-blocking-gate in this pass, since retrofitting lint compliance across an existing, un-linted codebase is a separate, potentially large effort (see §10 deferred) — landing the tooling in report-only mode first lets that be scoped and prioritized deliberately rather than as a side effect of this plan.
- **Blast radius/risk:** Low — all additions are report-only/non-blocking in this pass.
- **Verification:** CI run shows a coverage report and a dependency-audit report artifact; neither blocks merge yet.
- **Migration/infra needs:** New CI steps/dependencies (`pytest-cov`, `pip-audit`).
- **Rollback:** Remove the CI steps; no code impact.

---

## §9 — Disagreements and decisions needed (not code bugs)

1. **The graph contamination is worse than described, not overstated.** Covered in Phase 3.0 — the eval script wipes the *entire* graph on every run, not just writes fixtures alongside real data. This isn't a disagreement with severity (Critical is still correct, if not an understatement) but a correction to the mechanism, which changes the fix's shape (isolation + a hard runtime guard, not just a cleanup script).

2. **Graph-review queue's cross-case bypass** (*Graph-review queue and confirm/reject bypass `case_assignments` entirely for any global supervisor-or-above*) — `issues.md` itself flags this as "suspected-intentional... may be an intentional tradeoff." Having read `graph_review.py` in full, the module's docstring does argue cross-case matching is the deliberate point of the queue (finding the same person across cases is the whole feature). I don't think this is a bug to silently "fix" by adding case-scoping that would defeat the queue's purpose — but I also don't think it should ship unexamined. **This needs a product decision, not a code change**: is cross-case entity-resolution review, by design, exempt from per-case confidentiality? If yes, document that explicitly (in the module's docstring and in `docs/graph_schema.md`) as a deliberate, reviewed tradeoff rather than an implicit gap. If no, it needs the same `case_assignments`-scoping treatment as everything else, which would require redesigning what the review queue even shows (cross-case matches would need to surface to *both* cases' assigned reviewers, or to a dedicated cross-case-reviewer role). I've deliberately not built a module for this in the plan above pending that decision.

3. **Admin Audit Logs role gate — which layer is wrong?** (*the frontend's role gate is looser than the backend's*) — `issues.md` correctly identifies the mismatch but doesn't resolve which side is "right." I implemented Module 9.1 assuming the backend's `platform-admin`-only restriction is authoritative (narrower/safer default), but the product's own role model (`supervisor` is described elsewhere as "the role the product itself designates for reviewing chain-of-custody/audit data") suggests the *frontend's* looser model might be the intended one and the backend is the actual bug. **This needs a one-line product answer** ("should a supervisor be able to see audit logs: yes or no") before Module 9.1/5.x's audit-log-adjacent work is finalized — I sized the module for the narrower (revert-frontend) direction since it's the safer default to ship first, with the alternative (widen backend) as a small, clearly-scoped follow-up either way.

4. **The failing CI test** (Module 0.1) — resolved above as "code is correct, test is stale," but flagging again here since the instructions asked for this to be surfaced explicitly as a decision, not assumed: if the team disagrees and actually wants the Gemini retry-fallback restored, that's a product-scope reversal, not a bug fix, and should be treated as new work, not folded into "fixing" this test.

---

## §10 — Deliberately deferred (not fixed in this plan, with reasons)

- **Rich, sanitized markdown rendering for `MessageBubble.tsx`** — real Medium-severity readability defect, but fixing it properly means adding a markdown-rendering dependency and doing an XSS-safety review (retrieved case documents and attachments are exactly the untrusted-content class this same audit flags elsewhere) — meaningfully larger scope than a bug fix. Recommend as a separate, explicitly-scoped follow-up once this plan's security work has landed (rendering untrusted content safely should come *after* the prompt-injection/untrusted-content hardening context established by Phase 6, not before).
- **Full responsive/mobile-tablet redesign of the chat layout** — real Medium (High if field-tablet use is expected) finding, but the current layout is architecturally fixed-width by design (three-column: sidebar/chat/pipeline panel) — a real fix is a layout redesign, not a CSS patch, and needs a product decision on whether tablet/field-officer use is actually a target platform before investing in it.
- **Fully reconciling Alembic's migration history with the plain-SQL `migrations/` chain** — the schema drift is real (Alembic's head is missing most of the actual production schema), but rewriting migration history that may already be partially applied in real deployments is a higher-risk operation than the diagnostic guard proposed in Module 11.2. Recommend a separate, carefully-sequenced migration-consolidation project, ideally coordinated with whoever manages the actual deployed environments' current migration state (which this audit has no visibility into).
- **Retroactively fixing already-collided `doc_id`s / already-orphaned Postgres rows from before Module 4.1/4.2 land** — the code fixes prevent new occurrences; finding and repairing already-corrupted historical data needs a live-data audit script (sketched in Module 4.1) run by someone with production DB access, and a case-by-case human decision about which case's data is authoritative where a collision is found. Not something to automate blindly.
- **Blocking on `mypy`/`ruff`/ESLint as CI gates** — see Module 11.4; landing them as blocking gates on an existing, never-linted codebase would surface a large, unscoped backlog of unrelated findings as a side effect of this plan. Add them in report-only mode now; decide separately whether/when to make them blocking.
- **A full dependency compatibility audit (beyond pinning to current-known-working versions)** — see Module 11.3; pinning ≠ verifying every package's current version is actually the best/safest choice. Out of scope here.
- **`docx_loader._iter_blocks`'s O(n²) rescan** — included as Module 7.4 item (4) since it was cheap to bundle, but flagging that on its own it's genuinely Low severity per the audit's own text ("negligible for short documents") — if review bandwidth is tight, this specific item is the safest one to cut from Phase 7 without meaningfully changing the plan's risk profile.
- **The optional synthetic-case-ID rename in the eval roster CSV** (Module 3.1, step 5) — explicitly marked optional in that module; the graph-namespace isolation already fully solves the contamination risk without it.

---

## §11 — Relative effort per phase

| Phase | Size | Why |
|---|---|---|
| 0 — Foundations | S | Test rewrite + config validation logic; no schema changes. |
| 1 — Independent Critical fixes | M | Three unrelated subsystems (export sanitization, MCP role provisioning, auth hardening); MCP least-privilege role needs real DB access to provision/verify. |
| 2 — RLS/AGE redesign | **XL** | The largest, riskiest item in the plan — a new migration, a new cross-cutting FastAPI dependency, reordering security-critical control flow in `orchestrator.py`, and a new AGE case-scoping chokepoint. Needs live-Postgres verification before merge; budget real review time, not just implementation time. |
| 3 — Graph contamination | M | Isolation change is small (parameter threading); the purge is procedurally heavy (reconciliation + dry-run + backup discipline) even though the code itself is simple. |
| 4 — Ingestion/retrieval integrity | M | Four largely-independent sub-fixes, each small, but touching a shared, sensitive write path (case-scoped evidence). |
| 5 — RBAC app-layer hardening | M | Mostly small logic changes, but Module 5.1's station-scoping needs a new column + a real-world data backfill, which is the slow part. |
| 6 — LLM pipeline correctness | S/M | Mostly mechanical (shared JSON utility, exception guard, regex loosening); the max_tokens fix needs a `git log` rationale-check before picking numbers. |
| 7 — Ingestion performance/safety | S/M | Each item is small and independent; the MIME-validation dependency choice (Module 7.2) is the only real decision point. |
| 8 — Main frontend | S/M | Mostly small, well-scoped React fixes reusing existing patterns (`fresh: true`, existing `confirm` usage). |
| 9 — Admin frontend | M | Larger surface area (many pages) but every fix is mechanical/additive; Module 9.3's sweep is the bulk of the volume, not the difficulty. |
| 10 — Dead code/contract cleanup | S | Deletions and a Protocol sync; genuinely low effort. |
| 11 — Docs/deployment/CI | S | Documentation edits + narrow, additive CI steps. |

---

## §12 — One more file-grouping note

Per the instruction to avoid touching the same file across unrelated phases repeatedly: `src/pipeline/orchestrator.py` is touched in **Phase 2** (RLS/cross-case context activation — lines ~187-189, ~428-429, ~994-1100) and separately in **Phase 4** (`where_clause` construction — lines ~872-874, ~1248-1250) and **Phase 6** (RAG exception guard — lines ~1349-1401). These are three genuinely unrelated sections of a very large file with no logical dependency between them (security-context plumbing vs. retrieval-filter construction vs. error handling) — I considered merging them into one "orchestrator.py sweep" phase but decided against it, since their *dependencies* point in different directions (the RLS work depends on Phase 1 being done first for review bandwidth; the retrieval-filter fix depends on Phase 4's ID-generation redesign; the RAG guard depends on nothing and could land any time). Forcing them into one phase would violate the "dependency first" ordering rule to satisfy the "group by file" rule; where the two conflict, I followed dependency ordering and left this note instead.
