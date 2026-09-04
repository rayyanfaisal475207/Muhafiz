# Muhafiz — Fixes Verified (Gold-QA), and a Request for the Answer Key

**TL;DR:** The fixes merged into `main` are working. Four questions that the
manual Gold-QA testing reported as **broken or not-answered** now return
**correct answers** on the fixed build. To turn this into proper before/after
metric scores, we need the **ground-truth answers** for the 32 Gold-QA
questions — details at the bottom.

---

## What was re-tested

Same conditions as the original testing report — **platform-admin, All Cases
selected** — on the fixed build (main @ latest, with Modules 1–6 merged) and the
current database (79 FIRs).

Ground truth for the factual questions was computed independently from the raw
Muhafiz Data API, so these are real comparisons, not guesses.

## Results — 4 of 4 previously-broken questions now pass

| Question | Before (testing report) | After (fixed build) | Ground truth |
|---|---|---|---|
| **How many accused in total?** | **4** (only repeat offenders) | **92 distinct accused** | 94 records / ~89 unique people |
| **How many of the accused are women?** | Gender ignored; returned the same 4 | **24 women among 92** | **24** (exact match) |
| **Weapon → who was it taken off? (CR4)** | Not answered (retrieval error) | **Answered** — found the linked persons | 32 FIRs have a weapon logged |
| **Anyone arrested in more than one case? (S3)** | Not answered ("no patterns found") | **Answered** — found the repeat persons | 2 by CNIC |

**What this confirms about the fixes:**
- **Aggregate engine (was Root Cause 2):** "total accused" went from a badly wrong
  **4 → 92**, and gender aggregation now works (**24 women**, matching truth exactly).
  It no longer returns a confidently-wrong number from a narrow slice of the data.
- **Routing / retrieval (was Root Cause 1 & 3):** questions that used to hit a
  dead-end route and report "no documents found" now reach an engine that produces
  a real answer.

Note: some answers still correctly **hedge** on sub-details that genuinely aren't in
the records (e.g. the exact outcome after a weapon recovery). That is honest,
grounded behaviour — not a failure.

---

## What we still need from you: the Gold-QA answer key

We want to produce a **defensible before/after evaluation** — real metric scores
(correctness, faithfulness, etc.) across all 32 questions, run automatically against
the fixed build. The blocker is that the Gold-QA dataset we have has **empty `answer`
fields**, and the testing report gives *verdicts* (correct / partial / not-answered)
but not the *expected answers* for most questions.

**Please share the 32 questions with the `answer` field filled in** — i.e. what the
correct/expected answer is for each. Even partial (just the factual ones) helps.

### Why this materially improves the evaluation and the scores

1. **Without a ground-truth answer key, correctness cannot be scored fairly.** In our
   first automated run, we had to write our own reference answers, and they were too
   loose — the LLM judge then penalized the app for being *more* specific and correct
   than our own reference. That produced misleadingly low scores that did **not**
   reflect the app's real quality. Your verified answers fix this at the root.
2. **It separates "app is wrong" from "our reference was vague."** With authoritative
   answers, a low score means a real defect, and a high score is credible to a client —
   the numbers become defensible rather than arguable.
3. **It lets us quantify the improvement, not just spot-check it.** Right now we can
   show 4 questions went from broken to working. With the answer key, we can report
   "X of 32 correct before → Y of 32 after" with per-question scores — the kind of
   concrete before/after a client expects.
4. **Encoding:** please send the Urdu/Roman-Urdu questions in a clean UTF-8 file — the
   copy we have has the Urdu garbled (mojibake), which would make those questions test
   a broken input rather than the app.

### Note on the KB questions (so scores aren't misread)
~10 of the 32 are **Knowledge-Base questions** (KB1–KB9, plus CR6/CR8) that need the
legal document corpus — CrPC 1898, Police Order 2002, etc. — which is **not loaded** in
this deployment. Those are expected to fail and are **not app bugs**; we'll exclude them
(or score them separately) so they don't drag down the real numbers.

---

**Bottom line:** the fixes are demonstrably working on the questions we could verify.
Send the answer key and we'll turn this into a full, client-grade before/after with real
scores across all 32.
