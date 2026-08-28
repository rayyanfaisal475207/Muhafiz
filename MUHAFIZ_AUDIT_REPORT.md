# Muhafiz Audit Dossier

**QA · SECURITY · RAG · PERFORMANCE AUDIT**
**HEAD:** `d755b0f` (6 teammate commits pulled & re-verified) · **Date:** 28 Aug 2026

A full-implementation review of the Islamabad Police evidence-intelligence platform — run against live code, a restored database, and real model calls, not documentation.

| | |
|---|---|
| **Scope** | 67-section stress protocol |
| **Backend** | Python 3.12 · FastAPI · PG16+AGE |
| **Method** | Live execution + source verification |
| **Auditor role** | QA / AppSec / RAG eval |
| **Database** | Fresh restore from 2026-08-27 dump |

---

## Verdict: GO WITH CONDITIONS

**The application-layer security is genuinely strong** — cross-case isolation, CSRF, RBAC/ABAC, prompt-injection resistance and grounded abstention all held under live fire. The conditions are two deployment-configuration facts and one missing dependency:

- **RLS is completely inert** because the app connects as a Postgres superuser
- A **missing `asteval` package** breaks Groq streaming on a clean install
- A shared `.env` carries the public JWT default

None is an application-logic flaw; all are release-blockers until fixed. **No verified cross-case evidence leak, auth bypass, or forged-admin path was found.**

---

## 0. Post-Audit Remediation Update (28 Aug 2026)

All three P0 blockers and one of the two P1 findings have been fixed, tested, and merged to `main` on the same day as the audit:

| Finding | Status | Fix | Verification |
|---|---|---|---|
| F-01 — RLS inert (superuser bypass) | ✅ **FIXED** — `bc1effd` | Applied `migrations/015_app_least_privilege_role.sql`, created `muhafiz_app` (no superuser/BYPASSRLS), repointed local `DATABASE_URL` at it | `scripts/verify_app_role.py` — all checks pass; `tests/test_rls_integration.py` — 4/4 pass (was 2/4 failing) |
| F-02 — missing `asteval` dependency | ✅ **FIXED** — `bc1effd` | Added `asteval>=1.0.0` to `requirements.txt` / `asteval==1.0.10` to `requirements.lock.txt` | Clean import via `src.llm.tools`; Groq streaming path unblocked |
| F-04 — domain allowlist substring bypass | ✅ **FIXED** — `fc1be82` | New `src/pipeline/url_safety.py::is_domain_allowed()` (hostname-parsed exact/suffix match), replacing `domain in url` at both call sites (`orchestrator.py`, `harness/tools/web.py`) | 23 new/updated tests, including all 7 of this report's attack URLs — full 93-test suite passes |
| F-03 — shared `.env` public JWT default | ⚪ **Deferred, downgraded** | No code fix applies — `.env`/`SHARE/` are gitignored and `.env.example` already documents the risk. Distribution confirmed team-internal only, with a planned secret reset | Owner to confirm the team-shared copy is actually rotated; third-party API key rotation (Groq/Gemini/Tavily/Muhafiz) remains manual, at each provider's dashboard |
| F-05 — attachment metadata leak before sessions row exists | ✅ **FIXED** — `72043da` | Fall back to the attachment rows' own `user_id` (set at upload time) when there's no `sessions` row to check ownership against, on both the list and upload endpoints | 3 new tests reproducing the exact audit scenario (cross-user list denied, own pre-session list allowed, cross-user upload denied); full suite green |
| F-06 — unvalidated UUID casts → 500 | ✅ **FIXED** — `4bd9cbd` | New `src/api/validation.py::validate_uuid_field()` at every entrypoint (chat, attachment upload/list/delete); `jwt.py`'s `except JWTError` now also catches `ValueError` | 8 new tests, including both live reproductions end-to-end (forged non-UUID JWT `sub` → 401, malformed `session_id` → 422) |
| F-07 — unbounded admin pagination | ✅ **FIXED** — `165bb11` | `Query(ge=1, le=1000)` added to all 7 admin `limit` params (`errors`, `kb/jobs`, `audit-logs`, `runs`, `files`, `mcp-calls`, `users`) | 21 new parametrized tests (excessive limit → 422, boundary → 200, default → 200) across all 7 routes |
| F-08 — NULL-station admin bypass | ✅ **FIXED** — `863533b` | Changed the NULL-`police_station` fallback from unrestricted access to a 403 deny, matching the fail-closed posture of every other ABAC check in this codebase | Live DB check confirmed the one remaining NULL-station account is a synthetic smoke-test fixture, not a real officer — nothing left to backfill. Existing + new tests updated/added, full suite green |
| F-09 — rate limiter keyed on raw IP | ✅ **FIXED** — `e93e79a` | New allowlist-gated `_rate_limit_key()`: `X-Forwarded-For` only honored when `TRUST_PROXY_HEADERS=true` AND the immediate peer is in `TRUSTED_PROXY_IPS`; default behavior unchanged | 5 new tests, including the anti-regression case (an untrusted peer's forged header is ignored even with the flag on) |
| F-10 — minor correctness & hygiene (5 items) | ✅ **4/5 FIXED** — `6c6caa1` | Nonexistent-case delete now 404s; `/health` reads `app.version` directly; `RUN.md` port refs corrected to `:8001`; `npm audit fix` run in both frontends (0 vulnerabilities) | 2 new tests; both frontend builds + Vitest suites (23/23, 34/34) verified green. Docling test flakiness item **not fixed** — not reproducing after 5+ full-suite runs, so no speculative fix applied |

Every finding from this audit is now fixed except F-03 (deferred, owner decision) and F-10's Docling flakiness item (open, non-reproducing).

---

## 0.1 Further Remediation — Hypothesis Ledger, Cross-Lingual Graph Matching, and a Post-Audit Discovery (28 Aug 2026, same day)

Beyond the 10 numbered findings, §4's Hypothesis Ledger carried 7 more VERIFIED defects that were never given an F-number and so were untouched by the work above. All 7 are now fixed, plus one new capability and one significant bug this work surfaced that was outside the original audit's scope entirely:

| Item | Status | Fix | Verification |
|---|---|---|---|
| Hyp #1 — frontend/backend password-length mismatch | ✅ **FIXED** — `ba79491` | `RegisterPage.tsx` `minLength` 8→12, matching the backend's real minimum, plus a visible helper hint | 2 new tests; full frontend suite 25/25 |
| Hyp #5 — `case_assignments.role` no API allow-list | ✅ **FIXED** — `ddc2c86` | `CaseAssignmentCreate.role` → `Literal["investigator","supervisor","station-admin","platform-admin"]` | 8 new tests (bogus role → 422, each real value → 200) |
| Hyp #6/#7 — `preferred_language`/`llm_mode` unrestricted strings | ✅ **FIXED** — `6ca10d9` | Both → `Literal[...]` matching the frontend's own `<select>` values | 7 new tests (no prior profile tests existed at all) |
| Hyp #12 — Data-Quality/Extraction-Coverage unreachable | ✅ **FIXED** — `ac12896` | New `_DATA_QUALITY_TRIGGER_PATTERNS` (English/Urdu-script/Roman-Urdu) wired into `classify_to_subagent()` | 9 new tests; harness compliance suite 58/58 |
| Hyp #13/#14 — RLS integration tests and frontend Vitest never ran in CI | ✅ **FIXED** — `200a247` (PR #5) | New `rls-integration` CI job (Postgres+AGE service container, full migration chain, live RLS tests); `npm test -- --run` added to the frontend job | **Actually watched live in CI**, not just merged blind — first push failed (`ModuleNotFoundError: No module named 'src'` in Alembic), diagnosed from real logs, fixed, re-run confirmed RLS tests genuinely execute (4/4 passed, not skipped) before merging |
| **New** — cross-lingual graph name matching | ✅ **SHIPPED** — `18785e8` | RAG retrieval already handled this (`cross_script_variant.py`, pre-existing); the graph side (`graph_retriever._find_seed_nodes()`) did a raw script-blind `CONTAINS` on `canonical_name`. Reused `entity_resolution._consonant_skeleton()` (already built for ingestion-time cross-script merges, never reached query time) — precomputed at write time, matched at query time | 5 new unit tests (both directions + a false-positive guard) **and a live, real-data end-to-end proof**: backfilled `name_skeleton` on 1,459 real nodes; a real Urdu-stored node (`نعیم اختر`, case `fir-891-24`) is now found by the English query `"Naeem Akhtar"` — confirmed the *old* logic found 0 results for the identical query |
| **Post-audit discovery** — `muhafiz_app` had zero AGE graph-schema access | ✅ **FIXED** — `8db03a5` | Found live while verifying the item above: every graph call under `muhafiz_app` failed (`type agtype does not exist`). Two gaps, both required: (1) `docker-compose.yml`'s Postgres never set `shared_preload_libraries=age`, so `LOAD 'age'` — superuser-only — had nothing to fall back on; (2) migration 015 granted DML on relational tables but never touched `ag_catalog`/`evidence_graph`/`evidence_graph_eval` at all. New migration 031 grants `USAGE` + DML via `ALTER DEFAULT PRIVILEGES` (so every future AGE-created label is covered automatically, unlike 009/015's per-table style) | Live: a basic Person read and a full `versioning.write_node()` round-trip both failed identically pre-fix, succeeded post-fix; `verify_app_role.py` and `test_rls_integration.py` re-run live afterward, unaffected (still all-pass) |

**Severity note on the post-audit discovery:** this means F-01's own fix (repointing `DATABASE_URL` at `muhafiz_app`) had an unintended side effect this audit's own verification never caught, because neither `verify_app_role.py` nor `test_rls_integration.py` exercises the graph/AGE path at all — both are relational-only. Every XGRAPH/GRAPH/GRAPH_HYBRID route and every ingestion-time entity-resolution write would have been silently broken in any deployment that completed migration 015 without also applying 031. Worth treating as a reminder that a least-privilege migration needs a smoke test against *every* subsystem the app touches, not just the one the migration was written for.

---

## 1. Production Readiness Scorecard

Scored 0–10 against verified evidence. A single class of defect can cap a category regardless of other strengths — RLS/data-isolation is capped by the inert-backstop finding even though its *design* is correct and its own integration tests pass under a least-privilege role.

| Category | Score | Category | Score |
|---|:---:|---|:---:|
| Authentication | 9 | Agent harness | 8 |
| Authorization (app layer) | 9 | Security (overall) | 6 |
| RLS / data isolation | **3** | Citations & hedging | 8 |
| Groundedness | 9 | RAG retrieval* | 6 |
| Prompt-injection defense | 9 | Code quality | 8 |
| Performance | 6 | Observability | 8 |
| Deployment config | **4** | Test coverage (77%) | 8 |

> **Overall: 6.5 / 10 — not averaged mechanically.** The application code would score ~8; the inert RLS backstop plus the broken-on-clean-install dependency hold the deployable figure down until both are corrected. *RAG retrieval could not be fully scored: the shared dump ships no ChromaDB vectors, so semantic recall/precision metrics are `NOT EXECUTED`.

---

## 2. Findings (ranked by severity)

**Legend:** 🔴 Critical / P0 · 🟠 High / P1 · 🟡 Medium / P2 · 🟢 Low / P3

---

### 🔴 F-01 — RLS is silently inert: the app connects as a Postgres superuser
**Critical · P0 · ✅ FIXED `bc1effd` (28 Aug 2026) — see §0**

All five RLS policies (`cases, documents, sessions, messages, pipeline_runs`) are correctly defined and `FORCE`-enabled, but `DATABASE_URL` connects as `postgres`, which has `SUPERUSER` + `BYPASSRLS`. Superusers bypass row-level security unconditionally. The least-privilege `muhafiz_app` role from migration 015 does not exist in the shipped database, nor does `muhafiz_mcp_readonly`.

**Proof (both directions):** the project's own `tests/test_rls_integration.py` — **4 passed** under a least-privilege role I created, **2 failed** under the deployed `postgres` role, with a Case-A row visible to a request scoped to a different case. `scripts/verify_app_role.py` reports *"postgres has BYPASSRLS — RLS policies remain inert."* App-layer authorization is unaffected and is today's real protection (see §3).

---

### 🔴 F-02 — Missing `asteval` dependency breaks all Groq streaming on a clean install
**Critical · P0 · ✅ FIXED `bc1effd` (28 Aug 2026) — see §0**

`src/llm/tools.py:3` does `from asteval import Interpreter`, and `src/llm/client.py:378` imports that module unconditionally inside `_stream_groq` — yet `asteval` appears in neither `requirements.txt` nor `requirements.lock.txt`. Every Groq streaming call raises `ModuleNotFoundError`. CI never caught it because the suite mocks all external LLM calls.

**Live impact:** under concurrency 8 the chat completion rate was **0 / 8**. After I installed `asteval` into the venv only (requirements untouched), the identical test ran **8 / 8**, then **16 / 16**. Fix: add `asteval` to requirements and regenerate the lockfile.

---

### 🟠 F-03 — Shared `.env` ships the public default JWT secret
**High · P1 · ⚪ DEFERRED (28 Aug 2026) — see §0**

The SHARE `.env` sets `ENVIRONMENT=development` and keeps `JWT_SECRET_KEY=your-secret-key-for-dev`. Anyone with the folder can forge a valid JWT for any user, including platform-admin. The startup guard *does* work — with `ENVIRONMENT=production` it correctly raises a critical error and `src/main.py:190` refuses to boot — so this is a foot-gun of the shared file, not a code defect. It also carries live Groq/Gemini/Tavily API keys in a distributable folder.

**Verified:** `validate_config()` returns 1 critical error the instant `ENVIRONMENT` is flipped to `production`. Rotate the shared keys; require a real secret before any non-dev deploy.

---

### 🟠 F-04 — Web-search domain allowlist is bypassable via substring match
**High · P1 · ✅ FIXED `fc1be82` (28 Aug 2026) — see §0**

`_filter_allowed_domains()` in `src/pipeline/orchestrator.py:521` (and a verbatim port in `harness/tools/web.py`) filters with `domain in url` — a raw substring test. Any URL containing an allowed domain anywhere passes.

**Tested — 6 of 7 attack URLs slipped through:** `dawn.com.attacker.tld`, `evil.example/?ref=gov.pk`, `evil/gov.pk/path`, `#dawn.com`, `?to=dawn.com`, `192.0.2.1/gov.pk`. Exploitation requires the search provider to return a hostile URL (content-injection, not direct control), bounding severity. Fix: parse the hostname, match exact-or-suffix on a dot boundary.

---

### 🟡 F-05 — Attachment metadata leaks across users when the session row doesn't yet exist
**Medium · P2 · ✅ FIXED `72043da` (28 Aug 2026) — see §0**

`src/api/attachments.py:170` guards ownership with `if session and session.get("user_id") != …`. When the session row is absent — the normal case, since attachments can be uploaded before the first chat message creates the session — the check is skipped and any user can list another user's attachment *metadata*.

**Proven:** invB read invA's attachment list (HTTP 200, 1 item) before the session existed; after one chat message created the session row, the same call correctly returned 403. Content is safe — `extracted_text` is stripped at line 156 and never in any API schema — so this leaks filename/size/char-count/timestamps only. Fix: deny when `session is None`.

---

### 🟡 F-06 — Unvalidated identifiers reach a UUID cast → HTTP 500 (a repeated pattern)
**Medium · P2 · ✅ FIXED `4bd9cbd` (28 Aug 2026) — see §0**

Two endpoints accept a string where a UUID is required, then cast it and let `ValueError` escape as a 500:

- **JWT `sub`:** `src/auth/jwt.py:72` — `UUID(sub)` sits inside a `try` that only catches `JWTError`. A validly-signed token with a non-UUID `sub` → 500 (should be 401). *Requires a valid signature, so not an auth bypass.*
- **Chat `session_id`:** `src/main.py:257` types it as `str`; a malformed value hits `uuid.UUID()` → 500 (should be 422).

**Both reproduced live.** Contrast: the file-download route validates and returns a clean **400** for a malformed id — the correct pattern. Fix: type these as `UUID` / catch `ValueError`.

---

### 🟡 F-07 — Unbounded admin pagination: `audit-logs` returns 149k rows
**Medium · P2 · ✅ FIXED `165bb11` (28 Aug 2026) — see §0**

Every admin `limit` is a bare `int` with no upper bound (`src/api/admin.py` lines 153, 219, 227, 402, 415, 421, 539). `/api/admin/audit-logs?limit=99999999` returned **149,502 rows in 5.9 s** — a memory/serialization DoS vector. Other endpoints are naturally capped by small tables. Platform-admin-only, which bounds it. Fix: `Query(le=…)` ceilings.

---

### 🟡 F-08 — Station-admin with NULL `police_station` bypasses station scoping
**Medium · P2 · ✅ FIXED `863533b` (28 Aug 2026) — see §0**

`src/api/case_assignments.py:50` falls back to unrestricted access (logged) when the caller's `police_station` is NULL. Migration 012 adds the column with no backfill, so this is the default state. In the shipped DB, **1 of 2 station-admins is NULL**. Fix: backfill stations, then deny on NULL.

---

### 🟡 F-09 — Rate limiting keys on raw remote address — collapses behind a proxy
**Medium · P2 · ✅ FIXED `e93e79a` (28 Aug 2026) — see §0**

`src/auth/routes.py:23` uses `get_remote_address` with no `X-Forwarded-For` handling or trusted-proxy config. Behind a reverse proxy all users share one IP; one client can exhaust the limit for everyone. The limits themselves are exact (login 10/min, case-create 20/min — both verified to the boundary).

---

### 🟢 F-10 — Minor correctness & hygiene
**Low · P3 · ✅ 4/5 FIXED `6c6caa1` (28 Aug 2026) — see §0**

- **Delete nonexistent case → `{"status":"deleted"}`** (`cases.py` `delete_case`) and writes a misleading audit event. Verified as platform-admin. — ✅ FIXED: existence check added, 404s before acting or logging.
- **`/health` version mismatch:** app metadata `1.0.0` vs `/health` body `0.1.0`. — ✅ FIXED: `/health` now reads `app.version` directly.
- **4 high-severity npm advisories** in `react-router-dom` (CSRF-bypass advisory applies to RSC mode, which this app doesn't use), plus nanoid/PostCSS build-time — `npm audit fix` available. — ✅ FIXED: `npm audit fix` run in both `frontend/` and `admin-frontend/`, 0 vulnerabilities in each; both builds and full Vitest suites (23/23, 34/34) verified green.
- **`RUN.md` vs Vite proxy port mismatch:** both frontends proxy to `:8001` while `RUN.md §` starts the backend on `:8000` — following RUN.md verbatim leaves the UI unable to reach the API. — ✅ FIXED: every port reference in `RUN.md` updated to `:8001` to match the (agreeing) frontend proxy configs.
- **Docling PDF-loader tests flaky under the full suite** (3 failures that vanish in isolation and on re-run) — ML warm-up ordering, not a product bug. — ⚪ NOT FIXED: the full backend suite was run 5+ times during this remediation pass with zero Docling failures; not currently reproducing, so no speculative fix was applied. Remains open.

---

## 3. What Passed (held under live fire)

These are the controls that make the platform trustworthy for police evidence. Each was exercised at runtime against the synthetic multi-user, multi-case fixture — not read from the source.

### ✅ Cross-case isolation
- Every unauthorized case read/update/delete → **403**, across chat, REST, and file download.
- A global supervisor assigned to a case as *investigator* is correctly denied per-case supervisor rights — **ABAC does not inherit global role**.
- Investigator XGRAPH/XAGG/XNETWORK → **PermissionError**, fail-closed, with an `authorization_violation` audit row. Unknown/empty/case-variant roles all denied.

### ✅ AI safety & grounding
- Zero-evidence query → **explicit abstention**, no fabricated facts or citations.
- **Indirect prompt injection failed:** a poisoned attachment's "reveal all cases / print the JWT secret" block was ignored while the legitimate fact was extracted.
- 4/4 direct injections refused — no system-prompt or secret disclosure, no SQL execution.
- Exhausted RAG **abstains** rather than silently invoking web search.

### ✅ Auth & session
- Double-submit CSRF enforced on every mutating verb; correct 403-before-401 ordering.
- Session ownership strict — **even platform-admin gets 403** on another user's session.
- Account-enumeration resistant (identical 401 for real vs unknown email).
- Cookies: `access_token` HttpOnly, both env-gated `Secure`, `SameSite=lax`.

### ✅ Injection surfaces
- No f-string SQL anywhere in `src/`.
- **Cypher scoping holds:** `scoped_cypher()` refuses unscoped templates and empty case_ids; an injection payload in `case_id` returned 0 rows (parameterized).
- No `dangerouslySetInnerHTML` / raw-HTML markdown — model output renders as escaped text. **Stored-XSS surface closed.**
- MCP fails safe (502) with the read-only role absent — no superuser fallback.

### ✅ Harness & routing
- Supervisor maps routes correctly; **DENIED never collapses to EMPTY/ABSTAINED** in any cross-case tool.
- Cross-case-shaped query with within-case scope is **demoted** to Semantic Search, not bypassed.
- **58 compliance tests pass** (the CI-blocking enforcement suite).

### ✅ Data integrity & audit
- Each chat turn persists exactly 1 user + 1 assistant message → 1 pipeline_run. No duplicates/orphans.
- Audit coverage is broad (`authorization_violation`, `graph_traversal_cross_case`, review decisions) with **zero secret patterns** in log bodies.
- Config guards fire: chunk-overlap, air-gap, environment, JWT default.

---

## 4. Hypothesis Ledger (15 historical claims, re-tested against HEAD)

**Legend:** 🔴 **VERIFIED** — defect present · 🟢 **NOT REPRODUCED** — fixed / never real · ⚪ **NOT EXECUTED**

| # | Hypothesis | Status | Evidence |
|---|---|---|---|
| 1 | Frontend allows 8-char password, backend requires 12 | 🔴 VERIFIED — ✅ **FIXED** `ba79491` | `RegisterPage.tsx:68` vs `routes.py:38`; see §0.1 |
| 2 | Non-UUID JWT `sub` → 500 not 401 | 🔴 VERIFIED — ✅ FIXED | Reproduced live; see F-06 |
| 3 | NULL-station station-admin bypasses station match | 🔴 VERIFIED — ✅ FIXED | 1/2 live station-admins NULL; see F-08 |
| 4 | Admin Vite proxy → 8001 while backend defaults 8000 | 🟢 NOT REPROD. — ✅ FIXED | Both proxies → 8001; but `RUN.md` said 8000 — see F-10 |
| 5 | `case_assignments.role` lacks API allow-list | 🔴 VERIFIED — ✅ **FIXED** `ddc2c86` | Free string at schema vs DB enum; see §0.1 |
| 6 | `preferred_language` unrestricted server string | 🔴 VERIFIED — ✅ **FIXED** `6ca10d9` | XSS/arbitrary accepted; downstream impact low; see §0.1 |
| 7 | `llm_mode` unrestricted server string | 🔴 VERIFIED — ✅ **FIXED** `6ca10d9` | Accepted verbatim; degrades to default, no crash; see §0.1 |
| 8 | Deleting nonexistent authorized case returns success | 🔴 VERIFIED — ✅ FIXED | `{"status":"deleted"}` live; see F-10 |
| 9 | Some admin pagination `limit` unbounded | 🔴 VERIFIED — ✅ FIXED | 149,502 rows in 5.9s; see F-07 |
| 10 | Health version ≠ app metadata version | 🔴 VERIFIED — ✅ FIXED | `1.0.0` vs `0.1.0`; see F-10 |
| 11 | Timeline Building unreachable from classification | 🟢 NOT REPROD. | Reachable via trigger patterns; stale docstring |
| 12 | Data-Quality unreachable from classification | 🔴 VERIFIED — ✅ **FIXED** `ac12896` | No trigger patterns; fell through to Semantic Search; see §0.1 |
| 13 | RLS integration tests skipped by default CI | 🔴 VERIFIED — ✅ **FIXED** `200a247` | 4 skips without `RUN_POSTGRES_TESTS`; see §0.1 |
| 14 | Frontend tests exist but aren't run in CI | 🔴 VERIFIED — ✅ **FIXED** `200a247` | `ci.yml` ran `build` only, never `test`; see §0.1 |
| 15 | Reranker text-remap ambiguous for duplicate text | 🟢 NOT REPROD. | Order-consumed queue disambiguates; documented |

---

## 5. Test & Load Results (executed vs blocked)

| Metric | Result |
|---|---|
| Main pytest suite | **PASS** (~5 skips: live-PG & slow-marked) |
| Harness compliance tests | **58** — all pass (CI-blocking) |
| RLS integration tests | **4 / 4** pass under least-privilege role |
| Backend line coverage | **77%** · 12,647 statements |
| Frontend Vitest | **57** (23 chat + 34 admin) — both build clean |
| Concurrent chat streams | **16 / 16** completed (after F-02 fix) |

### Load stages

| Load stage | Conc | Completed | p50 | p95 / dur | Note |
|---|:---:|:---:|:---:|:---:|---|
| A — `/health` | 100 | 100% | 595ms | 1127ms | 0 errors; peak throughput ~10 conc |
| A — `/api/auth/me` | 100 | 100% | 655ms | 1358ms | 0 errors, graceful degrade |
| A — `/api/cases/` | 100 | 100% | 884ms | 1383ms | RBAC-filtered, 0 errors |
| B — chat SSE (pre-fix) | 8 | 0% | — | — | All failed — F-02 asteval |
| B — chat SSE (post-fix) | 8 | 100% | 54s | 55s | Linear scaling; TTFE 1.2s |
| C — chat SSE (post-fix) | 16 | 100% | 57s | 58s | No errors; recovered to healthy |

> **NOT EXECUTED:** full RAG retrieval metrics (Recall@K, MRR, nDCG) and entity-resolution precision/recall — the shared dump ships Postgres state only, so ChromaDB was empty (534 docs / 793 fulltext chunks in PG, **0 vectors** in Chroma). Semantic retrieval is non-functional on this restored environment until documents are re-embedded. Multi-store consistency itself is therefore a real finding (dump doesn't carry the vector store).

---

## 6. Remediation Plan (ordered by urgency)

| Pri | Action | Files | Risk if deferred |
|---|---|---|---|
| **P0** | Repoint `DATABASE_URL` at `muhafiz_app`; apply migrations 009 & 015; verify with `verify_app_role.py` | `.env` · `migrations/` | RLS backstop entirely absent in prod |
| **P0** | Add `asteval` to requirements + lockfile | `requirements*.txt` | Groq streaming dead on clean install |
| **P0** | Rotate the shared secret & API keys; require real JWT secret pre-deploy | `.env` | Forgeable admin JWTs; leaked keys |
| **P1** | Hostname-parse the web domain allowlist (exact/suffix on dot) | `orchestrator.py` · `tools/web.py` | Content injection past sovereignty guard |
| **P1** | Deny attachment list when `session is None` | `api/attachments.py:170` | Cross-user metadata disclosure |
| **P1** | Run `npm run test` + RLS integration in CI; add frontend/E2E gate | `.github/workflows/ci.yml` | Regressions merge unblocked |
| **P2** | Type `session_id`/`sub` as UUID; bound admin `limit`; backfill stations | `main.py` · `jwt.py` · `admin.py` | 500s on bad input; pagination DoS |
| **P2** | `X-Forwarded-For` + trusted-proxy for rate limiting | `auth/routes.py` | Shared-IP limit exhaustion |
| **P3** | Existence check on delete; align health version; `npm audit fix`; reconcile RUN.md port | `cases.py` · `main.py` | Misleading responses / stale docs |

---

*MUHAFIZ AUDIT DOSSIER · HEAD `d755b0f` (6 teammate commits pulled & re-verified) · fresh DB restored from 2026-08-27 dump*

*Method: live execution against a running FastAPI backend, real Groq calls, Postgres 16 + Apache AGE 1.5, synthetic multi-role fixture · no production data used · no secrets reproduced*

*All synthetic test data removed post-audit · working tree unmodified (0 tracked files changed) · scores reflect verified evidence, not documentation claims.*
