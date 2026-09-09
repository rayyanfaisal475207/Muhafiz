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
| 21 | XNETWORK/XGRAPH relevance-gate over-refusal | `fix/xnetwork-relevance-gate-over-refusal` | ✅ **Merged (PR #12)** — investigated, no fix belongs in this module's files, split into Modules 28/29 |
| 22 | M7 reporting-delay: wrong metric | `feature/xagg-incident-to-report-delta` | ✅ **Merged (PR #14)** — M7 AND its non-gold paraphrase both verified live, matching gold exactly |
| 23 | M5 weapon × statute co-occurrence join | `feature/xagg-weapon-statute-cooccurrence` | ✅ **Merged (PR #20)** — new `_weapon_statute_cooccurrence_by_year()` aggregate; M5 AND its non-gold paraphrase both verified live, **matching gold's per-year pairings exactly**; G5 and M1 negative-controlled live. Result: `docs/gold-qa-wave2-results/MODULE23_RESULT.md` |
| 24 | M4 statute × court-stage join | `feature/xagg-statute-court-stage-join` | ✅ **Merged (PR #22)** — new `_statute_court_stage_join()` aggregate reusing Module 14's reader; **gold's 33 / 1 / 30 matched exactly** and the non-gold paraphrase passes; G3, CR7 and M5 negative-controlled live. M4 end to end is now blocked **above** XAGG, in Meta-Analysis (Module 25's `[Document N]` defect) — see §8. Result: `docs/gold-qa-wave2-results/MODULE24_RESULT.md` |
| 25 | M2 Meta-Analysis → verifier rejection | `fix/meta-analysis-synthesis-verifier-rejection` | ✅ **Merged (PR #16)** |
| 26 | M1 routing miss (XGRAPH instead of aggregate) | `fix/router-year-over-year-comparison-to-xagg` | ✅ **Merged (PR #13)** |
| 28 | CR4 routing miss (weapon-recovery chain sent to cross-case entity linkage) | `fix/router-weapon-evidence-chain-to-xagg` | ✅ **Merged (PR #19)** — routing fixed AND a new aggregate added (none existed); CR4 now returns gold's exact chain live; all-32 negative control clean; results: `docs/gold-qa-wave2-results/MODULE28_RESULT.md` |
| 29 | Meta-Analysis decomposer doesn't split broad synthesis asks into XAGG-shaped sub-questions (CR3/G1/G6) | `fix/meta-analysis-decompose-broad-synthesis` | ✅ **Merged (PR #23)** — deterministic decomposition plans added; G1, G6 and G1's paraphrase now return real synthesized answers built from computed aggregates, CR3 partially; gap analysis split out as Modules 31–36; results: `docs/gold-qa-wave2-results/MODULE29_RESULT.md` |
| 30 | KB3/KB8/KB9 retrieval-completeness gap (correct statutory chunk never enters the candidate pool) | `fix/kb-retrieval-completeness-statutory-chunks` | ✅ **Merged (PR #24)** — four causes found and fixed, plus a fifth (mid-sentence chunks) found only after the first four; KB3/KB8/KB9 all went from abstaining to answering live, KB8 matching gold's statutory half exactly. Evaluator NOT changed. Ran against a private copy of Chroma. Result: `docs/gold-qa-wave2-results/MODULE30_RESULT.md` |
| 31 | G1 — offender age profile: no XAGG aggregate, and a now-stale hard refusal | `feature/xagg-g1-caseload-profile-aggregates` | ✅ Done — aggregate live (24–49, mean 31.5); G1 itself unchanged, see §31 |
| 32 | G1 — accused ↔ complainant relationship breakdown: no XAGG aggregate | `feature/xagg-g1-caseload-profile-aggregates` | ✅ Done — اجنبی 15 of 24; person-recurrence fall-through killed |
| 33 | G1 — seized-property disposition counts: no XAGG aggregate | `feature/xagg-g1-caseload-profile-aggregates` | ✅ Done — 13 forensic-lab / 7 heirs, matches gold exactly |
| 34 | G1 — incident time-of-day distribution: no XAGG aggregate | `feature/xagg-g1-caseload-profile-aggregates` | ✅ Done — gold's "flat across the day" is a date-only artefact |
| 35 | G6 — arrest rate: no XAGG aggregate | `feature/xagg-arrest-rate-and-fir-listing` | ✅ Done — aggregate live; published rule gives **1 in 6.6**, not gold's 1 in 9, see §35 |
| 36 | CR3 — subject-filtered FIR listing: no aggregate returns FIR numbers filtered by statute/station/crime type | `feature/xagg-arrest-rate-and-fir-listing` | ✅ Done — returns `fir-64-26`/`fir-65-26` exactly; **wiring into `record_consistency` deferred behind Module 41**, see §36 |
| 37 | **Orphaned `chunk_fulltext` rows are load-bearing, not hygiene** — 2,246 rows BM25 ranks and serves whose Chroma ids no longer exist | *(not yet branched)* | ⬜ **Re-scoped and approved.** Found by Module 30 and filed as cleanup; **Module 82 proved it costs a gold question**: 2 of KB2's 5 window chunks are orphans and the answer's **lead citation is one of them on 3 of 3 runs**, after which it concludes the *opposite* of gold. Re-measured 2026-09-10: **2,246 orphans** (2,243 CrPC + 3 test docs) and, in the other direction, **790 Chroma ids absent from BM25** (Module 99). Approved scope: **back the rows up to a committed file, then delete — orphans only**. Approved timing: **after the live tracks land**, because m83/m92/m89/m64 are measuring against this shared store and a mid-flight change would void their baselines |
| 38 | `cross_rerank_multi()` merges by max score across queries, and cross-encoder scores are not comparable across them | `fix/cross-rerank-rrf-fusion` | ✅ **Done — the per-query lists are fused by reciprocal rank**, reusing `reranker.py`'s own `reciprocal_rank_fusion()` rather than a second implementation. Measured on one shared KB4 candidate pool so only the merge differs: the wrong "Forensics guidelines" hypothesis scored 0.86–0.97 where the question topped out at 0.16, and took **all five slots**; fused by rank, ranks 1–4 are Punjab Police Rules register chunks carrying gold's **rule 27.16** and its **three-year** rule, verified in the chunk text by id. **KB4's retrieval went 0-of-3 → 3-of-3 runs, its `status` went 3-of-3 → 1-of-3** — reported, not tuned: all three `main` runs "answered" that the corpus does not contain this. KB6 and KB9 both went from abstaining to answering. KB bucket unchanged at 6/8 answering. Ran against a private copy of Chroma. Result: `docs/gold-qa-wave2-results/MODULE38_RESULT.md` |
| 39 | The "and does our data show it?" half of every gold KB answer is unreachable from the RAG sub-agent | `feat/kb-data-half-composition` | ✅ **Done — the data half went from 0 of 48 runs to 8 of 12** on the four KB questions that have an aggregate to reach. `rag_tool()` now dispatches one canned XAGG sub-query concurrently with retrieval for a compound legal-KB question and folds the aggregate's deterministic rendering in as one extra citable chunk. **Live, KB1–KB9 × 3 runs × 2 arms:** KB4 **45 property entries** 3/3, KB5 **8 women-violence reports** 1/3, KB6 **32 weapons** 2/3, KB9 2/3; answered 18/24 → **19/24**. **The substantive finding was measured, not predicted:** wired only into the RESULT, KB9 abstained 3/3 and the gate's own reason on all 18 refusals was that the statute chunks do not say whether OUR system records an inquest — a correct verdict on a compound question judged against half the evidence. Showing the gate the data half took KB9 to 2/3 and KB4 to 3/3 (un-doing Module 52's own KB4 regression). **Cost: 0.59 s mean per aggregate, +1.6 s (+0.6 %) end to end**, 0 of 30 dispatches dropped. All-32 EQUALITY control exact (only KB4/5/6/9 gated); all 8 regression questions route to XAGG and never enter this code. **Gold challenged** on KB9: the corpus has **10** FIRs citing PPC 302, not 8. New defects split out as Modules **67/68/69**. Result: `docs/gold-qa-wave2-results/MODULE39_RESULT.md` |
| 41 | **G2/G5 REGRESSION** — Meta-Analysis over-decomposes questions XAGG answers in one call; the supervisor guard fired only for time-comparison shapes | `fix/supervisor-skip-decomposition-for-resolvable-aggregates` | ✅ **Done — PR #31** — guard now asks `run_aggregate()`'s extracted, resolution-only chain; G2 and G5 correct on 3/3 live runs each. Result: `docs/gold-qa-wave2-results/MODULE41_RESULT.md` |
| 42 | KB6 hard failure — `route=None`, FactualCorrectness 0.0 **and** AnswerRelevancy 0.0, did not recover on re-run | `fix/kb6-hard-failure-route-none` (PR #34) | ✅ Done — **not a pipeline defect.** `route=None` was the eval harness's own 300s client timeout; live KB6 is `route='RAG'` 5/5. Harness fixed so a timeout is unscored, not a 0.0. The real reason KB6 abstains is a new cross-language evaluator defect — split out as Module 52 |
| 43 | M7 answers with the wrong facts (FC 0.0 / AR 1.0) despite Module 22 verifying it live against gold | `fix/m7-m2-cs4-factual-accuracy` | ✅ **Done — PR #38 — the report is wrong, not Module 22.** M7 returns gold exactly (15.0 min / 13 FIRs; 1401.3 min / 51 FIRs) on **6 of 6** live runs. The only M7 answer in this repo is `_reporting_delay_rate_by_year()`'s output — a function with no dispatch entry since Module 22 — recorded 2026-09-06, two days *before* Module 22 landed. Independently confirms Module 47. Fixed: the missing `XAGG <kind>` log line. Result: `docs/gold-qa-wave2-results/MODULE43_RESULT.md` |
| 44 | M2 and CS4 reach XAGG and answer fluently but factually wrong (FC 0.0 / AR 1.0) | `fix/m7-m2-cs4-factual-accuracy` | ✅ **Done — PR #38 — both gold answers are computable and now computed.** CS4 had NO aggregate and was answered by the person-recurrence fall-through (third recorded time); it now returns gold exactly — وقاص / 00000-9000020-1 — on 3/3 live runs. M2's `unsupported_station_type` refusal was **asserting something false**: 2 of the 19 stations are named Cyber Crime Circles and carry 9 of 73 FIRs, which is gold's own claim. Reproduced 3/3. **Gold challenged** on the growth half — general-purpose stations grew 7→39, the specialised ones 3→5. Result: `docs/gold-qa-wave2-results/MODULE44_RESULT.md` |
| 45 | Eval harness: a judge `null` FactualCorrectness is scored as 0 rather than re-run | `fix/eval-harness-null-scores-and-truncation` | ✅ Done — confirmed and fixed. `measure()` never retried the CR8 failure at all (its `tries` loop only ran for rate limits), and **no mean was computed in the repo at all** — every published figure was hand-derived. Nulls are now retried, then recorded as unscored and excluded by a new null-safe `summarize()`. 22 new tests. The 900-char cap was measured and is **not** behind any current 0.0 — split out as Module 46. Result: `docs/gold-qa-wave2-results/MODULE45_RESULT.md` |
| 46 | Eval harness: the 900-char answer cap silences whole answers, and its stated (Faithfulness) justification no longer exists | `fix/eval-answer-cap-and-quota-check` | ✅ **Fixed** — default is now **0 (no cap)**. Behaviour-preserving on the committed run (only M4/M5 exceeded 900, and Module 45 measured both unchanged); protective from here, since the build now emits 1,000–2,500-char KB answers. Also fixes the quota check: `grep "rate limit"` misses Gemini's `RESOURCE_EXHAUSTED`. |
| 47 | The 2026-09-08 post-fix evaluation's artefacts were never committed | `eval/module27-final` | ✅ **Closed by Module 27 (PR #55)** — its three-pass rerun regenerated `gold32_results.json` and `gold32_pipeline_outputs.json` and committed them, along with the per-pass artefacts |
| 40 | M4's Meta-Analysis synthesis rejected by the verifier — **closed as superseded** | `fix/meta-analysis-reliability-53-57-40` (branch `fix/meta-analysis-m4-synthesis-verifier-rejection` left unmerged on origin) | ✅ **Closed as superseded.** Merged in and judged on its merits against CR3/G6: 8 live runs with the change applied, and the attribution-blind second opinion **never fired once** — the first verifier pass passed every time. Measured directly instead: it does what it claims (on a swapped-citation input its objection moves off attribution) but **cannot rescue CR3's actual rejection**, which is a *negative inference over a complete listing*, not a citation mis-numbering — still `grounded=False` with the flag on, 3 of 3. Ablation showed the synthesis prompt rule made no difference either (4/4 with and without), and reverting the whole module gave CR3 3/4. A hallucinated synthesis is still rejected, 3 of 3, live. Its `statute_vs_court_stage` plan was **not** taken because a matched plan vetoes Module 41's guard. Result: `docs/gold-qa-wave2-results/MODULE40_RESULT.md` |
| 48 | KB2 and KB9 answer fluently but factually wrong (FC 0.0 / AR 1.0) after Module 30 fixed their retrieval | *(not yet branched)* | ⬜ New — the last two KB questions with no module of their own |
| 49 | KB3 reaches Police Order Article 18 but still scores 0.1 — it quotes the article without drawing gold's conclusion | *(not yet branched)* | ⬜ New — Module 30 got the chunk in; the synthesis half is unaddressed |
| 50 | G1/G6/CR3 consolidation: wire Modules 31–36's aggregates into their decomposition plans, and settle `_MAX_SUB_QUERIES` | `fix/meta-analysis-wire-g1-g6-aggregates` | ✅ **Done** — all six wired; every one now fires on **every** live run (`caseload_review` 7/7 × 4 kinds, `orientation_note` 6/6, `record_consistency` 3/3). G1 reproduces 3 of gold's 4 findings exactly and corrects the 4th per Module 34; CR3 now gets gold's exact `CMS-ISB-2026-0341`; G6 reports the arrest rate for the first time. **Cap settled at 5 with measurement** — N=9 loses 4 sub-queries to the 60 s timeout, N=6 fails 1 run in 3. New defects split out as Modules 53–54. Result: `docs/gold-qa-wave2-results/MODULE50_RESULT.md` |
| 54 | Two provider-failure gaps Module 46's quota fix did not close: `503 UNAVAILABLE` is not in its grep, and a failed cutover classification silently falls back to `orchestrator.py` | `fix/provider-failure-visibility` | ✅ **Fixed** — pattern widened to `rate limit\|RESOURCE_EXHAUSTED\|429\|quota\|UNAVAILABLE\|503`, canonical in `gold32_score.py`'s `LOG_GREP_PATTERN`; the cutover fallback now emits a `cutover_classification_failed` SSE event, recorded per row in `gold32_pipeline_outputs.json`. 18 new tests, 197 passing. Live deferred (two backends mid-wave). Module 46 owns the `RESOURCE_EXHAUSTED` half. See `MODULE54_RESULT.md` |
| 53 | Meta-Analysis' 60 s sub-query timeout is one shared wall-clock deadline, so it kills whichever sub-query is served LAST, not the slow one | `fix/meta-analysis-reliability-53-57-40` | ✅ **Done.** Reproduced by controlled experiment (deadline forced to 25 s, G1 at N=5): **8 of 10 sub-answers dropped before, 0 of 10 after**, with all five aggregates reaching the answer on both after-runs. Fix reuses `large_scale_aggregate.py`'s existing verifier-rejection fallback — a `ContextVar` salvage box (`agents/_salvage.py`) survives `wait_for()`'s cancellation, so the already-computed aggregate is served raw and disclosed. `META_ANALYSIS_SUBQUERY_TIMEOUT` 60 → 150, derived from the measured staircase. At the shipped config, **zero dropped sub-answers at N=5 across 12 runs** of G1/G6/CR3. `_MAX_PLAN_SUB_QUERIES` deliberately left at 5 — see Module 59. Result: `docs/gold-qa-wave2-results/MODULE53_RESULT.md` |
| 51 | `backend.log` is written through a cp1252 stream, so any Urdu-carrying log record is **silently destroyed** inside `logging.emit()` | `fix/backend-log-utf8-encoding` | ✅ **Fixed** — handler-level UTF-8; the `XAGG <kind>` diagnostics every module is verified against were being deleted |
| 55 | Every XAGG aggregate predating Modules 31–36 emits no `XAGG <kind>:` log line, so no live run of it can be identified from `backend.log` | `fix/xagg-log-lines-and-m2-vocabulary` | ✅ **Done** — measured **32 distinct aggregate kinds, 11 log lines, 21 families silent** (the brief's "~18" undercounted). All 21 now log, each carrying the **figures**, plus the three honest-refusal paths that were previously indistinguishable from XAGG never running. Confirmed live in-process against Postgres + AGE: 21 of 21 emit real figures, Urdu values intact post-PR #30. The durable fix is an **AST-derived enforcing test** — a new family with no log line fails the suite. Backend/SSE run **deferred for contention** (8015 and 8016 both mid-run). Result: `docs/gold-qa-wave2-results/MODULE55_RESULT.md` |
| 56 | M2's dispatch vocabulary is narrower than the family it now serves — a station-type question that avoids `_STATION_TYPE_KEYWORDS`' exact wording falls to the per-station catch-all | `fix/xagg-log-lines-and-m2-vocabulary` | ✅ **Done** — tuple widened from 10 to **58** entries: specialist/specialised/dedicated unit and station forms, the ordinary/normal/regular general-purpose side, and their Roman-Urdu (`makhsoos`/`khaas`/`aam thana`) and Urdu (خصوصی/مخصوص/عام تھانہ) equivalents. Every entry binds the qualifier to a station word; no bare عام-class word. **All-32 EQUALITY control passes** — every gold question's resolved kind byte-identical to the pre-change map. The measured failing phrasing now reaches `station_caseload_by_specialisation`, as do 8 non-gold paraphrases across all three languages; M2 still reproduces gold's 9-of-73 from 2-of-19. New defect split out as **Module 59**. Result: `docs/gold-qa-wave2-results/MODULE56_RESULT.md` |
| 57 | CR3 is non-deterministic across runs at three different layers | `fix/meta-analysis-reliability-53-57-40` | ✅ **Diagnosis closed; CR3 still unstable. No code change of its own.** Re-measured over 20 consecutive runs of CR3's gold text after Module 53: sub-query timeouts **0 of 20** (removed by Module 53); `route=None` **0 of 20** (did not reproduce — not fixed, *not observed*; `xnetwork.py` correctly untouched); synthesis-verifier rejection **7 of 14 on the shipped code** and now the only failure left. Its reason is singular and verbatim-identical every time — the claim that FIR 65/26 is *absent* from the CMS linkage list, which is exactly what gold asserts. Measured **not** to be citation attribution, so Module 40 cannot fix it either. CR3's dispatch is fully stable (same route, plan and 3 sub-queries, 20/20). Result: `docs/gold-qa-wave2-results/MODULE57_RESULT.md` |
| 58 | `_STATION_TYPE_KEYWORDS` is a 58-entry substring list where the file's other hard dispatch calls use a multi-signal predicate | `fix/m4-routing-and-station-predicate` | ✅ **Done.** The tuple is **deleted** and replaced by `_is_station_specialisation()` — a station/unit **noun** AND a specialisation qualifier, in two tiers. Tier 1 (`specialist`, `general-purpose`, `one type of crime`…) is free-floating; tier 2 (the ordinary-language `ordinary`/`normal`/`regular`/`dedicated`/`aam`/`khaas`/`عام`/`خصوصی`) counts only **directly on the station noun**, generating the pairings Module 56 enumerated by hand. Two design points are measured, not asserted: a naive AND hijacks **KB9** (`police` + `khaas tor par`) out of `graph_recurrence_person`, so a bare `police`/`پولیس` is **not** a station signal; and `unit` is word-bounded against `opportunity`/`impunity`. **All-32 EQUALITY control re-run: identical, 32 of 32**, and exactly one of the 32 (`M2`) matches the predicate. **The filed defect is confirmed and fixed** — *"How many cases involve one type of crime only?"* and two siblings resolved to this family before and now do not; live, that phrasing gets `total_count` and an honest limitation, 2/2. **14 out-of-gold adversarial negatives** added, so the guarantee no longer rests on the 32. Live: M2 gold **3/3** to `station_caseload_by_specialisation` (gold's 9-of-73 from 2-of-19 on 2 of 3 — the aggregate is right 3/3, the prose drops it once: Module 70); Module 56's own measured failure phrasing **2/2**. Regression G3/CR7/G2/G5/M5 **2/2 each**, no family moved. New defects: **69, 70**. Result: `docs/gold-qa-wave2-results/MODULE58_RESULT.md` |
| 59 | `_MAX_PLAN_SUB_QUERIES` is still 5, and the case for raising it is now open but unmeasured | *(not yet branched)* | ⬜ New — found by Module 53, which met Module 50's stated prerequisite for revisiting the cap and then deliberately did not revisit it. Salvage degrades an answer's *prose*; it does not remove the model server's serialisation, and model spend and the user's wall-clock wait are unchanged. The post-Module-53 staircase at N=6..9 has never been measured. Concretely at stake: gold's G6 "mostly men" element, dropped by Module 50 to fit the arrest rate. |
| 60 | M4 does **not** skip decomposition live — its route is XNETWORK, and Module 41's guard only fires on XAGG | `fix/m4-routing-and-station-predicate` | ✅ **Done — and the defect is worse than filed.** Re-measured live on one backend, router reverted vs. restored, 6 runs each arm. **Before: four distinct outcomes in six runs.** 2/6 `XNETWORK` → decomposed, 325–329 s, gold's court-stage half lost to the sub-query timeout; 2/6 `XAGG` → answers correctly (**these are the runs Module 41's static verification assumed were universal**); **2/6 plain `RAG` → `status=error` after 456 s / 487 s** — a failure mode never previously recorded for M4, filed as **Module 74**. The router itself was probed directly, 8 calls on M4's gold text: **6 XAGG / 2 XNETWORK, `confidence: medium` on all eight** (Module 75). **Option 3 taken — fix the ROUTE, not the guard.** Option 2 was rejected on measurement, not preference: with `route=XNETWORK` the supervisor falls through to Cross-Case Linkage / Global Search, which still cannot reach the aggregate, so widening the guard would repeal its protection for every cross-case route and fix nothing. Option 1 (Module 40's plan) was rejected because a matched plan **vetoes** the guard. The new `router.py` override is gated on **`resolve_aggregate_kind()`**, Module 41's single source of dispatch truth — so it inherits every precedence rule above `_is_statute_court_stage_join()` and **G3's `court_readiness_scan` and CR7's cross-check can never reach it**. The broad form (`resolves_to_specific_aggregate()`) was measured and rejected: it moves **7 of the 32**, including KB5. **After: 7 of 7 `XAGG` → Large-Scale Aggregate, one dispatch, `statute_court_stage_join` logged every run, 13.6–29.7 s, every gold element present on every run** — including the court-stage half, which had never appeared before. **All-32 EQUALITY negative control: exactly one question changed, M4** (None → XAGG); variants swept, none moved. Regression G3/CR7/G2/G5/M5 **2/2 each**. New defects: **67, 68**. Result: `docs/gold-qa-wave2-results/MODULE60_RESULT.md` |
| 62 | The deterministic decomposition plans have a narrow lexical reach | *(not yet branched)* | ⬜ New — found by Modules 57/40's non-gold paraphrase check, generalising Module 41's M4 finding to CR3 and G6. A CR3 paraphrase keeping "online banking fraud" and "handled the same way" matches `record_consistency` and answers correctly 2/2; one asking whether the records are "equally complete" matches nothing and never reaches Meta-Analysis. Same boundary for G6's `orientation_note`. The capability is **not** tied to the gold string, but it is tied to a small neighbourhood around it. The fix is probably not "add more patterns" — it is deciding whether a plan should be selected by regex at all, or by the same aggregate-resolution mechanism Module 41 used for its guard. |
| 52 | The relevance gate cannot judge a roman-Urdu question against English statute text (English 6/6 relevant, roman-Urdu 1/6, identical chunks) | `fix/kb-evaluator-english-rendering` | ✅ **Done — the gate defect is real and fixed, and it does not on its own win the KB bucket.** The legal-KB path now renders the QUESTION in English (`render_question_in_english()`) for both retrieval and `evaluate_relevance()`; `prompts/evaluator.txt` untouched. Layer 1 re-measured on this branch, chunk set constant and containing gold's text: **0/3 roman-Urdu → 3/3 English**. Live, KB1–KB9 × 3 runs × 2 arms, **re-baselined against `main` @ `a841f5d`**: answered **18/24 → 19/24**, thematic law-half coverage **14/24 in BOTH arms**. KB9 abstained 2/3 → answers 3/3 with CrPC s.174 + Rule 25.35; KB6's six rounds collapse to **1/1/1** and its runtime falls 65%; **KB4 regresses 2/3 → 0/3**, isolated by probe to the verifier, not the gate. Non-gold roman-Urdu paraphrase: 2/3 → 3/3 answered and gold's three statutory specifics on 2 of 3 runs vs **0 of 3** before. New defects split out as Modules 58–61. Result: `docs/gold-qa-wave2-results/MODULE52_RESULT.md` |
| 63 | `_is_legal_kb_intent()` misses an ordinary Roman-Urdu paraphrase, so every KB fix is silently skipped for it | `fix/router-paraphrase-generalisation` | 🔄 In flight — committed on its branch, not yet pushed. Originally — found by Module 52, measured directly (`False` for a plain rephrasing of KB6). Routed to the mixed FIR pool; no KB scope, no statute hypotheses, no English rendering. **Sits ahead of Modules 30/38/52 in the path** |
| 64 | KB6's `c19`/`c18` window is still not retrieved from KB6's own wording | *(not yet branched)* | ⬜ New — found by Module 52. Module 52's brief predicted the English rendering in retrieval would surface it; measured, it does not (3/3 runs land on `c116`). A **paraphrase** of KB6 does reach it, so corpus/widening/generator are all capable. **[Corroborated by Module 39]** independently, on a different paraphrase and a different branch: an ordinary ENGLISH rephrasing of KB6 retrieves gold's *"unloaded … safety on … no live rounds in the chamber, magazine, or parcel"* on 2 of 3 live runs |
| 65 | KB2's gold rests on Qanun-e-Shahadat Arts 38/39 and CrPC s.162 — no run ever retrieves them | `fix/kb2-retrieval-qanun-shahadat` | ✅ **Done — the retrieval gap is real, and closed.** The probe settled the ingestion-vs-retrieval question first: **all three provisions ARE in the corpus** — Art. 38 `2_qanun-e-shahadat-order-1984_pdf_3e604153_c176`, Art. 39 `…_c177`, CrPC s.162(1) `1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_f9908363_c675` — and **no query the pipeline generated reached any of them**, because both statute hypotheses read "why isn't this recorded?" as a record-keeping question and spent themselves on the case diary and CrPC s.161. Fixed **entirely in `prompts/statute_hypothesis.txt`**: an evidence book-selection branch, the Qanun-e-Shahadat's map entry enlarged, and a **prohibitive** rule 4c. Nothing in `src/retrieval/` or `rag.py` changed. Live, 3 runs each arm, `route=RAG` throughout: KB2 **0/3 → 3/3** on Art. 38 and **0/3 → 3/3** on s.162, answered **1/3 → 3/3**. A Roman-Urdu non-gold paraphrase goes **0/3 → 3/3 on all three provisions** and draws gold's *"this is why they are not kept"* conclusion. Equality control on all eight KB questions: **6 of 8 hypothesis book-pairs unchanged**. **KB2 still does not reach gold's conclusion** — with Art. 38 in the window it hedges into *"may be considered a … data gap"*, citing Module 37's orphaned `…_0519abd8` s.164 chunks; that residue is **generation**, split out as **Module 77**. **Module 38's `DEFAULT_HYPOTHESES` question is answered and closed**: n=3 is now affordable and still buys nothing measured, so it stays at 2. **KB4 answers 0 of 5 on the new prompt vs 3 of 5 on the old** — measured on a reverted-prompt control to be its documented verifier flap, same book and same rule-27.16 chunk either way, but reported as a worsening rather than explained away. Also found: **KB3 now routes to XNETWORK and KB9 to XAGG**, not RAG. Result: `docs/gold-qa-wave2-results/MODULE65_RESULT.md` |
| 66 | The English rendering can narrow a question's scope (KB5: "violence" → "domestic violence") | *(not yet branched)* | ⬜ New — found by Module 52 in its own output, against `prompts/question_english.txt` rule 4. KB5's evaluator rounds also worsened, mean 1.7 → 4.0 |
| 67 | The router classifies a plainly cross-case aggregate question as plain **RAG**, and the pipeline then burns ~8 minutes before failing hard | `fix/router-rag-misclassification` | ✅ **Done — both halves, and the row's own exposure count was wrong.** The override-less set is **sixteen**, not eleven: this row omitted **A1, CS4, CP1, M2, M7**, and those five are the ones RAG structurally cannot answer (the eleven it named are exposed to a slow failure; these were exposed to a wrong one). **(1) The misclassification.** Module 60's resolver-gated override is widened from one aggregate kind to a named allow-list of five — `statute_court_stage_join` (M4), `station_caseload_by_specialisation` (M2), `incident_to_report_minutes_by_year` (M7), `criminal_record_local_match_gap` (CS4), `weapon_recovery_rate_by_district` (CP1). This is **not** the broad `resolves_to_specific_aggregate()` form Module 60 rejected: that form's real blast radius is **eight** (Module 60's prose says seven and then names eight — the count was the typo), and the two kinds producing its wrong moves are excluded **by name** — `gender_breakdown` (KB5's false positive, shared with A1, indistinguishable at the kind, so **neither** is routed) and `case_completeness_scan` (G1; measured — G1's live route is already XAGG 2/2 and it still decomposes, so capturing it would buy nothing). **All-32 EQUALITY control: exactly four move, M2/M7/CS4/CP1**, none other, variants swept. **Live, 4 runs each, both arms: route unchanged 16/16 → 16/16** — the LLM already said XAGG on this machine today, so this is insurance against Module 60's measured tail, not a repair of an observed misroute — but wall-clock falls **53.0 s → 14.3 s mean** because the router LLM call is skipped. **(2) The cost.** `SEMANTIC_SEARCH_DEADLINE_S` (360 s, env-overridable, 0 disables) bounds the Semantic Search dispatch only, returning `ABSTAINED` + `error.kind="timeout"` and an `error` PipelineEvent naming the deadline — never another Module-42-shaped `route=None`. Not in `tools/rag.py`'s retry loop: that file is Module 39's and lowering `MAX_RETRIES` would take attempts the KB path needs. The number is measured (slowest known success KB6 247.1 s; doomed 347–487 s) and its limit is stated: the populations **overlap** (KB3 abstained at 176.7 s, answered at 178.4 s), so 360 s bounds the tail, it does not classify. **KB3 reproduced the pathology before the fix: 468.1 s / 347.5 s `status=error`**; after, the ceiling **fired live** (`exceeded its 360s deadline`) — but the request still took 406.7 s, because the rewriter and two `route_query()` calls sit outside the bound (**Module 73**). Regression: **M4 7/7** (Module 60's holds, every gold element), G3/CR7/G2/G5/M5 2/2 each, **KB5 2/2 still RAG**, **G1 2/2 still decomposes**, A1 2/2 unchanged. M2 carries gold's 9-of-73 on 3 of 4 — the miss is **Module 70**, whose aggregate emitted the figure every run in both arms. New defects: **72, 73**. Module 68 measured but left open. Result: `docs/gold-qa-wave2-results/MODULE67_RESULT.md` |
| 68 | `route_query()`'s `confidence` is `medium` for a classification it gets wrong 1 run in 4 | *(not yet branched)* | ⬜ **Still open — re-measured by Module 78, still no principled fix, still not forced.** Module 67's measurement stands and is not overturned: 128 calls, `medium` on KB3's eight including both wrong ones, so the field cannot distinguish a coin flip from a certainty. Module 78 adds a second sample that points the other way and is reported rather than acted on — in its bisect the field separated correct from incorrect 12 of 12 (`medium` on KB3's and KB9's wrong classifications, `high` on both paraphrases' right ones). A field that is `medium` for a coin flip AND `medium` for a stable-but-wrong answer AND `high` for a stable-right one carries some signal and not enough to branch on. **What did change is the exposure**: Module 78 makes all eight KB questions deterministic, so the override-less set drops from **16 to 8** and `confidence` is an honest `"high"` for the eight. Correlating the field still needs run-to-run stability the router cannot see from one call, and removing it still changes the SSE `router done` contract. |
| 69 | `_is_station_specialisation()`'s tier-2 proximity window (0–1 words) is a judgement, not a measurement | *(not yet branched)* | ⬜ New — found by Module 58 in its own work. The window covers the pairings Module 56 enumerated by hand and survives 14 adversarial phrasings, but the number was chosen, not fitted: a gap of 2 would admit *"ordinary, everyday police stations"* and also *"normal for a case at the station"*. **No corpus of real user phrasings exists to fit it against** — one should, before the window is widened. |
| 70 | M2's headline concentration figure is dropped ABOVE XAGG | `fix/synthesis-citation-durability` (diagnosis only) | ⬜ **Open — diagnosed, not fixed** (Module 83's track, `MODULE83_RESULT.md` §9). **The filed rate is wrong and the defect is worse:** filed as 1 in 3, measured here at **0 of 9** consecutive live M2 runs (0 of 10 counting the all-32 baseline run). Three-way capture, `large_scale_aggregate.py` temporarily instrumented: the **aggregate payload** (`19 stations, 73 FIRs; crime_type=2/9`) and the **rendered `raw_summary_text`** (*"… though even so, **9 of 73 FIRs (~12.3%) are carried by just 2 of 19 stations**…"*, stated twice) are **byte-identical on 9 of 9**; the **paraphrase drops the headline on 9 of 9**, and the served answer is byte-identical to the paraphrase. **Root cause:** `verifier.py::verify_structured_aggregate_paraphrase()` computes only `ans_nums - src_nums` — one-directional by construction, so it catches every number a paraphrase INVENTS and cannot see one it OMITS; it logged `grounded=True unsupported_numbers=[]` on all nine. Module 44 already pushed the rendering side as far as it goes (its two comments record the same loss at 3 of 3). **Proposed fix:** an omission half — the figures of `raw_summary_text`'s first rendered line must appear in the paraphrase, else serve `raw_summary_text` via the existing branch. **Left open deliberately:** that gate is shared by most XAGG-composing questions (G2/G5/M2/M4/M5/KB), so it needs its own all-32 equality control, and the check may belong in `verifier.py`, which is **Module 85's territory (`muhafiz-kbgen`)** — the two tracks should agree where the rule lives first. |
| 61 | The grounding verifier refuses a **negative inference over a complete listing**, which is exactly what CR3's gold answer asserts | `fix/verifier-over-rejection` | ✅ Done — `MODULE61_RESULT.md`. A chunk can now be DECLARED a complete enumeration (`metadata["exhaustive_scope"]`, set only for an XAGG-only sub-answer), and non-membership in one is supported rather than inferred. Two deterministic guards keep it narrow: the claim is checked against the listing it NAMES (CR3 serves three listings at once, and 65/26 legitimately appears in a sibling one — pooling them made every correct negative look fabricated), and a claim asserting the absence of a record the listing CONTAINS is still rejected. Live before/after on one machine: CR3 **4 of 8 → 7 of 8**, with all four pre-fix rejections carrying the FIR 65/26 absence reason. The validation gate now IMPORTS the same rule, so the two gates agree — the "could only be partially confirmed" caveat for that claim is gone on 8 of 8. Hallucination still rejected (unit test + live: CR3's one remaining failure is a different, correctly-refused claim). **KB4 is a separate defect** — its rejections are over-attribution to a RAG chunk, which this rule never touches. New defect split out as **Module 74**. |
| 71 | G1's synthesis verifier intermittently rejects an unsupported **numeric** claim | `fix/verifier-numeric-overreach` | ✅ Done — `MODULE71_RESULT.md`. **The fabricated 73 is the incident-time-of-day sub-answer's denominator** ("64 of 73 incidents"), the only one of G1's five sub-answers that states 73, borrowed onto seized property (which computes 45 entries across 28 FIRs). Fixed on the GENERATION side only: rule 2 rescoped from "appears literally in a sub-answer above" — **the rule the fabricated 73 satisfied** — to "the sub-answer you cite for it", the `caseload_review` synthesis goal stopped inviting a coverage figure to travel between findings, and a rejected synthesis now serves the verified sub-answers instead of `status=error` (`large_scale_aggregate.py`/Module 53's shape). `verifier.py`, `validation.py` and `prompts/verifier.txt` untouched. **The defect did not reproduce in 25 pre-fix live G1 runs** (Module 61 saw 1 in 3; this path has since got ~3× faster), so no answer-rate improvement is claimed — proven instead on a forced-hallucination control: same fabricated synthesis, same real verifier, **`status=error` 3/3 → `status=done` 4/4**, all four gold findings served, fabrication never served, and the deterministic detector naming `73 -> [Document 3]` on 4/4. A per-document figure roster was built, measured, **found harmful (G6 0/4 → 7/8 rejections, Module 29's citation refusal) and deleted**. Regressions CR3/G6/M2/G2/G5 **40 of 40**. New defect split out as **82**. |
| 72 | `A1` and `KB5` are indistinguishable at `gender_breakdown`, so neither can be given a deterministic route | *(not yet branched)* | ⬜ New — found by Module 67 in its own work. `resolve_aggregate_kind()` sends both A1 (نامزد ملزمان میں مرد اور عورت کا تناسب — a genuine cross-case count) and KB5 (a legal-KB question about violence against women) to `gender_breakdown`. One is a true dispatch, the other a vocabulary false positive, and nothing **at the kind level** separates them — which is the sole reason Module 67 could not give A1 a deterministic route without dragging KB5 into a cross-case aggregate, the exact mistake Module 60 refused. A1 is 8-of-8 XAGG on the probe and 2-of-2 live: stable today, unpinned tomorrow. **Verify:** whether `_is_gender_breakdown()` can require a caseload-counting signal KB5's statutory phrasing does not carry, negative-controlled against all 32 and against KB5's variants. |
| 73 | A doomed RAG request costs more than the sub-agent it is bounded at, because the same classification is computed twice | *(not yet branched)* | ⬜ New — found by Module 67 while measuring its own fix. `SEMANTIC_SEARCH_DEADLINE_S` bounds the Semantic Search dispatch and **fires live** (`exceeded its 360s deadline`), yet the request still took **406.7 s**: the query rewriter, `main.py`'s cutover classification call and `Supervisor.handle()`'s own `route_query()` all sit outside the bound, and a router call alone is **15–30 s** (Module 67 measured 128 of them). Two of those three are the same classification, computed twice per request. **Verify:** whether `main.py` can hand its already-computed route to `Supervisor.handle()` instead of having it recomputed, and what the end-to-end saving is. Owner is `main.py`/`cutover.py` — neither of which Module 67 was allowed to touch. |
| 74 | KB3's data half — "the registering officer and the investigating officer are the same person in 68 of 74 pairs" — had **no aggregate**; the question resolved to `unsupported_officer`, an honest refusal | `feat/xagg-kb-data-half-aggregates` | ✅ **Done — figure reproduces gold exactly, third independent derivation.** Re-probed rather than inheriting Module 39's numbers: 144 `(:Officer)-[:ASSIGNED_TO {role}]->(:Case)` edges, **74 investigating / 70 recording**, and **68 of the 74 investigating assignments name the same person as that case's recording officer = 91.9 %**, 6 split across 6 cases — gold's "68 of 74 pairs (92%)" and "only 6 cases", exactly. **Grain is the ASSIGNMENT edge, not the case, and that is load-bearing**: `fir-205-26` carries TWO investigating edges (a superseded placeholder ASI plus its real successor), so a per-case count gives 68/5 — true, and not gold. (Module 39's breakdown listed `fir-205-26` among three "genuine" splits; corrected in `MODULE74_RESULT.md` §1.1.) The refusal is **preserved**: `_is_officer_role_pair_comparison()` needs THREE signals (registering AND investigating AND sameness), so a general "which officer" identity question still gets `unsupported_officer` — pinned both ways by `TestOfficerRolePairBoundary`. All-32 EQUALITY control moves **exactly one** question, KB3. Live: the canned sub-query is `route=XAGG`, `XAGG officer_role_pair_overlap: … same officer on 68 of 74 pair(s) (91.9%)` on **3 of 3** runs plus 3 in-process, answering "68 out of 74 … 92%". **Vocabulary widened after BOTH paraphrases missed** — Module 56's finding again; 5 of 6 post-widening, the miss pinned rather than tuned. **KB3's own data half is still not live**, for two reasons neither of which is in `xagg.py`: the plan entry is in `rag.py` (out of bounds, Module 77) and **KB3 routes to XNETWORK 3/3, never RAG** (Module 78). Result: `docs/gold-qa-wave2-results/MODULE74_RESULT.md` |
| 75 | KB8's data half — "26 challans sent to court" — had **no aggregate**; the question resolved to `criminal_record_court_crosscheck`, which counts the 33 criminal records instead | `feat/xagg-kb-data-half-aggregates` | ✅ **Done — and the suspected cause is CONFIRMED, and worse than a two-way confusion.** Probed before any code: there are **three** record types and **three** quantities — `criminal_record` **33**, `chalaan_dispatch` **26** (26 rows / 26 distinct cases, all linked), `chalaan_outcome` **20**. `grep -c chalaan_dispatch src/pipeline/xagg.py` was **0**; the file had a challan reader and it read the wrong one of the two challan types, while the question routed to a family that read neither. **26 = gold exactly.** `_is_chalaan_dispatch_count()` takes two signals (challan AND sent-to-court) and sits ABOVE `_CRIMINAL_RECORD_KEYWORDS`; **CR7 is untouched** (2/2 live, still 33) and the all-32 EQUALITY control is **byte-identical** — KB8's gold TEXT never says "challan", so it reaches the family only through the canned sub-query, exactly as KB4/KB5/KB6 do. The aggregate reports the `chalaan_outcome` count **alongside** rather than instead, and the log line names both, so a future wrong-metric run is legible rather than plausible. Gold's schema-absence claim ("no interim report anywhere in the schema") is **derived from the record types present**, not declared, and stops being claimed the moment such a type is ingested. Live: `route=XAGG`, `XAGG chalaan_dispatch_count: 26 challan dispatch record(s) across 26 case(s) … interim-report record type present=False` on **3 of 3** runs plus 3 in-process; answer "26 challans have been sent to court, covering 26 cases". Paraphrases 6/6 (one Urdu verb-stem bug found and fixed by its own test). **KB8's data half is still not live** — it needs the one-entry `rag.py` addition (Module 77); unlike KB3 it DOES reach RAG 3/3. Result: `MODULE75_RESULT.md` |
| 76 | KB9's data half needs a **per-section FIR count** ("how many FIRs cite PPC 302?"); the only aggregate publishing one was `statute_court_stage_join`, a whole-caseload two-view report at the wrong grain | `feat/xagg-kb-data-half-aggregates` | ✅ **Done — and gold is challenged for the third independent time.** `fir_section_case_count` answers the per-section question and nothing else, counted **per FIR**, leading with the section the query names (extraction is generic — any act, any code, English/Roman-Urdu/Urdu — nothing special-cased to 302) and stating an explicit "no FIR cites section X" when absent. **Re-derived independently as the brief required, not inherited: 218 `fir_section` rows over 73 FIRs, 36 distinct sections, and PPC §302 = 10 FIRs** — with BOTH inflation routes checked and excluded (exactly 10 rows and 10 distinct case_ids, so no double counting; `302` is the only section code in the corpus containing "302"). **Gold says 8. 10 vs 8 is 25 %, outside the brief's 5–10 % tolerance, and nothing here is tuned toward 8 — the recommendation is to CORRECT GOLD to 10.** The dispatch placement — immediately BELOW `_is_statute_court_stage_join()` — is load-bearing, not cosmetic: Module 39 already ships a KB9 plan naming `statute_court_stage_join`, and `rag.py::_run_kb_data_half()` DROPS a half whose family differs, so capturing that string would have taken KB9 from a measured 2/3 to **0/3 with every unit test green**. Pinned as a test. All-32 EQUALITY control **byte-identical**; M4 unmoved 2/2 live. Live: 3 in-process runs, `status=OK`, `XAGG fir_section_case_count: … focus=302 -> 10 FIR(s)`, rendering "10 of the 73 FIR(s) … cite PPC §302". **Gold's SECOND KB9 element (property to a deceased's heirs) is unchanged by this work and does not need a new aggregate** — `seized_property_disposition` already computes it; what is missing is a TWO-aggregate plan shape (Module 79). **KB9's data half is still not live**: it routes to **XAGG 3/3 and lands on `graph_recurrence_person`** (four repeat accused, answering nothing it asked) and never reaches RAG — Module 78. Result: `MODULE76_RESULT.md` |
| 77 | The three `rag.py::_KB_DATA_HALF_PLANS` entries that land Modules 74/75/76 — KB3, KB8, and KB9 re-pointed from `statute_court_stage_join` to `fir_section_case_count` | `feat/kb-data-half-plan-entries` | ✅ **Done — all three entries are correct; two are never consulted, and that is the finding.** The landing itself was the one-entry-each addition Module 39's design note advertised: two entries added, KB9's re-pointed **atomically** (`sub_query` and `expected_kind` together — a half-done re-point is a silent 2/3 → 0/3 with every unit test green), all three sub-queries copied byte-for-byte from the strings Modules 74/75/76 pinned in `tests/test_xagg.py`. **All-32 EQUALITY control: exactly KB3, KB4, KB5, KB6, KB8, KB9 resolve to a plan and the other 26 to `None`**, identical before and after the §6 widening; CR7 and CP6 each pinned at `None` by their own regression, so `criminal_record_court_crosscheck` and `unsupported_officer` keep every question they answer correctly. Unit tests **81 → 92, all green**. **Live, the brief's predicted complication reproduced exactly: KB3 → XNETWORK 3/3 and KB9 → XAGG 3/3, so their entries fired ZERO times on their own gold wording**; only **KB8** reaches RAG (3/3, plan fired 3/3, **26 = gold exactly**, both halves 2/3 — the third is a grounding-verifier `status=error` *after* the aggregate was composed). Data half over the six KB questions: **10 of 18** (Module 39 took it 0-of-48 → 8-of-12; this is not 18-of-18). **The most useful measurement is the one the brief did not ask for: an ordinary ENGLISH PARAPHRASE of KB3 and of KB9 routes to RAG 2/2 and fires its plan 2/2**, P-KB3 answering *"68 out of 74 recorded assignment pairs (92%)"* — so the router discriminates on the gold WORDINGS, not the subjects, and **Module 78 now has a bisectable control rather than "the router is the variable"**. In-process against the real graph all three compose (**68/74 · 26 + no interim-report type · 10 FIRs cite PPC 302**). **Vocabulary widened after TWO of three paraphrases missed** — Module 56's finding for the third time, Module 74's for the second — and reported as a miss, not designed away. KB4/KB5/KB6 regression **9/9 route, 9/9 plan, 9/9 data half**. 0 quota lines over 26 live runs. New defects **84** (KB8's norm half never reaches gold's 14-day interim report), **85** (the grounding verifier rejects a composed KB answer that HAS its figure), **86** (KB4 serves the breakdown but drops the 45). Result: `docs/gold-qa-wave2-results/MODULE77_RESULT.md` |
| 78 | KB3 and KB9 never reach the RAG path at all — KB3 routes to **XNETWORK** 3/3, KB9 to **XAGG** 3/3 — so both lose the statutory half Modules 30/38/52 built for them, and neither fires its Module 77 data-half plan even once | `fix/router-paraphrase-generalisation` | ✅ **Done — both reach RAG 3/3 on their own gold wordings, and both plans fire 3/3.** **The cause was bisected, not assumed.** `orchestrator.py` routes on the REWRITTEN query, so the rewriter had to be excluded first: `rewrite_query()` returns both gold strings **byte-identical 3 of 3**, and the route is the same on the gold text and on the rewriter's output. The LLM classifier is the variable and its own `reason` field says why — KB3 *"an open-ended synthesis of cross-case data"*, KB9 *"a cross-case aggregate … specifically mentions that there are many such cases"*. **Both questions are compound and both carry a whole-caseload scope cue in their DATA clause; the classifier weighs that cue above the legal-norm clause and classifies the second half of the question**, which is exactly why Module 77's paraphrases — which carry no such cue — route RAG. 3/3, 3/3, 3/3, 3/3, temperature 0. **The fix is Module 60/67's resolver-gated pattern one route over**: route to RAG exactly when `rag.py::_is_legal_kb_intent()` — the same predicate that decides the KB-only corpus scope, the statute hypotheses, the English rendering and the data-half plan — claims the question. No new vocabulary, so it cannot drift from what RAG will do. **Checked LAST of all the overrides, and that is the blast-radius bound**: it can only fire where this file had no opinion at all, so G5/G3/CR7/M4 keep their entries by construction. **All-32 EQUALITY control: eight questions move from `None` to a deterministic RAG and they are exactly the eight KB questions; only TWO change route, KB3 and KB9**; the other 24 byte-identical. Live: **KB3 RAG 3/3, plan 3/3, gold's "68 of 74 (92%)" in the answer 3/3** (it returned XNETWORK's refusal on all three of Module 27's passes); **KB9 RAG 3/3, plan 3/3, s.174 + s.176 + PPR 25.31 + "10 FIRs cite PPC §302" + the schema gap on 1 of 3** — the other two are the grounding verifier's `status=error` AFTER the aggregate was composed (**Module 85**, `verifier.py`, out of bounds). **One regression this caused, caught by the existing suite and fixed not re-baselined**: `_CASE_ANCHOR_RE` does not know "this weapon", so findings.md Module 7's compound example was pulled to RAG and lost its `secondary_methods`; guarded with the router's own `_SQL_OVERRIDE_COMPOUND_THIS_X_RE`. Regression live: M4 4/4, G3/CR7/G2/G5/M5 2/2 each, KB5 **8** 2/2, KB6 **32** 2/2, KB8 **26** 2 of 3, KB4 2/2 — **no question moved**. Unit 417 → 431 green. 0 quota lines over 42 live runs. **Module 63 closed in the same chain**; **Module 68 re-measured and left open**. Paraphrase bar reported honestly: gate 8/8, route 7/8 (**88**), and a NEW KB3 paraphrase routes RAG 3/3 but fires no plan (**89**). Result: `docs/gold-qa-wave2-results/MODULE78_RESULT.md` |
| 84 | KB8's norm half reaches CrPC s.172's case diary and s.173's **final** report, and never gold's **14-day interim report** to the magistrate | *(not yet branched)* | ⬜ New — measured by Module 77 on `:8027`, 5 live runs (3 gold + 2 post-widening), `route=RAG` throughout. The **data** half is perfect on every one of them (26 challans, the schema gap, the missing challan↔investigation link); the **norm** half states the reporting duty only in general terms and never reaches gold's mechanism — an interim report through the public prosecutor within three days of the fourteenth. This is the exact mirror of the problem Module 39 set out to fix, and it is retrieval/generation on the statute side: adjacent to Modules 30/52/65, NOT to a plan entry |
| 82 | KB2 hedges against the provision it was just given | `fix/kb-downstream-generation` | ✅ **Done — root-caused to BOTH named leads plus a third nobody expected, and no code change survived measurement.** KB2's window is identical 3 of 3 and **2 of its 5 chunks (`…_0519abd8_c590`, `…_c601`) return nothing from `get_by_ids()`** — Module 37's orphans, confirmed on the **shared** store by id — and **the answer's lead citation is one of them, 3 of 3**, concluding *"it is not a data gap; the system does maintain such records"*, the opposite of gold on the facts. **CrPC s.162(1) sits at window position 1 on every run and is never used for its bar.** The third finding undermines the premise: **QSO Art. 38 `…3e604153_c176` reaches the window 0 of 3 for gold's wording and 3 of 3 for Module 65's Roman-Urdu paraphrase**, which also draws gold's conclusion 3 of 3 with no code change — so the split is retrieval, filed as **97**. Module 37 is very likely part of the real fix and was deliberately not touched (2,243 rows in a shared `chunk_fulltext`). A generation fix was written, shipped to a live arm and **deleted**: it left KB2 at 0-of-N on gold's conclusion and cost KB4 and its paraphrase two of three answers each. Also found: **no deterministic pre-check in `verify_grounding()` fires on the legal-KB path at all**, so the LLM judge is the only hallucination guard there — proven live, 3 of 3, by a forced control. Result: `docs/gold-qa-wave2-results/MODULE82_RESULT.md` |
| 85 | The grounding verifier rejects a composed KB answer that has already been handed its figure | `fix/kb-downstream-generation` | ✅ **Done — and the defect did NOT reproduce as a rejection.** On `main` @ `6cf89fb`, KB4 is `status=done` **3 of 3** and the whole shipped-code arm (12 primary + 17 regression runs) carries **zero** Semantic-Search verifier rejections; Module 38 saw 2 of 3 rejected, Module 61 saw 5 of 5 answering, and Module 61's warning that "the 1-of-3 figure should not be treated as fixed" still stands. **What reproduced 3 of 3 is the defect underneath it**: KB4's window holds `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2197` (*"…custody of the police for over three years… This register is a permanent record"*) at position 2 and `…_c2421` (Form 27.18(1)'s column list) at position 4 — gold's own substance, read out of the store by id — and the answer is built entirely on three Anti-Rape/Forensics **packaging** chunks at positions 1, 3 and 6, then reports that *"the records do not show any entries that explicitly mention the marking, packaging, or chain of custody"* — a false negative manufactured by answering a question nobody asked. That is Module 38's recorded rejection reason one step earlier, now passing the judge. **A generation fix was written, measured live and DELETED**: two rules in `semantic_search.py`'s prompt took KB4 **3/3 → 1/3** and its Roman-Urdu paraphrase **3/3 → 1/3**, adding **4 verifier rejections**, every one of them the judge correctly refusing a *compliance* claim the rules invited (*"It is clear from the records that the police have followed the procedures…"*). `src/` is byte-identical to `main`. Hallucination guard proven live: a forced control rejected an invented rule 27.41(3) / FIR 512/26 / "fully compliant" **3 of 3**, markers never served. New defect **97**. Result: `docs/gold-qa-wave2-results/MODULE85_RESULT.md` |
| 86 | KB4's answer serves the disposition breakdown but drops the denominator | `fix/kb-downstream-generation` | ✅ **Done — reproduced 6 of 6, root-caused, and NOT fixed; the honest finding is where it is not.** `xagg.py::render_seized_property_disposition` opens its chunk with *"What happens to seized property — **45** property-register entr(ies) across 28 FIR(s)"*, so **the total is the first number in the first line handed to the generator**, on every run — this is not an upstream rendering gap and `xagg.py` needs no change. The answer reproduces the 28, all nine dispositions and gold's 13-forensic / 7-heirs pair, and states the **45 on 0 of 6 runs** (KB4 3/3 and its paraphrase 3/3; Module 77 measured 3, this is six). Module 39's rule (*"the figures exactly as that summary gives them"*) is already satisfied — a breakdown is a faithful **subset** — the same scoping miss Module 71 found in its own rule 2. **The obvious fix was tried and measured to do nothing**: adding *"INCLUDING the totals and denominators it states and not only the breakdown beneath them"* produced the 45 on **0 of 5** runs, and shipped alongside rules that cost KB4 two of three answers, so all of it was reverted. A third restatement is the least likely thing to work; §8f records the measurement that would actually discriminate. Folded into `MODULE85_RESULT.md` |
| 93 | Module 15's CR6 pattern claims an ordinary **KB1 paraphrase** and routes it to an aggregate that has no statute to give it | *(not yet branched)* | ⬜ New — found by Module 78's paraphrase battery. *"What rule decides when a written **complaint** has to be turned into a formal **FIR**, and do our own records follow it?"* passes `rag.py`'s legal-KB gate but is intercepted first by `_XAGG_OVERRIDE_PATTERNS`' `complaint … FIR` entry (Module 15, question CR6) and routed to XAGG's `cms_fir_linkage`. This is the stated cost of Module 78's "checked last" placement: the property that makes that override safe also lets an earlier list claim a KB paraphrase. **KB1's own gold text is unaffected** — it says "report of a crime", not "complaint" — and still routes RAG. The fix is a discriminator on CR6's own pattern (CR6 asks whether a complaint *links to* an FIR record, not what the law requires), and it needs live CR6 measurement Module 78 does not have. Pinned at its measured value in `test_module78_every_kb_paraphrase_reaches_rag_too` rather than removed from the battery |
| 94 | `_KB_DATA_HALF_PLANS`' `officer_role_pair` pattern misses a **third** KB3 paraphrase — Module 56's finding for the fourth time | *(not yet branched)* | ⬜ New — found by Module 78, live, 3 of 3 runs. *"Is the officer who **writes up** an FIR meant to be a different person from the one who investigates the case, and do our own case records **bear that out** across the whole caseload?"* now routes to RAG 3/3 (Module 78 fixed that half) and fires **no** plan, so the answer carries no data half at all. Module 74 widened this vocabulary once and Module 77 widened it again, each after its own paraphrases missed; a fourth instance says the plan patterns want the same treatment the ROUTE just got — a predicate, not a word list. `rag.py`'s plan table is Module 77's and widening it needs that module's all-32 plan-resolution control re-run, which is why Module 78 reported it instead of widening it |
| 79 | A compound KB question can need **two** aggregates, and `_KbDataHalfPlan` is single-aggregate by construction | *(not yet branched)* | ⬜ New — found by Module 76. KB9's gold answer carries a charging-side figure ("8 FIRs cite PPC 302", really 10) **and** a property one ("kai zabt shuda property entries meyyat ke warisaan ko wapas karne ke liye nishaanzad hain"). The second is `seized_property_disposition`'s own heirs figure and **already exists** — Module 39 reaches it for KB4 — so nothing needs computing; only the plan SHAPE is missing. Module 39 predicted this in its own row ("this question may want two aggregates rather than a better one") and Module 76 confirms it: a better aggregate closes the first element and leaves the second exactly where it was |
| 80 | `"How many FIRs cite PPC section 302, across all cases?"` and two rewordings are all classified **SQL** by the router, 2 of 3 ending `status=error` after 77–93 s | *(not yet branched)* | ⬜ New — found by Module 76 while looking for live evidence. **Harmless to the composition path and fatal to a user typing the question**: a `_KB_DATA_HALF_PLANS` sub-query is handed straight to `xagg_tool()` and never sees the router (Module 39 §2.1, and Module 76 exercised that path 3/3 successfully), so the new aggregate lands correctly where it is meant to. But no phrasing of a per-section FIR count reaches XAGG on its own. `router.py`, adjacent to Modules 60/67 |
| 81 | The canonical quota grep matched a **timestamp** — `16:19:29,503` scored as a 503 UNAVAILABLE | `fix/quota-grep-timestamp-false-positive` | ✅ **Fixed** — numeric codes boundary-guarded; every module that certified a clean run with the bare pattern was reading its own milliseconds as a provider failure |
| 83 | Meta-Analysis' synthesis intermittently cites nothing at all | `fix/synthesis-citation-durability` | ✅ **Done — `MODULE83_RESULT.md`. The filed diagnosis was WRONG, and that is the finding.** It was filed as "the citation rule is held only by its position in the prompt". Logged live before the Verifier sees it (20 G6 runs), **every refused synthesis is a degenerate repetition loop** — 758 tokens, 18 unique (ratio **0.024**), one 4-gram repeated **370** times, zero digits, **byte-identical across six runs** because `call_llm` decodes at `temperature=0.0`. The passing runs measure 0.57–0.69 and cite `[Document 1]`–`[Document 5]` correctly. `verifier.py`'s refusal is **not a false positive** — it is the only gate catching a broken generation — and restating or moving the citation rule could not have helped, because the collapsed text has no prose to cite. **Module 71's length sensitivity reproduces** (G6 refusals 3 of 4 at current length, **4 of 4** with the sub-answers section lengthened) but **its 0-of-4 baseline does not**: on current `main` G6 refuses on **12 of 16** baseline runs — what length changes is the probability of the COLLAPSE. **Fix:** a deterministic `_is_degenerate()` (≥ 60 tokens AND unique ratio < 0.15 AND a 4-gram repeated ≥ 10× — thresholds inside the measured gap), **one regeneration at `temperature=0.4`** (a same-temperature retry is provably useless), and a twice-collapsed synthesis is **never served and never verified**, routing into Module 71's composition with its own honest caveat. A secondary `_attach_provenance()` re-attaches `[Document N]` deterministically from the figure roster when a synthesis cites nothing — uniquely-supported sentences only — covering the case that was FILED; **it fired 0 times live** and that is reported. `verifier.py`, the synthesis prompt and `_format_subanswers_for_prompt` are **all untouched**; the refusal is not relaxed. **Live, with the lengthened-prompt control:** G6 synthesis served **1 of 8 → 7 of 8** (baseline plain 1/4, baseline lengthened **0/4**; fixed plain 3/4, fixed lengthened **4/4**), no-citation refusals **7 → 0**; CR3 15 of 16 throughout. **All-32 equality control run both sides: routes identical 32 of 32**, and only 3 questions execute the changed code (CR3/G1/G6) — G1 unchanged in substance, CR3's one move is Module 61's known 1-in-8 grounding flap. New defects: G6's plan matches **only its gold wording** (all 3 pre-registered rewordings, English and Urdu-script included, refuse at XNETWORK, 6 of 6); the plan computes 5 of gold's 7 findings; the Roman-Urdu synthesis contains fluent-sounding non-words. |
| 88 | CR2 scored **0.00 on all three of Module 27's passes**, deterministically — `graph_recurrence_person` found gold's exact pair and rendered it as bare case ids, with no dates, roles, status or conviction | `fix/cr2-recurrence-temporal-context` | ✅ **Done — an evidence-rendering gap, not a reasoning failure, exactly as the brief predicted.** The pre-fix live answer names its own missing evidence: *"there is no information provided that would indicate the chronological order of these cases"*. **Cypher-probed before any code and derived independently:** 4 recurring `Person` nodes, all `case_count=2`; شہزیب عرف شابی `PERSON-685fc54914` on `fir-891-24` (incident **2024-09-14**, role accused, `گرفتار، بعد ازاں سزا یافتہ`, criminal record **"Convicted, on bail pending appeal"**) and `fir-214-26` (incident **2026-03-03**, role accused, `گرفتار`, **"Under trial"**) — **gold reproduces exactly and is NOT challenged**. `_top_recurring_nodes()` now attaches a **date-ordered per-case timeline** (year via `OCCURRED_ON`→`Date` as `_statute_mix_by_year()` does; role/`arrest_status` via `INVOLVED_IN`; `conviction_status` via `_fir_key()` — Module 28's own chain, reused not reinvented), and a **shared `render_graph_recurrence()`** replaces the f-string copied across all three render sites. **The FIR id is deliberately NOT parsed for the year: the live graph disproves the shortcut — `fir-401-26` carries an incident date of 2024-09-25.** Nothing is narrowed toward CR2: no new kind, no `Literal` entry needed, no dispatch change, and the **all-32 dispatch equality control is byte-identical, 0 questions move**. Undated cases (9 of 73) sort last and get no sequence number; the conviction join is `(fir, subject)` so one person's conviction is never attributed to another named on the same FIR; عاصم رشید's two FIRs are **four days apart** and are excluded from the cross-year count by `_spans_calendar_years()`. Live on :8032, both arms same backend: CR2 **0/3 refusal → 7/7 naming شہزیب عرف شابی, FIR 891/24's conviction and FIR 214/26's current accusation**, `route=XAGG` 10/10, `XAGG graph_recurrence: … 4 with a date-ordered timeline, 2 with a criminal-record outcome; شہزیب عرف شابی=2[2024-09-14..2026-03-03]`. An **intermediate arm is reported, not hidden**: the timeline alone took CR2 to a three-name "Yes" and let a paraphrase reproduce a 2024 and a 2026 case in its own body while concluding "none are years apart"; the leading summary line is what closed it. **S3 improved** — it named all four recurrers pre-fix and never said "arrested"; now it names gold's two exactly. Regression **24 runs over S3/CR4/CR6/CR7/CR8/CS4/M4/G3, 3/3 each, 0 route changes**; unit **430 + 645**; 0 quota lines over 39 live runs. New defect **92**. Result: `docs/gold-qa-wave2-results/MODULE88_RESULT.md` |
| 89 | **KB1 (0.30 ×3)** — a compound KB question with no data-half plan: it answers the law and says the documents cannot say whether our recordkeeping follows it | `fix/kb1-data-half-plan` | ✅ **Done — the filed diagnosis was right, and the brief's coverage table reproduced exactly.** Re-derived against `evidence_graph` before any code: **73** cases, complainant **73/73**, recording officer **70/73** (`fir-117-26`, `fir-954-26`, `fir-955-26`), report timestamp **64/73** (9 missing) — the corrected gold (PR #57) is reproduced exactly and is **not** challenged. **One correction to the brief's schema sketch:** `INVOLVED_IN` runs **Person→Incident**, not Person→Case; written as filed the query returns **0**, and the case is reached via `[:PART_OF]`. `complainant_cms` (4 edges, CR6's walk-in record) is deliberately excluded. There was **no aggregate producing these figures**, so this is Modules 74/75/76 + 77 in one: a new **`fir_register_completeness`** aggregate (4 Cypher reads, one shared renderer across all **three** hand-copied XAGG rendering sites — Module 91 de-duplicated the placeholder-officer family, not this one), a two-signal `_is_fir_register_completeness()` inserted **immediately above** G1/G2's `case_completeness_scan`, and `_KB_DATA_HALF_PLANS` entry **(7)**, checked **last** because FIR/register vocabulary is the broadest in that tuple. Gold's value is the honest *"mostly, with these gaps"*, so the renderer states the verdict, both gap lists, the 61/73 intersection, **and** the two s.154 steps (read back, signed) that have **no field at all** — reported as *unmodelled*, not *missing*. **The all-32 dispatch equality control is byte-identical: 0 of the 32 move**, and `resolves_to_specific_aggregate()` is identical main-vs-branch too, so nothing leaked into routing (`router.py`/`supervisor.py` untouched). The plan table moves **exactly one** question. Three first-draft over-matches (D1, CR6, G5 — G5's Roman-Urdu gold says *"record keeping"* about a **weapons** register) were caught **by** that control and are each pinned. Live on :8089: KB1 **RAG 3/3, plan fired 3/3, both halves composed 3/3**, answers byte-identical, every gold figure present; pre-fix was equally deterministic and carried **no figure at all**. Pre-registered paraphrases (committed before the first run): **3 of 4 route RAG and fire the plan 2/2 each**, including Roman-Urdu and Urdu-script; the fourth is intercepted by Module 15's CR6 override — **Module 93 reproduced live on a second paraphrase**, RAG-side already closed. One pattern was widened **after** a pre-registered paraphrase missed, not before (Module 56's finding for the fifth time). Regression **12 questions live, 0 route changes attributable here** (KB3/KB9 XNETWORK/XAGG→RAG are **Module 78's**, and KB3/KB9 now fire Module 77's plans and Modules 74/76's aggregates — first live confirmation the 74→77→78 chain composes). Unit **23 fail before / 32 pass after**. **0 quota lines over 25 live runs.** New defect **100**. Result: `docs/gold-qa-wave2-results/MODULE89_RESULT.md` |
| 90 | **M1 (0.40/0.70/0.40, 1-of-3)** — answered ACT-level counts where gold asserts SECTION-level | `fix/m1-cp6-detail-loss` | ✅ **Done (PR #60).** `_statute_mix_by_year()` now also reads `StructuredRecord{record_type:'fir_section'}` — the identical query Modules 23/24/76 issue — de-duplicated per case, adding `section_counts` and `case_count` alongside the unchanged act grain; it reproduces **every figure gold names, both years**. The *"reports only 2026"* half of this row was **wrong**: all three captured passes contain an explicit 2024 block. The *"PPC (Preventive Detention and Control Act)"* gloss is a **model invention** — that phrase exists nowhere in the repo — closed by spelling the acts out in the evidence. Regression: all 32 through `run_aggregate()` + renderers, main vs branch, **exactly one output differs and it is M1**. Corpus holds **218** `fir_section` rows in total (an earlier note citing 273/979 was wrong). Result: `docs/gold-qa-wave2-results/MODULE90_91_RESULT.md` |
| 91 | **CP6 (0.30 ×3)** — the code defect did NOT reproduce; **gold was wrong and was corrected instead** | `fix/m1-cp6-detail-loss` | ✅ **Done (PR #60), with no behavioural change.** The three-way measurement came back clean: aggregate right (10 current / 11 ever / 7 ASI / 3 SI), renderer emits the split, and re-run live the answer **keeps** it 5/5 local and 3/3 cloud on code byte-identical to the evaluation commit — the drop was model behaviour on the day. The real defect was gold asserting 11, fixed by PR #57 (one case, `fir-205-26`, has since been assigned a real officer). The obvious hardening was implemented, measured at **0/5 local and 0/3 cloud** — a headline bullet gives the model something to answer *"kitne"* with and stop — and **reverted**. Ships a de-duplication of three hand-copied renderings with byte-identical output, plus two regression pins |
| 92 | **Non-English paraphrases lose their route.** A capability is reachable only from a narrow neighbourhood of the gold wording — fourth independent sighting (62 CR3/G6, 78 KB3/KB9, 88 CR2) | `fix/router-language-generalisation` | 🔶 **Measured in full; the defect is NOT fixed, and that is the reported result.** Built a committed 96-paraphrase corpus (one en, one roman-ur, one ur per gold question, all written before any run) plus `evaluation/router_paraphrase_harness.py`. **The plan’s own framing was too narrow**: the probe’s 5/8-vs-0/8 scores the REGEX layer alone, which over 96 paraphrases is weak in every language (en 19%, ru 25%, ur 25%); the shipped router recovers to **55/96 (en 59% / ru 50% / ur 62%)**, a 9-point gap, not total failure. All three directions measured: e5 kNN **42/96** (cross-lingual but not discriminative — unrelated questions also sit at ~0.85), translate-to-English **62/96** (inverts the gap), compact prompt on the **cloud** model **66/96 and flat at 69%/69%/69%** — the only design reaching parity, and **no design gets past ~69%**. **Direction 1 is blocked on quota, not code**: router.txt is **13,003 tokens against Groq’s 8,000 per-request cap** (hard 413), and Gemini’s pool is one 429 key + three 401 keys. **The key finding: route accuracy is NOT a proxy for answer quality** — the compact prompt on LOCAL scored 55→61/96 with nothing regressing, then end-to-end turned CR3’s and G6’s answers into RAG abstentions and sent G6’s English paraphrase to DIRECT, which fabricated an ungrounded note; it did **not** ship. **Shipped instead:** `cloud_system_prompt` on `call_llm_json()` + `prompts/router_compact.txt`, repairing an escalation path that had been silently 413-ing for weeks (before: 413→Gemini 429→`result=None`; after: `route=XAGG` in 0.7 s). **Local path byte-identical on all 32 gold × all 8 routing fields.** **CR2’s ru/ur paraphrases still route XGRAPH — Module 88’s symptom is NOT closed**, nor is Module 62’s. New defects **98–102**. Result: `docs/gold-qa-wave2-results/MODULE92_RESULT.md` |
| 95 | **G2 and G5 produce fluent briefings that name DIFFERENT findings from the ones their gold answers name** — not hallucination, not refusal: wrong evidence selected | `fix/g2-g5-finding-coverage` | ✅ **Done (PR pending).** The original *"passes by a hair"* framing was superseded by Module 87's re-judge of Module 27's **frozen** answers (system held constant): G2 **0.5/0.5/0.5 → 0.3/0.3/0.3**, G5 **0.5/0.6/0.6 → 0.4/0.4/0.4**, both for coverage. Phase 1 established that **4 of the 6 gold findings were not computable at all** — `psrms.fir.station_departure_datetime` (FIR form field 6, populated on 44 of 73) was read by **no line of this codebase**, `fir_zimni.entry_type` is never projected, and two findings are statements about the SCHEMA's shape that no aggregate could express. **Every gold figure reproduces exactly** against the recorded API snapshot — 9 missing incident dates, 188 of 259 untyped zimni entries, **13 of 73** departure-before-report contradictions, 30 of 32 (94%) unlicensed — so this is a code fix and **not** a sixth gold correction. Fix: one projection field + two counts, a Module-22-style MATCH-only backfill, and both existing scans deepened; **no new aggregate kind and not one line of `resolve_aggregate_kind()` touched**, so all 32 dispatches are unchanged by construction. Finding coverage, measured on the evidence each finding rests on rather than on wording: G2 **1/3 → 3/3** and G5 **1/3 → 3/3**, three gold-wording runs each plus 6 pre-registered paraphrases. Result: `docs/gold-qa-wave2-results/MODULE95_RESULT.md` |
| 95b | **CP1 (was 1.0 / 0.5 / 0.9)** — the third question in the original row 95 | *(no module needed)* | ✅ **Closed by measurement, not by a fix.** Module 87's re-judge scored CP1 **1.0 / 1.0 / 1.0**. The "one unlucky draw" framing was right about the mechanism and wrong about which question it threatened: G2 and G5 were flat failures on every pass, and CP1 needed nothing |
| 96 | **KB2 is the last gold question with no module** — 0.00 on all three passes, AnswerRelevancy 1.00 | *(not yet branched)* | ⬜ New — diagnosis first, fix second. Gold is *"no, that's by design"*: an assertion about what the schema deliberately does NOT hold (Qanun-e-Shahadat Arts. 38/39, CrPC s.162 make police-recorded statements unusable, so only witness identity is stored). Module 39 excluded it from `_KB_DATA_HALF_PLANS` as a "schema claim, not a count" — correct for KB2, unlike KB1. FC 0.00 with AR 1.00 usually means fluent and wrong. **Decide which of three it is before writing code**: our answer is wrong, gold is wrong, or the judge cannot score a correct negative |
| 97 | **Gold's own wording loses the provision its paraphrase retrieves** — the same split on two independent questions, one layer below Module 92's router finding | *(not yet branched)* | ⬜ **New — measured by Module 82/85 on `main` @ `6cf89fb`, shared Chroma, reranker live, 3 runs each, `route=RAG` on all twelve.** KB2's QSO Art. 38 `2_qanun-e-shahadat-order-1984_pdf_3e604153_c176`: **0 of 3** for gold's literal text, **3 of 3** for a Roman-Urdu paraphrase. Art. 39 `…_c177`: 0 of 3 vs 3 of 3. KB4's rule 27.16(1) chunk `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2209`: **0 of 3** vs **3 of 3**. Module 65 measured KB2's Art. 38 at 3/3 and Module 38 measured KB4's `…_c2209` at 3/3 — both against **private** Chroma copies on earlier merge-bases; neither reproduces here for the gold question and both do for a paraphrase, same build, same store, same session. Book selection still works (KB2's hypothesis names the Qanun-e-Shahadat and uses its verb *"shall not be proved"*); what varies is the section number and surrounding wording, and Module 65 §7a already measured that variation alone flipping an outcome on an unchanged prompt. The window is not deterministic either — a control run on the same question and same code returned `…_c2209` at position 2. **This is Module 92's shape one layer down**: the route is identical and the *retrieval window* is what the paraphrase changes, so the narrow-neighbourhood problem is not confined to `router.py`. Owner needs `src/retrieval/` and/or `prompts/statute_hypothesis.txt`; probes exist (`scripts/module65_probe.py` for rank-by-query, `scripts/module82_window_probe.py` for text-by-id, read-only) |
| 87 | The Gold-32 **judge**: weakest-tier model pinned as a literal, no polarity rule, and 0.5 score spread on identical answers | `fix/eval-judge-upgrade-and-prompt` | ✅ **Fixed** — judge model is a named, env-overridable constant (`GOLD32_JUDGE_MODEL`) defaulting to **`gemini-3.1-flash-lite`**, chosen on measurement not size: `gemini-2.5-flash`, `3.5-flash` and `3.7-flash` are all capped at **20 requests/DAY** on both working keys and cannot produce one 96-call re-score. A **polarity rule with M7's own worked example** lands the measured false negative: **M7 0.20 → 1.00, 3/3**. Two more false negatives found and justified individually — **CP6 0.30 → 0.90** (the tolerance rule was applied and then overridden by an omission the question never asked for) and **G1 0.53 → 0.97** (its answer carries all four gold findings with gold's figures). **Variance: spread ≥0.3 on 4 of 32 → 1 of 32, mean spread 0.106 → 0.025**, and on a FIXED answer × 5 draws it is **0.0 on 9 of 9** — the model did that, not the prompt. Genuinely wrong answers still fail (CR2 **0.00** 3/3, KB2 0.17, KB3 0.00, G6 0.10) and **seven questions fell**, Creative Generation 0.580 → **0.553**. All-32 mean 0.666 → **0.702**. The rule's first version broke KB9 (0.27 → **1.0**) and is now **fenced** and pinned by a test. Result: `docs/gold-qa-wave2-results/MODULE87_RESULT.md` |
| 98 | **Three of the five Gemini keys in `.env` are dead, and `GEMINI_MODEL`'s model cannot serve one evaluation run** | *(not yet branched)* | ⬜ New — found by Module 87 while choosing a judge. `GEMINI_API_KEY_2/_3/_4` return **401 UNAUTHENTICATED**; only `GEMINI_API_KEY` and `_1` work. On both, **`gemini-2.5-flash` — the value of `GEMINI_MODEL` — is capped at 20 requests per DAY** (so are `3.5-flash` and `3.7-flash`), and the judge's own 500/day tier was exhausted on **both** keys mid-session with other tracks running. Every Gemini caller in the project is therefore on two keys, not five, and can exhaust a model's daily quota part-way through a run — which surfaces as slow calls and `UNSCORED` rows, not as a credential error. Same class as the `SHARE/.env` incident that produced 1,410 rate-limit errors and an invalid KB bucket. **Verify:** hit `/v1beta/models` with each key and record the code; then hit each candidate model once and record `limit:` and `PerDay`/`PerMinute` from the 429 body |
| 99 | **790 Chroma chunks are unreachable by keyword search** — present in `muhafiz_kb`, absent from `chunk_fulltext` | *(not yet branched)* | ⬜ New — found while measuring Module 37 on 2026-09-10. The two indexes disagree in **both** directions; Module 37 covers the half that serves dead ids, this covers the half that silently drops live ones from BM25. **Non-destructive**, unlike 37 — nothing is served wrongly, some chunks are simply invisible to the keyword arm of hybrid retrieval. Cost unmeasured: establish which documents the 790 belong to and whether any gold question needs them before deciding whether to re-index |
| 100 | `xagg.py::_is_officer_role_pair_comparison()` (KB3's family) rejects ordinary rewordings — the capability is reachable only from a narrow neighbourhood of KB3's gold wording | *(not yet branched)* | ⬜ New — found by Module 89 while probing its own dispatch boundaries; measured offline against `resolve_aggregate_kind()` and **identical on `origin/main` and on `fix/kb1-data-half-plan`**, so it is pre-existing and not caused here. KB3's own gold text resolves to `officer_role_pair_overlap`. Three ordinary rewordings do not: *"Is the officer who registers a case normally the one who investigates it?"* → `station_or_category_counts` (a generic station/category group-by), *"Is the registering officer usually the investigating officer?"* → `unsupported_officer`, *"Does the same officer both register and investigate our cases?"* → `station_or_category_counts` **and fires no RAG plan either**. The first two fail on the THIRD signal alone (`_OFFICER_ROLE_SAMENESS_TERMS`): both name both roles, and both express sameness with *"the one who"* / *"usually the"*, which is not a listed token. **This is the XAGG-side twin of the widening Modules 74 and 77 each applied to the RAG-side plan patterns** — `rag.py`'s `officer_role_pair` entry matches the first two of these; `xagg.py`'s predicate does not. It matters because Modules 65/74/78 all measured KB3's ACTUAL route as XNETWORK/XAGG rather than RAG, so the predicate that never got widened is the one that actually runs. Same family as Module 92 (a capability reachable only near the gold wording), one layer down. Not fixed by Module 89: `_OFFICER_ROLE_SAMENESS_TERMS` is Module 74's and widening it needs that module's all-32 control re-run |
| 101 | **KB9's composed answer is rejected by the grounding verifier 2 runs in 3, AFTER its aggregate has already returned gold's figure** | *(not yet branched)* | ⬜ New — filed 2026-09-10 from Module 78's committed `module78_runs.json`. This is Module 85's defect, **reproducing on the question Module 85 did not test**: 85 measured KB4, found zero rejections, and closed. On KB9 the run record shows `status=error`, `route=RAG`, `transport_ok=True`, `error=None`, `quota_lines=[]`, the plan fired (`death_investigation_charging` / `fir_section_case_count`, 829 chars), the aggregate logged `focus=302 -> 10 FIR(s)` — **corrected gold's exact figure** — the relevance evaluator returned `relevant=True`, and the served answer is the verifier's refusal string: *"The generated answer could not be verified as grounded in the retrieved documents."* Nothing failed except the check |
| 108 | **KB6's score spread is the pipeline's, not the judge's — and Module 27 attributes it to the judge** | *(not yet branched)* | ⬜ New — found by Module 87. Of the four questions Module 27 lists with spread ≥0.3 "on identical answers", only **CP1 and M2** have byte-identical captured answers across the three passes; **KB6 (1,840 / 1,929 / 1,943 chars) and G1 (2,949 / 2,649 / 2,371) do not**. Judged five times on ONE fixed answer, KB6 scores 0.4 every time, spread **0.0**; across the three passes it is 0.4 / 0.4 / 0.9. With the judge's variance removed it is the only question left above 0.3 spread, and the remaining movement belongs to the RAG path. **Verify:** several live KB6 runs, diffing the answers, before attributing any KB6 movement to a code change |
| 27 | Final Gold-32 rerun (Module 18 redo) | `eval/module27-final` | ✅ **Done — 3 passes, 96 question-runs.** FactualCorrectness **0.428 → 0.572 → 0.666**; AnswerRelevancy **0.931**; **19 of 32 pass on all three runs** (20 counting M7, which is correct and mis-scored); **routes stable 32/32**; 0 nulls, 0 timeouts, 0 quota, 0 cutover fallbacks. Result: `docs/gold-qa-wave2-results/MODULE27_RESULT.md` |

### Coverage check — every failing question maps to a module

From `EVALUATION_REPORT_POST_FIXES.md` (2026-09-08): 19 of 32 pass, so **13
questions still fail**. This table exists so none of them is quietly dropped;
it is the checklist Module 27 should be able to tick off.

| Q | Score | Owning module | State |
|---|---|---|---|
| G2 | 0.0 ⬇ *regression* | **41** | in progress |
| G5 | 0.0 ⬇ *regression* | **41** | in progress |
| M4 | 0.0 | **40** (routing groundwork in 24) | branch unverified |
| M7 | 0.0 | **43** | queued |
| M2 | 0.0 | **44** | queued |
| CS4 | 0.0 | **44** | queued |
| KB6 | 0.0 | **42** | ✅ done — the 0.0/0.0 was a harness timeout; the abstention's cause split out as **52** |
| KB2 | 0.0 | **48** | queued |
| KB9 | 0.0 | **48** | queued |
| KB3 | 0.1 | **49** | queued |
| KB4 | 0.2 | **38** (its cause: the max-score rerank merge) | queued |
| G1 | 0.1 | **50** (aggregates from 31–34 exist, unwired) | queued |
| G6 | 0.3 | **50** + **35** (arrest rate) | 35 in progress |

Supporting modules that gate the *measurement* rather than a question: **45**
(done — a judge `null` was being read as zero), **46** (the 900-char answer cap
will fire on Module 27's longer answers), **47** (the 2026-09-08 artefacts are
not in the repo, so the report cannot be reproduced here). **37** and **39**
remain open KB-infrastructure items behind 48/49.

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
| 29 *(new, split from 21)* | `src/pipeline/harness/agents/meta_analysis.py` **+ `src/pipeline/harness/supervisor.py` + `prompts/meta_analysis_decomposer.txt`** — scope grew into `supervisor.py` for the same reason Module 26's did; done, after 25 |
| 31–35 *(new, from 29's gap analysis)* | `src/pipeline/xagg.py` — **same file as 22/23/24; serialize inside that track** (each also adds one sub-query line to `meta_analysis.py`'s `_DECOMPOSITION_PLANS`) |
| 36 *(new, from 29)* | `src/pipeline/xagg.py` — same track as 31–35 |

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

# Module 30 — KB3/KB8/KB9 retrieval-completeness gap ✅ done

**Branch:** `fix/kb-retrieval-completeness-statutory-chunks`
**Brief:** `MODULE30_KB_RETRIEVAL_COMPLETENESS_PROMPT.md`
**Full result, with every captured measurement:**
`docs/gold-qa-wave2-results/MODULE30_RESULT.md`

**`prompts/evaluator.txt` was NOT changed.** Module 19b's fix stands.
**Chroma:** run entirely against a **private copy** at
`D:/Rapids AI/muhafiz-m30/data/chroma_db`; the shared store was never
touched. Nothing was re-embedded or re-indexed in the end.

## What was probed, and what each attempt returned

The brief listed three candidate directions and said none had been
investigated. All three were probed against the live corpus first.

| Probed | Result |
|---|---|
| **Query expansion naming the governing statute** | **This is the mechanism.** With `{"is_global": True}` scope, top-30 per query: the gold questions themselves put KB3's Article 18 chunks at rank 15/absent and KB8's and KB9's CrPC chunks nowhere at all; one English statute-vocabulary query put them at ranks 1, 1 and 6. |
| **Statute NAME only** ("Police Order 2002 Article 18") | **Rejected.** Returned *none* of the Article 18 chunks in the top 30. The corpus's chunks mostly do not repeat their own section number — the CrPC s.173 interim-report chunk contains the string "section 154" and never "173". The provision's own wording is what retrieves it. |
| **BM25 / statute-token boost** | **Rejected on evidence.** BM25 never returned any of the three target chunks for any query tried, before or after. There is no section-number token in the chunk text to boost. |
| **Raising `TOP_K_RETRIEVAL` / `CROSS_CASE_RETRIEVAL_MULTIPLIER`** | **Insufficient alone, kept as one of four.** With the pool widened but the dedupe still keeping first-seen scores, KB3's key chunk sat at pool rank 55 and was still cut. Applied scoped to KB intent only, never globally. |
| **Cross-script variant path** | **Confirmed as part of the cause, not a fix.** `cross_script_variant.py` sends a Latin-script query — English *or* Roman-Urdu — to **Urdu script**, never to English, and the whole legal corpus is English. |
| **Three statute hypotheses instead of two** | **Rejected, measured.** The third slot pushed the model onto a book that does not govern the question (the Anti-Rape Act for KB8 and KB9); those chunks won the rerank and displaced the correct CrPC s.173 and s.174 chunks. |

## The four causes, all of them load-bearing

1. **No generated query ever contains the statute's vocabulary.** Every
   variant paraphrases the *question*; the expander may not change
   language and the cross-script variant goes to Urdu. Fixed by
   `src/pipeline/statute_hypothesis.py` (+ `prompts/statute_hypothesis.txt`).
2. **The multi-variant dedupe kept the FIRST score, not the best.** KB3's
   "shall be investigated by the investigation staff" chunk was seen at
   0.87 by the question and 0.91 by the statute query; locked to 0.87 it
   sorted 54th and was cut.
3. **`cap_case_diversity` truncated the case-less KB corpus to five.**
   Every KB chunk buckets under `case_id=None`, so the per-case cap
   applied to the whole corpus at once: pools of 71–85 candidates cut to 5
   before RRF. Now skipped for a KB-only scope; a mixed pool is unchanged.
4. **The cross-encoder cut the right chunk even at RRF rank 1.** Scored
   against a Roman-Urdu question, every candidate came back inside
   0.0007–0.0022 — noise. `cross_rerank_multi()` scores against the
   statute phrasings too and keeps each chunk's best.

**A fifth cause, found only after those four:** KB3's Article 18 chunk then
came back at rank 1 on every attempt and the evaluator *still* returned
`relevant=False` three times — correctly, because the chunk ends
mid-sentence and the answering words are in the next one. This corpus is
chunked at ~350 characters and Article 18 spans five chunks.
`expand_with_neighbors()` widens each surviving chunk with its neighbours
for the KB scope only — retrieve narrow, read wide.

## Live result — before/after, both measured by this module

Baseline was **re-derived**, not inherited: the worktree was detached to the
branch's merge-base `6b86d9e` and the same eight questions re-sent.

| Q | Before (merge-base) | After |
|---|---|---|
| **KB3** | abstained — "No sufficiently relevant documents were found…" | answers, quoting Article 18's investigation-staff language |
| **KB8** | abstained | answers with **s.173**, **14 days**, **interim report within 3 days** — gold's statutory half exactly |
| **KB9** | abstained | answers with **s.174** and the inquest duty, via Punjab Police Rules 25.31's verbatim cross-reference (verified in the chunk text, not from the citation) |

**KB bucket: 3 of 8 answering at all → 7 of 8.** On gold match rather than
abstention: KB8 is a clear pass, KB9 a substantive pass by a different
citation route, KB3 improved but still not gold's conclusion.

## Regression guard — KB1, KB2, KB4, KB5, KB6

- **KB1** — no change (CrPC ss.154/155 before and after).
- **KB2** — improved: the verifier refusal is gone. Still not gold (names
  no statute).
- **KB4** — changed for the worse, and **neither** version matches gold.
  Baseline reached register material; after the change the answer is
  dominated by the Forensics guidelines, because `cross_rerank_multi()`
  merges by max score and cross-encoder scores are not comparable across
  queries. Reported, not tuned away — see Module 38 below.
- **KB5** — no change (verifier refusal before and after).
- **KB6** — no meaningful change (forensic handling both times).

No question that answered at baseline stopped answering.

**Correction to this plan's own numbers:** Module 19b is recorded above as
taking the KB bucket to 5/8. That does **not** reproduce on this machine
today — at the merge-base, with the evaluator fix present, KB2 and KB5 both
fail with the verifier's "could not be verified as grounded", before any
change from this module. The 5/8 figure should be re-derived before reuse.

## Non-gold paraphrase

A Roman-Urdu case-diary question ("Tafteesh ke doran police ko roz-ba-roz
kya likhna zaroori hota hai…"), which names no statute, went from the
verifier's "could not be verified as grounded" at the merge-base to a
correct answer citing **CrPC s.172** and **Police Rule 25.53**, grounded in
chunk `…_f9908363_c748` (verified by id). Two further paraphrases on the
24-hour production rule (Roman-Urdu and Urdu script) pass **both** before
and after — a useful negative control, but honestly not evidence of new
capability.


---

# Module 37 — `chunk_fulltext` holds an orphaned re-ingestion ⬜ new, not yet branched

**Found while verifying Module 30.**

BM25's candidate pool returns chunks whose ids **do not exist in Chroma**:

```
1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_f9908363 | 2577   <- current, in Chroma
1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_0519abd8 | 2243   <- orphan, not in Chroma
```

2,243 rows in Postgres `chunk_fulltext` from an earlier ingestion of the
CrPC PDF that was replaced in Chroma but never deleted from the full-text
index. `ChromaVectorStore.get_by_ids()` returns nothing for them. They
carry real statute text, so answers are not wrong, but they duplicate the
whole CrPC in BM25's pool, compete for RRF slots against the live copy, and
resolve to nothing for provenance lookups. Several reached the evaluator in
Module 30's live runs.

**Likely fix:** call `fulltext_index.delete_by_source()` alongside the
Chroma delete on re-ingest (the function exists), plus a one-off cleanup of
the orphaned rows. Add a consistency check — `chunk_fulltext` row count per
source vs. Chroma's — to the health/admin surface so this cannot recur
silently.

---

# Module 38 — `cross_rerank_multi()` merged by max score across queries ✅ fixed

**Branch:** `fix/cross-rerank-rrf-fusion`
**Result:** `docs/gold-qa-wave2-results/MODULE38_RESULT.md`
**Found while verifying Module 30.**

`cross_rerank_multi()` (added by Module 30) scored candidates against several
query phrasings and kept each candidate's **best**. Cross-encoder scores are
not comparable across queries, so whichever phrasing happened to produce the
largest absolute scores took the whole final window.

**Confirmed, with the cleanest numbers this wave has produced.** One live KB4
candidate pool of 32, the cross-encoder called once per phrasing, both merges
computed from that one set of lists so nothing but the merge differs:

| phrasing | score range over the same 32 candidates |
|---|---|
| the Urdu-script question | 0.00002 – 0.16390 |
| "Punjab Police Rules … case property and malkhana" (**right book**) | 0.00003 – 0.34222 |
| "Forensics guidelines … chain of custody" (**wrong book**) | 0.00022 – **0.96709** |

The wrong hypothesis's *worst* candidate scores about as high as the right
one's *best* — and yet that same forensics query ranks the register chunks
11th–24th while the other two rank them 1st–6th. Only the scale was wrong.

**Fixed** by fusing the per-query reranked lists by reciprocal rank, calling
`src/retrieval/reranker.py`'s existing `reciprocal_rank_fusion()` rather than
growing a second implementation. It gained two keyword-only parameters, both
defaulting to prior behaviour: `score_key` (so a second fusion pass does not
overwrite the `rrf_score` the pool was built with) and `apply_year_boost` (the
recency prior belongs to the fusion that builds the pool). Each phrasing also
keeps a capped, appended voice for its own rank-1 chunk — same shape as the
existing semantic floor; measured on KB8/KB9, the chunk it saves is the
*original question's* rank 1.

A per-list `weights` multiplier was built and then **removed**: swept over
0.25/0.5/1.0/2.0/3.0 on live KB1/KB4/KB8/KB9 pools, the whole 0.5–3.0 range is
flat, and 0.25 was the only value that made KB4 worse.

## Live result — before/after, both re-derived this session

Baseline re-derived by reverting the three retrieval files to `origin/main` and
restarting the same backend against the same private Chroma.

| Q | Before (`main`) | After |
|---|---|---|
| **KB4** | "answers" — *the corpus does not contain this*; **0 Punjab Police Rules chunks in 3 of 3 runs** | **rule 27.16 and the three-year rule in the window on 3 of 3 runs**; 1 of 3 runs clears the verifier and answers with Register No. 1 / rule 27.16(1) / s.512 |
| **KB6** | abstains — 6 evaluator attempts, 290s | **answers** — 1 attempt, 93s |
| **KB9** | abstains — 6 attempts | **answers** — s.174, Rule 25.31, s.174(3) post-mortem |
| KB1, KB3, KB5, KB8 | answer | answer, same substance |
| KB2 | done / error / done over 3 runs | error / done / done over 3 runs — **variance, not a regression** |

**KB bucket unchanged at 6 of 8 answering**, with KB6 and KB9 gained and KB4
traded from a confident miss to a correct retrieval the verifier rejects 2 of 3
times. **Reported, not tuned** — see the result file's §5.

**Correction to this plan's own numbers:** the 7-of-8 figure recorded for
Module 30 does not reproduce on this machine today. At `origin/main`, before
any change from this module, the bucket measures **6 of 8** (KB6 and KB9 both
abstain). And the verdict is not reproducible from one run: of four
(question, arm) pairs repeated three times, three gave a mixed verdict.

## New defects found

- **The third statute hypothesis is no longer harmful** (`--n-hyp 3`, live).
  Module 30 rejected `DEFAULT_HYPOTHESES = 3` *because* the wrong book's chunks
  "won the cross-encoder rerank" — that mechanism no longer exists. Raising it
  now looks safe and possibly useful, but is unmeasured on answer quality, so
  it needs its own module. `src/pipeline/statute_hypothesis.py` is untouched
  here and its `n=2` comment now cites a dead rationale.
- **`_is_legal_kb_intent()` misses a plain "which register / how long until
  disposal" question.** A Roman-Urdu paraphrase of KB4 that omits the word
  *qanoon* never narrows to the KB corpus, generates no statute hypotheses, and
  abstains against the case-narrative pool — while the governing rule sits in
  the KB corpus. Its own comment predicts this: the patterns were mined from
  the gold questions' literal text.
- **Module 37's orphaned `_0519abd8_` CrPC chunks still reach the evaluator**
  on KB2/KB3/KB8/KB9 in both arms — fresh confirmation, already tracked.

---

# Module 39 — the KB questions' "and does our data show it?" half ✅ done

**Branch:** `feat/kb-data-half-composition` · **Result:**
`docs/gold-qa-wave2-results/MODULE39_RESULT.md`

**Found while verifying Module 30; re-baselined by Module 52**, which measured
**0 of 48 live runs** producing gold's data half in either arm and called it
the largest remaining cause of KB failure.

**Diagnosis confirmed, and it is a composition problem, not a retrieval one.**
The RAG sub-agent searches seven English statute PDFs; the figure gold asks for
is in none of them. `rag_tool()` now dispatches one canned XAGG sub-query
CONCURRENTLY with retrieval for a compound legal-KB question, and folds the
aggregate's deterministic rendering in as one extra citable chunk — appended
last, so every statutory chunk keeps the `[Document N]` position
`verify_grounding()` checks positionally. The shape is copied from Module 29's
`_DECOMPOSITION_PLANS` (a question SHAPE → a canned sub-query, no LLM call),
but it lives in `rag.py` because Meta-Analysis is only reachable on the XAGG
route and a KB question routes to RAG; moving it would need `router.py` and
would cost the statutory half Modules 30/52 just fixed.

**Which figures were reachable — probed by hand before any code was written:**

| Q | Gold | Derived independently | Aggregate |
|---|---|---|---|
| KB3 | 68 of 74 officer pairs (92%) | **68/74 = 91.9%** ✅ exact | **none** → Module 67 |
| KB4 | 45 property entries | **45** ✅ exact | `seized_property_disposition` |
| KB5 | 8 women-violence reports | **8** ✅ exact | `dv_report_fir_match` |
| KB6 | 32 weapons | **32** ✅ exact | `weapon_compliance_scan` |
| KB8 | 26 challans to court | **26** ✅ exact | **none** → Module 68 |
| KB9 | 8 FIRs cite PPC 302 | **10** ❌ diverges | only the generic `statute_court_stage_join` → Module 69 |

So four of the six were wired here and two became their own modules rather
than scope creep, since building them means editing `xagg.py`. KB1 and KB2
were deliberately left unwired too: their data halves are schema claims, not
counts, and the nearest aggregate reports the opposite shape.

**The substantive finding, measured rather than argued.** The first wiring
added the aggregate to the tool's RESULT, after `evaluate_relevance()`. KB4,
KB5 and KB6 answered with both halves and **KB9 abstained 3 of 3** — with the
gate's own reason, on all 18 of its refusals, that the retrieved documents
describe inquest procedure and do not say whether OUR system records one. That
is a *correct* verdict on a compound question judged against statute-only
evidence, and the chunk answering the other half was 0.6 s away on the far side
of the gate. Folding it in BEFORE the gate (the same reasoning
`orchestrator.py`'s `_case_record_chunk()` already uses) took KB9 to 2/3 and
KB4 to 3/3, which also un-does the KB4 regression Module 52 recorded in its own
§5.2.

**Live, all 8 KB questions × 3 runs × 2 arms, plus 6 paraphrase and 8
regression runs (54 runs, 0 quota lines):**

| | dispatched/returned | answered | norm half | **data half** | figure |
|---|---|---|---|---|---|
| KB4 | 3/3 | 3/3 | 3/3 (27.16 named 2/3) | **3/3** | 45 = gold ✅ |
| KB5 | 3/3 | 1/3 | 1/3 | **1/3** | 8 = gold ✅ |
| KB6 | 3/3 | 2/3 | 2/3 | **2/3** | 32 = gold ✅ |
| KB9 | 3/3 | 2/3 | 2/3 | **2/3** ⚠ | present, not gold's number |
| KB1/2/3/8 | 0/0 by design | 3/3 · 3/3 · 2/3 · 3/3 | 3/3 · 0/3 · 1/3 · 3/3 | 0/3 each | — |

**Cost, measured because Module 53 asked for it:** the aggregate's own wall
time is **0.59 s mean / 0.88 s max** over 16 dispatches, and the four compound
questions average **256.1 s** against **254.5 s** for the four that dispatch
nothing — **+1.6 s, +0.6 %**. Unlike Module 53's shared sub-query deadline this
has its own private one, is memoised across scope retries and evaluator rounds,
and degrades to the byte-for-byte pre-Module-39 answer on a timeout, a denial,
an exception, an empty rendering or a drifted aggregate family. **0 of 30
dispatches dropped.**

**Negative control.** The all-32 EQUALITY control resolves exactly
`{KB4, KB5, KB6, KB9}` and `None` for the other 28. CR8 is the proof the gate
has to be an AND: it matches the violence-against-women plan on subject alone,
fails `_is_legal_kb_intent()`, and already reaches the same aggregate on its
own route. Live, all eight regression questions (G2, G5, G3, CR7, M2, CR3, G1,
G6) route to **XAGG** and never enter `rag.py` at all; each answered as before
except G1, which hit the pre-existing Meta-Analysis synthesis-verifier
rejection (Modules 53/57/61), unreachable from this code.

**Non-gold paraphrases** (written before the sweeps, not adjusted): a
Roman-Urdu rephrasing of KB4 lands its data half **3/3**, and an English
rephrasing of KB6 **2/3** — and, incidentally, retrieves gold's own
*"unloaded … safety on … no live rounds"* window on 2 of 3 runs, which KB6's
gold wording does not (corroborating Module 64).

**Gold challenged — KB9.** Ten distinct FIRs carry a `fir_section` row with act
`PPC` and section `302`, not gold's 8; verified twice by different routes, with
no `302`-variant section code in the corpus. Nothing here is tuned toward 8.

**Not fixed, filed:** Modules **67** (KB3's officer-pair aggregate), **68**
(KB8's challan count), **69** (a per-section FIR count for KB9). Module **61**
widens — three of twelve compound runs died on the verifier refusing a negative
inference over a complete listing ("our register has no field for X"), the same
shape as CR3's, which compound KB answers make routine.

---

# Modules 74, 75 and 76 — the three missing KB data-half aggregates ✅ done

**Branch:** `feat/xagg-kb-data-half-aggregates` · **Results:**
`MODULE74_RESULT.md`, `MODULE75_RESULT.md`, `MODULE76_RESULT.md`
**Filed by:** Module 39, which derived all three figures and then declined to
build them because `xagg.py` was not its file.

All three landed in one branch, serialized, one commit per module. Every
figure was **re-probed by hand-written Cypher before any aggregate was
written** rather than inherited from Module 39's row — which is how the one
correction below was found.

| Q | gold | Module 39 | **re-derived here** | verdict |
|---|---|---|---|---|
| KB3 | 68 of 74 pairs (92 %) | 68/74 = 91.9 % | **68 of 74 = 91.9 %**, 6 split | ✅ exact |
| KB8 | 26 challans to court | 26 rows / 26 cases | **26 rows / 26 distinct cases** | ✅ exact |
| KB9 | 8 FIRs cite PPC 302 | 10 | **10** | ❌ **gold diverges by 25 %** |

**KB8's cause is confirmed and is worse than the row said.** Three record
types, three quantities: `criminal_record` **33**, `chalaan_dispatch` **26**,
`chalaan_outcome` **20**. `grep -c chalaan_dispatch src/pipeline/xagg.py` was
**0** — the file had a challan reader, it read the wrong one of the two
challan types, and KB8 routed to a family that read neither.

**KB9's 10-vs-8 stands, third derivation.** Both inflation routes were
checked and excluded: exactly 10 `fir_section` rows with `section_code='302'`
across exactly 10 distinct case_ids (no double counting), and `302` is the
only section code in the corpus containing the substring "302". 25 % is
outside the brief's tolerance. **The recommendation is to correct gold to
10.** Nothing was tuned toward 8.

**One correction to Module 39's own breakdown**, recorded rather than edited
into that module's file: its three "genuine role splits" for KB3 include
`fir-205-26`, which actually carries **two** investigating edges — a
superseded placeholder ASI who *is* the recording officer, plus the real
successor who is not. Headline figures are unaffected; the grain is not. Gold
says "68 of 74 **pairs**", 74 is the edge count and 73 is the case count, so
the aggregate counts at the assignment grain. A per-case count gives 68/5 —
true, and not gold.

**What did NOT change, and is the safety story.** The all-32 EQUALITY control
moves **exactly one** question across all three modules (KB3, to its own new
family); KB8's and KB9's gold TEXTS reach nothing new, because neither says
"challan" or names a section — those words are in their gold ANSWERS, and both
questions reach their family through a canned sub-query exactly as KB4/KB5/KB6
do. `unsupported_officer` still answers every officer-IDENTITY question.
Module 39's shipped KB9 sub-query still resolves to `statute_court_stage_join`
— pinned as a test, because `_run_kb_data_half()`'s runtime family check would
otherwise have turned KB9's measured 2/3 into 0/3 with every unit test green.

**What is still not live, and why it is not `xagg.py`'s to fix.** None of
KB3/KB8/KB9 shows its data half in an answer yet. Two independent blockers,
both filed: the landing step is a one-entry addition to
`rag.py::_KB_DATA_HALF_PLANS` (**Module 77**; `rag.py` belongs to the live
Module 65 track), and KB3 and KB9 **do not reach the RAG path at all** on this
machine today — KB3 is XNETWORK 3/3, KB9 is XAGG → `graph_recurrence_person`
3/3 (**Module 78**). KB8 does reach RAG 3/3, so Module 77's entry is all it
needs. New defects also split out: **79** (a KB question needing two
aggregates), **80** (per-section FIR counts route to SQL), **81** (the
canonical quota grep matches timestamp milliseconds).

> **Module 77 has since landed all three, and settled this paragraph.** KB8's
> entry was indeed all it needed — `route=RAG` 3/3, plan fired 3/3, **26 in
> the answer**. KB3's and KB9's entries are correct and compose in process,
> and were consulted **zero** times live, exactly as predicted. What Module 77
> added on top is the control this paragraph could not have: an ordinary
> **English paraphrase** of KB3 and of KB9 **does** route to RAG and **does**
> fire its plan, 2/2 each, P-KB3 answering *"68 out of 74 … (92%)"*. So
> Module 78 is not "the router is unstable across days" — it is the router
> discriminating on these two specific gold wordings, with a working
> counter-example to bisect against. See `MODULE77_RESULT.md`.


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

# Module 28 — CR4: weapon-evidence chain routed to cross-case linkage instead of XAGG ✅ fixed

**Branch:** `fix/router-weapon-evidence-chain-to-xagg` — **merged (PR #19).**
**Split out of:** Module 21 (see its writeup above for the original live
evidence — CR2 control, `_recover_target_entity()` read, distance capture).
**Full results:** `docs/gold-qa-wave2-results/MODULE28_RESULT.md`.
**Primary files:** `src/pipeline/router.py` (routing) and
`src/pipeline/xagg.py` (a new aggregate — one did NOT already exist; see
below), in two separate commits.

**Root cause — confirmed by this module's own live capture, not inherited.**
Re-run of CR4's exact gold text on `main` @ `dbd308f` before changing
anything reproduced Module 21's finding number for number:

```
[step=supervisor:dispatch] Classified query as route='XGRAPH' -> sub-agent='Cross-Case Linkage'
```
> "…nearest cluster found was distance **0.184** against a relevance cutoff
> of **0.145**. Rather than describing an unrelated cluster, no cross-case
> network finding is being reported here."

CR4 names no entity, so it classified to XGRAPH; Cross-Case Linkage's
`_recover_target_entity()` correctly returned `None`; with no seed the tool
ran its recurring-entity-across-cases traversal — the wrong shape for a
question asking for ONE example chain — and the relevance gate then correctly
refused. **The gate was right, the route was wrong.** `xnetwork.py` and
`RELEVANCE_DISTANCE_THRESHOLD` are untouched by this module.

**Did the aggregate already exist? No.** The brief allowed for it. `xagg.py`'s
weapon family is `_weapon_compliance_scan()` (G5),
`_weapon_recovery_rate_by_district()` (CP1) and `_top_recurring_weapon_types()`
— none can name who a weapon was taken off. So this module is router **plus**
one new aggregate, `_weapon_evidence_chain()`, kept in its own commit as the
brief requires. It reads links that were **already in the graph** — nothing is
re-ingested or re-projected: `Weapon-[:BELONGS_TO_CASE]->Case`,
`Person-[:OWNS]->Weapon` (written by `structured_projection._write_weapons()`
from `weapon_register.recovered_from`),
`Person-[:INVOLVED_IN {role:'accused', arrest_status}]->Incident`, plus the
criminal-record system's `conviction_status` joined on the FIR number via the
existing `_fir_key()`.

**The routing rule and the exact discriminator.**
`_WEAPON_EVIDENCE_CHAIN_XAGG_PATTERNS` in `router.py`, appended to
`_XAGG_OVERRIDE_PATTERNS`. Every pattern — and the `xagg.py` dispatch that
follows it — is a **CONJUNCTION of two term families**, the house technique
G5's own entry already uses:

- a **weapon** term: `weapon|firearm|pistol|gun|hathiyar|aslaha|ہتھیار|اسلحہ`
- AND an **attribution** term: `taken off` / `taken from` / `recovered from` /
  `seized from` / `trace … back` / `whose` / `کس سے برآمد` / `کس کے قبضے` /
  `kis se baramad`.

The attribution half is what separates CR4 from **CR2**: CR2 ("is there anyone
with an earlier case … who has since resurfaced as a suspect?") contains no
weapon vocabulary at all, so no conjunction can reach it. A *bare* weapon
keyword — the mistake Module 21 warned against — would additionally have
collided with **G5, CP1, M5 and KB6**, four gold questions that already work;
none of them asks *whose* weapon it was.

No `supervisor.py` counterpart was needed (unlike Module 26's M1 fix): CR4 and
its paraphrase match none of `_META_ANALYSIS_TRIGGER_PATTERNS`, and the live
stream shows exactly one route event — no decomposition.

**All-32 negative control — verbatim result.** Two forms, both automated.

(a) Pattern-family match across the 32 gold questions:

```
--- router weapon-chain pattern family matches ---
 MATCH CR4 [0]
--- xagg dispatch conjunction matches ---
 XAGG-CHAIN CR4
--- xagg G5 compliance conjunction (must be unchanged) ---
 G5-PATH G5
paraphrase router match: True
paraphrase xagg match: True
```

CR4 and only CR4. Unlike Module 26's M1/M5 pair there is no legitimate
co-match to allow, so the test asserts exact equality with `["CR4"]`.

(b) The stronger whole-route form the brief asks for — the check that would
have caught the M4/G3 collision of PR #8.
`test_module28_changes_no_other_gold_question_route` computes
`_deterministic_route_override()` for all 32 gold questions with the new
pattern family in place and again with it removed, and asserts the diff is:

```
changed == {"CR4": (None, "XAGG")}
```

CR4 is the **only** gold question whose deterministic route changes, and it
changes from "no deterministic override at all" to XAGG. CR2 and G5 are
asserted by name as well.

**The live chain captured (CR4, `/api/chat`, platform-admin, All Cases,
2026-09-08).** One route event —
`route='XAGG' -> sub-agent='Large-Scale Aggregate'`, `status=ok`, 12.5 s:

> Yes, for 30 of the 32 weapons logged as evidence, the register records who
> the weapon was recovered from … A **30 بور پستول, بغیر لائسنس** logged in
> **FIR 891/24** was recovered from **شہزیب عرف شابی**, who was recorded as
> **گرفتار، بعد ازاں سزا یافتہ** ("arrested, later convicted"). The
> criminal-record system also notes "Convicted, on bail pending appeal" for
> this person in the same FIR. … **2 of the 32 weapons** have no person linked
> to them—they were recovered from crime scenes … **Limitation**: The weapon →
> person link is not a database-enforced key. The **recovered_from** field
> contains only a name, which is matched to the accused named in the **same
> FIR** (i.e., weapon → FIR number → accused).

Every element of gold's answer matches, **including gold's own hedge**, which
the renderer states rather than hides (brief §3). CR4 was 0.0 before.

**Non-gold paraphrase** — *"For weapons we've seized as evidence, can we trace
them back to whoever they were taken from?"*: before the change it was
`route='XGRAPH' -> Cross-Case Linkage` with the same refusal (nearest cluster
**0.178** vs. 0.145); after, one route event `route='XAGG' -> Large-Scale
Aggregate`, `status=done`, 11.5 s, returning the same real FIR 891/24 chain.
**Passes** — a capability fix, not curve-fitting.

**Regression results, both re-run live before AND after on the same backend
and data:**

- **CR2** — unchanged. Before and after: one route event,
  `route='XAGG' -> 'Large-Scale Aggregate'`, and the identical answer —
  4 persons: فیصل, طارق (fir-202-26 / fir-401-26), شہزیب عرف شابی
  (fir-214-26 / fir-891-24), عاصم رشید (fir-64-26 / fir-65-26). The collision
  Module 21 warned about did not happen.
- **G5** — unchanged, word for word. Before and after: **two** route events
  (`XAGG -> Meta-Analysis`, then `XAGG -> Large-Scale Aggregate`) and the same
  answer, 30 of 32 (94%) unlicensed plus 2 with no licence status. G5 is also
  the live instance of the documented parsing trap: reading only the last
  route event would have hidden that Meta-Analysis handled it first.

**Unit:** `tests/test_router.py` 118 passed; `tests/test_xagg.py` 90 passed,
1 skipped (pre-existing). Combined with the supervisor / harness-xagg /
orchestrator / large-scale-aggregate / cross-case-linkage / xnetwork suites:
**501 passed, 1 skipped, 1 xpassed**, no new failures. The full `pytest -q`
suite was deliberately not run (it empties the real
`muhafiz_entity_descriptions` collection). New tests include a regression
pinned to CR4's literal gold **answer** (FIR 891/24 / شہزیب عرف شابی /
گرفتار، بعد ازاں سزا یافتہ / the FIR-matching caveat text), six paraphrases in
three languages, and both negative controls above. One existing test's sample
query was swapped: `test_no_case_id_prefixes_the_llm_call_with_active_case_none`
used CR4's own text with an LLM stub returning XGRAPH — itself a record of the
misroute — and is now intercepted before the LLM call; its assertions are
unchanged.

**Left unfixed, deliberately:** G5's trailing citation-verifier footnote
("a cited claim could not be confirmed against its source") is byte-identical
before and after this change, so it is pre-existing and belongs to Module 25's
verifier family, not here. `_top_recurring_weapon_types()` is structurally
dead on this data (weapon `entity_id`s are FIR-scoped, so no weapon spans
cases — its own docstring records this and the live probe confirms all 32
weapons belong to exactly one case each); `xagg.py`'s weapon family is Module
23's for this wave, so it is left to that track.

---

# Module 29 — Meta-Analysis decomposer doesn't split broad synthesis into XAGG-shaped sub-questions ✅

**Branch:** `fix/meta-analysis-decompose-broad-synthesis` — **merged (PR #23).**
**Split out of:** Module 21 (CR3, G1, G6).
**Full results, every number traced to a live capture:**
`docs/gold-qa-wave2-results/MODULE29_RESULT.md`.
**Files:** `src/pipeline/harness/agents/meta_analysis.py`,
`src/pipeline/harness/supervisor.py`,
`prompts/meta_analysis_decomposer.txt`.

**The brief's framing was right, and is now confirmed first-hand.** Calling
`_decompose()` directly against each question's literal gold text on this
branch's base commit (`main` @ `c2a6ac6`) returned `decompose=False` for
CR3, G1 and G6, and `decompose=True` (2 sub-queries) for M2 — so
Meta-Analysis re-dispatched the original, un-split question, which landed
back on XNETWORK/Cross-Case Linkage and was correctly refused there.
**`xnetwork.py` and `RELEVANCE_DISTANCE_THRESHOLD` were not touched.**

**Two things the brief did not anticipate, both measured:**

1. Decomposing alone would not have been enough. Nine naturally-phrased
   candidate sub-questions were run through `route_query()` +
   `classify_to_subagent()`: **6 of 9 landed on `XGRAPH → Cross-Case
   Linkage`**, the same dead end. Sub-query *phrasing* is load-bearing.
2. **G1's own paraphrase never reached this module at all** — *"As the
   on-duty analyst, look over everything currently open and tell me what's
   worth a second look"* matched no `_META_ANALYSIS_TRIGGER_PATTERNS` entry,
   so no `meta_analysis.py` change could have affected it. That list was
   curve-fit to the gold strings, the same gap Module 22 caught in its own
   reporting-speed keywords. Scope therefore grew into `supervisor.py`, as
   Module 26's did.

**The fix.** `_DECOMPOSITION_PLANS` in `meta_analysis.py`: three
deterministic question-SHAPE families, checked before the decomposer LLM
call and skipping it entirely on a match.

| Plan | Sub-queries it dispatches | Each routes to |
|---|---|---|
| `record_consistency` (CR3) | person recurrence; CMS↔FIR linkage | XAGG ×2 |
| `orientation_note` (G6) | district spread; case mix by year; reporting speed; weapon licence; gender | XAGG ×5 |
| `caseload_review` (G1) | completeness; person recurrence; weapon licence; case mix by year; criminal-record vs court | XAGG ×5 |

Deterministic rather than prompt-only because the brief requires tests
pinned to the *literal* decomposition across model drift, because every
sub-query is re-routed by another LLM call unless a deterministic router
override catches it first, and because `xagg.py` is itself a family of
canned aggregates selected by keyword — a canned plan per question shape is
the same paradigm one layer up. **Every planned sub-query is worded to
match `router.py::_deterministic_route_override()`** (verified live: all 9
return `det=Y route=XAGG`), so decomposition adds no extra router LLM call.
The decomposer prompt is extended as well, for paraphrases outside these
families, including the correction that the phrasing it previously
recommended ("What is the breakdown of…") is **not** one the deterministic
router recognizes — "How many cases … across all cases" is.

Two synthesis-prompt rules were added after the sub-answers, both
live-caught rejecting otherwise-correct decomposed answers: cite
`[Document N]` (CR3 was rejected `off_topic=True` for citing nothing), and
never state a number that appears in no sub-answer (G6 was rejected for
summing the per-district counts into a 73 no sub-answer states). Same
Module 25 verifier-interaction family; M2 was re-run against the new
wording.

**Live results** (`/api/chat`, platform-admin, All Cases, whole SSE stream
parsed — Meta-Analysis's second route event per sub-query is the evidence):

| Q | Before | After |
|---|---|---|
| **CR3** | 1 dispatch, XNETWORK → Cross-Case Linkage, refused (0.2) | **2** XAGG dispatches, `status=done`, *"No, not identically … fir-64-26 has a matching walk-in complaint … fir-65-26 does not appear in the linkage list"* — gold's verdict and both FIRs, but the **wrong case tag** (`CMS-KHI-2026-0417`, not `CMS-ISB-2026-0341`) |
| **G1** | 1 dispatch, XNETWORK, refused (0.0) | **5** XAGG dispatches, `status=done`, a real analytical answer from computed facts |
| **G6** | 1 dispatch, XNETWORK, refused (0.0) | **5** XAGG dispatches, `status=done`, an orientation note matching **6 of gold's 9 elements** |

**Honest mismatches, reported not tuned.** G1's live answer is a legitimate
"flag anything unusual" answer built entirely from computed facts, but it
shares almost none of gold's *specific* findings — because **all four** of
gold's headline facts need aggregates that do not exist (Modules 31–34), and
gold's "all Pakistani nationals" is not in the data model at all. CR3 is the
least stable of the three: across this module's runs it produced one
refusal, one correct verdict with the wrong companion FIR, one run where two
sub-queries hit the 60 s `META_ANALYSIS_SUBQUERY_TIMEOUT`, and the good run
above. The cause is not the decomposition — nothing in the sub-answers says
*which* FIRs "the online banking fraud matter" refers to, so the synthesis
has to infer the pair. That is Module 36.

**Gap analysis, as the brief required — published in full in the result
file.** Nine of the needed primitives already existed and are used by the
plans above (`_cms_fir_linkage`, `_statute_mix_by_year`,
`_incident_to_report_minutes_by_year`, `_weapon_compliance_scan`,
`_top_districts_by`, `_gender_breakdown`, `_case_completeness_scan`,
`_top_recurring_nodes("Person")`, `_criminal_record_court_crosscheck`). Six
were missing and became Modules 31–36 below rather than being built here.
For four of the six the **underlying graph data is already present** — the
gap is purely the aggregate — and dispatching their sub-questions today
would be actively harmful, because `run_aggregate()`'s keyword chain falls
through to the unrelated person-recurrence family and answers confidently
and wrongly with no caveat.

**Follow-up created by the merge, recorded not acted on.** Module 24's
`_statute_court_stage_join()` (33 / 1 / 30 under trial) landed while this
module was in live verification, and it answers gold G6's "most matters are
still pending in court" element that this module scores as a miss. It is a
natural sixth sub-query for the `orientation_note` plan, but
`_MAX_SUB_QUERIES` is 5 and that plan is full — the same cap decision
Modules 31-34 need.

**Negative control (the "too broad" half of the risk).** Across all 32 gold
questions, exactly `{CR3: record_consistency, G1: caseload_review,
G6: orientation_note}` match a plan — asserted as an equality, not a
containment. The `supervisor.py` widening captures the **same 15** gold
questions it did before. **G5** deserves separate mention: it already
reaches Meta-Analysis via its own `flag karne layak` trigger and already
answers correctly through the `decompose: false` fallback, so no pattern
here uses a bare "flag" or "briefing"; re-run live, it is unchanged (30 of
32 unlicensed, 2 no status).

**Regression guard.** **M2** (Module 25's own question, same file): 85.5 s,
`status=done`, still LLM-decomposed into 2 XAGG sub-queries, **no verifier
rejection** — exactly the "honest no-classification-data shape" Module 25
recorded as its own post-fix result. **G5**: unchanged. **D1**: one
dispatch, not decomposed, 73. **Paraphrases:** G1's passes (5 XAGG
dispatches, real answer — it previously never reached the module); G6's
passes; CR3's decomposes correctly but its synthesis was rejected, matching
CR3's own instability.

**Unit:** `tests/test_harness_agent_meta_analysis.py` 44 passed (28 before).
Combined with the supervisor / router / xagg / xnetwork / cross-case-linkage
/ large-scale-aggregate / harness-xnetwork / verifier / orchestrator suites:
**611 passed, 1 skipped, 1 xpassed**, no new failures. New tests pin each of
CR3/G1/G6's literal sub-query tuple, prove the decomposer LLM is not called
on a plan match, assert every planned sub-query hits the deterministic XAGG
router override, and carry both negative controls above. Module 25's own
three tests still pass. The full `pytest -q` suite was deliberately not run
(it empties `muhafiz_entity_descriptions`).

**No quota limit was hit** in any live run — no 429s in any backend log. The
one failure that looked like a decomposition bug was a sub-query timeout
caused by an experimental sub-query that rendered all 73 cases (~4.6 KB) and
starved its two concurrent siblings; it was reverted and the reason is
recorded in `_SQ_CASE_LISTING`'s own comment.

---

# Module 31 — G1: offender age profile ✅

**Branch:** `feature/xagg-g1-caseload-profile-aggregates` (Modules 31–34,
one commit each). **File:** `src/pipeline/xagg.py` + the harness wrapper and
the two orchestrator rendering sites. **Full writeup:**
`docs/gold-qa-wave2-results/MODULE31_RESULT.md`, which also carries the
**shared** live G1 run and the shared new-defect list for Modules 31–34.

**The hypothesis was right.** `_UNSUPPORTED_AGE` ("accused/witness age is
not currently extracted into this system's data model") was false and had
been since Module 1d. Probed live before writing anything: 19 `Person` nodes
carry an age, 19 of 94 accused edges reach one, **17 of 92 distinct accused**,
range **24–49**, mean **31.5** accused-scoped (31.8 corpus-wide — the plan
quoted the corpus-wide figure; both round to gold's "~31").

**Built:** `_offender_age_profile()` — range, mean, and BOTH denominators
(distinct accused and accused entries, because A1/G3's gold answers use the
latter). `_UNSUPPORTED_AGE` kept but demoted to the data-driven
empty-corpus fallback. `_AGE_KEYWORDS` keeps its first-in-the-chain
precedence; the all-32 negative control asserts it matches **no** gold
question directly (equality, not containment).

**Nationality: not fixed, stated.** Gold's "all Pakistani nationals" is not
derivable — no nationality field exists anywhere in the data model. The
answer says so outright rather than letting it be inferred from Urdu names.

**Live** (`route='XAGG'`, 23.4 s, `status=done`, proved by
`XAGG offender_age_profile:` in `backend.log`): *"92 distinct accused … 17
have recorded ages … 24 to 49 … mean 31.5 … 75 of the 92 accused have no age
recorded … Nationality data is entirely absent."*

**Gold comparison: 2 of 4.** Range and mean match exactly. Gold's "every
accused … none younger or older" is an overclaim on 18% coverage and the
answer says so; nationality is not derivable. Reported, not tuned.

---

# Module 32 — G1: accused ↔ complainant relationship breakdown ✅

**Full writeup:** `docs/gold-qa-wave2-results/MODULE32_RESULT.md`.

**The hypothesis was right, including that the silent fall-through is as
much the defect as the missing aggregate.** Measured pre-fix, in process:
the relationship sub-question returned `graph_recurrence`/Person — a ranked
list of repeat offenders — because "accused" is in `_PERSON_KEYWORDS`.

**Probed live:** 24 `RELATED_TO` edges across **10 FIRs** — اجنبی 15,
محلے دار 2, ساس 2, سینئر ساتھی کار 2, شوہر 2, بھائی 1 — and only **12 of 92**
distinct accused carry any relationship. Reproduces the plan exactly.

**Built:** `_accused_relationship_breakdown()`, ranked with a per-value FIR
count, the dominant value named, an English gloss (display-only; unmapped
values pass through verbatim), and the coverage caveat. Two counting
decisions published rather than hidden: the raw edge count is the headline
(gold's denominator) with `distinct_pair_count` (**22**) alongside it, and
jurisdiction scoping uses the edge's own `source_doc_id` — walking to `Case`
turns 24 edges into 27 rows.

**Ordering:** below G3/G2 (G3 reads this same data as a completeness gap and
scores 1.0 — asserted end to end), decisively above `_PERSON_KEYWORDS`.
The bare Urdu "تعلق" is excluded: it is a substring of "متعلق" in KB4.

**Live** (`route='XAGG'`, 22.1 s, `status=done`): اجنبی (stranger) 15 of 24
across 10 FIRs, with the 12-of-92 coverage caveat. No recurrence ranking
anywhere in the answer.

**Gold comparison: 3 of 4**, with the fourth qualified rather than agreed
with — gold's "largely stranger-perpetrated crime" is an inference over the
12 accused who have a record, not the 92 who exist.

---

# Module 33 — G1: seized-property disposition counts ✅

**Full writeup:** `docs/gold-qa-wave2-results/MODULE33_RESULT.md`.

**The plan's stated root cause was wrong about the branch.** It predicted a
person-recurrence fall-through; the measured pre-fix result was
`kind="case_listing"` — the unfiltered **73-row corpus dump** — because the
sub-query ends "…, across all cases?" and that contains the literal
`_LIST_ALL_KEYWORDS` entry "all cases". Same class of defect, different
branch.

**Probed live:** 45 `malkhana_register` entries across **28 FIRs** —
ضبط شدہ 14 (13 FIRs), forensic-lab dispatch **13** (11 FIRs), ورثاء **7**
(7 FIRs), مدعی کے حوالے 6 (6 FIRs), plus 5 singletons. Gold's 13 and 7
reproduce exactly. Note `source_case_ref` is **empty on every malkhana row**,
so FIR attribution comes from the `BELONGS_TO_CASE` edge.

**Built:** `_seized_property_disposition()`, grouped verbatim by
disposition, with BOTH an item count and a FIR count per group (13 lab items
span 11 FIRs — conflating them is the easiest way to report a wrong number).
The 13-vs-16 question the plan raised is answered by a **published rule**:
16 entries mention a forensic process in some wording, only 13 record an
actual dispatch; the headline uses the literal reading — gold's — and the
answer says so.

**Ordering:** below G5's compliance scan and CR4's attribution chain
(adjacent subjects, G5 scores 1.0 — asserted end to end), above
`_LIST_ALL_KEYWORDS`. Matches "forensic lab"/"forensic laboratory", never a
bare "forensic" — KB6 says "forensics guidelines".

**Live** (`route='XAGG'`, 21.0 s, `status=done`): 45 items across 28 FIRs,
13 in 11 FIRs sent to a forensic laboratory, 7 in 7 FIRs held for heirs,
counting rule intact. **Gold comparison: both countable claims exact.**

---

# Module 34 — G1: incident time-of-day distribution ✅

**Full writeup:** `docs/gold-qa-wave2-results/MODULE34_RESULT.md`.

**The plan's branch prediction was right** (`case_listing`, same "all cases"
substring as Module 33) **and its midnight figure was wrong, in a way that
reverses one of gold's claims.**

The plan states 9 incidents record 00:00:00 exactly. The live count is
**14**; **9** is the number of Incidents carrying no datetime at all. And 14
of the 15 naive "night" incidents ARE those date-only rows — the only
genuine overnight incident in the corpus is one at 01:00.

**Built:** `_incident_time_of_day()`, excluding exact-midnight rows as
date-only and reporting them as `date_only_count`, while also returning
`naive_bucket_counts` so the difference is auditable. Only exact midnight is
excluded; a real 00:30 incident stays in the night band.

| Band | Naive (64) | With clock time (50) |
|---|---|---|
| evening 18–24 | 19 | **19 (~38%)** |
| afternoon 12–18 | 16 | 16 (~32%) |
| morning 06–12 | 14 | 14 (~28%) |
| night 00–06 | 15 | **1 (~2%)** |

**Ordering:** above `_TIME_COMPARISON_KEYWORDS` (M1), `_TREND_KEYWORDS`
("over time" — an hour-of-day question is not a time series) and
`_LIST_ALL_KEYWORDS`; below M7's reporting-speed comparison (asserted end to
end). The bare Urdu "رات" is a substring of "کراتا" (CR6) and "شام" of
"شامل" (KB5); only bound forms are matched, both collisions pinned.

**Live** (`route='XAGG'`, 27.2 s, `status=done`): both readings and the rule
separating them survived generation intact.

**Gold comparison: gold's conclusion is right, its stated reason is an
artefact.** "A mild evening lean" holds and is stronger than gold says
(38%). "Fairly flat across the day" holds only if 14 date-only timestamps
are read as real midnights — on recorded clock times, overnight is close to
empty. Gold's operational conclusion (no night-crime patrol case) survives,
better supported by the corrected reading than by gold's own.

---

# Modules 31–34 — what is NOT done, and why ⬜

**G1's answer is unchanged by these four modules.** The aggregates exist,
are correct against the live graph, and are reachable — each verified live
with `route='XAGG'` and its own `backend.log` line — but `caseload_review`
in `harness/agents/meta_analysis.py` still emits its original five
sub-queries, so none of the four fires during G1. The live G1 run confirms
this: `['XNETWORK','XAGG','XAGG','XAGG','XAGG','XAGG']`, plan
`'caseload_review'` matched, and **no** new aggregate log line anywhere in
its section of the log.

Wiring them needs `meta_analysis.py`, **owned by another track this wave**,
plus a decision on `_MAX_SUB_QUERIES = 5`: that plan is already full, and
Module 29 recorded a sixth candidate (Module 24's statute×court-stage join)
waiting on the same cap. Module 29's own report names this cap decision as
something "Modules 31-34 need". It is a decomposition change, not an
aggregate change, and belongs in its own module.

The four canonical sub-query strings are pinned in `tests/test_xagg.py`
(`_G1_SQ_ACCUSED_AGE`, `_G1_SQ_RELATIONSHIP`, `_G1_SQ_SEIZED_PROPERTY`,
`_G1_SQ_TIME_OF_DAY`) and each is asserted to route deterministically to
XAGG, so whoever wires them can copy them across unchanged.

**Three further defects found and deliberately left unfixed** — detail in
`MODULE31_RESULT.md` §8:

1. `XAggToolResult.aggregate_kind` is a hand-maintained `Literal` that no
   test forced anyone to update. All four new kinds were missing, which
   produced an **empty answer with `status=None` and no user-visible error**
   while every unit test stayed green. Fixed here plus a membership test,
   but this is the **fourth** module family to hit it (13, 22, 23/24, 31–34)
   — deriving it from `run_aggregate()` deserves its own module.
2. `_XGRAPH_OVERRIDE_PATTERNS`' `\bacross\b.{0,15}\bcases\b` swallows any
   sub-query written in `meta_analysis.py`'s natural "…, across all cases?"
   house style. Three of the four first drafts came back `route='XGRAPH'`
   live. Worked around by leading each with "How many cases …" rather than
   widening `router.py` (another track's file).
3. Generation can silently re-filter a rendered aggregate: Module 33's
   paraphrase computed the correct 45 items / 28 FIRs but reported "2
   FIR(s)", having keyword-filtered the rendering on "مالخانہ" from the
   question. The claim verifier caught it, so it did not ship silently.

**Regression guard, all re-run live after the four modules landed:** M5 ✅,
G5 ✅, G3 ✅ (82 of 94 — the figure `MODULE24_RESULT.md` already recorded;
gold says 81), CR7 ✅, G6 ✅. **M4** returned the statute half only across
three runs — the pre-existing instability Module 24 already documented
("completed end to end on only 1 of 5 runs"; its route is LLM-decided and
usually not XAGG). `git diff c797e70..HEAD` touches only `xagg.py`,
`orchestrator.py`, `harness/tools/xagg.py` and `tests/test_xagg.py`, and
removes exactly **four** lines from `xagg.py` — all in the old AGE refusal
branch — so no other aggregate changed.

---

# Module 35 — G6: arrest rate has no aggregate ✅ DONE

**Branch:** `feature/xagg-arrest-rate-and-fir-listing`
**Result file:** `docs/gold-qa-wave2-results/MODULE35_RESULT.md`

**Outcome.** `_arrest_rate()` ships with its classification rule **published**
in `_classify_arrest_status()` and restated in the rendered answer:

> **arrested** = the status contains **گرفتار**, does **not** negate it, and
> does **not** attribute it to an earlier different case.

Live (73 FIRs, 94 accused entries, 92 distinct accused): **11 FIRs record an
arrest — 1 in 6.6**, *not* gold's 1 in 9. The alternatives are reported
alongside: +earlier-case 12 (1 in 6.1), naive containment 14 (1 in 5.2), and
**exact equality with the bare token `گرفتار` 8 (1 in 9.1)**. Only the last
reproduces gold, and only by discarding three entries that record an arrest
in as many words (`موقع پر گرفتار`, `گرفتار، بعد ازاں سزا یافتہ`,
`گرفتار، ڈی این اے مطابقت پر`). **It was not tuned to gold**; the bare-token
figure is returned as `bare_token_fir_count` so the gap stays attributable.

Correction to the survey below: the naive containment count is **14** FIRs,
not 13 — re-derived live 2026-09-08.

`_is_arrest_rate()` is a three-signal predicate, not a keyword tuple, because
the bare arrest vocabulary collides with gold **S3** outright
(`کیا کسی شخص کو ایک سے زیادہ بار گرفتار کیا گیا ہے؟`); S3 is asserted to
still reach `graph_recurrence` end to end with its literal gold text.

---

**Found by:** Module 29's gap analysis. **File:** `src/pipeline/xagg.py`.

Gold G6: "girftari sirf har no mein se taqreeban ek FIR par darj hai" —
an arrest is recorded on roughly 1 in 9 FIRs. Probed live: the data
**exists** — `Person-[:INVOLVED_IN {role:'accused', arrest_status}]->Incident`,
94 accused edges, and 13 distinct FIRs carry a status containing گرفتار.
73/13 ≈ 1 in 5.6, **not** gold's 1 in 9, so the definition matters: the
statuses are free Urdu text (گرفتار 11, موقع پر گرفتار, گرفتار بعد ازاں سزا
یافتہ, گرفتار ڈی این اے مطابقت پر, and negations such as
تاحال مفرور، گرفتار نہیں ہوا and نامزد، گرفتاری کی نوبت نہ آئی). **Deriving
the rate is the module's real work, and it must publish the classification
rule it used** rather than matching gold's number.

No aggregate exists; the sub-question falls through to person-recurrence.

---

# Module 36 — CR3: no subject-filtered FIR listing ✅ DONE (wiring deferred)

**Branch:** `feature/xagg-arrest-rate-and-fir-listing`
**Result file:** `docs/gold-qa-wave2-results/MODULE36_RESULT.md`

**Outcome.** `_filtered_fir_listing()` answers "which FIRs are registered
under `<act>` / at `<station>` / of `<type>`, with FIR number and status".
Live, it returns **exactly `fir-64-26` (64/26) and `fir-65-26` (65/26)** with
both Urdu statuses — the stated ground truth. Bounded by construction for the
reason Module 29 measured: it **refuses to list anything** when no filter is
recognised, and caps rendered rows at `_FIR_LISTING_RENDER_LIMIT = 15`.

Two substring collisions were found live and fixed before commit:
`"cyber crime circle"` (station) contains PECA 2016's `"cyber crime"`, so a
pure station question silently answered 2 FIRs instead of 9 —
`_mask_station_aliases()`; and an Urdu question naming the circle by its real
name matched no station signal at all, since neither `سائبر کرائم سرکل` nor
`موٹروے پولیس اسٹیشن` contains `تھانہ` — `_STATION_NAME_HINTS`.

**Verification differs from the instruction below, deliberately.** The
`meta_analysis.py` `record_consistency` wiring was **not** done: Module 41 is
editing that dispatch path concurrently. The aggregate was verified
**standalone** through `/api/chat` instead, and the literal sub-query string
is pinned in `tests/test_xagg.py` as `_CR3_SQ_FIR_LISTING` for whichever
module consolidates the plan after Module 41 lands. **CR3's own end-to-end
instability is therefore not yet fixed** — the capability it needs now exists
and is live-correct, but is not connected to CR3's decomposition.

---

**Found by:** Module 29's live runs. **File:** `src/pipeline/xagg.py`.

CR3 asks about "the online banking fraud matter involving two separate
victims". After Module 29 the question decomposes correctly and both
sub-answers carry the facts gold needs, but **nothing in them says which
FIRs the question is about** — so the synthesis model has to infer the pair,
and across Module 29's runs it sometimes refused, and once paired
`fir-64-26` with the wrong FIR entirely.

The reason is a real capability gap: **no aggregate returns FIR numbers
filtered by statute, station or crime type.** `_station_or_category_counts()`
returns counts only; `_filtered_cases()` can filter by act but the
`case_listing` branch that returns per-FIR rows is deliberately guarded
*against* act keywords; and the unfiltered listing is the whole 73-row
corpus. Module 29 tried adding that unfiltered listing as a sub-query and
**reverted it**: rendering 73 cases takes ~4.6 KB of generation and starved
its two concurrent siblings into the 60 s `META_ANALYSIS_SUBQUERY_TIMEOUT`.

**Work:** an aggregate that answers "which FIRs are registered under <act> /
at <station> / of <type>, with their FIR number and status". Ground truth
for CR3: `fir-64-26` and `fir-65-26` are the only PECA 2016 cases at a
سائبر کرائم سرکل station that share an accused (عاصم رشید). **Verify** by
adding it to `meta_analysis.py`'s `record_consistency` plan and re-running
CR3 and its paraphrase several times — the instability, not a single good
run, is what this module has to fix.

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

# Module 23 — M5: weapon × statute co-occurrence join ✅

**Branch:** `feature/xagg-weapon-statute-cooccurrence`
**Full result:** `docs/gold-qa-wave2-results/MODULE23_RESULT.md` (every number
below traces to a captured live output recorded there).
**Question:** M5. Gold: Arms Ordinance §13 paired only with robbery statutes
in 2024, but with narcotics (CNSA §9(c), 8 cases) and murder (PPC 302, 8
cases) in 2026.

**Root cause — the brief's hypothesis, confirmed first-hand.** M5 contains
the literal substring "کے مقابلے میں", an entry in
`_TIME_COMPARISON_KEYWORDS`, so the ordered dispatch chain in `xagg.py`
matched M1's branch first and answered M5 with `_statute_mix_by_year()`. That
aggregate has no weapon dimension at all, and groups by ACT rather than
section (its source is `cases.crime_category`, which
`muhafiz_cases._crime_category()` reduces to the comma-joined `act` list), so
it cannot distinguish robbery (PPC 392) from murder (PPC 302) — the exact
distinction M5 turns on. Reading the chain end to end confirmed the brief's
other claim too: **no aggregate joined Weapon nodes to their case's statute
set**. A missing primitive, not a mis-tuned one.

**What the brief did not say, and what decided the module:** section-level
statutes ARE in the graph, as `StructuredRecord{record_type: 'fir_section'}`
nodes carrying `act` + `section_code`, each with a `BELONGS_TO_CASE` edge
(218 of them, verified live against `evidence_graph`). Without them gold's
section granularity would not have been derivable and this module would have
had to report a data-availability gap instead.

**Aggregate added:** `_weapon_statute_cooccurrence_by_year()` in
`src/pipeline/xagg.py`, plus `render_weapon_statute_cooccurrence()` wired at
all three XAGG rendering sites and the new kind added to the harness
wrapper's hand-maintained `AggregateKind` Literal (omitting that is the
documented silent-`literal_error` crash class Modules 13 and 22 each hit).
Three graph reads joined on `case_id`: the year query is byte-identical to
`_statute_mix_by_year()`'s, so the two can never disagree about which year a
case falls in; the weapon query is `_top_recurring_weapon_types()`'s, through
the same `_normalize_weapon_type()`.

**Where it sits in the dispatch chain, and why:** immediately BEFORE
`_TIME_COMPARISON_KEYWORDS` (the branch that was swallowing M5), and
therefore also before `_TREND_KEYWORDS`' refusal, `_DISTRICT_KEYWORDS`
(Module 1c's district+weapon path) and the bare `_WEAPON_KEYWORDS` recurrence
branch — the three prior collision sites this chain's own comments name. It
sits AFTER G5's weapon+compliance check and M7's
`_is_reporting_speed_comparison()`. The predicate is a three-signal AND
(weapon + case-type/statute + change-over-time), the same discipline Module
22 established, because every narrower combination is already some
neighbour's shape. `router.py` was NOT touched — M5 reaches XAGG on its own,
confirmed by the live `route=` capture, so Track A/B independence with Module
28 held.

**Measured per-year pairings (live, All Cases, platform-admin), against
gold:**

| | 2024 | 2026 |
|---|---|---|
| Cases with a recovered weapon | 13 | 19 |
| Carrying a weapons-law charge | 13 | 16 |
| §13 co-occurs with | PPC §34 (13), PPC §392 (13) — nothing else | CNSA 1997 §9(c) (8), PPC §302 (8), PPC §34 (8), PPC §392 (8) |

**Every one of gold's seven figures matches exactly**, and the qualitative
point ("weapons no longer confined to robbery") is produced as a set
difference over the buckets, not as a fixed narrative — a test feeds the
renderer two identical years and asserts it then says *unchanged*. The raw
data reproduced gold from a hand-written Cypher probe BEFORE the aggregate
was written, so nothing was tuned to hit it.

One number is deliberately not a gold match: the broader `statutes` view
reports PPC §34 at 10 for 2026 where gold says 8. Different quantity —
`statutes` counts all weapon-bearing cases, `cooccurring` counts only those
also carrying a weapons-law charge, and two 2026 cases have a weapon and PPC
§34 with no Arms-Ordinance section. Both views are reported and labelled.

**Non-gold paraphrase (required):** *"Are guns turning up in different types
of cases than they used to?"* — plain English, no statute vocabulary, no
keyword shared with M5's literal phrasing. Reached the same aggregate and
returned the same figures. Capability fix, not curve-fitting.

**Negative controls — both re-run LIVE after the change:**

- **G5** (weapon-register compliance, 1.0): unchanged, still
  `_weapon_compliance_scan()`, still "30 of 32 (94%) recorded without a
  licence" plus the 2 with no status — matching gold. The new aggregate's
  log line did not fire on that request.
- **M1** (year-over-year statute mix, Module 26): unchanged, still
  `_statute_mix_by_year()`'s act-level breakdown — 2024 PPC 13 / Arms Ord 13;
  2026 PPC 39 / Arms Ord 16 / CNSA 12 / PECA 9 / PDVA 4 / IDA 2. The new
  aggregate's log line did not fire on that request either.

Both boundaries are also pinned as unit tests, alongside an **all-32 negative
control** asserting the new predicate matches exactly `["M5"]` across the
whole gold set — the same assertion that would have caught all three
historical pattern collisions on this plan.

**Unit:** `tests/test_xagg.py` 105 passed / 1 pre-existing skip; the four
neighbouring suites (`test_harness_tool_xagg`, `test_orchestrator`,
`test_harness_agent_large_scale_aggregate`, `test_router`) 218 passed. 21 new
tests, including the regression pinned to M5's literal Urdu gold text. The
full suite was deliberately not run (it empties the real
`muhafiz_entity_descriptions` Chroma collection).

**New defects found, deliberately left unfixed** (see the result file for
detail): (1) the generation layer splits Urdu weapon canonical names on their
internal commas, so a 3-type list renders as 4 items — a rendering-layer bug
affecting any answer that lists Urdu canonical names; (2) one of the two live
runs conflated "16 of 19 carry a weapons-law charge" with the per-statute 8s,
generation variance the numeric verifier did not catch; (3)
`cases.crime_category` permanently discards `section_code`, so every
Postgres-side statute aggregate — M1's included — is act-level while the
graph holds the section. Whether M1 should also become section-level is a
real question and a scope change, flagged rather than taken.

---

# Module 24 — M4: statute × court-stage join ✅

**Branch:** `feature/xagg-statute-court-stage-join`
**Question:** M4.
**Full result:** `docs/gold-qa-wave2-results/MODULE24_RESULT.md`.

**PR #8 is merged, so this module was never blocked.** Its fix is intact and
was re-confirmed live: M4 no longer matches `_COURT_READINESS_KEYWORDS` and
no longer hijacks G3's readiness scan.

**Root cause found — the plan's framing needed two corrections.**

1. *"M4 gets the statute half right (PPC 61, Arms Ordinance 29)"* — those
   numbers are **act-level**, from `_station_or_category_counts()`'s
   `counts_by_act` over `cases.crime_category`, which
   `muhafiz_cases._crime_category()` reduces to a comma-joined act list.
   M4 asks about **دفعات** (sections), and gold's own answer cites **no**
   statute counts at all. Section-level statutes exist only in the graph, as
   `StructuredRecord{record_type:'fir_section'}` (`act` + `section_code`) —
   the source Module 23 found and this module reuses.
2. *"M4 gets the statute half then claims no court-stage data exists"* —
   true as a symptom, but **not because a single XAGG call answered half the
   question**. M4's route is LLM-decided (`_deterministic_route_override()`
   returns `None`) and came back `XNETWORK` on 4 of 5 live captures and
   `XAGG` on 1; in every case the supervisor handed it to **Meta-Analysis**,
   which decomposed it into a charging sub-query and a court sub-query. The
   "no court-stage data" sentence is the **court sub-query's** own answer.
   Inside `run_aggregate()`, M4's whole text lands on the **person-recurrence**
   branch, because `_PERSON_KEYWORDS` contains the bare token `"لوگ"` and
   M4's second word is `"لوگوں"`.

**Work done:** `_statute_court_stage_join()` + `render_statute_court_stage_join()`
in `src/pipeline/xagg.py`, dispatched by a two-signal `_is_statute_court_stage_join()`
(a court term **and** a progression/stage term — never the bare word
"court", which is what PR #8 had to untangle). Placed **after** CR7's and
G3's branches (so both keep first claim structurally) and **before**
`_TIME_COMPARISON_KEYWORDS` and `_PERSON_KEYWORDS`. The court half **calls
`_criminal_record_court_crosscheck()` as-is and renders it through
`render_criminal_record_crosscheck()`** — no second query path, and
`_conviction_is_settled()` is not re-derived. Module 14's reader needed no
extension. `router.py` was not touched.

**Measured live (`_statute_court_stage_join()` against `evidence_graph`),
derived from a hand-written Cypher probe before the aggregate was written:**

- Charging side: **218** `fir_section` entries over **73** cases, **36**
  distinct sections — PPC §34 ×40, Arms Ord 1965 §13 ×29, PPC §392 ×21,
  CNSA §9(c) ×12, PPC §420 ×11, PPC §302 ×10, …
- Court side: **33** criminal records — **30** `Under trial`, **1**
  `Convicted, on bail pending appeal`, 1 `Sub judice, external prior case`,
  1 `Under trial, priority hearing requested` → **1 settled / 32 in progress**.
- Join coverage: only **4 of 33** criminal records name an FIR present in
  this corpus (reported, not hidden).
- Verdict (a derived majority test, not a tuned threshold): **do not agree**.

**Gold comparison: 33 / 1 / 30 match exactly, and were not tuned for.**

**Regression guard, all live:** **G3** unchanged (still `_court_readiness_scan()`,
all three gold findings; the new aggregate's log counter did not increment);
**CR7** unchanged (still `_criminal_record_court_crosscheck()`, still 33/30/1
plus the FIR 891-24 consistency finding); **M5** unchanged (still Module 23's
co-occurrence aggregate). Unit: `tests/test_xagg.py` 132 passed; the five
touched suites 359 passed, 1 xpassed.

**What is still broken, and it is not in `xagg.py`:** M4 completed end to end
on only **1 of 5** post-change runs. The aggregate ran on all five (proved by
its own log line). Three runs were rejected by Meta-Analysis's synthesis
verifier with *"A claim is attributed to Document 1 but is absent from its
text and instead appears in Document 2"* — **Module 25's defect exactly**
(PR #16, since merged). One timed out on the model-server tunnel. Module 26's
supervisor guard ("XAGG already answers this in one call, do not decompose")
would cover M4's shape but is gated on `route == "XAGG"`, which M4 usually
is not. A deterministic router override for the statute×court-stage shape
plus an extension of that guard is the follow-on — it belongs in
`router.py`/`supervisor.py`, both owned by other tracks this wave, so it is
flagged here rather than folded in.

**Also fixed in passing:** Module 22's own all-32 negative control
(`test_matches_m7_and_no_other_gold_question`) pointed at
`Gold_QA_Dataset_Final32.json` at the repo root, which is **not tracked in
this repo** — so it silently skipped and had never run. Re-pointed at
`evaluation/Gold_QA_Dataset_Final32_With_Answers.json` and asserted rather
than skipped. It passes.

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

# Module 41 — G2/G5 regression: Meta-Analysis over-decomposition ✅ DONE

**Branch:** `fix/supervisor-skip-decomposition-for-resolvable-aggregates`
(off `main` @ `06de8ea`) · **Result:**
`docs/gold-qa-wave2-results/MODULE41_RESULT.md`

**Found by the 2026-09-08 post-fix evaluation** (`EVALUATION_REPORT_POST_FIXES.md`
§4, `HOW_TO_REPRODUCE_THIS_EVALUATION.md` §4.2). G2 fell **0.4 → 0.0** and G5
**0.6 → 0.0** — a regression this wave caused.

**Root cause — the brief's diagnosis confirmed exactly.** The aggregates were
never at fault (`_case_completeness_scan()` → 73 cases / 9 missing incident
dates / 52 missing status; `_weapon_compliance_scan()` → 30 of 32 unlicensed),
and `run_aggregate()` dispatched both correctly. The failure sat one layer up:
`supervisor.py`'s Meta-Analysis skip guard fired only for
`_TIME_COMPARISON_XAGG_PATTERNS`, so G2 and G5 — which satisfy every other
condition — were handed to Meta-Analysis, decomposed by the **LLM decomposer
fallback** (Module 29's deterministic plans return `None` for both), and their
sub-queries re-classified by a fresh router call one level down. Measured live
on `06de8ea`, four runs each: G5's own sub-query landed on **XGRAPH /
Cross-Case Linkage** and returned `empty`; G2 abstained on one run with four
timed-out sub-questions and answered a *different* question (the 2024→2026
case-type shift) on another. Intermittent, exactly as the report warned:
2 of 4 G5 runs and 2 of 4 G2 runs failed. Pre-fix latency 78–214 s.

**Fix — the structural option, not a second pattern list.** `run_aggregate()`'s
keyword chain was **extracted** (not duplicated) into the pure
`xagg.resolve_aggregate_kind()`; `run_aggregate()` now dispatches on its
return value, so there is one source of truth and every aggregate Modules
31–36 add is automatically visible to the guard. The guard asks
`xagg.resolves_to_specific_aggregate()`, which **runs no aggregate** — pure
substring matching, no `await`, no gateway, no RLS arming, no audit event
(pinned by an AST test).

Three deliberate calls, each with its own test:
- **Subordinate to Module 29's `_DECOMPOSITION_PLANS`.** G1 is the
  load-bearing case — it *does* resolve to `case_completeness_scan`, so
  without the veto this guard would have silently repealed Module 29 for it.
  CR3, G1 and G6 still decompose.
- **`unsupported_aggregate` counts as resolved.** M2's station-type refusal
  is a purpose-built honest outcome; decomposing it yields two halves that
  each invent a split — the "fluent, on-topic, confidently wrong" mode the
  report names as worse than abstaining.
- **The three trailing catch-alls and the three bare entity-recurrence
  families do NOT count.** Reaching either tier means the chain recognised
  nothing, or recognised only a noun.

**Result.** G2 and G5 both dispatch to `Large-Scale Aggregate` on **3/3** live
runs each, deterministic, 16–54 s. G2 returns 9 missing incident dates and 52
missing status exactly; G5 returns 30 of 32 (94%) unlicensed exactly. Both are
partial against gold — gold's other points need aggregates that do not exist
(logged as new defects in the result file), so neither is a 1.0.

**Nine of 32 gold questions change dispatch** (assuming an XAGG route, pinned
by an all-32 negative control test): **CR6, CR7, CR8, M2, M4, M7, G2, G3, G5**.
All were re-run live. Six of the nine previously scored 0.0; the other three
(CR6, CR7, CR8) plus G3 already scored 1.0 and reach the *same* sub-agent as
before — the LLM decomposer was already returning `decompose: false` for them
and falling back to a single dispatch, so the guard removes two LLM round
trips and the variance they carried, rather than changing the destination.

---

# Module 27 — Final Gold-32 rerun ⬜

Blocked on everything above. Re-run `gold32_run.py` + `gold32_score.py`
against the fully-merged build, on a **freshly restored, UTF-8-clean** dump
(see §0's Environment note). Report per-bucket movement against Module 18's
0.428 / 0.550-excl-KB baseline, and state for each module whether its
non-gold paraphrase confirmed a real capability gain.

---

# Modules 41–47 — found by the 2026-09-08 post-fix evaluation (46–47 by Module 45)

**Source:** `EVALUATION_REPORT_POST_FIXES.md` and
`HOW_TO_REPRODUCE_THIS_EVALUATION.md`, an independent Gold-32 rerun against
`main` @ `06de8ea` with the regenerated 2026-09-08 dump and Chroma, judged by
`gemini-flash-lite-latest`.

**Headline: the wave worked.** FactualCorrectness **0.425 → 0.572** all-32,
pass rate **14/32 → 19/32**, KB bucket **0.062 → 0.350**, AnswerRelevancy
**0.687 → 0.886**. Complex Reasoning 0.64 → 0.86. M5, CR4, CR3, KB8, KB5 and
M1 were fixed outright; KB1, KB3, KB4, G1 and G6 improved.

**Read this before trusting any earlier number in this plan.** The report's
first pass was invalid because `SHARE/.env` was never copied into place, so a
stale local `.env` carried 3 of 5 wrong Groq keys and produced **1,410
rate-limit errors**; every KB question abstained and the bucket scored 0.000.
That was an environment artefact, not a defect. Before any future run:
`grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota|UNAVAILABLE|503" backend.log` must stay near zero
(widened by Modules 46 and 54 — the bare `grep -c "rate limit"` originally
prescribed here returns 0 over a Gemini `429 RESOURCE_EXHAUSTED` and over a
`503 UNAVAILABLE`).

**A pattern worth naming across 41–44:** six questions now score
AnswerRelevancy **1.0** with FactualCorrectness **0.0** — fluent, on-topic,
confidently wrong, where before the fixes they abstained. That is arguably a
*worse* failure mode for an evidence platform than refusing, and it is the
thread connecting these modules.

---

# Module 42 — KB6 hard failure ✅ investigated; the defect was in the harness

**Branch:** `fix/kb6-hard-failure-route-none`
**Result:** `docs/gold-qa-wave2-results/MODULE42_RESULT.md`

**Finding, stated up front: `route=None` was never a pipeline state.** It was
`evaluation/gold32_run.py`'s own **300s `urlopen` timeout**. KB6 takes
428.8-628.2s on the runs that abstain, so the request was still in flight when
the client gave up; the exception handler then recorded `route=None` with an
empty answer, and `gold32_score.py` handed `"(no answer produced)"` to the
judge, which scored it 0.0 on both metrics.

Live, five runs, `admin@example.com`, All Cases: **`route='RAG'` on 5 of 5**,
never `None`. That also explains the detail this plan flagged as most
diagnostic — KB6 "did not recover" from the credential fix because correct API
keys do not make a request finish inside 300 seconds.

**KB6 is intermittent**, which the old harness hid: 4 of 5 runs abstain (and
exceed 300s), 1 answers in 247.1s. Because passing the relevance gate early is
what makes a run *fast*, the 300s ceiling did not sample KB6 randomly — it
decided the published row by a coin flip.

**This is not a KB6-only problem.** Re-running all eight KB questions live,
**five of them cross the old 300s ceiling** — KB1 (403.0s on one of two runs),
KB2 (383.3s, returning a good 1,148-character answer), KB3 (481.2s), KB9
(341.0s) and KB6. Each would have been published as `route=None` / 0.0 / 0.0.
`route='RAG'` on all eight, every run; no `None` anywhere.

**Fixed here (harness only, `src/` untouched):** `GOLD32_TIMEOUT_S` (default
900) replaces the hard-coded 300; a failed request records `transport_ok:
False`; and such a row is left **unscored** rather than judged, so
`summarize()` excludes it. Same principle as Module 45's "a judge `null` is not
a zero", one layer earlier. 16 new tests in
`tests/test_gold32_transport_failure.py`, pinned to KB6's literal gold text,
with negative controls so a genuine abstention is still scored.

**Not Module 39's gap, measured rather than argued.** Holding the chunk set
constant *and containing gold's own statutory text*, `evaluate_relevance()`
returns `relevant=True` **6/6** for an English phrasing and **1/6** for the
roman-Urdu one; deleting the "and does our data show it?" clause makes it
**worse** (0/3), not better. The gate cannot judge roman-Urdu against English
statute text. Split out as **Module 52**; artefact in
`evaluation/kb6_evaluator_language_experiment.json`.

**Not fixed:** KB6 still does not reach gold. This module corrected the
measurement, not the score, and says so.

---

# Module 43 — M7 answers with the wrong facts ✅ DONE

**Branch:** `fix/m7-m2-cs4-factual-accuracy` · **Result:**
`docs/gold-qa-wave2-results/MODULE43_RESULT.md`

**Answered, and the answer is that the report is wrong.** This section asked
which of two contradicting observations was wrong — the post-fix report's
M7 row (FC 0.0) or Module 22's recorded live verification. **Module 22 was
right. M7 never regressed, and nothing in `xagg.py` needed fixing.**

The three-layer check, with the aggregate re-derived by hand-written Cypher
rather than by calling it:

- **Layer 1** — direct Cypher over `Incident.incident_datetime` /
  `.report_datetime`: 64 of 73 nodes carry both (matching this section's own
  note), giving **2024 n=13 mean 15.0 min** and **2026 n=51 mean 1401.3 min**.
  Gold exactly, both means and both counts.
- **Layer 2** — `resolve_aggregate_kind()` returns
  `incident_to_report_minutes_by_year` for M7's literal gold text, and no
  aggregate added since Module 22 has taken it. An all-32 equality control now
  pins that, which is what ruled out this section's "an aggregate added since
  stole the dispatch" hypothesis.
- **Layer 3** — **6 of 6** live `/api/chat` runs on `route='XAGG' ->
  Large-Scale Aggregate` returned 15.0 min / 13 FIRs and 1401.3 min (~23.4 h)
  / 51 FIRs. The non-gold paraphrase does too.

**Where the report's answer came from.** The only M7 answer recorded anywhere
in this repository (`evaluation/gold32_pipeline_outputs.json`) is the
delay-REASON rate — "0% of FIRs recorded a delay reason (0 of 13), 14% (7 of
50)", down to a paraphrase of `_reporting_delay_rate_by_year()`'s own `note`.
That function has had **no dispatch entry since Module 22 re-pointed M7 away
from it**, so it is unreachable from any query text. The file was last written
by `d313a60` on **2026-09-06 11:27**; Module 22's aggregate landed `f364bf9`
on **2026-09-08 00:48**. The answer predates the fix it is being used to
judge.

This **independently confirms Module 47** from the opposite direction: Module
45 found the committed artefacts reproduce Module 9's headline figures, and
Module 43 finds the same files carry per-question scores (M7 AR 0.667, M2 AR
0.0, CS4 AR 0.2) that are not the report's (AR 1.0 for all three). Whether the
post-fix run *also* hit M7 — plausibly via the Meta-Analysis decomposition its
own §4 diagnoses for G2/G5, before Module 41 merged — cannot be settled,
because that run's artefacts were never committed. Both readings put the fault
outside `xagg.py` and both are closed on `main` today.

**What was actually fixed.** M7's aggregate emitted **no `XAGG <kind>:` log
line** — Module 22 predates the convention Modules 31–36 established. Since
XAGG's SSE reports only `route='XAGG'`, the single aggregate under
investigation was the one whose live runs could not be identified from the
log; every run had to be recognised by matching numbers out of prose. That
line now exists and carries the per-bucket figures, not just the kind.

**Also pinned:** M7's decomposition skip (Module 41 made it true; nothing
asserted it for M7), and an all-32 equality control on M7's dispatch key.

**New defect split out as Module 55**, not folded in: every aggregate
predating Modules 31–36 still has no log line.

---

# Module 44 — M2 and CS4: fluent but factually wrong ✅ DONE

**Branch:** `fix/m7-m2-cs4-factual-accuracy` · **Result:**
`docs/gold-qa-wave2-results/MODULE44_RESULT.md`

**Both gold answers were computable. Both are now computed, 3 of 3 live runs
each, on `route='XAGG' -> Large-Scale Aggregate`.**

**CS4 — no aggregate existed, and the fall-through did the damage.** Measured:
`resolve_aggregate_kind()` returned `graph_recurrence_person`, so CS4 was
answered with "4 people appear in 2 cases each" — the **third** time this file
has recorded the person-recurrence tier silently swallowing a question
(Modules 32 and 35 are the others). Two causes compounded: nothing in
`_CRIMINAL_RECORD_KEYWORDS` matches "criminal-**history** records" (that tuple
has "criminal record"/"criminal records", neither a substring), so CS4 never
reached CR7's family either and fell to `_PERSON_KEYWORDS` on "shakhs"; and
there was no aggregate for the question anyway. Because
`graph_recurrence_person` is in `_ENTITY_RECURRENCE_AGGREGATE_KINDS`, CS4 also
kept its route to Meta-Analysis, whose recorded decomposition **inverted the
set difference** (asking which of *our* accused are missing from the
criminal-records system). A purpose-built aggregate closes both at once —
Module 41's guard does the second half for free. CS4 now returns gold exactly:
**وقاص (Waqas), CNIC 00000-9000020-1**, matched on CNIC (his NAME appears
among our accused on a different CNIC, so a name-keyed join answers "no"), with
gold's "this is expected" caveat earned from 31 of 32 subjects matching rather
than asserted.

**M2 — the refusal was asserting something false.** This section said no
station-type dimension exists in the data model. There is no `station_type`
FIELD, but the refusal's second clause — "caseload cannot be compared across
that dimension" — does not follow. Hand-written Cypher: **2 of the 19
PoliceStation nodes are named سائبر کرائم سرکل (Cyber Crime Circle) and carry
9 of the 73 FIRs**, which is gold's claim exactly and needs only the names.
Retired for the reason Module 31 retired `_UNSUPPORTED_AGE`. The
classification is name-derived, says so in its own output, and lists every
station with its bucket so the call can be checked; two further stations
(Women's, Motorway) are specialised by complainant class / jurisdiction rather
than by crime type and are held out in their own named bucket, because folding
them in gives 4 stations / 19 FIRs and contradicts gold.

**Gold challenged, on M2, with evidence.** M2 asks which group is growing
FASTER; gold answers with a static share and never addresses growth. On the
growth question the data points the other way — general-purpose stations went
**7 → 39** FIRs between 2024 and 2026 while the crime-type units went **3 →
5**. And "disproportionate" overstates it: 2 of 19 stations is 10.5% of the
stations carrying 12.3% of the FIRs, barely above proportional on a base of 9.
The system now states the growth finding first and still gives gold's
concentration figures in the same sentence.

**Two measured behaviours recorded in the result file rather than smoothed
over:** the first rendering lost gold's numbers to the paraphrase on 3 of 3
runs (fixed by putting both halves in one sentence), and the verifier now
rejects M2's paraphrase on 3 of 3 runs because the model derives a **457%**
growth rate that is in no computed result — the deterministic guard catching
exactly the over-claim the rendering warns against, and serving the raw
aggregate, which carries every gold fact.

**Note on this section's own premise.** It said M2 "contradicts recorded
results" because Modules 25 and 29 had found it completing cleanly. Module 43
established that the post-fix evaluation's artefacts were never committed (see
Module 47), so the per-question row that premise rests on cannot be read. The
work here did not depend on resolving that.

---

# Module 45 — the eval harness scores a judge `null` as zero ✅

**Branch:** `fix/eval-harness-null-scores-and-truncation` · **Result:**
`docs/gold-qa-wave2-results/MODULE45_RESULT.md`

**Confirmed, and worse than filed.** The brief assumed `measure()` "already
returns `None` on timeout or error" so the danger was purely downstream. Two
corrections, both from reading the code:

1. **The retry loop never ran for this failure.** `measure(metric, tc, tries=8)`
   looked like it retried eight times; it `continue`d only for rate limits. A
   timeout returned `None` on attempt 1, and any other exception returned `None`
   on attempt 1. The exact CR8 case — a judge call producing no parseable number
   — was therefore **never retried**, despite the report showing that one re-run
   recovers it to 1.0. Worse, a `null` did not even reach that path cleanly:
   `round(float(metric.score), 3)` on `metric.score is None` raised `TypeError`,
   so the recorded reason read `ERROR: float() ...` and said nothing about the
   question being unscored rather than wrong.

2. **There was no mean computation in the repository at all.** `gold32_score.py`
   ended at `print(f"wrote {len(results)} ...")`. Every published figure — the
   0.572 and 19/32 in `EVALUATION_REPORT_POST_FIXES.md`, the 0.39 and 13/32 in
   `evaluation/MODULE_9_RERUN_REPORT.md` — was hand-derived, once per report,
   with nothing to stop a `null` counting as 0.0 and nothing in the output to say
   one was present. That absence *is* the defect.

**Fixed.** `measure()` now retries null/timeout/error a bounded 3 times with
backoff (rate-limit retries budgeted separately, so a quota wobble cannot eat
the budget reserved for genuine judge failures), detects `metric.score is None`
explicitly, and records an unretrievable score as `None` with the reason
`UNSCORED after N attempt(s) — NOT a zero`. A new null-safe `summarize()` is the
single authoritative aggregation — unscored rows are **excluded** from every
mean and per-bucket figure, never zeroed — and `format_summary()` prints them
first, in a banner, before any number. New `--summary` mode recomputes the
published figures from an existing results file with zero judge calls.

**Verified.** 22 new tests in `tests/test_gold32_score.py`, no network (the
judge is a stub, backoff sleeps injected); 32 passed with
`tests/test_eval_scripts.py`. The pinned regression builds the report's exact
shape — 31 scored rows plus CR8 `null` — and asserts the mean is 1.0 and
explicitly **not** the 0.969 that zeroing CR8 gives. Live: CR8 re-scored with
the real Gemini judge returns **1.0/1.0**, matching
`EVALUATION_REPORT_POST_FIXES.md` §5. Whole-file guard: `--summary` over the
untouched committed results file reproduces `MODULE_9_RERUN_REPORT.md`'s
published 0.39 / 0.65 / 13-of-32 on every bucket — the new aggregation agrees
with the hand computation it replaces.

**Also tested and NOT confirmed: the 900-char truncation hypothesis.** Split out
as Module 46 below rather than folded in. Bottom line for prioritisation:
**no currently-0.0 score is a scoring artefact** — Modules 41–44 keep their
priority exactly as filed.

---

# Module 46 — the 900-char scoring cap will corrupt Module 27's rerun ⬜

**Found by:** Module 45 · **Evidence:**
`evaluation/gold32_truncation_experiment.json`,
`docs/gold-qa-wave2-results/MODULE45_RESULT.md` §5

`gold32_score.py` truncated every answer to 900 chars before scoring, justified
in its own comment by **Faithfulness** — which makes one judge call per atomic
claim. Faithfulness was later dropped from `_METRICS`. **The rationale no longer
exists**, and both surviving metrics make an O(1) number of judge calls
regardless of length.

**Measured, same judge and prompt, cap on (900) vs off:**

| Q | variant | FC @ 900 | FC @ no cap | AR @ 900 | AR @ no cap |
|---|---|---|---|---|---|
| M5 | natural, 1,318 ch | 0.0 | 0.0 | 1.0 | 1.0 |
| M4 | natural, 1,582 ch | 0.1 | 0.1 | 1.0 | 0.909 |
| CR8 | **padded**, 1,519 ch | **0.0** | **1.0** | 0.0 | 0.875 |
| G3 | **padded**, 1,749 ch | **0.0** | **0.9** | 0.0 | 0.833 |

*Padded* = 1,020 chars of filler prepended to a known-good answer, pushing its
gold facts past char 900 and changing nothing else. Both collapse to exactly
**0.0**; uncapped, both recover. The mechanism is total signal loss, not
degradation.

**It does not currently fire.** Only 2 of the 32 committed answers exceed 900
chars, and both score identically capped or not — what the cap discards from
them is verifier footnotes, not gold facts. M4/M5 were stable across 3 draws,
so that is measurement, not luck.

**But it will.** `EVALUATION_REPORT_POST_FIXES.md` §3 states the current build's
KB answers are **1,000–2,500 chars** — routinely past the cap — where the
2026-09-06 run produced only two such answers.

**Work:** decide `GOLD32_MAX_ANSWER_CHARS` against the real 2026-09-08 answers
(needs Module 47), then re-run. Recommended **3,000**. The named constant and
its env override already exist, so this is one line plus a rerun. Measured cost
of the raise: **under 2 s per metric, and no additional judge calls** — M4 at
1,582 chars took the same 5 s as at 925.

**Settle this before Module 27 runs.** A cap that silently zeroes a correct long
answer would make the final rerun's numbers unusable in exactly the way Module
45 exists to prevent.

---

# Module 47 — the 2026-09-08 evaluation's artefacts were never committed ⬜

**Found by:** Module 45

`EVALUATION_REPORT_POST_FIXES.md` names `evaluation/gold32_results.json` and
`evaluation/gold32_pipeline_outputs.json` as its sources. Both files on `main`
were last written by `d313a60` (*"Module 9 — first full Gold-32 rerun"*,
2026-09-06) and have not been touched since. Running Module 45's new
`--summary` over the committed file reproduces **Module 9's** headline
(0.394 / 0.653 / 13-of-32), not the post-fix report's (0.572 / 0.886 /
19-of-32). The files are byte-identical across the main checkout and the
`muhafiz-m35`, `muhafiz-m40` and `muhafiz-m41` worktrees, so no track is
holding a newer copy.

**Consequences.** §4.1's per-question table cannot be reproduced from this
repository. Nobody can read the judge's `reasons` for the six AR-1.0 /
FC-0.0 questions — which `HOW_TO_REPRODUCE_THIS_EVALUATION.md` §Part 6 calls
"the fastest way to check whether a given score is fair", and which Modules
42–44 are scoped against. And Module 46 cannot pick a cap value against real
answer lengths without them.

**Work:** commit the 2026-09-08 `gold32_results.json` /
`gold32_pipeline_outputs.json` pair, or re-run and commit. Note that both files
are *resumed* in place by their scripts, so a rerun overwrites them — committing
each run's pair, or writing per-run copies, is what makes any of these reports
checkable.

This does not invalidate the post-fix report. It makes it unverifiable — which,
for a document written expressly to be independently reproduced, is its own
defect.

---

# Module 55 — pre-Module-31 aggregates emit no `XAGG <kind>:` log line ✅

**Found by:** Module 43

XAGG's SSE stream reports only `route='XAGG'`. The `XAGG <kind>: ...` line in
`backend.log` is therefore the only evidence of WHICH aggregate answered a
live question — the fact every module in this wave is verified against.
Modules 31–36 each added one. Everything older emits nothing, including the
CR6/CR7/CR8/G2/G3 families and the whole entity-recurrence tier.

Module 43 hit this directly: M7's own aggregate (Module 22) had no line, so
the question "did M7's aggregate run, or the neighbouring delay-reason one?"
— the entire point of that module — could only be answered by matching
numbers out of rendered prose. Module 43 added M7's line; the rest are
untouched.

**Work:** one `logger.info("XAGG <kind>: ...")` per remaining family, carrying
the figures and not just the kind (a kind alone cannot distinguish a correct
run from a wrong-metric one). Mechanical, no behaviour change.

**Do it before Module 27's rerun.** A 32-question rerun whose aggregate
choices cannot be read back from the log is a rerun whose failures have to be
re-investigated from scratch, which is what this wave spent Modules 43 and 44
doing.

## Outcome — `fix/xagg-log-lines-and-m2-vocabulary` ✅

**Measured, not estimated.** `xagg.py` returned **32 distinct aggregate
kinds** (38 `"kind":` returns) behind **11** `XAGG <label>:` format strings.
**21 families were silent**, not the ~18 the brief estimated — the brief's
"20 log lines" counted every source line containing the string `XAGG `,
comments included. The silent set was the whole CR6/CR7/CR8/G2/G3 group, the
entire entity-recurrence tier, `case_listing` (the corpus dump several modules
in this wave had to prove they were *not* hitting) and `relational_aggregate`
(the catch-all Module 44 measured M2 falling into).

All 21 now log, each carrying the **figures**. The three honest-refusal paths
(`gender_breakdown`, `offender_age_profile`, `reporting_delay_count`) got
their own lines too — a refusal was previously indistinguishable in the log
from XAGG never running at all. 211 lines added, 0 removed: no dispatch, no
computation and no returned key changed.

**The durable fix is the enforcing test, not the 21 lines.** It walks the AST
for every literal `{"kind": ...}` value in `xagg.py` and fails when one has no
matching log format string, so the next family added cannot silently skip its
line. A second test keeps every format string ASCII — it caught three
em-dashes on its first run. One documented alias: `time_bucketed_mean` stays
labelled by its dimension (`incident_to_report_minutes_by_year`), because
MODULE43_RESULT.md and its regression test are pinned to that string.

Confirmed live **in-process** against the real Postgres gateway and AGE graph:
21 of 21 emit real figures on the first attempt, with Urdu values legible
(`مرد=67, عورت=24`, Urdu district and station names) — PR #30's utf-8
stream fix holding. Figures cross-check against Modules 7/10.1/13/15/2a and
the wave's own graph census.

**Backend/SSE verification deferred for contention** and stated as such: 8015
and 8016 were both LISTENING and both mid-run against the shared model server,
with 4.2 GB of 16 GB free. Module 27's rerun will confirm the SSE half for
free — which is exactly why this landed before it.

Result: `docs/gold-qa-wave2-results/MODULE55_RESULT.md`.

---

# Module 56 — M2's dispatch vocabulary is narrower than its new family ✅

**Found by:** Module 44

M2's dispatch still rests on `_STATION_TYPE_KEYWORDS`, which Module 44 did not
widen — only the KIND it returns changed, from an honest refusal to
`station_caseload_by_specialisation`. That asymmetry is the defect: a narrow
trigger list is a safe default for a refusal (a missed match just means the
question falls through to a generic answer), and a bad one for a real
aggregate (a missed match means the question gets the plain per-station count
that the refusal was written to prevent).

Measured, with the failing phrasing:

| Query | Resolves to |
|---|---|
| *"Do the specialist units handle a bigger share of our cases than the ordinary police stations?"* | `station_or_category_counts` ❌ |
| *"Are our specialised station types taking on more cases than the general purpose stations?"* | `station_caseload_by_specialisation` ✅ |
| *"Which is busier relative to its size: a station set up for one specific type of crime, or a normal thana?"* | `station_caseload_by_specialisation` ✅ |

**Work:** widen the trigger vocabulary ("specialist unit", "specialised unit",
"ordinary station", "normal thana", "dedicated unit", and their Urdu /
roman-Urdu forms), with an all-32 equality control. Deliberately NOT done
inside Module 44: `_STATION_KEYWORDS` sits a few rungs lower in the same
chain, and every widening candidate risks pulling ordinary per-station
questions into this family — the exact collision class this file has recorded
five times.

## Outcome — `fix/xagg-log-lines-and-m2-vocabulary` ✅

`_STATION_TYPE_KEYWORDS` widened from 10 entries to **58**: the specialised
side (`specialist`/`specialised`/`specialized`/`dedicated` × unit / station /
thana / police, `crime-specific station`), the general-purpose side
(`ordinary`/`normal`/`regular` × station / police station / thana,
`general-purpose thana`/`unit`), Roman Urdu (`makhsoos`/`khaas`/`aam` ×
`thana`/`thanay`/`thane`, `aam police station`) and Urdu (`خصوصی تھانہ`,
`مخصوص تھانے`, `عام تھانے`, `خصوصی یونٹ`, …).

**One rule governs every entry: the qualifier and the station word travel
together.** No bare station word (that is S2's question and CR6's opening
clause) and no bare qualifier — `عام` alone appears in KB5 qualifying a *case*,
which is the عام-class substring collision this file has recorded five times.

**The all-32 control is EQUALITY, not absence.** The full id → resolved-kind
map was captured from `resolve_aggregate_kind()` before a single keyword was
added, is pinned in `tests/test_xagg.py`, and is byte-identical after. An
absence-only control would have passed even if a new keyword pushed CR3 or KB4
sideways into a third family.

Nothing needed mirroring into `resolve_aggregate_kind()` — Module 41 already
made it the sole reader of this tuple. A new AST test pins that, so a second
inline check in `run_aggregate()` fails the suite rather than drifting.

The measured failure — *"Do the specialist units handle a bigger share of our
cases than the ordinary police stations?"* — now reaches
`station_caseload_by_specialisation`, as do 8 non-gold paraphrases across
English, Roman Urdu and Urdu. M2's literal gold text still reproduces gold's
**9 of 73 FIRs from 2 of 19 stations**, and Module 44's honest dissent from
gold's *growth* framing stands unchanged.

Live SSE verification **deferred for contention** (same conditions as Module
55). Module 44 had already recorded three consecutive live SSE runs of M2's
gold text on this family, and nothing between the router and the aggregate
changed here — only the keyword tuple.

**New defect raised as Module 58:** the tuple is now a 58-entry substring
list where this file's other hard dispatch calls use a multi-signal predicate.

Result: `docs/gold-qa-wave2-results/MODULE56_RESULT.md`.

---

# Module 57 — CR3 is non-deterministic across runs at three layers ✅ diagnosis closed; CR3 still unstable

**Done, with no code change of its own.** Branch
`fix/meta-analysis-reliability-53-57-40`. Full write-up:
`docs/gold-qa-wave2-results/MODULE57_RESULT.md`.

Re-measured over **20 consecutive runs** of CR3's literal gold text after
Module 53 landed, exactly as the brief instructed:

| Layer | Module 44 saw | Measured over 20 runs |
|---|---|---|
| both sub-queries timing out | 1 of 3 | **0 of 20** — removed by Module 53 |
| `route=None`, XNETWORK relevance gate | 1 of 3 | **0 of 20** — did **not** reproduce. Not fixed, *not observed*. `xnetwork.py` and its 0.145 cutoff correctly untouched. |
| synthesis not grounded | 1 of 3 | **7 of 14 on the shipped code** — the whole residue |

**CR3's route is stable after all.** Module 44 read "XAGG twice, `None` once"
as route instability; 20 runs give the same top-level route, the same
`record_consistency` plan match, the same three sub-queries and the same
`filtered_fir_listing` aggregate every time. The instability is entirely at
the last step.

**The residue has one reason, verbatim-identical on every rejection:** the
claim that FIR 65/26 is *absent* from the CMS linkage list is "inferred but
not directly supported". That is the verifier refusing **exactly what gold
asserts** — a negative inference over a complete listing. Measured, not
assumed, **not** to be citation attribution, so Module 40 does not fix it
either. Split out as **Module 61**.

**When CR3 answers it is right** — four of gold's five elements including
gold's exact `CMS-ISB-2026-0341`. The problem is the rate, not the content:
the rejections cluster in time (six consecutive at a uniform 64–66 s against
94–219 s everywhere else), so a four-run sample can land anywhere between 0/4
and 4/4 on unchanged code. That is the property this module was filed to name,
and it is still true.

**New defect:** the deterministic plans have a narrow lexical reach — a CR3
paraphrase saying "handled the same way" matches and answers 2/2, one saying
"equally complete" matches nothing and never reaches Meta-Analysis. Same
boundary for G6. Generalises Module 41's M4 finding; split out as
**Module 61**.

<details>
<summary>Original brief, kept for the record</summary>


**Found by:** Module 44's regression guard

Three consecutive live runs of CR3's literal gold text, on one backend, failed
three different ways:

1. `status=error` — "The synthesized answer could not be verified as grounded
   in the sub-answers."
2. `status=error` — "Could not answer sub-question (timed out)" for **both**
   of `record_consistency`'s sub-queries.
3. `route=None` — the XNETWORK relevance gate: "nearest cluster found was
   distance 0.156 against a relevance cutoff of 0.145".

Note the third: CR3 routed **XAGG** twice and **not at all** once, so its
route is not stable either.

**Ruled out as a Module 44 effect, by measurement.** Every `_SQ_*` sub-query
constant in `meta_analysis.py` was resolved through the new dispatch chain and
none changed family; `_SQ_CRIMINAL_RECORD_VS_COURT` still resolves to
`criminal_record_court_crosscheck`, and CR3's own two sub-queries still
resolve to `graph_recurrence_person` and `cms_fir_linkage` exactly as before.
CR3's gold text itself still resolves to `station_or_category_counts`.

**Why this needs its own module.** Module 36 deferred CR3's aggregate wiring
and Module 50 owns the G1/G6/CR3 consolidation, but both are scoped to
*completeness* — wiring the right aggregates into the right plans. Neither is
scoped to the non-determinism, and that is the part that matters for Module
27: a question whose route and failure mode change run to run cannot be scored
once and believed. Whoever takes this should run CR3 at least five times
before and after any change.

</details>

---

# Module 48 — KB2 and KB9: retrieval fixed, synthesis still wrong ⬜

**Both score FC 0.0 / AR 1.0.** Module 30 took the KB bucket from 3/8 to 7/8
*answering*, and the report confirms every KB question now routes to RAG and
returns 1,000–2,500 characters instead of abstaining. So the remaining gap for
these two is **answer quality, not retrieval** — which is a different kind of
work from Module 30's.

**KB9** needs CrPC **s.174** (inquest / cause-of-death duty when a person dies
by suicide, homicide or suspiciously). Module 30 reached it *indirectly*, via
Punjab Police Rules 25.31's verbatim cross-reference, and verified that text by
chunk id. Check whether the answer is reasoning from the cross-reference rather
than the provision itself, and whether that is why it misses gold.

**KB2** has no recorded diagnosis at all. Start from scratch with the
three-layer method.

**Read `MODULE30_RESULT.md` first** — it records the hallucination trap that
matters here: an evaluator once cited "section 174" when that text was **not**
in the chunks it was judging. **Verify chunk contents by id; never treat a
citation in an answer as evidence of retrieval.**

**Overlaps Module 39** (the "and does our data show it?" half of every KB
answer being unreachable from the RAG sub-agent). Decide early whether these
two are instances of 39 — if so, say so and fold them in rather than building
a parallel fix.

**Verify:** both live several times; the other six KB questions as a regression
guard (**KB4 is a known open regression, Module 38** — do not make it worse).

---

# Module 49 — KB3 quotes Article 18 but does not draw gold's conclusion ⬜

Module 30 succeeded at what it scoped: KB3's Police Order 2002 **Article 18**
chunk now reaches the pool and the answer quotes it. KB3 still scores **0.1**.

Module 30's own writeup is explicit that KB3 *"still won't draw gold's 'the law
expects separation' conclusion"* — and that the chunk **ends mid-sentence**,
with the answering words in the next one, which is what neighbour widening was
added for. Establish whether that widening is actually firing for KB3.

Gold's second half is the data comparison: the recording and investigating
officer are the same person in **68 of 74 pairs (92%)**, role-splitting in only
6 cases. That is a computable fact the RAG path may have no way to reach — see
**Module 39**. If so, KB3 cannot pass without 39, and this module's honest
outcome is to say that and stop.

**Verify:** KB3 live several times; confirm from chunk ids what the answer is
grounded in; the other seven KB questions as a regression guard.

---

# Module 50 — G1/G6/CR3 consolidation: the aggregates existed but nothing called them ✅

**Done.** Branch `fix/meta-analysis-wire-g1-g6-aggregates`. Full write-up:
`docs/gold-qa-wave2-results/MODULE50_RESULT.md`.

**The brief's diagnosis was right in every particular** and nothing had to be
re-derived. Confirmed on the base commit by dispatching each of the six
pinned sub-query strings individually through `/api/chat`: all six reached
their aggregate and returned gold's numbers, and all six were unreachable
from G1/G6/CR3. The defect was an **absent call**, not a wrong answer.

**Wired** — the six strings copied byte-identical from where Modules 31–36
pinned them in `tests/test_xagg.py`, with a test asserting both copies stay
equal:

| Plan | Sub-queries now |
|---|---|
| `record_consistency` (CR3) | **M36 filtered FIR listing** (placed first, as `[Document 1]`), person recurrence, CMS linkage |
| `orientation_note` (G6) | district spread, case mix by year, **M35 arrest rate**, reporting speed, weapon licence |
| `caseload_review` (G1) | **M31 age**, **M32 relationship**, **M33 seized property**, **M34 time-of-day**, person recurrence |

`caseload_review` was **re-composed, not extended**. Gold's G1 states its own
method — *"profile the accused, the victims, the property and the timing"* —
and its four findings are exactly Modules 31–34's four aggregates. Module
29's five scans answered a legitimate but different question, as its own
result file recorded.

**`_MAX_SUB_QUERIES` settled at 5, and the brief's framing of the cost was
wrong.** It is not primarily latency or model spend: `META_ANALYSIS_SUBQUERY_TIMEOUT`
(60 s) is a **single wall-clock deadline shared by the whole fan-out**, and
the shared model server serialises the sub-queries into a staircase, so the
deadline measures **queue position**. Measured live:

| N | Last sub-answer | Timeouts | Synthesis |
|---|---|---|---|
| 3 (CR3) | +25.0 s / +29.2 s | 0 / 0 | grounded |
| 5 (G1 baseline) | +56.7 s / +53.5 s | 0 / 0 | grounded |
| 6 (G6) | +41.1 / +57.7 / +58.1 s | 0 / 0 / **1** | last run **NOT grounded** |
| 9 (G1) | +55.4 s for 5 of 9 | **4** | 4 findings missing |

At N=9 two of the four killed sub-queries were the age and time-of-day
aggregates the wiring exists to reach. Raising the cap buys timeouts, not
coverage. The plan cap is now a separate constant (`_MAX_PLAN_SUB_QUERIES`)
from the LLM-decomposer cap, because a plan was previously **silently
truncated** by the LLM-path cap — a nine-entry `caseload_review` would have
dispatched only its first five with nothing saying so.

**Cost of holding the line, stated plainly:** G6 dropped `_SQ_GENDER` to fit
the arrest rate, so gold's "mostly men" element is no longer computed.

**Live:** every wired aggregate fires on **every** run — `caseload_review`
7/7 for all four kinds, `orientation_note` 6/6, `record_consistency` 3/3.
G1's clean run reports all four gold findings (24–49/mean 31.5; اجنبی 15 of
24; 13 forensic-lab / 7 heirs; time-of-day). CR3 now quotes gold's exact
`CMS-ISB-2026-0341` — Module 29 had `CMS-KHI-2026-0417`. G6 reports the
arrest rate in 3 of 4 runs, having reported it in none before.

**Honest caveats.** (a) On gold's G1 finding (4) the answer deliberately does
**not** say "flat across the day": Module 34 established that is a date-only
artefact (14 rows at 00:00:00) and the corrected reading is reported instead.
(b) G6's measured arrest rate is **1 in 6.6**, not gold's 1 in 9. (c) Several
runs degraded because three other backends (8012/8014/8015) were driving the
same model server concurrently, and two paraphrase runs died on provider
`503` / `429 quota exceeded` — reported, not worked around.

---

# Module 53 — the sub-query timeout measures queue position, not cost ✅

**Done.** Branch `fix/meta-analysis-reliability-53-57-40`. Full write-up:
`docs/gold-qa-wave2-results/MODULE53_RESULT.md`.

**The brief's diagnosis was right in every particular**, and was re-derived by
controlled experiment rather than inherited. With the deadline forced to 25 s
to make the latent pathology fire, G1 at N=5 on the base commit dropped **8 of
10 sub-answers across two runs** — and every one of them had its `XAGG <kind>`
line already in the log, so the aggregate had finished and only the paraphrase
was thrown away. The four lost sub-questions were exactly Modules 31–34's
aggregates. With the fix, the same experiment drops **0 of 10** and all five
aggregates reach the answer on both runs.

**The fix reuses `large_scale_aggregate.py`'s existing verifier-rejection
fallback rather than inventing a second one.** New `agents/_salvage.py`: a
`ContextVar` holding a mutable box, opened by `_dispatch_one()` *before* the
awaited task exists, so the task's copied context shares the same object and
anything written into it survives `wait_for()`'s cancellation.
`large_scale_aggregate.py` deposits `raw_summary_text` the moment
`xagg_tool()` returns OK, before the paraphrase call. On timeout the aggregate
is served raw, disclosed by caveat, and the fan-out is marked `PARTIAL`.
Deliberately narrow: a RAG/GRAPH timeout still fails, pinned by a test.

`META_ANALYSIS_SUBQUERY_TIMEOUT` **60 → 150** — derived from
`_MAX_PLAN_SUB_QUERIES` (5) × the measured ~12 s worst-case slot × 2.5 for
measured contention, not chosen by taste.

**At the shipped configuration: zero dropped sub-answers at N=5, 12 runs of
12** (G1/G6/CR3 ×4), routes stable on all 12. The only failures were the
synthesis verifier — Modules 57/40.

**Honest caveat: the machine was not fully quiet.** A sibling worktree's
backend held port 8015 throughout. And at the shipped 60 s default the timeout
never fired on this machine at all (0 of 6 baseline runs), so the defect is
real and reproducible on demand but **latent** here rather than continuous.

**`_MAX_PLAN_SUB_QUERIES` deliberately left at 5.** Module 50 made revisiting
it conditional on this module, and the prerequisite is now met — but salvage
degrades an answer's prose rather than removing the serialisation, and the
post-fix staircase at N=6..9 has not been measured. Split out as **Module 59**.

**New defects:** **Module 55** confirmed, with a measured list of which
aggregate families do and do not log; **Module 58** (the cap); **Module 59**
(M4 does not skip decomposition live).

<details>
<summary>Original brief, kept for the record</summary>


**Found by Module 50**, and the single change that would most improve
Meta-Analysis reliability.

`meta_analysis.py::_dispatch_one()` wraps each sub-query in
`asyncio.wait_for(..., timeout=META_ANALYSIS_SUBQUERY_TIMEOUT)` and
`asyncio.gather()`s all N at once — so every sub-query shares **one 60 s
wall-clock deadline starting at fan-out**. The shared model server does not
run them in parallel; it serialises them into a ~6–10 s staircase. A
sub-query is therefore killed for being **served last**, not for being slow.
Module 50 measured the last slot consuming 53–58 s of the budget at N=5 even
on a quiet machine, and failing outright under contention.

**The waste is specific and avoidable:** the `XAGG <kind>` log line is
present for every timed-out sub-query, so the aggregate had **already
computed**. What is discarded is only its LLM paraphrase.

**Work:** either dispatch in bounded batches so each batch gets a fresh
window, or — cheaper — serve the raw aggregate when a sub-query's paraphrase
times out, exactly as `large_scale_aggregate.py` already does when the
*verifier* rejects a paraphrase. `src/config.py`'s 60 s default was out of
Module 50's scope and should be revisited here.

**Verify:** G1/G6/CR3 several runs each with no other backend running;
confirm zero dropped sub-answers at N=5. **This is the prerequisite for
reconsidering `_MAX_PLAN_SUB_QUERIES`** — Module 50 measured three gold G6
elements that fit only if this is fixed first.

</details>

---

# Module 54 — the two provider-failure gaps Module 46 did not close ✅

**Found by Module 50, which hit both live.** Half of this was already found
and fixed independently while Module 50 was running: **Module 46** (prompted
by Module 42's transient Gemini 429) widened the eval preflight check to
`grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota"`, having established
that *"Groq says `rate limit`; Gemini says `RESOURCE_EXHAUSTED`"*. Module 50
reproduced exactly that failure independently, which is good corroboration
that it is real and recurring, and **that half needs no further work.**

Two residuals remain.

**(1) `503 UNAVAILABLE` is in neither grep.** Module 50 captured:

```
503 UNAVAILABLE ... 'This model is currently experiencing high demand.'
```

It is a transient capacity failure, not a quota failure, but it has the same
consequence and the same invisibility. Module 46's pattern should gain
`|UNAVAILABLE|503`, and `WAVE2_ORCHESTRATION_PROMPT.md` — which still
prescribes the bare `grep -c "rate limit"` for *live module verification*,
separately from the eval preflight Module 46 fixed — should adopt the
widened pattern too.

**(2) A failed cutover classification is silently invisible in the SSE
stream.** Both of Module 50's failures hit `src/main.py`'s
`Cutover classification failed, falling back to orchestrator.py` path, which
**changes which sub-agent answers the question** — the harness is skipped and
the legacy orchestrator re-routes. Module 50's G1 paraphrase went from
Meta-Analysis to Cross-Case Linkage's relevance-gate refusal purely because
of this, and nothing in the SSE stream said so. It is indistinguishable from
a routing regression unless you happen to read `backend.log`.

**Work:** widen the pattern in both places; then decide whether the fallback
should emit a visible SSE event (or at minimum whether module verification
should treat a `Cutover classification failed` line as invalidating that run,
the way a rate-limit line already does).

**Verify:** re-run any decomposing question with the pattern in place;
confirm a forced fallback is detectable without reading the log.

## Outcome — fixed on `fix/provider-failure-visibility` (`MODULE54_RESULT.md`)

**The pattern is now canonical in one place:**
`evaluation/gold32_score.py`'s `LOG_GREP_PATTERN` =
`rate limit|RESOURCE_EXHAUSTED|429|quota|UNAVAILABLE|503`. Three prescriptive
documents now cite exactly that string, and a test asserts they still do:
`HOW_TO_REPRODUCE_THIS_EVALUATION.md`, `WAVE2_ORCHESTRATION_PROMPT.md` and
`docs/gold-qa-wave2-results/MODULE27_PREFLIGHT.md`, plus this plan's own
"before any future run" line. Result files under `docs/gold-qa-wave2-results/`
were deliberately left alone — they record what was measured at the time.

**This section's premise was wrong on one point.**
`WAVE2_ORCHESTRATION_PROMPT.md` did **not** prescribe the bare
`grep -c "rate limit"` for live module verification. It prescribed **no log
check at all** — §4's five verification requirements never mentioned the
backend log. The fix there is an addition, not an edit, and it explains how
the bare form propagated: each module improvised it from
`HOW_TO_REPRODUCE_THIS_EVALUATION.md`.

**The fallback is now visible three ways:** an SSE event carrying
`cutover_classification_failed: True` (emitted first, so a mid-stream timeout
still shows it), a per-row field of the same name in
`gold32_pipeline_outputs.json`, and the pre-existing log line. The fallback
behaviour itself is unchanged.

**A third gap was found in the same place and fixed:**
`gold32_score.py`'s `_is_rate_limit()` matched `RateLimit`/`rate_limit`
case-sensitively, and Groq's actual message is `due to rate limits` — so the
code classifier missed the *most common* signature and charged a Groq throttle
to the 3-attempt null budget instead of the 8-attempt quota budget. Now
case-insensitive.

**Not fixed, tracked:** `src/llm/client.py`'s own `_is_rate_limit()` — a
different function, governing live retry and key rotation — still does not
treat a 503 as retryable. That is a behaviour change rather than a visibility
one and needs its own module with measured before/after retry counts.

**Live verification deferred**, plainly: `8016` and `8018` were both mid-wave
at 70.4% memory, and a forced fallback is exactly what the unit test already
drives through the real `/api/chat` route. Module 27's rerun gets the
end-to-end confirmation for free.

---

# Module 40 — M4's verifier rejection: **closed as superseded** ✅

**Closed as superseded**, which the brief named in advance as a legitimate
outcome. Branch `fix/meta-analysis-reliability-53-57-40`; the original branch
`fix/meta-analysis-m4-synthesis-verifier-rejection` is **left unmerged on
origin and should not be deleted**. Full write-up:
`docs/gold-qa-wave2-results/MODULE40_RESULT.md`.

Its source changes were merged in cleanly (`git apply --3way`, no conflicts),
judged on their merits, and verified against CR3 and G6:

- **8 live runs with the change applied (CR3 ×4, G6 ×4), all answered — and
  the attribution-blind second opinion never fired once.** It only runs after
  a first-pass rejection, and the first pass passed every time.
- **Measured directly instead**, off the live path, against the exact
  rejection CR3 produced six times, 3 repetitions each: the flag **does what
  it claims** (on a swapped-citation input the strict pass objects to the
  citation index, and the blind pass's objection moves off attribution
  entirely) but it **cannot rescue CR3**, whose rejection is a *negative
  inference over a complete listing*, not a mis-numbering — still
  `grounded=False` with the flag on, 3 of 3.
- **A hallucinated synthesis is still rejected**, with and without the flag,
  3 of 3, live — the brief's explicit requirement.
- **Ablation:** removing the synthesis prompt rule changed nothing (CR3 4/4
  either way); reverting the whole module gave CR3 3/4. The 2/8 block that
  made Module 40 look effective was a **temporal** cluster, not a causal one.

**Its `statute_vs_court_stage` plan was deliberately NOT taken**, even though
M4 turns out to still need something: a matched deterministic plan *vetoes*
Module 41's guard (`_xagg_answers_in_one_call()` returns False the moment
`_match_decomposition_plan()` matches), so taking it would force M4 to
decompose even on the runs where the router says XAGG — trading a measured
failure for a regression of a merged module. Split out as **Module 60**, which
should start from Module 40's branch rather than from scratch.

<details>
<summary>Original brief, kept for the record</summary>


**Branch:** `fix/meta-analysis-m4-synthesis-verifier-rejection` — pushed, two
commits, **never live-verified**. Real work: `meta_analysis.py` (+113),
`verifier.py` (+111), the decomposer prompt, 410 lines of tests, and a 530-line
result file.

**Why it was filed.** Module 24 measured M4 completing end to end on only **1
run in 5**, with three failures rejected by the Meta-Analysis synthesis
verifier — on a branch that already contained Module 25's merged fix. So PR #16
did not cover this case.

**Why its premise no longer holds.** Module 41 (PR #31) generalised the
supervisor guard: any query XAGG resolves to a specific aggregate now skips
decomposition. Measured on `main` @ `687c87a`:

```
M4  resolve=statute_court_stage_join   skips_decomposition=True
```

**M4 no longer reaches Meta-Analysis at all.** The code path this module fixes
is one its own question no longer takes. Module 41 solved M4 structurally, and
by routing rather than by hardening the verifier.

**Why the work is not discarded.** CR3 and G6 still genuinely decompose
(`skips_decomposition=False`, and their deterministic plans win ahead of the
guard at `supervisor.py:653`). **CR3 is measurably unstable** — across Module
29's runs it produced one refusal, one wrong companion FIR, one sub-query
timeout and one good answer. That is the same synthesis-verification territory
this module hardened, so the changes may well pay off there. Nobody has tested
that, because the module was never live-verified.

**Decision, 2026-09-08:** rebase onto `main` **after Module 50 lands** (Module
50 is editing `meta_analysis.py` now, and merging into a live dispatch path is
what caused a real worktree incident earlier today), then **re-target the
verification at CR3 and G6** rather than M4.

**Verify, when resumed:** CR3 and G6 live, several runs each — the instability,
not a single good run, is the thing to fix. Confirm a genuinely hallucinated
synthesis is still rejected; a verifier that passes everything is worse than
one that is too strict. Regression-guard M2, G2/G5 (Module 41) and M4 itself,
which must keep skipping decomposition.

**If CR3 and G6 turn out not to need it,** close this module as superseded and
say so — that is a legitimate outcome, as Module 21 established.

</details>

---

# Module 52 — the relevance gate cannot judge a roman-Urdu question against English statute text ✅

**Found while investigating Module 42.** This is what actually makes KB6
abstain, once its `route=None` is understood to have been a harness timeout.

The legal KB corpus is seven **English** statute books. Module 30 fixed the
cross-language asymmetry for *retrieval* (English statute hypotheses) and for
the *cross-encoder* (`cross_rerank_multi`). It was never fixed for the
**evaluator**, which is now the only component in that path still reading the
raw roman-Urdu question.

Measured at Layer 1 (`evaluate_relevance()` called directly, temperature 0.0,
`prompts/evaluator.txt` unmodified), **holding the chunk set constant and
containing gold's own statutory text in every cell**:

| | roman-Urdu | English |
|---|---|---|
| **compound** (gold KB6 shape) | **1/3 relevant** | **3/3 relevant** |
| **norm-clause only** | **0/3 relevant** | **3/3 relevant** |

English 6/6, roman-Urdu 1/6, on identical evidence. The live abstention rate
(4 of 5 runs) matches the 1/3 cell independently.

**This is not Module 39.** Deleting the "and does our data show it?" clause —
Module 39's whole subject — makes the verdict *worse* (0/3, the weakest cell).
It is also not Module 19b's compound rule, which is present in the prompt and
whose absence is not what fails.

**No cheap wiring change works**, all measured: the Module 30 statute
hypothesis passed as `rewritten_query` **1/3**, as both arguments **2/3**, the
live retry rewrite **0/3**, an English rendering appended to the original
**0/3**. Only a genuine English *question* reaches 3/3.

**The existence proof, and the sharpest evidence for the fix.** A non-gold
roman-Urdu paraphrase of KB6 that says **"zabt shuda pistol"** instead of
**"baramad shuda aslaha"** — one noun changed, to a word spelled identically in
English — **answers on both runs in 97.9s and 103.8s** and returns gold's
statutory half *in full*:

> "Firearms must be **packaged separately**, **unloaded with the safety on**,
> and **without live rounds** in the chamber, magazine, or parcel"

Those are the three specifics KB6's own gold wording never produces on any of
its five runs. So the corpus, retrieval, the reranker, the gate and the
generator are all capable; the failure is lexical. It is not that roman-Urdu
fails generically — it is that a roman-Urdu term sharing **no surface form**
with the English corpus fails, while a loanword passes. That also explains why
KB8 (whose vocabulary maps more transparently) survived Module 30 and KB6 did
not.

**Work:** on the legal-KB path only, give the evaluator an English rendering of
the question, mirroring what Module 30 did for the cross-encoder. Note
`prompts/evaluator.txt` is **not** the place — the compound rule there is
correct and the 2×2 shows it is not the failing part. The same rendering should
also reach retrieval, since the paraphrase shows the correct chunk
(`5_Forensics_guidelines_pdf_62ee00b3_c19`) is missed for the same reason.

**Expected side benefit:** a gate that passes on attempt 1 stops KB6 paying six
retrieve/rerank/evaluate rounds, collapsing its runtime from ~600s to ~250s and
removing the timeout pressure Module 42 had to work around at its source.

**Scope:** this gates the whole roman-Urdu half of the KB bucket, not just KB6.
Blast radius is the shared RAG path, which already carries an open regression
(Module 38, KB4), so this needs live re-verification of all eight KB questions,
not just the one it was found on.

**Artefact:** `evaluation/kb6_evaluator_language_experiment.json` — all five
experiments, every verdict and reason, including the negative results.

---

# Module 58 — `_STATION_TYPE_KEYWORDS` should be a predicate, not a 58-entry list ✅ DONE

**Found by:** Module 56, which is the module that made it 58 entries.

Module 56 widened M2's trigger vocabulary from 10 entries to 58 and proved,
with an all-32 equality control, that nothing else moved. That control is the
only thing protecting the widening — and it is only as broad as the 32
questions in it.

The soft spot is already visible in the tuple: `single type of crime` and
`one type of crime` are the two entries that **name no station at all**. They
are there because M2's own gold text is phrased that way ("set up for one
specific type of crime"), and none of the 32 collides with them. A question
like *"how many cases involve one type of crime only?"* would be pulled into
the station-specialisation family wrongly, and no test in the repository would
notice.

**Work:** replace the flat tuple with a **multi-signal predicate** — a
station-or-unit signal AND a specialisation-contrast signal — the shape
`_is_arrest_rate()`, `_is_criminal_record_local_gap()` and
`_is_weapon_statute_cooccurrence()` already use in this same file for exactly
this reason: so no single phrase can carry the dispatch alone.

**Deliberately out of scope for Module 56.** That module's brief was "widen
the vocabulary"; this is a behaviour change to the dispatch shape, with its
own regression surface, and it needs the all-32 equality control re-run plus
a set of adversarial non-gold phrasings that the current control does not
contain.


---

## Outcome (2026-09-09, branch `fix/m4-routing-and-station-predicate`)

**Done. The defect above is confirmed and fixed, and the all-32 control did not
move.** Full evidence in `docs/gold-qa-wave2-results/MODULE58_RESULT.md`.

**What shipped.** `_STATION_TYPE_KEYWORDS` is deleted;
`_is_station_specialisation()` replaces it — a station/unit **noun**
(`station`, `thana`, `\bunits?\b`, `تھانے`, `یونٹ`, `چوکی`) **AND** a
specialisation qualifier, in two tiers. Tier 1 names a station kind outright
and is free-floating. Tier 2 — the ordinary-language qualifiers — counts only
**directly on the station noun** (0–1 intervening words), which generates the
pairings Module 56 wrote out by hand and refuses the ones it did not.

**Two design points were measured rather than assumed, and both changed the
shape:**

- A naive `station-word AND qualifier-word` hijacks **KB9** (*"police ko …
  khaas tor par …"*) out of `graph_recurrence_person`. A bare
  `police`/`پولیس` is therefore **not** a station signal. This is an accepted
  narrowing relative to Module 56's `specialist police` entry, taken knowingly.
- `unit` is word-bounded — it is a substring of `opportunity` and `impunity`.

**The filed defect is real and is fixed.** Measured on the pre-fix tree, three
phrasings resolved to `station_caseload_by_specialisation` with no station
named at all: *"How many cases involve one type of crime only?"*, *"How many
FIRs cover a single type of crime?"*, *"Are most of our cases a single type of
crime, or a mix?"*. Live, the first now gets `total_count` and an honest *"the
data does not categorise cases that way"*, **2 of 2**.

**The all-32 EQUALITY control was re-run against the completely different
dispatch mechanism: identical, 32 of 32**, and exactly one of the 32 (`M2`)
matches the predicate. **14 out-of-gold adversarial negatives** were added, so
the guarantee no longer depends solely on those 32 questions.

**Live:** M2's gold text **3/3** to `station_caseload_by_specialisation`, one
route event and one dispatch each, the aggregate line identical every time.
Module 56's own measured failure phrasing **2/2**. Regression G3/CR7/G2/G5/M5
**2/2 each**, no family moved.

**Honest limit:** gold's headline "9 of 73 FIRs from 2 of 19 stations" reaches
the served answer on **2 of 3** runs. The aggregate is correct 3/3 — the loss
is in the paraphrase/verifier layer above XAGG, the same variance Modules 44
and 56 recorded, and it is filed as **Module 70** rather than papered over.

**New defects split out as Modules 69 and 70.**

---
---

## Outcome (2026-09-09, branch `fix/kb-evaluator-english-rendering`)

**Done. The defect above is confirmed and fixed; the KB bucket did not move.**
Full evidence in `docs/gold-qa-wave2-results/MODULE52_RESULT.md`.

**What shipped.** `render_question_in_english()` in
`src/pipeline/statute_hypothesis.py` plus `prompts/question_english.txt`
restate the QUESTION — not the provision — in English. On the legal-KB path
only (`_is_legal_kb_intent()`), `rag.py` folds it into retrieval and hands it
to `evaluate_relevance()` as both arguments on attempt 1. `None` on any
failure, so a dead call is byte-for-byte the old behaviour.
**`prompts/evaluator.txt` was not touched.**

**Layer 1, re-measured on this branch** with this module's own rendering,
chunk set held constant and asserted to contain gold's text: KB6 as gold asks
it **0/3 relevant**, the same question in English **3/3**. Module 42's finding
reproduces.

**The baseline moved, as the brief warned, and was re-measured rather than
inherited** — `main` @ `a841f5d`, KB1–KB9 × 3 runs: **18/24 answered**, mean
2.25 evaluator rounds. KB6 no longer shows Module 42's 4-of-5 abstention (it
answers 3/3); **KB9** now carries that signature instead (abstains 2/3, six
rounds, ~500s); **KB3 routed to XAGG on one run of three**, so any single-run
KB3 number is a coin flip.

**After: 19/24 answered, mean 2.33 rounds.** Thematic law-half coverage is
**14/24 in both arms** — consistency improved, correctness did not:

- **KB9 1/3 → 3/3** on the law half, abstentions gone.
- **KB6** answers 3/3 in both arms; its evaluator rounds collapse 3/2/1 →
  **1/1/1** and runtime 373/169/124s → 95/91/91s — the brief's predicted
  cross-check, confirmed.
- **KB4 2/3 → 0/3.** Isolated with a gate-only probe: retrieval still reaches
  gold's Punjab Police Rules **27.16** and the gate names it; the **verifier**
  refuses the generated answer. Downstream of this module, and the same
  constraint Module 38 flagged.
- **Mean rounds across all 24 runs is flat (2.25 → 2.33)**, because KB5 got
  worse (1.7 → 4.0) as much as KB6 and KB9 got better.

**Non-gold roman-Urdu paraphrase** (keeping the hard "baramad shuda hathyar"
vocabulary): answered **2/3 → 3/3**, mean rounds 5.0 → 3.0, and gold's three
statutory specifics ("packaged separately, unloaded with safety on, without
live rounds") appear on **2 of 3** after-runs against **0 of 3** before-runs.
This is the module's strongest evidence, and it also shows the limit: KB6's
*own* gold wording still does not reach the chunk that carries those words.

**Verdict on 48, 49 and 39 — none is subsumed:**

- **Module 39: still needed, unchanged, and now the largest single cause of KB
  failure.** **0 of 48 runs** across both arms produce gold's data half.
- **Module 48: half advanced, half re-diagnosed.** **KB9** was not a synthesis
  failure at all — it was abstaining; Module 52 fixed that and it now names
  CrPC s.174 with Rules 25.31/25.35, grounded in CrPC chunks by id, so its
  residue is Module 39. **KB2 did not move (0/3 in both arms)** and now has the
  diagnosis Module 48 says it lacks: gold rests on Qanun-e-Shahadat Arts 38/39
  and CrPC s.162, and all six runs answer from CrPC s.161 / case diaries
  instead — a Module-30-shaped **retrieval** gap, not a synthesis one.
- **Module 49: still needed, unchanged, and should be sequenced AFTER Module
  39** — its own stated honest outcome ("KB3 cannot pass without 39") is now
  supported by measurement (gold's 68-of-74 half unreachable on 24 of 24 runs).
  It must also account for KB3's router instability.

**New defects, split out rather than folded in — Modules 58–61 below.**

---

# Module 63 — `_is_legal_kb_intent()` misses an ordinary Roman-Urdu paraphrase ✅ DONE

**Found by Module 52, measured directly.** A plain Roman-Urdu rephrasing of KB6
that says *"forensics ke usoolon"* instead of *"forensics guidelines"* returns
**False** from `_is_legal_kb_intent()`. It is therefore routed to the mixed
FIR-narrative pool and **every KB fix is silently skipped** — Module 8c's
KB-only scope, Module 30's statute hypotheses, Module 38's RRF fusion and
Module 52's English rendering. Measured live, 3 runs each arm: no rendering
generated, and every chunk the evaluator judged was a
`psrms_fir_fir-…#narrative` chunk.

The gate's patterns key on English/loanword surface forms, so a question that
uses ordinary Urdu vocabulary for the same concept falls through. **This sits
ahead of Modules 30/38/52 in the path and gates the whole Roman-Urdu half of
the KB bucket** — it should be the next module in this line.

**Verify:** the gate function directly over a set of Roman-Urdu and
Urdu-script paraphrases; then live, that a paraphrase reaches the KB corpus.
The eight gold KB questions as a regression guard — widening this gate must not
pull case-data questions into the KB-only scope.

> **Closed by Module 78**, in whose causal chain it sits: after that module the
> router routes on this gate, so a False here is a whole wrong route and not
> just a wrong corpus.
>
> **Re-measured before touching it, and it is worse than this section says —
> the miss is FOUR of eight, not one.** One ordinary paraphrase per KB
> question: KB2, KB4, KB5 **and** KB6's Roman-Urdu rewordings all returned
> False. The cause is surface form, not concept: `\busool\b` cannot see
> **"usoolon"**, the ordinary oblique plural and the actual word in Module 52's
> own KB6 paraphrase.
>
> Three widenings, each keeping the existing two-signal AND: the Roman-Urdu
> norm group goes from seven fixed word forms to their inflected forms (plus
> "muqarrara"/"baqaida", the Roman-Urdu twin of the Urdu-script
> `(باقاعدہ|مقررہ)` pattern already in the list, and "miyaar"); the
> witness/accused statement-recording pattern gains its Roman-Urdu and
> Urdu-script forms; and the two named corpora gain "forensics ke usoolon" and
> its Urdu-script form, still requiring the corpus name AND its
> principles/rules word. **After: 8 of 8.**
>
> **One over-widening was caught by this module's own negative control and
> narrowed rather than shipped.** Bare "tareeqa" is both *procedure* and
> *manner*: *"cases kis tareeqe se station ke hisaab se bante hain"* is a plain
> data question and passed the AND with "hamare record" in the same sentence.
> The interrogative form is now excluded by lookbehind, the declarative kept,
> and the failing string pinned as a test.
>
> **Regression guard, as this section required:** the all-32 gate equality
> control is **identical before and after** — exactly the eight KB questions
> pass and the other 24 do not — nine non-KB paraphrases stay False, and the
> case-anchor refusal is re-asserted. `tests/test_harness_tool_rag.py` 41 → 46.
> Live, KB4/KB5/KB6/KB8 all still reach RAG and fire their plans (2/2, 2/2,
> 2/2, 3/3). See `docs/gold-qa-wave2-results/MODULE78_RESULT.md` §2.2 and §6.

---

# Module 64 — KB6's `c19` window is still not retrieved from KB6's own wording ⬜

**Found by Module 52.** The brief predicted that giving retrieval the English
rendering would surface `5_Forensics_guidelines_pdf_62ee00b3_c19`. Measured, it
does not: all three post-change runs land on `_c116` and `_c0`, and the
baseline reached the answering text only by *stumbling onto* `_c18` on 1 run in
3 during a retry. Because Module 52 makes the gate pass on attempt 1, those
retries are gone — KB6's answer rate is 3/3 either way, but its statutory
specifics went from "1 in 3 by luck" to "0 in 3 reliably".

Note the discriminating evidence: a **paraphrase** of KB6 does reach that text
(2 of 3 runs), so the corpus, the widening and the generator are all capable.
It is KB6's own phrasing that misses. **A gate fix cannot compensate for a
retrieval miss.**

**Verify:** chunk ids, never citations. KB6 live several times; the other seven
KB questions as a regression guard.

---

# Module 65 — KB2's gold rests on statutes no run ever retrieves ✅ DONE

**Found by Module 52; this is the diagnosis Module 48 records as missing for
KB2.** Across **6 live runs in two arms**, KB2's thematic coverage is **0/6**.
Gold's answer rests on **Qanun-e-Shahadat Order 1984 Arts 38 and 39** (a
confession to a police officer is not evidence) and **CrPC s.162** (a
police-recorded statement is barred at trial), concluding that the missing
statement text is *by design, not a gap*. Every run instead answers from CrPC
s.161 and Punjab Police Rules 25.54's case diaries, and several conclude the
**opposite** of gold — that the system does have recording mechanisms.

So KB2 is a Module-30-shaped **retrieval** gap on a statute book Module 30's
own probe never targeted, not the synthesis failure Module 48 assumes.

**Verify:** probe the corpus for the Qanun-e-Shahadat Art. 38/39 and CrPC s.162
chunks by id first — establish they exist before building anything.

## Outcome (2026-09-09, branch `fix/kb2-retrieval-qanun-shahadat`)

**Done, and this row's diagnosis was right in every particular.** Full evidence
in `docs/gold-qa-wave2-results/MODULE65_RESULT.md`. Measured against a **private
Chroma copy** at `D:/Rapids AI/muhafiz-m65/data/chroma_db`; the shared store was
never read from and never written to.

**The probe answered the ingestion-or-retrieval question before any code was
written, which is the part worth keeping.** All three provisions are in the
corpus, verbatim, by chunk id:

- Art. 38 — `2_qanun-e-shahadat-order-1984_pdf_3e604153_c176`
- Art. 39 — `…_3e604153_c177` (Explanation in `_c178`)
- CrPC s.162(1) — `1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_f9908363_c675`
  (heading `_c673`, proviso `_c677`)

And **nothing the pipeline generated reached them.** At the KB-only scope,
top-30, the gold question, its English rendering and both live statute hypotheses
returned Art. 38 and Art. 39 **absent** in every case, while a control query
carrying Art. 38's own wording returned it at **rank 1**. Module 30's "a bare
statute name retrieves nothing" reproduced exactly: *"Qanun-e-Shahadat Order 1984
Article 38"* alone returns none of them.

**The fix is one file: `prompts/statute_hypothesis.txt`.** The prompt had three
book-selection branches (a STEP, WHO in the force, WHAT IS WRITTEN DOWN), and a
question about whether material may be **used** fell into the third. A fourth
branch, an enlarged Qanun-e-Shahadat map entry, and rule 4c now route it to the
evidence book. Nothing in `src/retrieval/` or `src/pipeline/harness/tools/rag.py`
was changed — the pool machinery was never the failing part.

**Three things had to be measured rather than assumed, and each changed the fix:**

- **Vocabulary decides it.** *"may not be used as evidence"* put Art. 38 at rank
  88; *"shall be proved as against"* put it at rank 3. The Qanun-e-Shahadat's noun
  is *confession*, not *statement*, and the rule now says so.
- **Rule 4c had to be prohibitive, not advisory.** An advisory version reached
  Art. 38 at rank 3 on its own query and still retrieved **0 of 2** gold chunks
  live, because its second hypothesis was the case diary and one such query pulls
  the whole reranked window back.
- **Enlarging the CrPC's map entry was reverted on a measured regression.** It
  fixed KB2 and **hijacked KB5's first hypothesis** off the Anti-Rape Act (5/5
  answering → 1/3, ~45 s → 97–172 s). A unit test now pins that entry.

**Live, 3 runs each arm, `route=RAG` throughout:** KB2 goes **0/3 → 3/3** for
Art. 38 and **0/3 → 3/3** for s.162, and **1/3 → 3/3** answering. A Roman-Urdu
non-gold paraphrase goes **0/3 → 3/3 on all three provisions** and reaches gold's
conclusion in its own words.

**What it does NOT fix, stated plainly.** KB2 still misses gold's conclusion.
With Art. 38 in the window on 3 of 3 runs, the generator quotes s.162's bar
correctly and then hedges — *"which may be considered a limitation or data gap"* —
citing **Module 37's orphaned `…_0519abd8` s.164 chunks** instead. The residue is
**generation, not retrieval**, and it is the honest successor to Module 48's KB2
half. Filed as **Module 82**.

**Module 38's open `DEFAULT_HYPOTHESES` question is answered and closed here.**
n=3 is affordable under RRF, as Module 38 predicted, and buys nothing measured on
the question with the most to gain from it. It stays at 2, with the measurement in
the code comment replacing the obsolete reason.

**Regression.** Equality control on the hypothesis step, 3 runs per question per
arm: **6 of 8 book-pairs unchanged** (KB2 intended; KB9's second slot drifts, and
KB9 no longer routes to RAG anyway). Live: KB1, KB5 and KB6 unchanged; KB3 and
KB9 byte-identical because they no longer take this route at all. **KB4 answers 0
of 5 on the new prompt against 3 of 5 on the old** — a reverted-prompt control
measured KB4 flipping on its own hypothesis wording with the prompt *unchanged*,
so this is its documented verifier flap, with the same book and the same
rule-27.16 chunk in the window either way. It is reported as a worsening rather
than explained away.

**Also found, and not this module's rows to edit:** on this `main`, **KB3 routes
to XNETWORK and KB9 to XAGG**, 2/2 in both arms — so **Modules 48/49's KB3 and KB9
work now aims at a route those questions no longer take.**

---

# Module 82 — KB2 answers from the recording provision and never from the bar ✅

**Branch:** `fix/kb-downstream-generation` · **Result:**
`docs/gold-qa-wave2-results/MODULE82_RESULT.md`

**Filed by Module 65 as "KB2 hedges against the provision it was just given".
Re-measured on `main` @ `6cf89fb` against the SHARED Chroma, the failure is one
step earlier and worse, and the module's own premise is only half true.**

**What reproduced.** KB2's window is identical on 3 of 3 runs, five chunks, and
**two of them (`…_0519abd8_c590`, `…_c601`) resolve to nothing in Chroma** —
Module 37's orphans, confirmed by id on the shared store rather than inherited
from a private copy. **The answer's lead citation is one of them, 3 of 3**, and
it concludes *"it is not a data gap; the system does maintain such records"* —
the opposite of gold on the facts, while landing on gold's words for the wrong
reason. **CrPC s.162(1) is at window position 1 on every run and is never cited
for its bar.**

**What did not.** Module 65 measured QSO Art. 38 reaching the window 3 of 3;
here it is **0 of 3** for gold's literal wording and **3 of 3** for Module 65's
own Roman-Urdu paraphrase — which also reaches gold's conclusion 3 of 3 **with
no code change at all**. So the capability is present and it is gold's phrasing
that loses the chunk. Split out as **Module 97**.

**The generation fix was written, measured, and deleted.** Two rules in
`semantic_search.py`'s prompt (one scoping a claim to the document that states
it, one forbidding a conclusion from being walked back) plus Module 86's
denominator clause were shipped to a live arm: KB2 stayed at 0-of-N on gold's
conclusion, the 45 stayed at 0-of-N, and **KB4 and its paraphrase each went 3/3
→ 1/3 answering with four new verifier rejections** — every one of them the judge
correctly refusing a *compliance* claim the rules invited. `src/` on the branch
is byte-identical to `main`. Same posture as Module 71's deleted figure roster;
the wording and the numbers are in the result file so the next module does not
spend the runs again.

**The hard constraint holds, proven three ways.** A forced control on the RAG
path (`scripts/module82_forced_hallucination_control.py`, the Module 71 harness
moved to this sub-agent) had the real `verify_grounding()` reject an invented
rule 27.41(3), an invented FIR 512/26 and a *"fully compliant"* conclusion **3 of
3**, with no marker reaching the served text; the four rejections above are the
same guard firing unforced; and the shipped-code regression bucket carries a
fifth, CR3 run 2's refusal of a **fabricated negative** in exactly the direction
Module 61 declines to rescue.

**Also found, and not fixed here:** `verify_grounding()`'s four deterministic
pre-checks are **all inert on the global legal-KB corpus** — no chunk carries a
`case_id`, so `_check_fabricated_case_ids()` returns early by design, RAG
computes no per-chunk confidence so `_check_hedging()` never fires, and a fluent
fabrication is neither a refusal nor uncited. On KB questions the LLM judge is
the only guard, which is why the proof above had to be live. And **Module 71 §8
files a different defect under this same number 82** (G6's *"cites no [Document
N]"*), which fired unforced here and was absorbed by Module 71's `PARTIAL`
fallback; flagged, not edited, since it is Module 71's row.

---

# Modules 85 and 86 — KB4 answers from the neighbouring subject, and drops the denominator ✅

**Branch:** `fix/kb-downstream-generation` · **Result:**
`docs/gold-qa-wave2-results/MODULE85_RESULT.md` (86 folded in — one attempted
change covered both and the evidence is the same six runs).

**Module 85 did not reproduce as a rejection.** KB4 is `status=done` **3 of 3**
and the whole shipped-code arm — 12 primary plus 17 regression runs — carries
**zero** Semantic-Search verifier rejections. Module 38 saw 2 of 3 rejected;
Module 61 saw 5 of 5 answering and warned the 1-of-3 figure should not be
treated as fixed. It still should not.

**The defect underneath it reproduced 3 of 3.** KB4's window holds
`4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2197` (*"…custody of the police for over
three years… This register is a permanent record"*) at position 2 and `…_c2421`
(Form 27.18(1)'s own column list) at position 4 — gold's substance, text read out
of the store by id — and the answer is built entirely on three
Anti-Rape/Forensics **packaging** chunks at positions 1, 3 and 6. It then reports
that *"the records do not show any entries that explicitly mention the marking,
packaging, or chain of custody"*: a false negative manufactured by answering a
question nobody asked. That is Module 38's recorded rejection reason
(*"incorrectly attributes alignment with the Anti-Rape Act to the property record
system"*) one step earlier, now passing the judge instead of being caught by it.

**Module 86 reproduced 6 of 6, and the honest finding is where it is not.**
`render_seized_property_disposition()` opens the chunk with *"What happens to
seized property — **45** property-register entr(ies) across 28 FIR(s)"*, so the
total is the **first number in the first line** the generator is handed, every
run. It reproduces the 28, the nine dispositions and gold's 13-forensic /
7-heirs pair, and states the 45 on **0 of 6**. Not an upstream rendering gap;
`xagg.py` needs no change. Module 39's rule is already satisfied — a breakdown is
a faithful *subset* of a summary — the same scoping miss Module 71 found in its
own rule 2.

**Both fixes were measured and reverted.** The prompt clause naming the
denominator explicitly produced the 45 on **0 of 5** runs, and the subject rule
aimed at the over-attribution made it worse: KB4 **3/3 → 1/3** and its
Roman-Urdu paraphrase **3/3 → 1/3**, with four rejections whose common cause is
an unsupported *compliance* verdict the rules invited. The positive control
regressing is what made the revert unambiguous.

**Not a reasoning limit.** Module 38's paraphrase, shipped code, 3 of 3, gets
gold's norm half exactly — *"Rule 27.16 … register of case property and
unclaimed property in Form 27.16(1) … may be destroyed three years after being
completed"* — from an all-Punjab-Police-Rules window including `…_c2209`, which
gold's own wording gets 0 of 3. That split is **Module 97**. The same paraphrase
still drops the 45 on 3 of 3, so Module 86 is independent of it.

**Left for the next attempt (§8f of the result):** the measurement that would
actually discriminate on Module 86 is whether the model states the total when
the nine-row breakdown is removed — a `rag.py`/`xagg.py` rendering question, not
a `semantic_search.py` one. A third restatement of the same prompt instruction is
the least likely thing to work.

---

# Module 97 — gold's own wording loses the provision its paraphrase retrieves ⬜

**Found by Modules 82/85**, on `main` @ `6cf89fb`, against the **shared** Chroma,
reranker live, 3 runs per question, `route=RAG` on all twelve.

| | gold's literal wording | its non-gold paraphrase |
|---|---|---|
| **KB2** — QSO Art. 38 `2_qanun-e-shahadat-order-1984_pdf_3e604153_c176` | **0 of 3** | **3 of 3** |
| **KB2** — QSO Art. 39 `…_c177` | 0 of 3 | **3 of 3** |
| **KB4** — rule 27.16(1) `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2209` | **0 of 3** | **3 of 3** |

Module 65 measured KB2's Art. 38 at 3 of 3 and Module 38 measured KB4's
`…_c2209` at 3 of 3 — both against **private** Chroma copies on earlier
merge-bases. Neither reproduces here for the gold question; both reproduce for a
paraphrase, same build, same store, same session. So this is not "a fix
regressed" in general: **it is gold's own phrasing that loses the slot, on two
independent questions, in the same direction.**

Book selection still works — KB2's hypothesis 1 names the Qanun-e-Shahadat and
uses its verb *"shall not be proved"* on every run. What varies is the section
number and surrounding wording the model attaches, and Module 65 §7a already
measured that variation alone flipping a KB4 outcome on an unchanged prompt. The
window is not fully deterministic either: a control run on the same question and
the same code returned `…_c2209` at position 2.

**This is Module 92's shape one layer down.** Module 92 measures the *router*
losing a question's route on a paraphrase; here the route is identical on all
twelve runs and it is the **retrieval window** that the paraphrase changes. The
narrow-neighbourhood problem is therefore not confined to `router.py`, which is
worth knowing before 92 is scoped as a router fix.

**Verify:** rank-by-query for gold's wording and the paraphrase side by side
(`scripts/module65_probe.py`), then the live window by id
(`scripts/module82_window_probe.py`, read-only), several runs each because the
window moves. Module 65 §1.4 — one off-target hypothesis pulls the whole window
under RRF — is the place to start. Owner needs `src/retrieval/` and/or
`prompts/statute_hypothesis.txt`.

---


# Module 61 — the English rendering can narrow a question's scope ⬜

**Found by Module 52, in its own output.** KB5's *"عورت پر تشدد"* (violence
against a woman) is rendered as *"**domestic** violence against a woman"*,
against `prompts/question_english.txt`'s explicit rule 4 (preserve scope, add
no term the question did not use). KB5's evaluator rounds also worsened, mean
**1.7 → 4.0**, which is the most likely explanation — a lead, not yet a
measurement.

**Verify:** render all eight gold KB questions plus a set of paraphrases and
check faithfulness clause by clause; then KB5 live several times against the
rounds figure above.
# Modules 89–91 — the three questions that had no owning module ⬜

**Diagnosed 2026-09-09 from Module 27's captured three-pass run**, not from
fresh guesses: each of KB1, M1 and CP6 fails consistently (never 1-of-3 luck)
and none had a module. Every figure below was re-derived against the live
graph.

---

# Module 89 — KB1: the compound question's data half ✅

**KB1 scores 0.30 on all three passes**, `route=RAG`, AnswerRelevancy 1.00.

**Question:** *"What legal requirement governs how a report of a crime becomes
a formal FIR, **and does our recordkeeping actually follow it?**"*

The answer gets the law right — CrPC ss.154/155, "recorded in writing by the
officer in charge" — and then says the documents do not say whether our
recordkeeping follows. The judge's reason names exactly that: *"fails to
address whether the recordkeeping follows it."*

**This is Module 39's defect on a question Module 39 deliberately excluded.**
Its result file records KB1 and KB2 as "deliberately unwired — their data
halves are schema claims, not counts". That was a reasonable call for KB2,
whose gold answer is *"no, by design"*. It is wrong for KB1, whose gold asserts
a **checkable** claim about the register.

**Work:** determine what KB1's data half actually needs (whether FIRs carry the
s.154 elements — written record, read back, signed, registered), whether any
aggregate can express it, and either wire a `_KB_DATA_HALF_PLANS` entry or say
plainly that the schema cannot support it. **If the schema genuinely cannot,
that is gold's problem** — gold asserts our records *do* follow, and under the
stated standard an honest "the schema does not record this" would then be the
correct answer and gold needs correcting, as G1, G6 and KB9 already were.

**Closed by Module 89** — see `docs/gold-qa-wave2-results/MODULE89_RESULT.md`.
The answer to the question this section poses is **both**: three of s.154's
elements have a structural equivalent and two do not, and the honest answer says
so in one breath rather than choosing.

**The diagnosis filed here was right, and gold needed no correction.** Gold had
already been fixed in PR #57; this module's independent derivation reproduces it
exactly — 73 cases, complainant **73/73**, recording officer **70/73**, report
timestamp **64/73**, with gold's "3 missing" and "9 missing" both confirmed and
the gap case ids named. **One correction to the brief's schema sketch:**
`INVOLVED_IN` runs **Person → Incident**, not Person → Case. Written the filed
way the complainant query returns **0**; the case is reached through
`[:PART_OF]`, which is what makes the three counts share a denominator rather
than happen to agree.

**There was no aggregate to wire, so one was built.** `fir_register_completeness`
in `xagg.py` — four Cypher reads, one shared renderer imported by all **three**
hand-copied XAGG rendering sites (Module 91 de-duplicated the placeholder-officer
family, not this one), dispatched by a two-signal predicate placed **immediately
above** G1/G2's `case_completeness_scan`. `_KB_DATA_HALF_PLANS` entry **(7)**
lands it, checked **last** because FIR/register vocabulary is the broadest in
that tuple, so no earlier entry's claim can change.

**Gold's value is the honest "mostly, with these gaps", and the renderer says
that** — the verdict, the three coverage figures, both gap lists, the 61/73
intersection, and the two s.154 steps (read back to the informant, signed by
them) that have **no field anywhere in this schema**, reported as *unmodelled*
rather than *missing*. An unrecorded step is not a skipped one, and gold does
not claim it is.

**Regression, because this inserts into two ordered first-match-wins chains.**
The all-32 dispatch equality control is **byte-identical — 0 of the 32 move**;
`resolves_to_specific_aggregate()` (the value routing consumes) is identical
main-vs-branch for all 32; `router.py` and `supervisor.py` are untouched; the
plan table moves **exactly one** question. Three first-draft over-matches — D1,
CR6, and **G5**, whose Roman-Urdu gold says *"record keeping"* about a **weapons**
register — were caught **by** that control rather than by inspection, and each is
pinned individually. Live: 12 regression questions, **0 route changes
attributable to this module** (KB3 XNETWORK→RAG and KB9 XAGG→RAG are **Module
78's**, and both now fire Module 77's plans and Modules 74/76's aggregates — the
first live confirmation that the 74→77→78 chain composes).

**Live result.** KB1 on `:8089`: **`route=RAG` 3/3, plan fired 3/3, both halves
composed 3/3**, answers byte-identical to each other, every figure gold asserts
present plus the named gap case ids. The pre-fix arm was equally deterministic
and carried **no figure at all**. Of four **pre-registered** paraphrases
(committed before the first run), **three route RAG and fire the plan 2/2 each**
— including the Roman-Urdu and the Urdu-script one, where Module 92 measured
Roman-Urdu keeping its route 0 of 8. **0 quota lines over 25 live runs.**

**Two things are reported rather than fixed.** One pattern was widened **after**
a pre-registered paraphrase missed it, not before — Module 56's finding for the
fifth time, and recorded in the code comment as such. And the fourth paraphrase
never reaches RAG at all: Module 15's CR6 `complaint … FIR` override claims it,
which is **Module 93 reproduced live on a second, independently written KB1
paraphrase**. The RAG-side half is already closed — the plan matches it offline —
so it fires the moment that route is fixed. New defect **100**.

---

# Module 90 — M1: act-level answer to a section-level question ✅

**M1 scored 0.40 / 0.70 / 0.40 — 1 of 3**, `route=XAGG`. Fixed by Module 90;
see `docs/gold-qa-wave2-results/MODULE90_91_RESULT.md`.

Gold contrasts **2024 (13 FIRs, every case armed robbery: PPC 34, 392, Arms
Ordinance §13, each 13/13)** with **2026 (51 FIRs, diversified: PPC 34 ×23,
392 ×8, Arms Ord ×16, CNSA §9(c) ×12, PECA §14/§21 ×9 each…)**.

Three defects were filed here. **Two were real; one was not.** Corrected
diagnosis, each point measured live rather than read off the captured answer:

1. **Act vs section — REAL, and now fixed.** `_statute_mix_by_year()` read
   `cases.crime_category`, which `muhafiz_cases._crime_category()` reduces to
   a comma-joined act list, discarding `section_code`. It could not
   distinguish PPC 302 from PPC 392, which is what "what KINDS of cases"
   asks. Module 24 found this and explicitly declined to take it. Module 90
   takes it: the aggregate now also reads
   `StructuredRecord{record_type:'fir_section'}` (`act` + `section_code`),
   the identical read Modules 23, 24 and 76 already perform, and returns
   `section_counts` alongside the unchanged act-level `counts`.
   **Correction to the row count stated in this plan:** the corpus holds
   **218 `fir_section` rows in total**, not "273 for 2024 and 979 for 2026".
   Counted per CASE (de-duplicated, since the corpus holds repeat rows for
   the same case/act/section), the derivation reproduces gold **exactly** —
   2024: PPC §34 13, PPC §392 13, Arms Ord §13 13 over 13 FIRs; 2026: PPC
   §34 23, Arms §13 16, CNSA §9(c) 12, PECA §14 9, PECA §21 9, PPC §419 9,
   PPC §420 9, PPC §302 8, PPC §392 8, PPC §365-A 5, DV 4, over 51 FIRs.
2. **"Only one year" — NOT REAL.** This plan says the answer "omits 2024
   entirely". It does not. Re-read from
   `evaluation/gold32_pass{1,2,3}_outputs.json`, all three captured M1
   answers contain an explicit *"In comparison, in 2024, the cases included:
   PPC: 13 cases, Arms Ordinance 1965: 13 cases"* block. The 2024 bucket was
   produced, rendered and paraphrased correctly throughout. No fix was
   needed and none was made.
3. **The wrong expansion — REAL, and now fixed.** The answer rendered *"PPC
   (Preventive Detention and Control Act)"*; PPC is the **Pakistan Penal
   Code**. Grepped repository-wide: that phrase occurs **only inside the
   captured evaluation outputs** — nothing in the code, prompts or data ever
   wrote it. It is a model invention filling a gap in the evidence, which
   said only "PPC: 39". The fix is to close the gap rather than to hunt a
   non-existent code bug: the renderer now spells the act out in the document
   the model is told not to alter ("PPC (Pakistan Penal Code)", likewise
   CrPC, CNSA 1997, PECA 2016).

Also added while there: a per-year **FIR total** on each bucket, since gold's
own framing is "2024 (13 FIRs)" vs "2026 (51 FIRs)" and an act count is not
an FIR count (a case carrying two acts contributes twice).

---

# Module 91 — CP6: the diagnosis below was wrong; gold was corrected instead ✅

**CP6 scored 0.30 on all three passes**, `route=XAGG`. Closed by Module 91;
see `docs/gold-qa-wave2-results/MODULE90_91_RESULT.md`.

**The text this plan previously carried was wrong and is corrected here.** It
said gold's 11 / 8 ASI / 3 SI was right, that the aggregate "reports 10 where
the two placeholder classes total 11", and that it "emits only a total where
gold's value is the split". All three are false:

- **The aggregate is correct.** There are 11 `ASSIGNED_TO {role:'investigating'}`
  placeholder edges over 11 distinct cases, but one of them — on `fir-205-26` —
  carries `superseded_by`, and that case now has a real named officer
  (سلمان). So **10** cases still carry a placeholder (7 ASI + 3 SI) and 11
  ever did. `_placeholder_officer_count()` returns exactly that. There is no
  double-counted case.
- **The renderer already emits the split.** It has emitted
  `7 marked "(نامزد ASI)", 3 marked "(نامزد SI)"` plus an ever-vs-current
  caveat since Module 7, at all three XAGG rendering sites, unchanged at the
  evaluation commit.
- **Gold was the thing that was wrong**, and was corrected to 10 / 7 / 3 in
  PR #57 (`fix/gold-cp6-kb1-measured`).

The remaining question — why the captured answer dropped the split the
renderer had produced — **does not reproduce**. Re-run live on both
generation providers with the identical prompt and rendered text, the split
survives paraphrasing 5/5 on the local slot and 3/3 on the cloud fallback.
The obvious hardening (breaking the sentence into a headline plus one bullet
per figure) was implemented, measured, and **reverted**: it made the omission
strictly worse, 0/5 local and 0/3 cloud, because a headline bullet gives the
model something it can answer "kitne" with and stop. Module 91 therefore
ships **no behavioural change** to CP6 — only the de-duplication of the three
hand-copied renderings into one shared `render_placeholder_officer_count()`
(byte-identical output, proven over all 32 gold questions) and two regression
tests pinning the split into the rendered evidence.

**Still open:** gold's closing comparison — *"the busiest named officers
(Faisal and Tariq) at 4 FIRs each"* — has no aggregate behind it and the
answer does not make it. Filed as a known gap, not taken by Module 91.

---

---

# Module 92 — the router generalises unevenly across languages 🔶 partially closed

**Branch:** `fix/router-language-generalisation` ·
**Result:** `docs/gold-qa-wave2-results/MODULE92_RESULT.md`

Three modules each found the same thing on their own question and filed it as a
one-off: **Module 62** (CR3/G6), **Module 78** (KB3/KB9), **Module 88** (CR2).
It is not a one-off. It is the router's design working as built.

**Status: the measurement is done and committed; the defect is NOT fixed.**
What shipped is a latent-path bug fix with a measured zero blast radius. The
language gap needs a cloud classifier, and that is blocked on quota, not code.

## What was measured

`evaluation/gold32_paraphrases.json` — **96 paraphrases**, one English, one
Roman-Urdu and one Urdu for each of the 32 gold questions, all written before
anything was run and none adjusted afterwards.
`evaluation/router_paraphrase_harness.py` scores any candidate over 32 gold +
96 paraphrases, by language. Both are committed and reusable.

| candidate | gold 32 | paraphrases 96 | en | roman-ur | ur | miss latency |
|---|---|---|---|---|---|---|
| deterministic layer only | 28 | 22 (23%) | 19% | 25% | 25% | 0.02 s |
| **shipped (local Qwen3-14B)** | **29** | **55 (57%)** | **59%** | **50%** | **62%** | 4.25 s |
| e5 kNN over route exemplars | 28 | 42 (44%) | 34% | 69% | 28% | 1.46 s |
| compact prompt, local | 29 | 61 (64%) | 69% | 59% | 62% | 5.34 s |
| translate to English first | 29 | 62 (65%) | 59% | 69% | 66% | 13.22 s |
| **compact prompt, CLOUD model** | **29** | **66 (69%)** | **69%** | **69%** | **69%** | 14.79 s |

**Corrections to this plan's own earlier framing, from the wider sample:**

- The probe's *5/8 vs 0/8* measures the **regex layer alone**. Over 96
  paraphrases that layer is weak in every language (19/25/25%) — English has no
  real advantage. The language asymmetry lives in the **classifier**, and there
  the shipped gap is **9 points** (en 59% vs roman-ur 50%), not total failure.
- **Only a better MODEL flattens the languages.** Every prompt-side lever
  raises all three by similar amounts. The cloud arm is the sole design
  reaching parity — and no design measured gets past ~69%, so roughly **30% of
  ordinary rewordings are misrouted under all of them**.
- **Module 78's approach is vindicated by this data**: gating on
  `_is_legal_kb_intent()` rather than on vocabulary lifted the deterministic
  layer 10/96 → 22/96 *evenly across all three languages*.

## Why direction 1 could not simply be shipped

- `prompts/router.txt` is **13,003 request tokens** against Groq's **8,000**
  `on_demand` per-request cap — a hard **413**, not a slow call. So
  `route_query()`'s own `escalate_to_cloud_on_failure=True` had been dead for
  weeks, failing over to Gemini, whose pool is one 429 key and three 401 keys.
- With a compact prompt Groq works but sustains only **~2.7 calls/minute** on
  shared quota. Not viable on the miss path of every query.

## The finding that matters most

**Route accuracy is not a proxy for answer quality.** The compact prompt on the
*local* model improved route-match 55→61/96 with **nothing regressing** — and
end-to-end it turned CR3's and G6's substantive answers into RAG abstentions
and sent G6's English paraphrase to **DIRECT**, which fabricated an ungrounded
welcome note. Cause: the compact prompt drops router.txt's `ACTIVE_CASE:`
block, three of whose entries are gold questions verbatim. **That gold-specific
prompt debt is load-bearing**, and removing it without a replacement regresses.
So it did not ship, and any future attempt here must be judged on answers.

## What shipped

`cloud_system_prompt` on `call_llm_json()` (inert for every other caller) and
`prompts/router_compact.txt` (~2,560 tokens, derived from router.txt, pinned
against drift by a test). The **local path is untouched**: all 32 gold
questions, all 8 load-bearing router fields, **byte-identical before and
after**. Live proof of the fix: before → Groq 413 → Gemini 429 → `result=None`;
after → `route=XAGG` in 0.7 s. 8 new tests, 6 fail before; 409 tests green.

## Still open, and owned by nobody yet

- **CR2's Roman-Urdu and Urdu paraphrases still route XGRAPH** — Module 88's
  symptom reproduces before and after. **Module 92 does not close it.** Nor
  does it close CR3's or G6's (Module 62's). KB3/KB9 were closed by Module 78.
- Closing the gap needs a paid provider tier or a better local classifier. The
  code side is now ready: `escalate_to_cloud_on_failure` exists and works.

**New defects filed:** **98** (Gemini pool: one 429 key, three 401 keys, and
rotation turns a throttle into a hard failure — this also affects
`gold32_score.py`'s judge), **99** (`_is_legal_kb_intent()` cross-lingual false
positive on `taqaza`/`تقاضا` + `hamare`/`ہمارے`), **100** (the gold-32 route
baseline is stale: CR3, G1 and G6 now reach XNETWORK, not the recorded XAGG),
**101** (the local classifier reproduces only 63/74 of its own prompt's
few-shot examples), **102** (a router.txt few-shot sets `target_entity` to the
descriptive phrase its own schema forbids).

---

# Modules 95–96 — what stands between 30 and a *reliable* 30 ⬜

Waves A and B (the in-flight tracks plus Modules 89 and 64) project to about
**30 of 32**. These two are what decides whether that number holds on a rerun
or was a good afternoon.

---

# Module 95 — G2 and G5 name the wrong findings ✅

**Superseded framing.** This section originally read *"three questions pass
by a hair"* (G2 0.5/0.5/0.5, G5 0.5/0.6/0.6, CP1 1.0/0.5/0.9) and asked
Module 95 to find out why substantively-correct answers sat at the pass
line. **Module 87's re-judge of Module 27's FROZEN answers — the system
held constant, only the judge changed — reframed it:**

| Q | old judge | new judge |
|---|---|---|
| **G2** | 0.5 / 0.5 / 0.5 | **0.3 / 0.3 / 0.3** |
| **G5** | 0.5 / 0.6 / 0.6 | **0.4 / 0.4 / 0.4** |

The judge's reasons were **coverage, not strictness**. Both questions are
open-ended *"what would you flag?"* briefings whose gold answer is a method
line plus three numbered, specifically-evidenced findings, and the system
produced a fluent, plausible briefing naming *different, also-true*
findings. Not hallucinating, not refusing — **selecting the wrong
evidence.**

## What Phase 1 established

Four of the six gold findings were **not computable at all**, and the
computability question — not a generation prompt — was the whole module:

| Finding | Verdict |
|---|---|
| G2 (1) 9 FIRs with no incident date | **(a)** already computed and already reported |
| G2 (1) most zimni entries untyped | **(b)** `fir_zimni.entry_type` null on 188/259, but `fir_zimni` is the one child table never projected |
| G2 (2) 13 of 73 departure-before-report | **(b)** `psrms.fir.station_departure_datetime` populated on 44 of 73 — and read by **no line of this codebase** |
| G2 (3) complaints and FIRs joined by an optional tag | **(b)**, schema-shaped — measurable through the CMS↔FIR linkage that already existed but never reached this answer |
| G5 (1) 30 of 32 (94%) unlicensed | **(a)** already computed and already reported |
| G5 (2) no packaging/photo/custody field | **(b)**, schema-shaped — derivable from the register's own column inventory |
| G5 (3) soft FIR-code join, no enforced key | **(b)**, schema-shaped — measurable as weapons resolving to no case |

**Two corrections to this module's own filed diagnosis.** It predicted that gold's *13 of 73* "would have to come from FIR narrative text, and nothing computes it today" — the second half was right, the first was wrong: `station_departure_datetime` is a typed, CONFIRMED **structured column**, returned populated on 44 of 73 FIRs, that simply nothing had ever read. And it filed CP1 alongside G2/G5; Module 87's re-judge scored CP1 **1.0 / 1.0 / 1.0**, so CP1 closed itself and this module scoped to the two that had not.

**No finding was (c), and no gold figure was wrong.** Every one reproduces
exactly against the recorded API snapshot. After five gold corrections on
this programme (G1, G6, KB9, CP6, KB1) that was the live possibility, and
it did not happen: `tests/test_module95_finding_coverage.py::
test_gold_figures_reproduce_from_the_api_snapshot` pins all of them and
passes on the merge-base as well, deliberately.

**The schema-shaped findings needed a different mechanism from a count**,
as this module was warned they might. G5 (2) is computed as a set
difference between the register's column inventory and a declared list of
custody controls, so adding a `chain_of_custody` column upstream RETIRES the
finding automatically instead of leaving a stale assertion in the answer.

## What shipped

- `structured_projection.py` — `Incident.station_departure_datetime`
  (Module 22's own pattern and its optional convention) plus
  `zimni_entry_count` / `zimni_typed_count`.
- `scripts/backfill_incident_completeness_fields.py` — MATCH-only, writes
  no nodes and no edges, modelled line for line on Module 22's backfill and
  for the same recorded reasons (a past re-projection duplicated every edge
  type; the graph is shared with four live tracks).
- `xagg.py` — `_case_completeness_scan()` and `_weapon_compliance_scan()`
  each grew the findings they were missing. **No new aggregate kind, and
  not one line of `resolve_aggregate_kind()` was touched**, so the all-32
  dispatch control holds by construction rather than by luck.

## The debt this module did NOT pay

`case_completeness_scan` is shared with **G1**, which therefore also gained
three findings it did not ask for; that cost is measured in
`MODULE95_RESULT.md` §7 rather than assumed away. And `fir_zimni` is still
not projected as StructuredRecords — Module 95 stood two per-Incident
counts in for 259 nodes to keep its blast radius off a shared graph, and
filed the real fix as **Defect 104**.

**Result:** `docs/gold-qa-wave2-results/MODULE95_RESULT.md`

Both are open-ended *"what would you flag?"* briefings, and both gold answers
are **a method line plus three numbered, specifically-evidenced findings**. The
system produces a fluent briefing that names *different, also-true* findings.

**G2** (Urdu — brief an SHO on which cases could get buried or lost):
1. 9 FIRs carry no incident date and most zimni entries no type, so events
   cannot be ordered;
2. **13 of 73 FIRs record the officer's departure for the scene as EARLIER than
   the report time** — a chronology contradiction that will not survive
   scrutiny;
3. walk-in complaints and FIRs live in separate systems joined only by an
   optional shared tag, so a mistagged complaint looks unactioned.

The judge: *"fails to address the key points … chronological contradictions in
departure times and the systemic issue of disconnected walk-in complaint and
FIR systems. It focuses on different metrics (missing investigation status)."*

**G5** (Roman-Urdu — anything worth flagging in recovered-weapon
recordkeeping):
1. 30 of 32 weapons (94%) unlicensed;
2. the register records what was recovered and its condition but has **no
   field** for packaging, photographs or chain of custody;
3. weapons join to cases by a **soft FIR-code match with no enforced key**, so
   a mistyped code silently orphans an item.

The judge: *"correctly identifies the primary compliance issue regarding …
unlicensed weapons. However, it is materially incomplete"* — it misses (2) and
(3).

## The first question is computability, not generation

Findings (2) and (3) in both answers are **schema-shaped observations**, not
counts an existing aggregate produces:

- No structured column holds an officer's departure time. `Incident` carries
  only `incident_datetime` and `report_datetime`; `StructuredRecord`'s
  `fir_position` carries `status_date`. Gold's *13 of 73* would have to come
  from FIR narrative text, and **nothing computes it today**.
- "Two systems joined by an optional tag" and "a soft FIR-code match with no
  enforced key" are statements about the schema's shape, which no aggregate
  expresses at all.

**So do not start by tuning the generation prompt.** Establish, finding by
finding, whether the fact is (a) computable now, (b) computable with a new
aggregate, or (c) not present in the data. Only (a) and (b) are generation
problems; (c) means gold asserts something the system cannot know, and that is
a gold correction — the sixth this wave, after G1, G6, KB9, CP6 and KB1.

**Verify gold's figures before building anything.** The 30-of-32 unlicensed
figure is confirmed by the Module 27 pre-flight. The 13-of-73 and 9-missing-date
figures are not, and this programme has repeatedly found gold overstated.


# Module 96 — KB2, the last question with no module ⬜

> **Note added by Module 82 (`fix/kb-downstream-generation`), which was running
> when this was filed.** Two of the three branches below are now answered with
> live evidence in `docs/gold-qa-wave2-results/MODULE82_RESULT.md` §8e:
> **(1) our answer is wrong** — it does not call the absence a gap, it *denies
> the absence*, built on a Module 37 orphan chunk it cites first on 3 of 3 runs
> while CrPC s.162(1) sits uncited at window position 1; **(2) gold is not
> wrong** — all three provisions exist in the corpus by id and a Roman-Urdu
> paraphrase reproduces gold's conclusion from them 3 of 3, though gold's
> *schema* claim was not checked and is still this module's to verify; **(3) the
> judge branch is untested**, because no measured answer ever stated the correct
> negative. Start at **Module 97**: KB2's law half is 0 of 3 on gold's own
> wording and 3 of 3 on a paraphrase, so any fix begun elsewhere works with two
> of gold's three provisions missing from the window.

**KB2 scores 0.00 on all three passes with AnswerRelevancy 1.00** — the
signature of a fluent, confident, wrong answer.

Gold: *"No, that's by design, not a gap."* Under Qanun-e-Shahadat Order 1984
Arts. 38–39 a confession to police is not evidence, and CrPC s.162 bars a
police-recorded statement at trial, so the system stores only witness identity
— name, CNIC, address, relationship — and has no field for interview text
anywhere.

Module 39 excluded KB2 from `_KB_DATA_HALF_PLANS` as a *"schema claim, not a
count"*. That call was **right** for KB2, unlike for KB1 (Module 89), whose
gold asserted something checkable.

**This module is a diagnosis before it is a fix.** Establish which of three
things is true, with evidence, and only then decide what to change:

1. **Our answer is wrong** — it calls the absence a data gap, or invents a
   field, or cites the wrong law. Then fix the answer.
2. **Gold is wrong** — a fifth gold correction, after G1, G6, KB9, CP6 and KB1.
   Check the claim about witness fields against the live schema before
   asserting this.
3. **The judge cannot score a correct negative.** A gold answer whose content is
   *"this absence is deliberate and here is the law that makes it so"* may be
   structurally hard to score against a hedged paraphrase. If so this belongs
   with Module 87, not here.

Plan for **31, not 32**: KB2 is the one that may not come, and saying so now is
better than discovering it in the final rerun.

---

# Module 37 — the orphaned rows are load-bearing ⬜

Filed by Module 30 as index hygiene and left in the no-score-impact pile for
most of this wave. **Module 82 disproved that**, live and by chunk id.

## What is actually wrong

`chunk_fulltext` is the BM25 half of hybrid retrieval. It holds rows whose
`chunk_id` no longer exists in Chroma. BM25 still ranks them, retrieval still
hands them on, `get_by_ids()` returns **nothing** for them, and the model is
handed a citation marker pointing at an empty chunk.

Measured 2026-09-10, directly against both stores:

| | |
|---|---|
| `chunk_fulltext` rows for the legal PDFs | 9,172 |
| Chroma `muhafiz_kb` ids | 7,716 |
| **orphans — in BM25, absent from Chroma** | **2,246** |
| — of those, the CrPC PDF | 2,243 |
| — of those, leftover test documents | 3 |
| **in Chroma, absent from BM25** | **790** -> Module 99 |

The CrPC document was evidently re-chunked at some point and the BM25 index
kept the old ids. 4,820 CrPC rows are indexed; only 2,577 of them resolve.

## The cost, measured on a gold question

Module 82, on KB2: **2 of the 5 chunks in the window are orphans**, returning
nothing from `get_by_ids()` on 3 of 3 runs — and the answer's **lead citation
is one of them on 3 of 3**. It concludes *"it is not a data gap; the system
does maintain such records"*, which is the opposite of gold on the facts. KB2
scores **0.00 on all three of Module 27's passes**.

So this is very likely KB2's real fix, and KB2 was the question the plan had
already written off (Module 96).

## Approved scope and timing

**Scope — orphans only.** Back the 2,246 rows up to a committed file first,
then delete them. Minimal, reversible, and aimed exactly at the defect. A full
re-index would also close Module 99 but re-runs ingestion across the whole legal
corpus and changes retrieval for every KB question at once; that is a bigger
blast radius than this defect justifies.

**Timing — after the live tracks land.** Modules 83/70, 92, 89 and 64 are all
running live measurements against this shared store. Changing it mid-flight
would silently void their baselines and regression controls — the exact failure
mode that has already cost this programme two full evaluation runs.

## Required verification

- KB2 before and after, several runs, with the window chunk ids captured each
  time — the fix is proven by *which chunks are served*, not only by the score.
- The all-32 regression control. Removing rows from the BM25 index changes
  hybrid ranking for **every** RAG question, so this is broad by construction
  and a silent regression here would be easy to miss.
- Confirm the 3 test documents are genuinely test leftovers before deleting.

---

# Module 101 — the verifier rejects an answer built from a figure it was handed ⬜

**KB9 scores 0.40 under Module 87's judge and fails 2 live runs in 3** — not
because anything was wrong, but because the grounding verifier refused an
answer whose second half was **computed** rather than retrieved.

## The run record, from Module 78's committed artefacts

`docs/gold-qa-wave2-results/module78_runs.json`, KB9, arm `after`:

| field | value |
|---|---|
| `route` | `RAG` |
| `status` | **`error`** |
| `transport_ok` | `True` |
| `error` | `None` |
| `quota_lines` | `[]` |
| `data_half_answered` | `death_investigation_charging` / `fir_section_case_count`, 829 chars |
| `xagg_log_lines` | `fir_section_case_count: 218 section entr(ies) over 73 FIR(s) … focus=302 -> 10 FIR(s)` |
| `evaluator_verdicts` | `relevant=True` |
| `actual_answer` | *"The generated answer could not be verified as grounded in the retrieved documents."* |

Every stage worked. Routing reached RAG (Module 78), the plan fired, the
aggregate returned **10 FIRs citing PPC §302** — exactly the figure KB9's gold
was corrected to — and the relevance evaluator passed it. The only thing that
failed is the check at the end, and it replaced the whole answer with its own
refusal string.

## Why Module 85 closed without finding this

Module 85 was scoped to *"the grounding verifier rejects a composed KB answer
that has already been handed its figure"* and measured it on **KB4**, where the
whole shipped-code arm carried **zero** rejections across 12 primary and 17
regression runs. It closed as not-reproducing, and its result file said so
honestly. **It never ran KB9.** Module 61's warning that the 1-of-3 figure
should not be treated as fixed was right.

## The mechanism to establish first

A KB data-half answer's second half cites a chunk that `xagg_tool()` composed in
memory. If `verify_grounding()` only trusts chunks that came from the retriever,
then **any** composed answer is unverifiable by construction, and every KB
question with a data-half plan is exposed — the failure just shows up
intermittently because the verifier is an LLM call.

Establish that before changing anything. Then, in order of preference:

1. **Make the composed chunk first-class evidence**, so the verifier can ground
   against it — the aggregate's own rendered text is at least as trustworthy as
   a retrieved chunk, being machine-derived from the graph.
2. If that is not possible, scope the verifier so it does not adjudicate the
   composed half.

**Do NOT simply disable the verifier or downgrade its refusal to a warning.**
Module 82 measured it rejecting genuine hallucinations three separate ways —
invented `rule 27.41(3)`, an invented FIR number, a fabricated "fully
compliant" claim, and a fabricated negative on CR3 — and those rejections are
load-bearing. The bug is the false positive, not the check.

---

## Reference

- `MODULE_18_FINAL_REPORT.md` — the run this plan responds to.
- `GOLD32_RESULTS_FOR_TEAMMATE.md` — the same run, ownership-oriented.
- `GOLD_QA_MASTER_FIX_PLAN.md` — Modules 1–20, and the working discipline
  this plan inherits.
- PRs #7 (KB1 router/verifier false positives), #8 (M4/G3 keyword collision),
  #9 (KB-intent coverage) — the three fixes already landed or open from this
  round.

---

# Module 59 — `_MAX_PLAN_SUB_QUERIES` is still 5, and revisiting it is now unblocked but unmeasured ⬜

**Found by:** Module 53.

Module 50 fixed the plan cap at 5 from measured timeout data and wrote that
Module 53 was *"the prerequisite for reconsidering `_MAX_PLAN_SUB_QUERIES`"*.
That prerequisite is met: at N=5 the shipped code drops **zero** sub-answers
over 12 live runs, and a sub-query cancelled mid-paraphrase now serves its
computed aggregate raw instead of nothing.

**Module 53 deliberately did not raise the cap**, and the reasons are worth
keeping rather than re-deriving:

- The deadline was only **one** of Module 50's three arguments. Model spend
  and the user's wall-clock wait are entirely unchanged by Module 53.
- Salvage degrades an answer's **prose**, not the serialisation. At N=9 the
  ninth sub-query would still queue behind eight others; it would now return a
  raw aggregate rather than nothing, which is better, but a synthesis over
  nine raw renderings is a different and worse artefact than one over five
  paraphrases.
- **The post-fix staircase has not been measured at N=6..9.** Module 50's
  numbers were taken at the 60 s deadline; Module 53 moved it to 150 s and
  changed what a cancellation costs. Raising the cap on Module 50's old
  measurements would be exactly the inherited-claim mistake this wave keeps
  finding.

**Concretely at stake.** Module 50 dropped `_SQ_GENDER` from
`orientation_note` to fit Module 35's arrest rate inside the cap, so gold's
G6 element *"zyada tar mulzim aisay mard hain"* ("mostly men") is not computed
today. Module 50 also recorded three G6 elements that fit only if the cap
rises.

**Work:** measure G6 and G1 live at N=6, 7 and 9 on the post-Module-53 code —
last sub-answer arrival time, salvage count, synthesis quality — then decide.
If the cap rises, `_SQ_GENDER` goes back into `orientation_note` first.

**Verify:** G1/G6/CR3 several runs each at whatever N is chosen; the answer
quality at the new N compared against the current one, not just the timeout
count. A synthesis that reaches all nine findings but reads as a list of raw
aggregate dumps is not an improvement.

---

# Module 60 — M4 does not skip decomposition live; Module 41's guard never fires for it ✅ DONE

**Found by:** Modules 53/40's regression guard, on the branch
`fix/meta-analysis-reliability-53-57-40`.

The tracker records M4 as skipping decomposition since Module 41. **Live, it
does not — 7 runs of 7, across two code states.** Four runs on `main` @
`9942db9`, byte-identical every time:

```
routes = ['XNETWORK', 'XGRAPH', 'XGRAPH']
answer = "No information was found for any part of this question. Checked:
          What is the breakdown of cases by legal section (e.g., Section 325,
          354, etc.) across all cases?; What is the breakdown of case
          progression stages (e.g., filed, investigation, trial, dismissed)
          in court across all cases?."
```

**After Modules 38/55/56 merged, M4 changed and became non-deterministic.**
Three further runs on current `main`:

| Run | Route events | Outcome |
|---|---|---|
| 1 | `XNETWORK` → `XAGG`, `RAG` | **answers**, 257.6 s — statute counts PPC 61 / Arms 29 / CNSA 12 / PECA 9, and states the court-stage data is absent |
| 2 | `XNETWORK` → `XAGG`, `RAG` | **answers**, 235.7 s — same shape |
| 3 | `XNETWORK` → `XGRAPH`, `XGRAPH` | "No information was found", 33.4 s — the pre-merge behaviour |

The **LLM decomposer** is what varies. Under this wave's judging standard the
answering runs are a partial pass, not a wrong answer: they cover gold's
statute half and correctly say the court-stage data is not available. The
court-stage half is lost to a **live sub-query timeout at the 150 s deadline
with no salvage** — that sub-query routes to RAG, which holds no
deterministic rendering to fall back on, so Module 53's deliberately narrow
scope behaved exactly as designed. **What does not vary is that M4 never skips
decomposition.**

**Both statements are true, and the reconciliation is the finding.** Module 41's
guard at `supervisor.py:745` is deliberately conditional on `route == "XAGG"`
— its own comment says so, so that it "can never suppress a genuine
Meta-Analysis decomposition for a DIFFERENT cross-case route (e.g. an XGRAPH-
or XNETWORK-classified comparison question)". Module 41 measured M4's
*aggregate resolution* statically (`resolve_aggregate_kind` →
`statute_court_stage_join`) and recorded `skips_decomposition=True` from it.
That resolution still holds, re-derived on this branch. But the live router
classifies M4 as **XNETWORK**, so the guard is never consulted. Module 41's
own §8 is explicit that this was an assumption: *"pinned by the all-32
negative control, **assuming an XAGG route**"*, and *"CR6, CR8, M4 and M7 were
**not re-run live here**"*.

**Start from Module 40's branch, not from scratch.**
`fix/meta-analysis-m4-synthesis-verifier-rejection` already contains a
deterministic `statute_vs_court_stage` plan whose two sub-queries were probed
live to reach XAGG, plus the `prompts/meta_analysis_decomposer.txt` fix that
removes the self-contradiction which produced the exact useless sub-queries
quoted above. It applies cleanly to current `main`. Module 40 was closed as
superseded for CR3/G6 only — its M4 work was never disproved, and this is the
module it belongs to.

**The trap that stopped Module 40's branch being taken wholesale.** A matched
deterministic plan **vetoes** the Module 41 guard:
`_xagg_answers_in_one_call()` returns `False` the moment
`_match_decomposition_plan()` matches. So adding `statute_vs_court_stage`
makes M4 decompose on **every** run, including the runs where the router does
say XAGG and Module 41's one-call aggregate is the better answer. Three
options, and this module must pick one explicitly rather than drift into it:

1. Take the plan and accept the veto — M4 always decomposes.
2. Widen the guard beyond `route == "XAGG"`, against that guard's own stated
   reasoning. Requires re-running Module 41's all-32 negative control.
3. Fix the routing: establish why `route_query()` classifies M4 as XNETWORK
   when `resolve_aggregate_kind()` has a purpose-built aggregate for it.

**Verify:** M4 live, several runs, in Urdu gold text; **M1, G2, G5, CR7 and G3
as the Module 41 regression guard** — option 2 in particular must negative-
control against all 32. Confirm from `backend.log` which aggregate answered,
noting Module 55 (`statute_court_stage_join` may not log a line).

---

---

## Outcome (2026-09-09, branch `fix/m4-routing-and-station-predicate`)

**Done. The finding is confirmed, and the before-arm is worse than the tracker
recorded.** Full evidence in `docs/gold-qa-wave2-results/MODULE60_RESULT.md`.

**Before, re-measured on one backend with only `router.py` reverted to
`c5533ba`, 6 runs — four distinct outcomes:**

| Runs | Route events | Seconds | Outcome |
|---|---|---|---|
| 1–2 | `XNETWORK` → `XAGG`, `RAG` | 328.9 / 324.6 | **partial** — statute half only; the answer says outright that court-stage data is not specified |
| 3–4 | `XAGG` | 86.9 / 69.7 | **answers** — the runs Module 41's static check assumed were universal |
| 5–6 | `RAG` | 456.0 / 486.6 | **`status=error`** — *"No sufficiently relevant documents were found…"*. Never previously recorded for M4 |

The router was also probed directly, 8 calls on M4's literal gold text with
the new override disabled: **6 XAGG / 2 XNETWORK**, `confidence: medium` on
all eight. The router's own output is the flaky part, which is why 7-of-7 and
2-of-3 were both true.

**Option 3 was taken — fix the ROUTE — and options 1 and 2 were rejected on
measurement:**

- **Option 2 (widen the guard) does not work.** With `route=XNETWORK`,
  skipping Meta-Analysis falls through to `_ROUTE_TO_SUBAGENT["XNETWORK"]` —
  Cross-Case Linkage / Global Search — which still cannot reach
  `_statute_court_stage_join()`. It would repeal the guard's stated protection
  for every cross-case route and fix nothing. Pinned by
  `test_module60_m4_under_the_live_xnetwork_route_still_decomposes`.
- **Option 1 (Module 40's plan) is vetoed by design** — a matched plan makes
  `_xagg_answers_in_one_call()` return `False`, so M4 would decompose even on
  the runs where the one-call aggregate is the better answer.

**Module 41's guard is unmodified.** It now fires for M4 on its own terms.

**The override is gated on `resolve_aggregate_kind()`**, not on a new regex —
Module 41's single source of dispatch truth, and Module 62's warning taken
seriously. It therefore inherits every precedence rule above
`_is_statute_court_stage_join()`, so **G3's `court_readiness_scan` and CR7's
`criminal_record_court_crosscheck` resolve first and can never reach it**. The
PR #8 collision is guarded structurally rather than by hand. The broad form,
`resolves_to_specific_aggregate()`, was measured and rejected: it moves
**seven** of the 32, including **KB5**, a legal-KB question that resolves to
`gender_breakdown` as a pure resolver false positive.

**After: 7 of 7** — `route='XAGG'`, one dispatch to Large-Scale Aggregate, no
decomposition, `XAGG statute_court_stage_join` logged every run, **13.6–29.7 s**
against 325–487 s. **Every gold element present on every run**, including the
court-stage half (33 records, 1 verdict, the rest under trial, read severity
from the section counts) which had never appeared in any recorded M4 run.

**All-32 EQUALITY negative control: exactly one question changed — M4, from no
deterministic override to `XAGG`.** All 31 others identical; the 32
questions' own `question_variants` swept as well, none newly captured.
Regression G3/CR7/G2/G5/M5 live, 2 runs each: **`XAGG` → Large-Scale
Aggregate, correct aggregate, 10 of 10**.

**Honest bound:** the override is exactly as wide as
`_is_statute_court_stage_join()`. Three of five non-gold paraphrases reach it;
a Roman-Urdu one and an English one naming no court *stage* do not. Widening
that predicate is **Module 62**'s job, not this module's — doing it here would
have changed the blast radius the negative control measures.

**New defects split out as Modules 67 and 68.**


---

# Module 61 — the grounding verifier refuses a negative inference over a complete listing ⬜
# Module 61 — the grounding verifier refuses a negative inference over a complete listing ✅

**Done — `docs/gold-qa-wave2-results/MODULE61_RESULT.md`.** Branch
`fix/verifier-over-rejection`, two commits.

**The rule that shipped.** A caller may DECLARE a chunk a complete
enumeration over its stated scope (`metadata["exhaustive_scope"]`), and
non-membership in one is then supported rather than inferred. Exactly one
caller does so today: `meta_analysis.py`, for a sub-answer whose
`tools_used` is exactly `["XAGG"]` — a deterministic aggregate computed
over the whole corpus, not a retrieved sample. A RAG/GRAPH/WEB chunk is
never exhaustive and there is no path in the code to make it one, which is
the narrowness the brief demanded. `xagg.py` was not touched: the
provenance the fix needs is already recorded at the sub-agent boundary.

**Both halves shipped.** `prompts/verifier.txt` gains rule 7 and two worked
examples (one grounded negative, one fabricated); `verifier.py` gains a
deterministic post-pass so the outcome is reproducible rather than
resampled. Honest note: the post-pass **never fired live** (0 activations in
22 post-fix runs) — the prompt rule carried the judge every time, and the
post-pass is proven by unit test only.

**Two things the brief did not name, both found by measurement.** (a) The
unit the validation gate checks matters: its flagged unit is the answer
SENTENCE, and CR3's sentence names both a record the listing contains and
one it does not, so a first attempt checking the sentence left the caveat
on 5 of 5 runs. Checking the judge's own `reason` is what reconciles the
gates. (b) Absence is absence FROM A PARTICULAR REGISTER — CR3 serves three
complete listings at once and 65/26 legitimately appears in a sibling one,
so a pooled check called every correct negative fabricated.

**Measured, before and after, one machine, one session:** CR3 **4 of 8 →
7 of 8** answered. All four pre-fix rejections carry the FIR 65/26 absence
reason, one of them Module 57's verbatim string. The single post-fix failure
is a DIFFERENT claim the verifier correctly refused. Validation caveat for
the identical claim: 5 of 5 → **0 of 8**. Non-gold paraphrase 3 of 3.
Regression M2/G1/G6/G2/G5/KB4: 25 of 26 answered.

**KB4 was checked and is a different defect.** Its recorded rejections are
over-attribution to a RAG chunk (`MODULE38_RESULT.md` §5), which this rule
never touches. It measured 5 of 5 answering here, but nothing in this change
is what did that, and Module 38's 1-of-3 should not be treated as fixed.

**New defect split out as Module 67** (G1's numeric over-reach rejection).

---

# Module 71 — G1's synthesis blends denominators across sub-answers ✅

**Done — `docs/gold-qa-wave2-results/MODULE71_RESULT.md`.** Branch
`fix/verifier-numeric-overreach`.

**Where the fabricated number comes from, exactly.** G1's five aggregates
count five different populations. `incident_time_of_day` renders *"64 of 73
incident(s) carry a datetime"* — and it is **the only one of the five that
states 73 at all**. `seized_property_disposition` computes 45 register
entries across 28 FIRs. Module 61's recorded rejection, *"the 73-case total
for seized property"*, is that denominator borrowed from [Document 4] onto
[Document 3]'s subject: not an invention out of nothing, not arithmetic, a
**cross-denominator borrow**. The verifier was right, and Module 61's
exhaustive-listing rule correctly neither fires nor rescues.

**Why the existing rule could not catch it.** The synthesis prompt already
said *"every number you state appears literally in a sub-answer above"*
(Module 29, after G6 summed per-district counts). **73 satisfies that
rule.** It was scoped to the page; the property it needed was scoped to the
document. Rule 2 is now "…in THE SUB-ANSWER YOU CITE FOR IT, not merely
somewhere above", with an explicit ban on moving a total, denominator or
coverage figure between documents; the derived-number half survives as rule
3. `caseload_review`'s synthesis goal also stopped inviting the blend — it
used to end at *"carry that coverage across too"*, spoken over five
different denominators.

**`verifier.py`, `validation.py` and `prompts/verifier.txt` were not
touched**, as the brief requires. One production file changed.

**An error is no longer the outcome.** A verifier-rejected synthesis now
returns `PARTIAL` carrying a deterministic composition of the verified
sub-answers (no second LLM call), with a caveat saying the synthesis is
missing — the same shape as `large_scale_aggregate.py`'s raw-aggregate
fallback and Module 53's. The rejected text is dropped and never served.

**The headline live number is a null result, reported as one.** The defect
**did not reproduce in 25 pre-fix live G1 runs** on this machine (Module 61
measured 1 in 3, on a build where this path took ~120 s rather than today's
~35 s). No before/after improvement in G1's answer rate is claimed. The fix
is justified by the root cause and by a **forced-hallucination control**
(`scripts/module71_forced_hallucination_control.py`: the ordinary backend
with `meta_analysis.call_llm` replaced, everything downstream real):
same fabricated synthesis, same real verifier, **`status=error` 3 of 3 →
`status=done` 4 of 4**, all four gold findings served, the fabrication never
served, and the new deterministic detector naming `73 -> [Document 3]` on
4 of 4 before the verifier spoke.

**A change was built, measured, found harmful and deleted.** The brief's
second direction — make the sub-answers carry their denominators explicitly
— was implemented as a per-document figure roster in the prompt. G6 went
from **0 rejections in 4 pre-fix runs to 3 of 4, then 7 of 8**, every one
Module 29's *"cites no [Document N] source at all"*; rewording the roster to
drop the marker did not recover it; deleting it did, 8 of 8 at the pre-fix
timing. Against that cost, no measured benefit — the defect it prevents did
not occur in 25 runs. The measurement is written into the function's
docstring and pinned by a test, so the next module to have the idea finds
the answer before spending the runs.

**Regression CR3/G6/M2/G2/G5: 40 of 40 answered, 0 rejections.** CR3 holds
Module 61's result. Non-gold Roman-Urdu paraphrase 3 of 3 with all four
findings; the English paraphrase routes to XNETWORK 3 of 3 and never reaches
the plan — **Module 62 reproducing verbatim**, reported rather than swapped
out.

**New defect split out as Module 82** (the citation rule is held only by
prompt proximity).

---

# Module 62 — the deterministic decomposition plans have a narrow lexical reach ⬜

**Found by:** Modules 57/40's non-gold paraphrase check. Generalises
Module 41's M4 finding ("reachable only from the literal gold phrasing") to
CR3 and G6.

Measured live on the shipped code:

| Question | Paraphrase | Plan matched | Result |
|---|---|---|---|
| CR3 | "Were the two online banking fraud FIRs **handled the same way** in the records, or was one processed differently?" | `record_consistency` | ✅ answered 2/2, gold's verdict |
| CR3 | "Take the two online banking fraud FIRs with different complainants — are their records **equally complete**, or does one have supporting paperwork the other lacks?" | **none** | `route='XAGG'`, one dispatch, honest refusal — never reaches Meta-Analysis |
| G6 | "Write a brief **orientation note** for an **officer newly posted** here — what should they expect from the current case load?" | `orientation_note` | ✅ answered 1/1 |
| G6 | "A constable has just transferred in. Give them a short briefing on what the current pile of open cases actually looks like." | **none** | `route='XAGG'`, one dispatch |

**The good news is real and should not be lost:** the capability is **not**
tied to the gold string. Not one content word of CR3's gold phrasing survives
in the passing paraphrase and the answer is still right, so Modules 29/50's
wiring is not curve-fitted to the scorer.

**The bad news bounds what every pass rate in this wave means.** A plan is
selected by a regex pattern list, so the set of questions that reach the
wired-up aggregates is a small neighbourhood around the gold text. A real user
asking CR3's question in their own words has a meaningful chance of getting a
single-aggregate answer instead.

**Work — and the trap.** The fix is probably **not** "add more patterns": a
module that widens a pattern list until the paraphrases *it chose* pass has
measured nothing. The more promising direction is the one Module 41 already
took for the supervisor guard — replace "does this text match a pattern?" with
"does this question resolve to something specific?" — i.e. select a
decomposition plan by the same aggregate-resolution mechanism rather than by
regex.

**Verify:** for each of CR3, G1 and G6, **paraphrases written before the fix
and not adjusted afterwards**, several each; plus the full 32-question
negative control, since a broader plan selector can newly capture questions
that currently answer correctly in one call (that is exactly the regression
Module 41 was filed for).


---

# Module 88 — CR2's recurrence evidence had no time axis ✅

**Found by:** Module 27 §7 · **Branch:** `fix/cr2-recurrence-temporal-context`
· **Result:** `docs/gold-qa-wave2-results/MODULE88_RESULT.md`

CR2 — *"Is there anyone with an earlier case already on record who has since
resurfaced as a suspect in a newer, separate case?"* — was the one clear
regression in Module 27's final rerun: **0.00 on all three passes**, the same
refusal each time, on `route='XAGG'`.

**The aggregate was never wrong.** `resolve_aggregate_kind()` returned
`graph_recurrence_person` every time and the result already carried gold's
exact pair. What reached the model was `appears in 2 cases — fir-214-26,
fir-891-24` and nothing else: no dates, no per-case role, no arrest status,
no conviction. The refusal was **correct given the evidence it was handed**,
which is why it was deterministic rather than flaky. The pre-fix answer says
so itself — *"there is no information provided that would indicate the
chronological order of these cases"*.

**Probed with hand-written Cypher before any code was written**, and the
figures re-derived rather than inherited:

- 4 recurring `Person` nodes, every one `case_count = 2`.
- شہزیب عرف شابی (`PERSON-685fc54914`): `fir-891-24` incident **2024-09-14**,
  role accused, `arrest_status` `گرفتار، بعد ازاں سزا یافتہ`, criminal record
  **"Convicted, on bail pending appeal"**; `fir-214-26` incident
  **2026-03-03**, role accused, `گرفتار`, **"Under trial"**.
- **Gold reproduces exactly and is not challenged** — unusually for this
  wave.
- Only **4 of 33** `criminal_record` rows carry a parseable FIR reference, so
  the `_fir_key()` join is sparse by construction and is disclosed rather
  than widened into a bare-name match.
- **`fir-401-26` has an incident date of 2024-09-25.** The FIR id encodes 26;
  the incident is 2024. That single row is why the year is derived from the
  `OCCURRED_ON` edge and never parsed out of the id.

**The fix** attaches a date-ordered per-case timeline to every recurring
entity — year from `Incident-[:OCCURRED_ON]->Date` (as `_statute_mix_by_year()`
resolves it), role and `arrest_status` from `Person-[:INVOLVED_IN]->Incident`,
and `conviction_status` from the `criminal_record` StructuredRecord joined on
`_fir_key()` (Module 28's own weapon→FIR→accused→status chain, reused with
the same free-text hedge) — and renders the sequence **explicitly** instead
of leaving it to be inferred. A shared `render_graph_recurrence()` replaces
three copies of one f-string across the two orchestrator branches and the
harness tool.

**Nothing is narrowed toward CR2.** No new aggregate kind (so no
`XAggToolResult.aggregate_kind` `Literal` entry and no exposure to the
empty-answer trap), no dispatch change, and the **all-32 dispatch equality
control is byte-identical — 0 questions move**. Three refusals are pinned by
tests: an undated case gets no sequence number, a conviction is never
attributed across subjects sharing a FIR, and two FIRs four days apart
(عاصم رشید's real shape) are not counted as an "earlier case already on
record".

**Live, :8032, both arms on the same backend:** CR2 **0/3 → 7/7**, naming
شہزیب عرف شابی, FIR 891/24's prior conviction and FIR 214/26's current
accusation; `route='XAGG'` 10/10. The `XAGG graph_recurrence:` line now
carries the ordering and conviction counts, because on all three of Module
27's failing passes it read *"4 recurring node(s)"* and was true every time.

**The intermediate arm is recorded rather than hidden.** With the timeline
but without the leading summary line (7 live runs), CR2 answered "Yes" and
listed three people including the right one, and a paraphrase reproduced a
2024 case and a 2026 case for the same person **in its own body** while
concluding *"none of these are years apart"*. The summary line is the half
that closed the gap, and that is measured, not asserted.

**S3 improved from the same generic change**, without being named anywhere in
the code: Module 27's captured S3 answer listed all four recurrers and never
used the word "arrested"; post-fix it names gold's two exactly. Regression
over S3/CR4/CR6/CR7/CR8/CS4/M4/G3: **24 runs, 3/3 each, 0 route changes.**
Unit **430** (`tests/test_xagg.py`) + **645** adjacent. **0 quota lines over
39 live runs.**

**New defect 89**: CR2's Roman-Urdu and Urdu paraphrases route to XGRAPH and
never reach this aggregate at all.
---

# Module 87 — the Gold-32 judge: a stronger model, and a prompt that stops it making the mistake we measured ✅

**Branch `fix/eval-judge-upgrade-and-prompt`. Full result:
`docs/gold-qa-wave2-results/MODULE87_RESULT.md`.** Nothing here changes the
platform; it changes the **instrument**. All 96 question-runs are re-scorings of
the answers Module 27 already captured, so the system under test is frozen and
the only variable is the judge. No backend, no Docker.

**The model was chosen on quota, not on size, because quota is what binds here.**

| candidate | free-tier cap, both working keys | M7 | s/call |
|---|---|---|---|
| `gemini-flash-lite-latest` *(retired)* | 500 / **day** | 0.22, spread **0.4** | 5–6 |
| `gemini-2.5-flash` | **20 / day** | 0.92 *(old prompt)* | 16–68 |
| `gemini-3.5-flash` | **20 / day** | 1.0 | 20–90 |
| `gemini-3.7-flash` | **20 / day** | 1.0 | 7–354 |
| **`gemini-3.1-flash-lite`** ← default | **15 / minute** | **1.0, spread 0.0** | **8.1** |

One three-pass re-score is **96 judge calls**, so a 20-per-day model cannot
produce one however many times it is retried. `gemini-2.5-flash` would have
fixed M7 on the *old* prompt and still cannot be the default — that is the whole
of "measure, do not assume". `GOLD32_JUDGE_MODEL` moves it the moment there is a
paid key.

**The polarity rule** carries M7's own worked example, because Module 20 already
proved an abstract rule is not honoured on its own. **M7 0.20 → 1.00 (3/3)**, and
the judge's own reason now names the rule.

**Two more false negatives, found here rather than in the brief, each justified
separately:** **CP6 0.30 → 0.90** — the retired judge applied the tolerance rule
and then overrode it (*"the number is very close and within an acceptable
tolerance, [but] … omits the specific details about the placeholders"*) for a
breakdown the question never asked for; **no rule was added for CP6**. **G1
0.53 → 0.97** — its answer carries all four of gold's findings with gold's own
figures (24–49 / avg 31.5, stranger 15-of-24, 13 forensic + 7 to heirs, the
time-of-day split).

**Variance.** Measured the only way that isolates the judge — one fixed answer,
five draws: **spread 0.0 on 9 of 9 probe questions**, from 0.5 (CP1, M2), 0.4
(M7) and 0.3 (KB6, G1). The middle arm shows the **model** did that, not the
prompt. Across all 32 × 3 passes, spread ≥0.3 goes **4 → 1** and mean spread
**0.106 → 0.025**; the one survivor is KB6, whose captured answers differ across
passes (**Module 108**).

**Proof it is not a leniency shift.** All-32 mean 0.666 → **0.702**, but **seven
questions FELL** (M1 −0.30, M2 −0.27, G2/G6 −0.20, G5 −0.17, KB6 −0.13, CR8
−0.10), Creative Generation goes **down** 0.580 → 0.553, and the pass rate barely
moves (20/22/21 → 20/20/21). CR2 stays **0.00** on all three passes; KB2's hedge
stays at 0.17; KB3 stays 0.00. Held-out controls: a polarity flip on a question
the prompt has never seen scores **1.0 3/3**, while M7's facts *reversed* score
**0.2 3/3** and M7 with 2026 given as 18.0 minutes instead of 1401.3 scores
**0.2 3/3** — tolerance is for phrasing, not magnitude, at a 78× error.

**The regression this module caused, and fixed.** The first version of the
polarity rule took **KB9 from 0.27 to 1.0 on all three passes** on an answer that
never reaches gold's CrPC s.174 or its 10 PPC-302 FIRs: the judge settled
polarity and stopped checking facts. Attributed, not guessed — the same model
with the rule *removed* scores KB9 0.3. The rule is now fenced (*"excuses the
OPENING WORD AND NOTHING ELSE"*), KB9 returns to 0.4, M7 stays 1.0, and a test
pins the fence. Both arms are committed so the claim is checkable.

**Also fixed:** `ATTEMPT_TIMEOUT_S` was 120 — *lower* than the DeepEval
per-attempt override beside it — so under contention S3 and A7 lost all three
attempts to it and were recorded `UNSCORED` while scoring 1.0 in 6 s on a quiet
machine. A slow call is not a failed call (Module 45's rule, one layer out).

**Verification:** 56 unit tests pass, including the docs-match tests — §3.3 of
`HOW_TO_REPRODUCE_THIS_EVALUATION.md` is now asserted to list as many numbered
rules as the prompt has steps, so the two cannot drift apart silently again.

**New defects: 98** (three dead Gemini keys; `GEMINI_MODEL`'s model capped at
20/day) and **99** (KB6's residual spread is the pipeline's, not the judge's).


---

# Module 100 — KB3's aggregate family is reachable only near its gold wording ⬜

**Found by Module 89**, while measuring which questions its own new predicate
must not steal. Measured offline through `xagg.py::resolve_aggregate_kind()`,
which needs no backend, and **run on `origin/main` as well as on
`fix/kb1-data-half-plan` — the results are identical, so this is pre-existing.**

| query | `resolve_aggregate_kind()` | reg? | inv? | same? |
|---|---|---|---|---|
| KB3's gold text | `officer_role_pair_overlap` | ✔ | ✔ | ✔ |
| *"Is the officer who registers a case normally the one who investigates it?"* | `station_or_category_counts` | ✔ | ✔ | **✘** |
| *"Is the registering officer usually the investigating officer?"* | `unsupported_officer` | ✔ | ✔ | **✘** |
| *"Does the same officer both register and investigate our cases?"* | `station_or_category_counts` | **✘** | **✘** | ✔ |

`_is_officer_role_pair_comparison()` is a three-signal AND
(`_OFFICER_REGISTERING_TERMS` ∧ `_OFFICER_INVESTIGATING_TERMS` ∧
`_OFFICER_ROLE_SAMENESS_TERMS`). The third is the one that fails on the two
closest rewordings: both name both roles, and both express sameness as *"the one
who"* / *"usually the"*, neither of which is a listed token. A generic
station/category group-by is a confidently irrelevant answer to a question about
role separation.

**Why it is worth its own module rather than a one-line tuple addition.**
Modules 74 and 77 each widened the **RAG-side** plan patterns for this same
question after their own paraphrases missed, and `rag.py`'s `officer_role_pair`
entry matches the first two rows above today. `xagg.py`'s predicate was never
widened to match. Modules 65, 74 and 78 all measured KB3 routing to **XNETWORK**
or **XAGG**, not RAG — so the layer that was never widened is the layer that
actually answers KB3 on this machine. This is Module 92's finding (a capability
reachable only from a narrow neighbourhood of the gold wording) one layer below
the router, and the fix wants the same treatment Module 78 gave the route: a
predicate, not a fourth word list.

**Not taken by Module 89.** `_OFFICER_ROLE_SAMENESS_TERMS` belongs to Module 74,
and widening it moves questions inside an ordered first-match-wins chain, which
needs that module's all-32 equality control re-run. Module 89 pinned the two
probes at their measured values in `TestModule89Dispatch` instead, so the gap is
recorded rather than silently carried.
