# Muhafiz — Module 26: M1's year-over-year comparison is routed to XGRAPH instead of an aggregate

**Status:** Task brief, ready to implement.
**Branch to create:** `fix/router-year-over-year-comparison-to-xagg`
**Authoritative tracker:** `GOLD_QA_REMAINING_FIXES_PLAN.md` — read its
"Which modules can run in parallel" section first, then **update that file's
status table and this module's section when you finish** (see §6 below;
required part of the module).

**Parallel-safe:** yes. This module owns `src/pipeline/router.py`. Modules
19b, 21 and 25 run concurrently against `prompts/evaluator.txt`,
`xnetwork.py`, and `meta_analysis.py`/`verifier.py`. **Two cautions:**
- If Module 21 concludes CR4 is a *routing* problem (it may — see that
  module's brief), it will need `router.py` too. Coordinate before either of
  you edits it.
- The `xagg.py` modules (22/23/24) are deliberately **not** running in
  parallel with each other; if this module's fix also needs a new aggregate
  in `xagg.py`, sequence behind them rather than editing concurrently.

---

## 0. The situation

**M1** — *"What kinds of cases are we dealing with now compared to a couple
of years back?"*

Gold answer:

> 2024 (13 FIRs) is a single narrow pattern — every case is armed robbery
> (PPC 34, 392, Arms Ordinance §13, each 13/13). 2026 (51 FIRs) is far more
> diversified: the armed-robbery cluster persists (34 ×23, 392 ×8, Arms Ord
> ×16) alongside narcotics (CNSA §9(c) ×12) …

M1 scores **0.0**. Per `GOLD32_RESULTS_FOR_TEAMMATE.md` §4, it is **routed
to XGRAPH**, which refuses to provide the requested year-over-year
comparison.

This is a countable, bucketed comparison — squarely XAGG's job. **Module 13
already built the time-bucket primitive** (partition any aggregate by year
from `incident_date`/`report_date`, diffable across buckets) precisely for
this shape. The capability exists; the question isn't reaching it.

## 1. Objective

M1 routes to an aggregate that produces a real year-over-year case-type
comparison, instead of XGRAPH refusing it.

**First, verify the premise yourself.** The routing claim above comes from
the Module 18 reports, not from first-hand evidence in this brief. Run M1
live and read the raw SSE `route=` event before writing any pattern. If it
turns out M1 reaches XAGG and the aggregate is what's failing, this module's
scope changes — say so and update the plan rather than forcing a router fix
that isn't the problem. (Note: Module 24 separately handles a statute ×
court-stage join; if M1 needs a *new* aggregate rather than routing to an
existing one, that overlaps `xagg.py` — see the cautions above.)

## 2. The trap this module exists to avoid

Three separate false-positive collisions have already been found in this
codebase's pattern lists, and **all three were introduced by someone adding
a reasonable-looking pattern without negative-controlling it against the
other gold questions**:

1. **Module 8c's KB patterns** — mined from KB1's phrasing, matched **0 of
   the other 7** KB questions (fixed in PR #9).
2. **Module 15's CR8 pattern** — `report|converted|forwarded` + a bare
   `f.i.r.` mention also matched **KB1** ("how a **report** of a crime
   becomes a formal **FIR**"), hijacking it to XAGG's statute count (fixed
   in PR #7).
3. **Module 16's G3 keyword** — the bare Urdu word `عدالت` ("court") also
   matched **M4** ("how far cases have progressed **in court**"), hijacking
   it to the court-readiness scan (fixed in PR #8).

So: **a pattern that looks reasonable in isolation is not evidence that it
matches the question you want or misses the ones you don't.** Both
directions must be measured.

## 3. Work

Add a year-over-year / period-comparison override in
`src/pipeline/router.py`, in the same additive style as Modules 3/4/15
(`_XAGG_OVERRIDE_PATTERNS`), routing to Module 13's existing time-bucket
aggregate.

**Mine the pattern from M1's literal gold text**, then widen only as far as
genuine paraphrases require. English / Urdu / Roman-Urdu, consistent with
every other override family in that file. Candidate shapes to consider (M1's
own phrasing is deliberately colloquial — no "year over year" string
appears in it):

- *"now compared to a couple of years back"*, *"compared to last year"*,
  *"versus two years ago"*, *"how has X changed since Y"*
- Urdu/Roman equivalents (*"دو سال پہلے کے مقابلے میں"*, *"do saal pehle ke
  muqablay mein"*)

Note the router's existing `_ACTIVE_CASE_RE` guard: a query naming a
specific case (`CASE-xxx` / `FIR-xxx`) is deliberately excluded from XAGG
overrides — preserve that.

## 4. Verification — both halves required

**Unit:**
- `tests/test_router.py` full pass (100 tests currently).
- **A negative-control test over all 32 gold questions is mandatory** —
  assert your new pattern matches M1 and matches **none** of the other 31.
  `tests/test_router.py` already has this shape from PR #7 (the KB1
  negative-control) and `tests/test_xagg.py` from PR #8 (M4/G3) — follow
  those. This single test is what would have caught all three historical
  collisions listed in §2.
- A positive-control test with at least one non-gold paraphrase, so the
  pattern isn't pinned to one literal string.

**Live:**
1. Postgres healthy (`docker compose up -d postgres` — a dead Postgres reads
   as a *hang*, not an error), model server up, backend logged to a file.
2. M1's exact gold text through `/api/chat` as `admin@example.com` /
   `MuhafizAdmin2026!`, All Cases.
3. Confirm from the raw SSE that `route=XAGG` (not XGRAPH) and that the
   answer contains a real per-year breakdown — not "additional data
   required" or a flat count.
4. Capture the before/after answer.

**Non-gold paraphrase** (required): e.g. *"Has the mix of crimes we handle
shifted since 2024?"*

**Regression guard:** re-run at least D1 ("how many FIRs"), CP1, and A7 —
the Fact Retrieval bucket currently scores 0.98 (6/6) and must stay there.
Any router change is a regression risk to already-passing questions.

## 5. Git discipline

- Branch off current `main`: `git checkout main && git pull && git checkout
  -b fix/router-year-over-year-comparison-to-xagg main`
- **Author every commit as `rayyanfaisal475207
  <rayyanfaisal475207@users.noreply.github.com>`.**
- Keep the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer.
- End PR descriptions with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Push and open a PR against `main` (protected; "Backend tests" must pass).
- Every number in your writeup traces to a captured live output.

## 6. Required: update the plan file when you finish

Update `GOLD_QA_REMAINING_FIXES_PLAN.md` **in the same PR**:

1. **Status snapshot table** — Module 26's row → `✅ PR #<n> open` / `✅
   Merged`.
2. **Module 26's own section** — replace the work description with what you
   found and did: M1's **actual** observed route before the fix (confirming
   or correcting the reports' XGRAPH claim), the pattern added, and the
   before/after answer.
3. **Record the negative-control result explicitly** — "matches M1, matches
   0 of the other 31 gold questions" with the test that proves it. Given the
   three historical collisions, this is the number a reviewer will look for.
4. **If M1 turns out to need a new aggregate** rather than just routing, say
   so and flag the `xagg.py` overlap with Modules 22/23/24 in the
   parallelization table — that changes what can safely run concurrently.
