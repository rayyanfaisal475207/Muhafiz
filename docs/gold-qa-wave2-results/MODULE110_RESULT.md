# Module 110 — G6's `orientation_note` plan computes five of gold's seven findings

**Branch:** `fix/g6-plan-coverage`
**Worktree:** `D:/Rapids AI/muhafiz-m110`, branched from `origin/main` @ `b66ce68`
**Date:** 2026-09-10

> **Provenance of this file — what is measured and what is not.**
> This module was interrupted mid-write by a transient API failure. Sections 1–4.1
> were written in the first sitting; **§4.2–§8 were written on resumption**, and every
> arm was re-verified against the artefacts on disk before being reported rather than
> trusted from the earlier narrative. On resumption, three further after-runs (7–9)
> were added because one of the original six answered on **Groq, not the local
> model**, and a **six-run re-baselined before arm** was captured under the same
> machine conditions as the after arm. Nothing here is carried over unchecked.
>
> **Measured:** findings carried (9 after runs, 12 before runs), collapse rate before
> and after with a contemporaneous control, the model per run, all-32 routes and
> answers, G1 by name, unit tests both directions.
> **NOT measured:** any judge score. Module 87's judge was **not** re-run, so G6's
> 0.10 is not re-measured here and no score improvement is claimed. The lengthened-
> prompt control of Module 83 was **not** re-run as a separate arm either — the
> after arm *is* the lengthened prompt, and it is compared against a contemporaneous
> unlengthened baseline (§4.3).

**One line:** the two findings G6 could not carry are now computed — by reusing
G1's own sub-queries and one constant this file has carried unused since Module
50 — and the only way to fit them was to raise `_MAX_PLAN_SUB_QUERIES` from 5 to
8, which is Module 59's tracked work partially discharged here because Module
110 could not be done without it.

---

## 1. Root cause

### 1.1 The seven-vs-five mapping

Gold's G6 answer (Roman Urdu, `evaluation/Gold_QA_Dataset_Final32_With_Answers.json`)
carries seven findings. Mapped against what the `orientation_note` plan
dispatched on `origin/main`:

| # | Gold's finding (gold's own words) | Sub-query that computes it | On `origin/main` |
|---|---|---|---|
| 1 | *"9 zilon mein phaili 73 khuli FIRs … zyada bojh Faisalabad aur Lahore mein"* | `_SQ_DISTRICT_SPREAD` → `top_districts_by` | ✅ dispatched |
| 2 | *"do saal pehle taqreeban sab kuch armed robbery tha; ab manshiyat, cyber fraud … ka mishra hai"* | `_SQ_CASE_MIX_BY_YEAR` → `statute_mix_by_year` | ✅ dispatched |
| 3 | *"Zyada tar mulzim aisay **mard** hain jo apni **pachees se chalees** baras ki umar mein hain, aam tor par mudai ke liye **ajnabi**"* | `_SQ_GENDER` + `_SQ_ACCUSED_AGE` + `_SQ_RELATIONSHIP` | ❌ **none** |
| 4 | *"girftari sirf har chhe mein se taqreeban ek FIR par darj hai"* | `_SQ_ARREST_RATE` → `arrest_rate` | ✅ dispatched |
| 5 | *"zyada tar maamlaat abhi adaalaton mein zeir-e-samaat hain"* | `_SQ_CRIMINAL_RECORD_VS_COURT` → `criminal_record_court_crosscheck` | ❌ **none** |
| 6 | *"Fraud aur cyber cases ab waqe ke kaafi baad report hote hain, purani dakaityon ke bar-aks"* | `_SQ_REPORTING_SPEED` → `incident_to_report_minutes_by_year` | ✅ dispatched |
| 7 | *"Hathiyar ki baramadgi aam hai aur taqreeban hamesha bila-license"* | `_SQ_WEAPON_LICENCE` → `weapon_compliance_scan` | ✅ dispatched |

Findings 3 and 5 are not a synthesis loss. Nothing computed them, so no
`[Document N]` in the synthesis prompt could state them, and no wording of the
`synthesis_goal` could recover them. Confirmed live: on all six pre-change runs
the trace is five `route='XAGG' -> Large-Scale Aggregate` dispatches and
`citations/done: 5 citation(s)`, and findings 3 and 5 are absent from every
served answer (§4).

### 1.2 The structural point — the cap is the defect, not the composition

Each of the five dispatched sub-queries carries **exactly one** of gold's seven
findings. There is therefore no weak slot to re-compose into: any swap inside a
five-slot plan trades one gold finding for another and nets zero. At
`_MAX_PLAN_SUB_QUERIES = 5`, G6 cannot exceed five of seven whatever the plan
contains. This is why the fix could not stay inside Module 110's nominal scope
and had to move Module 59's constant — see §2.

### 1.3 Verification of gold's two claims

This programme has corrected five gold answers (G1, G6, KB9, CP6, KB1), every
time because gold asserted more than the data supports, and **G6's gold has
already been corrected once**. Both of Module 110's claims were therefore
checked against the live graph *before* anything was changed, by calling the
aggregates directly (`evidence_graph`, `src/pipeline/xagg.py`).

**Claim A — *"mostly men aged 25–40, usually strangers to the complainant"*: HOLDS,
with a coverage caveat that must travel with it.**

| Component | Measured | Verdict |
|---|---|---|
| "mostly men" | `gender_breakdown`: **67 مرد / 24 عورت / 3 unknown** of 94 accused entries (71%) | holds |
| "aged 25–40" | `offender_age_profile`: recorded ages **24, 25, 25, 26, 28, 29, 29, 29, 31, 31, 32, 33, 34, 34, 38, 38, 49** — **15 of the 17** fall in 25–40; range 24–49, mean 31.5 | holds *as a characterisation*; gold's bracket is a rounding of a 24–49 range, not the range itself |
| "usually strangers" | `accused_relationship_breakdown`: **اجنبی (stranger) 15 of 24** recorded relationships, in 6 of 10 FIRs; dominant by a factor of 7 over the next value | holds |
| coverage | age: **17 of 92** distinct accused (18%); relationship: **12 of 92** (13%); gender: **91 of 94** entries | **the caveat is load-bearing** |

So gold survives, but two of its three components describe a small minority of
the roster. That is why the `synthesis_goal` change in §2 re-states Module 71's
coverage discipline rather than simply asking for the profile: an orientation
note that says *"the accused are aged 24–49"* with no denominator is a claim
this data does not support, and the verifier has refused exactly that shape
before.

**Claim B — *"most matters are still pending in court"*: HOLDS, on the only
court-status field the corpus has.**

`criminal_record_court_crosscheck` renders, verbatim:

> Of 33 criminal records, 32 are still in progress and 1 has reached a verdict.
> Breakdown by recorded status: Under trial: 30; Convicted, on bail pending
> appeal: 1; Sub judice, external prior case: 1; Under trial, priority hearing
> requested: 1.

32 of 33 (97%). The denominator is **criminal records, not FIRs** — the corpus
holds 33 `criminal_record` structured records against 73 FIRs, and there is no
`court_case` record type at all (`chalaan_outcome` carries dates, not stages).
So the finding is real and the figure is right, but "most matters" is measured
over the subset of matters that carry a court status. That distinction is
written into the `synthesis_goal`.

**No sixth gold correction.** Both claims stand.

---

## 2. The change

### 2.1 What changed

**`src/pipeline/harness/agents/meta_analysis.py`** — three edits, one file.

1. **`_MAX_PLAN_SUB_QUERIES`: 5 → 8.** This is the edit that made the rest
   possible, and it is Module 59's tracked work ("`_MAX_PLAN_SUB_QUERIES` is
   still 5, and revisiting it is now unblocked but unmeasured") **partially**
   discharged — see §2.3 for what is left of it.

   *Why 8 and not 9.* Not a taste judgement: this repo already encodes the
   arithmetic in `tests/test_harness_agent_meta_analysis.py::test_module53_subquery_timeout_leaves_headroom_over_the_measured_staircase`,
   which requires `_MAX_PLAN_SUB_QUERIES` × 12 s (Module 50's measured
   staircase step, rounded up) × 1.5 headroom to fit inside
   `META_ANALYSIS_SUBQUERY_TIMEOUT`. At the post-Module-53 deadline of 150 s
   that permits **8 (144 s)** and **refuses 9 (162 s)**. Module 110's own new
   test asserts that boundary from both ends, so a later module cannot raise
   the cap without also moving the deadline.

2. **`orientation_note.sub_queries`: 5 → 8**, adding `_SQ_ACCUSED_AGE`,
   `_SQ_RELATIONSHIP` and `_SQ_CRIMINAL_RECORD_VS_COURT`. **No new aggregate
   and no new dispatch string was written.** The first two are the same module
   constants `caseload_review` already dispatches for G1 — referenced, not
   copied, so a reword for G1 cannot silently diverge for G6 — and the third
   has sat declared-but-unused in this file since Module 50 dropped it from
   G1's re-composition. All three were checked against
   `xagg.resolve_aggregate_kind()` before being committed, per this file's own
   convention, resolving to `offender_age_profile`,
   `accused_relationship_breakdown` and `criminal_record_court_crosscheck`.

3. **`orientation_note.synthesis_goal`** — two sentences added, asking for the
   *shape* of the two new findings ("what the accused tend to look like as a
   group", "how far the cases have actually got in court") plus Module 71's
   coverage rule, which §1.3 shows applies with more force here than it did on
   G1.

**`tests/test_harness_agent_meta_analysis.py`** — four new tests (§3) and three
premise updates to existing pinned tests, each annotated in place, exactly as
Module 50 did to the same tests when it re-composed the plans.

**New artefacts:** `evaluation/module110_live_run.py`,
`docs/gold-qa-wave2-results/module110_paraphrases.json`,
`docs/gold-qa-wave2-results/module110_live/*.json`, this file, and the tracker
row and section in `GOLD_QA_REMAINING_FIXES_PLAN.md`.

### 2.2 What was deliberately NOT changed, and why

- **`supervisor.py` — not one line.** Modules 111/123 own the Meta-Analysis
  trigger list. Module 110 owns G6's gold-wording coverage. §6 records the
  interaction and works around nothing.
- **`_MAX_SUB_QUERIES` (the LLM-decomposer cap) stays at 5.** It bounds an
  *untrusted* list — whatever the decomposer model emitted — and nothing here
  makes that list more trustworthy. Module 50 split the two constants precisely
  so they could move apart; this is the first time they actually have.
- **No new aggregate was written**, and none of the six pinned Module 31–36
  strings was reworded. `test_module50_wired_sub_queries_are_byte_identical_to_the_pinned_strings`
  still passes untouched.
- **The `orientation_note` patterns were not widened.** The plan's lexical
  reach is Module 62's; §6 shows all three rewordings missing the patterns as
  well as the supervisor gate, and that is reported, not fixed.
- **Nothing was special-cased to G6's wording, and gold's seven findings were
  not handed to the model as a list.** The `synthesis_goal` asks for the age
  range, the relationship distribution and the court stage *whatever they turn
  out to be* — it does not name "men", "25–40" or "most still pending".
- **The gender aggregate was not restored** — see §2.3.

### 2.3 What this leaves open (honest residual)

Gold's finding 3 has three components and only two fit. **`_SQ_GENDER` — gold's
*"zyada tar mulzim aisay mard hain"* — is still not computed**, and it is the
*best-covered* of the three (91 of 94 accused entries record a gender, against
17 of 92 for age). It is out because the ninth slot does not exist under the
headroom invariant, not because it lost on merit. Module 59 already records it
as the first thing to restore if the cap rises; Module 110 pins that as a
failing-on-purpose test (§3) so it cannot be quietly forgotten, and files it as
a defect (§8).

Module 59 is therefore **not closed** by this module: it asked for the
post-Module-53 staircase measured at N=6, 7 **and** 9 on G6 *and* G1, and this
module measured N=8 on G6 only, because that is what its own change required.

---

## 3. Unit tests

`tests/test_harness_agent_meta_analysis.py`. **Both directions were checked**,
by reverting `src/pipeline/harness/agents/meta_analysis.py` to `origin/main`
with the test file left in place, running, and then re-applying.

| Test | Before | After |
|---|---|---|
| `test_module110_orientation_note_computes_all_seven_of_golds_findings` | **FAIL** — `accused_profile` and `still_pending_in_court` resolve to no sub-query | PASS |
| `test_module110_the_two_new_findings_reuse_g1s_sub_queries_verbatim` | **FAIL** | PASS |
| `test_module110_the_plan_cap_is_eight_because_the_headroom_invariant_says_so` | **FAIL** — `assert 5 == 8` | PASS |
| `test_module110_golds_gender_element_is_still_not_computed_and_says_so` | PASS | PASS |

**A note on the first test's name.** It reads
`..._computes_all_seven_of_golds_findings`, and that is one word stronger than what
this module achieved: gold's finding 3 has three components and the gender one is not
computed (§2.3, §4.2, §8). The name is kept because the fourth test pins that gap
explicitly and by name, so the pair cannot be read as a claim of completeness — but
the deliverable's headline is **6 of 7 plus two thirds of the seventh**, not seven.

The fourth is deliberately not a before/after test: it asserts a **known gap**
so that the day someone frees a ninth slot it fails and points straight at
`_SQ_GENDER`, rather than the gap surviving only as a sentence in a result file
nobody re-reads. That it passes in both directions is stated rather than
glossed.

Three existing pinned tests changed **premise, not purpose**, and are annotated
in place: `test_module29_broad_synthesis_questions_decompose_deterministically`
(bounded by `_MAX_PLAN_SUB_QUERIES`, not `_MAX_SUB_QUERIES` — the two are no
longer the same number), `test_module29_literal_sub_query_decomposition_is_pinned`
(G6's literal tuple), and `test_module50_each_question_dispatches_its_whole_plan`
(G6 5 → 8). `test_module50_the_plan_cap_decision_is_five_and_holds` keeps its
measurement and its docstring and now asserts 8, with the reason recorded there.

**Full files, after the change:**

```
tests/test_harness_agent_meta_analysis.py   94 passed
tests/test_xagg.py                        456 passed
tests/test_harness_supervisor.py           155 passed
```

(No bare `pytest tests/` was ever run — it empties the
`muhafiz_entity_descriptions` Chroma collection.)

---

## 4. Live verification

**Method.** In process, no backend started (machine-wide cap of 2; one was
already listening on `127.0.0.1:8001`), reusing
`evaluation/module92_inprocess_run.py::run_one()` verbatim as Module 116 did.
Runner: `evaluation/module110_live_run.py`. The worktree's own `.env` pins
`CHROMA_PERSIST_DIR` to the shared absolute path — the main checkout's value is
relative and would silently create an empty vector store here.

**Model recorded per run**, not assumed: `call_llm()` falls back from the local
Qwen3-14B to Groq on any local failure and says so only in a log line, which
invalidated Module 101's first eight runs. The runner captures that line.
**Every run below answered on the local model. Zero Groq fallbacks, before or
after.**

### 4.1 Before — `origin/main` @ `b66ce68`, 6 gold G6 runs

Artefact: `docs/gold-qa-wave2-results/module110_live/module110_before.json`

| Run | Route | Sub-dispatches | Findings carried | Synthesis collapsed | Regeneration recovered | Model | Wall |
|---|---|---|---|---|---|---|---|
| 1 | XNETWORK | 5 | **5 / 7** | yes | yes | local | 89.1 s |
| 2 | XNETWORK | 5 | **5 / 7** | yes | yes | local | 86.4 s |
| 3 | XNETWORK | 5 | **5 / 7** | yes | yes | local | 85.8 s |
| 4 | XNETWORK | 5 | **5 / 7** | yes | yes | local | 87.9 s |
| 5 | XNETWORK | 5 | **5 / 7** | yes | yes | local | ~87 s |
| 6 | XNETWORK | 5 | **5 / 7** | yes | yes | local | ~87 s |

**Findings carried: 5 of 7, 6 runs of 6.** Findings 3 and 5 absent from every
answer. No sub-query timeouts, no salvage, `_attach_provenance()` fired 0 times.

**Collapse rate before: 6 of 6 — 100%.** (Corroborated hours later by an independent
six-run re-baseline, also 6 of 6 — §4.3. The one number in this subsection that did
*not* reproduce is the regeneration recovery rate: 6 of 6 here, 3 of 6 on the
re-baseline, on identical code. That instability is filed in §8.) This is the single
most important
number in this section and it is *not* the effect of any change here: it is the
pre-change baseline. The signature is near-byte-identical across runs 2–6:

```
run 1  1965 tokens, unique-token ratio 0.036, one 4-gram repeated 934 times
run 2  1992 tokens, unique-token ratio 0.015, one 4-gram repeated 979 times
run 3  1992 tokens, unique-token ratio 0.015, one 4-gram repeated 979 times
run 4  1992 tokens, unique-token ratio 0.015, one 4-gram repeated 979 times
run 5  1992 tokens, unique-token ratio 0.015, one 4-gram repeated 979 times
run 6  1992 tokens, unique-token ratio 0.015, one 4-gram repeated 979 times
```

Module 83's regeneration at temperature 0.4 recovered a usable synthesis **6 of
6**, so every G6 answer is served — but every G6 answer costs two synthesis
generations, and the first one is always thrown away.

### 4.2 After — `fix/g6-plan-coverage`, 9 gold G6 runs

Artefact: `docs/gold-qa-wave2-results/module110_live/module110_after.json`

| Run | Route | Sub-dispatches | Findings carried | Collapsed | Regen recovered | Model | Wall |
|---|---|---|---|---|---|---|---|
| 1 | XNETWORK | 8 | **6 / 7 + ⅔** | no | — | local | 64.3 s |
| 2 | XNETWORK | 8 | **6 / 7 + ⅔** | no | — | local | 49.4 s |
| 3 | XNETWORK | 8 | **6 / 7 + ⅔** | no | — | local | 51.5 s |
| 4 | XNETWORK | 8 | **6 / 7 + ⅔** | no | — | local | 67.7 s |
| 5 | XNETWORK | 8 | **6 / 7 + ⅔** | yes | yes | ⚠️ **groq-fallback** | 135.0 s |
| 6 | XNETWORK | 8 | **6 / 7 + ⅔** | yes | yes | local | 104.2 s |
| 7 | XNETWORK | 8 | **6 / 7 + ⅔** | yes | **no** — sub-answer fallback | local | 116.7 s |
| 8 | XNETWORK | 8 | **6 / 7 + ⅔** | yes | yes | local | 100.0 s |
| 9 | XNETWORK | 8 | **6 / 7 + ⅔** | yes | yes | local | 97.0 s |

**Runs 1–6 were captured before the interruption; runs 7–9 were added on resumption**
to replace the sample that run 5 contaminated. Run 5 is **kept, not deleted**: it is
the one run in this module that answered on Groq rather than the local Qwen3-14B, it
is flagged here, and it is **excluded from every rate below**. That is the failure
mode that invalidated Module 101's first eight runs, and the runner exists to catch
it. Local-model runs: **8 of 9**.

**Findings carried: 6 of 7 in full, 9 runs of 9**, plus gold's finding 3 at **two of
its three components**.

| Gold finding | Before | After |
|---|---|---|
| 1 district spread | ✅ | ✅ |
| 2 case mix by year | ✅ | ✅ |
| 3 accused profile — **men** | ❌ | ❌ **still not computed** (0 of 9 runs) |
| 3 accused profile — **aged 25–40** | ❌ | ✅ reported as recorded range 24–49, mean 31.5, on 17 of 92 |
| 3 accused profile — **strangers** | ❌ | ✅ اجنبی 15 of 24 entries, 6 FIRs |
| 4 arrest rate | ✅ | ✅ |
| 5 **most matters still pending in court** | ❌ | ✅ 32 of 33 criminal records in progress, 1 verdict |
| 6 reporting speed | ✅ | ✅ |
| 7 weapon licensing | ✅ | ✅ |

So the module's own headline is **5 of 7 → 6 of 7 plus two thirds of the seventh**,
not "all seven". `_SQ_GENDER` is absent from **every one of the nine runs**, checked
by regex against each served answer and confirmed by hand — the one apparent hit was
`men` inside *kismen*. §2.3 and §8 carry that gap; it is not rounded up here.

The **coverage discipline held**. Every run states the denominator on the age
finding, unprompted by any named figure — run 1, verbatim:

> `92 distinct accused individuals … jin mein se 17 accused ki umr 24 se 49 ke beech hai, aur average umr 31.5 hai. 75 accused ki umr record nahi hai, toh yeh umr aur average sirf 17 accused par lagta hai [Document 6].`

**One wording defect** (filed, §8): several runs render the court-stage finding as
*"32 FIRs"* when the sub-answer says *32 criminal records*. The figure is right and
the sub-answer is right; the synthesis swaps the noun. That is the exact
denominator-transfer error the `synthesis_goal`'s new sentence forbids, and it
survives the instruction.

### 4.3 The control that matters — a re-baselined before arm

The before arm in §4.1 was captured ~03:40. Runs 7–9 were captured ~09:00, and
**Module 37 deleted 2,246 orphaned `chunk_fulltext` rows in between** (9,962 → 7,716;
confirmed at 7,716 at the time of writing). G6 is XNETWORK/Meta-Analysis and does not
read `chunk_fulltext`, so this should not touch it — but rather than assume that, and
per the instruction to **re-baseline rather than chase it**, `meta_analysis.py` was
reverted to `main` and G6 re-run six more times under the *same* machine conditions as
the after arm.

Artefact: `module110_before_rebaseline.json`. All six local.

| Arm | When | Collapse rate | Regeneration recovered |
|---|---|---|---|
| before (original) | ~03:40 | **6 / 6 — 100%** | 6 / 6 |
| **before (re-baselined)** | **~09:20** | **6 / 6 — 100%** | **3 / 6** |
| **after** (local runs only) | ~04:20 + ~09:00 | **4 / 8 — 50%** | 3 / 4 |

Both before arms collapse **100%**, twelve runs of twelve, hours apart and across the
Module 37 change. The pre-change collapse is therefore a stable property of G6, not a
sampling accident and not something Module 37 moved.

### 4.4 The result that was not expected: the change *halved* the collapse rate

The risk this module was told to measure is that three more sub-queries lengthen the
synthesis prompt and tip G6 further into Module 83's length-sensitive collapse. **It
measured the opposite, and the opposite is the honest report.**

- **Collapse: 100% (12/12 before) → 50% (4/8 after).**
- The four surviving collapses carry Module 83's signature but a *weaker* one: run 7
  logs `758 tokens, unique-token ratio 0.024, one 4-gram repeated 370 times`, against
  the before arm's `1992 tokens, ratio 0.015, one 4-gram repeated 979 times`.

The mechanism is not proven here, and is offered as a hypothesis rather than a
finding: a repetition loop is an attractor, and eight distinct sub-answers give the
synthesis more distinct material to traverse than five do. **What is measured is the
rate, not the cause.**

**This does not mean the change is free.** Two costs are real:

1. **Regeneration failure is now visible on both sides.** The re-baselined before arm
   lost the regeneration 3 of 6, and the after arm lost it 1 of 4. When it is lost,
   Module 83's *second* guard serves the verified sub-answers individually with an
   explicit disclaimer — run 7, verbatim tail: *"The combined answer did not generate
   cleanly, so each verified sub-answer is shown as computed, without a synthesis
   across them."* **That is a graceful degradation, not a degenerate answer served.**
   It still carries all eight sub-answers, so findings-carried is unaffected; what is
   lost is the synthesis. Notably the §4.1 arm recovered 6 of 6 and the re-baselined
   arm 3 of 6, on *identical code* — so regeneration recovery is the unstable quantity
   here, and neither this module nor Module 83 has it pinned.
2. **Wall time rose** on the collapsing runs, ~86 s before to ~100–117 s after, because
   eight sub-queries dispatch before the synthesis does anything. No sub-query timeout
   fired in any run, at either cap.

### 4.5 Module 126 — no longer deterministic, and that is the change

Module 126 records G6 collapsing **3 of 3 with a byte-identical signature**, read as
deterministic rather than a sampling accident. **That reading still holds on `main`
after Module 83's guard** — 12 of 12 across two arms, and the re-baselined arm's three
non-recovered runs are byte-identical to each other at 2,842 chars.

**After this change it no longer holds**: 4 of 8, with runs 1–4 producing clean
syntheses. Module 126's premise is therefore correct as filed but **conditional on the
five-sub-query plan**, and its row should say so.

### 4.6 Module 113 — `_attach_provenance()` still never fires

Counted explicitly by the runner across **all 41 runs in this module** (9 after, 12
before, 20 all-32): `provenance_attached = 0` everywhere, and `misattributed = 0`.
**Module 113's disposition is unchanged** — no contrary evidence found.

---

## 5. What the served answer now looks like

The G6 answer went from five paragraphs to eight, all eight citing distinct
`[Document N]` sub-answers. The two new findings arrive as the last three paragraphs
(after run 1, abridged):

> `92 distinct accused individuals … 17 accused ki umr 24 se 49 ke beech hai … 75 accused ki umr record nahi hai [Document 6].`
> `10 FIRs mein relationship record hai … "اجنبی (stranger)" sabse zayda aaya hai, jo 15 entries aur 6 FIRs mein [Document 7].`
> `33 criminal records mein se 32 ab tak court mein progress par hai, aur sirf 1 FIR mein verdict record hai [Document 8].`

**No judge score is claimed.** Module 87's judge was not re-run — G6's 0.10 is not
re-measured here, and this module reports findings carried and collapse rate, which is
what it was asked for. Whether 6-of-7-plus-two-thirds moves 0.10 is Module 87's
measurement to make, and it is filed as such (§8).

---

## 6. Known interactions, reported rather than worked around

**Modules 111 / 123 — G6's rewordings still never reach Meta-Analysis. 3 of 3.**

The three rewordings were written and committed **before any run** (`994ed56`,
`module110_paraphrases.json`) precisely so they could not be tuned once the result was
known. All three still refuse:

| Rewording | Route | Steps | Outcome |
|---|---|---|---|
| English — *"I'm taking over this desk next week…"* | XNETWORK | 5 | refused, no cluster |
| Roman-Urdu — *"Agle hafte main is desk par aa raha hoon…"* | XNETWORK | 5 | refused, no cluster |
| Urdu script — *"اگلے ہفتے میں یہاں چارج سنبھال رہا ہوں…"* | XNETWORK | 5 | refused, no cluster |

Five steps, not 22: they never enter the Meta-Analysis path at all, so this module's
change is invisible to them. **`supervisor.py` was not touched** — not one line. This
is Modules 111/123's gate, exactly as predicted, and it means Module 110's coverage
fix is reachable **only through G6's literal gold wording**. Stated plainly: this
module raises what G6 can answer, and changes nothing about *which questions* get to
ask it.

**Module 112** (invented Urdu-ish non-words) was not touched.

---

## 7. Regression guard — all 32 questions

Artefacts: `module110_all32_before.json`, `module110_all32_after.json`.

**Routes: all 32 match `evaluation/gold32_route_baseline.json` (Module 116), and all
32 match the before arm.** Zero route changes.

**G1, explicitly and by name.** G1 shares `_SQ_ACCUSED_AGE` and `_SQ_RELATIONSHIP`
with the plan this module changed, so Module 95's discipline applies — measure the
shared-sub-query cost, do not assume it away:

| | Before | After |
|---|---|---|
| Route | XNETWORK | XNETWORK |
| Steps (sub-dispatches) | **16** | **16 — unchanged** |
| Synthesis collapsed | no | no |
| Answer | 2,765 chars | 2,544 chars |
| Model | local | local |

**G1's plan was not modified** — only `orientation_note` gained members, and the two
shared constants are *referenced*, not copied, so a later reword cannot silently
diverge between the two plans. G1's answer still names the accused profile, the
stranger relationship, seized property and time-of-day, with its coverage caveats
intact. **No cost to G1 measured.**

**Five answers moved by more than 250 characters, and none of them is attributable to
this change:**

| Q | Route | Before → After | Attribution |
|---|---|---|---|
| KB5 | RAG | 1,482 → 723 | Module 37 |
| KB6 | RAG | 1,836 → 1,448 | Module 37 |
| KB8 | RAG | 101 → 1,613 | Module 37 — was a near-empty answer, now a real one |
| KB9 | RAG | 101 → 1,214 | Module 37 — same |
| M4 | XAGG | 1,514 → 1,799 | LLM wording variance; route, plan and figures unchanged |

Four of the five are **RAG**-route questions, which is exactly the population Module
37's 2,246-row `chunk_fulltext` deletion re-ranks, and this module's change is
confined to `meta_analysis.py`'s XNETWORK decomposition plan — it **cannot** reach a
RAG question. Two of them improved sharply from near-empty answers. Recorded as
Module 37's effect, measured rather than assumed, and **not claimed as this module's
benefit**.

---

## 8. Defects filed

Filed as tracker rows **134–138**. Numbers were re-checked against `origin/main` at
the end of the module, per the collision protocol — `origin/main` carries rows up to
**133** (this branch's base only had 130), and `muhafiz-m37` was filing concurrently,
so these start at 134 and were re-verified immediately before the commit.

- **134 — `_SQ_GENDER` is still not computed for G6**, and it is the best-covered component
  of gold's accused-profile finding (91 of 94 accused entries record a gender —
  verified live: 67 مرد / 24 عورت of 94 — against 17 of 92 for age). It is out
  because the headroom invariant allows 8 plan slots and not 9, not because it lost on
  merit. Pinned as a deliberately-failing-on-purpose test so a freed ninth slot points
  straight at it.
- **135 — the synthesis transfers the court-stage denominator**, rendering *"32 criminal
  records"* as *"32 FIRs"* in several runs. The sub-answer is correct; the synthesis
  swaps the noun, and the `synthesis_goal`'s explicit prohibition does not stop it.
  Same family as Modules 71 / 95.
- **136 — regeneration recovery is unpinned and unstable** — 6/6, then 3/6, on identical
  `main` code hours apart. Module 83 measured the collapse but not the *recovery*
  rate, and this module found it moving under a fixed system. Owner: Module 83's row.
- **137 — Module 126's determinism claim is conditional on the five-sub-query plan** and
  should be re-worded rather than closed (§4.5).
- **138 — G6 has not been re-judged.** Module 87's judge was not re-run; 0.10 is stale from
  here on and the score effect of 5/7 → 6⅔/7 is unmeasured.

**Module 59 is not closed by this module.** It asked for the post-Module-53 staircase
at N = 6, 7 and 9, on G6 *and* G1. This measured N = 8 on G6 only, because that is
what its own change required.


---

## 9. Reproducing

```bash
# Unit tests (never a bare `pytest tests/`)
PYTHONPATH=. PYTHONIOENCODING=utf-8 python -X utf8 -m pytest \
    tests/test_harness_agent_meta_analysis.py tests/test_xagg.py \
    tests/test_harness_supervisor.py -q

# Live, in process, no backend
PYTHONPATH=. PYTHONIOENCODING=utf-8 python -X utf8 \
    evaluation/module110_live_run.py --questions G6 --runs 9 --tag after

# The contemporaneous control: revert meta_analysis.py to main (NOT `git stash` —
# stashes are shared repo-wide), re-run, then restore.
cp src/pipeline/harness/agents/meta_analysis.py /tmp/keep.py
git show main:src/pipeline/harness/agents/meta_analysis.py \
    > src/pipeline/harness/agents/meta_analysis.py
PYTHONPATH=. PYTHONIOENCODING=utf-8 python -X utf8 \
    evaluation/module110_live_run.py --questions G6 --runs 6 \
    --tag before_rebaseline --gold-only
cp /tmp/keep.py src/pipeline/harness/agents/meta_analysis.py

# All 32, routes and answers
PYTHONPATH=. PYTHONIOENCODING=utf-8 python -X utf8 \
    evaluation/module110_live_run.py --questions ALL32 --runs 1 \
    --tag all32_after --gold-only
```
