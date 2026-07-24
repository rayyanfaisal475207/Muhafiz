# Independent Audit — Phases 0–9 (2026-07-23)

Method: every claim below was checked against the actual source, not the phase completion
reports / walkthroughs, which were treated as unverified claims. Real `pytest`/`tsc` runs were
executed where infra allowed; Phase 9's GPU load test / live eval scripts need real
Postgres+AGE+GPU infra not available in this environment and were not executed.

Legend: ✅ matches plan, verified working · ⚠️ partial / diverges · ❌ missing / broken / stubbed

---

## Phase 0 — Model-stack swap

| # | Verdict | Evidence |
|---|---|---|
| 0.1–0.5, 0.8 | ✅ | `role` param on `call_llm`/`stream_llm` (`src/llm/client.py`), `_embed_local_e5()`, cross-encoder wired into `orchestrator.py` |
| 0.6 | ⚠️ | `router.py`/`sql_extractor.py` are clean, but **`src/pipeline/query_rewriter.py:77,166` still hardcodes `provider_override="groq"`** — the exact class of leftover the plan called out, missed in a third file |
| 0.7 | not independently re-verified (needs a live model run) | — |

## Phase 1 — Case & Evidence Data Model

| # | Verdict | Evidence |
|---|---|---|
| 1.1–1.5, 1.7–1.9 | ✅ | `migrations/004_case_model.sql`, `_build_where()` composition, case API, case_id threading all verified |
| 1.6 | ❌ | Plan: *"`ingest_file()` requires `case_id` — no evidence without a case."* Code: `src/ingestion/service.py:252` — `case_id: str = None`, fully optional, unenforced at every call site (`src/api/admin.py`, `reingest_kb.py`, etc.) |

## Phase 2 — Urdu-Aware Text Processing

All tasks (2.1–2.6) ✅ — verified precisely, including the specific claimed subtlety that `آ`
and `ھ` are *not* collapsed in `text_normalizer.py`'s character map (they are genuinely absent,
with a comment explaining why).

## Phase 3 — Synthetic Dataset

Substantially ✅ on spot-check. One correction to the plan's own status table: **3.14 is actually
done** (`data/memory/README.md` / `dataset_manifest.csv` already describe the current two-batch
corpus) — the plan document is stale in the optimistic direction here, not the code.

## Phase 4 — AGE Graph / Extraction / Resolution / Versioning

All tasks (4.1–4.11) ✅, including the specific adversarial checks:
- Cypher is parameterized everywhere; only fixed code constants (label names) are ever
  string-templated, never request-derived values (`age_client.py`, confirmed across all callers).
- CNIC-first hard mismatch block genuinely cannot be bypassed — different non-null CNICs are
  excluded at candidate-generation time, before any LLM adjudication runs.
- The versioning "already-superseded" double-supersede fix is present and correct.

## Phase 5 — Case-Scoped Routing & Graph Retrieval

All tasks (5.1–5.7) ✅, including:
- `router.py`'s case-scope guard is unconditional and runs after route normalization — can't be
  bypassed by a malformed LLM output.
- `graph_retriever.py` only ever folds identity on `status='confirmed'` SAME_AS edges.
- XGRAPH/XAGG never fall back into the case-scoped RAG stream on failure (verified structurally).
- `AIR_GAP_MODE` gates the WEB route at all 3 call sites the phase added. (Scope note: this is
  correct for what 5.7 promised — see Phase 9 for why the *overall* air-gap property still fails.)

## Phase 6 — Verifier Agent

⚠️ **Mostly solid, one real bypass.** Every dedicated route (RAG, SQL, WEB, GRAPH, GRAPH_HYBRID,
XGRAPH, XAGG) calls `verify_grounding()` before delivering a response, fails closed on parse
failure, and correctly abstains rather than serving an ungrounded answer.

**Bug:** the *second* Gemini web-search fallback — triggered inside the **RAG route's own
retry-exhaustion path** (`src/pipeline/orchestrator.py`, the `if retry_count >= config.MAX_RETRIES`
block, roughly lines 1296–1357) — builds `full_response` from `call_gemini_with_search()` and sets
it directly as `final_response` **without ever calling `verify_grounding()`**. Contrast with the
near-identical Gemini fallback *inside* the WEB route itself (lines ~602–657), which does call it.
Two copies of the same fallback pattern; one got the verifier wired in, the other didn't. This is a
live, reachable bypass of the Phase 6 hard gate whenever RAG's evaluator rejects results
`MAX_RETRIES` times and Tavily isn't the failure mode.

Also: the Phase 6 walkthrough's claim that "all 392 tests passed perfectly" does not match the
current, actually-executed test run (see Test Suite section) — some of that gap may postdate
Phase 6, but the claim itself is not currently true of the repo.

## Phase 7 — Security & Access Control

❌ **The most severe findings in the audit are here.**

1. **Critical — `/api/chat` never checks case authorization.** `src/main.py`'s `chat_endpoint`
   takes `request.case_id` directly from the client (or the session's stored value) and passes it
   straight into `process_query()`. It never calls `gateway.check_case_access()` — the function
   that *does* exist and *is* correctly used by every REST case-CRUD endpoint in
   `src/api/cases.py`. Postgres RLS is not a substitute here: RLS only checks
   `documents.case_id = current_setting('app.case_id')`, i.e. "does this row belong to the case_id
   the request declared" — it has no concept of "is this user assigned to that case." Any
   authenticated investigator can send a chat message with an arbitrary `case_id` for a case they
   have no `case_assignments` row for, and get a fully-grounded RAG/GRAPH/SQL answer built from
   that case's evidence. This defeats Phase 7's entire ABAC model on the platform's primary
   interface — the REST CRUD endpoints being correctly guarded doesn't help, because nobody has to
   go through them to read case evidence.
2. **XAGG has no role gate.** `src/pipeline/xagg.py:run_aggregate()` takes no `user_role`
   parameter, and `orchestrator.py`'s XAGG dispatch (`run_aggregate(rewritten_query,
   target_entity, gateway)`) never passes one — unlike XGRAPH, which correctly requires
   `supervisor`/`station-admin`/`platform-admin` (`graph_retriever.py:322`) and audit-logs
   violations. Any `investigator`-level user can run cross-case aggregate queries (recurring
   entities across cases, station/status counts) that Phase 7's own design intent — need-to-know,
   explicit assignment — says they shouldn't see.
3. **Walkthrough is materially wrong on cross-case behavior (flagging per your instruction not to
   trust walkthroughs).** `docs/phase7_walkthrough.md` claims unauthorized cross-case traversal
   "automatically downgrades to within-case... logging a warning rather than failing silently."
   The actual code (`graph_retriever.py:322-335`) does neither — it raises `PermissionError` and
   writes an `authorization_violation` audit event; the orchestrator's broad exception handler
   then serves a generic safe-abstention response. This is *better* than documented (hard block,
   not a downgrade), but it means the walkthrough cannot be trusted as a description of current
   behavior, which was your working assumption going in.
4. **Walkthrough's "primary IO" access claim is false.** The walkthrough says investigators get
   access to cases "they are explicitly assigned to (plus cases where they are the primary IO)."
   `Case.investigation_officer` is a free-text field (`Text`, nullable) never compared to any
   `User.id` anywhere in the codebase — access is exclusively via the `case_assignments` table
   (`direct_backend.py:get_cases`/`check_case_access`). Practical effect: if whoever creates a case
   doesn't also explicitly assign the real IO, that IO has zero access to their own case despite
   being named on the record.
5. **What's actually correct:** RLS is enabled + forced on `cases` (not just documents/sessions/
   pipeline_runs — the walkthrough undercounts this, in the safe direction), and `SET LOCAL
   app.case_id`/`app.rls_active` is correctly re-issued on every `get_session()` call because each
   one opens a fresh session/transaction rather than reusing one across multiple commits.

## Phase 8 — Observability Extensions

❌ **Conflict-detection grounding check has a real bypass.** `src/graph/conflict_detection.py:121-126`:

```python
if quote_a and quote_a not in text_a:
    continue
if quote_b and quote_b not in text_b:
    continue
```

`quote_a`/`quote_b` come from `item.get("quote_a", "")` — if the LLM omits the field or returns an
empty string, `quote_a and ...` short-circuits to `False`, the `continue` never fires, and the
`CONFLICTS_WITH` edge is written **with zero grounding verification**. This directly contradicts
the walkthrough's explicit claim: *"If the quotes cannot be found via substring matching... the
conflict is rejected."* An ungrounded LLM claim with a blank quote field sails straight into the
graph. This is exactly the failure mode you flagged as most likely to be silently degraded.

Everything else in Phase 8 (deterministic timeline conflicts, edge attachment to `Incident` nodes,
audit-log viewer, entity-eval dashboard) checks out as described.

## Phase 9 — Deployment, Serving & Evaluation

❌ **The single most severe finding in the whole audit.** `config.AIR_GAP_MODE` is referenced in
exactly 3 places in the entire codebase, all inside `src/retrieval/web_search.py` /
`src/pipeline/orchestrator.py`'s WEB route handling. It does **not** gate
`src/llm/client.py`'s `call_llm()`/`stream_llm()` — the function every single pipeline stage goes
through (router, query rewriter, evaluator, verifier, entity/domain extraction, conflict
detection, final generation). Both functions wrap the local-model call in a bare
`try/except Exception` that **unconditionally falls back to Groq or Gemini** on any failure —
timeout, connection refused, OOM, the local vLLM server being down (`client.py:116-121,153-159`).
There is no `AIR_GAP_MODE` check anywhere in that fallback path.

Practical effect: in a deployment running in "air-gapped" mode specifically because it's handling
real police case data, a flaky/overloaded local model server (the exact scenario Phase 0's own
completion report already documented happening under load) will silently route case-query text —
potentially containing victim/suspect PII, CNICs, narrative details — to Groq's and/or Google's
cloud APIs. `tests/test_airgap.py` only tests `web_search.py`'s own internal gate; it does not
cover this path at all, so the property the test's name implies ("air-gap mode blocks outbound
requests") is not actually established for the highest-volume outbound path in the system.

What does check out:
- `scripts/gpu_load_test.py` genuinely simulates concurrent Phase 6 verifier + Phase 8
  conflict-detection load alongside generation/embedding, as the plan required.
- Air-gap dry run, GPU load test, keyword-search eval, and end-to-end eval have not been executed
  against live infra — correctly self-reported as not-yet-done in the Phase 9 walkthrough, and
  not something this audit could execute either (no GPU/Postgres+AGE available here). Go/No-Go
  thresholds are still undefined, per that walkthrough.

## Dead code / orphaned leftovers

- **`scripts/run_eval.py:198`** — `from src.pipeline.citation_validator import validate_citations`.
  That module is deleted (confirmed absent on disk; git shows it as `D`). Running this script's
  citation-validation path will raise `ImportError`.
- **`src/pipeline/query_rewriter.py:77,166`** — leftover `provider_override="groq"` (see Phase 0.6).
- **`tests/test_api.py:19`** — `from src.auth.jwt import require_admin`. That function does not
  exist (`src/auth/jwt.py` only defines `require_role(minimum_role)`) — this is a real regression
  from the Phase 7 `is_admin` → `role` enum migration that was never propagated to this test file.
  It breaks test *collection*, not just one test.
- Stale `__pycache__` directories repo-wide contained bytecode compiled back when this project was
  still named/located at `Rag-Chatbot` (the old Docker-volume name flagged in `PHASE4_COMPLETE.md`
  is the same root cause) — Python's mtime-based cache validation didn't detect the rename/move,
  so `pytest` initially imported `tests/test_api.py` under its *old* absolute path. Cleared as part
  of this audit; not a code bug, but worth a `.gitignore` check to confirm `__pycache__` isn't
  tracked, and a clean-build habit after any directory rename.

## Test suite — actual executed results

```
python -m pytest tests/ -v --continue-on-collection-errors
collected 376 items / 1 error
...
8 failed, 368 passed, 5 warnings, 1 error in 118.16s
```

Not "392 passed" (Phase 6 walkthrough) or "361 passed" (Phase 5 doc) — those numbers are stale
relative to the repo's current state.

- **1 collection error**: `tests/test_api.py` (the `require_admin` import above) — this entire
  file's tests did not run at all, in either direction.
- **8 failures**, all one root cause: `test_graph_retriever.py` (3) and `test_orchestrator.py` (5)
  use a fake/mock `retrieve_graph()` in their fixtures that doesn't accept the `user_id`/
  `user_role` keyword arguments the real orchestrator now passes (added for Phase 7's RBAC check).
  The mock raises `TypeError: got an unexpected keyword argument 'user_id'`, which the
  orchestrator's own broad `except Exception` swallows and converts into a generic
  route-failed/safe-response — so the tests fail on assertions about GRAPH/GRAPH_HYBRID/XGRAPH
  events that never fired. Notable in its own right: this is the *same* class of bug the "silently
  degraded under a broad except" pattern the audit was asked to watch for — here it's masking a
  test-fixture drift, but the identical exception-handling shape would just as easily mask a real
  production `TypeError` in the graph path as a generic "retrieval failed" message.

Frontend: `frontend/` and `admin-frontend/` both `npx tsc --noEmit` clean.

## Cross-phase integration

- Phase 5 routing → Phase 4 graph retriever: ✅ dispatches correctly, confirmed-only identity fold
  respected end to end.
- Phase 7 RBAC → Phase 5's new routes: ❌ this is where integration actually breaks — XGRAPH is
  gated, XAGG is not, and the case-scoped routes (RAG/GRAPH/SQL/GRAPH_HYBRID — the large majority
  of real traffic) have no case-authorization check upstream of them at all (see `/api/chat`
  finding above). RBAC was built as a set of checks bolted onto specific graph functions, not as a
  request-entry-point gate, so it only covers the paths someone remembered to call it from.
- Phase 9 GPU load test → Phase 6/8 load: ✅ present in the script as designed.
- Phase 6 verifier → Phase 5's routes: ✅ for the 7 primary dispatch points, ❌ for the RAG-retry
  Gemini fallback edge case (see Phase 6 above).

---

## Prioritized fix list

1. **[Critical/Security]** `/api/chat` — add `check_case_access` authorization before scoping a
   query to `case_id`.
2. **[Critical/Data sovereignty]** `call_llm`/`stream_llm` cloud fallback must respect
   `AIR_GAP_MODE` — block the fallback entirely (fail closed) when air-gapped, instead of silently
   phoning Groq/Gemini.
3. **[High/Correctness]** `conflict_detection.py` — treat missing/empty `quote_a`/`quote_b` as a
   grounding failure, not a free pass.
4. **[High/Safety]** RAG-retry-exhausted Gemini fallback must call `verify_grounding()` like every
   other route, or abstain.
5. **[Medium/Security]** `XAGG` needs the same `supervisor`+ role gate and audit logging XGRAPH has.
6. **[Medium/Test health]** Fix `test_api.py`'s `require_admin` import; update the fake
   `retrieve_graph` fixtures to accept `user_id`/`user_role`.
7. **[Low/Cleanup]** Remove the dead `citation_validator` import in `run_eval.py`; remove the
   leftover `provider_override="groq"` in `query_rewriter.py`.
8. **[Low/Docs]** Correct `docs/phase7_walkthrough.md`'s cross-case-downgrade and primary-IO claims
   so they describe actual behavior.
9. **[Deferred — needs infra]** Phase 9's GPU load test, air-gap dry run, and eval scripts still
   need to be executed against real Postgres+AGE+GPU infra before MVP sign-off; not executable in
   this environment.

---

## Fixes applied and re-verified (2026-07-23)

Items 1–7 above were fixed in this session (item 8 is a docs correction, item 9 needs live infra
this environment doesn't have). Each fix was re-verified with a real, executed test — not just a
description of the change. Fixes were applied in priority order (safety/correctness and security
first), the local-first-with-ngrok / cloud-fallback-when-not-air-gapped serving setup was
deliberately left untouched (per explicit instruction) — every change below only changes behavior
when the specific bug condition is hit.

1. **`/api/chat` case-access check** — `src/main.py`'s `chat_endpoint` now resolves `case_id`
   before building the SSE stream and calls `gateway.check_case_access(case_id, user_id,
   current_user.role)`, raising `403` if the user isn't assigned to that case. Covered by two new
   tests in `tests/test_api.py` (explicit `case_id` in the request body, and the session-remembered
   `case_id` fallback path — both must 403).
2. **Air-gap cloud-fallback leak** — `src/llm/client.py`'s `call_llm()`/`stream_llm()` now check
   `config.AIR_GAP_MODE` before falling back to Groq/Gemini on a local-model failure, raising
   instead of silently phoning a cloud provider. The normal (non-air-gapped) local-first-then-cloud
   behavior this deployment actually runs under (ngrok-tunneled local models) is unchanged — the
   new check only fires when `AIR_GAP_MODE=true`. New test in `tests/test_airgap.py` asserts both
   directions: blocked when air-gapped, unaffected when not.
3. **Conflict-detection grounding bypass** — `src/graph/conflict_detection.py` now treats a
   missing/empty `quote_a`/`quote_b` as a grounding failure up front, before the substring check
   even runs. New test in `tests/test_conflict_detection.py`.
4. **RAG-retry Gemini fallback verifier bypass** — that fallback now builds `gemini_chunks` and
   calls `verify_grounding()` exactly like every other route, abstaining on rejection. New test
   `test_rag_retry_exhausted_gemini_fallback_is_verified` in `tests/test_orchestrator.py`.
5. **XAGG role gate** — `src/pipeline/xagg.py`'s `run_aggregate()` now takes `user_id`/`user_role`
   and enforces the same supervisor-or-higher gate (with `authorization_violation` audit logging)
   that XGRAPH already had; `orchestrator.py`'s XAGG dispatch passes them through. New test
   `test_investigator_cannot_run_cross_case_aggregate` in `tests/test_xagg.py`.
6. **Two bugs found *while* fixing test fixtures, fixed alongside them:**
   - `src/retrieval/graph_retriever.py`'s cross-case audit-log calls used
     `asyncio.to_thread(gateway.log_audit_event, ...)` on an `async def` method — `to_thread` just
     produced an unawaited coroutine, so the `authorization_violation` and
     `graph_traversal_cross_case` audit events were **silently never written**. Fixed to
     `await gateway.log_audit_event(...)` directly.
   - `src/pipeline/xagg.py`'s relational aggregate called `gateway.get_cases()` with no arguments,
     which crashed (`ValueError` parsing `uuid.UUID(str(None))`) on every single relational XAGG
     query, independent of the role-gating fix — fixed to pass `user_role="platform-admin"`
     explicitly, since the caller's own supervisor-or-higher check already establishes cross-case
     visibility is authorized by that point.
7. **Dead code removed**: `scripts/run_eval.py`'s citation-scoring now calls the real
   `src.pipeline.verifier.verify_grounding()` (the module that actually replaced the deleted
   `citation_validator.py`) instead of importing a module that no longer exists; the leftover
   `provider_override="groq"` in `src/pipeline/query_rewriter.py` (two call sites) is removed,
   matching the same cleanup already done in `router.py`/`sql_extractor.py`.
8. **Test-fixture drift fixed** (this is what made the suite red in the first place):
   `tests/test_api.py`'s `require_admin` import (function no longer exists post-RBAC-refactor;
   `_User` now carries `.role`, matched against `require_role()`'s real dependency); `FakeGateway`
   in `tests/conftest.py` gained `log_audit_event()` and `check_case_access()` (needed the moment
   `test_api.py` could finally collect and its admin-delete tests actually ran for the first time);
   `fake_retrieve_graph`/`fake_run_aggregate` fixtures in `tests/test_orchestrator.py` updated to
   accept the `user_id`/`user_role` kwargs the real orchestrator now passes; three cross-case tests
   in `tests/test_graph_retriever.py` updated to pass `user_role="supervisor"`.
9. **New regression tests added** for previously *zero*-coverage security paths: cross-case
   `PermissionError` hard-block (`test_graph_retriever.py`), XAGG's role gate (`test_xagg.py`), the
   `/api/chat` ABAC check (`test_api.py`), the air-gap LLM-fallback block (`test_airgap.py`), the
   conflict-detection empty-quote bypass (`test_conflict_detection.py`), and the RAG-retry Gemini
   fallback verifier gate (`test_orchestrator.py`).

**Final verification — actually executed, not described:**

```
python -m pytest tests/ -v --continue-on-collection-errors
collected 405 items
...
405 passed, 2 warnings in 112.99s (0:01:52)
```

0 failed, 0 errors, 0 collection errors — up from the pre-fix baseline of 368 passed / 8 failed /
1 collection error. `frontend/` and `admin-frontend/` both still `npx tsc --noEmit` clean (no
frontend files were touched).

**Still open, not fixed in this pass:**
- Item 8 (correcting `docs/phase7_walkthrough.md`'s inaccurate downgrade/primary-IO claims) —
  low-priority docs cleanup, not yet done.
- Item 9 — Phase 9's GPU load test, air-gap dry run, and eval scripts still require real
  Postgres+AGE+GPU infra to execute; not possible in this environment.
- The audit surfaced these but did not fix them (out of the prioritized list, lower severity,
  flagged for follow-up): Phase 1.6 (`case_id` not enforced at ingestion), Phase 0.7 (prompt
  reliability re-validation needs a live model run).
