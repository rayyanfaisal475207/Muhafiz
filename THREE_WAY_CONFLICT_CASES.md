# Three-Way Conflict Cases

Scenarios where the three routes disagree. **Catalogued, not solved** — each
carries the open question, and where a prior phase already settled it, that
is marked.

Every example below is drawn from a measured corpus run, not invented.

---

## Category A — Structured vs AGE numeric disagreement

Both routes compute a number; the numbers differ.

### A1 · Same question, different population *(observed, 3 cases)*

| Case | Structured | AGE | Truth |
|---|---|---|---|
| `accused_distinct_through_relationship` | 92 | **208** | 92 |
| `time_window_2024` | 13 | **0** | 13 |
| `time_window_2026` | 51 | **73** | 51 |

AGE dropped a role filter (208), matched nothing (0), and dropped a date
restriction (73). **Current behaviour:** `CONFLICT`, no value served.

**Settled (Phase 6–7D):** conflicts are never averaged and no route is
preferred. `reconcile.py` returns `value=None` on CONFLICT.
**Still open:** what the *user* sees. A refusal? The structured figure with
a warning? Today this never reaches a user, because the chat path doesn't
use this engine.

### A2 · Same number, different declared semantics *(observed, 2 cases)*

| Case | Structured | AGE |
|---|---|---|
| `weapons_unlicensed_property_filter` | 30 / `count_distinct` / ENTITY | 30 / `count` / ENTITY |
| `malkhana_records` | 45 / `count_distinct` / **RECORD** | 45 / `count` / **ENTITY** |

Identical values, different meaning. **Settled (Phase 7):**
`canonical.py` proves `count ≡ count_distinct` only where no traversal
exists and the label's key is measured unique — otherwise they stay
`SEMANTICALLY_DIFFERENT`. The 45 case stays different on *grain*, which is
a genuine disagreement about what one unit is.

### A3 · Agreement by coincidence

`time_window_2024` agreed at 13 in Phase 6 while filtering *different
fields* (`Incident.report_datetime` vs `cases.incident_date`). Numerically
equal, semantically unrelated.

**Open:** nothing detects coincidental agreement. Two routes reaching the
same number by different populations currently reads as AGREEMENT.

---

## Category B — One route unavailable

### B1 · Structured succeeds, AGE unsupported *(observed, 12 cases)*

Ratio, threshold, comparison, median, time-window — AGE declines by design.
**Current:** `SINGLE_ROUTE_VALID`, structured value served.

**Settled (Phase 6):** approved. Not an AGE failure.
**Open:** should an unverified answer carry a visible "no independent
verification" marker? Today the classification records it; nothing surfaces
it to a reader.

### B2 · AGE succeeds, structured refuses *(observed, 3 cases)*

`fanout_case_person_no_key_refused` — structured refuses
(`missing_distinct_key`), AGE returns 73. The structured refusal is
*correct*; AGE answered a question it should not have.

**Open:** `SINGLE_ROUTE_VALID` here is arguably wrong — it implies the lone
route is valid, when the refusing route was the right one. Serving AGE's 73
would serve a known-unsound number.

### B3 · Both computation routes refuse

**Settled:** `REFUSED`. Semantic evidence alone cannot answer an aggregate.

---

## Category C — Semantic route interactions

### C1 · Semantic text contains a contradicting number

485 numeric mentions were recorded across 42 cases; **zero** were promoted.
`RouteResult.numeric` returns `None` for `EvidenceResult` by construction.

**Settled (Phase 4 onward):** semantic is corroboration only, never a third
numeric oracle.
**Open:** if retrieved text says "70 cases" and structured says 73, should
that lower confidence, raise a flag, or be ignored entirely? Currently only
`semantic_support` (SUPPORTS / CONTRADICTS / NEUTRAL) is recorded.

### C2 · Semantic asserts absence while structured returns a figure

`_semantic_stance()` returns `CONTRADICTS`; reconciliation sets
`requires_investigation=True` but still serves the value.

**Open:** is "evidence says no records exist" strong enough to withhold a
computed number? Current answer: no.

---

## Category D — Gate and coverage interactions

### D1 · Structured passes gates, AGE has no gate evaluation

Gate profiles apply to the structured route only. AGE has its own validator
and guard but no `GateEvaluation`.

**Open:** is an AGE result that never faced gate evaluation comparable to a
structured result that did?

### D2 · Coverage INSUFFICIENT on one route only *(observed, 5 cases)*

`min/max/avg/sum_person_age` and `threshold_cases_with_more_than_one_weapon`
serve with `INSUFFICIENT_DATA_COVERAGE`. AGE carries no coverage report at
all.

**Settled (Phase 7D):** INSUFFICIENT is served with a warning, not blocked.
**Open:** does the warning survive into a three-way response, and does it
apply when AGE agrees?

---

## Category E — Execution failures

### E1 · One route errors mid-flight

Handled per route today (`EXECUTION_ERROR`); the harness catches crashes
per route so one failure does not abort the run.

**Open:** in a live request, does a slow or failed AGE call block the
response? The AGE planner takes a median 41 s and a maximum 92 s.

### E2 · AGE planner times out

**Open entirely.** No timeout policy exists for a live request path. The
corpus harness simply waits.

---

## Summary

| Category | Observed | Settled | Open |
|---|---:|---|---|
| A · Numeric disagreement | 3 + 2 | no averaging; canonicalization proven | user-facing behaviour; coincidental agreement |
| B · Route unavailable | 12 + 3 | AGE gaps approved | unverified marker; B2 mislabel |
| C · Semantic | 42 | evidence only | contradiction handling |
| D · Gates / coverage | 5 | coverage advisory | AGE has no gate/coverage parity |
| E · Failures | — | per-route isolation | **live timeout policy** |
