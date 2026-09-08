# Gold-QA Wave 2 — results directory

One result file per module, written by that module's own PR. This directory is
the record of **what was actually measured**, separate from
`GOLD_QA_REMAINING_FIXES_PLAN.md` (the tracker) and the `MODULE*_PROMPT.md`
briefs (the instructions).

**Baseline everything is measured against** — Module 18's run
(`MODULE_18_FINAL_REPORT.md`): FactualCorrectness **0.428** all-32, **0.550**
excluding KB, **14/32** passing.

## Index

| Module | Question(s) | Result file | Status |
|---|---|---|---|
| 23 | M5 — weapon × statute co-occurrence | `MODULE23_RESULT.md` | in progress |
| 28 | CR4 — weapon-evidence chain routing | `MODULE28_RESULT.md` | in progress |
| 24 | M4 — statute × court-stage join | `MODULE24_RESULT.md` | done |
| 29 | CR3/G1/G6 — Meta-Analysis decomposition | `MODULE29_RESULT.md` | done — G1/G6 pass, CR3 partial; gap analysis split out as Modules 31–36 |
| 30 | KB3/KB8/KB9 — retrieval completeness | `MODULE30_RESULT.md` | done — KB3/KB8/KB9 stopped abstaining; new defects split out as Modules 37–39 |
| 27 | Final Gold-32 rerun | `MODULE27_RESULT.md` | queued (last) |
| 31 | G1 — offender age profile | `MODULE31_RESULT.md` | done — aggregate live and correct; G1 unchanged (not yet wired into its plan) |
| 32 | G1 — accused↔complainant relationship | `MODULE32_RESULT.md` | done — same; kills the person-recurrence fall-through |
| 33 | G1 — seized-property disposition | `MODULE33_RESULT.md` | done — same; reproduces gold's 13 / 7 exactly |
| 34 | G1 — incident time-of-day | `MODULE34_RESULT.md` | done — same; gold's "flat across the day" is a date-only artefact |
| 35 | G6 — arrest rate | `MODULE35_RESULT.md` | done — aggregate live with a published rule; **1 in 6.6**, not gold's 1 in 9 |
| 36 | CR3 — subject-filtered FIR listing | `MODULE36_RESULT.md` | done — returns `fir-64-26`/`fir-65-26` exactly; wiring into `record_consistency` deferred behind Module 41 |
| 45 | Eval harness — a judge `null` scored as zero | `MODULE45_RESULT.md` | done — nulls retried then excluded from the mean; the 900-char cap tested and cleared of every current 0.0; split out as Modules 46–47 |
| 41 | G2/G5 — Meta-Analysis over-decomposition (regression) | `MODULE41_RESULT.md` | done — guard generalised from one pattern list to "does XAGG resolve this?"; G2/G5 correct and deterministic on 3/3 live runs each |
| 42 | KB6 — `route=None`, FC 0.0 **and** AR 0.0 | `MODULE42_RESULT.md` | done — **not a pipeline defect**: `route=None` was the harness's own 300s client timeout, live KB6 is `route='RAG'` 5/5. A timeout is now unscored, not a 0.0. The real cause of the abstention (a cross-language relevance gate) split out as Module 52 |
| 43 | M7 — incident-to-report mean, filed as factually wrong | `MODULE43_RESULT.md` | done — **the report is wrong, not Module 22**; M7 returns gold on 6/6 live runs. Fixed the missing `XAGG <kind>` log line; independently confirms Module 47 |
| 44 | M2, CS4 — station specialisation, and criminal-record vs local-FIR gap | `MODULE44_RESULT.md` | done — both gold answers computable and now computed, 3/3 live each. CS4 returns وقاص / 00000-9000020-1 exactly; M2 reproduces 9-of-73 from 2-of-19 and **challenges gold's growth claim** |
| 50 | G1/G6/CR3 — wiring Modules 31–36's aggregates into their plans | `MODULE50_RESULT.md` | done — six aggregates wired and firing on every live run; `_MAX_SUB_QUERIES` settled at **5** with the measured timeout data; G6 trades `_SQ_GENDER` for the arrest rate; defects split out as Modules 53–54 |
| 38 | KB4 — `cross_rerank_multi()` merged by max score across queries | `MODULE38_RESULT.md` | done — fused by reciprocal rank, reusing `reranker.py`'s own RRF. KB4's retrieval goes **0-of-3 → 3-of-3** runs containing gold's rule 27.16; its answer/abstain `status` goes **3-of-3 → 1-of-3**, because all three `main` runs "answered" that the corpus does not contain this. KB6 and KB9 both stop abstaining; bucket unchanged at 6/8. Module 30's rejected third statute hypothesis re-measured and no longer harmful — split out as its own module |
| 55 | Every XAGG aggregate — which one answered a live run? | `MODULE55_RESULT.md` | done — measured **32 kinds, 11 log lines, 21 families silent**; all 21 now log with their figures, confirmed live in-process. The durable fix is an AST-derived test that fails when a new family has no line. Backend/SSE run **deferred for contention** |
| 56 | M2 — dispatch vocabulary vs. the family Module 44 gave it | `MODULE56_RESULT.md` | done — `_STATION_TYPE_KEYWORDS` 10 → 58 entries across English/Roman-Urdu/Urdu; **all-32 equality control byte-identical**; the measured failing phrasing and 8 non-gold paraphrases now reach `station_caseload_by_specialisation`. New defect split out as Module 58 |
| 53 | G1/G6/CR3 — the shared sub-query deadline that measures queue position | `MODULE53_RESULT.md` | done — raw aggregate now served when a paraphrase is cancelled; **8 of 10 dropped sub-answers → 0** in a forced-deadline control, and **zero dropped at N=5 over 12 live runs**; timeout 60 → 150 s; split out as Modules 59/59 |
| 57 | CR3 — non-determinism at three layers | `MODULE57_RESULT.md` | done, **no code change** — 20 runs: timeouts 0/20 (fixed by 53), `route=None` 0/20 (did not reproduce), synthesis-verifier rejection 7/14 and now the only failure left. Its reason measured **not** to be citation attribution; split out as Modules 61/61 |
| 40 | M4 → CR3/G6 — the synthesis verifier's attribution-blind second opinion | `MODULE40_RESULT.md` | **closed as superseded** — merged in and judged: 8 live runs, the second opinion **never fired**; measured directly, it works as designed but cannot rescue CR3's negative-inference rejection (3/3). Hallucination still rejected 3/3. Branch left unmerged on origin for Module 59 |

## Required shape for each result file

Every module's result file must contain all of the following. A module whose
file is missing a section is not finished.

1. **Root cause** — what was actually wrong, with the evidence that
   established it. If it differed from the brief's hypothesis, say so plainly
   and say what the brief got wrong.
2. **Change** — files touched, and why the fix sits where it does.
3. **Unit tests** — the command run, the pass/fail counts, and the new
   regression test pinned to the question's literal gold text.
4. **Live verification** — the exact question sent, the captured `route=`
   event proving which sub-agent handled it, and the **verbatim answer**.
5. **Gold comparison** — measured values against the gold answer's values,
   and an honest verdict. A mismatch that is reported is worth more than a
   match that was tuned for.
6. **Non-gold paraphrase** — the paraphrase used and its result. This is the
   check that separates a capability fix from curve-fitting.
7. **Regression guard** — which previously-passing questions were re-run, and
   their results.
8. **New defects found** — anything discovered but deliberately left
   unfixed, tracked as its own new module rather than folded into this one.

## Infrastructure state when this wave started (2026-09-08)

Verified live, so later runs have something to compare against:

- **Chroma** (`data/chroma_db`): `muhafiz_kb` 7,716 · `muhafiz_community_reports`
  18 · `muhafiz_entity_descriptions` 568.
- **Embeddings**: `e5` via the ngrok tunnel, real 1024-dim vectors returned.
  The tunnel's bare base URL returns 404 (no root route) — check `/health`,
  not `/`.
- **Graph (AGE)**: Case 73 · Incident 73 · Person 430 · Officer 1,155 ·
  Weapon 32 · Date 137. Module 22's `incident_datetime` / `report_datetime`
  are present on **64 of 73** Incidents.
- **Postgres**: `DirectGateway`, 73 cases.

See `WAVE2_ORCHESTRATION_PROMPT.md` for the runbook and the measured machine
constraints that govern how many tracks can run at once.
