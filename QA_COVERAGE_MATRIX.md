# QA Coverage Matrix

Companion to `MANUAL_QA_CORPUS.md` — **135 cases** across 9 categories.

---

## Category summary

| Category | Number of Tests | Purpose |
|---|---:|---|
| Basic | 15 | Baseline counts; tombstone exclusion; source selection (graph vs Postgres); legitimate zeros |
| Filters | 15 | Single-attribute filtering; Urdu value matching; sparse-field coverage warnings; ambiguous text matching |
| Multi constraint | 20 | Constraint completeness; single- vs multi-source routing; population intersection; grain traps |
| Temporal | 15 | Date authority; inclusive bounds; empty windows; inverted ranges; missing temporal authority; relative time |
| Relationships | 15 | Graph traversal; direction and grain; versioned edges; fanning paths; self-referential edges |
| Analytical | 15 | Value aggregates; grouping; unsound aggregates that must refuse; ranking and trend gaps |
| Ambiguous | 15 | Undefined terms; subjective qualifiers; domain vocabulary that does and does not map |
| Unsupported | 10 | Median, percentile, stddev, threshold, top-N, forecasting, rates — hallucination resistance |
| Security | 15 | Prompt injection; Cypher injection; constraint removal; PII extraction; role claims; encoding evasion |
| **Total** | **135** | |

---

## Expected-behaviour distribution

| Expected behaviour | Cases | Share |
|---|---:|---:|
| Answer (exact value known) | 48 | 36% |
| Answer (requires ground-truth calculation) | 28 | 21% |
| Answer **with mandatory coverage warning** | 14 | 10% |
| Refusal / clarification expected | 45 | 33% |

**A third of the corpus expects a refusal.** That is deliberate: the most
dangerous failure mode is a confident number where the system should have
declined.

---

## Route coverage

| Expected route | Cases | Notes |
|---|---:|---|
| structured (direct) | ~58 | single authoritative source |
| composite (multi-source) | ~22 | Postgres date + graph facts |
| refusal | ~45 | unsupported, ambiguous, or unsafe |
| AGE unsupported / structured-only | ~6 | ratio cases, approved gap |
| semantic | 0 | evidence only; never a numeric answer |

---

## Known-failure classes each category probes

| Failure class | Probed by | Signature of failure |
|---|---|---|
| Merge-tombstone leak | QA-002, 066, 122 | **430** where **208** expected |
| Superseded-edge leak | QA-066, 070 | **429** or **144** instead of 208 / 76 |
| Constraint dropping | QA-031, 051, 127, 128 | **73** for a 2026 question |
| Grain confusion | QA-050, 071, 077, 094 | 70 vs 4; 449 vs 208; 94 vs 92 |
| Fanout inflation | QA-076, 086, 095 | counts exceeding the population |
| Urdu value mismatch | QA-017, 020, 135 | **0** where 85 / 30 expected |
| Missing coverage warning | QA-016, 025, 081–084 | answer served with no disclosure |
| Wrong date authority | QA-063, 064, 065 | a number instead of a refusal |
| Hallucinated capability | QA-085, 113–120 | any number at all |
| Undefined-term guessing | QA-096–107 | a number instead of clarification |
| Raw Cypher execution | QA-123, 124, 135 | **production counts change** |
| Value injection | QA-125, 126, 135 | 30 instead of 0; missing table |
| Role escalation via prose | QA-133 | access granted on a text claim |

---

## Ground-truth status

| Status | Cases | Meaning |
|---|---:|---|
| **Measured** | 62 | verified against the live database while writing this corpus |
| **Requires calculation** | 28 | tester must compute; the query is given |
| **n/a (refusal expected)** | 45 | no numeric answer should exist |

No numeric expectation in this corpus was invented. Where a value was not
measured, the case says so and supplies the query to run.

---

## Highest-priority cases

Run these first — each has caused a real defect in this system's history,
or would be a critical incident.

| Case | Why |
|---|---|
| **QA-124** | Embedded `DETACH DELETE` — verify production immediately after |
| **QA-051** | The original 73-vs-51 constraint drop |
| **QA-002** | 430-vs-208 tombstone leak |
| **QA-031** | Three constraints, two sources, non-vacuous |
| **QA-050** | The 4-vs-70 grain trap |
| **QA-086** | Unsound average over a fanning population must refuse |
| **QA-133** | Role claimed in prose must not grant access |
| **QA-135** | Urdu + SQL payload as a bound value |

---

## Deliberate traps for the tester to be aware of

**QA-032** (persons 20–30 in 2026 = 9) is **vacuous**: the unfiltered
answer is also 9, so passing it proves nothing about the date filter. It is
included precisely so a tester does not mistake it for evidence. QA-031
(the 2024 variant = 1) is the discriminating case.

**QA-108/109/110** are domain terms that **should** resolve (malkhana 45,
zimni 259, drugs → CNSA 12), in contrast to QA-096–107 which should not.
A system that refuses all domain vocabulary is as wrong as one that guesses
at all of it.

**QA-072** has three defensible answers (126 graph nodes, 1012 Postgres
rows, 74 case-linked). The test is whether the answer states which
population it counted.

---

## Out of scope — do not file as bugs

- AGE declining ratio, threshold, comparison, median or time-window
  questions (approved capability gaps).
- Two same-number results classified `SEMANTICALLY_DIFFERENT` where
  equivalence is not provable.
- A CONFLICT serving no value — that is the architecture working.
- Planner latency of 30–90 seconds per question.
