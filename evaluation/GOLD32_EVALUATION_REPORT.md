# Muhafiz — Gold-QA Evaluation Report (32 Questions)

**What this is.** An automated evaluation of the fixed Muhafiz build against the
testing team's 32-question Gold-QA dataset (with their verified answer key), run
through the live `/api/chat` pipeline as **platform-admin with All Cases** —
matching the testing team's own conditions.

**Judge.** Google **Gemini flash-lite** (a strong, current model), grading each
answer against ground truth per the testing team's explicit rule: *"Don't match
word-by-word. If the app covers the main facts in its own way — even with extra
info — that's a pass. The problem is when it states something opposite or
incomplete."* (An earlier run used a weaker Qwen-27B judge that scored correct
answers unfairly low; it was replaced and its results discarded.)

**Ground truth.** The testing team's answer key, cross-checked against the raw
Muhafiz Data API where the answer is a computable fact (independently confirmed:
94 accused, 24 women, 8 delay-reason FIRs, 73/79 FIRs).

---

## 1. Headline results — honest, and lower than we'd hoped

Scored on the **24 non-KB questions** (8 Knowledge-Base questions were excluded —
see §4, they fail on a separate, confirmed retrieval bug that a teammate will fix).

| Metric | Value |
|---|---|
| **Factual Correctness** (semantic, vs ground truth) | **0.11 mean** — 1 pass, 3 partial, 20 fail (of 24) |
| **Answer Relevancy** (on-topic, well-formed) | **0.76 mean** — 17/24 pass |
| Precision (correct ÷ answered) | 0.05 |
| Recall (correct ÷ total) | 0.04 |
| F1 | 0.04 |
| Answered (did not abstain) | 22/24 |

**The one-line truth:** the app produces **relevant, on-topic, well-formed
answers** (relevancy 0.76) that **usually miss the specific fact asked**
(correctness 0.11). It is not producing garbage, and it rarely states the
*opposite* of the truth — its dominant failure mode is **abstaining ("the data
does not specify") or returning adjacent output (a statute breakdown, or raw
aggregate data) instead of the direct answer.**

---

## 2. What we verified by hand (the numbers are real, not a judge artifact)

Before trusting the low scores, we hand-checked the lowest-scoring questions
against the actual database. The failures are genuine:

- **A7** (how many cases have a reporting-delay reason?) — ground truth **8**,
  independently confirmed in the data. The app answered *"the document does not
  specify."* **Genuine miss: the data exists, the app abstained.**
- **D1** (how many FIRs are registered?) — the app routed to the aggregate engine,
  produced a **statute breakdown**, then the Verifier rejected the summary and it
  showed *"the raw computed aggregate instead"* — never stating the simple count
  (79). **Genuine miss caused by a specific bug** (raw-aggregate fallback).
- **A1** (accused gender ratio) — the app got **24 females exactly right** and 92
  vs the true 94 accused (a close, de-duplication-level difference), yet scored
  0.30. Per the team's "close numbers are OK" rule this is **arguably a partial
  pass, understated by the judge** — the one place scoring is still a touch harsh.

So: mostly **real app failures**, with a small amount of residual judge harshness
on close-number cases.

---

## 3. Breakdown

**By question type (Factual Correctness):**
| Type | Mean | Pass |
|---|---|---|
| Fact Retrieval | 0.23 | 1/6 |
| Complex Reasoning | 0.10 | 0/8 |
| Contextual Summarization | 0.06 | 0/5 |
| Creative Generation | 0.04 | 0/5 |

**By language:**
| Language | Mean | Pass |
|---|---|---|
| English | 0.17 | 1/8 |
| Urdu | 0.10 | 0/9 |
| Roman Urdu | 0.06 | 0/7 |

The app does best on simple Fact Retrieval and English; it degrades on
multi-step reasoning, open-ended summarization/creative tasks, and non-English
(though relevancy stays high across languages — it *understands* the questions,
it just doesn't ground the specific answer).

**The one clear pass:** S2 ("busiest police station") — scored 1.0, correctly
answered "Model Town, Lahore, 7 cases."

---

## 4. What was excluded and why (transparency)

**8 Knowledge-Base questions (KB1–KB6, KB8, KB9) were excluded from scoring.**
They all abstained ("no relevant documents found"). We confirmed this is a
**retrieval bug, not the app lacking the answer**: the legal documents (CrPC
1898, Police Order 2002, etc.) ARE loaded and ARE directly retrievable — a direct
vector query returns "Sections 154 and 155, Code of Criminal Procedure" — but the
pipeline's relevance evaluator rejects them and the app abstains. Scoring against
these would unfairly penalize the app for a bug a teammate is fixing separately,
so they are excluded and noted here rather than counted.

The full 32-question raw outputs are preserved in
`gold32_pipeline_outputs_FULL.json` for complete transparency.

---

## 5. Why the numbers are low — and why they can improve substantially with fixes

The low correctness is **not** the app being fundamentally wrong. It traces to a
small number of **specific, fixable behaviours** — the same classes flagged in
the earlier root-cause work, some of which the recent fixes only partially
closed:

1. **Abstaining when the answer exists.** A7, CP6 and others returned "the data
   does not specify" for facts that ARE in the database (e.g. 8 delay-reason
   FIRs). The retrieval/relevance layer is rejecting answerable content.
2. **Raw-aggregate fallback instead of a plain answer (D1).** The aggregate engine
   computes the right data, but when the Verifier can't confirm the natural-language
   summary, it dumps raw aggregate output instead of stating the count. The
   Verifier-relaxation fix did not fully close this path.
3. **Graph output not turned into answers.** Some questions returned raw
   entity-graph output ("connections across 6 cases") instead of a stated answer.
4. **Aggregate coverage gaps on multi-field questions** (district-level, trends,
   comparisons over time) still return "not available."

**These are debuggable.** Each low-scoring question has a captured answer and a
judge reason in `gold32_results.json`, so a developer can see exactly where each
one broke. Because relevancy is already high (0.76 — the app is asking the right
questions of its data), fixing the "state the answer you computed" and "don't
abstain when data exists" behaviours should move a meaningful number of these
from fail → pass **without re-architecting anything.**

**Recommended next step:** hand this report + `gold32_results.json` to the
developer who owns the retrieval/aggregate/verifier layers to debug the specific
failures above. After those fixes, re-running this exact evaluation (the harness
is built and reusable) should show a materially higher Factual Correctness score —
this run establishes the honest baseline to measure that improvement against.

---

## 6. Artifacts
- `Gold_QA_Dataset_Final32_With_Answers.json` — the 32 questions + ground truth
- `gold32_pipeline_outputs.json` (24 non-KB) / `_FULL.json` (all 32) — actual app answers
- `gold32_results.json` — per-question Gemini-judge scores + reasons
- `gold32_results_QWEN_judge.json` — the discarded weaker-judge run (kept for reference)
- `gold32_run.py` / `gold32_score.py` — reusable runner + scorer for the re-run after fixes
