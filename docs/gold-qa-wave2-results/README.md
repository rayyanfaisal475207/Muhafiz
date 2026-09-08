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
