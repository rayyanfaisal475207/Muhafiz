# Verification Policy — V2 (production first version)

**Design review only. No code was modified.**

V2 revises V1 against measured evidence from the Phase 7D corpus run. The
revision is substantial: **V1's trigger set was both too broad and had a
hole in it**, and the measurement shows exactly where.

---

## 0. What the evidence actually says

I re-ran V1's triggers against the 42-case Phase 7D results and asked two
questions: where did verification *find* something, and where did it cost
41–92 s to confirm what was already right?

### Verification found something in 5 cases

| Case | Outcome | V1 trigger that fired |
|---|---|---|
| `time_window_2026` | CONFLICT (73 vs 51) | **T1** multi-source |
| `time_window_2024` | CONFLICT (0 vs 13) | **T1** multi-source |
| `accused_distinct_through_relationship` | CONFLICT (208 vs 92) | **T2** traversal |
| `weapons_unlicensed_property_filter` | SEMANTICALLY_DIFFERENT | **none** |
| `malkhana_records` | SEMANTICALLY_DIFFERENT | **none** |

### Verification confirmed a correct answer in 8 cases

Of 8 AGREEMENT cases, **2 would have been forced to verify by T2** — paying
full AGE latency to confirm a figure the structured route already had right.

### Two findings that change the design

**Finding A — V1 had a hole.** Two of the five disagreements fired *no V1
trigger at all*. Both are single-source, zero-traversal, dense-property
counts — precisely V1's "structured-only is safe" profile:

```
weapons_unlicensed_property_filter  count_distinct / ENTITY / Weapon
    predicate Weapon.license_status op=contains  presence 30/32 (94%)
malkhana_records                    count_distinct / RECORD / StructuredRecord
    predicate StructuredRecord.record_type op=eq  presence 713/713 (100%)
```

V1 would have served both structured-only. Both were genuine semantic
disagreements. **The signal V1 missed is not sparsity or traversal — it is
a property-value filter, where the model can pick a wrong literal, and a
non-ENTITY grain declaration.**

**Finding B — T4 (sparse field) caught nothing.** It fired on 6 cases, none
of which disagreed. Sparsity is a *coverage-warning* concern, already
handled by `INSUFFICIENT_DATA_COVERAGE` at serve time. It is not a
correctness-divergence signal, and V1 mis-classified it as mandatory.

---

## 1. Mandatory vs advisory

**Mandatory** = skipping verification would let a *measured* failure class
reach the user unnoticed.
**Advisory** = raises confidence; the failure it guards against is either
hypothetical or already caught by a cheaper mechanism.

### MANDATORY (4)

| # | Trigger | Condition | Measured basis |
|---|---|---|---|
| **M1** | Multi-source composition | `requires_multiple_sources` | 2 of 5 disagreements. The 73-vs-51 class — a restriction from one authority silently dropped when assembling across two. |
| **M2** | Fanning traversal | any traversal with `fans_in_direction(travel)` true | 1 of 5 disagreements (208 vs 92). Row multiplication: 73→449, 73→2100 measured. |
| **M3** | Value-literal filter | any `FieldPredicate` with `op` in `{eq, contains, in}` against a **string** property | **Finding A.** 2 of 5 disagreements. The model must choose a literal; `'بغیر لائسنس'` vs an English guess returns 0 instead of 30. Silent and total. |
| **M4** | Non-ENTITY grain declared | `grain != "ENTITY"` | **Finding A.** RECORD-vs-ENTITY was one of the two missed cases. A grain disagreement means the routes counted different *kinds of thing*. |

These four catch **5 of 5** measured disagreements.

### ADVISORY (4)

| # | Trigger | Why advisory, not mandatory |
|---|---|---|
| **A1** | Non-fanning traversal | T2 in V1. Caught 1 disagreement — but that one also fans, so M2 covers it. The 2 needless AGREEMENT verifications were both here. |
| **A2** | Sparse field (<50% presence) | **Finding B** — 6 fired, 0 disagreements. Already handled by `INSUFFICIENT_DATA_COVERAGE` at serve time. Cheaper mechanism already exists. |
| **A3** | Grouping | 4 fired, 0 disagreements. Group sums are checkable arithmetically against the population without a second route. |
| **A4** | Profile escalated / corrected | Signals the LLM misread the *profile*, which trusted code already corrected deterministically. The correction is the mitigation. |

### Reclassified from V1

- **T3 fanning** promoted into M2 as the *primary* form; T2's broad
  "any traversal" demoted to A1.
- **T4 sparse** → A2 (evidence: caught nothing).
- **T5 grouping** → A3.
- **T6 ratio** → removed as a trigger; becomes a *capability* question (§3).
- **T7 escalation** → A4.
- **T8 novel shape** → replaced by the lifecycle in §4.
- **M3, M4 are new** — added because V1 demonstrably missed two real cases.

---

## 2. Does 76% defeat selective verification?

**Yes, and V1's number was worse than it looked.** Verifying 32 of 42 cases
means paying 41–92 s on three-quarters of questions to catch 5
disagreements — and V1's set *still missed two of the five*. That is the
worst of both: high cost, incomplete coverage.

### V2 reach, measured

| Policy | Cases verified | Disagreements caught |
|---|---:|---:|
| V1 (T1–T8 mandatory) | 32 / 42 (76%) | 3 of 5 |
| **V2 (M1–M4 mandatory)** | **~14 / 42 (33%)** | **5 of 5** |

V2 verifies **less than half** as often and catches **more**. That is not a
tradeoff — V1 was simply mis-targeted, spending on traversal and sparsity
while missing value-literals and grain.

The reduction comes from dropping A1–A3, which together accounted for most
of V1's volume and zero of its catches.

---

## 3. Capability differences — what "verification" means per shape

AGE cannot verify everything. Making that explicit prevents a result
looking verified when only part of the check ran.

| Query shape | AGE | Semantic | Verification level | Result marking |
|---|---|---|---|---|
| Count / count_distinct, single source | available | available | **FULL** | verified |
| Multi-source composite (M1) | available | available | **FULL** | verified |
| Value-literal filter (M3) | available | available | **FULL** | verified |
| **Ratio / percentage** | **cannot** | available | **PARTIAL** | *corroborated, not independently computed* |
| **Median** | **cannot** | available | **PARTIAL** | *corroborated only* |
| **Time-window** | **cannot** (Postgres authority) | available | **PARTIAL** | *corroborated only* |
| Grouped breakdown | available | weak | **FULL** | verified |

**Definitions**

- **FULL** — AGE computes an independent number; reconciliation compares
  them. This is what AGREEMENT / CONFLICT mean today.
- **PARTIAL** — AGE cannot produce a comparable figure. Semantic evidence
  runs; reconciliation records `SINGLE_ROUTE_VALID` with
  `semantic_support`. **The answer must be marked as not independently
  verified** — otherwise PARTIAL is indistinguishable from FULL in the
  output, which would overstate assurance.
- **UNAVAILABLE** — neither route can contribute. Serve structured-only
  with an explicit marker, or refuse (open question Q3 from the prior
  review, still unresolved).

**Consequence for M1:** time-window questions are the largest multi-source
class, and AGE cannot verify them. So M1 frequently resolves to PARTIAL,
not FULL. This must be stated in the result, not silently absorbed.

---

## 4. Shape validation lifecycle

Replaces V1's T8, which could not ship (no inventory existed).

```
NEW ──────────► VERIFIED ──────────► TRUSTED
 │   first N runs    │  agreement      │  stable
 │   always verify   │  threshold met  │  advisory only
 │                   │                 │
 └───────────────────┴─── any CONFLICT ┘
                          demotes to NEW
```

**State definitions**

| State | Rule | Rationale |
|---|---|---|
| **NEW** | shape never executed, or demoted | Full algebra exposure means a shape may reach the compiler having never run. Verify until evidence exists. |
| **VERIFIED** | ≥ N executions, all AGREEMENT or SINGLE_ROUTE_VALID, zero CONFLICT | Evidence the two routes concur on this shape. |
| **TRUSTED** | VERIFIED and stable for M further runs | Verification becomes advisory; mandatory triggers M1–M4 still apply regardless. |

**Key rule:** a shape is identified by its **structural tuple**
`(measure, entity, traversal_count, predicate_count, has_grouping,
has_ratio, has_window, grain)` — *not* by `spec_hash()`, which includes
literal values. "Cases in 2024" and "cases in 2026" are the same shape with
different literals and should share a verification history.

**Demotion is the important half.** Any CONFLICT returns a shape to NEW
immediately. Promotion is slow, demotion is instant — the asymmetry that
makes the lifecycle safe rather than merely optimistic.

**TRUSTED never overrides M1–M4.** A trusted shape carrying a value-literal
filter still verifies. The lifecycle relaxes *shape novelty*, not the
measured failure classes.

### Open: thresholds

N and M are unset. They cannot be derived from current data — the corpus
runs each case once. A defensible first version: **N = 3, M = 10**, flagged
as a guess to be revisited once real traffic exists. I am not presenting
these as measured.

---

## 5. Decision flow

```
typed AggregateSpec
        ↓
validate()  ── not ok → refuse
        ↓
requirements_from_spec()  ← registry
shape_state lookup        ← lifecycle store
        ↓
M1…M4 fire?  ─── yes ──→ VERIFY (level per §3: FULL or PARTIAL)
        │ no
        ↓
shape_state == NEW? ── yes ──→ VERIFY
        │ no
        ↓
LLM recommends? ── yes ──→ VERIFY     (raise only, never lower)
        │ no
        ↓
A1…A4 fire?  ── yes ──→ record advisory note, do NOT force
        │
        ↓
STRUCTURED-ONLY  (marked unverified)
```

`effective_level = max(deterministic, llm_recommendation)` — reusing the
approved gate-escalation precedent.

---

## 6. Risks

| # | Risk | Severity | Note |
|---|---|---|---|
| R1 | M3 may be broad — most filters use string literals | MEDIUM | Measured: 3 of 42 corpus cases carry a predicate at all, so real-world impact is unknown. Revisit with traffic. |
| R2 | Lifecycle store is new persistent state | MEDIUM | Needs a home, and a migration if it lives in Postgres. Q1. |
| R3 | N=3 / M=10 are guesses | MEDIUM | Stated as such. |
| R4 | PARTIAL marking must reach the UI | **HIGH** | If PARTIAL renders identically to FULL, the policy silently overstates assurance — worse than not verifying. |
| R5 | Demotion needs CONFLICT history persisted | MEDIUM | Same store as R2. |
| R6 | A1–A3 demoted on 42 cases of evidence | MEDIUM | Small sample. If a demoted trigger later catches a real failure, promote it back — and record why. |

---

## 7. Summary

V2 halves verification volume (76% → ~33%) while catching **5 of 5**
measured disagreements instead of 3 of 5, by retargeting from
*structural complexity* to *measured failure classes*.

The correction worth stating plainly: **V1 was wrong, and the corpus said
so.** It forced verification on traversal and sparsity — which caught
nothing beyond what fanning already covered — while serving structured-only
on value-literal filters and non-ENTITY grain, where two real disagreements
lived. The evidence to see this existed when V1 was written; I did not
check it before proposing the triggers.

**Decisions still required:** the lifecycle store's home (R2), N/M
thresholds (R3), and how PARTIAL verification is surfaced to the reader
(R4 — the one that matters most, because a PARTIAL result presented as
verified is a stronger claim than the system can support).
