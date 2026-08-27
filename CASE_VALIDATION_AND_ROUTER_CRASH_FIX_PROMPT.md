# Fix the two Critical/High bugs from the 2026-08-27 50-question route sweep

**Trigger:** a 50-question live-fire sweep across all 9 chat routes (`scratchpad/run_questionnaire.py`,
results in `testingbugs.md`) surfaced two reproducible, independently-confirmed defects. Full
investigation notes, evidence, and rejected/corrected findings are in `testingbugs.md` §3 and §4 —
read those before starting, this prompt is the condensed action version.

Everything else in `testingbugs.md` (BUG-3 orphaned graph nodes, BUG-4 evaluator rejecting graph
evidence, BUG-5 router TPM cap) is explicitly **out of scope for this branch** — do not touch
`src/retrieval/graph_retriever.py`, `src/graph/*`, or `prompts/router.txt`. Do not touch anything
related to the KB content gap or `investigation_status` — both confirmed non-issues, not bugs.

## Bug 1 — a stale/foreign `case_id` crashes the turn with an unhandled 500

**Symptom:** any chat turn whose `case_id` isn't a real row in `cases` reaches a raw
`INSERT INTO sessions` and dies on a foreign-key violation — HTTP 500, empty body, full stack
trace in the server log, before the router or any pipeline step ever runs.

**Root cause, confirmed by reading the code:**
- `src/data_gateway/direct_backend.py:614-616` — `check_case_access()` returns `True`
  immediately for `platform-admin`, **without checking the case exists**:
  ```python
  async def check_case_access(self, case_id, user_id, user_role, min_role=None):
      if user_role == "platform-admin":
          return True
  ```
- `src/main.py` (~line 356, in `chat_endpoint()`) then passes that unchecked `case_id` straight
  into `gateway.create_session(...)`, whose INSERT hits the `sessions_case_id_fkey` constraint.
  There is no existence check anywhere between router receipt and that INSERT.
- `src/data_gateway/direct_backend.py:594` already has `get_case(case_id) -> Optional[dict]` —
  it's just never called on this path.

**Fix:**
1. In `src/main.py`'s `chat_endpoint()`, immediately before the existing
   `check_case_access(...)` call, add an existence check:
   ```python
   if case_id:
       if await gateway.get_case(case_id) is None:
           raise HTTPException(status_code=404, detail="Case not found")
       if not await gateway.check_case_access(case_id, user_id, current_user.role):
           raise HTTPException(status_code=403, detail="Not assigned to this case")
   ```
   404-before-403 is deliberate: case ids aren't secret in this system (they appear in FIR
   numbers, cross-case answers, etc.), so this doesn't leak anything a user couldn't already
   infer, and it turns a silent 500 into an actionable message. Keep that order.
2. This check runs on the **resolved** `case_id` (request field or session's stored one,
   whichever `chat_endpoint()` already settles on before this point) — so a session that later
   points at a deleted case is covered too. Don't special-case the two sources separately.
3. Add `tests/test_chat_case_validation.py`:
   - Bogus `case_id` → 404, for both `platform-admin` and a plain `investigator`.
   - Valid `case_id`, caller not assigned → 403 (unchanged existing behavior — regression-proof it).
   - Valid `case_id`, caller assigned (or admin) → 200 and the turn proceeds normally.
4. Verify live: re-run sweep-style request with a nonexistent `case_id` against the running
   dev server, confirm 404 (not 500) and no stack trace in the log; then confirm a real, valid
   `case_id` still streams a normal SSE response end-to-end.

## Bug 2 — a router failure kills the whole turn via an UnboundLocalError

**Symptom:** when `route_query()` raises (confirmed trigger: a Groq 413 TPM-limit error on the
cloud-escalation branch), the client receives **no SSE events at all** — not even the
degraded-to-RAG fallback the `except` block was clearly written to produce.

**Root cause, confirmed by reading the code, exact line:**
- `src/pipeline/orchestrator.py:893-904` — the `except Exception as exc:` branch around the
  router call assigns every fallback variable it needs (`route_str`, `output_format`,
  `case_scope`, `target_entity`, `secondary_methods`, `router_confidence`, `router_station`,
  `router_district`, `elapsed_ms`) **except `route_result`**.
- `src/pipeline/orchestrator.py:975` (outside the try, unconditionally reached) then does:
  ```python
  yield event(
      "router", "done", f"Route decided: {route_str}", elapsed_ms,
      confidence=router_confidence, case_scope=case_scope,
      reason=route_result.get("reason"),   # UnboundLocalError on the error path
  )
  ```
  This raises inside the SSE generator, which kills the stream — the router's own
  fallback-to-RAG design is completely defeated by one missing assignment.

**Fix:**
1. In the `except Exception as exc:` block at `orchestrator.py:893`, add a `route_result`
   assignment alongside its siblings, matching the shape `route_query()` itself returns on its
   own internal failure (`src/pipeline/router.py:571-582`, the `reason` key specifically):
   ```python
   route_result = {"reason": f"Router failed ({type(exc).__name__}), defaulting to RAG"}
   ```
2. Defensive hardening in the same edit — change line 975's access to
   `reason=(route_result or {}).get("reason")` so a future edit that forgets this again
   degrades to `None` instead of killing the stream a second time.
3. Add `tests/test_orchestrator_router_failure.py`: monkeypatch/mock `route_query` (or the
   underlying `call_llm_json` it uses) to raise, and assert the SSE generator still yields a
   complete sequence ending in a real (RAG-fallback) response — not an exception propagating out
   of `event_generator()`.
4. Verify live: temporarily force the failure the same way (or find a live trigger — the
   sweep's Q21 "What is the latest press release from Islamabad Police?" is a known repro if the
   Groq TPM cap is still tight), confirm the turn now completes with a real answer instead of
   nothing.

## Process

1. **One branch** for both — they're both in `orchestrator.py`'s/`main.py`'s request-handling
   layer, small, and were found together; no reason to split like the harness-sweep prompt did
   for its two file-disjoint bugs. Branch name: `fix/case-validation-and-router-crash`.
2. Implement both → **test live against the running dev server** (not just import/compile) →
   run the existing test suite to confirm no regressions → only merge once both are confirmed
   working live.
3. Merge into `main`, push to `origin/main`. **No Claude co-author trailer.** Author:
   `rayyanfaisal475207` (already the configured git identity — confirm with `git config user.name`
   before committing if unsure).
4. Report back: what changed (diff summary), the before/after live-test result for each bug,
   and confirmation the full test suite is still green.
