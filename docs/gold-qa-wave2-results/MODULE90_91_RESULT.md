# Modules 90 / 91 — M1 (section grain, wrong act gloss) and CP6 (the split that "vanished")

**Branch:** `fix/m1-cp6-detail-loss`, rebased onto `origin/main` at `90fc68e`
(so the **corrected** CP6 gold from PR #57 is in this tree).

**Headline.** M1 is fixed: the aggregate now answers at section grain, names
the FIR total per year, and spells out the act abbreviations — and the live
answer now reproduces gold's figures. **CP6's stated defect does not
reproduce**, on either generation provider; the aggregate, the renderer and
(since PR #57) gold are all correct, and the one hardening that looked
obvious was implemented, measured, and reverted because it made things
strictly worse. Module 91 therefore ships no behavioural change for CP6 —
only a de-duplication with byte-identical output and two regression pins.
That is reported as a partial, deliberately.

---

## 1. Root cause

The brief asked for a three-way comparison per question — the aggregate
payload dict, the rendered `lines`, and the final answer — before any edit.
Done in-process (`run_aggregate()` → `_render_aggregate_text()` →
`large_scale_aggregate.py`'s own prompt and `call_llm()` call, same role and
same `max_tokens`), so no backend was started. The `logger.info("XAGG …")`
lines confirm which aggregate answered in every run below.

### CP6 — *"Kitne cases abhi tak bina kisi tafteeshi afsar ke asal tor par muqarrar kiye pade hue hain?"*

**Log line (proves the aggregate):**

```
INFO src.pipeline.xagg: XAGG placeholder_officer_count: 10 case(s) still carry a
placeholder investigating officer (11 ever); ASI=7 SI=3
```

**(a) Aggregate payload:**

```json
{"kind": "placeholder_officer_count", "current_count": 10, "ever_count": 11,
 "asi_count": 7, "si_count": 3}
```

**(b) Rendered lines:**

```
10 FIRs currently carry only a placeholder investigating officer — 7 marked
"(نامزد ASI)", 3 marked "(نامزد SI)". 1 additional case(s) originally had a
placeholder officer too but have since been assigned a real one.
```

**(c) Final answer — captured in the committed three-pass run (all three
passes byte-identical):**

```
Abhi tak 10 FIRs bina kisi tafteeshi afsar ke asal tor par muqarrar kiye pade
hue hain [Document 1].
```

**(c′) Final answer — re-run live today, same prompt, same rendered text:**

```
Abhi tak 10 FIRs bina kisi tafteeshi afsar ke asal tor par muqarrar kiye pade
hue hain — 7 mein "(نامزد ASI)" likha hai, 3 mein "(نامزد SI)" likha hai
[Document 1].
```

**The captured loss does not reproduce.** Local generation slot (qwen3:14b via
the ngrok tunnel — the provider `call_llm()` reaches first): **5/5** runs keep
the split. Cloud fallback (`force_cloud=True`, Groq `openai/gpt-oss-120b`):
**3/3** keep it. Ten out of ten.

I also checked that the code is not the difference: `git show 0113c47` (the
evaluation commit) renders `placeholder_officer_count` with the identical
inline block that is on `main` today, and `_placeholder_officer_count()` is
unchanged. So the code path that produced the captured answer is the code path
I re-ran. What differs is the model's behaviour on the day, and I cannot
recover that state.

**Then I tried to harden it, and the measurement said no.** The intuitive fix
is to break the one prose sentence into a headline plus one bullet per figure
— exactly the shape M1's per-year breakdown uses, which survived paraphrasing
with every figure intact in the same captured run. Three rendering shapes,
same prompt, same question, both providers:

| CP6 rendering shape | split kept, local | split kept, cloud |
|---|---|---|
| **A** — the shipped one sentence | **5/5** | **3/3** |
| **B** — headline + one bullet per figure | **0/5** | **0/3** |
| **C** — one sentence, split named up front | **5/5** | **3/3** |

Shape **B** is a regression, on both providers, unanimously. The mechanism is
legible in the outputs: a bulleted list hands the model a self-contained
headline it can answer the *"kitne"* (how many) question with and stop —
`"Abhi tak 10 FIRs … pade hue hain [Document 1]."`, which is exactly the
captured answer. The single sentence gives it no such stopping point. I had
this backwards, and shipping B would have manufactured the very defect the
module was opened to fix.

Raw runs: `scratchpad/ab_cp6.json`, `scratchpad/ab_cp6_cloud.json` (scratchpad
is gitignored; the table above is the record).

**Conclusion for CP6.** Aggregate correct (10 current / 11 ever / 7 ASI / 3
SI, matching the graph). Renderer correct and already emitting the split.
Gold was the thing that was wrong, and PR #57 corrected it to 10 / 7 / 3. The
paraphrase omission is real but stochastic and currently unobservable. **No
behavioural change shipped.**

### M1 — *"What kinds of cases are we dealing with now compared to a couple of years back?"*

**Log line:**

```
INFO src.pipeline.xagg: XAGG time_bucketed_breakdown: dimension=statute_by_year,
2 year bucket(s) [2024:cases=13,acts=26,sections=3, 2026:cases=51,acts=82,sections=31]
```

**(a) Aggregate payload, before:** act grain only —
`{"year": 2024, "counts": [{"key": "PPC", "count": 13}, {"key": "Arms Ordinance 1965", "count": 13}]}`
and the 2026 equivalent. No sections, no FIR total.

**(b) Rendered lines, before:**

```
**2024:**
  - PPC: 13
  - Arms Ordinance 1965: 13
**2026:**
  - PPC: 39
  - Arms Ordinance 1965: 16
  - CNSA 1997: 12
  - PECA 2016: 9
  - Punjab Domestic Violence Act: 4
  - Illegal Dispossession Act 2005: 2
```

**(c) Final answer, captured (all three passes byte-identical):** the full act
table for 2026 **and** for 2024, closing with a comparison paragraph — plus
the invented gloss **"PPC (Preventive Detention and Control Act)"**.

Three separate findings, and only two of the three the brief listed are real:

1. **Act vs section — REAL, structural.** `_statute_mix_by_year()` bucketed
   `split_crime_category(c["crime_category"])`, and `crime_category` is a
   comma-joined **act** list that `muhafiz_cases._crime_category()` builds
   with `section_code` already discarded. It cannot in principle express
   gold's counts: every one of 2026's 39 "PPC" cases reads as the same kind
   of case whether it is a murder (302), a robbery (392) or a fraud (420).
   Module 24 found this and declined to take it.
2. **"2024 is missing" — NOT REAL.** The brief (and the plan) say the answer
   gives 2026 only. Re-read from `evaluation/gold32_pass{1,2,3}_outputs.json`,
   all three captured answers contain
   *"In comparison, in 2024, the cases included: **PPC:** 13 cases,
   **Arms Ordinance 1965:** 13 cases"*. The 2024 bucket exists, is rendered
   and is paraphrased correctly. The 64-of-73 `report_datetime` population
   the brief flagged is a red herring here: `_statute_mix_by_year()` keys off
   the `OCCURRED_ON {event_type:'incident'}` edge, and that resolves a year
   for 64 cases — 13 in 2024, 51 in 2026 — which is gold's own denominator on
   both sides. **No fix made, because none was needed.**
3. **The wrong act gloss — REAL.** `grep -rn "Preventive Detention"` over the
   whole repository matches **only** inside the four captured evaluation
   output files. Nothing in `src/`, `prompts/`, the data or the frontend ever
   wrote it. It is a model invention filling a gap: the evidence document said
   only `PPC: 39`, and the model supplied an expansion. PPC is the **Pakistan
   Penal Code**.

**Ground truth, re-derived live** (`StructuredRecord{record_type:'fir_section'}`
joined to the incident-year edge, de-duplicated per case):

```
2024 — 13 FIRs:  PPC §34 13, PPC §392 13, Arms Ordinance 1965 §13 13
2026 — 51 FIRs:  PPC §34 23, Arms Ord §13 16, CNSA §9(c) 12, PECA §14 9,
                 PECA §21 9, PPC §337-A(i) 9, PPC §419 9, PPC §420 9,
                 PPC §302 8, PPC §392 8, PPC §506 6, PPC §365-A 5,
                 PPC §384 4, Punjab DV Act 4, Illegal Dispossession §3 2
```

That reproduces **every figure** gold names, on both sides of the comparison.

**Correction to the brief's stated row counts:** the corpus holds **218**
`fir_section` rows in total, not "273 for 2024 and 979 for 2026". Those
numbers cannot be right — 273 rows over 13 cases would be 21 sections per FIR.
Counting per CASE (a set per case, since the corpus does hold repeat rows for
the same case/act/section triple) is what reproduces gold; counting rows would
inflate every figure.

---

## 2. The change

**`src/pipeline/xagg.py`**

- `_statute_mix_by_year()` now issues a second read —
  `StructuredRecord{record_type:'fir_section'}` → `act`, `section_code`,
  `case_id` — the **identical** query `_weapon_statute_cooccurrence_by_year()`
  (Module 23) and `_statute_court_stage_join()` (Module 24) already perform,
  labelled through the same `_statute_label()`, so the three cannot disagree
  about what a statute label is. De-duplicated into a set per case.
- Each bucket gains **`case_count`** (FIRs that year) and **`section_counts`**
  (top 15, ties broken alphabetically so the cut is reproducible). The
  existing act-level **`counts` is unchanged** — this is additive.
- New `render_statute_mix_by_year()` and `render_placeholder_officer_count()`,
  replacing the three hand-copied inline blocks each had, per the convention
  every other `render_*` in this module follows.
- `_ACT_FULL_NAMES` / `_act_with_full_name()`: PPC → Pakistan Penal Code,
  CrPC → Code of Criminal Procedure, CNSA 1997 and PECA 2016 spelled out. An
  unmapped act renders bare, exactly as before.
- The `XAGG time_bucketed_breakdown` log line now reports cases/acts/sections
  per year instead of one summed act count.

**`src/pipeline/harness/tools/xagg.py`**, **`src/pipeline/orchestrator.py`** (both
XAGG rendering sites): the four inline blocks replaced by calls to the two
shared renderers. No text change at the CP6 site.

**`tests/test_xagg.py`**: seven new tests (§3).

### What I deliberately did NOT do, and why

- **Did not reshape CP6's rendering.** Measured as a cross-provider
  regression (§1). The shared renderer emits byte-identical text and is pinned
  by an exact-string test.
- **Did not touch `_SYSTEM_PROMPT_TEMPLATE` in
  `src/pipeline/harness/agents/large_scale_aggregate.py`.** It is shared by
  **25 of the 32** gold questions, and it *already* says "Do not add, omit, or
  alter any number". Strengthening it would have moved two dozen answers to
  chase a defect that does not currently reproduce — and that file is in
  `D:/Rapids AI/muhafiz-kbgen`'s territory (Modules 82/85/86). **This branch
  touches no shared generation code at all**, so there is nothing to reconcile
  at merge.
- **Did not change the verifier.** Making
  `verify_structured_aggregate_paraphrase()` reject a paraphrase that omits a
  source number would fall back to `raw_summary_text` correctly for CP6 but
  would reject almost every legitimate paraphrase of a 15-row listing. Wrong
  blast radius.
- **Did not add gold's busiest-named-officer clause** to CP6 (see §8).
- **Did not fix the act gloss globally.** `_act_with_full_name()` is applied
  only in M1's renderer. Applying it inside `_statute_label()` would have
  changed M4, M5 and KB9's rendered evidence — out of scope, and filed in §8.

---

## 3. Unit tests

Seven new tests in `tests/test_xagg.py`, plus a query-routing fake
(`_StatuteMixAgeClient`) since the aggregate now issues two distinct reads.

| Test | Asserts |
|---|---|
| `test_m1_statute_mix_reports_section_grain_not_only_act_grain` | `section_counts` exists and is per-CASE (a case citing PPC §34 twice counts once); act grain unchanged |
| `test_m1_statute_mix_reports_the_fir_total_per_year` | `case_count` present, and provably not the act sum (3 acts over 2 FIRs) |
| `test_m1_section_counts_capped_at_the_render_limit` | 30 tied sections cut to 15 as a stable alphabetical prefix |
| `test_m1_renderer_names_sections_the_fir_total_and_expands_ppc` | renders "51 FIR(s)", "PPC §302: 8", "PPC (Pakistan Penal Code)" — and asserts "Preventive Detention" is **not** in the output |
| `test_m1_renderer_still_renders_a_payload_with_no_section_counts` | a pre-Module-90 payload still renders at act grain instead of raising |
| `test_cp6_renderer_keeps_the_asi_si_split_and_the_ever_caveat` | exact-string pin on the rendered text, including both Urdu markers |
| `test_cp6_renderer_drops_the_caveat_when_nothing_was_superseded` | ever == current suppresses the caveat |

**Both directions checked.** With `src/` stashed back to `origin/main` and the
new tests in place, **all seven fail** (the five M1 ones on missing
`section_counts` / `case_count` / `render_statute_mix_by_year`; the two CP6
ones on `AttributeError: module 'src.pipeline.xagg' has no attribute
'render_placeholder_officer_count'`). With the change applied, all seven pass.

Full files green: `tests/test_xagg.py` (426 passed),
`tests/test_harness_tool_xagg.py`, `tests/test_orchestrator.py`. Run as
targeted files, not a whole-suite `pytest -q`, which is known to empty the
entity-embeddings Chroma collection.

---

## 4. Live verification

Run in-process against the real graph and Postgres, through the same
`call_llm()` path `large_scale_aggregate.py` uses (same role, same
`ANSWER_MAX_TOKENS=3000`, same system prompt), so this exercises everything
below the HTTP layer. **No backend was started** — see §7 for what that costs.

### M1 — before

> Currently, in 2026, the types of cases being dealt with include:
> **PPC (Preventive Detention and Control Act):** 39 cases; **Arms Ordinance
> 1965:** 16; **CNSA 1997:** 12; **PECA 2016:** 9; **Punjab Domestic Violence
> Act:** 4; **Illegal Dispossession Act 2005:** 2. In comparison, in 2024, the
> cases included: **PPC:** 13 cases, **Arms Ordinance 1965:** 13 cases. …

(Committed capture. My own pre-change re-run produced the same shape and the
same class of invented gloss — *"PPC (Preventive Detention and Control of
Dangerous Persons Act)"*.)

### M1 — after

> Compared to 2024, the number of FIRs has increased significantly in 2026,
> from **13 to 51**. In 2024, all cases were related to the **Pakistan Penal
> Code (PPC)** and the **Arms Ordinance 1965**, specifically under **sections
> 34 and 392 of the PPC and section 13 of the Arms Ordinance 1965**
> [Document 1].
>
> In 2026 … cases also involve the **Control of Narcotic Substances Act 1997**,
> the **Prevention of Electronic Crimes Act 2016**, the **Punjab Domestic
> Violence Act**, and the **Illegal Dispossession Act 2005**. The specific
> sections charged in 2026 include a broader range of PPC sections, such as
> **302, 337-A(i), 365-A, 384, 419, 420, and 506**, as well as sections from
> the CNSA 1997, PECA 2016, and other acts [Document 1].

Three runs; all three carry the year totals, the 2024 section triple, the
2026 section spread, and the correct PPC expansion. **No run invented a
gloss.** One of the three enumerated all fifteen 2026 sections with their
counts individually.

### CP6 — before and after

Rendered evidence and final answer are **unchanged by this branch** (proven
byte-identical in §7). Live re-run, both providers:

> Abhi tak 10 FIRs bina kisi tafteeshi afsar ke asal tor par muqarrar kiye
> pade hue hain — 7 mein "(نامزد ASI)" likha hai, 3 mein "(نامزد SI)" likha
> hai [Document 1].

---

## 5. Gold comparison

Judged on substance, per the standing standard — matching the content of the
idea, not the wording.

### CP6 — against the **corrected** gold (PR #57, in this tree)

Gold: *10 FIRs … "(Naamzad ASI)" 7 par aur "(Naamzad SI)" 3 par. Ek gyarhwan
case (fir-205-26) par pehle "(Naamzad ASI)" tha, magar wahan ab asal afsar
(Salman) muqarrar ho chuka hai … Iske muqable mein sab se masroof naamzad
afsraan (Faisal aur Tariq) ko 4-4 FIRs assign hain.*

| Gold claim | Answer | |
|---|---|---|
| 10 FIRs still placeholder-only | "10 FIRs" | ✅ |
| 7 × "(نامزد ASI)" | "7 mein (نامزد ASI)" | ✅ |
| 3 × "(نامزد SI)" | "3 mein (نامزد SI)" | ✅ |
| an 11th case since reassigned | present in the rendered evidence; carried into the answer in some runs, not all | ⚠️ partial |
| busiest named officers 4 FIRs each | absent — no aggregate computes it | ❌ (§8) |

Three of gold's five claims fully, a fourth partially. The core of the
question — how many, and split how — is exact.

### M1

| Gold claim | Answer | |
|---|---|---|
| 2024 = 13 FIRs | "13" | ✅ |
| 2024 uniformly PPC 34 / 392 / Arms §13, 13/13 each | "sections 34 and 392 of the PPC and section 13 of the Arms Ordinance 1965", all at 13 | ✅ |
| 2026 = 51 FIRs | "51" | ✅ |
| PPC 34 ×23, 392 ×8, Arms ×16 | in the evidence and in the enumerating runs; named-not-counted in the condensing runs | ✅ / ⚠️ |
| CNSA §9(c) ×12 | ✅ | ✅ |
| PECA §14 & §21 ×9 each | ✅ | ✅ |
| 419 / 420 ×9 each | ✅ | ✅ |
| 365-A ×5, 302 ×8 | ✅ | ✅ |
| domestic violence ×4 | ✅ (rendered as "Provincial Act §Punjab Domestic Violence Act" — see §8) | ✅ |
| "mix has broadened toward cyber-enabled and financial crime" | "the range of legal acts charged has expanded significantly" + the cyber/fraud statutes named | ✅ |
| **no invented act expansion** | PPC now correctly "Pakistan Penal Code" in every run | ✅ |

Every figure gold names is present in the evidence and reproduced exactly.
Whether a given run enumerates all fifteen sections with counts or names the
sections and gives counts for the headline ones varies run to run; both
carry gold's substance.

---

## 6. Non-gold paraphrase

Rewordings written to test the capability, not the string. Routed offline
first (`resolve_aggregate_kind()`), then run live end to end.

| # | Question | Routes to | Result |
|---|---|---|---|
| M1-p1 | *"Do saal pehle ke muqable mein aaj kal hamare paas kis tarah ke cases aa rahe hain?"* (Roman-Urdu; gold is English) | `statute_mix_by_year` | ✅ full 2024/2026 answer, both grains, all 15 sections with counts, correct PPC expansion |
| M1-p2 | *"Compared with two years ago, which statutes are we charging today?"* | `statute_mix_by_year` | ✅ correct contrast, all acts spelled out |
| CP6-p1 | *"How many FIRs still have only a placeholder investigating officer instead of a real named one?"* (English; gold is Roman-Urdu) | `placeholder_officer_count` | ✅ "10 … 7 marked (نامزد ASI) and 3 marked (نامزد SI)", plus the reassigned 11th |
| CP6-p2 | *"Kitne FIRs mein asal tafteeshi afsar ki jagah sirf placeholder darj hai?"* | `placeholder_officer_count` | ✅ content exact, all three figures + the caveat — **but answered in Devanagari script**, see §8 |

A third M1 paraphrase, *"Has the mix of offences changed between 2024 and
now?"*, **mis-routes** to `station_or_category_counts`. Pre-existing (the
predicate is untouched by this branch, and the before/after sweep in §7 shows
zero predicate changes across all 32 gold questions). Filed in §8.

---

## 7. Regression guard

**What I ran.** All **32** gold questions through
`run_aggregate()` → `_render_aggregate_text()` live against the real graph and
Postgres, on `origin/main` and on this branch, comparing the routed aggregate
kind, the `resolve_aggregate_kind()` predicate, and the **rendered evidence
byte for byte**:

```
questions: 32
CHANGED: ['M1']
kind changes anywhere: []
predicate changes anywhere: []
```

**Exactly one question's output differs, and it is M1.** CP6's rendered
evidence is byte-identical, which is the claim the shared-renderer
de-duplication rests on. No question changed aggregate family or routing
predicate.

**What I did NOT run, and what that costs.** I did not re-run all 32 through
the live HTTP backend. The standing constraint is at most two backends across
the machine and two other agents were holding them; a quota-poisoned run
measures nothing. The sweep above is exhaustive over the layer this branch
changes — `run_aggregate()` and the three renderers are the only code
touched, and everything downstream of them is a byte-identical input for 31 of
32 questions, so those 31 cannot move for any reason attributable to this
branch. What the sweep cannot cover is LLM-side variance, which is not
attributable to this branch either and is **not byte-stable even with no
change at all**: my pre-change M1 re-run produced different prose from the
committed capture on unmodified code. A byte-equality assertion on final
answers is therefore not a test this pipeline can pass, and I would rather say
so than report a pass I did not earn. **A live 32-question rerun is the
outstanding verification for this branch.**

---

## 8. New defects found, not fixed

1. **Gold's busiest-named-officer comparison has no aggregate behind it.**
   CP6's gold closes with *"sab se masroof naamzad afsraan (Faisal aur Tariq)
   ko 4-4 FIRs assign hain"*. Nothing in `xagg.py` computes a per-named-officer
   caseload, so the answer cannot make that comparison at any prompt. Adding
   it means a new field on `_placeholder_officer_count()` and a second clause
   in a rendering that §1 shows is sensitive to shape — worth doing
   deliberately, with the same A/B measurement, not as a rider on this branch.

2. **`Provincial Act §Punjab Domestic Violence Act` is a malformed statute
   label, in the data.** Four 2026 FIRs carry a `fir_section` row whose `act`
   is the generic literal `"Provincial Act"` and whose `section_code` is the
   *act's own name*. `_statute_label()` composes them faithfully, so the label
   reads as a section of a non-existent act. Substance survives (gold's
   "domestic violence ×4" matches), but the ingestion that wrote it has the
   two columns swapped in effect. Affects M5 and M4 too, which read the same
   record type.

3. **The invented-act-expansion failure mode is general; only M1 is
   immunised.** `_act_with_full_name()` is applied in M1's renderer alone.
   Every other aggregate that renders a bare `"PPC"` — M4's
   `statute_court_stage_join`, M5's `weapon_statute_cooccurrence`, KB9's
   `fir_section_case_count` — hands the model the same gap that produced
   "Preventive Detention and Control Act". None of those three currently
   shows the symptom, but nothing prevents it. The clean fix is to apply the
   expansion inside `_statute_label()` for all of them, which changes three
   other questions' rendered evidence and needs its own regression pass.

4. **Language fidelity: a Roman-Urdu question answered in Devanagari.**
   CP6-p2 (§6), asked in Roman Urdu, came back in Hindi/Devanagari script from
   the local generation slot; the cloud fallback answers CP6 in Urdu script
   rather than the Roman-Urdu the question used. `resolved_language` is the
   literal string *"the same language as the user's question"* whenever the
   user profile carries no `preferred_language` — which is the case for the
   evaluation admin, and therefore for every gold run. Content is unaffected;
   readability for the actual user is not. Likely overlaps
   `D:/Rapids AI/muhafiz-kbgen`'s territory.

5. **`resolve_aggregate_kind()` misses a plain M1 paraphrase.** *"Has the mix
   of offences changed between 2024 and now?"* routes to
   `station_or_category_counts`, losing the year comparison entirely, despite
   naming a year and asking for a change. Pre-existing; the keyword predicate
   is untouched by this branch.

6. **Nine of 73 cases have no resolvable incident year** (64 `OCCURRED_ON
   {event_type:'incident'}` edges over 73 cases with `fir_section` rows), so
   they fall out of M1's year buckets silently. Gold's own denominators
   (13 + 51 = 64) agree with the aggregate, so this is not an M1 defect today
   — but the aggregate reports no "undated" figure, unlike
   `_weapon_statute_cooccurrence_by_year()`, which does. A future corpus with
   more undated cases would shrink M1's totals with nothing saying so.
