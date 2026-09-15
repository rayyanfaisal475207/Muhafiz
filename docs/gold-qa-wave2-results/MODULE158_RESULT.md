# Modules 158 / 164 — semantic sub-agent selection under the trigger list

**Branch** `feat/supervisor-semantic-selection` · **worktree** `D:/Rapids AI/muhafiz-m158` · branched from `origin/main` @ `b782446` (Modules 79, 145 and 150 included); rebuilt onto `origin/main` @ `4bfd840` (Modules 173 and 161 merged underneath) before the PR — the probe and the unit suites were re-run on the merged table, §6.3. Covers tracker rows **158** and **164**; measures, without closing, row **162**.

**Headline:**

1. **Row 158 is closed deterministically, not semantically.** A matched
   decomposition plan now selects Meta-Analysis on its own — one clause in
   `classify_to_subagent()` (§2). Q1 reaches `most_cited_section_by_district`
   live through `/api/chat` 3/3 and answers with PPC §34's per-district
   spread (§4); Module 79's four pre-written paraphrases all select it.
   The clause is free and independent of the model server, so a dead tunnel
   (which this session had for ~17 hours, 2026-09-14 23:15 → 2026-09-15 16:30; every live row below is from after it came back) cannot send a plan-matched
   question back to the wrong agent.
2. **Row 164 is built — Module 145's mechanism at the supervisor
   chokepoint** (`harness/subagent_selection.py`, reusing 145's scorer via
   an additive `SemanticGate` class in `semantic_dispatch.py`). Module
   116's six paraphrases: the three that were refused now reach Meta-Analysis
   **3/3** through the semantic clause (0.985 / 0.835 / 0.999); the three that
   passed still pass through the trigger list. **5/6 overall, not 6/6** — G6's
   Urdu rewording no longer reaches the *harness* at all: today's router
   classifies its *"نوٹ لکھیں"* as `output_format=file_docx` 3/3, and
   `main.py`'s cutover check hands it to the legacy orchestrator before any
   sub-agent is selected (§4.2; filed as 179 — router-level, not this layer).
3. **The margin is +0.485** at the shipped threshold 0.40: lowest fired true
   positive 0.648, highest hazard that reaches the layer 0.162 (§6). Of 250
   non-target items, **0** have a plan as their best description at or above
   0.40 — after one round of description edits on two pre-written hazards
   that fired in round 1 at 0.996 and 0.525 (§6.3, recorded) — the hazard set is the 29 non-Meta-Analysis gold questions,
   Module 145's 30, Module 92's 87 non-Meta-Analysis paraphrases, Module
   144's 13, router.txt's exemplars and 20 pre-written for this layer.
4. **All 32 gold questions select the same sub-agent, byte-identical**, and
   CR3/G1/G6 still select Meta-Analysis through the trigger list, not the
   new layer (§5). The semantic layer is reachable by exactly four gold
   questions (D1, S2, S3, CR2) and lands all four on an absorbing class.
5. **Row 162: the G1 hazard does NOT leave the aggregate layer's candidate
   set by virtue of this module** — `route_query()` runs Module 145's layer
   *before* the supervisor, so the router still scores that rewording at
   0.351 on `statute_mix_by_year`. It *would* leave if Module 145's table
   absorbed the five plan descriptions (measured offline: its argmax moves
   to `caseload_review` at 0.835; 37 argmax flips in the corpus, none a
   fired true positive). Filed on row 162 as the follow-up measurement, not
   bundled here (§6.5).

---

## 1. Root cause

### 1.1 Row 158 — the parallel-list mechanism, traced on Q1

`classify_to_subagent()` on `origin/main` @ `b782446`, for *"How is the
most frequently cited offence pattern distributed across districts?"*
(route `XAGG`, `cross_case`, Module 79 §4.1 live 3/3):

| clause | result | why |
|---|---|---|
| `route == "XAGG" and (_TIME_COMPARISON… or _xagg_answers_in_one_call())` | **False** | `_xagg_answers_in_one_call()` calls `_match_decomposition_plan()` first; the plan `most_cited_section_by_district` matches, so the one-call skip is **vetoed** — exactly as Module 79 designed |
| `elif allow_meta_analysis and any(_META_ANALYSIS_TRIGGER_PATTERNS)` | **False** | no trigger names this shape; the 40-odd patterns are Module 11/29's mining of the 18 gold compound questions plus three plan families' paraphrase words |
| `else:` … `_ROUTE_TO_SUBAGENT.get("XAGG")` | **Large-Scale Aggregate** | the veto had no effect: the question lands on the very agent it was vetoed away from |

The plan's patterns and the trigger list are two definitions of "what
Meta-Analysis decomposes", and only one of them was ever consulted for
selection. Module 29 added the three plan families' paraphrase words to
*both* lists by hand; Module 79 added two plans to one list and could not
touch the other (`supervisor.py` was Module 145's file that session). The
result: the plan matches, vetoes, and is never run — measured live 1/1 by
Module 79 (§4.1 "after" row, same answer as before) and reproduced here
before any change (§4, before arm).

### 1.2 Row 164 — 3/6 on Module 116's paraphrases

Module 116 §6: six pre-written rewordings of CR3/G1/G6 route correctly 6/6
(all `XNETWORK`) and reach Meta-Analysis **3/6**. Traced on `b782446`
through the same function with `route=XNETWORK, case_scope=cross_case`:

| paraphrase | trigger hit | plan hit | selected |
|---|---|---|---|
| CR3-en *"…deal with both of their complaints in the same way on paper?"* | `\b(the\s+)?same\s+way\b` | `record_consistency` (same pattern) | Meta-Analysis |
| CR3-ur *"…یکساں طور پر نمٹائے گئے؟"* | — | — | **Cross-Case Linkage** → refused |
| G1-en *"Put your crime-analyst hat on … looks off or deserves a closer eye."* | — | — | **Cross-Case Linkage** → refused |
| G1-roman_ur *"…kya cheez ghair mamooli lagti hai…"* | `\bghair\s*[- ]?\s*mamooli\b` | `caseload_review` | Meta-Analysis |
| G6-en *"Write a brief starter note for an officer who has just been posted here…"* | — | — | **Cross-Case Linkage** → refused |
| G6-ur *"یہاں نئے تعینات ہونے والے افسر…"* | `نئے\s*تعینات` | `orientation_note` | Meta-Analysis |

The variable is trigger-pattern membership, not language: G6's Urdu passes
because Module 29 put *"نئے تعینات"* on the list and its English fails
because *"just been posted"* is not *"newly posted"*. The three that pass
would also pass through the plan clause alone (every trigger word Module 29
added is also a plan pattern) — which is why row 158's clause does nothing
for row 164: the three refusals miss both lists. They need a layer that
matches on what the question *means*.

### 1.3 The mechanism, once

The same disease Module 145 named at four layers, at the fourth. Module 145
built the semantic fallback at the aggregate chokepoint and explicitly left
this one (§8, row 164). This module builds it, and — because the plans are
Meta-Analysis's own definition of its capability — also makes the plan
match itself sufficient, so the trigger list stops being a second list that
has to agree.

---

## 2. The change, and what did not change

### 2.1 `src/pipeline/harness/supervisor.py`

Three new pure helpers in front of `classify_to_subagent()`:

- `_matched_plan_name(query_text)` — the Module 29 plan the text matches
  (lazy import, same circularity reason as `_xagg_answers_in_one_call()`).
- `_meta_analysis_decided_by_phrase(route, query_text)` — how the
  deterministic chain decides: `"xagg-one-call"`, `"trigger"`,
  `"plan:<name>"`, or `None` (undecided).
- `semantic_selection_applies(route_result, query_text, *, allow_meta_analysis)`
  — the **one** statement of when the semantic layer is consulted: never for
  a nested sub-query, `DIRECT`, a file request, or a within-case question
  (the demotion guard would discard the selection), and never when the
  chain has decided. Used by `handle()` to decide whether to pay for the
  scorer and by `classify_to_subagent()` to decide whether to read it.

Two new `elif` clauses in `classify_to_subagent()`, **after** the trigger
clause, which is unchanged in body and order:

```python
elif allow_meta_analysis and (_plan := _matched_plan_name(query_text)) is not None:
    sub_agent = META_ANALYSIS                      # row 158
elif semantic_selection_applies(...) and (_sem := subagent_selection.lookup(query_text)) is not None:
    sub_agent = META_ANALYSIS                      # row 164
```

Both flow through the existing `case_scope` demotion guard like the trigger
clause does. All three Meta-Analysis sources log one
`SUBAGENT-SELECT Meta-Analysis via trigger | plan=<name> | semantic=<plan>(score) …`
line, so a live log says which decided.

`Supervisor.handle()` gains one awaited call between `route_query()` and
`classify_to_subagent()`: `await subagent_selection.prepare(query_text)`
when `semantic_selection_applies()`. That is the async half; the same
sync/async split, for the same reason, as Module 145's
`prepare()`/`lookup()` under `resolve_aggregate_kind()`.

### 2.2 `src/pipeline/harness/subagent_selection.py` — new

- `PLAN_DESCRIPTIONS` — one sentence per decomposition plan, keyed by plan
  name, written from the plan's sub-queries and synthesis goal (what it
  computes), never from its trigger words (a test bans the trigger list's
  load-bearing phrases from them). A test pins the key set equal to
  `_DECOMPOSITION_PLANS`, so a plan cannot be added without a description.
- `NEIGHBOUR_DESCRIPTIONS` — nine absorbing entries for the sub-agents a
  cross-case question could otherwise belong to (within-case facts, case
  summary, timeline, investigative analysis, report file, data-quality
  audit, global theme summary, and the two half-question rankings).
- `CAPABILITY_DESCRIPTIONS` = the five plans + the seven neighbours + **all
  of Module 145's aggregate table, absorbed** (`xagg:<kind>`). Every
  one-call aggregate is a neighbour of some plan, and those 40 sentences
  were measured once already; when m161's new aggregate lands in that
  table it becomes one more absorbing class here automatically.
- `DISPATCHABLE_KINDS` = the five plan names. Nothing else can be selected.
- `SUBAGENT_SELECTION_THRESHOLD = 0.40`, measured (§6). Two more absorbing
  neighbours were added in round 2 — `section_ranking_only`,
  `district_ranking_only` — the halves of the chained plans (§6.3).
- `gate = SemanticGate(tag="SEMANTIC-SELECT", …)`; `prepare()`, `lookup()`,
  `reset_for_tests()`, `seed_for_tests()` delegate to it.

### 2.3 `src/pipeline/semantic_dispatch.py` — additive only

- `score_descriptions(query_text, descriptions)` — Module 145's scorer with
  the table as a parameter. `_score_descriptions(q)` is now a one-line
  wrapper over it; behaviour identical.
- `class SemanticGate` — the prepare/lookup/cache/cooldown state machine as
  an object (table, dispatchable set, threshold, log tag, its own cache and
  cooldown). Module 145's module-level functions and `_decisions` state are
  **not** migrated onto it — that would be a refactor of measured code for
  no behavioural gain, and the two gates should fail independently.
- `from typing import Callable` added.

**Merge reconciliation with Module 161 (PR #99, merged while this ran):**
its `applicant_accused_overlap` entry in `CAPABILITY_DESCRIPTIONS` and this
module's edits (`_score_descriptions()` ~line 300, the class appended after
it) do not overlap; both are in. Its entry is absorbed into this layer's
table automatically (`xagg:applicant_accused_overlap`), which is why the
table is re-measured in §6.3 round 3. `xagg.py` is untouched by this PR.

### 2.4 Tests and evaluation

`tests/test_subagent_selection.py` (new, §3). `evaluation/module158_selection_probe.py`
(the offline measurement), `evaluation/module158_live_run.py` (the
`/api/chat` runner with per-run log evidence). Corpora written before any
score was seen: `docs/gold-qa-wave2-results/module158_targets.json` (22),
`module158_hazards.json` (20). Outputs: `module158_probe.json`,
`module158_rerank_cache.json` (every score, keyed by table hash),
`module158_live/`.

### What did NOT change

- **`_META_ANALYSIS_TRIGGER_PATTERNS`** — not one pattern added, removed or
  reordered. CR3/G1/G6 select through it (§5).
- **No plan definition** in `meta_analysis.py`; the file is untouched (its
  Module 79 comment on plan (4), *"Reaching this plan LIVE needs a
  `_META_ANALYSIS_TRIGGER_PATTERNS` entry"*, is now stale — noted, not
  edited, to keep this diff out of that file).
- **`xagg.py`**, **`router.py`**, Module 145's table, threshold and
  module-level functions.
- **No Q1 or Q3 special case.** The plan clause is for every plan; the
  description table was revised once, on two pre-written hazards, and the
  revision is recorded in §6.3.
- **The recursion guard**: both new clauses sit behind `allow_meta_analysis`
  exactly like the trigger clause; a nested sub-query is classified as if
  none of this existed.

---

## 3. Unit tests

`tests/test_subagent_selection.py` — **32 tests**, every semantic decision
planted with `subagent_selection.seed_for_tests()` at its *measured* score
from `module158_probe.json`; no test reaches the model server.

**Both directions.** With `origin/main`'s `supervisor.py` swapped in (the
new module files present so the test file imports; `semantic_selection_applies`
stubbed to `False`), **16 fail**:

```
test_q1_matches_a_plan_and_selects_meta_analysis_without_a_trigger_or_a_score
test_module_79s_four_pre_written_paraphrases_select_meta_analysis[×4]
test_plan_clause_is_logged_as_a_subagent_select_line
test_module_116s_three_refused_paraphrases_select_meta_analysis_once_prepared[×3]
test_q1s_english_rewording_is_claimed_by_the_xagg_one_call_skip_above_this_layer
test_semantic_clause_is_logged_with_score_and_runner_up
test_semantic_selection_applies_only_where_meta_analysis_is_reachable_and_undecided
test_handle_prepares_only_when_selection_applies
test_prepare_never_raises_and_disables_itself_on_a_dead_scorer
test_cr3_g1_g6_still_select_meta_analysis_through_the_trigger_list
test_only_four_gold_questions_can_reach_the_semantic_layer_and_all_four_are_absorbed
```

The other 16 pass in both states by design: the table/plan key-set pin,
Module 145's table absorbed, descriptions-are-sentences, the banned
trigger-phrase check, **the threshold pin (0.40)**, the recursion and
demotion guards on both new clauses, the unprepared-question fall-through,
below-threshold and absorbing-best non-selection, **the 29 non-Meta-Analysis
gold hazards** (each measured best is absorbing or below threshold; the 8
within-case KB questions never reach the layer), the M4 thinnest-margin pin,
`test_all_32_gold_questions_select_the_same_subagent_as_before` (exact
expected sub-agent per question, from the committed route dicts) and the
force-armed all-32 control. `test_the_probe_records_the_measurement_the_threshold_was_chosen_from`
re-asserts 0.162 / 0.648 / gold moved 0 and the table hash, so a description
edit that was not re-measured fails here.

Suites run on this branch: `test_subagent_selection`, `test_semantic_dispatch`,
`test_harness_supervisor`, `test_harness_agent_meta_analysis`,
`test_module79_chained_plans`, `test_router`, `test_xagg`,
`test_harness_tool_xagg`, `test_harness_agent_large_scale_aggregate`,
`test_config` — **1,089 passed, 0 failed** on `origin/main` @ `f9b2399`
and **1,107 passed, 0 failed, 0 errors** after rebuilding onto `4bfd840`
(Module 161's 18 tests included; junit counts, since the project's
`sessionfinish` hook eats pytest's summary line). No bare `pytest tests/`.

---

## 4. Live verification

**Backend:** own process on `:8158` from this worktree (`.env` copied from
`muhafiz-m145` with the absolute `CHROMA_PERSIST_DIR`), platform-admin, All
Cases, through `/api/chat` with `evaluation/gold32_run.py`'s login/ask/parse
(`evaluation/module158_live_run.py` wraps them and reads this request's
`SUBAGENT-SELECT` / `SEMANTIC-SELECT` / `Falling back` lines out of
`module158_live/backend_8158.log`). **"Before" is the pristine main
checkout @ `b782446` on `:8079`** (one run; Module 79 §4.1 has three more on
`50862d6` with the same outcome). Model server `/health` 200 before every
arm. **Model per run** is read from the log: a run with zero `Falling back`
lines answered entirely on the local model.

A first backend on `:8158` was started on 2026-09-14 at 23:0x, minutes
before the tunnel died; **no request was served by it** and its log is kept
as `backend_8158_DISCARDED_tunnel_dead.log`. The Q1 arm below ran on
2026-09-15 16:55–16:59 on the round-1 description table
(`backend_8158_round1_q1.log`); Q1 is decided by the plan clause, which
does not read the table, so it was not re-run. Every other row ran on the
shipped (round-2) table after a restart.

### 4.1 Q1 — Module 79's live example, `/api/chat`

| arm | run | route | sub-agent | selection line | sub-queries | model | s | answer served |
|---|---|---|---|---|---|---|---|---|
| before (`main`, `:8079`) | 1 | XAGG | **Large-Scale Aggregate** | *(none — no `SUBAGENT-SELECT` line exists on main)* | — | local | 35.4 | *"The provided data shows the number of cases recorded across different districts but does not specify the most frequently cited offence pattern…"* |
| **after** | 1 | XAGG | **Meta-Analysis** | `SUBAGENT-SELECT Meta-Analysis via plan=most_cited_section_by_district` | chained (0.6 s) | local, 0 fallbacks | 90.8 | *"The most frequently cited offence pattern is under **PPC §34**, which appears in **40 FIR(s)** [Document 1]. Across districts, these 40 cases are distributed as follows: فیصل آباد: 12 · لاہور: 10 · راولپنڈی: 4 · کراچی ایسٹ: 4 · چنیوٹ: 3 · حیدر آباد: 3 · اسلام آباد: 2 · کراچی وسطی: 2"* |
| after | 2 | XAGG | Meta-Analysis | same line | chained (0.2 s) | local, 0 | 88.8 | same figures, same wording |
| after | 3 | XAGG | Meta-Analysis | same line | chained (0.2 s) | local, 0 | 92.8 | same figures, same wording |

Raw: `module158_live/module158_q1_before_main.json`, `module158_q1_after.json`.
The after-answer is Module 79's in-process answer (§4.2 of its result file)
now produced through the API: the plan's chained step, the synthesis, the
grounding verifier. The 89–93 s is one synthesis call plus the verifier on
the shared serial server, versus Module 79's 35–46 s in-process on a quieter
day; the chain itself is 0.2–0.6 s.

### 4.2 Module 116's six paraphrases — reach before (3/6) and after

Before is Module 116 §6 (2026-09-14; the trigger list is unchanged, so the
three refusals reproduce by construction — §1.2 traces them). After, one
run each on the shipped table:

| paraphrase | before (M116) | after: route → sub-agent | selection line | sub-queries | model | s |
|---|---|---|---|---|---|---|
| CR3 · en | Meta-Analysis | XNETWORK → **Meta-Analysis** | `via trigger` | XAGG×3 (plan) | local, 0 | 121.5 |
| CR3 · ur | **refused** | XNETWORK → **Meta-Analysis** | `via semantic=record_consistency(0.985) runner_up=xagg:fir_register_completeness(0.000)` | RAG×2 (LLM decomposer — see 178) | local, 0 | 223.2 |
| G1 · en | **refused** | XNETWORK → **Meta-Analysis** | `via semantic=caseload_review(0.835) runner_up=xagg:statute_mix_by_year(0.351)` | XAGG×4 (LLM decomposer) | local, **1 fallback** | 175.9 |
| G1 · roman_ur | Meta-Analysis | XNETWORK → **Meta-Analysis** | `via trigger` | XAGG×5 (plan) | local, 0 | 126.9 |
| G6 · en | **refused** | XNETWORK → **Meta-Analysis** | `via semantic=orientation_note(0.999) runner_up=caseload_review(0.305)` | XAGG×4 (LLM decomposer) | local, 0 | 102.4 |
| G6 · ur | Meta-Analysis | **never reaches the harness** (3/3: first run + 2 re-runs) | *(none)* | — | local, 0 | 55–66 |

**Reach: 3/6 → 5/6**, and the composition of the five matters: the three
Module 116 refusals are all reached now, each by the semantic clause with
the right plan as its best description and a runner-up ≤ 0.351. G1-en's
answer names the three recurring accused, 30 unlicensed weapons and the 73
incomplete FIRs; G6-en's gives the district concentration, the 2024→2026
mix change and the reporting delay — gold's substance for both. CR3-ur is
reached but answered by the LLM decomposer's two RAG sub-queries, not the
`record_consistency` plan, and its answer is the weaker "same date, same
sections, cannot confirm" — **reaching the sub-agent is not reaching its
plan** (178, §8).

G6-ur is the honest loss, and it is not this layer's: `route_query()`
returns `output_format="file_docx"` for it today (checked in-process:
*"Output format is file_docx as the user explicitly asked for a 'note'
(document)"*), so `main.py`'s cutover check (`classified_output_format ==
"chat"`) sends it to the legacy orchestrator, whose XNETWORK gate refuses
(nearest cluster 0.186 vs 0.145). Module 116 got `chat` for the same text
on 2026-09-14. No `supervisor` line is emitted at all, so no selection
clause — trigger or semantic — is consulted. Filed as **179**.

Raw: `module158_live/module158_m116_after.json`, `module158_m116_g6ur_rerun.json`.

---

## 5. Gold comparison — all 32 select the same sub-agent

`classify_to_subagent()` with each gold question's committed route dict
(`module145_live/module145_route_dict_after.json`), three ways:

1. **Structural.** 28 of 32 are decided before the semantic clause: 17 by
   the XAGG one-call skip (A1, A7, CP6, CR4, CP1 and — outranking a trigger
   hit they also carry, exactly as before — CR6, CR7, CR8, CS4, M1, M2, M4,
   M5, M7, G2, G3, G5), **3 by the trigger list (CR3, G1, G6)**, and the 8
   within-case KB questions that `semantic_selection_applies()` refuses on
   `case_scope`. **CR3, G1 and G6 are decided `"trigger"` by
   `_meta_analysis_decided_by_phrase()` and never consult the layer**
   (`test_cr3_g1_g6_still_select_meta_analysis_through_the_trigger_list`).
2. **Force-armed.** The 4 that reach the layer — D1, S2, S3, CR2 — with
   their measured decisions planted: D1 → `xagg:total_count` 0.973, S2 →
   `xagg:station_or_category_counts` 0.999, S3 → `xagg:graph_recurrence_person`
   0.931, CR2 → `xagg:graph_recurrence_person` 0.999 — all absorbing.
   Planting every gold question's measured decision: **moved 0 of 32**
   (`module158_selection_probe.py`, and the test of the same name).
3. **Live.** §7's 32-question run: `sub_agent` per question against Module
   150's run on `main`.

The exact expected sub-agent for every gold question is pinned in
`_EXPECTED_GOLD` (Meta-Analysis ×3, Large-Scale Aggregate ×21, Semantic
Search ×8).

---

## 6. Non-gold paraphrases — written before running

Two rewordings of Q1, written into `module158_targets.json` with their
predicted path before any score or run:

| id | text | predicted | measured (offline) | live |
|---|---|---|---|---|
| Q1-R1 · en | *"Take whichever offence shows up in the largest number of FIRs — how do the cases charged under it spread out from one district to the next?"* | misses the plan regex; reaches Meta-Analysis through the semantic layer | scores **0.996** on `most_cited_section_by_district` — but its phrase aggregate kind is `top_districts_by` (row 157's unfiltered district table), so on the XAGG route **the one-call skip claims it above this layer**; `semantic_selection_applies()` is False | XAGG → Large-Scale Aggregate, 15.3 s, local: *"The document does not specify which offence corresponds to the largest number of FIRs…"* — **not reached** |
| Q1-R2 · roman_ur | *"Jo dafa sab se zyada FIRs mein lagti hai, us ke muqadmat alag alag zilon mein kis tarah phaile hue hain?"* | not caught: the cross-encoder is blind to Roman-Urdu (row 160) | 0.002 on every plan; `SEMANTIC-SELECT no-fire: best=most_cited_section_by_district(0.002)` | XAGG → Large-Scale Aggregate, 46.8 s, local: a statute-combination table for "PPC, 61 cases" — **not reached**, as predicted |

**0 of 2**, and the prediction for Q1-R1 was wrong in an instructive way:
the layer read it correctly and never got to say so, because a phrase kind
that is *not* one of `_GENERIC_AGGREGATE_KINDS` counts as "XAGG answers this
in one call" (`_xagg_answers_in_one_call()`), and `top_districts_by` is
purpose-built. Module 145 met the same wall one tier over (row 165, the
refusal tier). Filed as **180**; not special-cased.

Module 79's four pre-written paraphrases (`module79_paraphrases.json`) all
select Meta-Analysis through the plan clause (unit-pinned, 4/4); they were
not run live again — Module 79 §6 ran their plans in-process and nothing
between the plan match and the plan has changed.

### 6.1 The corpus, and what "reaches the layer" means

`evaluation/module158_selection_probe.py`. **275 items**, every one scored
once against every description (`module158_rerank_cache.json`, keyed by the
table's hash so an edited table cannot reuse a score):

| set | n | role |
|---|---|---|
| gold | 32 | CR3/G1/G6 targets; the other 29 hazards |
| Module 92 paraphrases | 96 | 9 targets (CR3/G1/G6 ×3 languages); 87 hazards |
| Module 116 paraphrases | 6 | targets |
| `module158_targets.json` | 22 (7 new after de-dup) | targets: Q1, Module 79's four, Q1-R1/R2, Module 29's G1 paraphrase |
| Module 144 count paraphrases | 13 | hazards |
| `prompts/router.txt` exemplars | 73 | hazards |
| `module145_hazards.json` + `module145_targets.json` | 30 | hazards (a one-call aggregate must not be decomposed) |
| `module158_hazards.json` | 20 | hazards, pre-written for this layer (§6.2) |

An item **reaches the layer** under the shipped condition
(`semantic_selection_applies()`): cross-case, not DIRECT/file, and
`_meta_analysis_decided_by_phrase()` is None — using its deterministic
router override where one exists, else its recorded/LLM route. **137 of 275
reach.** A **true positive** is a target whose best description is its own
plan; a **hazard** is a non-target whose best description is any plan.

### 6.2 The 20 pre-written hazards

Each chosen to sit near one plan on topic while being a one-call or
one-route question: the two *halves* of each chained plan (the section
ranking alone, the district ranking alone, a per-district table, a
per-section table, a filtered district count — row 157's shape, in English,
Roman-Urdu and Urdu), single components of the orientation note asked alone
(statute mix, arrest rate, a plain count), G2's and G5's shapes, a
within-case review, a legal-procedure question, the linkage half of CR3, a
named-entity cross-case question, a PDF request, a timeline, a data-quality
audit, a whole-dataset summary.

### 6.3 The measurement — two rounds, recorded

Cross-encoder scores (the same `/rerank` bge-reranker-v2-m3 as Module 145).

**Round 1** (the table as first written) — over the 137 reaching items:

```
true positives (8):  .999 .991 .985 .976 .836 .835 .648 | .176
hazards (8):         .996 .525 | .162 .058 .030 .014 .004 .001
```

Two hazards fired at 0.40 — both pre-written, both the shape the set was
built to catch:

- *"Which section is cited in the most FIRs?"* → `most_cited_section_by_district`
  **0.996** (runner-up `xagg:fir_section_case_count` 0.798). The plan's
  description led with its first step, and the first step alone is a
  one-call question.
- *"Give me a short summary of what the caseload looks like overall."* →
  `caseload_review` **0.525** (runner-up `xagg:total_count` 0.475). Global
  Search's shape, described too thinly to absorb it.

Also seen in round 1, below threshold or outside reach: *"Review this case
and tell me if anything about the investigation looks unusual"* scored
0.514 on `caseload_review` with nothing closer — and turned out to be
decided by the **trigger list** (`\blooks?\s+unusual\b`), a pre-existing
false positive that only the within-case demotion guard catches (181).

**Round 2 — the shipped table.** Fixed by description, not by threshold,
per Module 145's rule: (a) two new absorbing neighbours,
`section_ranking_only` and `district_ranking_only` — the *halves* of the
chained plans; (b) both chained-plan descriptions now open with *"a
two-step question that needs both answers together: first … and then, for
… that one winning … only, …"*; (c) `global_theme_summary` now says *"a
general overview or short summary of what the caseload … look like
overall … without a structured analytical breakdown"*; (d)
`investigative_analysis` now names *"a review of one particular case:
whether anything about that case's investigation looks unusual"*. Every
item re-scored:

```
true positives (9):  .999 .991 .985 .976 .836 .835 .648 | .176 .002
hazards (6):         .162 .058 .030 .014 .004 .001
```

| threshold | TP fired / 9 | hazards fired / 6 | non-targets (all 250) with a plan as best ≥ thr |
|---|---|---|---|
| 0.15 | 8 | 1 | 1 |
| 0.20 | 7 | 0 | 0 |
| **0.40** | **7** | **0** | **0** |
| 0.60 | 7 | 0 | 0 |
| 0.65 | 6 | 0 | 0 |
| 0.85 | 4 | 0 | 0 |

**Lowest fired true positive 0.648, highest hazard 0.162: margin +0.485.**
0.40 is kept as the threshold — the same number as Module 145's, chosen
from the same scorer's bimodal shape, and this layer's gap would accept
anything in 0.20–0.60. The strict margin (lowest true positive of all,
0.002, versus 0.162) is negative as it was for Module 145: two wanted items
fall through as today — G6's English Module 92 paraphrase (*"Draft a short
welcome brief for someone just posted in"*, 0.176) and every Roman-Urdu
item (≤ 0.002; row 160).

**Where the absorbing design is thinnest**, since the sweep above hides it:
the half-questions now lose to their absorbing class rather than fall below
threshold — *"Which section is cited in the most FIRs?"* `section_ranking_only`
0.965 vs the plan 0.887 (gap **0.078**); *"Which district has the most
registered cases?"* `xagg:top_districts_by` 0.9995 vs 0.737; its Urdu
form 0.990 vs 0.831. And the round-2 "two-step question" wording pulled
**M4's Urdu gold text** (*"on one side … on the other side"*) to 0.951 on
`most_cited_section_by_district`, absorbed by its own aggregate at 0.998
(gap **0.047**). M4 never reaches the layer — the XAGG one-call skip
claims it — and its English and Urdu paraphrases are absorbed at 1.000 vs
0.280 and 0.991 vs 0.698; the number is pinned
(`test_m4_is_the_thinnest_absorbing_margin_and_is_pinned`) so a future
description edit that flips it fails a test, not a live run.

**Round 3 — the merged table.** Module 161 (PR #99) merged while the live
arm ran and added `applicant_accused_overlap` to Module 145's table, which
this layer absorbs; every item was re-scored against the 55-description
table before the PR. Every number above is unchanged — the same 9 true
positives at the same scores, the same 6 hazards, margin +0.485, gold moved
0/32 — and the one absorbing entry Module 161 added wins nothing a plan
was winning. The committed `module158_probe.json` and the table hash the
test pins are round 3's.

### 6.4 Reach over the targets

| set | before (trigger only) | after (trigger, plan, semantic) |
|---|---|---|
| gold CR3/G1/G6 | 3/3 | 3/3 — all `trigger` |
| Module 116's six | 3/6 | **6/6** offline (5/6 live, §4.2) |
| Module 92's nine CR3/G1/G6 paraphrases | 3/9 | **7/9** (+CR3-en 0.991, CR3-ur 0.976, G1-en 0.836, G6-ur 0.648; G6-en 0.176 and G6-roman_ur 0.000 stay) |
| Module 79's Q1 + four paraphrases | 0/5 | **5/5** — all `plan:` |
| Q1-R1 / Q1-R2 | 0/2 | 0/2 (§6) |
| **total** | **10/25** | **21/25** |

Non-targets pulled to Meta-Analysis: **0 of 250.** Targets sent to a wrong
plan: **0.**

### 6.5 Row 162 — measured, not assumed

The question was whether sending Meta-Analysis-shaped questions to
Meta-Analysis first removes the G1-en hazard from the *aggregate* layer's
candidate set. It does not, and the reason is order: `Supervisor.handle()`
calls `route_query()` — which runs Module 145's `_semantic_xagg_override()`
and scores the question against the aggregate table — *before*
`classify_to_subagent()`. G1-en still reaches the router's layer and still
scores 0.351 on `statute_mix_by_year` there (visible live: `SEMANTIC-DISPATCH
no-fire: best=statute_mix_by_year(0.351)` precedes `SEMANTIC-SELECT
caseload_review(0.835)` in the same request).

What *would* remove it: adding the five plan descriptions to Module 145's
table as absorbing classes. Computed offline from the two caches (each score
is a pair-independent judgement): G1-en's aggregate-layer argmax moves to
`plan:caseload_review` 0.835; **37 corpus items' argmax flips**, all to a
plan, none of them a true positive Module 145 fires on (they are the
Meta-Analysis targets, low-scoring exemplars, and two hazards). The
aggregate layer's next-highest hazard is then 0.104, which puts L3's 0.218
inside a catchable window at ~0.15–0.20. That is a second threshold change
and a second table edit at Module 145's layer; noted on row 162 as the
follow-up measurement, not made here.

---

## 7. Regression — all 32 gold questions, live

`evaluation/module158_live_run.py --gold all` against this branch's backend
on `:8158` (shipped table), platform-admin, All Cases, one pass, 2026-09-15
18:03–18:38 — `module158_live/module158_gold32_after.json`,
`backend_8158.log`, and the comparison in
`module158_live/module158_regression_compare.txt`
(`evaluation/module158_regression_compare.py`). Baseline: **Module 150's
32-question run on `main`** (`module150_live/module150_gold32_outputs.json`,
the last all-32 pass on the code this branch was cut from — Modules 79, 145
and 150 all in) and the Module 116 route baseline.

**Routes: 32 of 32 equal to main and to the baseline. Sub-agent: 32 of 32
equal.** XAGG × 21 → Large-Scale Aggregate, XNETWORK × 3 → Meta-Analysis,
RAG × 8 → Semantic Search. CR3/G1/G6's `subquery_routes` are the same plan
lists as main's (XAGG × 3 / × 5 / × 8).

**Which clause selected, live, per gold question** (from `backend_8158.log`):
CR3, G1, G6 — `SUBAGENT-SELECT Meta-Analysis via trigger`, as required; the
semantic layer was consulted for exactly the four questions §5 predicts —
D1, S2, S3, CR2 — and did not fire on any: `SEMANTIC-SELECT no-fire:
best=xagg:total_count(0.973)`, `xagg:station_or_category_counts(0.999)`,
`xagg:graph_recurrence_person(0.931)`, `xagg:graph_recurrence_person(0.999)`
(1.5–1.7 s each, the only cost this layer adds to a gold question). The
other 25 emitted no selection line at all: decided before the layer.

**Answers: byte-identical to main on 23 of 32.** The nine that differ are
every question whose answer is written fresh by the model each call — G1
and G6 (Meta-Analysis synthesis, 2,232 vs 2,220 and 1,948 vs 2,111 chars,
same findings) and the seven RAG-answered KB questions (KB1, KB2, KB4, KB5,
KB6: longer prose today, same sources; **KB3 and KB9 abstain in both
runs**, 102 vs 220 chars — Module 152's caveat wording differs, the
outcome does not). Every deterministic answer — all 21 aggregates, CR3's
plan — is byte-equal.

**Groq fallbacks: 2, both inside G6** (`Local LLM failed: . Falling back to
groq` ×2 at 18:23; the empty message is row 169's), against 3 on Module
150's main run. ≤ 2 on G6 is the number Module 150 said to expect; this
run sits on it, not over it. No other question fell back. **Total wall
34.5 min (main 33.6).**

---

## 8. New defects, filed not fixed

Highest number in use on `origin/main` at filing: **177** (Module 161's
174–177 merged as PR #99 while this module ran; it was 173 when these were
numbered, and 178 was chosen to stay clear of 161's open PR). These start at
**178**. All four are rows on the tracker as well as here.

**178 — Semantic selection selects the sub-agent, not its plan.**
`meta_analysis._match_decomposition_plan()` is still pattern-only, so a
question the semantic clause sends to Meta-Analysis with
`record_consistency` as its best description (CR3-ur, 0.985) arrives with
no plan match and goes to the LLM decomposer: two RAG sub-queries, an
answer about registration dates and sections instead of the linkage check,
223 s. G1-en and G6-en happened to get gold's substance from the decomposer
(4 XAGG sub-queries each), CR3-ur did not. The fix is one lookup —
`meta_analysis.py` can consult `subagent_selection.lookup(query_text)` when
its own patterns miss and take the plan by name — and it is deliberately
not made here: `meta_analysis.py` was kept untouched and the plan-side
hazard (a *wrong* plan's whole sub-query list at 5–8 model calls) needs its
own measurement, though the corpus shows 0 targets with a wrong plan as
best.

**179 — Today's router reads G6-ur's *"نوٹ لکھیں"* as a file request.**
`route_query()` returns `output_format="file_docx"` for Module 116's Urdu
G6 paraphrase 3/3 (reason text: *"the user explicitly asked for a 'note'
(document) to be written"*), so `main.py`'s cutover check sends it to the
legacy orchestrator and no sub-agent selection runs at all; the legacy
XNETWORK gate refuses. Module 116 measured `chat` for the same text on
2026-09-14. A within-chat "write a note" is not a file request; the
router's few-shots may need the distinction, or the cutover check could
let a file-format XNETWORK/XAGG question through to Report Drafting
instead of the orchestrator.

**180 — A purpose-built phrase kind is treated as proof of a one-call
answer, above this layer.** Q1-R1 resolves by phrase to `top_districts_by`
(row 157's unfiltered district table), so `_xagg_answers_in_one_call()` is
True and neither the plan clause nor the semantic clause is consulted —
while the cross-encoder scores it 0.996 on the right plan. Row 165 is the
same wall at the refusal tier. Whether a non-generic phrase kind should
still yield to a plan description that outscores its own aggregate's is a
design question with the one-call hazard set (every gold XAGG question)
attached; not a threshold change.

**181 — The trigger list has a false-positive side too.** *"Review this
case and tell me if anything about the investigation looks unusual"* is
decided `trigger` by `\blooks?\s+unusual\b` — a within-case question sent to
Meta-Analysis and saved only by the `case_scope` demotion guard, which
depends on the router saying `within_case`. The semantic layer scores it
0.988 on `investigative_analysis` and would have got it right, but a
trigger hit ends the chain before the layer runs — row 173's mechanism at
this layer.

Not filed as new rows, reaffirmed: **row 160** (Roman-Urdu blindness: Q1-R2
and G6-roman_ur score ≤ 0.002 on everything); **row 157** (Q1-R1's served
answer is the unfiltered district table).
