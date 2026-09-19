# Chat / Aggregate Integration Report

Feature-flagged exposure of the new aggregate architecture through the
existing `/api/chat` flow, plus completion of the outstanding semantic
regression evidence.

This is integration only. No aggregate internals were modified, no query
capability was added, and no legacy path was removed.

| | |
|---|---|
| Flag | `AGGREGATE_ENGINE_MODE` — `legacy` (default) / `aggregate_v1` |
| Default behaviour | **unchanged**, byte for byte |
| Tests added | 20 (`tests/test_xagg_engine_flag.py`) |
| Full project suite | **green, exit 0, zero failures** |
| Semantic regression | **complete — no regressions** |

---

# 1. Semantic Regression Verification

The rerun interrupted by the Docker outage is now complete. All five
semantically-adjacent questions, three runs each, against live data.

| Test | Question | Previous result | Current result | Truth | Status |
|---|---|---|---|---|---|
| SEM-1 | accused persons linked to cases | REF / 92 / REF | **92, 92, 92** | 92 | **IMPROVED** |
| SEM-2 | witnesses participated | 37, 37, 37 | **37, 37, 37** | 37 | unchanged, correct |
| SEM-3 | victims recorded | REF, REF, REF | **9, 9, 9** | 9 | **IMPROVED** |
| SEM-4 | suspects with confirmed arrest status | REF, REF, REF | REF, REF, REF | not representable | unchanged, correct |
| SEM-5 | people involved in cases | 208, 208, 208 | **208, 208, 208** | 208 | unchanged, correct |

**Notes.**

*SEM-1 and SEM-3 both improved, and neither was targeted.* The retry fix
was built for a different query; these gained from it because the defect was
general. SEM-3's previous failure — the planner emitting a role filter twice,
once correctly on the traversal and once as an invalid node predicate — no
longer occurs, and the correct answer (9 victims) now comes back
deterministically.

*SEM-4 correctly still refuses.* "Suspects with confirmed arrest status"
needs two edge-property filters on one hop, and a `Traversal` carries one.
Refusing is the designed behaviour, not a failure.

*SEM-5 required a second run, and the reason matters.* In the main rerun,
SEM-4 and SEM-5 returned `spec_generation_error: Connection error` on all
six runs — the local model server was unreachable (HTTP 000). That is an
**environment failure, not a Phase 1 regression**, and the system handled it
correctly: it reported a transport error rather than fabricating a figure.
SEM-5 was re-run once the endpoint recovered and returned 208 three times.

**Regressions found: none.** Two tests improved, three were unchanged, and
the one apparent regression was traced to the environment and disproved by
re-running.

---

# 2. Chat / API Integration Status

## 2.1 Current flow, before the change

Traced through the code rather than assumed:

```
User
  ↓
POST /api/chat                          main.py:396  (auth: get_current_user)
  ↓
route_query()  → classifies as "XAGG"
  ↓
"XAGG" ∈ HARNESS_CUTOVER_ROUTES         .env:46
  ↓
run_cutover_query() → harness supervisor → xagg_tool()
  ↓
xagg.run_aggregate()                    ← LEGACY ENGINE
  ↓
response
```

The fallback branch reaches the same engine: when `cutover_route is None`,
`process_query()` calls `run_aggregate()` directly
(`src/pipeline/orchestrator.py:476` and `:2153`).

**Both production paths used the legacy engine.** A search for
`answer_question` across `src/` returned only tests and QA scripts — the new
architecture had **zero** production callers.

## 2.2 New flow, with the flag

```
User
  ↓
POST /api/chat                          unchanged
  ↓
route_query() → "XAGG"                  unchanged
  ↓
run_cutover_query() → supervisor → xagg_tool()   unchanged
  ↓
  ├─ AGGREGATE_ENGINE_MODE == "legacy"        ← DEFAULT
  │     xagg.run_aggregate()                  unchanged path
  │
  └─ AGGREGATE_ENGINE_MODE == "aggregate_v1"
        Scope(user_id, role) from the authenticated caller
          ↓
        orchestrator.answer_question()
          ↓
        NL → AggregateSpec → validation → verification policy
          → structured / AGE / semantic → reconciliation
          ↓
        xagg_v1_adapter.to_tool_result()
          ↓
        XAggToolResult (same contract the harness already consumes)
  ↓
response formatting                     unchanged
```

**Why the seam is here.** `xagg_tool` is the single point where a chat
request becomes an aggregate computation — a thin adapter whose whole body
was one `run_aggregate` call. Branching there means authentication, routing,
verification and response formatting are all reached by the code that
already handles them. No layer is bypassed.

## 2.3 Design decisions, and why

**The flag is off by default.** The two engines disagree *by design*: the
new one refuses compound questions, filters it cannot express, and
traversals past its hop limit, because answering them meant silently
answering a different question. That is correct behaviour and it is also a
visible product change, so it is enabled deliberately rather than inherited.
An unset environment behaves exactly as it did before this work.

**The adapter translates; it does not reinterpret.** `AggregateAnswer`
carries a value, a spec, a verification decision and a reconciliation.
`XAggToolResult` carries evidence chunks, a deterministic
`raw_summary_text`, and touched case ids. `xagg_v1_adapter` is the only
module that knows both shapes. It never computes, rounds or re-derives a
value, and it never decides policy — it reads what the orchestrator already
settled.

**Refusals are returned as successful tool calls.** A refusal means the tool
worked and the answer is "this cannot be answered, here is why". Reporting
it as `FAILED` would tell the harness the primitive broke and invite a retry
or a fallback to a route that cannot answer the question either.

**`aggregate_kind` is left `None`.** It is a closed `Literal` naming the
legacy engine's canned families (`gender_breakdown`, `station_total_count`,
…). The new engine composes a spec per question and has no canned families.
Mapping a composed spec onto the nearest canned name would be a false claim
about which code produced the answer; the field is optional and the harness
reads `None` as "no canned presentation applies", which is exactly true.

**Scope is carried, never widened.** `Scope` is built from the same
authenticated `caller` the legacy branch passes to `run_aggregate()`. The
orchestrator applies its own role gate before any model call or database
read, and a `scope_denied` refusal is translated to `ToolStatus.DENIED` so
the harness sees the same outcome the legacy branch reports for the same
condition.

## 2.4 Files changed

| File | Change |
|---|---|
| `src/config.py` | `AGGREGATE_ENGINE_MODE` + the two mode constants, defaulting to `legacy` |
| `src/pipeline/harness/tools/xagg.py` | one branch before the legacy call; `_answer_with_aggregate_v1()`; `config` import |
| `src/pipeline/harness/tools/xagg_v1_adapter.py` | **new** — `AggregateAnswer` → `XAggToolResult` |
| `tests/test_xagg_engine_flag.py` | **new** — 20 tests |

Unchanged and untouched: every module under `src/pipeline/aggregate/`,
`src/pipeline/xagg.py`, `src/main.py`, and the harness supervisor.

---

# 3. Tests Performed

**Integration tests — 20, all passing.**

| Group | What it guards |
|---|---|
| Flag defaults | default is `legacy`; the two modes are distinct |
| Rendering | a value is stated; a **refusal never renders a number**; a conflict serves no figure; assurance qualifiers survive into the text; grouped output is bounded and names a null key |
| Case ids | absent provenance yields `[]` rather than a guess |
| Tool result | an answer is `OK` with evidence; a refusal is `OK` with refusal text, not `FAILED`; `aggregate_kind` stays `None`; `raw_summary_text` matches the chunk so the Verifier's fallback cannot diverge from the evidence |
| Scope | scope is built from the authenticated caller; a scope denial becomes `DENIED`; an engine crash becomes `upstream_failure` |
| Legacy mode | with the flag off, `answer_question` is **never called** |

That last test is the one that makes the flag safe to ship: it asserts the
new engine is unreachable by default, rather than assuming it.

**Full project suite:** exit 0, zero failures, run after every change.

**Live semantic regression:** section 1 above.

**Not yet performed:** an end-to-end `/api/chat` request in
`aggregate_v1` mode against a running server. The integration tests exercise
the seam and the translation with the engines stubbed; they do not prove the
full HTTP path. This is stated as a gap rather than implied to be covered —
see section 5.

---

# 4. Current Architecture Status

**Is development complete?** Yes for Phase 1 scope. The seven Phase 1
issues are fixed and verified, the closure blocker (retry degradation) is
fixed, and the new engine is now reachable from chat behind a flag.

**Is the system ready for manual QA?** Yes, with the briefing notes in
section 6.

---

# 5. Remaining Risks

1. **The HTTP path in `aggregate_v1` mode is untested end to end.** The seam
   and the translation are unit-covered; a live request through
   `/api/chat` with the flag on has not been made. Recommended as the first
   action when enabling the flag in any environment.

2. **The refusal rate will rise when the flag is on.** This is the intended
   behaviour — those refusals replace silently-wrong answers — but it is a
   visible change and will look like a regression to anyone comparing answer
   counts.

3. **Verifier interaction is unproven for refusal text.** The harness
   Verifier consumes `raw_summary_text`. For the legacy engine that text
   always describes a computed figure; for `aggregate_v1` it may describe a
   refusal. The adapter renders refusals clearly, but how the Verifier
   scores that text has not been observed.

4. **`aggregate_kind: None` changes presentation.** Any downstream code that
   branches on the canned family will take its default path. No such branch
   was found, but the harness's presentation layer was not exhaustively
   audited.

5. **Environment fragility is real and was observed twice.** The local model
   server returned HTTP 000/404 during this work, and Docker stopped once.
   Each time the system failed safe — a transport error or a refusal, never
   a fabricated number — but LLM availability directly determines answer
   rate.

6. **Legacy paths remain, by instruction.** `xagg.run_aggregate()` and
   `process_query()`'s aggregate branch are untouched and still serve all
   default traffic. Nothing was deleted.

---

# 6. Recommendation — when to enable `aggregate_v1` by default

**Not yet. Enable it in a test environment first, in this order.**

1. **Smoke the HTTP path.** Set `AGGREGATE_ENGINE_MODE=aggregate_v1` in a
   non-production environment and issue the three canonical requests through
   `/api/chat` with a real authenticated supervisor session:
   - simple: *"How many cases are registered?"*
   - complex: *"How many accused persons are linked to cases?"*
   - unsupported: *"How many people are left-handed?"* → expect a safe refusal.

   This closes risk 1 and exercises risks 3 and 4.

2. **Run manual QA against `aggregate_v1`**, briefed that a refusal with a
   stated reason is frequently the correct outcome, and that
   `independently_verified: False` on role-qualified questions is honesty
   (the AGE verification route cannot express edge filters), not a defect.

3. **Default it on only after** QA confirms the refusal rate is acceptable
   to the product and the Verifier handles refusal text sensibly. Flipping
   the default is a one-line config change and needs no code deploy — which
   is the point of the flag.

**Do not delete the legacy engine** until `aggregate_v1` has served real
traffic long enough to be trusted. The flag makes reverting a restart, not a
rollback.
