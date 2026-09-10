# Gold-32 rerun — the measured result after Wave 2

**Run 2026-09-10 against `main` @ `b52d712`**, three independent passes,
96 question-runs, 192 judge calls. Judge `gemini-3.1-flash-lite` at
temperature 0 (Module 87). Pass = `FactualCorrectness >= 0.5`.

## Headline

| | Module 27, re-scored | **this rerun** |
|---|---|---|
| **passing all three runs** | 20 / 32 | **26 / 32** |
| FactualCorrectness (96 runs) | 0.702 | **0.872** |
| AnswerRelevancy | 0.931 | 0.932 |
| routes stable | see Module 116 | **32 / 32** |
| UNSCORED cells | 0 | **0** |

**The comparison is against the re-scored 20, not the published 19.** Module 87
replaced the judge, so every figure produced before it came from a different
instrument. Comparing to 19 would overstate the gain by one question.

## Run integrity

Every check that has invalidated a run on this programme, per pass:

| check | pass 1 | pass 2 | pass 3 |
|---|---|---|---|
| questions completed | 32/32 `done` | 32/32 `done` | 32/32 `done` |
| empty answers | 0 | 0 | 0 |
| transport failures | 0 | 0 | 0 |
| **quota / provider-failure lines** | **0** | **0** | **0** |
| **cutover classification failures** | **0** | **0** | **0** |

Quota lines use the boundary-guarded pattern from Module 81 — the naive form
matched the log's own millisecond timestamps and certified poisoned runs clean.

**Two earlier attempts were discarded, and both reported success while measuring
the wrong thing:**

1. The backend served a checkout **274 commits behind `origin/main`** — none of
   Wave 2's fixes. Four questions came back on `XGRAPH` that should be `XAGG`.
   Kept as `evaluation/archive-INVALID-oldcode-*`.
2. The runner **resumed** from Module 27's committed artefacts and generated
   nothing; all 32 answers were byte-identical to it. Kept as
   `rerun3pass/DISCARDED_resume_of_module27.json`.

Neither surfaced as an error. Both were caught by comparing answers against the
previous run rather than trusting a completion count.

## Per question

| Q | type | route | FC 1/2/3 | mean | n-of-3 | spread | AR |
|---|---|---|---|---|---|---|---|
| D1 | Fact Retrieval | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| S2 | Fact Retrieval | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| A1 | Fact Retrieval | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| A7 | Fact Retrieval | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.67 |
| S3 | Fact Retrieval | XAGG | 0.9 / 1.0 / 1.0 | 0.97 | **3/3** | 0.1 | 1.00 |
| CP6 | Fact Retrieval | XAGG | 0.8 / 0.8 / 0.8 | 0.80 | **3/3** | 0.0 | 1.00 |
| CR2 | Complex Reasoning | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.53 |
| CR3 | Complex Reasoning | XNETWORK | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| CR4 | Complex Reasoning | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.99 |
| CR6 | Complex Reasoning | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.88 |
| CR7 | Complex Reasoning | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| CS4 | Complex Reasoning | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.86 |
| CP1 | Complex Reasoning | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.50 |
| CR8 | Complex Reasoning | XAGG | 0.9 / 0.9 / 0.9 | 0.90 | **3/3** | 0.0 | 1.00 |
| KB6 | Knowledge Base Reasoning | RAG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.96 |
| KB8 | Knowledge Base Reasoning | RAG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.80 |
| KB9 | Knowledge Base Reasoning | RAG | 0.9 / 1.0 / 1.0 | 0.97 | **3/3** | 0.1 | 0.93 |
| KB1 | Knowledge Base Reasoning | RAG | 0.7 / 0.7 / 0.7 | 0.70 | **3/3** | 0.0 | 0.87 |
| KB5 | Knowledge Base Reasoning | RAG | 0.4 / 0.6 / 0.6 | 0.53 | **2/3** | 0.2 | 1.00 |
| KB3 | Knowledge Base Reasoning | RAG | 0.4 / 0.6 / 0.4 | 0.47 | **1/3** | 0.2 | 1.00 |
| KB2 | Knowledge Base Reasoning | RAG | 0.3 / 0.2 / 0.2 | 0.23 | **0/3** | 0.1 | 1.00 |
| KB4 | Knowledge Base Reasoning | RAG | 0.2 / 0.2 / 0.2 | 0.20 | **0/3** | 0.0 | 0.98 |
| M1 | Contextual Summarization | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| M2 | Contextual Summarization | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| M4 | Contextual Summarization | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| M7 | Contextual Summarization | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| M5 | Contextual Summarization | XAGG | 0.9 / 0.9 / 0.9 | 0.90 | **3/3** | 0.0 | 1.00 |
| G2 | Creative Generation | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| G3 | Creative Generation | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| G5 | Creative Generation | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.87 |
| G1 | Creative Generation | XNETWORK | 1.0 / 0.9 / 0.4 | 0.77 | **2/3** | 0.6 | 1.00 |
| G6 | Creative Generation | XNETWORK | 0.0 / 0.4 / 1.0 | 0.47 | **1/3** | 1.0 | 1.00 |

## By question type

| type | FC mean | passing all three |
|---|---|---|
| Fact Retrieval | 0.961 | **6/6** |
| Complex Reasoning | 0.988 | **8/8** |
| Knowledge Base Reasoning | 0.637 | **4/8** |
| Contextual Summarization | 0.980 | **5/5** |
| Creative Generation | 0.847 | **3/5** |

Fact Retrieval, Complex Reasoning and Contextual Summarization are **19 of 19**.
Every remaining failure is Knowledge Base Reasoning or Creative Generation.

## What moved, and what moved back

**Gained (+10):**

| Q | before | after | FC |
|---|---|---|---|
| CR2 | 0/3 | **3/3** | 0.00 → 1.00 |
| KB6 | 1/3 | **3/3** | 0.57 → 1.00 |
| KB9 | 0/3 | **3/3** | 0.40 → 0.97 |
| KB1 | 0/3 | **3/3** | 0.30 → 0.70 |
| KB3 | 0/3 | **1/3** | 0.00 → 0.47 |
| M1 | 0/3 | **3/3** | 0.20 → 1.00 |
| M2 | 0/3 | **3/3** | 0.20 → 1.00 |
| G2 | 0/3 | **3/3** | 0.30 → 1.00 |
| G5 | 0/3 | **3/3** | 0.40 → 1.00 |
| G6 | 0/3 | **1/3** | 0.10 → 0.47 |

**Lost (−2):**

| Q | before | after | FC |
|---|---|---|---|
| KB5 | 3/3 | **2/3** | 0.90 → 0.53 |
| G1 | 3/3 | **2/3** | 0.97 → 0.77 |
### KB5 and G1 — variance, not regression

Both were re-scored at 3/3 on Module 27's **frozen** answers. Neither is
reproducing that here, and in both cases the answer text differs run to run:

- **KB5** 0.4 / 0.6 / 0.6, spread 0.2. Module 37 predicted the opposite — it
  measured KB5 as the orphan deletion's single clearest win, refused 3/3 →
  answered 3/3, anchor 0/3 → 3/3. It now answers every time and scores lower.
  The answer changed shape rather than disappearing.
- **G1** 1.0 / 0.9 / **0.4**, spread **0.6** — the largest spread in the set. It
  passed twice and failed once. Module 87 measured the *judge* at spread 0.0 on
  a fixed answer, so this is generation variance, not scoring noise.

Neither is attributable to a specific module with the evidence in hand. Both
need a repeat before anything is built on them.

## The six that do not pass all three

### KB2 — 0/3 (0.3 / 0.2 / 0.2). Gold's fact is not in the corpus

Gold is *"No, that's by design"* plus the assertion that the system stores only
witness identity. That second half is a **schema fact** — no field for
interview-statement text exists anywhere — and it is written in no document in
the corpus. **Retrieval cannot reach it at any quality.**

Module 37 proved this by accident: deleting the orphaned chunks removed the dead
citations from KB2's window exactly as predicted, and the answer got *worse*.
The verifier used to block it 5 of 7 times; now it answers confidently and
asserts the opposite of gold. The orphans were **masking** KB2, not causing it.

**What would close it:** an aggregate that reports a field's absence, the way
Module 95's `_missing_custody_controls()` is a set difference against the
register's real column inventory. Not a retrieval problem.

### KB4 — 0/3 (0.2 ×3). The generator drops a number it is handed first

`render_seized_property_disposition` opens its chunk with *"45 property-register
entries across 28 FIRs"*. The answer reproduces the 28, all nine dispositions
and gold's 13-forensic / 7-heirs pair, and states the **45 on 0 of 6 measured
runs**. Nothing upstream is broken.

Module 39's existing rule (*"the figures exactly as that summary gives them"*)
is already satisfied — a breakdown is a faithful **subset**. Module 86 tried the
obvious fix (*"INCLUDING the totals and denominators"*): **0 of 5**, and it
shipped alongside rules that cost KB4 two of its three answers. All reverted.

**Unowned on purpose.** A third restatement is the least likely thing to work,
and adding prompt rules to this generator is measurably dangerous.

### G6 — 1/3 (0.0 / 0.4 / **1.0**), spread 1.0. Half its runs collapse

The widest swing in the set, and it is understood. Module 83 measured the
failure as a **degenerate repetition loop** — 758 tokens, 18 unique, one 4-gram
370 times, byte-identical across six runs because decoding is greedy at
temperature 0. Its `_is_degenerate()` guard plus one regeneration at temperature
0.4 took the collapse from **100% to 50%**, and Module 110 then took coverage
from five of gold's seven findings to six.

At a 50% collapse rate G6 cannot pass 3-of-3 reliably. Its 1.0 in pass 3 shows
the ceiling is there when the synthesis survives.

**What would close it:** drive the collapse rate down further. The seventh
finding needs a ninth plan slot, and `_MAX_PLAN_SUB_QUERIES` is already at 8 —
9 exceeds the 150 s deadline (Module 59).

### KB3 — 1/3 (0.4 / 0.6 / 0.4). Routing fixed, synthesis half not

Module 78 fixed the routing: KB3 reaches RAG 3/3 and carries gold's *68 of 74
(92%)* figure. What it still does not do is draw gold's **conclusion** from
Police Order Article 18 — it quotes the article and stops. That is Module 49,
filed since wave 1 and never taken.

### KB5 — 2/3, and G1 — 2/3

Above. Both were passing before; neither is a code regression anyone has
attributed.

## What is still open

The tracker carries **~45 open rows**. The highest-value one is not on this list
of six:

**Module 123 — `supervisor.py::classify_to_subagent()` gates Meta-Analysis on a
literal trigger list.** Module 116 measured six paraphrases routing correctly
6/6 and reaching Meta-Analysis only **3/6**, and it is not language-driven: G6's
Urdu rewording passes while its English one fails, G1's Roman-Urdu passes while
its English fails. The variable is trigger-pattern membership.

That costs almost nothing on these 32 questions — which is exactly why it can
wait, and exactly why it matters. It is the difference between a system that
answers **these** 32 questions and one that answers a user's own words.

Related, at three other layers: Module 92 (router regex, measured), Module 106
(`resolve_aggregate_kind()` vocabulary), Module 111 (G6's rewordings refused).

## Reproducing this

Per `HOW_TO_REPRODUCE_THIS_EVALUATION.md`, with one addition that cost this run
two attempts:

**Verify the code revision, not just the data.** Check that the backend is
serving the commit you think it is. The data pre-flight passed on both discarded
attempts — the environment was perfect and the code was 274 commits stale.

Artefacts: `evaluation/rerun3pass/pass{1,2,3}_{outputs,results}.json`, one
backend log per pass, `per_question.json`, `baseline_module87.json`, and both
discarded attempts.
