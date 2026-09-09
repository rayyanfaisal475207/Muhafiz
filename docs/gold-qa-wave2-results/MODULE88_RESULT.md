# Module 88 — CR2: the person-recurrence evidence had no time axis

**Found by:** Module 27 §7 (*"the one clear regression in the set"*) ·
**Branch:** `fix/cr2-recurrence-temporal-context` · **Date:** 2026-09-09
· **Port:** 8032 · **Base:** `main` @ `6cf89fb`

---

## 1. Root cause

CR2 — *"Is there anyone with an earlier case already on record who has since
resurfaced as a suspect in a newer, separate case?"* — scored **0.00 on all
three of Module 27's passes**, with a byte-identical refusal, on
`route='XAGG'`. Module 28 had used CR2 as its control and recorded it
*"unchanged, byte-identical 4-person answer before and after"*, so nothing in
this wave had looked at what that answer actually said.

**The aggregate was never wrong.** `resolve_aggregate_kind(CR2)` returns
`graph_recurrence_person` on every run, and the result already contained
gold's exact pair. What reached the model was:

```
- شہزیب عرف شابی (Person): appears in 2 cases — fir-214-26, fir-891-24
```

and nothing else. No dates, no per-case role, no arrest status, no conviction
flag. **The model could not tell which of the two cases was earlier, or that
one ended in a conviction, so its refusal was correct given the evidence it
was handed.** The pre-fix live answer says so in its own words (§4):

> *"There is no information provided that would indicate the chronological
> order of these cases or whether any individual had a prior case before
> appearing in the listed ones."*

That is an **evidence-rendering gap, not a reasoning failure** — and it is
exactly why the answer was deterministic rather than flaky. The brief's
hypothesis was right in every particular; nothing about the diagnosis
changed.

### The Cypher probe — run before any code, figures derived independently

`scratchpad/probe_cr2.py`, hand-written Cypher against the live AGE graph
(73 cases), not a call into `xagg.py`:

**A. Recurring `Person` nodes** (`Person-[:BELONGS_TO_CASE]->Case`, no
canonical fold) — **4**, every one of them `case_count = 2`:

| entity_id | name | cases |
|---|---|---|
| `PERSON-f2a8ec2561` | فیصل | `fir-202-26`, `fir-401-26` |
| `PERSON-4447594024` | طارق | `fir-202-26`, `fir-401-26` |
| **`PERSON-685fc54914`** | **شہزیب عرف شابی** | **`fir-214-26`, `fir-891-24`** |
| `PERSON-90ce376334` | عاصم رشید | `fir-64-26`, `fir-65-26` |

**B. Incident dates** (`Incident-[:OCCURRED_ON {event_type:'incident'}]->Date`):

| case | incident date |
|---|---|
| `fir-891-24` | **2024-09-14** |
| `fir-214-26` | **2026-03-03** |
| `fir-401-26` | **2024-09-25** |
| `fir-202-26` | 2026-03-06 |
| `fir-64-26` | 2026-03-19 |
| `fir-65-26` | 2026-03-23 |

**This row is why the FIR id must not be parsed for the year.** `fir-401-26`
carries an incident date of **2024**-09-25. The id says 26; the incident is
2024. The brief flagged the shortcut as tempting and the live graph disproves
it outright, so the year is derived from the `OCCURRED_ON` edge exactly as
`_statute_mix_by_year()` does.

**C. `Person-[:INVOLVED_IN {role, arrest_status}]->Incident`** for the four:

| person | case | role | arrest_status |
|---|---|---|---|
| **شہزیب عرف شابی** | **`fir-891-24`** | **accused** | **گرفتار، بعد ازاں سزا یافتہ** |
| **شہزیب عرف شابی** | **`fir-214-26`** | **accused** | **گرفتار** |
| عاصم رشید | `fir-64-26` | accused | گرفتار |
| عاصم رشید | `fir-65-26` | accused | پہلے سے کیس 10 میں گرفتار، اس مقدمے میں بھی نامزد |
| فیصل | `fir-401-26` | accused | زیر تفتیش |
| فیصل | `fir-202-26` | witness | — |
| طارق | `fir-401-26` | complainant | — |
| طارق | `fir-202-26` | witness | — |

**D. `criminal_record` StructuredRecords** — **33 rows**, of which only **4**
carry a FIR reference `_fir_key()` can parse (the other 29 have
`source_case_ref = None`):

| `_fir_key` | subject | conviction_status |
|---|---|---|
| **`891-24`** | **شہزیب عرف شابی** | **Convicted, on bail pending appeal** |
| **`214-26`** | **شہزیب عرف شابی** | **Under trial** |
| `64-26` | عاصم رشید | Under trial |
| `77-26` | زاہد پرویز | Under trial, priority hearing requested |

Status distribution over all 33: `Under trial` 30 · `Convicted, on bail
pending appeal` **1** · `Sub judice, external prior case` 1 · `Under trial,
priority hearing requested` 1.

**Verdict on the figures: gold's CR2 answer is exactly right and fully
supported.** شہزیب عرف شابی is convicted on FIR 891/24 (incident 2024-09-14)
and separately accused, still under trial, on FIR 214/26 (incident
2026-03-03) — 2 calendar years later. Every fact gold asserts is in the
graph; none of it was in the evidence the model got.

**Two things the probe found that the brief did not name.** The FIR-key join
is **sparse by construction** — 4 of 33 records — so a person with no matched
record must be reported without one rather than guessed at by bare name. And
عاصم رشید's two FIRs are **four days apart**, which is a real recurrence but
emphatically not "an earlier case already on record"; that distinction
becomes load-bearing in §2.

---

## 2. Change

Three files, no new aggregate kind.

**`src/pipeline/xagg.py`** — `_top_recurring_nodes()` now attaches a
date-ordered `cases` timeline to every recurring entity. `case_ids` is
untouched (`_case_ids_touched()` and several pinned tests read it); the
timeline is additive. Four new helpers, each reusing a query this module
already runs somewhere else — reuse, not reinvention, was the brief's
instruction and it is what makes the hedges consistent with CR4's and CR7's:

| dimension | read from | already used by |
|---|---|---|
| date / year | `Incident-[:OCCURRED_ON {event_type:'incident'}]->Date` | `_statute_mix_by_year()`, `_extract_year()` |
| role, arrest_status | `Person-[:INVOLVED_IN]->Incident` | `_weapon_evidence_chain()` |
| conviction_status | `criminal_record` StructuredRecord joined on `_fir_key()` | `_weapon_evidence_chain()`, CR7's cross-check |

Ordering is by incident date. **A case with no incident date sorts last and
gets `sequence: None`** — 9 of 73 cases live have none, and placing one in the
order would be inventing the very fact CR2 asks about.

The conviction join is by **`(fir, subject)`**, not by FIR alone.
`_weapon_evidence_chain()`'s own `conviction_by_fir` is last-write-wins,
which is safe there and is not safe here: attributing one person's conviction
to another person named on the same FIR would be the worst possible failure
of this particular question. `_match_criminal_record()` falls back to the
single record when a FIR carries exactly one, and returns **None** when a FIR
carries several and none matches the name.

**`render_graph_recurrence()`** — a shared renderer, the same move Module 28
made for the weapon chain, so the enrichment cannot reach two of the three
rendering sites and silently miss the third. The pre-existing headline line
is emitted **byte-for-byte**; the timeline is appended beneath it and only
for entities that carry one, so Vehicle and Weapon recurrence render exactly
as before (pinned by a test).

The renderer **states the sequence rather than leaving it to be inferred** —
`Sequence: earliest case FIR 891/24 (2024-09-14), then FIR 214/26
(2026-03-03) — the two are 2 calendar year(s) apart` — and leads the whole
result with the family's own temporal summary statistic:

> Of the 4 recurring Person(s) below, 3 appear in cases from more than one
> calendar year, and 1 carries a decided criminal-record outcome on an
> EARLIER case than one they are also named in later.

**That summary line was added because of a measured failure, not a guess.**
Without it (an intermediate arm, 7 live runs, §4) CR2 answered *"Yes"* and
listed three people including the right one — an improvement, but not gold's
answer — and the paraphrase reproduced a 2024 case and a 2026 case for the
same person **in its own body** and still concluded *"none of these are years
apart"*. Stating the cross-year count and the prior-conviction count up front
took CR2 from a three-name list to gold's single identity, deterministically.
`_spans_calendar_years()` is what keeps عاصم رشید's four-days-apart pair out
of that count; `_has_prior_settled_conviction()` reuses CR7's published
`_conviction_is_settled()` rule and requires a **later** appearance, so a
settled outcome on someone's most recent case can never satisfy it.

**Nothing is narrowed toward CR2.** No question-shaped branch, no filter, no
new dispatch vocabulary. The aggregate returns the same entities in the same
order for every question that reaches it; only the evidence under each entity
is richer. S3's improvement in §7 is the proof — it got better from the same
generic change, without S3 being mentioned anywhere in the code.

**`src/pipeline/orchestrator.py`** (2 sites) and
**`src/pipeline/harness/tools/xagg.py`** (1 site) — the three
`graph_recurrence` render branches now call the shared renderer. The harness
site keeps its own leading `**N matching Person(s) found.**` total.

**`_log_graph_recurrence()`** carries the new figures. Module 55's rule is
that a kind alone cannot separate a correct run from an under-evidenced one
— and this defect is the strongest possible illustration: the line said
`XAGG graph_recurrence: entity_type=Person, 4 recurring node(s)` on all three
of Module 27's failing passes, and it was true every time.

**Not touched:** `router.py`, `supervisor.py`, `rag.py`, `verifier.py`,
`validation.py`, `prompts/`, `evaluation/`.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_xagg.py -q
→ 430 passed
```

Adjacent suites that import the touched modules — `test_harness_tool_xagg`,
`test_orchestrator`, `test_harness_supervisor`, `test_router`,
`test_harness_agent_large_scale_aggregate`, `test_harness_agent_meta_analysis`,
`test_harness_agent_cross_case_linkage`, `test_xnetwork`, `test_verifier`:
**645 passed, 1 xpassed** (the xpass is pre-existing).

The full `pytest -q` suite was **not** run — it empties the real
`muhafiz_entity_descriptions` Chroma collection.

Module 55's AST test (`test_module55_every_aggregate_kind_emits_an_xagg_log_line`)
passes: no new kind was introduced, so `XAggToolResult.aggregate_kind`'s
`Literal` needed no entry and the four-times-repeated empty-answer trap is
not in play here. The three render sites were still updated together, because
one shared renderer is the version of that discipline that cannot be
half-done.

**Eleven new tests.** The ones that matter:

- `test_module88_gold_fixture_texts_are_the_real_dataset_texts` — CR2's
  question **and answer** and S3's question are asserted byte-identical to
  `Gold_QA_Dataset_Final32_With_Answers.json`, so a gold edit upstream breaks
  here instead of turning the regression into a tautology.
- `test_module88_cr2_gold_facts_are_present_in_the_rendered_evidence` — the
  regression pinned to CR2's literal gold text. Gold asserts three things
  (the ordering, the prior conviction from FIR 891/24, the current accusation
  in FIR 214/26); all three must be **stated** in the rendered evidence. It
  also asserts the pre-fix headline line is still present verbatim.
- `test_module88_person_recurrence_carries_per_case_ordering_and_status` —
  asserts the timeline is ordered by **incident date**, explicitly noting
  that sorting by case id would put `fir-214-26` first and invert gold's
  sequence.
- `test_module88_undated_case_is_not_guessed_into_the_sequence`.
- `test_module88_conviction_is_never_attributed_across_subjects_on_one_fir`.
- `test_module88_two_firs_days_apart_are_not_counted_as_cross_year` —
  عاصم رشید's shape.
- `test_module88_prior_conviction_requires_a_LATER_appearance`.
- `test_module88_vehicle_recurrence_headline_is_byte_identical`.
- `test_module88_log_line_carries_the_new_figures`.

A new `FakeAgeClientByQuery` was needed: the existing `FakeAgeClient` returns
one row set for **every** query, which is fine for a single-statement
aggregate and not for one that now runs four.

**All-32 dispatch equality control.** Dispatch was not changed, and that is
measured rather than asserted: `resolve_aggregate_kind()` was evaluated for
all 32 gold questions on this branch and on `6cf89fb`, and the two JSON maps
are **byte-identical — 0 questions move**. The five Urdu substring collisions
this file has caused before cannot have been reintroduced, because no
vocabulary was touched.

---

## 4. Live verification

Backend on **:8032**, real Postgres (73 cases), real AGE graph, real model
server, platform-admin, All Cases. Both arms on the **same backend, same
data, same session** — the before arm is `6cf89fb`'s `src/pipeline/` checked
out under the same process, not an inherited claim.

**Quota grep over the whole session's `backend.log`: 0 hits.** No
`--- Logging error ---`; Urdu values render intact throughout.

### The `XAGG` log line, before and after

```
BEFORE  XAGG graph_recurrence: entity_type=Person, 4 recurring node(s);
        فیصل=2, طارق=2, شہزیب عرف شابی=2, عاصم رشید=2

AFTER   XAGG graph_recurrence: entity_type=Person, 4 recurring node(s);
        4 with a date-ordered timeline, 2 with a criminal-record outcome;
        فیصل=2[2024-09-25..2026-03-06], طارق=2[2024-09-25..2026-03-06],
        شہزیب عرف شابی=2[2024-09-14..2026-03-03], عاصم رشید=2[2026-03-19..2026-03-23]
```

### CR2 — before, 3 runs

`route='XAGG'` 3/3, `status=done` 3/3, 7.0–8.3 s. Two textual variants, both
the same refusal. Verbatim (run 1):

> Based on [Document 1], there is no indication that any person has appeared
> in an earlier case and then resurfaced as a suspect in a newer, separate
> case. All individuals listed have appeared in two cases each, but the cases
> are not explicitly described as being sequential or involving a resurfacing
> of suspects. For example:
>
> - **فیصل** appears in **fir-202-26** and **fir-401-26**
> - **طارق** appears in **fir-202-26** and **fir-401-26**
> - **شہزیب عرف شابی** appears in **fir-214-26** and **fir-891-24**
> - **عاصم رشید** appears in **fir-64-26** and **fir-65-26**
>
> There is no information provided that would indicate the chronological
> order of these cases or whether any individual had a prior case before
> appearing in the listed ones.

This reproduces Module 27's recorded answer, and its last sentence names the
missing evidence precisely.

### CR2 — after, 7 runs on the shipped code

`route='XAGG'` 7/7, `status=done` 7/7, 4.8–7.0 s. Two variants; both name
gold's identity, gold's ordering, and gold's two FIRs. Verbatim (the
5-of-7 variant):

> Yes, there is one individual who appears in an earlier case and has since
> resurfaced in a newer, separate case: **شہزیب عرف شابی (Shahzib a.k.a
> Shabi)**.
>
> - **Earlier case**: FIR 891/24, incident dated 2024-09-14. Role: accused.
>   Recorded status: گرفتار، بعد ازاں سزا یافتہ (arrested, later convicted);
>   the criminal-record system records "Convicted, on bail pending appeal"
>   for this person on that FIR.
> - **Newer case**: FIR 214/26, incident dated 2026-03-03. Role: accused.
>   Recorded status: گرفتار (arrested); the criminal-record system records
>   "Under trial" for this person on that FIR.
>
> The two cases are 2 calendar year(s) apart.

The other variant (2 of 7) is the same answer with the conviction carried
only by the Urdu `arrest_status` gloss *"(arrested, later convicted)"* rather
than also by the English criminal-record string. Both cover gold.

### The intermediate arm, reported because it is the interesting one

An arm with the per-case timeline but **without** the leading summary line
was run live, 7 times (`scratchpad/after_cr2.json`,
`after_cr2_v1_runs4to7.json`). It is a real improvement over the refusal —
*"Yes, there are individuals who have appeared in earlier cases and later
resurfaced"*, three people listed with dates, roles and (6 of 7 runs) the
conviction. But it is **not gold's answer**: gold names one identity, and
that arm named three, including two whose later role is *witness*. The
summary line is what closed the gap. Recording this arm rather than only the
final one is the difference between "the fix worked" and "here is which half
of it did the work".

---

## 5. Gold comparison

**Gold:** *"Yes — this identity has a prior conviction (from FIR 891/24) and
is separately, currently accused in FIR 214/26."*

| gold element | measured | after |
|---|---|---|
| "Yes" | — | ✅ 7/7 |
| the identity is a single one | شہزیب عرف شابی, `PERSON-685fc54914` | ✅ 7/7, named |
| prior conviction | `Convicted, on bail pending appeal` + `گرفتار، بعد ازاں سزا یافتہ` | ✅ 7/7 |
| from FIR **891/24** | incident 2024-09-14 | ✅ 7/7 |
| separately, **currently** accused | role `accused`, `Under trial` | ✅ 7/7 |
| in FIR **214/26** | incident 2026-03-03 | ✅ 7/7 |
| the earlier/later ordering | 2024-09-14 → 2026-03-03 | ✅ 7/7 |

**Verdict: full thematic coverage, in the model's own words, 7 of 7 runs.**
CR2 goes **0.00 / 0.00 / 0.00 → pass on every run measured**.

**Gold is not challenged.** Unlike Modules 35, 39, 44 and 76 in this wave,
every figure gold asserts here reproduces exactly from an independent Cypher
probe. The answer adds two facts gold does not state — the incident dates and
the two-year gap — both of which are in the graph and neither of which
contradicts gold.

One honest note on what the after-answer does **not** say: it stops at the
one identity gold names and does not mention that عاصم رشید is also accused
twice, four days apart. That is the correct reading of *"an earlier case
already on record"*, and it is the discrimination `_spans_calendar_years()`
was added to make — but it is a judgement the evidence now supports rather
than one gold explicitly asks for.

---

## 6. Non-gold paraphrase

Three paraphrases, written before the live sweeps and **not adjusted
afterwards**. The results split, and the split is informative.

**P1 (English, 4 runs, `route='XAGG'` 4/4) —** *"Do we have any repeat
offender — someone who already had a case years ago and now shows up again as
a suspect in a fresh, unrelated FIR?"*

**Partial, and reported as such.** The answer surfaces شہزیب عرف شابی's prior
conviction and later accusation correctly and in the right order:

> **شہزیب عرف شابی** has a prior criminal record from an earlier case (FIR
> 891/24, 2024-09-14), where he was convicted and on bail pending appeal. He
> later appears in another case (FIR 214/26, 2026-03-03) as an accused. This
> could be considered a repeat appearance, but it is not explicitly stated
> whether the second case is unrelated to the first.

…and then hedges its overall verdict on the word **"unrelated"**, which my
paraphrase added and gold does not ask for ("newer, separate case"). The
capability transfers; the extra predicate does not, because nothing in the
graph asserts relatedness between two FIRs either way. Pre-fix, the same
paraphrase produced a flat refusal with no dates and no conviction at all, so
this is a genuine improvement measured on a wording the fix was not built
against.

**P2 (Roman-Urdu, 2 runs) and P3 (Urdu script, 2 runs) — `route='XGRAPH'`
4/4. Module 88's aggregate is never reached.** XGRAPH answers *"the same
entity recurs across 6 case(s) … chain confidence 50%"* and names no person;
P3 additionally asserts *"there is no indication that any individual … has a
prior case record"*, which is wrong. This is a **routing** defect, not a
rendering one — `router.py` is out of bounds for this module — and it is
filed as **Module 89**. It is the same shape Module 78 records for KB3/KB9
and Module 62 for CR3/G6: a capability reachable only from a narrow
neighbourhood of the gold wording.

---

## 7. Regression guard

The mandated set, **3 live runs each on the shipped code**, all on :8032.
Baseline is `evaluation/gold32_pass{1,2,3}_results.json` (recovered from
commit `0113c47`), whose 19 all-three-passes questions were recomputed here
and match the brief's count exactly.

| Q | baseline FC (3 passes) | route before | route after | after, 3 runs | verdict |
|---|---|---|---|---|---|
| **S3** | 0.9 / 1.0 / 0.9 | XAGG | XAGG | **3/3 pass** | ✅ **improved** |
| CR4 | 1.0 / 1.0 / 1.0 | XAGG | XAGG | 3/3 pass, byte-identical | ✅ |
| CR6 | 1.0 / 1.0 / 1.0 | XAGG | XAGG | 3/3 pass, byte-identical | ✅ |
| CR7 | 1.0 / 1.0 / 1.0 | XAGG | XAGG | 3/3 pass | ✅ |
| CR8 | 1.0 / 1.0 / 1.0 | XAGG | XAGG | 3/3 pass | ✅ |
| CS4 | 1.0 / 1.0 / 1.0 | XAGG | XAGG | 3/3 pass, byte-identical | ✅ |
| M4 | 1.0 / 1.0 / 1.0 | XAGG | XAGG | 3/3 pass | ✅ |
| G3 | 1.0 / 1.0 / 1.0 | XAGG | XAGG | 3/3 pass | ✅ |
| **CR2** | 0.0 / 0.0 / 0.0 | XAGG | XAGG | **7/7 pass** | ✅ **fixed** |

**24 regression runs + 7 CR2 + 4 P1 + 4 P2/P3 = 39 live runs, 0 transport
failures, 0 quota lines, 0 route changes.**

### S3 specifically — it shares this aggregate, and it got better

S3 (*"کیا کسی شخص کو ایک سے زیادہ بار گرفتار کیا گیا ہے؟"*) is the question
the brief singled out as the one that must keep passing. It passes 3/3, and
the answer **changed for the better**. Module 27's own captured S3 answer
(`gold32_pipeline_outputs.json` @ `0113c47`) listed all four recurring
persons and never used the word "arrested" at all — it could not, because
arrest status was not in the evidence:

> Yes, there are individuals who have been involved in more than one case, as
> shown below:
>
> - **فیصل** (Faisal): appears in 2 cases — fir-202-26, fir-401-26
> - **طارق** (Tariq): appears in 2 cases — fir-202-26, fir-401-26
> - **شہزیب عرف شابی** (Shahzib a.k.a. Shabi): appears in 2 cases — fir-214-26, fir-891-24
> - **عاصم رشید** (Aasim Rashid): appears in 2 cases — fir-64-26, fir-65-26

Post-fix it names exactly gold's **two**, and answers the question actually
asked, because `arrest_status` finally distinguishes an accused who was
arrested from a witness:

> Yes, there are instances where individuals have been arrested more than
> once. Here are the details:
>
> - **شہزیب عرف شابی (Shahzib a.k.a Shabi)**: … In **FIR 891/24**, dated
>   2024-09-14, the recorded status is "گرفتار، بعد ازاں سزا یافتہ"
>   (Arrested, later convicted). In **FIR 214/26**, dated 2026-03-03, the
>   recorded status is "گرفتار" (Arrested).
> - **عاصم رشید (Asim Rashid)**: … In **FIR 64/26**, dated 2026-03-19 …
>   "گرفتار" (Arrested). In **FIR 65/26**, dated 2026-03-23 … "پہلے سے کیس
>   10 میں گرفتار، اس مقدمے میں بھی نامزد".

Gold: *"جی ہاں، دو شناختی کارڈ نمبر۔ شہزیب عرف شابی (00000-1000001-1) ایف آئی
آر 891/24 اور 214/26 میں؛ عاصم رشید (00000-1000002-1) ایف آئی آر 64/26 اور
65/26 میں۔"* — **both identities and all four FIRs match exactly.** The CNICs
gold quotes are still not carried by this family; that was true before this
module and is unchanged by it. It is the one respect in which S3's answer is
short of gold, and it is not a regression.

### Two pre-existing discrepancies, unchanged and not mine

- **G3** reports the accused↔complainant gap as **82 of 94**; gold says 81 of
  94. Within tolerance, identical before and after, pre-existing.
- **CR7** reports **1 settled / 32 in progress** against gold's 30 under
  trial + 1 convicted; the extra two are `Sub judice, external prior case`
  and `Under trial, priority hearing requested`, both visible in the probe's
  §1.D distribution. Pre-existing, passes.

---

## 8. New defects found

**Module 89 — CR2's Roman-Urdu and Urdu paraphrases route to XGRAPH, so this
fix never reaches them.** Measured on :8032, 2 runs each, both
`route='XGRAPH'` 2/2. XGRAPH answers with *"the same entity recurs across 6
case(s) … chain confidence 50%"* and names no person; the Urdu-script one
goes further and asserts *"there is no indication that any individual … has a
prior case record"*, which the graph contradicts. The English gold wording
reaches XAGG 7/7. This is `router.py`, out of bounds here, and it is the
third independent instance of the same finding in this wave (Module 62 for
CR3/G6, Module 78 for KB3/KB9): the capability is real but reachable only
from a narrow neighbourhood of the gold text. It has a clean bisectable
control — same question, three languages, two routes.

**Left deliberately unfixed, not filed:** the criminal-record FIR join covers
**4 of 33** records, because 29 have no `source_case_ref`. A name-only join
would triple the coverage and would attribute records on a bare Urdu name
with no case reference at all — precisely the inference
`_match_criminal_record()` exists to refuse. The sparsity is disclosed in the
code and reported here rather than engineered around.
