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
| 26 | M1 routing miss (XGRAPH instead of aggregate) | `fix/router-year-over-year-comparison-to-xagg` | ⬜ Not started |
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
| 26 | `src/pipeline/router.py` |

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

### Recommended wave plan

- **Wave 1 (now, 4 parallel chats):** 19b, 21, 25, 26 — plus merge PRs #8/#9.
- **Wave 2 (one chat, sequential):** 22 → 23 → 24.
- **Wave 3:** 27, the final rerun, once everything above is merged.

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

# Module 26 — M1: routing miss (XGRAPH instead of an aggregate) ⬜

**Branch:** `fix/router-year-over-year-comparison-to-xagg`
**Question:** M1 — year-over-year case-type comparison.

**Root cause:** routed to XGRAPH, which refuses ("cannot provide the
requested comparison"). This is a countable comparison and belongs in XAGG
(Module 13's time-bucket primitive already exists).

**Work:** a year-over-year comparison override in `src/pipeline/router.py`,
in the same additive style as Modules 3/4/15. **Mine the pattern from M1's
literal gold text**, then negative-control it against every other gold
question — the lesson from Module 8c (0/7) and the CR8→KB1 and M4→G3
collisions (PRs #7, #8): a pattern that looks reasonable in isolation is not
evidence it matches the real question or misses the others.

**Verify:** `tests/test_router.py` full pass **plus a negative-control test
over all 32 gold questions**; live M1 returns the comparison; a non-gold
paraphrase.

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
