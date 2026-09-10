# Module 70 — the paraphrase gate had no opposite direction, so a computed finding could vanish silently

Branch `fix/paraphrase-omission-check`, worktree `D:/Rapids AI/muhafiz-m70`,
branched from `origin/main` at **`21d3d0e`** (after Modules 92 and 101 merged;
101 touched `verifier.py`, which is this module's file).

All runs 2026-09-10. Every live run below recorded **which model answered**;
**every one of the 92 generations in this file was the local `qwen3:14b`** —
no silent Groq/Gemini fallback occurred, so no count here is a mixed arm.

> **Headline.** Module 83's diagnosis is confirmed exactly, on today's `main`:
> M2's headline is computed correctly, rendered correctly, and dropped from
> the served answer on **9 of 9**. The fix adds the missing direction to
> `verify_structured_aggregate_paraphrase()` and a single repair regeneration
> above it. M2 now carries the headline on **9 of 9**, and on **6 of 6**
> non-gold Urdu and Roman-Urdu rewordings. Module 87's judge scores M2
> **FactualCorrectness 0.20 → 1.00 on 3 of 3**. An exhaustive 900-cell
> old-vs-new control proves the change cannot move any verdict the gate
> returns today.
>
> **The blast radius is narrower than the row assumed, and that is the most
> useful measurement in this file.** M2 is *not* one of a class. Of the 32
> gold questions, **25 compose this gate**, and **exactly 1 — M2 — drops a
> load-bearing figure**. The naive rule the row implies ("every figure the
> aggregate rendered must survive") fires on **10 of 32** and is **wrong on at
> least 5 of them**, including reading the *1997* of "CNSA 1997" out of a
> caveat line as a computed figure. The measurements that establish that are
> in §1.2 and they are what shaped the rule.

---

## 1. Root cause

### 1.1 Module 83's three-way capture, reproduced on `21d3d0e`

Captured in-process by `scripts/module70_inprocess_runs.py`, which drives the
**real** `large_scale_aggregate()` — real `xagg_tool()`, real Postgres
aggregate, real deterministic rendering, real generation, real
`verify_structured_aggregate_paraphrase()` — and records all three layers plus
the model per run. Nothing in `src/` is modified by it; the wrappers record and
return the real values unaltered. In-process rather than through `/api/chat`
for the reason Module 101 gives: the backend budget is two machine-wide, and
neither the SSE stream nor `backend.log` carries `raw_summary_text` alongside
the *pre-gate* paraphrase, which is the thing Phase 1 asks to see.

| layer | carries gold's *9 of 73 FIRs … 2 of 19 stations* | runs |
|---|---|---|
| 1. aggregate payload (`kind=station_caseload_by_specialisation`) | yes | **9 of 9** |
| 2. rendered `raw_summary_text` headline | yes, byte-identical | **9 of 9** |
| 3. paraphrase, before the gate | **no** | **0 of 9** |
| 4. served answer | **no**, byte-identical to layer 3 | **0 of 9** |

The gate logged `grounded=True`, `unsupported_numbers=[]` on all nine. That
verdict is true and useless: the paraphrase stated a strict subset.

The served text was **five distinct strings across nine runs** — the drop is
not one memorised completion, it is what this model does with this rendering.

Root cause, unchanged from Module 83 and confirmed by reading the code:

```python
unsupported_numbers = sorted(n for n in (ans_nums - src_nums) ...)
```

One-directional by construction. It catches every number the paraphrase
*invented*; it is structurally incapable of seeing one the paraphrase
*omitted*.

Evidence: `scratchpad/m70_before_M2.json`, `scratchpad/m70_before_M2b.json`
(9 rows, arm `before`/`before2`).

### 1.2 Blast radius — which questions compose this gate, and what they drop

All 32 gold questions, one run each, pre-change (`scratchpad/m70_blast.json`).
**25 of 32 produce a real XAGG aggregate and pass through this gate**, across
19 distinct `aggregate_kind`s. For each, every figure in the rendered headline
line was checked against the served answer:

| what the headline is | questions | headline figures missing from the answer | is that a defect? |
|---|---|---|---|
| **a proportion** — "9 of 73 FIRs … 2 of 19 stations" | **M2** | `9, 73, 19, 12.3` | **yes — this module** |
| a bare total — "**4 matching Person(s) found.**" | S3, CR2, KB2, KB9 | `4` | **no** (see below) |
| a count + lead-in — "32 case(s) recorded a recovered weapon." | M5 | `32` | **no** — the answer states 13 (2024) + 19 (2026) |
| a **caveat line** — "NOTE: … (e.g. PPC, CNSA 1997, Arms Ordinance 1965)" | CR3, KB1, KB8 | `1997`, `1965` | **no — false positive** |
| a linkage sentence — "all 4 current CMS complaint(s) …" | CR6 | `4` | **no — false positive**, the Urdu answer says **چار** |
| headline fully carried | 15 others | — | — |

Three findings follow, and each one shaped the rule.

**(a) M2 is alone.** Module 83 only looked at M2 and left the class question
open. It is answered: **1 of 32**. This is an M2 fix that generalises, not a
class fix. Reporting it the other way would have overstated the change.

**(b) A naive omission rule is wrong on at least 5 of the 32.** CR3, KB1 and
KB8 lead with a `NOTE:` caveat whose only digits are statute years inside
"CNSA 1997" and "Arms Ordinance 1965". Those are not computed figures at all,
and an omission check that demanded them would push statute years into
answers as if they were findings. CR6 is worse: its Urdu answer **does** state
the figure — as the word **چار** — and no digit-based check in `verifier.py`
can see that. (Filed as **Defect 133**.)

**(c) The distinguishing property is that a proportion is destroyed by
dropping either half.** S3/CR2/KB2/KB9 omit a headline **total** (`4`) and M5
omits a headline **count** (`32`), and in every one of those five the answer is
still a good answer: a count survives being summarised away, and M5's answer
states the two year-buckets that sum to it. "2 stations" without "of 19" is
not a weaker version of M2's concentration finding — it is a **different and
much duller claim**. That is the line the rule draws.

### 1.3 Why the rendering side could not fix this

Module 44 already fought it twice from below.
`render_station_caseload_by_specialisation()` carries two comments recording
that a paraphrase kept the growth clause and dropped the share clause on 3 of
3 runs, and that the lead was made **one** sentence precisely so "a paraphrase
[has] no seam to drop". It drops it anyway, 9 times out of 9. The rendering
side is exhausted; the gate is where the rule belongs.

---

## 2. The change

Two files in `src/`, plus two scripts and two test files.

| file | change |
|---|---|
| `src/pipeline/verifier.py` | `_PROPORTION_PAIR_RE`, `_headline_line()`, `_omitted_headline_proportions()`, and two **additive** keys on the return dict: `omitted_source_figures`, `omitted_source_headline`. |
| `src/pipeline/harness/agents/large_scale_aggregate.py` | `_OMISSION_REPAIR_RULE` and one repair branch: on a reported omission, **one** regeneration, adopted only if it passes the full gate **and** omits strictly fewer figures. |
| `tests/test_verifier.py` | 10 tests (§3). |
| `tests/test_harness_agent_large_scale_aggregate.py` | 5 tests (§3). |
| `scripts/module70_inprocess_runs.py` | the three-way capture runner (new). |
| `scripts/module70_equality_control.py` | the 900-cell old-vs-new control (new). |
| `docs/gold-qa-wave2-results/module70_paraphrases.json` | the non-gold rewordings, written before running them. |

### 2.1 The detection — `_omitted_headline_proportions()`

Figures from an `X of Y` construction in the **first non-empty rendered line**
that the paraphrase does not state. Both properties it rests on are already
load-bearing and already documented at their own sites:

1. **Every renderer leads with its finding.** `_render_aggregate_text()`'s own
   comment: *"Every enumerating branch leads with its own total"*.
2. **A renderer writes "X of Y" only for a computed proportion** — the one
   numeric construction destroyed by dropping either half (§1.2c).

Two deliberate narrowings, each earned by a measured false positive:

* **Headline line only, never the rendered rows.** A `rate_breakdown` states a
  proportion on every district row; an answer is prose, not a table, and
  Module 104 records that a Markdown list ordinal is itself read as a claimed
  figure by both verifiers — so a rule that pushes answers into list shape
  trips a different gate. (That is not hypothetical: §8's Defect 122 is G5
  being rejected live for exactly the ordinal `4.`)
* **The check does not apply when the answer states *none* of the headline's
  figures.** KB2 and KB9 both produce the shape "the provided document does
  not address this" against a `graph_recurrence` payload. That is the
  sub-agent being correct, not a paraphrase that dropped a finding, and
  pushing figures into it would manufacture a claim — the exact failure the
  *other* direction of this function exists to stop.

When it fires it reports **both halves of the pair**, not just the absent
half: the repair pass has to restate the proportion, and "19" alone is not a
restatable fact.

### 2.2 The response — one repair pass, never a refusal

Module 83 proposed serving `raw_summary_text` on omission. **That was not
built, and the measurement in §1.2 is why.** A rule that cannot be perfect —
and this one cannot; §8's Defect 133 shows a figure stated in Urdu words is
invisible to it — must not be able to turn a good prose answer into a computed
dump. Rejecting is also what `[PRESERVE]` did to KB9 and what Module 101 had
to undo.

So the branch **repairs and never refuses**:

* one regeneration, with the missing figures and the headline sentence
  **computed at runtime from the rendering** — nothing about M2, its stations
  or its counts is written anywhere in the code;
* the repaired text goes through the **full** gate again, invented-number
  direction included, so an omission can never be traded for a hallucination;
* it is adopted only if it is grounded **and** omits strictly fewer figures;
* otherwise the original paraphrase is served **exactly as today**.

The branch is therefore structurally incapable of making any currently-served
answer worse. Its only cost is one extra generation on a paraphrase that
fired — **1 of 32** questions.

### 2.3 What was deliberately NOT changed

* **The existing `ans_nums - src_nums` check — untouched, byte for byte.** Not
  relaxed, not narrowed, not reordered. §3 pins it with its own test and §7's
  equality control proves it over 900 cells. Module 101 has just demonstrated
  how load-bearing this family is.
* **`grounded`, `unsupported_claims`, `reason`, `leaked_case_id`,
  `refusal_detected` — none of them can move.** The omission signal does not
  feed any of them. An omission is a completeness shortfall, not a grounding
  failure.
* **`_SYSTEM_PROMPT_TEMPLATE` — untouched.** No figure list, no "remember to
  state the headline", no M2 vocabulary. The repair rule is a *separate*
  string appended only on the repair call, and it says "state the figure(s)
  {figures} in that sentence's own terms" with both slots interpolated at
  runtime.
* **`render_station_caseload_by_specialisation()` and every other renderer —
  untouched.** Module 44 already pushed that side as far as it goes.
* **`meta_analysis.py` — untouched.** It references this function in a
  docstring but does not call it; `large_scale_aggregate.py` is the only call
  site, which is what bounds this change.
* **No retry on verifier *rejection*.** Module 71 pinned that closed and it
  stays closed. The regeneration here fires only on a signal that leaves
  `grounded=True`.

---

## 3. Unit tests — both directions, checked both ways

**Checked in both directions.** Every test below was run against
`origin/main`'s `verifier.py` and `large_scale_aggregate.py` (checked out over
the branch's, then restored) as well as against the branch.

`tests/test_verifier.py` — **fails before with a collection error**
(`ImportError: cannot import name '_omitted_headline_proportions'`), all 10
pass after:

| test | pins |
|---|---|
| `…_the_live_m2_paraphrase_is_reported_as_omitting_its_headline` | the exact layer-3 text captured on 9 of 9 runs reports `['19','2','73','9']` |
| `…_omission_does_not_flip_grounded_or_add_unsupported_claims` | **additive only** — `grounded` stays `True`, `unsupported_claims` stays `[]`, `reason` unchanged |
| `…_a_paraphrase_that_keeps_the_proportion_is_not_flagged` | no false positive on a complete answer |
| **`…_the_invented_number_direction_still_fires`** | **the existing hallucination guard.** An answer carrying the whole proportion *and* an invented `61` is still rejected |
| `…_a_statute_year_in_a_note_line_is_not_a_proportion` | CR3/KB1/KB8's live false positive |
| `…_a_bare_headline_total_is_not_a_proportion` | S3/CR2/KB2/KB9's `4` and M5's `32` |
| `…_an_answer_that_states_no_headline_figure_is_left_alone` | KB2/KB9's abstention shape |
| `…_only_the_headline_line_is_covered_not_every_rendered_row` | the list-bloat failure mode |
| `…_headline_line_skips_leading_blank_lines` | `_headline_line()` |

`tests/test_harness_agent_large_scale_aggregate.py` — **3 of 5 fail before**
(`assert 1 == 2`: there was no second generation), all 5 pass after. The other
two (`…_a_complete_paraphrase_makes_no_second_call`,
`…_a_repair_generation_that_raises_serves_the_original`) pass trivially before
because before there was never a second call at all; they are there to pin the
cost and the failure path, and that is stated rather than counted as evidence.

| test | pins |
|---|---|
| `…_a_dropped_headline_proportion_triggers_one_repair_pass` | exactly **one** regeneration; repaired text served; **no** fallback caveat added; the repair prompt names the computed figures and forbids a list |
| **`…_a_repair_that_invents_a_number_is_discarded`** | an omission is **never** traded for a hallucination — original served |
| `…_a_repair_that_omits_just_as_much_is_discarded` | no improvement, no swap |
| `…_a_complete_paraphrase_makes_no_second_call` | the 24 of 25 that carry their headline pay nothing |
| `…_a_repair_generation_that_raises_serves_the_original` | LLM unreachable mid-repair ⇒ original, not an abstention |

Both files in full, after: **149 passed**. (Named files only — never a bare
`pytest tests/`, which empties the `muhafiz_entity_descriptions` collection.)

---

## 4. Live verification — M2, nine runs each arm

`scripts/module70_inprocess_runs.py`, `platform-admin`, All Cases.

| arm | runs | model | **carries `9 of 73` and `2 of 19`** | served as raw aggregate | Markdown list lines | mean chars |
|---|---|---|---|---|---|---|
| **before** (`21d3d0e`) | **9** | local `qwen3:14b` 9/9 | **0 of 9** | 0 of 9 | 0 | 301 |
| **after** | **9** | local `qwen3:14b` 18/18 | **9 of 9** | 0 of 9 | **0** | 399 |

The repair fired on 9 of 9 and was **adopted** on 9 of 9 (two generations per
run, both local). Representative after-answer, run 4:

> "The caseload is growing fastest at the 15 general-purpose stations, which
> saw the number of FIRs increase from 7 in 2024 to 39 in 2026. In contrast,
> the 2 stations set up for one specific type of crime saw a much slower
> increase, from 3 FIRs in 2024 to 5 in 2026. Despite this slower growth, **9
> of the 73 FIRs (~12.3%) are handled by just 2 of the 19 stations**, which are
> set up for a single type of crime."

Answers grew **+33 % in length** (301 → 399 chars) and gained **zero** list
lines: one clause, still prose. That is the side effect the brief asks to be
watched for, measured and reported rather than assumed away.

Evidence: `scratchpad/m70_after_M2.json`.

---

## 5. Gold comparison — Module 87's judge, substance not wording

M2's gold: *"Specialized cybercrime units already carry real load: 9 of 73 FIRs
(~12%) from just 2 of 19 stations — disproportionate given they're
single-purpose."* Gold was independently re-measured earlier in this programme
and found exactly right; it is not re-challenged here.

Scored with `evaluation/gold32_score.py` as it stands after Module 87
(`gemini-2.5-flash` at `temperature=0`, the polarity rule in place), three
before-answers and three after-answers, gold as `expected_output`:

| arm | run | FactualCorrectness | AnswerRelevancy |
|---|---|---|---|
| before | 1 / 2 / 3 | **0.2 / 0.2 / 0.2** | 1.0 / 1.0 / 1.0 |
| after | 1 / 2 / 3 | **1.0 / 1.0 / 1.0** | 1.0 / 1.0 / **0.857** |

0.20 on 3 of 3 before is exactly the figure Module 87 recorded on all three of
its passes, so the before arm is calibrated against the published number
rather than against a fresh baseline of its own.

The judge's own reason on after-run 1: *"correctly identifies the key facts
regarding the caseload distribution (9 of 73 FIRs handled by 2 of 19
stations)"*.

**The one AnswerRelevancy dip is reported, not smoothed.** On after-run 3 the
judge said the answer *"includes unnecessary contextual information regarding
the total number of stations"* — 1.0 → 0.857. That is the predicted cost of an
omission check made visible: gold's own figure is, to a relevancy metric
scoring the growth question, extra context. FactualCorrectness gains 0.8 and
AnswerRelevancy loses 0.048 on 1 run of 3. Evidence: `scratchpad/m70_judge.json`.

---

## 6. Non-gold paraphrases — three rewordings, two non-English

Written **before** any of them was run, committed as
`docs/gold-qa-wave2-results/module70_paraphrases.json`, and **not adjusted
afterwards**. None names a figure, a station, or the word "concentration".

| id | language | runs | repair fired | **carries the full proportion** |
|---|---|---|---|---|
| `M2_ur` | Urdu (script) | 3 | 3 of 3 | **3 of 3** |
| `M2_roman_ur` | Roman Urdu | 3 | 3 of 3 | **3 of 3** |
| `M2_en_reworded` | English | 3 | **0 of 3 — never reached M2's aggregate** | n/a |

Urdu, run 1 (`PYTHONIOENCODING=utf-8` throughout):

> "…تاہم، **19 تھانوں میں سے 2 تھانوں** جو ایک خاص قسم کے جرم کے لیے قائم کیے
> گئے ہیں، ان میں **73 FIRs میں سے 9 FIRs (~12.3%)** درج کی گئی ہیں۔"

Roman Urdu, run 1:

> "…Halaakih, **73 FIRs mein se 9 (~12.3%)** sirf **19 thanon mein se 2
> thanon**, jo ek khaas qism ke jurm ke liye banaye gaye hain, par le kar gaye
> hain."

So the fix is not tied to gold's English wording: it works through the
computed rendering, in three languages.

**The English rewording is a negative result and is reported as one.** All
three of its runs resolved to `aggregate_kind=relational_aggregate` — a flat
per-station case listing — not to `station_caseload_by_specialisation` at all,
so M2's headline was never computed and there was nothing to omit or repair.
Its answer is a 10-row bullet list of station names. That is a **routing**
defect that predates this module and is filed as **Defect 122**; it means M2's
finding is only reachable under wordings close to gold's, which is a real limit
on what this fix buys.

Also honest: two of the three Urdu repairs appended the renderer's own
two-station bullet list under the prose. Grounded, correct, and slightly more
list-shaped than the pre-repair answer — the predicted failure mode, visible in
Urdu and not in English or Roman Urdu. Evidence:
`scratchpad/m70_after_paraphrase.json`.

---

## 7. Regression guard — all 32, plus a 900-cell exhaustive control

### 7.1 The exhaustive control (the stronger of the two)

`scripts/module70_equality_control.py` loads `origin/main`'s `verifier.py`
alongside this branch's and drives **both** over 10 real rendered headlines ×
15 answers × 3 `case_id`s × 2 `cross_case_ids` = **900 cells**, then compares
every key `origin/main` returns.

```
cells compared: 900
cells where the new omission check fired: 24
PASS — every pre-existing key is byte-identical between origin/main and this
branch on all 900 cells; the only difference is the two additive keys.
```

`grounded`, `unsupported_claims`, `reason`, `leaked_case_id`, `off_topic` and
`refusal_detected` are **provably** unmoved, including on invented numbers, a
fabricated percentage, a fabricated case id, a grand total, a derived
percentage, list-shaped text, Urdu and Roman-Urdu prose, and empty/whitespace
inputs. This is the answer to the specific worry that made Module 83 decline
this: a regression on this gate would be broad and quiet, and there is now no
room for one inside the verifier.

### 7.2 All 32 gold questions, live, before and after

One run each arm (`scratchpad/m70_blast.json` → `scratchpad/m70_blast_after.json`),
local model on 64 of 64 generations.

* **Route/`aggregate_kind` identical on 32 of 32.** Nothing re-routed.
* **The repair fired on exactly 1 of 32 — M2.** Every other question made one
  generation, as before. The cost claim in §2.2 is measured, not asserted.
* **Answers did not become longer or more list-like** anywhere except M2's one
  added clause. No question gained a Markdown list it did not already have.
* **Two questions moved, G5 and KB8** — both `grounded=True` → `grounded=False`
  on the **invented-number** direction, both then serving the raw aggregate.
  Neither can be caused by this change, and both were resampled to prove it:

| question | rejected, **pre-change** code | rejected, **post-change** code | reason |
|---|---|---|---|
| G5 | 1 of 3 | 2 of 3 | *"number(s) not present …: 4"* — the Markdown ordinal `4.` of its fourth list item |
| KB1 | **3 of 3** | 1 of 3 | *"…: 158"*, *"158, 159, 1898"* — CrPC section numbers |
| KB8 | 0 of 3 | 0 of 3 | the single blast rejection did not reproduce |
| KB4 | 0 of 3 | 0 of 3 | — |

Both populations straddle both arms — KB1 is *worse* on the pre-change code —
so this is the gate's known non-determinism at temperature, not a Module 70
effect. And mechanically it cannot be one: the repair branch is unreachable
unless `omitted_source_figures` is non-empty, and it was empty on every one of
these runs (one generation each, recorded). Evidence:
`scratchpad/m70_flake_before.json`, `scratchpad/m70_flake_after.json`.

**Limit of this arm, stated plainly.** The 32 runs drive
`large_scale_aggregate()` directly, so they exercise the gate on all 25
XAGG-composing questions but do **not** re-derive each question's supervisor
route, and for the Meta-Analysis fan-outs (G-series, CR3) they run the whole
question rather than its decomposed sub-queries. It is a complete control over
**the changed code path** and a partial one over the pipeline above it. n=1 per
question per arm; the four questions that moved or looked unstable were
resampled at n=3 each arm, the rest were not.

---

## 8. New defects — filed, not fixed

Highest number in use in `GOLD_QA_REMAINING_FIXES_PLAN.md` before this module:
**121**. These start at **122**.

### Defect 122 — a natural English rewording of M2 never reaches M2's aggregate

`resolve_aggregate_kind()` sends *"Which of our stations are picking up
caseload fastest right now — the ordinary ones that take everything, or the
small number that only handle one kind of offence?"* to
`relational_aggregate` (a flat per-station case listing) on **3 of 3** runs,
not to `station_caseload_by_specialisation`. The served answer is a 10-row
bullet list of station names with no growth comparison and no concentration
finding at all. Gold's own wording, and both Urdu paraphrases, resolve
correctly. This is Module 56's vocabulary finding again, and it bounds what
Module 70 buys: the fix repairs the paraphrase of a finding that was computed,
and under this wording the finding is never computed.
**Evidence:** `scratchpad/m70_after_paraphrase.json`, `M2_en_reworded` runs 1–3.
**Verify:** whether `_is_station_specialisation()`'s tier-2 signals can cover
"the ordinary ones that take everything" / "only handle one kind of offence"
without dragging in the neighbours Module 69's window note already flags.

### Defect 131 — a legal **section number** in an XAGG paraphrase is read as an invented figure

KB1's paraphrase correctly cites *"Section 158"*, *"Section 159"* and *"the
Criminal Procedure Code, 1898"*. `verify_structured_aggregate_paraphrase()`
sees `158`, `159`, `1898` as numbers absent from the computed aggregate and
rejects the whole answer, serving the raw case-count dump instead — **3 of 3
runs on `origin/main`, 1 of 3 after** (the difference is decode variance, not
this module). This is Module 101's family seen from the aggregate side: a
statute citation is provenance, not a claimed figure, exactly as a
`[Document N]` marker is — and `_CITATION_MARKER_RE` already strips the latter.
**Evidence:** `scratchpad/m70_flake_before.json` KB1 runs 1–3;
`scratchpad/m70_flake_after.json` KB1 runs 1, 3.
**Verify:** whether a `§|Section|Article|Art\.|Rule\s+\d+` and `Act/Ordinance/
Code,?\s+(19|20)\d\d` exemption can be added to `_numbers_in()`'s answer side
without letting a fabricated *count* through disguised as a section.

### Defect 132 — Module 104's list-ordinal defect reproduces on the XAGG gate

G5's Roman-Urdu paraphrase is a numbered list; the gate rejects it with
*"number(s) not present in the computed result: **4**"* — the ordinal of the
fourth list item, which is not a claimed figure. Measured **1 of 3 on
`origin/main`** and 2 of 3 after (decode variance; the change cannot reach it).
Module 104 filed this against the other verifier; this records that
`verify_structured_aggregate_paraphrase()` has it too, and that it is live on a
gold question today. It is also the reason §2.1 refuses to let an omission
check push answers into list shape.
**Evidence:** `scratchpad/m70_flake_after.json` G5 runs 2–3;
`scratchpad/m70_flake_before.json` G5 run 3.
**Verify:** strip leading `^\s*\d+[.)]\s` ordinals from the answer before
`_numbers_in()`, in both verifiers, negative-controlled against an answer whose
*first* real figure is line-initial.

### Defect 133 — every numeric check in `verifier.py` is blind to a number written as a word

CR6's Urdu answer states the aggregate's *"all 4 current CMS complaint(s)"* as
**چار**. `_numbers_in()` is a digit-run regex, so **both** directions of the
gate are blind to it: this module's omission check would report a false
omission, and — more seriously — the pre-existing hallucination guard cannot
see an *invented* count written in words either. The rule shipped here dodges
the false-positive half by construction (a renderer never writes a proportion
in words), but the hallucination half is a real hole in a guard the programme
relies on, and neither half is addressed here.
**Evidence:** `scratchpad/m70_blast.json`, CR6 — headline figure `4` reported
missing while the served Urdu answer states چار; same row post-change.
**Verify:** how many of the corpus's Urdu/Roman-Urdu answers state a figure in
words at all — measure before building anything, because a word-numeral parser
for Urdu, Roman Urdu and English is a large surface for a hole that may be rare.

---

## 9. Reproduce

```bash
# three-way capture, before/after
PYTHONPATH=. PYTHONIOENCODING=utf-8 python -X utf8 \
  scripts/module70_inprocess_runs.py --ids M2 --runs 9 --arm after \
  --out scratchpad/m70_after_M2.json

# non-gold paraphrases
PYTHONPATH=. PYTHONIOENCODING=utf-8 python -X utf8 \
  scripts/module70_inprocess_runs.py \
  --ids M2_ur,M2_roman_ur,M2_en_reworded --runs 3 \
  --extra docs/gold-qa-wave2-results/module70_paraphrases.json \
  --out scratchpad/m70_after_paraphrase.json

# all-32 regression sweep
PYTHONPATH=. PYTHONIOENCODING=utf-8 python -X utf8 \
  scripts/module70_inprocess_runs.py \
  --ids D1,S2,S3,A1,A7,CP6,CR2,CR3,CR4,CR6,CR7,CR8,CS4,CP1,M1,M2,M4,M5,M7,G1,G2,G3,G5,G6,KB9,KB1,KB2,KB3,KB4,KB5,KB6,KB8 \
  --runs 1 --arm blast_after --out scratchpad/m70_blast_after.json

# exhaustive old-vs-new equality control (no LLM, no backend)
PYTHONPATH=. python scripts/module70_equality_control.py

# unit tests — NAMED FILES ONLY
PYTHONPATH=. python -X utf8 -m pytest \
  tests/test_verifier.py tests/test_harness_agent_large_scale_aggregate.py -q
```

`CHROMA_PERSIST_DIR` must be an **absolute** path in a worktree's `.env`; the
shipped `./data/chroma_db` resolves to an empty per-worktree directory.
