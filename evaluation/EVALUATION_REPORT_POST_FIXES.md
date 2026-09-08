# Gold-32 Evaluation — Rerun After the Latest Round of Fixes

**Run date:** 2026-09-08
**Build:** `main` @ `06de8ea` (Wave 1 + Wave 2 modules: KB retrieval, XNETWORK
gating, derived aggregates, Meta-Analysis decomposition)
**Data:** regenerated 2026-09-08 dump + Chroma, restored fresh and verified
**Judge:** `gemini-flash-lite-latest`, "close numbers OK" prompt
**Sources:** `evaluation/gold32_results.json`, `evaluation/gold32_pipeline_outputs.json`

---

## 1. Setup error in the first pass of this run — and its correction

The first pass of this evaluation was invalid, through a **setup mistake on the
evaluation side, not a code defect**.

`SHARE/.env` was never copied into place during setup (SETUP.md step 1). The run
therefore used a stale local `.env` from 3 September, in which **3 of the 5 Groq
API keys differed** from the ones shipped in the new SHARE folder. The result was
**1,410 rate-limit errors** in the backend log, including hard failures:

```
Exception: Failed to call groq after 3 attempts due to rate limits.
```

Every KB question abstained after 224–600 seconds, and the KB bucket scored
0.000. That figure was an artefact of the wrong credentials, not a measure of
the KB modules.

**Correction applied:** `SHARE/.env` was copied into place, the backend was
restarted, and **only the failing questions were re-run**. 15 of the 17 failures
recovered immediately. All figures below are from the corrected run.

This is recorded explicitly so the earlier "KB regressed to zero" reading is not
mistaken for a defect in anyone's work.

---

## 2. Headline numbers (corrected run)

| Metric | Previous run | **This run** |
|---|---|---|
| FactualCorrectness — all 32 | 0.425 | **0.572** |
| Pass rate (≥0.5) — all 32 | 14/32 | **19/32** |
| FactualCorrectness — excluding KB (24 Q) | 0.546 | **0.646** |
| FactualCorrectness — KB bucket (8 Q) | 0.062 | **0.350** |
| AnswerRelevancy — all 32 | 0.687 | **0.886** |

**By question type:**

| Type | Previous | This run |
|---|---|---|
| Fact Retrieval | 0.98 | 0.95 (6/6 pass) |
| Complex Reasoning | 0.64 | **0.86** (7/8 pass) |
| Knowledge Base Reasoning | 0.06 | **0.35** (3/8 pass) |
| Contextual Summarization | 0.02 | **0.30** (2/5 pass) |
| Creative Generation | 0.40 | 0.28 (1/5 pass) |

Overall factual correctness improved from 0.425 to 0.572, and answer relevancy
from 0.687 to 0.886. Nineteen of 32 questions now pass, up from 14.

---

## 3. What the latest round of fixes achieved

Assessed per module against the question it targeted:

| Q | Module targeted | Before | After | Verdict |
|---|---|---|---|---|
| **M5** | weapon × statute co-occurrence | 0.0 | **1.0** | fixed |
| **CR4** | weapon evidence chain | 0.0 | **1.0** | fixed |
| **CR3** | XNETWORK relevance gate | 0.2 | **0.9** | fixed |
| **KB8** | KB retrieval | 0.0 | **1.0** | fixed |
| **KB1** | KB retrieval | 0.5 | **0.9** | improved |
| **KB5** | KB retrieval | 0.0 | **0.6** | fixed |
| M1 | year-over-year routing | 0.0 | 0.5 | fixed |
| G6 | orientation-note decomposition | 0.0 | 0.3 | improved |
| KB4 | KB retrieval | 0.0 | 0.2 | improved |
| KB3 | KB retrieval | 0.0 | 0.1 | improved |
| G1 | caseload-profile aggregates | 0.0 | 0.1 | improved |
| M2 | meta-synthesis verifier | 0.0 | 0.0 | no change |
| M4 | statute × court-stage join | 0.1 | 0.0 | no change |
| M7 | incident-to-report mean | 0.0 | 0.0 | no change |
| CS4 | XNETWORK gate | 0.0 | 0.0 | no change |
| KB2, KB6, KB9 | KB retrieval | 0.0 | 0.0 | no change |

**The KB work is confirmed effective.** With correct credentials, every KB
question now routes to RAG and returns substantive answers (1,000–2,500
characters), where previously all eight abstained. KB1 correctly cites
"Sections 154 and 155 of the Code of Criminal Procedure" with statutory text.
The bucket moved from 0.062 to 0.350, with 3 of 8 now passing.

**Six questions were fixed outright** (M5, CR4, CR3, KB8, KB5, M1) and five more
improved. Complex Reasoning rose from 0.64 to 0.86.

---

## 4. Two questions regressed — root cause identified

Six questions were verified working before this round. Four held or improved;
two regressed.

| Q | Before | After | Status |
|---|---|---|---|
| CR7 | 0.9 | **1.0** | improved |
| CR6 | 1.0 | **1.0** | held |
| CR8 | 1.0 | **1.0** | held |
| G3 | 1.0 | **1.0** | held |
| **G2** | 0.4 | **0.0** | regressed |
| **G5** | 0.6 | **0.0** | regressed |

G2 and G5 were re-run with correct credentials and **still failed**, so this is a
genuine code issue, not the credential problem described in section 1.

### Root cause — traced directly

The underlying aggregates were called in isolation and are **fully correct**:

- `_case_completeness_scan` returns 73 cases, 9 missing incident dates, 52 missing status
- `_weapon_compliance_scan` returns 30 of 32 weapons unlicensed
- `run_aggregate` dispatches correctly: G2 to `case_completeness_scan`,
  G5 to `weapon_compliance_scan`

The aggregate layer is intact. The failure is **above** it, in sub-agent
dispatch. The live pipeline trace for G5 shows:

```
supervisor:dispatch: Classified query as route='XAGG' -> sub-agent='Meta-Analysis'
```

The question routes to XAGG correctly, then is handed to the **Meta-Analysis**
sub-agent rather than the aggregate sub-agent. Meta-Analysis decomposes it into
sub-questions, one errors, and the synthesis is rejected:

> "The synthesized answer could not be verified as grounded in the sub-answers;
> Could not answer sub-question (encountered an error)."

G2 fails the same way, returning weapon-licence content instead of the
case-completeness content its question asks for.

### Why — an already-anticipated failure mode, scoped slightly too narrowly

`supervisor.py` contains a guard that skips Meta-Analysis decomposition when an
XAGG question already has a working single-call aggregate. Its own inline
comment describes exactly this failure:

> "Meta-Analysis decomposes a question XAGG already answers in one call into two
> independently-dispatched, undirected sub-queries ... each sub-query then DROPS
> the language that made this pattern list match in the first place, so its own
> re-classification is left to the flaky LLM router call one level down."

The guard is correct in principle but fires only for time-comparison questions
(`_TIME_COMPARISON_XAGG_PATTERNS`). G2 and G5 meet every other condition — they
route to XAGG and each has a working single-call aggregate — but do not match
the time-comparison patterns, so the guard never fires.

For completeness: the deterministic `_DECOMPOSITION_PLANS` (record-consistency,
orientation-note, caseload-review) were checked and correctly do **not** match
G2 or G5. The decomposition comes from the LLM decomposer fallback path.

### Suggested fix

1. **Narrow:** extend the guard with a pattern list covering the
   completeness-scan and weapon-compliance shapes, so G2/G5 skip decomposition
   as M1 already does.
2. **Structural (recommended):** rather than a parallel pattern list that will
   drift, have the guard ask `run_aggregate` whether it has a dispatch for the
   query. If it resolves to a real aggregate kind, skip decomposition. This is
   self-maintaining and prevents the whole class of regression.

---

## 5. Judge reliability note

CR8 initially returned **no FactualCorrectness score at all** (null) rather than
a number — a judge-side failure. Re-scoring that one question returned 1.0/1.0.
A null score should be re-run, never treated as zero.

---

## 6. Where things stand

**Working (19 of 32 passing):** all Fact Retrieval, 7 of 8 Complex Reasoning
including the four cross-record joins, the court-readiness scan, and 3 of 8 KB
questions.

**Fixed this round:** M5, CR4, CR3, KB8, KB5, M1 outright; KB1, KB3, KB4, G1, G6
improved.

**Open issues:**

1. **Meta-Analysis over-decomposition** — regresses G2 and G5. Guard exists but
   is scoped too narrowly. Clear, well-understood fix.
2. **M4, M7, M2, CS4** — unchanged at 0.0 across two rounds. M4 still routes to
   XGRAPH rather than reaching its purpose-built aggregate.
3. **KB2, KB6, KB9** — still 0.0 despite the bucket improving overall. These now
   return real answers rather than abstentions, so the remaining gap is answer
   quality rather than retrieval failure.

---

## 7. Recommended next steps

1. Widen the Meta-Analysis skip guard (section 4) to restore G2 and G5.
2. Investigate M4's XGRAPH routing — the aggregate exists and is correct, but
   the question does not reach it.
3. Review the three remaining KB questions, now that retrieval is confirmed
   working.
4. **Process note:** verify `SHARE/.env` is copied into place before any future
   evaluation run. This single omission invalidated an entire pass.
