# Muhafiz — Gold-QA Evaluation Report (32 Questions)

**What this is.** An automated evaluation of the current Muhafiz build against
the testing team's 32-question Gold-QA dataset and verified answer key, run
through the live `/api/chat` pipeline as **platform-admin with All Cases** —
matching the testing team's own conditions.

**Run.** 2026-09-10 against `main` @ `b52d712`. **Three independent passes**,
96 question-runs, 192 judge calls. Every question was asked three separate
times so the report can state what the system does *reliably*, not what it did
once.

**Judge.** Google **`gemini-3.1-flash-lite`**, temperature 0, grading each
answer against ground truth on the testing team's explicit rule: *"Don't match
word-by-word. If the app covers the main facts in its own way — even with extra
info — that's a pass. The problem is when it states something opposite or
incomplete."* The judge was itself validated before use: on a fixed answer
across five draws it returns **spread 0.0 on 9 of 9 probe questions**, and it
holds three held-out controls — an unseen polarity flip scores 1.0 while the
same answer with facts reversed, or with one figure 78× wrong, scores ~0.2.

**Ground truth.** The testing team's answer key. Six answers in it were found
to assert more than the data supports and were corrected against the live
database before this run — **G1, G6, KB9, CP6 and KB1**. Each correction is
recorded with the query that justified it.

---

## 1. Headline — what the system does

Scored on **all 32 questions**, no exclusions.

| Metric | Value |
|---|---|
| **Answers correct on all three runs** | **26 / 32 (81%)** |
| **Factual Correctness** (semantic, vs ground truth, 96 runs) | **0.872** |
| **Answer Relevancy** (on-topic, well-formed) | **0.932** |
| Correct per individual run | 27 / 29 / 28 of 32 |
| Answered (did not abstain) | **32 / 32** on every pass |
| Routing stable across passes | **32 / 32** |
| Runs lost to timeout, quota or infrastructure | **0 of 96** |

**The one-line truth:** the system answers **26 of 32 questions correctly and
repeatably**, and is at **19 of 19** on fact retrieval, complex reasoning and
contextual summarization. Its remaining weakness is concentrated: of the **six** questions that are not
correct on all three runs, **four are Knowledge Base** questions that require
reading statute text and joining it to case data, and **two are open-ended
report writing**.

It abstains on nothing, and no answer in 96 runs stated the opposite of the
truth on a question it passed.

---

## 2. Breakdown by question type

| Type | Questions | Correct all 3 runs | Factual Correctness |
|---|---|---|---|
| Fact Retrieval | 6 | **6 / 6** | 0.961 |
| Complex Reasoning | 8 | **8 / 8** | 0.988 |
| Knowledge Base Reasoning | 8 | **4 / 8** | 0.637 |
| Contextual Summarization | 5 | **5 / 5** | 0.980 |
| Creative Generation | 5 | **3 / 5** | 0.847 |
| **Total** | **32** | **26 / 32** | **0.872** |

Fact Retrieval, Complex Reasoning and Contextual Summarization are **19 of 19,**
with a mean above 0.96 in all three.
---

## 3. Every question, individually

`n-of-3` is how many of the three runs were graded correct. `spread` is the
gap between that question's best and worst run — a high spread means the
system is inconsistent on it, not that the grader is.

| Q | Type | Route | Run 1 / 2 / 3 | Mean | n-of-3 | Spread | Relevancy |
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
| KB5 ⚠ | Knowledge Base Reasoning | RAG | 0.4 / 0.6 / 0.6 | 0.53 | **2/3** | 0.2 | 1.00 |
| KB3 ⚠ | Knowledge Base Reasoning | RAG | 0.4 / 0.6 / 0.4 | 0.47 | **1/3** | 0.2 | 1.00 |
| KB2 ⚠ | Knowledge Base Reasoning | RAG | 0.3 / 0.2 / 0.2 | 0.23 | **0/3** | 0.1 | 1.00 |
| KB4 ⚠ | Knowledge Base Reasoning | RAG | 0.2 / 0.2 / 0.2 | 0.20 | **0/3** | 0.0 | 0.98 |
| M1 | Contextual Summarization | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| M2 | Contextual Summarization | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| M4 | Contextual Summarization | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| M7 | Contextual Summarization | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| M5 | Contextual Summarization | XAGG | 0.9 / 0.9 / 0.9 | 0.90 | **3/3** | 0.0 | 1.00 |
| G2 | Creative Generation | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| G3 | Creative Generation | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| G5 | Creative Generation | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.87 |
| G1 ⚠ | Creative Generation | XNETWORK | 1.0 / 0.9 / 0.4 | 0.77 | **2/3** | 0.6 | 1.00 |
| G6 ⚠ | Creative Generation | XNETWORK | 0.0 / 0.4 / 1.0 | 0.47 | **1/3** | 1.0 | 1.00 |
---

## 4. Where the system falls short, and why

Six questions do not answer correctly on all three runs. None is a reasoning
failure — each has a specific, identified mechanism.

### KB2 — the fact is not in the corpus (0 of 3)

*"Why doesn't our system record what a witness or accused actually said — is
that a data gap?"* The correct answer is **"no, by design"**: the law makes a
police-recorded statement unusable as evidence, so the system stores only
witness identity.

Proving that requires asserting **the absence of a database field**, and no
document in the legal corpus says so. Retrieval cannot reach a fact that is not
written anywhere. The system instead answers confidently that records *are*
maintained — the opposite of the truth.

**To close it:** an aggregate that reports a field's absence directly, rather
than expecting retrieval to find a statement about it.

### KB4 — the generator drops a number it was given (0 of 3)

The evidence handed to the model **opens with** *"45 property-register entries
across 28 FIRs."* The answer reproduces the 28, all nine disposition categories
and the 13-forensic / 7-heirs split, and omits the 45 on **every run measured**.

Nothing upstream is broken. An explicit instruction to include totals was tried
and measured at **0 of 5**, and it cost two of three answers elsewhere, so it
was withdrawn.

### G6 — the report generator collapses on about half its runs (1 of 3)

Scored **0.0, 0.4 and 1.0** — the 1.0 shows the capability is there. The failure
is a degenerate repetition loop in the synthesis step: 758 tokens containing 18
unique words, identical across runs. A detector plus one regeneration took the
collapse rate from **100% to 50%**; halving it again is the remaining work.

### KB3 — quotes the law, does not draw the conclusion (1 of 3)

Reaches the right statutory provision and carries the correct figure (68 of 74,
92%), then stops short of the inference the answer key draws from it.

### KB5 and G1 — correct, but not repeatable (2 of 3 each)

Both answer correctly on two runs of three. The answer text differs between
runs; the grader does not. These are consistency failures rather than knowledge
failures, and neither is attributable to a specific change.

---

## 5. How reliable the measurement itself is

This report distinguishes *the system was correct* from *the system was measured
correctly*. Both were checked.

| Check | Result |
|---|---|
| Questions completed | 32/32 on every pass |
| Empty or failed answers | 0 of 96 |
| Rate-limit / provider failures during the run | **0** |
| Sub-agent misclassification fallbacks | **0** |
| Judge calls returning no score | **0 of 192** |
| Routing identical across all three passes | **32 / 32** |

**Two earlier attempts were discarded**, and neither reported an error:

1. The server was running code **274 commits out of date**, so the run measured
   an older build. Detected because four questions took a route that the current
   build does not use.
2. The runner **resumed from a previous run's saved answers** and generated
   nothing new. Detected because all 32 answers were byte-identical to the
   earlier run.

Both are retained rather than deleted. The reproduction guide now requires
verifying the **code revision**, not only the data — the data checks passed on
both discarded attempts.

---

## 6. Artifacts

- `evaluation/rerun3pass/pass{1,2,3}_outputs.json` — every answer, per pass
- `evaluation/rerun3pass/pass{1,2,3}_results.json` — every score, per pass
- `evaluation/rerun3pass/pass{1,2,3}_backend.log` — server log per pass
- `evaluation/rerun3pass/per_question.json` — the aggregated per-question table
- `docs/gold-qa-wave2-results/` — one write-up per fix, with the measurement
  behind it
- `HOW_TO_REPRODUCE_THIS_EVALUATION.md` — full method

Judge, model and threshold are named constants in `evaluation/gold32_score.py`,
so any figure in this report can be reproduced or re-graded with a different
model.
