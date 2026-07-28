# Muhafiz — Audit Remediation Implementation

You are implementing a remediation plan for the Muhafiz Evidence Intelligence Platform
(FastAPI + Postgres/Apache AGE + ChromaDB backend, two React frontends).

## Read these two documents in full before doing anything else

1. `issues.md` — a completed 125-finding audit (11 Critical, 19 High, 56 Medium, 39 Low),
   12 categories, with file/line locations and an explicit confirmed-vs-suspected
   confidence level per finding. This is the source of truth for *what is broken*.
2. `solution.md` — the approved implementation plan derived from it: 12 phases, each
   containing modules with issues-addressed / files touched / approach / blast radius /
   verification / migration needs / rollback. This is the source of truth for *what we
   are doing about it, and in what order*.

Read `solution.md` §0 (how to read the plan), §9 (disagreements and open decisions),
§10 (deliberately deferred), §11 (effort sizing) and §12 (file-grouping note) especially
carefully — they contain constraints that are not obvious from the phase list alone.

## Non-negotiable working rules

- **One module at a time.** Implement exactly one module (e.g. "Phase 2" — see note
  below), then stop and report. Do not start the next module until reviewed and told to
  proceed. Never batch modules, even trivial-looking ones.
- **Stay inside the module's declared scope.** Each module lists the files and functions
  it touches. If the fix genuinely requires touching something outside that list, stop and
  flag it before editing — do not quietly widen scope.
- **The plan is not infallible.** It was written from reading the code, but if
  implementation reveals that a module's stated approach is wrong, incomplete, or would
  break something the plan didn't anticipate, stop and say so rather than implementing a
  fix believed to be incorrect. Same for `issues.md`: if a finding turns out to be a
  non-issue on closer reading, say so — drop a bad finding rather than ship a pointless fix.
- **Report outcomes honestly.** If tests fail, show the actual output. If a fix cannot be
  verified in this environment, say so plainly rather than implying it was validated.

## Progress so far

- **Phase 0 — Foundations**: Modules 0.1 (stale CI test replaced), 0.2 (fail-fast startup
  config validation), 0.3 (`.env.example` sync). Done, committed to `main`.
- **Phase 1 — Independent Critical fixes**: Module 1.1 (XLSX formula/CSV injection
  sanitization), Module 1.2 (MCP least-privilege role + dead-code deletion + injection
  cleanup — also surfaced that `@modelcontextprotocol/server-postgres` is now deprecated
  upstream with an unfixed high-severity advisory, flagged but not blocking since the
  stdio-only transport isn't the affected surface), Module 1.3 (password minimum length,
  CORS_ORIGINS env-configurable, rate limiting extended to `/api/chat` + KB upload +
  attachments + case creation/assignment). Done, committed to `main`. **Module 1.2
  closeout (2026-07-28, Phase 0-3 closeout Task 1)**: live-verified against real Postgres
  — `scripts/verify_mcp_role.py` passes every check (SELECT allowed on
  `police_reference_data`, denied on `users`/`audit_logs`/`cases`/`sessions`/`messages`,
  INSERT denied), the real SQL route confirmed end-to-end against the new role, and the
  superuser `DATABASE_URL` fallback in `src/mcp/client.py` removed — a missing
  `MCP_DATABASE_URL` now raises `RuntimeError` immediately instead of silently
  degrading. Merged to `main`.
- **Phase 2 — Row-Level Security & Apache AGE isolation**: Done, **merged to `main`**.
  Migration 010 fixes the NULL-vs-NULL bug and adds a `messages` policy;
  `src/auth/rls_context.py` is a new request-scoped RLS dependency wired into all 7 REST
  routers + the chat endpoint; `current_cross_case`'s arm-before-authorization ordering
  bug is fixed in `graph_retriever.py`/`xagg.py`; `src/graph/case_scope.py` is a new
  (narrowly-applied) AGE case-scoping chokepoint. **One deviation from the original plan,
  discovered during implementation and worth knowing before touching these files
  again**: the plan's "derive case_id from path param, else general" router design
  doesn't fit `sessions.py`/`attachments.py` (a session's case affiliation is a row
  property, not known pre-query — real per-case RLS scoping there would incorrectly hide
  a legitimate session from its own owner) or `admin.py` (platform-wide dashboards need
  cross-case visibility). Those routers, plus `graph_review.py` (cross-case by design,
  pending §9.2) and `projects.py` (no RLS policy on that table), got RLS armed with the
  case dimension explicitly bypassed instead — real access control there is still the
  existing app-layer ownership/role checks, unchanged. Full rationale in
  `src/auth/rls_context.py`'s module docstring and `MANUAL_RLS_VERIFICATION.md`. This is
  directly relevant to Phase 5 (Module 5.3 touches `attachments.py`/`memory/conversation.py`
  ownership checks — the same files, for a different reason) and to Module 9.1's Audit
  Logs work (`admin.py` is one of the routers now explicitly cross-case-bypassed).
  **Closeout (Phase 0-3 closeout Task 2, live-verified 2026-07-28)**: all 4 checks in
  `MANUAL_RLS_VERIFICATION.md` passed against a real Postgres+AGE instance as a genuine
  non-superuser test role (`muhafiz_app`); `/security-review` on the branch found zero
  HIGH/MEDIUM findings and one Low (RLS-arming order relied on caller convention in
  `retrieve_graph()`/`run_aggregate()`) — fixed as a same-phase addendum; a fixture bug
  in `tests/test_rls_integration.py` (FK violation on a fabricated `user_id`, never
  caught before since these tests always skipped without live DB) fixed too. **This live
  verification surfaced a new, significant, previously-unfindable-by-static-review
  finding — see below.**
- **NEW Critical finding (2026-07-28, only findable with live DB access) — the
  application's runtime `DATABASE_URL` connects as the Postgres superuser
  (`rolsuper=true, rolbypassrls=true`), which unconditionally bypasses every RLS policy
  regardless of correctness.** Written into `issues.md` §2 as the 13th Critical finding
  (12→13 Critical, 126→127 total). This does **not** make anything worse than
  pre-Phase-2 (app-layer checks are unchanged and remain the real protection today), but
  it means **Phase 2's entire RLS backstop is currently inert** in any deployment using
  this connection — not degraded, zero effect — until the app's own connection role is
  changed. **Not yet scoped or fixed** — the suggested fix (same pattern as Module 1.2:
  a least-privilege `muhafiz_app`-style role for the app's normal runtime connection,
  `DATABASE_URL` repointed at it) is flagged in `issues.md` for a future module, not
  implemented. Treat this as an urgent, explicitly-tracked follow-up, not something to
  lose track of — believing "Phase 2 landed, so there's a DB-level backstop now" is
  currently false.
- **Phase 3, Module 3.1 — Eval harness isolation**: Done, **merged to `main`**. New
  migration `011_age_eval_graph.sql` creates a second, physically separate AGE graph
  (`evidence_graph_eval`); `versioning.write_node/write_edge/get_edge`,
  `case_scope.scoped_cypher`, and `entity_resolution.resolve_and_write` all gained a
  `graph` parameter (default: production, zero behavior change for real ingestion);
  `scripts/eval_entity_resolution.py` now passes `graph=EVAL_GRAPH` on every call
  including the destructive wipe, plus a hard runtime guard at the top of `evaluate()`.
  **One deviation from the plan's literal scope, flagged before implementing, then
  done**: the plan named only `resolve_and_write` for the parameter threading, but that
  function calls `resolve_mention()` for the resolution decision *before* writing —
  threading `graph` only into the writes (as literally scoped) would have left eval
  *reads* (candidate scoring, CNIC lookup, case-membership check) silently hitting
  production `evidence_graph` while writes landed in `evidence_graph_eval`, leaking real
  case data into eval decisions. Widened to thread `graph` through `resolve_mention` and
  its four private read helpers too, plus `case_scope.scoped_cypher` (one file not named
  in the plan's file list, needed because `_shares_case` routes through it). Confirmed
  the new regression test actually catches this gap (temporarily reverted just the
  read-path threading, watched the test fail as predicted, restored the fix).
  **Closeout (Task 3, live-verified 2026-07-28)**: migration 011 applied cleanly against
  real Postgres+AGE; `evidence_graph`'s node/edge counts confirmed byte-for-byte
  unchanged before/after a real `eval_entity_resolution.py` run; the hard runtime guard
  confirmed to actually raise `RuntimeError` (temporarily repointed `EVAL_GRAPH` at
  production, confirmed the immediate raise, reverted). Aside, not chased: production
  `evidence_graph` carries two extra ad-hoc labels (`Date`, `DebugTest`) outside
  migrations 005/011's static list — `DebugTest` in particular is worth a two-minute
  look eventually, not urgent.
- **Phase 3, Module 3.2 — Purge existing contamination**: Done, **merged to `main`**
  (`scripts/purge_eval_contamination.py`). Reconciliation (a hard prerequisite per
  `solution.md`) found the audit's 33-vs-26 `Person` discrepancy was actually 41-vs-26
  live (real ingestion continued since the 2026-07-27 audit) — the extra 15 are real,
  non-eval `Person` nodes that are NER false positives (place names, role titles, stray
  Urdu phrases), confirming `source_doc_id STARTS WITH 'EVAL-'` is the correct
  identification query, not the `source_chunk_id IS NULL` proxy, which would have wrongly
  caught those 15. Real delete executed after a fresh backup: 133 nodes / 316 edges
  removed, every count matching its pre-computed dry-run expectation exactly. Notably,
  12 of the 316 deleted `SAME_AS` edges (beyond the audit's original 88) linked *real*
  junk-quality entities to eval fixtures — legacy contamination from before Module 3.1's
  isolation existed; the "either endpoint" deletion logic correctly removed only those
  edges, leaving the real (if low-quality) nodes and their other legitimate edges intact.
  Post-delete verification: re-run of the identification queries shows zero `EVAL-*`
  contamination anywhere; all 20 real `Case` nodes confirmed present; the 6 real
  junk-entities spot-checked with their eval-linking edges gone and other real edges
  intact. Script has two-flag safety (`--execute --yes-i-am-sure`) plus a hard runtime
  refusal if its own Case-node safety check ever returns nonzero.
- **Phase 4, Module 4.1 — Case-scoped ID generation and retrieval-filter consistency**:
  Done, **merged to `main`**. `Document._generate_id()` folds `case_id`/`project_id`
  into the `doc_id` hash seed; `chunker.chunk_documents()` tags `case_id`/`project_id`
  onto each parent doc and re-derives `doc_id` before deriving chunk ids;
  `direct_backend.py`'s `insert_documents` changed `ON CONFLICT (doc_id) DO NOTHING` to
  `DO UPDATE SET case_id/project_id/is_global/doc_type = EXCLUDED.*`;
  `orchestrator.py`'s two `where_clause` sites now filter on `case_id` alone when
  present, only falling back to `is_global: True` when there's genuinely neither
  `project_id` nor `case_id`. New `scripts/find_doc_id_collisions.py` (read-only
  Postgres-vs-Chroma audit script, written but not run without live infra). Repo-wide
  grep for `doc_id` exact-string comparisons outside the write path: none found.
  **Closeout (live-verified against real Postgres+Chroma, see
  `MODULE_4_1_CLOSEOUT_PROMPT.md`)**: `ON CONFLICT DO UPDATE` confirmed firing correctly
  (case_id/is_global/doc_type all update on a second insert, no FK errors); the
  collision script run against live data (101 Postgres rows / 98 Chroma doc_ids) found
  zero pre-existing damage from the old `DO NOTHING` bug; an end-to-end same-file,
  two-different-`case_id` ingest confirmed two distinct `doc_id`s with no cross-case
  leakage in case-scoped retrieval. **One real gap found and fixed as a same-phase
  addendum**: `filename` was missing from the `DO UPDATE SET` list (harmless in
  practice now that `doc_id` is source-derived, but inconsistent with every other
  column) — added `filename = EXCLUDED.filename`, committed directly to `main`.
  Two out-of-scope observations surfaced during closeout, not chased: a 3-row gap
  between Postgres `documents` (101) and Chroma doc_ids (98) — zero-chunk docs, worth a
  look later; and a Cypher `MATCH` after `OPTIONAL MATCH` syntax bug in Phase 8's
  background conflict-detection query (unrelated subsystem).
- **Phase 4, Module 4.2 — Ingestion transactional consistency**: Done, **merged to
  `main`**. `vector_store.upsert_documents()` reordered to write Chroma first, Postgres
  second — a Chroma failure now leaves no Postgres row; if Postgres then fails, the
  just-written Chroma chunks are deleted via new `ChromaVectorStore.delete_by_ids()`
  before re-raising. New `EmbeddingDimensionMismatch`, raised by
  `ChromaVectorStore.upsert()` before every write if the embedding's length doesn't
  match new `config.EXPECTED_EMBEDDING_DIM` (auto-derived per `EMBEDDING_PROVIDER`:
  e5=1024, gemini=3072, openai=1536, local=384; env-overridable) — fires regardless of
  collection state, closing the "silently adopts wrong dimension on an empty
  collection" gap Chroma's own error doesn't cover. **Blast-radius catch during
  implementation**: the new dimension guard broke `tests/test_chroma_vector_store.py`
  wholesale (toy 8-dim vectors, unrelated to embedding correctness) — fixed with an
  autouse fixture patching `EXPECTED_EMBEDDING_DIM` to 8 for that file, not by inflating
  every fixture to real dimensions. No live-infra gap this time — pure Python/Chroma
  logic, fully unit-tested.
- **Phase 4, Module 4.3 — Ingestion metadata correctness sweep**: Done, **merged to
  `main`**. Five independent fixes: (1) `total_pages` now always means the PDF's true
  page count (`doc.num_pages()`), never silently shrinking to "pages that survived";
  new `pages_ingested`/`dropped_pages` ride along on the returned Documents' own
  metadata rather than widening the shared `(file_path) -> list[Document]` loader
  contract every format shares. (2) Admin KB upload no longer silently overwrites a
  same-named file — disambiguates to `filename__2.ext` (incrementing) instead. (3)
  `date_registered` prefers a date near a recognized label over the first date-shaped
  substring anywhere in the document, tagged with new
  `date_registered_confidence: "labeled"|"unlabeled_fallback"|None`. **One real bug
  caught by a test during implementation**: the first version searched a proximity
  window on both sides of each date match, which let a label near a *later*, unrelated
  date incorrectly attach to an *earlier* one — fixed by restricting the window to
  before the date only, matching this corpus's consistent "Label: value" field
  convention. (4) Excel loader now blanks only genuine NaN cells
  (`df.where(pd.notna(df), "")` before the `str` cast), not cells whose real content is
  the literal string `"nan"`. (5) PDF table cells are XML-escaped before `Table()`
  construction, defensively (not an active bug today — `Table()` doesn't parse plain
  strings as XML — but closes the trap before any future change wraps cells in
  `Paragraph()`). **One small widening past the plan's named file list, flagged before
  doing it**: `service.py`'s graph Document-node write (one line, same call site
  already populating `date_registered`) now also carries `date_registered_confidence`
  through to the graph — without it the tag would be computed and immediately
  discarded, defeating the fix's stated purpose.
- **Phase 5 — Authorization/RBAC application-layer hardening**: All four modules done,
  **merged to `main`** (fast-forward, commits `8b52d4c`/`59769e5`/`af68e5e`/`3370d66`).
  - **Module 5.1 — Case mutation and assignment scoping**: `require_case_access` became a
    `min_role`-aware dependency factory; `update_case`/`delete_case` now require the
    caller's PER-CASE `case_assignments.role` to be supervisor-or-above (read/list
    unchanged). New `User.police_station` column (migration `012_user_station.sql`, no
    backfill data existed) backs a new station-scoping check in `case_assignments.py`,
    with a NULL-police_station bridge (loud warning log, falls back to old unrestricted
    behavior) since live data had zero station-admins with existing assignments to infer
    a backfill from. **Flagged, not resolved**: the `update_case`/`delete_case` role
    tightening is an intentional access change the team needs to sign off on —
    currently-assigned investigators lose destructive-write access unless their per-case
    role is supervisor+. Live-verified against real Postgres (migration applied;
    `check_case_access`/station-match logic exercised against real rows).
  - **Module 5.2 — Graph-review queue hardening**: `reviewed_by` is no longer
    client-supplied (derived from the authenticated admin); `confirm_match`/`reject_match`
    now write `log_audit_event` entries; `list_pending`'s `case_id` param now actually
    filters (via a new `OPTIONAL MATCH ... BELONGS_TO_CASE` Cypher query), with
    `case_id=None` still returning the full cross-case queue unchanged. **§9.2's
    cross-case-`case_assignments`-bypass item was deliberately NOT touched** — still an
    open product decision, not implemented. Live-verified the new Cypher against real
    Apache AGE (ran without error; confirmed `BELONGS_TO_CASE` is real/populated in
    production data; no pending review edges currently exist to exercise confirm/reject
    end-to-end without fabricating data, which wasn't done).
  - **Module 5.3 — Session/attachment ownership gaps**: `upload_attachment` gained the
    ownership check its `list`/`delete` siblings already had (a session with no
    `sessions` row yet — i.e. brand-new, client-generated `session_id` — is still
    allowed through); `save_history` gained an ownership check it previously had none of
    at all; `list_attachments`/`delete_attachment`'s falsy-ownership checks now deny by
    default on a missing owner instead of skipping the check. **Plan discrepancy found**:
    `solution.md` mischaracterized `load_history`/`delete_history`'s actual condition
    (gates on the caller's `user_id` argument, not the row's owner column) and
    `issues.md` itself calls those two already-correct — left them untouched rather than
    applying an unrequested change. Live-verified against real Postgres (a real
    cross-user `save_history` write attempt raises `PermissionError`, no message
    persisted; cleaned up after).
  - **Module 5.4 — Generated-file download scoping**: new nullable
    `generated_files.case_id` column (migration `013_generated_files_case_id.sql`, no
    backfill — not reliably derivable for existing rows); `orchestrator.py`'s
    `_generate_file` threads `case_id` through at both file-generation call sites;
    `download_file` no longer gives `station-admin` blanket cross-case access — now
    owner OR `platform-admin` OR (`station-admin` AND a real `case_assignments` row for
    that case); a file with `case_id IS NULL` (every pre-migration row) keeps the old
    blanket access, an explicit accepted limitation. **Found and fixed along the way**:
    `FakeGateway.check_case_access` (test double) short-circuited to `True` for the
    "any assignment" threshold without consulting `case_assignments` at all — tightened
    to match real `DirectGateway` behavior, full suite re-run to confirm nothing else
    relied on the old shortcut. Live-verified against real Postgres (migration applied;
    `case_id` round-trips through `log_generated_file`/`get_generated_file`; a stranger
    with no real assignment correctly denied; cleaned up after).
- **Phase 6 — LLM pipeline correctness**: All five modules done, **merged to `main`**
  (fast-forward, commits `47aa530`/`c122daf`/`7c35bea`/`86f663b`/`7e6d21e`, plus a
  docs-tracking commit `9a3ad4f`). `/security-review` run on the full branch before
  merge: zero HIGH/MEDIUM/LOW findings. Full suite on `main` post-merge: **574 passed,
  4 skipped, 6 deselected (slow), 0 failed** — same 4 pre-existing live-Postgres skips,
  no regressions.
  - **Module 6.1 — Shared, safe JSON extraction**: New `src/pipeline/json_extract.py`:
    `extract_json(response) -> Any`, moved verbatim from `file_structurer.py`'s existing
    correct implementation, extended to also handle JSON arrays (needed by
    `query_expander.py`'s list-returning case, per the plan). `file_structurer.py`'s local
    `_extract_json` replaced by an import alias so its existing call site/tests needed no
    changes. `evaluator.py`/`verifier.py`: local greedy-regex implementations deleted,
    call sites use `extract_json(raw)` directly (removes the old double-parse), `except
    json.JSONDecodeError` widened to `except ValueError` (the shared function raises plain
    `ValueError` on total failure). `router.py`: inline regex block replaced with
    `extract_json(response)` + an explicit `isinstance(result, dict)` check.
    `query_expander.py`: manual fence-stripping replaced with `extract_json(raw)`, same
    `except ValueError` widening. `sql_extractor.py`: hardcoded fence-strip replaced with
    `extract_json(response)`. Dead `import json`/`import re` removed where nothing else
    used them (verified via grep first; `verifier.py` keeps `re` for its hedging/leakage
    regexes). Tests: the 5 existing `_extract_json` tests moved from
    `tests/test_file_generation.py` into new `tests/test_json_extract.py`, plus 6 new
    cases covering each call site's historical failure shape. **Not verified**: no live
    LLM access in that session, so the plan's "a real call through one of the five
    updated call sites is a stronger check than mocked unit tests alone" was not done.
  - **Module 6.2 — RAG route exception guard**: `orchestrator.py`'s RAG route's
    generation+verification block (the one dispatch branch with no exception guard)
    wrapped in try/except degrading to `_SAFE_RESPONSE` on failure. **Deviation from the
    plan, found and flagged before implementing**: the plan called this "the same
    try/except pattern every sibling branch already uses," but the siblings
    (SQL/WEB/GRAPH/GRAPH_HYBRID) actually catch and fall back to `route_str = "RAG"` —
    RAG itself has nowhere further to fall back to, so the correct sibling to copy is
    RAG's own retrieval-stage guard a few lines above, which already degrades straight
    to `_SAFE_RESPONSE`. New regression test in `tests/test_orchestrator.py`.
  - **Module 6.3 — Local-vs-cloud aware `max_tokens`**: **Deviation from the plan, found
    and flagged before implementing**: `git log -p` on `evaluator.py` confirmed commit
    `f108833` raised `max_tokens` 800→2000 specifically to fix a *live-confirmed*
    Qwen3-14B truncation bug — lowering the local number back down (the plan's literal
    suggestion) would have reintroduced it. Also, the caller can't know local-vs-cloud in
    advance (`call_llm()` decides that per-attempt internally), so the fix necessarily
    widened into `src/llm/client.py`: new optional `cloud_max_tokens` param (defaults to
    `max_tokens`, every other call site unaffected). `evaluator.py`/`verifier.py` now
    pass `max_tokens=2000, cloud_max_tokens=800` — local keeps its live-verified value,
    cloud (no thinking-trace tax) drops to 800. Also fixed `verifier.py`'s adjacent stale
    docstring. The Suspected-confidence context-overflow risk on local was left
    unaddressed by agreement — the real fix (trimming chunk input text) is a separate,
    larger change. New `tests/test_llm_client.py`.
  - **Module 6.4 — Streaming/rotation robustness**: `_stream_local` gained the same
    empty/whitespace-content cloud-fallback trigger `_call_local` already had (tracks
    whether any chunk had real content; raises after the stream is exhausted if not —
    safe since nothing has reached the caller yet in that case).
    `key_manager.rotate_key()` changed from unconditional increment to a compare-and-swap
    keyed on the index the caller observed before its own call failed (new
    `get_current_index(provider)`; all three `client.py` call sites updated), so
    concurrent rate-limit failures on the same key no longer over-rotate. New
    `tests/test_key_manager.py` (5 tests, pure sync logic — directly covers the "N
    concurrent failures rotate only once" scenario).
  - **Module 6.5 — Extraction regex robustness**: `_CNIC_RE`/`_PHONE_RE`/`_PLATE_RE`/
    `_FIR_RE` now tolerate a missing separator, whitespace, or a non-ASCII dash/minus
    variant between identifier groups (real OCR/vision-extraction noise); canonical
    forms are rebuilt from captured groups, not the raw match, so the same identifier
    OCR'd two different ways still normalizes identically. `_PLATE_RE` is now
    case-insensitive. **Real false positive caught during implementation, not just the
    plan's theoretical risk**: making every separator fully optional let `"CASE-009"` in
    an existing test parse as a plate (`"CAS"` + zero-length separator + `"E"` + `"-"` +
    `"009"`) — fixed by requiring at least one separator character specifically at the
    plate's letter-to-letter gap; digit-adjacent gaps stay fully optional. 10 new cases
    in `tests/test_structured_fields.py`, including a regression test for the false
    positive.
- Test suite on `main` (current tip `3370d66`): **545 passed, 4 skipped, 0 failed**,
  excluding `test_pdf_loader.py`'s real-Docling `slow`-marked tests, which intermittently
  error in this environment with a Docling `ConversionError`/`std::bad_alloc` unrelated to
  any of this work (reproduced in isolation; passes cleanly on some runs, not others —
  environment resource flakiness, not a regression). The 4 skips are
  `tests/test_rls_integration.py`'s `requires_postgres`-marked tests, still confirmed
  passing for real against live Postgres+AGE from the Phase 0-3 closeout, just skipped by
  default in a plain `pytest` run without `RUN_POSTGRES_TESTS=1 TEST_DATABASE_URL=...` set.
- **Not pushed to origin** — `main` is 25 commits ahead of `origin/main` (1 behind, from
  an unrelated remote-side commit). Push only when explicitly asked.

## Environment constraints

- **This depends entirely on which session/account is running you — check before assuming
  either way.** Phases 0-3 were originally implemented with no live Postgres, Apache AGE,
  or GPU/model-server access, so anything requiring them was written with tests marked
  `@pytest.mark.requires_postgres` (skipped by default) plus a written manual verification
  procedure. The Phase 0-3 closeout (2026-07-28) was then run from a *different* session
  sharing this same working directory that *did* have real Postgres+AGE access (confirmed:
  `psql`/live queries against `localhost:5432/muhafiz` worked, `docker`/container-level
  `pg_dump` worked) — that's how Tasks 1-5 in `PHASE_0_3_CLOSEOUT_PROMPT.md` got done at
  all. **If you have live infra, use it** — run the `requires_postgres` tests for real
  (`RUN_POSTGRES_TESTS=1 TEST_DATABASE_URL=...`), verify migrations against the actual
  instance, don't defer to "write it but can't verify" if you don't have to. If you don't,
  fall back to the original constraint: write the fix, mark live-dependent tests skipped,
  document a manual verification procedure, and say plainly what wasn't verified.
- Windows 11 / PowerShell is the primary shell; a Bash tool is also available. Mind path
  separators and encoding (several scripts in this repo already handle cp1252/UTF-8 issues).
- The test suite is `python -m pytest tests/ --continue-on-collection-errors` (~100s).

## Open decisions still outstanding (do not start the affected work without an answer)

- **§9.2 — graph-review cross-case bypass**: Module 5.2's three unblocked items
  (`reviewed_by` spoofing, missing audit-log calls, `list_pending`'s `case_id` filter)
  are done. The cross-case-`case_assignments`-bypass item itself is **still open** — do
  not implement case-scoped access control on `src/api/graph_review.py`'s confirm/reject/
  list endpoints without a product answer to "is cross-case entity-resolution review
  deliberately exempt from per-case confidentiality, or a gap?"
- **§9.3 — Audit Logs role gate (frontend vs. backend authoritative)**: blocks Module 9.1.
- **§9.4 — the failing CI test**: resolved. Module 0.1 confirmed the code was correct and
  the test was stale; already fixed and committed.

§9.1 (the eval harness wipes the entire graph, not just adds fixtures) is a correction to
the issue description, folded into Phase 3's design — no decision needed.

## Untracked, unscoped follow-up (surfaced during Phase 2 live-verification, not yet a module)

**The application's runtime `DATABASE_URL` connects as the Postgres superuser**
(`rolsuper=true, rolbypassrls=true`), unconditionally bypassing every RLS policy —
written into `issues.md` §2 as a 13th Critical finding. Phase 2's RLS backstop is
currently inert wherever this connection is used (app-layer checks, the real protection
today, are unaffected). Not scoped into any phase yet — flag it if a future phase's work
touches `src/database/postgres.py`'s connection setup, and don't let "Phase 2 landed"
imply this is fixed.

## Git discipline

- **Branch before any code change** — one branch per module/phase, named e.g.
  `fix/phase-2-rls-redesign`.
- Commit only when explicitly asked; one commit per module, message referencing the
  phase/module and the `issues.md` finding titles addressed. Merge to `main` locally
  (fast-forward) only when explicitly asked — never push to origin without being asked.

## Per-module workflow

For each module assigned:

1. Re-read the relevant section of `solution.md` and the exact `issues.md` findings it cites.
2. Read the actual current code for every file the module touches — the plan's line numbers
   are from 2026-07-27 and may have drifted.
3. Use the task list to track the module's own steps if it has more than two or three.
4. Implement.
5. Run the full test suite and report the real result.
6. Report back: what changed, what the verification showed, what could not be verified here,
   anything found that contradicts the plan or the audit, and the rollback command.
7. Stop. Wait for review.

## Recommended gates

- After each security-heavy phase (Phase 1 done, **Phase 2 done — `/security-review` ran
  clean (zero HIGH/MEDIUM) with one Low addressed as an addendum, merged to `main`**,
  Phase 5): run `/security-review` on the branch before review. **Phase 5 was merged to
  `main` without this gate having been run**, and was never retroactively covered
  either — still an outstanding gap, worth doing before Phase 5-touched files
  (RBAC/ownership: `cases.py`, `case_assignments.py`, `graph_review.py`,
  `attachments.py`, `conversation.py`, `main.py::download_file`) are touched again.
  **Phase 6 — `/security-review` run on the full branch before merge: zero
  HIGH/MEDIUM/LOW findings.** Merged clean.
- `/code-review` is run manually at phase boundaries — don't attempt to launch it.

## Start here

Phase 6 is complete and merged to `main` (fast-forward; `main` tip: `9a3ad4f`). Full
suite on `main` post-merge: **574 passed, 4 skipped, 6 deselected (slow), 0 failed**.
Before doing anything else, run `git log --oneline -3` to confirm `main` is at or past
this tip, and `git branch` to check no Phase 6 module branches are still sitting
un-merged (they shouldn't be — all five landed in one fast-forward).

**Note the still-outstanding gap directly above**: Phase 5's RBAC/ownership changes
were never covered by `/security-review`, retroactively or otherwise. Not blocking for
Phase 7 (which doesn't touch those files), but flag it if a future phase does.

**Phase 7 — Ingestion performance & safety.** Four modules (`solution.md` §Phase 7):

- **Module 7.1 — Blocking vision-OCR retry loop.** `src/ingestion/loaders/pdf_loader.py`
  (`_load_scanned_page_with_vision`, `load_pdf`), `src/ingestion/loader_router.py:85`,
  `src/ingestion/service.py:348` (`ingest_file`) — a blocking `time.sleep(120)` retried
  up to 10 times runs inside an `async def` with no executor offload; a single bad file
  can freeze the whole server. Wrap the call chain in `await asyncio.to_thread(...)` at
  `ingest_file`'s call site.
- **Module 7.2 — Upload validation and size caps.** `src/ingestion/loader_router.py:55-90`,
  `src/api/admin.py:249-262`, `src/ingestion/service.py:260-306` (`ingest_directory`) — no
  MIME/magic-byte validation anywhere in the upload-to-ingestion path, and the 50MB size
  cap lives only on the admin HTTP endpoint, not the shared ingestion path. New shared
  `src/ingestion/validation.py::validate_file(path)`.
- **Module 7.3 — Entity-resolution N+1 batching.**
  `src/graph/entity_resolution.py:171-213` (`_generate_candidates`) — one Cypher
  round-trip per surviving candidate per mention during ingestion; batch into one query.
- **Module 7.4 — Analytics indexes, pagination, and bounded caches.**
  `src/database/models.py`, `src/data_gateway/direct_backend.py:948-973,804-817`,
  `src/ingestion/loaders/image_loader.py:21` (`_vision_cache`),
  `src/ingestion/loaders/docx_loader.py:134-171` — missing indexes on analytics tables,
  unbounded admin-page queries, an unbounded in-memory vision-OCR cache, and an O(n²)
  docx loader helper. Coordinate the pagination-contract change with Phase 9's admin
  frontend work per `solution.md`.

Re-read `solution.md`'s Phase 7 section in full before starting — line numbers above are
from the 2026-07-27 audit and may have drifted; this note has been true for every phase
so far. Cut a fresh branch off current `main` for Module 7.1, per the standard
one-module-at-a-time rule — implement **Module 7.1 only**, then stop and report.

