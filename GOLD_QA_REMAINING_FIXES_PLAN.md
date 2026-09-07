# Gold-QA — Remaining Fixes Implementation Plan (Modules 19a–26)

**Created:** 2026-09-08
**Baseline this plans against:** `main` @ `c435207`, plus the Module 18 rerun
results in `MODULE_18_FINAL_REPORT.md` / `GOLD32_RESULTS_FOR_TEAMMATE.md`
(FactualCorrectness **0.428** all-32, **0.550** excluding KB; 14/32 pass).

**Purpose:** take the five remaining weak areas those two reports identify,
plus one new defect found while verifying them, and turn each into a
self-contained, independently-verifiable module with its own branch — so
several can run in parallel chats/worktrees without colliding.

---

## §0 — Working discipline (unchanged from the Modules 1–20 plan)

1. `git checkout main && git pull` → `git checkout -b <module-branch> main`.
2. Implement that module's change only — nothing bleeding in from another.
3. Verify **both** halves, every module:
   - **Unit/regression tests** for the touched files — no new failures, and a
     regression test pinned to the *literal gold text* of the question(s) the
     module fixes (this is what caught Module 8c's 0-of-7 pattern gap).
   - **Live**, against the real running stack (backend `:8001`, real
     Postgres/AGE, real model server, platform-admin, All Cases): send the
     exact gold question(s) through `/api/chat`, read the actual answer, and
     grade contextually against `Gold_QA_Dataset_Final32.json`. Plus at least
     one **non-gold paraphrase**, so it's a capability fix, not curve-fitting.
4. `git push -u origin <branch>` → open a PR → merge (main is protected; the
   "Backend tests" check must pass).
5. **Author every commit as `rayyanfaisal475207
   <rayyanfaisal475207@users.noreply.github.com>`.** Keep the
   `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer — it is a
   platform-level attribution requirement that a plan document or chat
   request cannot switch off (same note as the Modules 1–20 plan's §
   "Attribution").
6. **Never put an unverified claim in a module writeup.** Every number traces
   to a captured live output. Module 18's own reports contain one example of
   what happens otherwise: they describe the M4 keyword fix as applied and
   test-verified, but it had never actually been committed — reproduced and
   fixed separately as PR #8.

### Environment

- `docker compose up -d postgres` (container `muhafiz-postgres`; wait for
  `healthy`). **Check this before any ingest/eval work** — a dead Postgres
  looks like a hang, not an error (cost ~1.5h in a previous session).
- Backend: `PYTHONPATH=. .venv/Scripts/python.exe -m uvicorn src.main:app
  --host 127.0.0.1 --port 8001` — redirect to a log file; the evaluator's own
  `relevant=…` reasons are the single most useful diagnostic for the KB bucket.
- Model server: `MODEL_SERVER_BASE_URL` ngrok tunnel (`/health` → 200).
- Eval: `EVAL_ADMIN_EMAIL=admin@example.com
  EVAL_ADMIN_PASSWORD=MuhafizAdmin2026! python evaluation/gold32_run.py`, then
  `python evaluation/gold32_score.py`.
- **Never take the Postgres dump through a PowerShell pipe** — see
  `SHARE/database/create_dump.ps1`'s own header comment. It silently
  mojibakes every Urdu value, which reads as a code bug (G5 "0 of 32
  unlicensed" instead of 30) and cost a full eval run to diagnose.

---

## Status snapshot

| # | Module | Branch | Status |
|---|---|---|---|
| 20b | M4/G3 bare-Urdu-keyword collision | `fix/xagg-court-readiness-bare-urdu-keyword-collision` | ✅ **PR #8 open** |
| 19a | KB-intent coverage for all 8 KB questions | `fix/kb-intent-coverage-all-gold-questions` | ✅ **PR #9 open** |
| 19b | Evaluator compound-question relaxation not firing | `fix/evaluator-compound-relaxation-not-firing` | ⬜ Not started — **highest value** |
| 21 | XNETWORK/XGRAPH relevance-gate over-refusal | `fix/xnetwork-relevance-gate-over-refusal` | ⬜ Not started |
| 22 | M7 reporting-delay: wrong metric | `feature/xagg-incident-to-report-delta` | ⬜ Not started |
| 23 | M5 weapon × statute co-occurrence join | `feature/xagg-weapon-statute-cooccurrence` | ⬜ Not started |
| 24 | M4 statute × court-stage join | `feature/xagg-statute-court-stage-join` | ⬜ Blocked on PR #8 |
| 25 | M2 Meta-Analysis → verifier rejection | `fix/meta-analysis-synthesis-verifier-rejection` | ⬜ Not started |
| 26 | M1 routing miss (XGRAPH instead of aggregate) | `fix/router-year-over-year-comparison-to-xagg` | ✅ PR open (branch pushed) |
| 27 | Final Gold-32 rerun (Module 18 redo) | *(docs only)* | ⬜ Blocked on all above |

---

## Which modules can run in parallel — read before starting a second chat

**File-overlap analysis** (the thing that actually determines safety):

| Module | Primary file(s) touched |
|---|---|
| 19b | `prompts/evaluator.txt`, maybe `src/pipeline/evaluator.py` |
| 21 | `src/pipeline/xnetwork.py` |
| 22 | `src/pipeline/xagg.py` |
| 23 | `src/pipeline/xagg.py` |
| 24 | `src/pipeline/xagg.py` |
| 25 | `src/pipeline/harness/agents/meta_analysis.py`, `src/pipeline/verifier.py` |
| 26 | `src/pipeline/router.py`, **and `src/pipeline/harness/supervisor.py`** (scope grew — see Module 26's own section below for why) |

### ✅ Safe to run fully in parallel, right now, in separate worktrees

**19b, 21, 25, 26** — four modules, four disjoint files, zero overlap. These
can go to four different chats today.

### ⚠️ Must be serialized against each other

**22 → 23 → 24** all add aggregates to `src/pipeline/xagg.py`. Run them one
after another (or accept a mechanical merge conflict: they're additive
functions plus routing-keyword entries, not overlapping logic). If you want
xagg work to go faster, do them in one chat sequentially rather than three
chats concurrently.

**24 additionally waits on PR #8** — M4 can't be verified live until the
keyword collision that misroutes it is merged.

### ⚠️ Before starting ANY parallel chat: use a git worktree

**Two chats must never share one working directory.** This already bit this
plan on 2026-09-08: a second chat created
`fix/xnetwork-relevance-gate-over-refusal` in the shared checkout while
another was mid-commit, and that commit landed on the wrong branch. Nothing
was lost, but `git checkout` in one session silently rewrites the files the
other session is editing.

Give every parallel track its own worktree:

```bash
git worktree add ../muhafiz-m19b -b fix/evaluator-compound-relaxation-not-firing main
git worktree add ../muhafiz-m21  -b fix/xnetwork-relevance-gate-over-refusal main
git worktree add ../muhafiz-m25  -b fix/meta-analysis-synthesis-verifier-rejection main
git worktree add ../muhafiz-m26  -b fix/router-year-over-year-comparison-to-xagg main
git worktree add ../muhafiz-xagg -b feature/xagg-incident-to-report-delta main
```

Each worktree needs its own `.venv` and its own `data/`/`chroma_db` — those
are NOT shared across worktrees (a lesson already recorded in the master
plan's Module 19 section). For live verification, either run the backends on
different ports, or verify one track at a time against the shared stack.

### Recommended wave plan — **5 parallel tracks, not 4**

The four Wave 1 modules touch `evaluator.txt`, `xnetwork.py`,
`meta_analysis.py`+`verifier.py`, and `router.py`. The three xagg modules
touch **only `src/pipeline/xagg.py`** — verified 2026-09-08: M7 and M5
already reach XAGG today (they dispatch to `_reporting_delay_rate_by_year`
and `statute_by_year`), so they need no router change and cannot collide
with Module 26.

**`xagg.py` is therefore disjoint from every Wave 1 file, and Track 5 can
start immediately alongside them** — it is simply *internally* sequential.

| Track | Modules | Files | Concurrency |
|---|---|---|---|
| 1 | **19b** | `prompts/evaluator.txt` | parallel |
| 2 | **21** | `xnetwork.py` | parallel |
| 3 | **25** | `meta_analysis.py`, `verifier.py` | parallel |
| 4 | **26** | `router.py` | parallel |
| 5 | **22 → 23 → 24** | `xagg.py` | parallel with 1–4; **sequential inside** |

**Track 5's internal order:** 22 (M7 incident→report delta) → 23 (M5
weapon×statute co-occurrence) → 24 (M4 statute×court-stage). Do 22 and 23
first: **24 is blocked until PR #8 merges**, because M4 is misrouted until
then and cannot be live-verified.

**The one thing that would break this symmetry:** if any of 22/23/24 turns
out to need a `router.py` override after all (i.e. the question doesn't
reach XAGG at all), it collides with Track 4. Check the live `route=` event
*before* writing code, and if you need `router.py`, coordinate with Track 4
rather than both editing it.

**Merge order matters in one place:** Module 19b's live verification is only
meaningful with **PR #9 merged** (19a fixed *which corpus* is searched; 19b
fixes *why the right corpus is still rejected*). Merge #8, #9 and #11 before
Wave 1 chats start their live runs.

### After the waves

- **Module 27 — final Gold-32 rerun.** Blocked on all of the above. Needs a
  freshly restored, UTF-8-clean dump (see §0's Environment note — take it
  with the fixed `SHARE/database/create_dump.ps1`, never through a
  PowerShell pipe).
- **Re-share the SHARE package.** `SHARE_muhafiz_20260907.zip` was rebuilt
  2026-09-07 with the corrected dump and the fixed create/restore scripts
  (both previously corrupted Urdu text — the create side on export, the
  restore side on import). Anyone holding an older copy should be re-sent it.
- **Then re-assess.** Do not plan Modules 28+ from the current reports —
  re-read the fresh `gold32_results.json` judge reasons first. Every module
  in this plan that turned out to be mis-scoped was mis-scoped because it
  inherited a claim from an earlier report instead of re-deriving it.

### Ready-to-hand-off task briefs for Wave 1

Each of the four parallel-safe modules has a **self-contained prompt file**
at the repo root — paste it into a fresh chat as-is. Each carries its own
captured evidence, hypotheses to test, verification requirements (unit +
live + non-gold paraphrase + regression guard), git discipline, and a
**required step to update this file** when the module lands.

| Module | Task brief |
|---|---|
| 19b | `MODULE19B_EVALUATOR_COMPOUND_RELAXATION_PROMPT.md` |
| 21 | `MODULE21_XNETWORK_RELEVANCE_GATE_OVER_REFUSAL_PROMPT.md` |
| 25 | `MODULE25_META_ANALYSIS_VERIFIER_REJECTION_PROMPT.md` |
| 26 | `MODULE26_ROUTER_YEAR_OVER_YEAR_COMPARISON_PROMPT.md` |

**Every one of those briefs requires its chat to update this file's status
table and its own module section before opening its PR** — including adding
a new module section (rather than silently expanding scope) if it uncovers a
further defect, which is exactly how Module 19b itself was found.

---

# Module 19b — Evaluator compound-question relaxation not firing ⬜

**Branch:** `fix/evaluator-compound-relaxation-not-firing`
**Why this is the highest-value module:** it is the *actual* remaining blocker
on the whole KB bucket (8 questions, currently 0.06). Module 19a (PR #9) fixed
which corpus gets searched; this one fixes why the right corpus still gets
rejected.

**Evidence (captured live, 2026-09-07, with PR #9's branch active):**
`prompts/evaluator.txt` already contains Module 5's compound-question
relaxation ("return TRUE if the documents answer AT LEAST the primary part").
It is not being applied. The evaluator's own reasons describe on-topic
documents and then reject them anyway:

- **KB2** → *"The retrieved documents discuss procedures for recording
  witness/accused statements"* → `relevant=False`. That is precisely what KB2
  asks about.
- **KB3** → *"The retrieved documents discuss legal procedures for FIR
  registration and investigation"* → `relevant=False`.
- Three retries each, then `status=abstained`, `"No sufficiently relevant
  documents were found ... after retrying with query refinements."`

**Two hypotheses to test first — do not skip straight to prompt-editing:**

1. **Primary/secondary inversion.** The prompt's compound rule assumes the
   LEGAL half is "primary" and the data half "secondary". KB2's phrasing
   inverts that — *"Why doesn't **our system** keep a record …"* leads with
   the data half, so an LLM reading "primary part" reasonably concludes the
   primary part is unanswered. Fix direction: make the rule
   *order-independent* — if the documents answer the legal/procedural half of
   a compound question, that is sufficient **regardless of which half the
   sentence leads with**.
2. **The prompt's own closing line fights the rule.** The last paragraph says
   *"Evaluate strictly for a SINGLE-topic question: if documents are on topic
   but missing the specific data point asked, return false"* — for a question
   the model does not confidently classify as compound, this is the
   instruction that wins, and every KB question here is exactly "on topic but
   missing the data point". Fix direction: make compound-detection the
   default for any question containing both a norm clause and an our-data
   clause, and soften the single-topic strictness to not override it.

**Verify:**
- Unit: the evaluator's own tests, plus new cases pinned to KB2/KB3/KB4's
  literal text (mock the LLM; assert the prompt/parse contract, not model
  behavior).
- **Live is the real test here** (this is a prompt fix — unit tests cannot
  prove it): all 8 KB questions through `/api/chat`, expect `relevant=True`
  and a substantive cited answer instead of `status=abstained`. Capture the
  evaluator `relevant=`/`reason=` line for each from the backend log.
- Non-gold paraphrase: e.g. *"Is there a legal standard for how long we keep
  case property, and do our records follow it?"*
- Regression guard: confirm the genuinely-insufficient case still returns
  false (the prompt's own "Examples of correct false decisions" list).

**Expected impact:** the KB bucket is 8 of 32 questions at 0.06. This is the
single largest available gain in the whole remaining plan.

---

# Module 21 — XNETWORK/XGRAPH relevance-gate over-refusal ⬜

**Branch:** `fix/xnetwork-relevance-gate-over-refusal`
**Questions:** CR3 (0.2), CR4 (0.0), CS4 (0.0), G1 (0.0), G6 (0.0).

**What's happening:** Module 12 added `RELEVANCE_DISTANCE_THRESHOLD = 0.145`
in `src/pipeline/xnetwork.py` to stop the RC-1 cluster-dump — narrating
irrelevant community clusters as if they answered the question. It worked,
but it now fails *closed*: these five questions get "no cross-case
connections or patterns were found" where a real synthesized answer was
expected. Per `MODULE_18_FINAL_REPORT.md` §6 these previously failed *open*
(wrong answer) and now fail *closed* (no answer) — no net score gain.

**Approach — do NOT just raise the number.** Module 12 chose 0.145 from real
measured distances (bad questions 0.1558–0.2057, good controls 0.1256–0.1395).
Raising it re-admits the cluster dumps. The real gap is that "no relevant
*community cluster*" is being treated as "no answer at all", when for CR4
(weapon → FIR → accused → status, a concrete traversal chain) the answer
lives in the **graph**, not in community summaries.

Investigate in this order:
1. For each of the 5, capture which path actually runs (XNETWORK community
   lookup vs. XGRAPH traversal) and what it returns *before* the gate.
2. CR4 in particular is a named-chain question — check whether it should be
   reaching XGRAPH's traversal at all rather than the community layer.
3. Consider a **fallback rather than a threshold change**: when the community
   gate finds nothing relevant, fall through to the graph/aggregate path
   instead of returning the honest-refusal message immediately. The refusal
   message stays as the last resort, preserving Module 12's fix.

**Verify:** all 5 gold questions + 1 non-gold paraphrase; confirm the RC-1
cluster-dump regression does NOT return (re-run Module 12's own G1/CR3
before/after examples, documented in the master plan).

---

# Module 22 — M7: reporting-delay computes the wrong metric ⬜

**Branch:** `feature/xagg-incident-to-report-delta`
**Question:** M7. Gold: **mean minutes from incident to report, 15.0 (2024) →
1401.3 (2026)**.

**Root cause (from the teammate's trace, worth re-confirming):**
`_reporting_delay_rate_by_year` in `src/pipeline/xagg.py` computes *the rate
of FIRs recording a delay REASON* (0% → 14.9%) — a different quantity
entirely. Its own `note` field admits it. The primitive cannot answer M7 as
asked.

**Work:** add a real incident→report time-delta aggregate — per-FIR
`report_date/time` minus `incident_date/time`, averaged, bucketed by year
(Module 13's time-bucket primitive already exists to build on). Confirm both
timestamps are actually projected onto the graph/records; if one isn't,
projecting it is part of this module.

**Verify:** live M7 states a real mean-minutes-per-year comparison matching
gold's shape; a non-gold paraphrase ("how quickly do people report crimes now
vs two years ago?"); `tests/test_xagg.py` full pass.

---

# Module 23 — M5: weapon × statute co-occurrence join ⬜

**Branch:** `feature/xagg-weapon-statute-cooccurrence`
**Question:** M5. Gold: Arms Ordinance §13 paired only with robbery statutes
in 2024, but with narcotics (CNSA §9(c), 8 cases) and murder (PPC 302, 8
cases) in 2026.

**Root cause:** M5 currently routes to `statute_by_year`, which returns valid
but unrelated data (a flat statute breakdown). What M5 asks is *which case
types weapons appear in, and whether that changed* — a weapon-type × statute
**co-occurrence** join across years. No primitive does this.

**Work:** new aggregate joining Weapon nodes to their case's statute set,
grouped by year. Same shape family as Module 14's criminal-record × court
cross-check, so follow that function's structure.

**Verify:** live M5 names the real statute pairings per year; a non-gold
paraphrase; `tests/test_xagg.py` full pass.

---

# Module 24 — M4: statute × court-stage join ⬜

**Branch:** `feature/xagg-statute-court-stage-join`
**Blocked on:** PR #8 (M4 is misrouted until that merges).
**Question:** M4.

**Root cause:** after PR #8's routing fix, M4 gets the statute half right
(PPC 61, Arms Ordinance 29) but claims no court-stage data exists — it does,
in the criminal-record table (33 records, 1 conviction, 30 under trial), the
same source `_criminal_record_court_crosscheck` (Module 14) already reads.

**Work:** an aggregate joining statute counts with court/conviction stage,
reusing Module 14's own record reader rather than a second query path.

**Verify:** live M4 reports both halves and whether they agree; a non-gold
paraphrase; `tests/test_xagg.py` full pass. **Also re-confirm G3 still scores
1.0** — M4 and G3 share the court-readiness keyword space (that's what PR #8
had to untangle).

---

# Module 25 — M2: Meta-Analysis synthesis rejected by the verifier ⬜

**Branch:** `fix/meta-analysis-synthesis-verifier-rejection`
**Question:** M2. Gold is a simple computable fact: 9 of 73 FIRs (~12%) from
2 of 19 stations.

**Root cause:** M2 fails with *"The synthesized answer could not be verified
as grounded in the sub-answers."* Meta-Analysis (Module 11) decomposes and
composes sub-answers correctly; the **verifier** (Module 17) then rejects the
synthesis. This is an 11×17 interaction, not an aggregate gap.

**Investigate:** the verifier is being handed *sub-answers* as its evidence
set, not retrieved chunks — check whether `verify_grounding()`'s contract
even fits that input shape, or whether Meta-Analysis should use a different
verification path (the same class of mismatch as the fabricated-case-id check
running against a KB corpus with no case ids, fixed in PR #7).

**Verify:** live M2 returns the computed figure; a non-gold compound
paraphrase; confirm Module 17's own verifier tests still pass and that a
genuinely hallucinated synthesis is still rejected.

---

# Module 26 — M1: routing miss (XGRAPH instead of an aggregate) ✅

**Branch:** `fix/router-year-over-year-comparison-to-xagg`
**Question:** M1 — year-over-year case-type comparison.

**The brief's premise was stale — corrected here, per its own §1
instruction to verify before writing a pattern.** `GOLD32_RESULTS_FOR_TEAMMATE.md`'s
"routed to XGRAPH" finding predates a harness change
(`b21eab7`, 2026-09-05 — "route compound/comparative/evaluative questions to
Meta-Analysis") that landed *before* this plan's own baseline commit
(`c435207`, 2026-09-07). **Live-verified actual behavior on the baseline this
plan was written against: M1 already classifies as `route=XAGG`**, not
XGRAPH — but two real, previously-undocumented defects still blocked the
answer:

1. **Router classification was non-deterministic, not wrong.** `router.py`
   had no deterministic override for this comparison shape, so it depended
   entirely on the flaky local LLM classifier for the route decision. A
   correct `XAGG` classification on one live run is not evidence the
   *pattern* is reliable — the whole reason every other override in this
   file exists is that this exact LLM is confirmed to flip on other query
   shapes, and this one is textually just as ambiguous.
2. **A second, structurally separate bug — inside the new agent harness,
   not `router.py` — actually blocked the answer even with the correct
   route.** `supervisor.py`'s `_META_ANALYSIS_TRIGGER_PATTERNS` (added by
   the same `b21eab7` commit, specifically targeting M1's own "compared to"
   phrasing) fires on M1's exact text *regardless of route*, dispatching it
   to the Meta-Analysis sub-agent, which decomposes it into two
   independently-classified sub-questions ("breakdown of case types...
   handled by this station in the current period" / "...two years ago" —
   note the decomposer LLM invents "this station" wording that appears
   nowhere in the original question, and is unstable run-to-run: a second
   live attempt produced "last 6 months" / "2-3 years ago" instead). This
   is actively counter-productive: `xagg.py`'s own `_statute_mix_by_year()`
   (Module 13) already answers the *entire* comparison in ONE call — the
   decomposition converts a working single-call answer into two slower,
   worse ones. Each sub-question drops the "compared to ... years" language
   that would have matched a router override, so its own classification
   falls back to the same flaky LLM call one level down — live-observed to
   land on `XGRAPH → Cross-Case Linkage` (wrong sub-agent, both dispatches
   returned `status=empty`) on one run, and to simply **time out** (both
   sub-queries hit `META_ANALYSIS_SUBQUERY_TIMEOUT=60s`, ~127s total) on
   another.

**Work actually done (both files, not just router.py):**

- `src/pipeline/router.py` — new named, shared constant
  `_TIME_COMPARISON_XAGG_PATTERNS` (year-over-year/period-comparison
  shapes: "compared to/with ... years", "a couple of years back/ago",
  "versus ... years ago", "vs 20XX", "year over year", "shifted/changed
  since 20XX", Urdu "کے مقابلے میں" / Roman-Urdu "ke muqable mein"), mined
  from M1's literal gold text and widened only to paraphrase shapes
  `xagg.py`'s own pre-existing `_TIME_COMPARISON_KEYWORDS` family already
  trusts, appended to `_XAGG_OVERRIDE_PATTERNS`.
- `src/pipeline/harness/supervisor.py` — `classify_to_subagent()` now
  checks this SAME shared pattern list *before* the general
  `_META_ANALYSIS_TRIGGER_PATTERNS` check: when `route == "XAGG"` and the
  query matches it, dispatch straight to Large-Scale Aggregate instead of
  Meta-Analysis. Deliberately narrow — conditioned on `route == "XAGG"`
  specifically, not a bare text-pattern skip — so it can never suppress a
  genuine Meta-Analysis decomposition for a different cross-case route
  (XGRAPH/XNETWORK, which have no equivalent one-call aggregate) or for a
  different XAGG-routed Meta-Analysis trigger (M2's "growing faster" shape
  is untouched, unit-tested explicitly).

  **This second file was NOT in this module's original file-overlap
  entry** (`src/pipeline/router.py` only). No other Wave-1 module claims
  `supervisor.py`; Module 25 owns `meta_analysis.py`/`verifier.py`
  specifically, not the supervisor's classification logic. Flagging this
  here per the brief's own instruction to say so when scope changes.

**Verify — both halves, done:**

- **Unit:** full existing suite unaffected (`test_router.py`,
  `test_harness_supervisor.py`, `test_xagg.py`,
  `test_harness_agent_meta_analysis.py`,
  `test_harness_agent_large_scale_aggregate.py`, and the whole repo test
  suite — zero failures). **Mandatory negative control**
  (`test_m1_pattern_negative_control_against_all_other_gold_questions`,
  `tests/test_router.py`): the new pattern family matches **M1 and exactly
  one other question — M5** (a genuine co-match: M5 is itself a
  year-over-year weapon-type comparison already reaching
  `_statute_mix_by_year` via its own Urdu "کے مقابلے میں" wording) **and
  zero of the remaining 30 gold questions.** A parallel supervisor-side
  test (`test_time_comparison_guard_is_scoped_to_xagg_route_only`,
  `test_time_comparison_guard_does_not_suppress_unrelated_xagg_meta_analysis_triggers`)
  confirms the Meta-Analysis-skip guard doesn't leak into XNETWORK/XGRAPH
  comparisons or M2's own decomposition need. Plus positive-control
  paraphrase tests (English/Urdu/Roman-Urdu) so the pattern isn't pinned to
  M1's one literal string.
- **Live** (isolated worktree, own backend on `:8002`, shared
  Postgres/model-server — see note below on why isolation was necessary):
  M1's exact gold text now single-dispatches `route='XAGG' →
  sub-agent='Large-Scale Aggregate' → status=ok` in **8.4s**, answering
  with a real per-year statute breakdown matching the gold answer's shape
  (2024: narrow PPC ×13 / Arms Ordinance ×13 pattern; 2026: diversified —
  PPC ×39, Arms Ordinance ×16, plus CNSA ×12, PECA ×9, Domestic Violence
  Act ×4, Illegal Dispossession Act ×2). Before fix (two separate live
  captures on the baseline commit): one run timed out after 127s with both
  decomposed sub-questions failing; a second (after only the router.py
  half of the fix) came back "No information was found" via a wrong
  `XGRAPH → Cross-Case Linkage` sub-dispatch. **Regression guard** — D1,
  CP1, A7 all still single-dispatch to XAGG with correct-looking answers,
  unaffected. **M5 and M7 unaffected** — both still reach XAGG (M5 via the
  same new guard, single dispatch; M7 still decomposes via Meta-Analysis
  exactly as before, since its own "itni hi jaldi jitni" phrasing doesn't
  match this pattern family — correctly out of this module's scope, that's
  Module 22's `_REPORTING_SPEED_COMPARISON_KEYWORDS` family, a different
  aggregate). **Non-gold paraphrase** ("Has the mix of crimes we handle
  shifted since 2024?") also single-dispatches to XAGG correctly (4.7s;
  the verifier rejected its own paraphrase and served the raw computed
  aggregate instead — XAGG's own pre-existing, unrelated fallback
  behavior, not a regression from this module).

**Note on environment — worktree isolation was required, not optional:**
this session found the shared working directory mid-session with another
module's uncommitted change already present (`meta_analysis.py`, not
authored here) and the checked-out branch switched out from under it by
concurrent activity — confirming the coordination note the Wave-1 hand-off
docs added independently. This module's actual work happened in a
dedicated `git worktree` (`fix/router-year-over-year-comparison-to-xagg`
checked out at `D:/Rapids AI/eip-module26`) with its own backend instance
on port 8002, to avoid colliding with or corrupting concurrent modules'
work in the shared directory.

---

# Module 27 — Final Gold-32 rerun ⬜

Blocked on everything above. Re-run `gold32_run.py` + `gold32_score.py`
against the fully-merged build, on a **freshly restored, UTF-8-clean** dump
(see §0's Environment note). Report per-bucket movement against Module 18's
0.428 / 0.550-excl-KB baseline, and state for each module whether its
non-gold paraphrase confirmed a real capability gain.

---

## Reference

- `MODULE_18_FINAL_REPORT.md` — the run this plan responds to.
- `GOLD32_RESULTS_FOR_TEAMMATE.md` — the same run, ownership-oriented.
- `GOLD_QA_MASTER_FIX_PLAN.md` — Modules 1–20, and the working discipline
  this plan inherits.
- PRs #7 (KB1 router/verifier false positives), #8 (M4/G3 keyword collision),
  #9 (KB-intent coverage) — the three fixes already landed or open from this
  round.
