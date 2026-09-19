# QA Readiness Report

**Status: ready for controlled manual QA, with one caveat that changes how
QA must be run.** See §4.1 — spec generation is not deterministic, so a
single run of a question does not establish its behaviour.

Every figure below comes from one full execution of the Smoke QA Set
against the live database on 2026-09-17. Nothing here is projected.

---

## 1. What is testable

The full production path executes end to end:

```
question -> NL->AggregateSpec -> validation -> verification decision
         -> structured [-> AGE] [-> semantic] -> reconciliation -> answer
```

`scripts/manual_qa_runner.py` runs it and prints every stage. A tester can
see the generated spec, the validation verdict, which triggers fired, which
routes ran, what each returned, how they reconciled, and whether the final
answer may be called verified.

### Measured on the smoke set (15 questions, one run)

| | |
|---|---:|
| Executed without a runner crash | 15 / 15 |
| Answered | 11 |
| Refused | 4 |
| Conflict | 0 |
| Independently verified | 3 |
| Verification levels | FULL 5, PARTIAL 3, NONE 4, n/a 3 |
| Mandatory triggers fired | M1×2, M2×3, M3×3, M4×1 |

All four mandatory triggers fired on real questions. The two failure
classes V1 missed were both caught:

- **`unlicensed weapons`** — the model selected the Urdu literal
  `'بغیر لائسنس'` from the schema card, M3 fired, structured and AGE both
  returned **30**, reconciled AGREEMENT, correctly marked verified.
- **`malkhana records`** — M3 fired; both routes returned **45**, but at
  different grains (RECORD vs ENTITY), so reconciliation returned
  `SEMANTICALLY_DIFFERENT` and the answer was **not** marked verified.
  Same number, different claim — correctly distinguished.

### Specific behaviours a tester can exercise

| Behaviour | How |
|---|---|
| Scope gate | `--role officer` — refuses `scope_denied` before any model call |
| Trigger firing | any question; read the DECISION block |
| PARTIAL marking | any temporal question — AGE must not run |
| Refusal paths | `median`, unknown labels, invalid fields |
| Coverage warnings | "people older than 30" (`Person.age` is 19/430) |
| Non-determinism | run the same question 3× and compare |

---

## 2. What requires human judgement

**The runner does not grade, and this is deliberate.** These questions have
no independently-established ground truth. A PASS column computed from the
same pipeline that produced the number would be the system marking its own
work. Graded evaluation against figures derived from direct SQL already
exists: `scripts/run_aggregate_three_way.py`, over the 42-case corpus.

The tester must judge four things **separately** (see
`MANUAL_QA_TEMPLATE.md`):

1. **Spec correctness** — did it understand the question?
2. **Route correctness** — did each route compute its own spec correctly?
3. **Final answer correctness** — is this the number the question asked for?
4. **Verification honesty** — does the stated confidence match what ran?

Separating 1 from 3 is what the smoke run showed to be essential.
`smoke_11` returned **73**: a perfectly correct count, of the wrong
population, because generation dropped the year filter. Judged as one
question — "is 73 right?" — the defect is invisible and the structured
route looks broken. It is not; the prompt is.

Judgement is also required where the system is right to be uncertain:

- Whether a refusal *should* have been an answer. `smoke_05` refused
  because `Case.status` is not a registered logical field. Correct
  behaviour, possibly a registry gap — a person decides which.
- Whether a `SEMANTICALLY_DIFFERENT` grain split is real or pedantic.
- Whether a coverage-warned figure is usable for the asker's purpose.
- Whether a plausible-looking number answers the question asked.

---

## 3. What the smoke run already found

These are findings, not projections. Each is reproducible with the command
shown.

| # | Finding | Severity |
|---|---|---|
| F1 | **Same question, three answers.** "cases registered in 2026" returned 73, then CONFLICT, then 51 across three runs. 51 is the Phase 6 verified value. | **HIGH** |
| F2 | **Silent time-window drop.** The model omitted `time_window` entirely, turning "cases in 2026" into "all cases". Every later stage behaved correctly on the wrong spec. | **HIGH** |
| F3 | **Nonsense answered, not refused.** "How many unicorns were seized?" produced `count(Weapon) where canonical_name = 'unicorn'` → **0**, with two coverage warnings. AGE declined explicitly (`model_declined`). A zero is a defensible answer to a filter that matches nothing, but the question named an entity that does not exist. | MEDIUM |
| F4 | **Wrong relationship, and the role filter dropped.** "distinct people *accused*" (`smoke_07`) produced a spec byte-identical to "distinct people involved" (`smoke_06`): `BELONGS_TO_CASE`, no role filter — **208** for both. Verified against the database: role lives on `Person-[INVOLVED_IN]->Incident` (94 accused edges), and the true answer is **92**. `BELONGS_TO_CASE` carries no `role` property at all, so no filter on that hop could ever have worked. | **HIGH** |
| F5 | **Gate refusal on a legitimate question.** `smoke_09` (2024 cases with >1 officer) refused: `MULTI_SOURCE_FILTERED_DISTINCT_COUNT` failed `STABLE_JOIN_KEYS` and `SET_COMPOSITION_COMPLETE`. Whether the gate or the spec is at fault needs investigation. | MEDIUM |

```bash
PYTHONPATH=. python scripts/manual_qa_runner.py -q "How many cases were registered in 2026?"
PYTHONPATH=. python scripts/manual_qa_runner.py --category relationships
```

F1, F2 and F4 are all the same layer: **NL→AggregateSpec is the weakest
component in the pipeline.** Every route, gate and reconciliation behaviour
observed in this run was correct on the spec it was given.

---

## 4. Current limitations

### 4.1 Spec generation is not deterministic — read this before testing

Temperature is 0, but the local model still produces different specs for
the same question across runs (F1). **Consequence for QA: a single run does
not establish behaviour.** Run each question at least three times before
recording a verdict, and record the variation rather than the last result.

This also means an intermittent defect can look fixed. It usually is not.

### 4.2 Latency

Measured over the smoke set:

| | total | generation |
|---|---:|---:|
| min | 5.1 s | 5.1 s |
| median | 18.9 s | 11.0 s |
| max | 64.6 s | 59.6 s |

**Generation is 66% of total wall time.** The full 15-question set took
6 min 26 s. Budget roughly 20 minutes for three passes.

A FULL-verification question adds the AGE route on top (7.5–37.8 s
observed).

### 4.3 No HTTP or UI surface

`answer_question()` is callable, but **no endpoint invokes it**. QA is via
the script only. The chat UI still uses the legacy path, so testing through
the browser does **not** exercise this architecture.

### 4.4 No timeout policy

Nothing bounds a slow route in a live request. One generation took 59.6 s.
Conflict-case E2 remains open.

### 4.5 Capability gaps (by design, must not be reported as bugs)

AGE cannot compute ratios, medians, bucketed comparisons, or
Postgres-authoritative time windows. Those resolve to **PARTIAL** — the
answer is corroborated but not independently verified, and says so. Only
report these if a PARTIAL result is presented *as* verified.

### 4.6 Smoke set is not a corpus

Fifteen questions across seven categories. Enough to prove the pipeline
runs and to catch obvious regressions. **Far too small to characterise
accuracy** — no rate derived from it should be quoted. It has no ground
truth attached, by design.

---

## 5. Known risks

| # | Risk | Severity | Note |
|---|---|---|---|
| R1 | Silent question alteration (F2, F4) — a correct computation over a population the asker did not ask for | **HIGH** | The most dangerous failure mode: nothing in the output looks wrong. Verification does not catch it, because both routes are given the same wrong spec. |
| R2 | Non-determinism (F1) masks and un-masks defects between runs | **HIGH** | Mitigate by running 3×. |
| R3 | Verification cannot detect a shared-input error | **HIGH** | Structural: AGE gets the question, structured gets the spec, but a dropped filter means both answer the altered question. This is the boundary of what three-way verification can do, and it is not fixable by adding routes. |
| R4 | M3's production breadth is unmeasured | MEDIUM | Fired on 3 of 15 here. Real traffic may differ enough to change the cost profile. |
| R5 | A2/A3 demoted to advisory on 42 cases of evidence | MEDIUM | If an advisory trigger coincides with a real disagreement in QA, record it — that is the evidence to promote it back. |
| R6 | Registry gaps look like refusals (F5, `smoke_05`) | MEDIUM | A missing logical field and a genuinely unanswerable question produce similar output. |
| R7 | The 42-case corpus and this smoke set share no ground truth | LOW | Use `run_aggregate_three_way.py` for graded numbers; this runner for inspection. |

**R3 is the one to keep in view.** Three-way verification checks that two
routes computing the *same spec* agree. It cannot check that the spec
matches the question. F2 and F4 both passed through verification
untroubled, because there was nothing for verification to disagree about.

---

## 6. Recommended QA order

1. **Categories with known-correct figures first** — simple counts (73
   cases, 19 stations). If these drift, stop.
2. **Value literals** — the class V1 missed; now the best-behaved.
3. **Relationships** — where F4 lives. Check role filters specifically.
4. **Temporal** — where F1 and F2 live. Three runs minimum.
5. **Multi-source** — where F5 lives.
6. **Unsupported** — confirm refusals stay refusals.

For each: run 3×, record all three outcomes, and judge the four checks
separately.

---

## 7. Files

| File | Purpose |
|---|---|
| `scripts/manual_qa_runner.py` | Execution and inspection |
| `MANUAL_QA_TEMPLATE.md` | Recording format, with a worked example |
| `docs/aggregate-shadow/manual_qa_report.json` | Machine-readable last run |
| `scripts/run_aggregate_three_way.py` | Graded evaluation vs ground truth (unchanged) |

No production code was modified for QA tooling. `executor.run_aggregate()`,
`shadow.py` and `xagg.py` are untouched; existing evaluation baselines
remain valid.
