# Phase 8 — Stabilization Checkpoint

**Status: complete. The system is ready for QA.**

No code was changed in this phase. Verification, documentation and risk
identification only.

Branch `feat/aggregate-engine-phase0` · HEAD `7fdd54c` · production
unchanged.

---

## 1. Current architecture

```
question → typed spec → requirement extraction → authority resolution
        → gate-profile selection → compatibility validation
        → direct OR composite execution → gate evaluation
        → coverage/confidence evidence → reconciliation
```

Three independent routes: **structured** (deterministic compiler, now with
multi-source composition), **AGE** (model plans, trusted code compiles),
**semantic** (evidence only, never numeric). Full detail in
`PHASE8_ARCHITECTURE_CHECKPOINT.md`.

---

## 2. Completed work since the redesign began

| Phase | Outcome |
|---|---|
| 0–4 | Schema registry, typed `AggregateSpec`, deterministic compiler, three-way harness |
| 5 | AGE has no read-only mode — established destructively |
| 5A–5C | Isolated evaluator database; containment proven |
| 5D | **Pivot**: model emits a typed plan, never executable Cypher |
| 6 | 42-case validation; 5 card defects found and fixed |
| 7 | Multi-path composition, canonicalization, consensus gates |
| 7B | Gate catalogue + profiles, composite wired into the live route |
| 7C | Full validation; 4 defects found |
| 7D | All 4 fixed; structured back to 25 correct / 0 wrong |

14 new modules (~4,110 lines), 478 tests, from a baseline of 190.

---

## 3. Verified guarantees

All five invariants verified against the live corpus run, not asserted.

| # | Invariant | Evidence |
|---|---|---|
| 1 | Raw LLM Cypher cannot reach production | `route_age.run` uses only `execute_compiled_age_query`; zero forbidden fields on `AgeQueryPlan`; execution targets across 42 cases = `{muhafiz.evidence_graph}` |
| 2 | A missing constraint cannot broaden the query | 0 cases served with an unapplied constraint; 3 refused naming theirs |
| 3 | Weak profile selection cannot weaken validation | 4 escalations + 6 corrections, 0 unknown, 0 incompatible; 0 escalations left the profile unchanged |
| 4 | Required gate failures block serving | 17 gate failures, **17 blocked, 0 served** |
| 5 | Coverage warnings preserved | 5 INSUFFICIENT results served, **5 carrying the warning** |

Structured route: **25 valid, 0 incorrect, 17 refused**.
`time_window_2026` = 51 · `min_person_age` = 24 · `max_person_age` = 49.

---

## 4. `compile_failed` investigation — not a defect

| | |
|---|---|
| **Case** | `avg_over_fanning_population_refused` |
| **Expected outcome** | refused (ground truth `None`) |
| **Root cause** | averaging over a fanning traversal would weight each value by its edge count; the compiler declines an unsound computation |
| **Severity** | none — counted as CORRECT_REFUSAL; structured correctness 25/25 |
| **Action** | none |

What changed between 7C and 7D is *which* refusal code fired: previously
`incompatible_gate_profile` (my gate bug), now `compile_failed` (the real,
pre-existing reason). The D1/D3 fix let the case reach the compiler that
had always been right about it. **A fix revealing correct behaviour, not a
new bug.**

---

## 5. Remaining implementation bugs

**None confirmed.** Three watch items, none a safety defect:

1. AGE wrong on 3 of 21 numeric results — all caught as CONFLICT, none
   served. This is the independent route doing its job.
2. The planner omits `gate_profile_id` on 20 of 42 cases; derivation
   covers it, but the selection path is under-exercised.
3. High planner retry rate (16 of 21 generations >25 s) — cost only.

---

## 6. Remaining capability gaps

AGE ratio/percentage (3 cases), threshold (1), comparison (1), median (1),
AGE time_window (10), composite MIN/MAX/AVG, scalar confidence weighting.
All deliberately unsupported. Of 14 `STRUCTURED_CORRECT_AGE_WRONG`
verdicts, **12 are approved gaps**, 1 a decline, 3 genuine AGE errors.

---

## 7. Repository state

7 tracked modifications, all intentional. 40 untracked files: 13 phase
reports, 14 source modules, 6 test files, 5 preserved corpus baselines,
plus 4 pre-existing items unrelated to this work.

**One cleanup recommended:** `.env.phase5.bak` — a Phase 5A backup holding
52 variables including credentials, **not git-ignored**. It should be
removed or ignored before QA. I have not deleted it: it is not mine to
discard unasked and may still be wanted as a rollback reference.

Credential rotation remains advisable (a `muhafiz_app` password appeared in
a Phase 7 traceback; never written to any source file).

---

## 8. Test status

**478 passed, 10 skipped** across 12 files. Coverage is strong on planning,
safety and reconciliation; adequate on routing and data correctness.

Five gaps identified but **not filled** (§8 said identify, not expand): live
composite via pytest (6 skipped, blocked on a product decision),
`compile_failed` end-to-end, two-source property filters, three-way
disagreement, `CompilerDefect` through the full route.

---

## 9. Production safety

| Object | Before | After |
|---|---|---|
| Case / Person / SAME_AS | 73 / 430 / 4702 | **73 / 430 / 4702** |
| vertices / edges / documents | 4942 / 13467 / 1012 | **4942 / 13467 / 1012** |

Unchanged. No destructive operation was performed in this phase.

---

## 10. Manual QA plan

`PHASE8_MANUAL_QA_PLAN.md` — six categories with measured ground truth:
basic (73, 208, 19), filters (9, 30, 45, 24/49), multi-source (13, 51, 28,
1, 19 groups), ambiguous, adversarial, and cross-cutting checks. Each entry
states expected behaviour, validation points, and whether failure is
acceptable.

It also lists **known-acceptable behaviours** so QA does not file approved
capability gaps as bugs.

---

## 11. Recommended next phase

**Manual QA against this checkpoint.** The system is stable, the
guarantees are verified, and the risks are documented.

Two decisions should be taken *before* deeper work, not during QA:

1. **Confidence-weighting policy** — the last unimplemented layer of the
   consensus design.
2. **Live composite test packaging** — six tests remain skipped because the
   conftest production guard blocks them and the evaluator database cannot
   host them.

**Do not change before QA:** gate catalogue or profiles, authority
resolution, the composite routing decision, or the AGE plan grammar.
