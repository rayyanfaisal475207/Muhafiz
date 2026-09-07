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
| 20b | M4/G3 bare-Urdu-keyword collision | `fix/xagg-court-readiness-bare-urdu-keyword-collision` | ✅ **Merged (PR #8)** |
| 19a | KB-intent coverage for all 8 KB questions | `fix/kb-intent-coverage-all-gold-questions` | ✅ **Merged (PR #9)** |
| 19b | Evaluator compound-question relaxation not firing | `fix/evaluator-compound-relaxation-not-firing` | ✅ **Merged (PR #15)** — 5/8 KB questions now pass (was 1/8); see below |
| 21 | XNETWORK/XGRAPH relevance-gate over-refusal | `fix/xnetwork-relevance-gate-over-refusal` | ✅ **PR #12 open** — investigated, no fix belongs in this module's files, split into Modules 28/29 |
| 22 | M7 reporting-delay: wrong metric | `feature/xagg-incident-to-report-delta` | ✅ **Merged (PR #14)** — M7 AND its non-gold paraphrase both verified live, matching gold exactly |
| 23 | M5 weapon × statute co-occurrence join | `feature/xagg-weapon-statute-cooccurrence` | ⬜ Not started — brief: `MODULE23_XAGG_WEAPON_STATUTE_COOCCURRENCE_PROMPT.md` |
| 24 | M4 statute × court-stage join | `feature/xagg-statute-court-stage-join` | ⬜ Not started — **unblocked**, PR #8 has merged; brief: `MODULE24_XAGG_STATUTE_COURT_STAGE_PROMPT.md` |
| 25 | M2 Meta-Analysis → verifier rejection | `fix/meta-analysis-synthesis-verifier-rejection` | ✅ **PR #16 open** |
| 26 | M1 routing miss (XGRAPH instead of aggregate) | `fix/router-year-over-year-comparison-to-xagg` | ✅ **Merged (PR #13)** |
| 28 | CR4 routing miss (weapon-recovery chain sent to cross-case entity linkage) | *(not yet branched)* | ⬜ New — split out of Module 21; **unblocked**, Module 26 has merged; brief: `MODULE28_ROUTER_WEAPON_EVIDENCE_CHAIN_PROMPT.md` |
| 29 | Meta-Analysis decomposer doesn't split broad synthesis asks into XAGG-shaped sub-questions (CR3/G1/G6) | *(not yet branched)* | ⬜ New — split out of Module 21; blocked on PR #16 (same file); brief: `MODULE29_META_ANALYSIS_DECOMPOSER_PROMPT.md` |
| 30 | KB3/KB8/KB9 retrieval-completeness gap (correct statutory chunk never enters the candidate pool) | *(not yet branched)* | ⬜ New — split out of Module 19b; brief: `MODULE30_KB_RETRIEVAL_COMPLETENESS_PROMPT.md` |
| 27 | Final Gold-32 rerun (Module 18 redo) | *(docs only)* | ⬜ Blocked on all above — brief: `MODULE27_FINAL_GOLD32_RERUN_PROMPT.md` |

> **Wave 2 hand-off:** `WAVE2_ORCHESTRATION_PROMPT.md` (added in PR #17)
> carries the per-track plan, the infrastructure runbook, and **measured
> machine constraints that supersede the "Which modules can run in parallel"
> section below** — in particular, per-worktree virtualenvs are no longer
> viable on this machine's free disk. Read it before starting a track.

---

## Which modules can run in parallel — read before starting a second chat

**File-overlap analysis** (the thing that actually determines safety):

| Module | Primary file(s) touched |
|---|---|
| 19b | `prompts/evaluator.txt` only — no `evaluator.py` change was needed |
| 30 *(new, split from 19b)* | Likely `src/pipeline/query_expander.py` / `src/pipeline/cross_script_variant.py` / retrieval top-k tuning — **not scoped yet**, disjoint from every other module's files today |
| 21 | `src/pipeline/xnetwork.py` |
| 22 | `src/pipeline/xagg.py` **+ `src/graph/structured_projection.py` + a graph backfill** (scope was larger than this table originally said — see Module 22's section) |
| 23 | `src/pipeline/xagg.py` |
| 24 | `src/pipeline/xagg.py` |
| 25 | `src/pipeline/harness/agents/meta_analysis.py`, `src/pipeline/verifier.py` |
| 26 | `src/pipeline/router.py`, **and `src/pipeline/harness/supervisor.py`** (scope grew — see Module 26's own section below for why) |
| 28 *(new, split from 21)* | `src/pipeline/router.py` — **same file as 26; serialize after it** |
| 29 *(new, split from 21)* | `src/pipeline/harness/agents/meta_analysis.py` — **same file as 25; serialize after it** |

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

# Module 19b — Evaluator compound-question relaxation not firing ✅ done

**Branch:** `fix/evaluator-compound-relaxation-not-firing`
**Why this was the highest-value module:** it was the *actual* remaining
blocker on the whole KB bucket (8 questions, was 0.06). Module 19a (PR #9)
fixed which corpus gets searched; this one fixes why the right corpus still
got rejected.

**Both hypotheses confirmed real, both fixed in `prompts/evaluator.txt`:**

1. **Primary/secondary inversion (confirmed).** The old rule assumed the
   LEGAL half is "primary." KB2 ("Why doesn't **our system** keep a
   record...") leads with the data half, so the model concluded the primary
   part was unanswered. **Fix:** the compound rule no longer talks about
   "primary/secondary" at all — it defines a question as compound whenever
   it contains BOTH a norm clause (law/rule/standard) and an our-data clause
   (our system/records/data), in **either order**, and says explicitly not
   to reason about which one is "primary."
2. **Closing single-topic override (confirmed).** The prompt's last
   paragraph — *"Evaluate strictly for a SINGLE-topic question..."* — read
   last, won by default for any question not confidently classified as
   compound. **Fix:** that paragraph now runs the compound check FIRST and
   states the single-topic default "does NOT apply... and cannot override
   this" once a question is compound.

A third defect surfaced during verification, **not fixed here — see Module
30 below**: for KB3/KB8/KB9 the ideal statutory citation never enters the
retrieval candidate pool at all (confirmed by manually querying the vector
store with better-targeted text — the correct chunk exists in the corpus and
is findable, just not by any of the query/expansion/rewrite variants the
pipeline actually tries). No evaluator-prompt wording fixes a chunk that was
never retrieved; forcing the evaluator to accept what *was* retrieved for
these three would violate the "still correctly reject genuinely insufficient
evidence" regression guard. Scope for 19b stayed prompts/evaluator.txt only,
per the module brief.

**Verified — unit:** `tests/test_pipeline.py`'s full evaluator suite (4
pre-existing + 6 new) plus the rest of the file, 47/47 pass. New tests pin to
KB2/KB3/KB4's literal gold text (mocking the LLM, asserting the prompt/parse
contract) plus a regression-guard test using the prompt's own
foreigner-registration/tenant-registration "false" example.

**Verified — live**, all 8 KB questions through real `/api/chat`
(`admin@example.com`, All Cases, Postgres + model server up), evaluator
`relevant=`/`reason=` lines captured from the backend log:

| Q | Before (Module 18/this module's own pre-fix probe) | After (live, this fix) |
|---|---|---|
| KB1 | `relevant=True` (already passing) | `relevant=True` — "Document 2 explicitly references Section 154..." |
| **KB2** | `relevant=False` — *"documents discuss procedures for recording witness/accused statements"* (rejected despite being on-topic) | **`relevant=True`** — "The documents address the legal requirements for recording police interview stat[ements]..." |
| KB3 | `relevant=False`, 3 retries, abstained | `relevant=False`, exhausted — genuine retrieval gap (Article 18 of Police Order 2002 never retrieved for any query variant tried); **see Module 30** |
| KB4 | `relevant=False` (compound over-rejection: "do not explicitly outline a formal standard...compliance aspect unanswered") | **`relevant=True`** — "documents from Punjab Police Rules (Rule 27.18) directly address t[he norm clause]..." |
| KB5 | `relevant=False` | **`relevant=True`** — "Documents mention legal provisions (e.g., Punjab Domestic Violence Act, 337-A(i)..." |
| KB6 | `relevant=False` | **`relevant=True`** — "Document 1 addresses forensic handling procedures for seized weapons prior to re[cording]..." |
| KB8 | `relevant=False`, abstained | `relevant=False`, exhausted — genuine retrieval gap (the specific CrPC provision on interim court reporting during a prolonged investigation is not surfaced); **see Module 30** |
| KB9 | `relevant=False`, abstained | `relevant=False`, exhausted — genuine retrieval gap (Section 174 inquest text is not consistently in the retrieved chunk set); **see Module 30** |

**Result: 5 of 8 KB questions now pass the evaluator gate and reach a
substantive, cited answer (KB1/2/4/5/6), up from 1 of 8 before (KB1 only).**
The remaining 3 (KB3/KB8/KB9) are a retrieval-completeness defect, not an
evaluator defect — see the new Module 30 below rather than this module's
scope being silently expanded.

**Non-gold paraphrase** tested during development (mocked-LLM level, not
re-run live in this final pass): *"Is there a legal standard for how long we
keep case property, and do our records follow it?"* — same norm/our-data
compound shape as KB1/KB4, exercised by the new unit tests.

**Regression guard:** confirmed live and in unit tests — a genuinely
off-topic retrieval (the prompt's own foreigner-registration /
tenant-registration example) still returns `false`; the fix does not make
the evaluator accept everything.

---

# Module 30 — KB3/KB8/KB9 retrieval-completeness gap ⬜ new, not yet branched

**Found while verifying Module 19b.** Not fixed there — deliberately kept
out of that module's scope (`prompts/evaluator.txt` only), per its brief's
own instruction to split out a newly-discovered defect rather than silently
expand scope.

**What's happening:** for KB3, KB8 and KB9, the specific statutory
provision the gold answer cites never enters the retrieval candidate pool —
not in the top-5 after cross-rerank, not in the wider ~15-30 item semantic +
BM25 pool before that cut, for the original question OR any of the 2
expanded queries OR the cross-script variant OR the evaluator-feedback retry
rewrite. Confirmed by manually querying the live vector store with better-
targeted text (e.g. "Article 18 Police Order investigation staff head of
investigation" for KB3) — **the correct chunk exists in the corpus and is
findable**, it just isn't reached by any query text the actual pipeline
generates from these questions' phrasing.

- **KB3** — needs Police Order 2002 Article 18 (separate investigation
  wing). Retrieved instead: CrPC sections on statements/bonds/imprisonment,
  Police Order administrative forms.
- **KB8** — needs the CrPC provision on interim court reporting during a
  prolonged investigation. Retrieved instead: CrPC ss.170-171 ("case sent to
  magistrate when evidence is sufficient"), a related but distinct
  provision.
- **KB9** — needs the CrPC s.174 inquest / cause-of-death investigation
  text consistently in the retrieved set (it showed up in some attempts, not
  reliably, and the evaluator's citation of "section 174" in an early
  probe run turned out to be a model hallucination — the text wasn't
  actually in the chunks it was judging that time).

**This is a retrieval quality/coverage defect, not an evaluator defect** —
confirmed by feeding the ACTUAL retrieved chunks (not idealized ones) through
both the pre-fix and post-fix evaluator prompt: the post-fix prompt correctly
recognizes when even a compound question's norm clause isn't addressed by
what's on the page, and returns false. No prompt-only fix should make it
return true here without also risking false-positive relevance elsewhere.

**Likely fix directions (not investigated yet):** widen `query_expander.py`
or `cross_script_variant.py`'s prompting to specifically try naming
candidate governing statutes when the question is legal-KB-intent; raise
`TOP_K_RETRIEVAL`/`CROSS_CASE_RETRIEVAL_MULTIPLIER` for the KB-only scope
specifically (cheap but blunt); or add a keyword/BM25-boost path that
searches for statute-name-shaped tokens directly. Whoever picks this up
should re-probe the corpus first (as this investigation did) to confirm
which of these would actually surface the missing chunk before committing to
one.

**Verify:** re-run KB3/KB8/KB9 live after any fix; confirm the evaluator
then sees the right chunk and correctly returns true (no evaluator change
should be needed if 19b's fix is present). Regression-guard against
Module 19b's fix: rerun KB1/2/4/5/6 too, to confirm a retrieval change here
doesn't regress what 19b just fixed.

---

# Module 21 — XNETWORK/XGRAPH relevance-gate over-refusal ✅ investigated

**Branch:** `fix/xnetwork-relevance-gate-over-refusal`
**Questions:** CR3 (0.2), CR4 (0.0), CS4 (0.0), G1 (0.0), G6 (0.0).

**Finding, stated up front:** the gate in `src/pipeline/xnetwork.py` is
**correctly calibrated and correctly refusing** on all 5 of these questions.
`RELEVANCE_DISTANCE_THRESHOLD = 0.145` was NOT touched — re-measuring live
against the current `muhafiz_community_reports` collection (2026-09-08, all
5 via real `/api/chat`) shows the same clean separation Module 12 originally
documented, undisturbed by the KB re-ingest (that ingest added chunks to the
*document* collection `muhafiz_kb`; the community-report collection this
gate reads is built from case/graph clustering, a completely separate
pipeline untouched by it):

| Q | Nearest community distance | vs. cutoff 0.145 |
|---|---|---|
| CR3 | 0.156 | above — correctly gated |
| CR4 | 0.184 | above — correctly gated |
| CS4 | 0.159 | above — correctly gated |
| G1 | 0.202 | above — correctly gated |
| G6 | 0.181 | above — correctly gated |

None of these five questions has a real answer sitting in the community-
report corpus; the gate's job is exactly to say so instead of narrating the
nearest-but-unrelated cluster, and it does. **The actual defects are all
upstream or in a different route entirely** — none of them lives in
`xnetwork.py`, and none is fixable there without either re-admitting RC-1
(narrating an unrelated cluster) or violating `cross_case_linkage.py`'s own
documented two-tool-composition contract (see below). Per this module's own
brief §2/§6.4 ("if it's a routing problem, the fix may not belong in
`xnetwork.py` — say so rather than forcing a fix into the wrong file"), this
module ends as an investigation, split into two new tracked modules (28,
29) plus one item folded into Module 25. **No production code changed.**

**Investigation, question by question (live captures, 2026-09-08):**

- **CR3** — routes to XNETWORK, dispatched to *both* Meta-Analysis and
  Cross-Case Linkage (confirmed the documented "second route event for the
  decomposed sub-query" parsing trap: the SSE shows two
  `route='XNETWORK'` dispatch lines, one for each sub-agent, not two
  distinct routes). Both return `status=empty`. The real gap: gold's answer
  ("64/26 has a matching walk-in complaint via case tag CMS-ISB-2026-0341;
  65/26 has none") is a **per-record field-consistency comparison across two
  named FIRs** — the same shape as Module 15's cross-check work, not a
  network/cluster synthesis or an entity traversal. Neither tool XNETWORK
  nor XGRAPH computes this. **Verdict: missing XAGG-shaped join, not an
  xnetwork.py defect.** Folded into Module 29 below (decomposition gap) —
  a decomposer that split this into "does 64/26 have a matching walk-in
  complaint?" / "does 65/26?" would let each sub-query reach that join, once
  it exists.

- **CR4** — routes to **XGRAPH** (not XNETWORK), dispatched to Cross-Case
  Linkage alone, `status=empty`. `_recover_target_entity()` correctly
  returns `None` (verified reading `cross_case_linkage.py:228-267`): the
  query names no person, only "a weapon" generically, so there is genuinely
  no entity to recover — this is not an extraction bug. With no seed,
  `xgraph_tool` runs its recurring-entity-**across-cases** traversal, which
  is architecturally the wrong shape for what CR4 asks: gold wants ONE
  example weapon→FIR→accused→status chain (a single-case lookup), not a
  cross-case recurrence pattern. Confirmed with a live control probe: **CR2**
  ("has anyone with an earlier case resurfaced as a suspect?") — a genuine
  cross-case recurrence question — instead routes to **XAGG**'s
  `Large-Scale Aggregate` sub-agent and returns real, useful results,
  including `شہزیب عرف شابی: appears in 2 cases — fir-214-26, fir-891-24`
  (the same person CR4's gold answer names, in the same FIR). This confirms
  the data and a working aggregate path both already exist elsewhere in the
  system, and that CR4's real fix is a **router classification change**
  (send weapon-evidence-chain questions to XAGG or a single-case
  GRAPH_HYBRID traversal instead of XGRAPH's cross-case linkage), which is
  `router.py` — Module 26's file, out of this module's scope. **Verdict:
  routing miss, not an xnetwork.py defect.** Split out as Module 28 below.

- **CS4** — routes through Meta-Analysis, which decomposes into an XAGG
  sub-query (answered OK) and two XGRAPH-via-Cross-Case-Linkage sub-queries
  (both correctly `status=partial`/empty, nearest cluster distances 0.159
  and 0.161, both above cutoff). The top-level failure is
  `status=error`: *"The synthesized answer could not be verified as
  grounded in the sub-answers."* — this is **not** a timeout (the module
  brief's guess) and **not** an xnetwork.py relevance-gate failure; it is
  the exact same Meta-Analysis-synthesis/Verifier-rejection signature
  Module 25 already tracks for M2. **Verdict: same root cause as Module
  25, a second instance of it, not a new defect.** No new module — folded
  into Module 25's own verification scope (Module 25 should re-run CS4
  alongside M2 once its fix lands).

- **G1 / G6** — both route to XNETWORK, both dispatched to Meta-Analysis
  *and* Cross-Case Linkage (same dual-dispatch parsing trap as CR3), both
  `status=empty`. Reading `meta_analysis.py`'s decomposer (`_decompose()`,
  lines 278-315): for these two queries it evidently returns
  `decompose: false` (only one dispatch event appears per sub-agent, not
  several), so Meta-Analysis falls back to re-dispatching the *original,
  un-split* query — which lands back on XNETWORK/Cross-Case-Linkage and
  gates the same way. Gold's expected content for both (G1: offender age
  range, relationship-type skew, seized-property counts, incident-time
  distribution; G6: case-mix trend, arrest rate, reporting-delay trend,
  weapon-licensing rate) is entirely **XAGG aggregate/profile data** — every
  one of those facts is a countable primitive, several already implemented
  (age/relationship breakdowns, case-mix-by-year, arrest rate). The real
  fix is the decomposer splitting a broad "flag anything unusual" /
  "orientation note" question into several XAGG-shaped sub-questions, the
  same mechanism that already works for compound questions elsewhere in
  this module (M2, decomposed correctly per Module 25's own notes) — a
  `meta_analysis.py` prompt/logic change. **Verdict: missing
  decomposition, not an xnetwork.py defect**, and `meta_analysis.py` is
  Module 25's file (shared with its verifier work), so this is flagged
  as Module 29 rather than edited here, to avoid exactly the collision this
  plan's parallel-safety table exists to prevent.

**Regression check (mandatory, both re-run live 2026-09-08):** G1 and CR3
(paraphrase-shape questions closest to Module 12's own before/after
examples) both still return the honest refusal text quoted above — neither
recites an unrelated cluster ("This cluster centers on FIR X…" / the
cybercrime-under-PECA-2016 dump never reappears). Also ran the module's
required non-gold paraphrase, *"As the on-duty analyst, look over
everything currently open and tell me what's worth a second look"* — same
honest-refusal shape, nearest cluster 0.243, correctly gated. **The RC-1
fix is intact; nothing regressed, because nothing in `xnetwork.py` changed.**

**Verify:** `tests/test_xnetwork.py` gained a parametrized regression test
(`test_module21_five_over_refusal_questions_still_correctly_gated`) pinning
all 5 questions' literal gold text + their measured live nearest-distance to
the gate's current, correct "still refuses" behavior — protection against a
future well-intentioned "just raise the threshold" change silently
re-admitting RC-1 for these exact questions. Full suite run
(`test_xnetwork.py`, `test_harness_tool_xnetwork.py`,
`test_harness_agent_cross_case_linkage.py`): all pass, no changes needed
beyond the new test.

---

# Module 28 — CR4: weapon-evidence chain routed to cross-case linkage instead of XAGG ⬜

**Branch:** not yet created.
**Split out of:** Module 21 (see its writeup above for the full live
evidence — CR2 control, `_recover_target_entity()` read, distance capture).
**Primary file:** `src/pipeline/router.py` (Module 26's file — **must be
serialized after Module 26**, not run in parallel with it; the
parallelization table below has been updated).

**What's happening:** *"If we've got a weapon logged as evidence, can we
tell who it was taken off and what happened to them?"* has no named entity,
so it routes to XGRAPH's cross-case recurring-entity traversal via
Cross-Case Linkage — the wrong tool for a single example chain (weapon →
FIR → accused → status) rather than a cross-case recurrence pattern. A
control probe of CR2 (a genuine cross-case recurrence question) shows XAGG's
`Large-Scale Aggregate` sub-agent already resolves person-across-cases data
correctly, including the exact person (شہزیب عرف شابی, fir-891-24) CR4's
gold answer names — the data and a working query path both already exist.

**Work:** add a router classification for "weapon logged as evidence →
who/what happened" style questions that sends them to XAGG (an aggregate
that, given a weapon-type filter or "give one example", surfaces a
weapon→FIR→accused→status chain) rather than XGRAPH. Mine the pattern from
CR4's literal gold text and negative-control it against all 32 gold
questions, same discipline as Module 26's own M1 fix — a keyword that also
matches CR2 (a genuinely different question shape) would misroute a working
question.

**Verify:** `tests/test_router.py` full pass plus a negative-control test
over all 32 gold questions; live CR4 returns the real chain; a non-gold
paraphrase (e.g. "For weapons we've seized as evidence, can we trace them
back to whoever they were taken from?"); confirm CR2 is unaffected.

---

# Module 29 — Meta-Analysis decomposer doesn't split broad synthesis into XAGG-shaped sub-questions ⬜

**Branch:** not yet created.
**Split out of:** Module 21 (CR3, G1, G6).
**Primary file:** `src/pipeline/harness/agents/meta_analysis.py` — **shared
with Module 25**, which already owns this file for its verifier-interaction
fix. Per this plan's own caution on Module 25's row, coordinate rather than
both editing it concurrently; doing 25 then 29 sequentially in one chat is
the safer order since 29 depends on understanding whatever the decomposer
looks like after 25's changes.

**What's happening:** CR3, G1 and G6 all route to XNETWORK, and Meta-
Analysis's `_decompose()` evidently returns `decompose: false` for all
three (only one sub-agent dispatch shows up per query in the live SSE trace,
not several) — so Meta-Analysis falls back to re-dispatching the original,
un-split query, which lands right back on the same XNETWORK/Cross-Case-
Linkage path and correctly finds no relevant community cluster. Every one
of these three questions' gold answer is actually built from several
independently-computable XAGG facts:

- **CR3**: two per-FIR field-consistency checks (does 64/26 have a matching
  walk-in complaint? does 65/26?) — Module 15's cross-check territory.
- **G1**: offender age range/average, accused–complainant relationship
  breakdown, seized-property/forensic-lab counts, incident-time-of-day
  distribution.
- **G6**: case-mix-by-year trend, arrest rate, reporting-delay trend,
  weapon-licensing rate.

**Work:** extend the decomposer's prompt/logic so a broad, open-ended
analytical or "orientation note" question triggers `decompose: true` with
sub-queries shaped like the countable facts above, each of which should
independently route to XAGG once decomposed and re-dispatched through
`Supervisor().handle()`. Confirm which of the needed XAGG primitives already
exist (several likely do, per Modules 13-15) vs. need adding — that gap
analysis, not this module, is what determines whether new `xagg.py`
aggregates are also required (if so, track them as Module 22/23/24-style
additions rather than silently expanding this module's scope).

**Verify:** live CR3/G1/G6 return real, gold-comparable synthesized answers;
Meta-Analysis's own tests plus new cases pinned to these three questions'
literal decomposition; a non-gold paraphrase for at least one (e.g. G1's own
"look over everything currently open" paraphrase, already captured in
Module 21's writeup, still correctly abstaining is the *before* state to
improve on); confirm a genuinely non-decomposable single-topic question
still returns `decompose: false` (regression guard).

---

# Module 22 — M7: reporting-delay computes the wrong metric ✅ DONE

**Branch:** `feature/xagg-incident-to-report-delta`
**Question:** M7. Gold: **mean minutes from incident to report, 15.0 (2024) →
1401.3 (2026)**.

**Root cause — confirmed, and deeper than the reports said.** The reports
correctly identified that `_reporting_delay_rate_by_year` computes the rate
of FIRs recording a delay REASON (0% → 14.9%), a different quantity. But the
real cause was one layer upstream: **neither timestamp reached a queryable
field at all.**

| Layer | State before this module |
|---|---|
| Source API snapshot | ✅ has `incident_datetime` AND `report_datetime`, full time precision |
| `cases` table | ❌ `incident_date` is a bare `DATE` (time discarded); no `report_date` column |
| Graph `Incident` node | ❌ no report timestamp — it existed only inside the free-text narrative |

`_write_occurred_on_edge()` truncates the incident timestamp to `[:10]` for
the day-granular `Date` node — correct for a timeline, and deliberately left
alone — but it means reporting SPEED was not computable finer than a day.
`_reporting_delay_rate_by_year()`'s own comment had already anticipated the
fix: *"self-heals to a true mean-days aggregate … once report_datetime is
projected."*

**What shipped:**
1. `structured_projection.py` projects both timestamps as `Incident`
   properties, following the same optional convention as
   `description`/`reporting_delay_reason` (absent → no property, so "not
   recorded" stays distinguishable from "recorded as blank"). Projects the
   raw pair, not a precomputed delta.
2. `xagg.py` adds `_incident_to_report_minutes_by_year()` plus pure,
   unit-testable `_minutes_between()`/`_parse_iso_datetime()` helpers.
   `_reporting_delay_rate_by_year()` is **kept** — it answers the A7-family
   question and now has its own direct regression test.
3. New `time_bucketed_mean` kind wired into **all three** rendering sites via
   one shared `render_time_bucketed_mean()`.
4. `scripts/backfill_incident_report_timestamps.py` — see the coordination
   note below.

**Result — live, through real `/api/chat`:**
> *"No, people are not reporting incidents to the police as quickly in 2026
> as they did in 2024… **15.0 minutes** across **13 FIRs** in 2024, compared
> to **1401.3 minutes (~23.4 hours)** across **51 FIRs** in 2026."*

An exact match to gold, including both FIR counts and the ~23.4-hour scale
gold itself cites.

**⚠️ Correction to this plan's own parallelization model.** This module was
listed as `xagg.py`-only. It was not — it needed a projection change **and a
write to the shared graph**. That is a *different kind* of conflict from file
overlap, and the table above does not model it: file-disjoint tracks can
still collide through the shared database. Modules 23/24 should assume the
same may apply to them.

The shared-DB write was made as safe as possible: a **targeted property
backfill**, not a re-projection. It writes zero nodes and zero edges, so it
cannot reproduce the duplicate-edge damage a previous re-projection caused
(`MODULE_18_FINAL_REPORT.md` §3 — every relationship type roughly doubled).
It is MATCH-only, idempotent, `--dry-run` capable. Result: **64 of 73 FIRs
carry both timestamps; 64 nodes updated, 0 not found** (no graph/snapshot
drift).

**Non-gold paraphrase: ✅ now passes end-to-end — resolved by Module 26.**
Mid-module this was an open gap: the paraphrase (*"How long does it typically
take someone to report a crime to us these days versus a couple of years
ago?"*) failed live because `router.py` classified it as `RAG`, so it never
reached XAGG at all — even though calling `run_aggregate()` directly with it
returned the correct answer. `router.py` is Module 26's file, so it was
flagged for coordination rather than edited here.

Module 26 (PR #13, deterministic XAGG routing) merged while this module was
in flight. After merging `origin/main` in and re-testing, the paraphrase now
routes `XAGG -> Large-Scale Aggregate` and returns:

> *"the mean time from incident to report was **15.0 minutes** in 2024
> (across 13 FIRs) compared to **1401.3 minutes (~23.4 hours)** in 2026
> (across 51 FIRs). Reporting is notably slower in 2026 than in 2024."*

Worth noting as a parallelization result: two file-disjoint tracks each fixed
one layer of the same end-to-end failure, and neither alone was sufficient —
Module 22 made the metric computable and the matcher paraphrase-tolerant,
Module 26 made the routing deterministic.

**A curve-fitting bug the paraphrase step caught** (this is what that step is
for): `_REPORTING_SPEED_COMPARISON_KEYWORDS` was pinned to M7's literal
wording and matched no paraphrase at all. Widened to a two-signal AND
(reporting/speed signal **and** time-period comparison signal), because a
one-signal widening collided in two directions — with **A7** (a
count-of-delay-reasons question checked *after* this one, so an over-broad
match silently hijacks it) and with **KB8**, which initially DID match on
"report" + "pehle" — but its "pehle" means "before completion", not "years
before", and matching it would have regressed PR #9's KB routing. Now
negative-controlled: **matches M7 and only M7 of the 32 gold questions**,
while catching three natural paraphrases. That all-32 assertion is now a
test — it is the check that would have caught all three historical pattern
collisions on this plan.

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

# Module 25 — M2: Meta-Analysis synthesis rejected by the verifier ✅

**Branch:** `fix/meta-analysis-synthesis-verifier-rejection`
**Question:** M2. Gold is a simple computable fact: 9 of 73 FIRs (~12%) from
2 of 19 stations — **verified directly against the graph** (Postgres/Apache
AGE, `evidence_graph`): `MATCH (c:Case)-[:FILED_AT]->(s:PoliceStation)` gives
exactly 19 stations and 73 cases total, and the two "سائبر کرائم سرکل"
(Cyber Crime Circle) stations — Islamabad and Rawalpindi — carry 5 + 4 = 9
cases. Gold's numbers are exact.

**Root cause found — NOT what the plan's own framing assumed.** The original
framing (an 11×17 interaction where Meta-Analysis composes correctly and the
verifier alone is at fault) was written from `MODULE_18_FINAL_REPORT.md`/
`GOLD32_RESULTS_FOR_TEAMMATE.md` without first-hand reproduction, and by the
time this module actually ran, the picture had also moved: PRs #7-#9 landed
in between, and **Module 13's own hard-refusal in `xagg.py`** (`_STATION_TYPE_KEYWORDS`
→ `_UNSUPPORTED_STATION_TYPE`) had already been merged, changing M2's whole
failure shape. Live reproduction (2026-09-08, this module's own session) found:

1. **M2's exact gold text no longer reliably reaches a rejectable synthesis
   at all** — LLM-driven decomposition is non-deterministic run to run. When
   it decomposes into "current vs. two-years-ago" sub-queries, both usually
   route to Large-Scale Aggregate, which correctly and honestly answers "this
   system does not classify stations by type" (Module 13's own deliberate,
   correct behavior — station type genuinely isn't a modeled column; it
   *is* derivable from station names, which Module 13 didn't have visibility
   into, but that's a Module 13/xagg.py-scope question, not this module's).
   That honest answer is real, self-consistent, and the verifier correctly
   passes it — there is no bug in this specific path today.
2. **The actual, reproducible verifier defect** surfaced on M2's exact gold
   text and on a compound paraphrase alike, roughly half the time: *"The
   answer incorrectly cites [Document 2], which is not present in the CHUNKS
   list... actually sourced from [Document 1] (chunk 2)."* Captured full
   verifier output live. **Confirmed the actual mechanism**: each
   sub-answer's own text already carries a `[Document N]` citation from
   *its own* originating sub-agent (RAG/GRAPH/XAGG/...), pointing at
   evidence that never travels past that sub-agent's own `SubAgentResult`.
   Meta-Analysis's pseudo-chunk builder left that citation embedded
   verbatim, so two sub-answers each independently ending "...[Document 1]"
   became meta-analysis chunks `[1]` and `[2]` — **both still containing a
   stale, identically-numbered "[Document 1]" inside their own text**,
   colliding with the *separate* `[Document N]` numbering the synthesis
   prompt assigns to the pseudo-chunks themselves. This is what actually
   confused the verifier's LLM judge into misreading which chunk backed
   which claim. **No deterministic pre-check ever fired** (confirmed: no
   `FABRICATED CITATION` / `SECURITY: Cross-case leakage` log lines on any
   rejected run) — this is the LLM judge alone, misled by the doubly-numbered
   citation scheme. Same *class* of defect as PR #7's
   `_check_fabricated_case_ids` fix — a citation-parsing assumption that
   doesn't hold for this input shape — just surfacing in the judge's own
   reasoning instead of a deterministic check.
3. `case_id="cross_case"` was checked and is **not** implicated:
   `_check_leakage()` and `_check_fabricated_case_ids()` both no-op cleanly
   on `"cross_case"` / an empty known-case-id set respectively (the latter is
   exactly PR #7's own fix, already covering this shape).

**Fix (`src/pipeline/harness/agents/meta_analysis.py`):** added
`_strip_nested_citations()` — strips any `[Document N]` / `(Document N)` /
`**Document N**` marker out of every sub-answer's text before it becomes
part of the synthesis prompt *or* a verifier pseudo-chunk. A precondition
guard, not a weakening of the verifier itself (`verifier.py` untouched) —
same shape as PR #7's fix, applied at the call site that was feeding the
verifier a shape it wasn't built for, rather than bending the general check.

**Live verification, before/after (isolated worktree, `../muhafiz-m25`,
own backend on :8010, own DB check independent of the shared dev stack):**
- *Before fix:* M2's exact gold text rejected with the reason quoted above
  (full verifier dict captured); a compound paraphrase ("Compared to
  general-purpose stations, are the specialized crime units seeing faster
  caseload growth?") rejected the same way.
- *After fix:* M2's exact gold text run 3×: 2 clean `status=ok` passes
  (no verifier rejection, in both the "honest no-classification-data" shape
  and a "partially confirmed" shape that still resolved to OK), 1 unrelated
  sub-query timeout (model-server latency — pre-existing infra flakiness,
  not a verifier defect). The same paraphrase that reliably rejected before
  the fix now also passes.
- **Second half confirmed**: a genuinely hallucinated synthesis is still
  rejected — both live (Module 17's own prior evidence of catching a real
  cross-chunk hallucination, unaffected since `verifier.py` itself was not
  touched) and by the new
  `test_hallucinated_synthesis_is_still_rejected` unit test.
- **Regression guard**: M4 (an existing Urdu compound-comparison question
  that already reaches Meta-Analysis via its own trigger pattern) re-run
  live post-fix — `status=ok`, no regression.

**Unit tests added** (`tests/test_harness_agent_meta_analysis.py`):
`test_strip_nested_citations_removes_every_document_marker_shape`,
`test_nested_citations_are_stripped_before_reaching_the_synthesis_prompt_and_verifier`,
`test_hallucinated_synthesis_is_still_rejected`. Full suite
(`test_verifier.py` + `test_harness_agent_meta_analysis.py`) passes: 117
tests (up from 111).

**Correcting the plan's own framing, as this section originally asked:**
the root cause is real and is squarely a verifier/Meta-Analysis interaction
bug as originally suspected — but the *mechanism* (a citation-numbering
collision confusing the LLM judge, not a precondition mismatch in a
deterministic check, and not a composition defect in Meta-Analysis itself)
differs from what §"Investigate" above guessed, and M2's *specific* live
failure mode had already partly shifted to a **separate, out-of-scope gap**:
Module 13's `xagg.py` hard-refusal for "station type" is honest but
conservative — it does not attempt a name-based heuristic (station names
literally contain "سائبر کرائم" — Cyber Crime — for the 2 specialized
stations), so M2 will not reliably return gold's *exact* 9-of-73 figure
until that separate gap is closed. That is a `xagg.py`/Module 13-scope
follow-up, not this module's file scope (`meta_analysis.py`/`verifier.py`),
and is flagged here rather than fixed in this PR to avoid touching a file
owned by a different concurrent track.

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
