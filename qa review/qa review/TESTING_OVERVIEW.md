# TESTING_OVERVIEW.md

> Sources: [pytest.ini](../pytest.ini), [tests/](../tests/), [tests/README.md](../tests/README.md),
> [tests/conftest.py](../tests/conftest.py), [.github/workflows/ci.yml](../.github/workflows/ci.yml),
> [src/pipeline/harness/compliance/](../src/pipeline/harness/compliance/),
> `frontend/package.json`, `admin-frontend/package.json`.

---

## 1. Backend testing (Python)

### 1.1 Framework and configuration

- **Framework:** `pytest` (+ `pytest-asyncio`, `auto` mode — no `@pytest.mark.asyncio` needed).
- **Config** ([pytest.ini](../pytest.ini)):
  ```ini
  testpaths = tests
  asyncio_mode = auto
  addopts = -q --strict-markers
  filterwarnings = ignore::DeprecationWarning
  markers =
      slow: tests that take more than a second
      requires_postgres: needs a live Postgres+AGE instance with migrations applied; skipped by default
  ```
- **Scope:** `tests/` — **120 test files**, roughly **1 793** individual `def test_*`
  functions counted directly from source (the compliance suite below is separate
  and not included in this count).

### 1.2 Design rules ([tests/README.md](../tests/README.md))

1. **No network, ever.** An autouse fixture in `conftest.py` blocks every
   non-loopback socket. A test that reaches for Postgres, Groq, or Gemini fails
   loudly ("a boundary is unpatched") instead of silently hitting production,
   going slow, or flaking.
2. **Tests guard behaviour, not implementation.** Most exist because the
   behaviour they assert *actually broke in production*; regression tests carry
   a `Regression:` comment naming the bug, "so nobody 'cleans up' the assertion
   later without understanding what it protects."
3. **Failures must be loud.** Several tests assert that an error *surfaces*
   rather than being silently swallowed (a file that never generated, a message
   that never saved) — described as "the single most damaging bug class in this
   codebase."

### 1.3 Isolation infrastructure ([tests/conftest.py](../tests/conftest.py))

Two isolation mechanisms run **before** any application module is imported,
because `config.CHROMA_PERSIST_DIR` / `config.DATABASE_URL` are module-level
constants read at import time:

- **ChromaDB isolation:** `CHROMA_PERSIST_DIR` is repointed at a fresh
  `tempfile.mkdtemp()` for the whole pytest process. A second, fail-closed guard
  wraps `chromadb.PersistentClient` so that *any* attempt to open the real
  production `data/chroma_db` directory raises instead of silently writing to it.
- **Postgres/AGE isolation:** `DATABASE_URL` is repointed at a uniquely-named
  nonexistent database (`muhafiz_pytest_<uuid>`), by identity rather than by
  server — `asyncpg.create_pool` is wrapped so any attempt to open the real
  `muhafiz` database or the real `evidence_graph` graph raises a
  `LiveDatabaseAccessError`. This closes a gap the generic network-block fixture
  cannot: Postgres listens on loopback, which the network guard deliberately allows.
- Most suites simply fake their gateway/`age_client` rather than touch either
  database at all; the guards exist for whatever tries to open a real connection anyway.

### 1.4 Fixtures and fakes

`conftest.py` provides `FakeGateway` and `FakeLLM` — every external boundary
(LLM calls, the database, the vector store) is faked so the entire suite runs
"anywhere in seconds" with no live dependency.

`tests/fixtures/muhafiz_api_snapshot.json` — a captured JSON snapshot of the
external Muhafiz Data API's response shape, used with `httpx.MockTransport` (a
class distinct from the real HTTP transports the network-block fixture patches,
so a mocked response reaches the client without a real socket call) by
`test_muhafiz_api_client.py` and `test_sync_muhafiz_data_script.py`.

### 1.5 Unit tests (by area — file names are the source of truth)

| Area | Representative files |
|---|---|
| Auth / API surface | `test_api.py`, `test_auth_hardening.py`, `test_case_scope.py`, `test_mcp_hardening.py` |
| RLS | `test_rls_context.py`, `test_postgres_rls.py`, `test_rls_integration.py` (see §1.7) |
| Retrieval | `test_reranker.py`, `test_cross_reranker.py`, `test_embedder.py`, `test_chroma_vector_store.py`, `test_fulltext_index.py`, `test_entity_vector_store.py`, `test_graph_retriever.py` |
| Pipeline | `test_pipeline.py`, `test_orchestrator.py`, `test_router.py`, `test_query_rewriter.py`, `test_query_expander.py`, `test_verifier.py`, `test_json_extract.py`, `test_xagg.py`, `test_xnetwork.py` |
| Ingestion | `test_ingestion_document.py`, `test_ingestion_validation.py`, `test_ingestion_metadata_sweep.py`, `test_ingestion_transactional.py`, `test_ingestion_blocking_offload.py`, `test_ingestion_graph_extraction.py`, `test_pdf_loader.py`, `test_docx_loader_iter_blocks.py`, `test_image_loader_vision_cache.py`, `test_text_normalizer.py`, `test_urdu_text_processing.py`, `test_cross_script_variant.py` |
| Extraction | `test_doc_classifier.py`, `test_ner.py`, `test_domain_entities.py`, `test_structured_fields.py`, `test_structured_projection.py` |
| Entity resolution / graph | `test_entity_resolution.py`, `test_entity_resolution_sampling*.py`, `test_identity_index.py`, `test_versioning.py`, `test_candidate_reprioritization.py`, `test_same_as_queue_history.py`, `test_audit_confirmed_same_as_integrity.py`, `test_conflict_detection.py`, `test_conflict_marker.py`, `test_cross_silo_projection.py`, `test_collapse_*duplicate*.py` (3 files) |
| Community detection | `test_community_detection.py`, `test_community_summarization.py`, `test_community_staleness.py`, `test_community_admin.py` |
| Ingestion quality (Module G) | `test_ingestion_quality.py`, `test_ingestion_quality_admin.py`, `test_ingestion_circuit_breaker.py`, `test_ingestion_conflict_failure.py` |
| Muhafiz Data API | `test_muhafiz_api_client.py`, `test_muhafiz_cases.py`, `test_muhafiz_records.py`, `test_sync_muhafiz_data_script.py`, `test_sync_muhafiz_cases_script.py` |
| Agent harness — types/supervisor/cutover | `test_harness_types.py`, `test_harness_supervisor.py`, `test_harness_cutover.py` |
| Agent harness — tools (9 files) | `test_harness_tool_{rag,graph,local_search,global_search,sql,web,xagg,xgraph,xnetwork}.py` |
| Agent harness — sub-agents (11 files) | `test_harness_agent_{semantic_search,local_search,global_search,case_summarization,timeline_building,cross_case_linkage,investigative_analysis,large_scale_aggregate,report_drafting,data_quality,meta_analysis}.py` |
| File generation / export | `test_file_generation.py`, `test_attachments.py`, `test_kb_stats_documents.py` |
| Analytics / persistence | `test_analytics.py`, `test_persistence.py`, `test_db_test_isolation.py`, `test_chroma_test_isolation.py` |
| Config / infra | `test_config.py`, `test_airgap.py`, `test_key_manager.py`, `test_llm_client.py` |
| Scripts / eval | `test_build_real_entity_roster.py`, `test_build_real_eval_set.py`, `test_eval_scripts.py`, `test_load_legal_code_acts_script.py`, `test_load_real_offense_sections_script.py`, `test_reset_evidence_state_script.py`, `test_script_admin.py`, `test_verify_milestone_d_cleanup.py`, `test_sql_extractor_relaxation.py`, `test_sql_param_extractor_prompt.py`, `test_title_generator.py`, `test_graph_eval_isolation.py` |

### 1.6 The `DirectGateway` protocol-completeness test

`test_persistence.py::test_direct_gateway_implements_full_protocol` fails if
`DataGateway` (the Protocol, ~90 async methods) declares a method that
`DirectGateway` (the sole backend) does not implement — catching a partial
implementation before it becomes a runtime `AttributeError`.

### 1.7 Integration-style tests requiring a live database

`requires_postgres` marker: tests needing a real Postgres+AGE instance with
migrations applied are **skipped by default**. `tests/test_rls_integration.py`'s
own module docstring documents how to run them explicitly. This means the
default `pytest` run — including CI — never exercises RLS against a real
database; it only exercises the pure-Python `ContextVar` logic and mocked SQL.

### 1.8 Compliance suite (separate from `pytest.ini`'s scope)

`src/pipeline/harness/compliance/` — **5 files, 20 `def test_*` functions**
found directly in source. Deliberately lives outside `testpaths = tests`, so it
is invoked as a **separate CI step**
(`pytest src/pipeline/harness/compliance/`) with **no `continue-on-error`** —
a compliance regression fails the build. See
[AI_RAG_ARCHITECTURE.md](AI_RAG_ARCHITECTURE.md) §13 for what each of the five
enforcement points checks; each combines a **static** check (AST-scanning the
harness source for a forbidden pattern) with a **behavioral** check
(exercising the real code path).

### 1.9 E2E tests

**None found in this repository.** There is no browser-driving E2E framework
(Playwright/Cypress/Selenium) configured in either frontend's `package.json`,
and no end-to-end script under `tests/` or `scripts/` that drives the full
stack (frontend + backend + real database) through a browser. The closest
approximations are:
- `scripts/_smoketest.py` — a scripted HTTP smoke test (register → login →
  `/api/auth/me` → one real chat SSE call) documented in [RUN.md](../RUN.md) §6, run
  manually against a live backend, **not** part of the automated test suite.
- `scripts/eval_end_to_end.py` — an offline evaluation script over the
  retrieval/generation pipeline, not a CI-gated test.

### 1.10 Test commands

```bash
pytest                             # everything under tests/ (~10s per the README)
pytest tests/test_api.py           # one file
pytest -k persistence              # by name
pytest --cov=src --cov-report=term-missing --cov-report=xml   # CI's coverage run
pytest src/pipeline/harness/compliance/                        # compliance suite (separate CI step)
```

`--cov` in CI is **report-only** — no `--cov-fail-under`, so coverage is made
visible but does not block merges on an agreed threshold.

---

## 2. Frontend testing (TypeScript)

### 2.1 Framework

`vitest` + `@testing-library/react` + `@testing-library/user-event` +
`@testing-library/jest-dom`, `jsdom` environment. Both apps share the same
stack and an equivalent `test/setup.ts` that calls `cleanup()` after every test
(otherwise jsdom's `document` is not reset between tests, and `render()` output
stacks).

### 2.2 Commands

```bash
npm run test          # vitest run (single pass, CI-style)
npm run test:watch    # vitest (watch mode)
npm run lint          # oxlint
npm run build         # tsc -b && vite build
```

**Note:** the CI workflow ([.github/workflows/ci.yml](../.github/workflows/ci.yml)) only
runs `npm run build` for each frontend app — **`npm run test` is not part of the
CI pipeline** for either frontend. The vitest suites exist and pass locally but
are not currently CI-gated.

### 2.3 Test files and coverage

**Chat app** (`frontend/`) — 6 test files:
`components/brand/Logo.test.tsx`, `components/layout/Sidebar.test.tsx`,
`components/pipeline/QueryChecks.test.tsx`, `hooks/useModalA11y.test.tsx`,
`lib/api.test.ts`, `store/authStore.test.ts`, `store/chatStore.test.ts`.

**Admin app** (`admin-frontend/`) — 8 test files, one per major page:
`components/Sidebar.test.tsx`, `pages/AuditLogPage.test.tsx`,
`pages/CaseManagementPage.test.tsx`, `pages/DashboardPage.test.tsx`,
`pages/ErrorsPage.test.tsx`, `pages/IngestionQualityPage.test.tsx`,
`pages/KnowledgeBasePage.test.tsx`, `pages/ReviewQueuePage.test.tsx`,
`pages/RunHistoryPage.test.tsx`.

Combined: roughly **43** `it()`/`test()` blocks across both apps (direct source
count). No test exists for `frontend/src/pages/ChatPage.tsx`,
`LoginPage.tsx`, `RegisterPage.tsx`, or `SettingsPage.tsx`, nor for most of
`admin-frontend`'s remaining pages (`EntityEvalPage`, `GeneratedFilesPage`,
`LoginPage`, `McpCallLogPage`, `ProfilePage`, `UsersPage`) — **untested surface
area for QA to prioritize.**

### 2.4 Fixtures / mocks

No dedicated mock-server library (e.g. MSW) was found; component tests mock
axios/store calls directly via vitest's mocking utilities within each test file.

---

## 3. Test-coverage gaps identified for QA

- **No automated E2E coverage** of the full stack through a real browser
  against a real (or containerized) backend.
- **RLS integration tests are skipped by default** — CI does not exercise real
  Postgres row-level security; only unit-level `ContextVar` logic is verified
  automatically.
- **Frontend tests are not part of CI** — only `npm run build` gates merges;
  regression risk on any change that breaks a passing local test but is never re-run in CI.
- **Several user-facing pages have no test file at all** (see §2.3).
- **No load/performance test is part of the automated suite** —
  `scripts/loadtest_embedding_pipeline.py`, `scripts/loadtest_fulltext_index.py`,
  `scripts/loadtest_identity_index.py`, and `scripts/gpu_load_test.py` exist as
  standalone manual scripts.
