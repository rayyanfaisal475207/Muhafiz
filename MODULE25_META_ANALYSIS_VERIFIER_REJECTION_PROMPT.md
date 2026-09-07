# Muhafiz — Module 25: the verifier rejects Meta-Analysis's synthesis (M2)

**Status:** Task brief, ready to implement.
**Branch to create:** `fix/meta-analysis-synthesis-verifier-rejection`
**Authoritative tracker:** `GOLD_QA_REMAINING_FIXES_PLAN.md` — read its
"Which modules can run in parallel" section first, then **update that file's
status table and this module's section when you finish** (see §6 below;
required part of the module).

**Parallel-safe:** yes, with one caution. This module's files are
`src/pipeline/harness/agents/meta_analysis.py` and
`src/pipeline/verifier.py`. Modules 19b, 21 and 26 run concurrently against
`prompts/evaluator.txt`, `xnetwork.py`, and `router.py`. **Caution:** if
Module 21 ends up needing verifier changes it will say so — `verifier.py` is
yours by default, but coordinate if that comes up.

---

## 0. The situation

**M2** — *"Is caseload growing faster at our general-purpose stations, or at
the handful set up for one specific type of crime?"*

Gold answer is a simple, computable fact:

> Specialized cybercrime units already carry real load: **9 of 73 FIRs
> (~12%) from just 2 of 19 stations** — disproportionate given they're
> single-purpose.

M2 scores **0.0**, failing with:

> *"The synthesized answer could not be verified as grounded in the
> sub-answers."*

Per `MODULE_18_FINAL_REPORT.md` §4 and `GOLD32_RESULTS_FOR_TEAMMATE.md` §4,
Meta-Analysis (Module 11) **decomposes and composes the sub-answers
correctly** — it's the **verifier** (Module 17) that then rejects the
synthesis. This is an 11×17 interaction defect, not a missing aggregate.

## 1. The specific code path

`src/pipeline/harness/agents/meta_analysis.py`, around line 515:

```python
pseudo_chunks = [_pseudo_chunk(i, sq, text) for i, (sq, text) in enumerate(entries, start=1)]

verification = await verify_grounding(answer=answer, cited_chunks=pseudo_chunks, case_id="cross_case")
verifier_passed = bool(verification.get("grounded", False)) and not verification.get("off_topic", False)

if not verifier_passed:
    ...
    return SubAgentResult(
        status=SubAgentStatus.ABSTAINED,
        caveats=["The synthesized answer could not be verified as grounded in the sub-answers.", ...],
    )
```

Note what's being fed in:

- `cited_chunks` are **`_pseudo_chunk`s built from sub-answer text** — not
  retrieved evidence chunks. They carry no real `metadata` (no `case_id`, no
  `source`, no `confidence`) beyond whatever `_pseudo_chunk` synthesizes.
- `case_id="cross_case"` is passed as a **literal string**, in the parameter
  that elsewhere means an actual active case id.

`verify_grounding()` was designed for retrieved narrative chunks. Its
deterministic pre-checks (`_check_temporal`, `_check_leakage`,
`_check_hedging`, `_check_fabricated_case_ids`, `_check_no_citation`) all
make assumptions about that shape.

## 2. The precedent you should read first

This is **the same class of bug already fixed in PR #7** (merged). There,
`_check_fabricated_case_ids()` — written for cross-case answers' `[Document
N, CASE-ID]` citation format — was running unconditionally against the
global legal-KB corpus, where no chunk carries a `case_id` at all. Any
citation with a second bracket field (e.g. `[Document 2, section 4(b)]`,
citing a statute subsection) was misread as an invented case id and a
correctly-grounded answer was rejected. Fix: skip the check when the chunk
set has no case ids to check against.

**Read that fix (`src/pipeline/verifier.py`,
`_check_fabricated_case_ids`) before starting** — the shape of the problem
here is likely the same: *a check whose preconditions don't hold for this
input, firing anyway.*

## 3. Investigate in this order

1. **Get the actual rejection reason.** Run M2 live with the backend logged
   to a file and read `verification.get("reason")` — the code already logs
   it at WARNING (`"Meta-Analysis: verifier rejected synthesized answer:
   %s"`), truncated to 150 chars. Widen that truncation temporarily if you
   need the full text. **Do not theorize before you have this string.**
2. **Determine which check fires.** Is it one of the deterministic
   pre-checks, or the LLM judge? Each pre-check logs distinctly (e.g.
   `FABRICATED CITATION: …`). This tells you whether the fix is "skip a
   check whose preconditions don't hold" (like PR #7) or "the judge prompt
   doesn't fit synthesis input".
3. **Ask whether `verify_grounding()` is the right call at all here.**
   Verifying a *synthesis against its own sub-answers* is a different task
   from verifying an *answer against retrieved evidence*. If the contract
   doesn't fit, the honest fix may be a dedicated synthesis-verification
   path rather than bending the general verifier — but say so explicitly
   with evidence, don't just weaken the general one.
4. **Check `case_id="cross_case"`.** Trace what `_check_leakage()` and
   friends do with a literal `"cross_case"` string in the active-case
   parameter. If any check compares it against real chunk case ids, that's a
   guaranteed mismatch.

## 4. Objective

M2 returns its computed figure (the ~12% / 9-of-73 / 2-of-19 shape) instead
of abstaining — **while a genuinely hallucinated synthesis is still
rejected.**

That second half is not optional. Module 17's own live evidence showed the
verifier correctly catching a real hallucination (a cross-chunk answer
misattributing names and case counts). Whatever you change must keep that
working — a verifier that passes everything is worse than one that's too
strict.

## 5. Verification — both halves required

**Unit:** `tests/test_verifier.py` full pass (79 tests currently, including
PR #7's no-case-id-concept case). `tests/test_harness_agent_meta_analysis.py`
full pass. Add a test for the synthesis-verification path specifically:
- a well-grounded synthesis over sub-answers **passes**;
- a synthesis asserting a fact absent from every sub-answer **still fails**.

**Live:**
1. Postgres healthy (`docker compose up -d postgres` — a dead Postgres reads
   as a *hang*, not an error), model server up, backend logged to a file.
2. M2's exact gold text through `/api/chat` as `admin@example.com` /
   `MuhafizAdmin2026!`, All Cases.
3. Confirm from the **raw SSE stream** that it goes through Meta-Analysis
   (`route=… -> sub-agent='Meta-Analysis'`) and returns a real answer.
   Beware the known parsing trap: Meta-Analysis emits a *second* route event
   for its decomposed sub-query, so a naive "last route event wins" parse is
   misleading (documented in the master plan's Module 11 section).
4. Capture the before/after `verification.reason`.

**Non-gold paraphrase** (required): another compound comparative question
that must route through Meta-Analysis — e.g. *"Are our specialist units
handling proportionally more work than the general ones?"*

**Regression guard:** re-run at least one question that Meta-Analysis
currently answers acceptably, and confirm it hasn't regressed.

## 6. Git discipline

- Branch off current `main`: `git checkout main && git pull && git checkout
  -b fix/meta-analysis-synthesis-verifier-rejection main`
- **Author every commit as `rayyanfaisal475207
  <rayyanfaisal475207@users.noreply.github.com>`.**
- Keep the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer.
- End PR descriptions with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Push and open a PR against `main` (protected; "Backend tests" must pass).
- Every number in your writeup traces to a captured live output.

## 7. Required: update the plan file when you finish

Update `GOLD_QA_REMAINING_FIXES_PLAN.md` **in the same PR**:

1. **Status snapshot table** — Module 25's row → `✅ PR #<n> open` / `✅
   Merged`.
2. **Module 25's own section** — replace the investigation steps with the
   finding: the **full verifier rejection reason** you captured, which check
   fired, and whether the fix was a precondition guard (PR #7 pattern), a
   dedicated synthesis-verification path, or something else.
3. **State explicitly** that a hallucinated synthesis is still rejected,
   with the test that proves it.
4. **If the root cause turns out to sit in Meta-Analysis's composition
   rather than the verifier**, correct the plan's own framing of this module
   — it currently asserts the composition is fine and the verifier is at
   fault, based on the Module 18 reports rather than first-hand evidence. If
   that's wrong, say so.
