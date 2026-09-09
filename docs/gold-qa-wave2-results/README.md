# Gold-QA Wave 2 — results directory

One result file per module, written by that module's own PR. This directory is
the record of **what was actually measured**, separate from
`GOLD_QA_REMAINING_FIXES_PLAN.md` (the tracker) and the `MODULE*_PROMPT.md`
briefs (the instructions).

**Baseline everything is measured against** — Module 18's run
(`MODULE_18_FINAL_REPORT.md`): FactualCorrectness **0.428** all-32, **0.550**
excluding KB, **14/32** passing.

## Index

| Module | Question(s) | Result file | Status |
|---|---|---|---|
| 23 | M5 — weapon × statute co-occurrence | `MODULE23_RESULT.md` | in progress |
| 28 | CR4 — weapon-evidence chain routing | `MODULE28_RESULT.md` | in progress |
| 24 | M4 — statute × court-stage join | `MODULE24_RESULT.md` | done |
| 29 | CR3/G1/G6 — Meta-Analysis decomposition | `MODULE29_RESULT.md` | done — G1/G6 pass, CR3 partial; gap analysis split out as Modules 31–36 |
| 30 | KB3/KB8/KB9 — retrieval completeness | `MODULE30_RESULT.md` | done — KB3/KB8/KB9 stopped abstaining; new defects split out as Modules 37–39 |
| 27 | Final Gold-32 rerun | `MODULE27_RESULT.md` | queued (last) |
| 31 | G1 — offender age profile | `MODULE31_RESULT.md` | done — aggregate live and correct; G1 unchanged (not yet wired into its plan) |
| 32 | G1 — accused↔complainant relationship | `MODULE32_RESULT.md` | done — same; kills the person-recurrence fall-through |
| 33 | G1 — seized-property disposition | `MODULE33_RESULT.md` | done — same; reproduces gold's 13 / 7 exactly |
| 34 | G1 — incident time-of-day | `MODULE34_RESULT.md` | done — same; gold's "flat across the day" is a date-only artefact |
| 35 | G6 — arrest rate | `MODULE35_RESULT.md` | done — aggregate live with a published rule; **1 in 6.6**, not gold's 1 in 9 |
| 36 | CR3 — subject-filtered FIR listing | `MODULE36_RESULT.md` | done — returns `fir-64-26`/`fir-65-26` exactly; wiring into `record_consistency` deferred behind Module 41 |
| 45 | Eval harness — a judge `null` scored as zero | `MODULE45_RESULT.md` | done — nulls retried then excluded from the mean; the 900-char cap tested and cleared of every current 0.0; split out as Modules 46–47 |
| 41 | G2/G5 — Meta-Analysis over-decomposition (regression) | `MODULE41_RESULT.md` | done — guard generalised from one pattern list to "does XAGG resolve this?"; G2/G5 correct and deterministic on 3/3 live runs each |
| 42 | KB6 — `route=None`, FC 0.0 **and** AR 0.0 | `MODULE42_RESULT.md` | done — **not a pipeline defect**: `route=None` was the harness's own 300s client timeout, live KB6 is `route='RAG'` 5/5. A timeout is now unscored, not a 0.0. The real cause of the abstention (a cross-language relevance gate) split out as Module 52 |
| 43 | M7 — incident-to-report mean, filed as factually wrong | `MODULE43_RESULT.md` | done — **the report is wrong, not Module 22**; M7 returns gold on 6/6 live runs. Fixed the missing `XAGG <kind>` log line; independently confirms Module 47 |
| 44 | M2, CS4 — station specialisation, and criminal-record vs local-FIR gap | `MODULE44_RESULT.md` | done — both gold answers computable and now computed, 3/3 live each. CS4 returns وقاص / 00000-9000020-1 exactly; M2 reproduces 9-of-73 from 2-of-19 and **challenges gold's growth claim** |
| 50 | G1/G6/CR3 — wiring Modules 31–36's aggregates into their plans | `MODULE50_RESULT.md` | done — six aggregates wired and firing on every live run; `_MAX_SUB_QUERIES` settled at **5** with the measured timeout data; G6 trades `_SQ_GENDER` for the arrest rate; defects split out as Modules 53–54 |
| 38 | KB4 — `cross_rerank_multi()` merged by max score across queries | `MODULE38_RESULT.md` | done — fused by reciprocal rank, reusing `reranker.py`'s own RRF. KB4's retrieval goes **0-of-3 → 3-of-3** runs containing gold's rule 27.16; its answer/abstain `status` goes **3-of-3 → 1-of-3**, because all three `main` runs "answered" that the corpus does not contain this. KB6 and KB9 both stop abstaining; bucket unchanged at 6/8. Module 30's rejected third statute hypothesis re-measured and no longer harmful — split out as its own module |
| 55 | Every XAGG aggregate — which one answered a live run? | `MODULE55_RESULT.md` | done — measured **32 kinds, 11 log lines, 21 families silent**; all 21 now log with their figures, confirmed live in-process. The durable fix is an AST-derived test that fails when a new family has no line. Backend/SSE run **deferred for contention** |
| 56 | M2 — dispatch vocabulary vs. the family Module 44 gave it | `MODULE56_RESULT.md` | done — `_STATION_TYPE_KEYWORDS` 10 → 58 entries across English/Roman-Urdu/Urdu; **all-32 equality control byte-identical**; the measured failing phrasing and 8 non-gold paraphrases now reach `station_caseload_by_specialisation`. New defect split out as Module 58 |
| 54 | Provider-failure visibility — `503 UNAVAILABLE` in no grep, and a silent cutover fallback | `MODULE54_RESULT.md` | done — pattern widened to `rate limit\|RESOURCE_EXHAUSTED\|429\|quota\|UNAVAILABLE\|503` and made canonical in `gold32_score.py`'s `LOG_GREP_PATTERN`, which three prescriptive docs now cite and a test enforces; the cutover fallback emits a `cutover_classification_failed` SSE event, recorded per row in `gold32_pipeline_outputs.json`. **Module 46 owns the `RESOURCE_EXHAUSTED` half.** `WAVE2_ORCHESTRATION_PROMPT.md` turned out to prescribe no log check at all. Live deferred; `src/llm/client.py`'s own 503 blindness split out |
| 53 | G1/G6/CR3 — the shared sub-query deadline that measures queue position | `MODULE53_RESULT.md` | done — raw aggregate now served when a paraphrase is cancelled; **8 of 10 dropped sub-answers → 0** in a forced-deadline control, and **zero dropped at N=5 over 12 live runs**; timeout 60 → 150 s; split out as Modules 59/59 |
| 57 | CR3 — non-determinism at three layers | `MODULE57_RESULT.md` | done, **no code change** — 20 runs: timeouts 0/20 (fixed by 53), `route=None` 0/20 (did not reproduce), synthesis-verifier rejection 7/14 and now the only failure left. Its reason measured **not** to be citation attribution; split out as Modules 61/61 |
| 61 | CR3 — a negative inference over a complete listing | `MODULE61_RESULT.md` | done — a chunk can be DECLARED exhaustive (XAGG-only sub-answer) and non-membership in one is supported, not inferred. Live before/after on one machine: CR3 **4 of 8 → 7 of 8**, all four pre-fix rejections carrying the FIR 65/26 absence reason. The validation gate imports the same rule, so the two gates agree — that claim's caveat goes 5 of 5 → **0 of 8**. Hallucination still rejected (unit test + live). **KB4 is a separate defect**, measured not assumed. Deterministic post-pass shipped but **never fired live** — prompt rule 7 carried all 22 runs. New defect split out as Module 67 |
| 40 | M4 → CR3/G6 — the synthesis verifier's attribution-blind second opinion | `MODULE40_RESULT.md` | **closed as superseded** — merged in and judged: 8 live runs, the second opinion **never fired**; measured directly, it works as designed but cannot rescue CR3's negative-inference rejection (3/3). Hallucination still rejected 3/3. Branch left unmerged on origin for Module 59 |
| 52 | KB1–KB9 — the relevance gate could not judge a non-English question against English statute text | `MODULE52_RESULT.md` | done — the gate defect is real and fixed (Layer 1, constant chunks: **0/3 roman-Urdu → 3/3 English**), and it does **not** on its own win the bucket: live answered 18/24 → 19/24, thematic law-half coverage **14/24 in both arms**. KB9 stops abstaining and gets CrPC s.174; KB6's six rounds collapse to 1/1/1; **KB4 regresses 2/3 → 0/3**, isolated to the verifier. Modules 48/49/39 all **still needed**. New defects split out as Modules 58–61 |
| 60 | M4 — routing, and the station predicate | `MODULE60_RESULT.md` | done — see its own row in the tracker; split out Modules 67/68 |
| 67 | The router sends a cross-case aggregate to plain RAG, and the pipeline burns ~8 minutes failing | `MODULE67_RESULT.md` | done — **both halves**. Module 60's resolver-gated override widened from one aggregate kind to a named allow-list of five; **all-32 equality control: exactly four move (M2/M7/CS4/CP1)**, with `gender_breakdown` (KB5/A1) and `case_completeness_scan` (G1) excluded by name. Live 4 runs each, both arms: route unchanged **16/16 → 16/16**, wall-clock **53.0 s → 14.3 s mean**. A measured 360 s ceiling on the Semantic Search dispatch **fired live**; KB3 reproduced the pathology at **468.1 s / 347.5 s `status=error`** before it. M4 **7/7**, KB5 still RAG, G1 still decomposes. The row's own "11 exposed questions" was wrong — it is **16**. New defects **72/73**; Module 68 measured, left open |
| 39 | KB1–KB9 — the "and does our data show it?" half | `MODULE39_RESULT.md` | done — **the data half went from 0 of 48 runs to 8 of 12** on the four KB questions that have an aggregate to reach (KB4 **45** 3/3, KB5 **8** 1/3, KB6 **32** 2/3, KB9 2/3); answered 18/24 → 19/24. Wired only into the RESULT, KB9 abstained 3/3 — the gate was refusing a compound question judged against half the evidence; showing it the data half took KB9 to 2/3 and KB4 to 3/3. Cost **0.59 s mean, +0.6 % end to end**, 0 of 30 dispatches dropped. All-32 equality control exact. **Gold challenged**: 10 FIRs cite PPC 302, not 8. New defects split out as Modules 67/68/69 |
| 74 | KB3 — registering vs. investigating officer | `MODULE74_RESULT.md` | done — **68 of 74 pairs (91.9 %), 6 split: gold exactly**, third independent derivation. `unsupported_officer` preserved for officer-IDENTITY questions by a three-signal predicate, pinned both ways. All-32 equality control moves exactly one question (KB3). Live 3/3 `route=XAGG` with the `XAGG officer_role_pair_overlap` line + 3 in-process. Vocabulary widened after BOTH paraphrases missed (Module 56's finding again); 5 of 6 after, the miss pinned not tuned. KB3's data half still not live — `rag.py` is out of bounds (**77**) and **KB3 routes to XNETWORK 3/3** (**78**) |
| 75 | KB8 — challans sent to court | `MODULE75_RESULT.md` | done — **26 = gold exactly**, and the suspected cause is confirmed and worse: three record types, three quantities (`criminal_record` 33 / `chalaan_dispatch` 26 / `chalaan_outcome` 20), and `chalaan_dispatch` appeared **nowhere** in `xagg.py`. CR7 untouched 2/2, all-32 equality control **byte-identical**. Gold's schema-absence claim derived from the record types present, not declared. Live 3/3 `route=XAGG` + 3 in-process; paraphrases 6/6 (an Urdu verb-stem bug found and fixed by its own test) |
| 71 | G1 — a synthesis that blends denominators across sub-answers | `MODULE71_RESULT.md` | done — root cause named exactly: **73 is the time-of-day sub-answer's denominator**, the only one of G1's five that states it, borrowed onto seized property (45 across 28). Fixed on the GENERATION side; `verifier.py`/`prompts/verifier.txt` untouched. **The defect did not reproduce in 25 pre-fix live G1 runs**, so no answer-rate improvement is claimed — proven instead on a forced-hallucination control: same fabricated synthesis, same real verifier, **`status=error` 3/3 → `status=done` 4/4** with all four gold findings served and the fabrication never served. Rejected synthesis now falls back to the verified sub-answers (`large_scale_aggregate.py`/Module 53's shape). A per-document figure roster was built, measured, **found harmful (G6 0/4 → 7/8 rejections) and deleted**. Regressions 40/40. New defect **82** |
| 76 | KB9 — per-section FIR count | `MODULE76_RESULT.md` | done — **gold challenged for the third time: 10 FIRs cite PPC 302, not 8** (both inflation routes checked and excluded; 25 % is outside tolerance; recommendation is to correct gold). New family answers the per-section question at the FIR grain and leads with the section the query names, generically. Placement immediately below M4 is load-bearing: capturing Module 39's shipped KB9 sub-query would have made it **0/3 with every unit test green**. Live 3/3 in-process, `focus=302 -> 10 FIR(s)`. Gold's SECOND KB9 element needs a two-aggregate PLAN, not a better aggregate (**79**); KB9 routes to XAGG → `graph_recurrence_person` 3/3 (**78**); the sub-query itself routes to SQL (**80**) |
| 65 | KB2 — Qanun-e-Shahadat Arts 38/39 and CrPC s.162, retrieved by no run | `MODULE65_RESULT.md` | done — **the chunks were in the corpus and unreachable, not missing**: Art. 38 `…_3e604153_c176`, Art. 39 `…_c177`, s.162(1) `…_f9908363_c675`, all absent from every generated query and returned at rank 1 by a control carrying the provision's own words. Fixed **entirely in `prompts/statute_hypothesis.txt`** (an evidence book-selection branch + a prohibitive rule 4c); `src/retrieval/` and `rag.py` untouched. KB2 **0/3 → 3/3** on Art. 38 and s.162, answered **1/3 → 3/3**; a Roman-Urdu non-gold paraphrase **0/3 → 3/3 on all three** and reaches gold's conclusion. Equality control **6 of 8 book-pairs unchanged**. KB2's answer still hedges against the provision it was given — split out as **Module 77**. Module 38's `DEFAULT_HYPOTHESES` question answered and closed (stays at 2). KB4's verifier flap measured on a reverted-prompt control and reported, not explained away |
| 77 | KB3/KB8/KB9 — landing Modules 74/75/76's aggregates as plan entries | `MODULE77_RESULT.md` | done — **all three entries correct, two never consulted**. All-32 equality control: exactly KB3/KB4/KB5/KB6/KB8/KB9 resolve, 26 `None`. KB9 re-pointed off `statute_court_stage_join`'s whole-caseload grain **atomically**. Live: **KB3 → XNETWORK 3/3, KB9 → XAGG 3/3**, so their plans fired zero times; **KB8 → RAG 3/3, plan 3/3, 26 = gold exactly**, both halves 2/3. Data half **8-of-12 → 10-of-18**, not 18-of-18 — the shortfall is entirely routing (**78**). The control that module needed came out of this one: an **English paraphrase** of KB3 and KB9 routes to RAG 2/2 and fires its plan 2/2 (*"68 out of 74 … (92%)"*). Vocabulary widened after **two of three paraphrases missed** — Module 56's finding again. KB4/5/6 regression 9/9. New defects **84/85/86** |
| 78 | KB3/KB9 — the router reads their data clause and drops their law clause | `MODULE78_RESULT.md` | done — **both reach RAG 3/3 on their own gold wordings and both plans fire 3/3**. Bisected, not assumed: the query rewriter returns both gold strings **byte-identical 3/3**, so the LLM classifier is the variable, and its own `reason` names the whole-caseload scope cue in each question's DATA clause as its grounds — it classifies the second half of a compound question. Fixed with Module 60/67's resolver-gated pattern one route over (`rag.py::_is_legal_kb_intent()`), **checked LAST**, which is what bounds the blast radius. **All-32 equality control: eight move to a deterministic RAG — exactly the eight KB questions — and only TWO change route.** KB3 answers gold's *"68 of 74 (92%)"* 3/3 where it used to return XNETWORK's refusal; KB9 gets s.174 + PPR 25.31 + **10 FIRs cite PPC §302** 1 of 3, the other two lost to Module 85's verifier. Regression M4 4/4, G3/CR7/G2/G5/M5 2/2, KB4/5/6/8 unchanged — **no question moved**. **Module 63 closed in the same chain** (the gate missed **4 of 8** Roman-Urdu paraphrases, not one; one over-widening caught by its own negative control and narrowed). **Module 68 re-measured and left open.** Misses reported not tuned: route 7/8 on the paraphrase battery (**88**) and a new KB3 paraphrase routes RAG 3/3 but fires no plan (**89**) |
| 82 | KB2 — the answer is built on Module 37 orphans, not on the bar | `MODULE82_RESULT.md` | done — **root-caused; the generation fix was measured, found harmful, and deleted.** Art. 38 reaches the window 0/3 for gold and 3/3 for a paraphrase, split out as Module 97 |
| 85 + 86 | KB4 — the answer is built on the neighbouring subject, and drops the 45 | `MODULE85_RESULT.md` | done — 85 does not reproduce as a rejection; the defect under it reproduces 3/3, 86 reproduces 6/6, and both attempted fixes regressed KB4 3/3 → 1/3 |
| 87 | The **judge** itself — model, polarity rule, score variance | `MODULE87_RESULT.md` | done — judge model is a named env-overridable constant defaulting to **`gemini-3.1-flash-lite`**, chosen on measured free-tier quota (`gemini-2.5-flash`/`3.5-flash`/`3.7-flash` are **20 requests/day** on both working keys; one 3-pass re-score is 96 calls). A **polarity rule** with M7's own worked example: **M7 0.20 → 1.00 3/3**. Two further false negatives justified individually — **CP6 0.30 → 0.90**, **G1 0.53 → 0.97**. **Variance: spread ≥0.3 on 4 of 32 → 1, mean spread 0.106 → 0.025**, and 0.0 on 9 of 9 with the answer held fixed — the model, not the prompt. Not a leniency shift: **seven questions fell**, Creative Generation 0.580 → 0.553, CR2 still 0.00 3/3, KB2 0.17, KB3 0.00; all-32 mean 0.666 → **0.702**. The rule's first version broke KB9 (0.27 → 1.0), attributed and **fenced**. New defects **98/99** |

## Required shape for each result file

Every module's result file must contain all of the following. A module whose
file is missing a section is not finished.

1. **Root cause** — what was actually wrong, with the evidence that
   established it. If it differed from the brief's hypothesis, say so plainly
   and say what the brief got wrong.
2. **Change** — files touched, and why the fix sits where it does.
3. **Unit tests** — the command run, the pass/fail counts, and the new
   regression test pinned to the question's literal gold text.
4. **Live verification** — the exact question sent, the captured `route=`
   event proving which sub-agent handled it, and the **verbatim answer**.
5. **Gold comparison** — measured values against the gold answer's values,
   and an honest verdict. A mismatch that is reported is worth more than a
   match that was tuned for.
6. **Non-gold paraphrase** — the paraphrase used and its result. This is the
   check that separates a capability fix from curve-fitting.
7. **Regression guard** — which previously-passing questions were re-run, and
   their results.
8. **New defects found** — anything discovered but deliberately left
   unfixed, tracked as its own new module rather than folded into this one.

## Infrastructure state when this wave started (2026-09-08)

Verified live, so later runs have something to compare against:

- **Chroma** (`data/chroma_db`): `muhafiz_kb` 7,716 · `muhafiz_community_reports`
  18 · `muhafiz_entity_descriptions` 568.
- **Embeddings**: `e5` via the ngrok tunnel, real 1024-dim vectors returned.
  The tunnel's bare base URL returns 404 (no root route) — check `/health`,
  not `/`.
- **Graph (AGE)**: Case 73 · Incident 73 · Person 430 · Officer 1,155 ·
  Weapon 32 · Date 137. Module 22's `incident_datetime` / `report_datetime`
  are present on **64 of 73** Incidents.
- **Postgres**: `DirectGateway`, 73 cases.

See `WAVE2_ORCHESTRATION_PROMPT.md` for the runbook and the measured machine
constraints that govern how many tracks can run at once.
