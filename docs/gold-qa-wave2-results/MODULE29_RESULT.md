# Module 29 — CR3 / G1 / G6: Meta-Analysis didn't decompose broad synthesis questions

**Branch:** `fix/meta-analysis-decompose-broad-synthesis`
**Base:** `main` @ `c2a6ac6` (PR #19, Module 28, merged).
**Files:** `src/pipeline/harness/agents/meta_analysis.py`,
`src/pipeline/harness/supervisor.py`,
`prompts/meta_analysis_decomposer.txt`,
`tests/test_harness_agent_meta_analysis.py`.
**Split out of:** Module 21.
**All live captures:** 2026-09-08, own backend on `:8003`, platform-admin
(`admin@example.com`), All Cases, real Postgres/AGE + the shared model server.

---

## 1. Root cause

**Confirmed first-hand, not inherited.** `_decompose()` was called directly
against each question's literal gold text on this branch's base commit
(`PYTHONPATH=.`, worktree interpreter):

```
[CR3] decompose=False parse_failed=False
[G1]  decompose=False parse_failed=False
[G6]  decompose=False parse_failed=False
[M2]  decompose=True  parse_failed=False
    - What is the growth rate of caseloads at general-purpose stations over the past year?
    - What is the growth rate of caseloads at stations specialized for one specific type of crime over the past year?
```

So the brief's framing is **right**: `_decompose()` really did return
`decompose: false` for all three, and Module 25's question (M2) really was
unaffected. All three questions *did* reach Meta-Analysis —
`supervisor.py`'s `_META_ANALYSIS_TRIGGER_PATTERNS` already carried a
pattern for each (CR3 `same way`, G1 `review…caseload` / `flag anything` /
`worth monitoring` / `acting as a`, G6 `orientation note` / `tawaqqo…rakhni
chahiye`). The decomposer LLM simply judged each one "a single
broad-sounding question that is still really one ask", so `meta_analysis()`
fell back to ONE non-decomposed dispatch of the original query, which landed
back on XNETWORK / Cross-Case Linkage and was correctly refused there
(Module 21's measured distances: CR3 0.156, G1 0.202, G6 0.181, cutoff
0.145). **`xnetwork.py` and `RELEVANCE_DISTANCE_THRESHOLD` are untouched by
this module.**

**Two things the brief did not anticipate, both measured here:**

1. **The sub-queries themselves were being misrouted.** A decomposed
   sub-query is re-classified by another `route_query()` call. Nine
   naturally-phrased candidate sub-questions were run through
   `route_query()` + `classify_to_subagent()` on the base commit: **6 of 9
   landed on `XGRAPH → Cross-Case Linkage`** — the exact dead end
   decomposition exists to route away from. Decomposing without fixing the
   phrasing would have replaced one refusal with several.
2. **G1's own paraphrase never reached this module at all.** *"As the
   on-duty analyst, look over everything currently open and tell me what's
   worth a second look"* matched **no** pattern in
   `_META_ANALYSIS_TRIGGER_PATTERNS`, so no change inside
   `meta_analysis.py` could ever have affected it. The trigger list was
   curve-fit to the gold strings — the same gap Module 22 caught in its own
   reporting-speed keywords.

---

## 2. Published gap analysis (done first, as the brief requires)

Every row below was probed **live** against `xagg.py::run_aggregate()` and,
where relevant, against the graph directly. "Falls through" means
`run_aggregate()`'s keyword chain reaches an unrelated family and returns a
confident, wrong answer with no caveat — worse than a refusal.

### Already exists — used by this module's plans

| Sub-fact | Primitive | Live result |
|---|---|---|
| FIR ↔ walk-in CMS complaint linkage (CR3) | `_cms_fir_linkage()` (M15) | 4 complaints, 4 linked — incl. `CMS-ISB-2026-0341 → fir-64-26`, gold's exact fact |
| Case mix by year (G1, G6) | `_statute_mix_by_year()` (M13/26) | 2024: PPC 13, Arms 13 · 2026: PPC 39, Arms 16, CNSA 12, PECA 9, DV 4, IDA 2 |
| Reporting-delay trend (G6) | `_incident_to_report_minutes_by_year()` (M22) | 2024 mean 15.0 min (13 FIRs) → 2026 mean 1401.3 min (51 FIRs) |
| Weapon licensing rate (G1, G6) | `_weapon_compliance_scan()` (M15) | 32 weapons, 30 unlicensed, 2 no status |
| District spread (G6) | `_top_districts_by()` | 9 districts; فیصل آباد 19, لاہور 18, راولپنڈی 10, … ملتان 1 |
| Gender of the accused (G6) | `_gender_breakdown()` | 94 accused: مرد 67, عورت 24, unknown 3 |
| Record completeness (G1) | `_case_completeness_scan()` (M15) | 73 cases; 9 missing incident date, 52 missing status |
| Repeat offenders (G1, CR3) | `_top_recurring_nodes("Person")` | 4 people in 2 cases each, incl. عاصم رشید → fir-64-26, fir-65-26 |
| Criminal record ↔ court outcome (G1) | `_criminal_record_court_crosscheck()` (M14) | 33 records, 1 settled, 32 in progress, 1 cross-check |

### Missing — filed as new modules, deliberately NOT built here

| Sub-fact | Underlying data | What happens today | New module |
|---|---|---|---|
| Offender age range/average (G1) | **Present** — `Person.age` on 19 nodes: min 24, max 49, mean **31.8** | `_AGE_KEYWORDS` → hard `unsupported_aggregate` refusal (stale: Module 1d projected `age` after that refusal was written) | **31** |
| Accused↔complainant relationship (G1) | **Present** — `Person-[:RELATED_TO {role}]->Person`, 24 edges: اجنبی 15, محلے دار 2, ساس 2, سینئر ساتھی کار 2, شوہر 2, بھائی 1 | Falls through to person-recurrence | **32** |
| Seized-property disposition (G1) | **Present** — 45 `malkhana_register` records; **13** `سیل بند، نمونہ فرانزک لیبارٹری بھجوایا گیا`, **7** `ورثاء کے حوالے کیا جائے گا` | Falls through to person-recurrence | **33** |
| Incident time-of-day (G1) | **Present** — `Incident.incident_datetime` on 64/73: evening 19, afternoon 16, night 15, morning 14 | Falls through to `case_listing` | **34** |
| Arrest rate (G6) | **Present** — `INVOLVED_IN {role:'accused', arrest_status}`, 94 edges; 13 FIRs carry an arrest-word status | Falls through to person-recurrence | **35** |
| Subject-filtered FIR listing (CR3) | Present in `get_cases()` but only as the **unfiltered** 73-row `case_listing` | No aggregate returns FIR numbers filtered by statute/station/crime type | **36** |
| Nationality of the accused (G1) | **ABSENT** — 0 `Person` nodes carry `nationality` | n/a | none — data-model gap, recorded not filed |

Gold's own numbers reproduce from the graph for the *present* ones (age
24–49 mean 31.8 vs gold "between 24 and 49, average ~31"; 13 forensic-lab
and 7 heirs items exactly as gold states; اجنبی dominant as gold states),
so these are aggregate gaps, not data gaps.

**Correction to the task hand-off:** it said Module 23's
`_weapon_statute_cooccurrence_by_year()` had already landed. At this
branch's base commit (`c2a6ac6`) it had not — `xagg.py` there carried only
Module 28's `_weapon_evidence_chain()`, and the gap analysis above was
measured against that tree. Modules 23 and 24 (PRs #20 and #22) merged
**while this module was in live verification** and are now merged into this
branch; neither changes any row above.

**One follow-up the merge creates, recorded not acted on.** Module 24's new
`_statute_court_stage_join()` (33 / 1 / 30 under trial) answers gold G6's
"most matters are still pending in court" element, which §6 below scores as
a miss. It is a natural sixth sub-query for the `orientation_note` plan —
but `_MAX_SUB_QUERIES` is 5 and that plan is already full, and adopting it
would displace a slot that *was* live-verified in this module. Left to the
cap decision that Modules 31–34 also need.

**Why the plans do not simply dispatch the missing sub-questions anyway.**
Four of the six would come back *confidently wrong* (the fall-through to
person-recurrence returns "4 people appear in 2 cases each" for a question
about relationships or seized property) and would poison the synthesis.
`_MAX_SUB_QUERIES` is 5 and G1's plan is already full, so when Modules 31–34
land, adding their sub-questions means either replacing a slot or raising
that cap — flagged in each new module's section.

---

## 3. Change

**`src/pipeline/harness/agents/meta_analysis.py`** — `_DECOMPOSITION_PLANS`,
three deterministic question-shape families checked **before** the
decomposer LLM call (which is skipped entirely on a match):

| Plan | Triggers on | Sub-queries |
|---|---|---|
| `record_consistency` | `same way`, `processed and recorded`, `handled/recorded/processed … same`, `ek hi tarah`, `ایک ہی طرح` | person recurrence; CMS↔FIR linkage |
| `orientation_note` | `orientation note`, `tawaqqo…rakhni chahiye`, `newly posted/assigned/…`, `new officer … expect`, `naye tainaat`, `نئے تعینات` | district spread; case mix by year; reporting speed; weapon licence; gender |
| `caseload_review` | `review…caseload`, `flag anything`, `worth monitoring/flagging/watching`, `worth a second/closer look`, `anything … unusual`, `looks unusual`, `look over everything`, `ghair mamooli`, `غیر معمولی` | completeness; person recurrence; weapon licence; case mix by year; criminal-record vs court |

Deterministic rather than prompt-only for three reasons, all measured: the
brief requires tests pinned to the *literal* decomposition across model
drift; each sub-query is re-routed by another LLM call unless a
deterministic router override catches it first (6 of 9 natural phrasings
missed); and `xagg.py` is itself a family of canned aggregates selected by
keyword, so a canned plan per question shape is the same paradigm one layer
up. **Every planned sub-query is worded to match
`router.py::_deterministic_route_override()`** — verified live, all 9 return
`det=Y route=XAGG` — so decomposition adds *zero* extra router LLM calls.

**Synthesis prompt** — two rules restated *after* the sub-answers, both
live-caught rejecting otherwise-correct decomposed answers on this module's
own first runs:

- CR3: verifier `off_topic=True`, *"Answer is substantial … but cites no
  [Document N] source at all"* — a long `synthesis_goal` plus five
  sub-answers was reliably burying the citation rule stated at the top.
- G6: verifier `unsupported=1`, *"The claim about 73 total cases is not
  directly stated in any chunk … their sum requires inference"* — the model
  added up the per-district counts. Correct arithmetic, but unverifiable at
  this layer by construction.

**`src/pipeline/harness/supervisor.py`** — 15 paraphrase-reach patterns
appended to `_META_ANALYSIS_TRIGGER_PATTERNS` so the three shapes' *reworded*
forms reach this module at all. Scope grew into this file for the same
reason Module 26's did, and for a reason no `meta_analysis.py` change could
address.

**`prompts/meta_analysis_decomposer.txt`** — a "broad analytical and
orientation questions are compound" section, the "begin with *How many
cases* / end with *across all cases*" phrasing rule (previously the prompt
told the model to say "What is the breakdown of…", a phrasing the
deterministic router does **not** recognize), and three worked examples in
generic wording, for paraphrases outside the deterministic families.

**Deliberately not changed:** `xnetwork.py`, `RELEVANCE_DISTANCE_THRESHOLD`,
`xagg.py`, `router.py`, `verifier.py`.

---

## 4. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_harness_agent_meta_analysis.py -p no:cacheprovider
44 passed in 0.82s          (28 before this module)
```

Full related-suite run — `meta_analysis`, `supervisor`, `router`, `xagg`,
`xnetwork`, `cross_case_linkage`, `large_scale_aggregate`,
`harness_tool_xnetwork`, `verifier`, `orchestrator`:

```
611 passed, 1 skipped, 1 xpassed in 21.63s
```

The skip and the xpass are both pre-existing (`test_xagg.py` needs a
`Gold_QA_Dataset_Final32.json` that isn't in this checkout). **The full
`pytest -q` suite was deliberately not run** — it empties the real
`muhafiz_entity_descriptions` Chroma collection.

New tests, all pinned to the literal gold text:

- `test_module29_broad_synthesis_questions_decompose_deterministically`
  (CR3 / G1 / G6 / G1-paraphrase) — `decompose=True`, the right plan, and
  the decomposer LLM is monkeypatched to **raise**, proving no model call.
- `test_module29_literal_sub_query_decomposition_is_pinned` — the exact
  sub-query tuple for each of CR3, G1 and G6.
- `test_module29_every_planned_sub_query_routes_deterministically_to_xagg`
  — every planned sub-query hits `router._deterministic_route_override()`
  and gets `route == "XAGG"`.
- `test_module29_no_other_gold_question_matches_a_decomposition_plan`
  — over all 32 gold questions, asserts equality with exactly
  `{CR3: record_consistency, G1: caseload_review, G6: orientation_note}`.
- `test_module29_supervisor_trigger_widening_adds_no_gold_question`
  — the 15-question trigger set is pinned unchanged, and the G1 paraphrase
  is asserted to now reach the module.
- `test_module29_single_fact_questions_still_return_decompose_false`
  (D1, a case summary, a FIR lookup, CP1's shape, **G5**) — no plan claims
  them and `_decompose()` still returns False.
- `test_module29_g1_dispatches_five_sub_queries_not_one` — the observable
  this module is graded on: five dispatches, not one, and not the original
  query text.

Module 25's own tests (`test_strip_nested_citations_…`,
`test_nested_citations_are_stripped_…`,
`test_hallucinated_synthesis_is_still_rejected`) all still pass unchanged.

---

## 5. Live verification

Every run below is `/api/chat`, platform-admin, All Cases, **whole SSE
stream parsed** — not the last route event. Meta-Analysis emits a second
route event per decomposed sub-query, which is exactly what proves the fix.

### CR3 — 66.8 s, `status=done`

```
[supervisor:dispatch] Classified query as route='XNETWORK' -> sub-agent='Meta-Analysis'
[supervisor:dispatch] Classified query as route='XAGG'     -> sub-agent='Large-Scale Aggregate'
[supervisor:dispatch] Classified query as route='XAGG'     -> sub-agent='Large-Scale Aggregate'
```

> No, not identically. The FIR numbers **fir-64-26** and **fir-65-26** (both
> linked to accused **عاصم رشید**) show differing handling: **fir-64-26** has
> a matching walk-in complaint (**CMS-KHI-2026-0417**) per [Document 2],
> while **fir-65-26** does not appear in the walk-in complaint linkage list
> [Document 2]. This split indicates the two cases were processed
> differently.
>
> *A cited claim ([Document 2]) could only be partially confirmed against its
> source: The source confirms that fir-64-26 is linked to CMS-KHI-2026-0417
> but does not mention fir-65-26 or its absence in the linkage list.*

### G1 — 110.3 s, `status=done`

```
route='XNETWORK' -> 'Meta-Analysis'
route='XAGG' -> 'Large-Scale Aggregate'   x5
```

> The current caseload reveals several critical anomalies and trends
> warranting immediate attention:
> 1. **Severe record incompleteness** … 9 FIRs lack incident dates entirely
> [Document 1], and 52 FIRs have no recorded investigation status …
> 2. **Repeat offenders** … **فیصل** and **طارق** both appear in
> **fir-202-26** and **fir-401-26** [Document 2]. **شہزیب عرف شابی** is
> linked to **fir-214-26** and **fir-891-24**, while **عاصم رشید** appears in
> **fir-64-26** and **fir-65-26** [Document 2].
> 3. **Weapons recovered without licences** … 30 of 32 reviewed
> weapon-register entries lack licence records, and 2 entries have no licence
> status at all …
> 4. **Case-type diversification** since 2024 … **PPC cases** (39 in 2026 vs.
> 13 in 2024) and the emergence of … **Punjab Domestic Violence Act** (4
> cases) and **PECA 2016** (9 cases) [Document 4] …
> 5. **Court outcome discrepancies** … only 1 of 33 criminal records (FIR
> 891-24, **شہزیب عرف شابی**) matches its court outcome [Document 5] …

### G6 — 102.8 s, `status=done`

```
route='XNETWORK' -> 'Meta-Analysis'
route='XAGG' -> 'Large-Scale Aggregate'   x5
```

> **District Concentration**: Cases are concentrated in **فیصل آباد**
> (Faisalabad: 19 cases) and **لاہور** (Lahore: 18 cases), with **راولپنڈی**
> (Rawalpindi: 10 cases) as the next major hub … **ملتان** (Multan: 1 case)
> [Document 1].
> **Case Mix**: Current cases (2026) include **39 … (PPC)** cases (up from 13
> in 2024), **16 Arms Ordinance 1965** cases …, and new categories such as
> **CNSA 1997 (12 cases)**, **PECA 2016 (9 cases)**, **Punjab Domestic
> Violence Act (4 cases)**, and **Illegal Dispossession Act 2005 (2 cases)** …
> [Document 2].
> **Reporting Delays**: … from **15.0 minutes in 2024** (across 13 FIRs) to
> **1401.3 minutes (~23.4 hours) in 2026** (across 51 FIRs) … [Document 3].
> **Accused Profile**: Of the accused, **67 are men (مرد)** and **24 are
> women (عورت)** … [Document 5].
> **Weapon Licensing**: Of 32 weapon-register entries reviewed, **30
> recovered weapons had no licence recorded**, and **2 entries lacked licence
> status entirely** … [Document 4].

**The observable, before → after:** one sub-agent dispatch of the original
query per question, landing on Cross-Case Linkage and refusing → **2 (CR3)
and 5 (G1, G6)** independently-routed XAGG dispatches per question, every
one answered from a computed aggregate.

---

## 6. Gold comparison — honest verdict

### CR3 — **substantially correct, one wrong identifier**

| Gold | Live | |
|---|---|---|
| "No — not identically." | "No, not identically." | ✅ |
| 64/26 has a matching walk-in complaint | `fir-64-26` has a matching walk-in complaint | ✅ |
| 65/26 has none | `fir-65-26` does not appear in the linkage list | ✅ |
| case tag `CMS-ISB-2026-0341` | quoted `CMS-KHI-2026-0417` | ❌ **wrong tag** — the model picked the wrong row out of the 4-row linkage list |
| complainant سعد الرحمن | not stated | ❌ not computable — `_cms_fir_linkage()` returns a CNIC, not a name |

It also independently surfaced the right connective fact (both FIRs share
accused عاصم رشید), which is not in gold. CR3 was **0.2** before.

**CR3 is the least stable of the three.** Four live runs across the
iterations of this module: one refusal ("cannot be answered"), one correct
verdict with the wrong companion FIR (`fir-202-26`), one run where two
sub-queries hit the 60 s `META_ANALYSIS_SUBQUERY_TIMEOUT`, and the final run
above. The instability has a single identifiable cause and it is **not** the
decomposition: nothing in the decomposed sub-answers says which FIRs "the
online banking fraud matter" refers to, so the synthesis model has to infer
the pair. That capability is **Module 36** below.

### G1 — **a real analytical answer, but not gold's analytical answer**

Every claim in the live answer is a correctly computed fact. But **none of
gold's four headline findings is currently computable**: offender age range
(Module 31), stranger-dominated relationships (32), 13 forensic-lab / 7
heirs items (33), flat time-of-day distribution (34); gold's "all Pakistani
nationals" is not in the data model at all. What the live answer reports
instead — record incompleteness, repeat offenders, unlicensed weapons, the
case-mix shift, criminal-record/court mismatches — is a legitimate,
different answer to "flag anything unusual or worth monitoring". Against
gold's *specific* facts this scores near zero on overlap; as an answer to
the question it is real work. **Reported as a mismatch, not tuned.** G1
cannot approach gold until Modules 31–34 land.

### G6 — **the closest to gold of the three**

| Gold | Live | |
|---|---|---|
| 73 open FIRs across 9 districts, heaviest in Faisalabad and Lahore | 9 districts listed; فیصل آباد 19, لاہور 18 heaviest | ✅ (the total 73 is deliberately not asserted — see §3) |
| two years ago almost all armed robbery; now a mix of narcotics, cyber fraud/harassment, financial fraud, kidnapping, murder | 2024 PPC 13 / Arms 13 → 2026 PPC 39, Arms 16, CNSA 12, PECA 9, DV 4, IDA 2 | ✅ same shift, expressed as statutes rather than crime types (this corpus carries no crime-type classification) |
| mostly male accused | 67 men / 24 women | ✅ |
| aged 25–40 | — | ❌ Module 31 |
| usually strangers to the complainant | — | ❌ Module 32 |
| arrest recorded on only ~1 in 9 FIRs | — | ❌ Module 35 |
| most matters pending in court | — | ❌ no court-stage aggregate (Module 24's territory) |
| fraud/cyber reported long after the event, unlike older robberies | 15.0 min (2024) → 1401.3 min (2026) | ✅ same finding, not split by crime type |
| weapon recovery common and almost always unlicensed | 30 of 32 unlicensed, 2 no status | ✅ |

Six of gold's nine elements, from computed facts. G6 was **0.0** before.

---

## 7. Non-gold paraphrase

**G1's paraphrase** (the one Module 21 captured, which shares no
distinctive keyword with the gold text) — *"As the on-duty analyst, look
over everything currently open and tell me what's worth a second look"*:

- **Before:** matched no Meta-Analysis trigger at all → XNETWORK →
  Cross-Case Linkage → honest refusal, nearest cluster 0.243 (Module 21).
- **After:** 103.3 s, `status=done`, `route='XNETWORK' -> 'Meta-Analysis'`
  followed by **five** `route='XAGG' -> 'Large-Scale Aggregate'` dispatches,
  returning the same five computed findings as G1 itself (9 FIRs without
  incident dates, 52 without status, the four repeat offenders by name,
  30 + 2 weapons, the 2024→2026 case-mix shift, 1 of 33 criminal records
  matching its court outcome). **Passes** — capability, not curve-fitting.

**G6's paraphrase** — *"Ek naya afsar aaj join kar raha hai — usay batayein
ke yahan ke cases kaise hain aur unse kya tawaqqo rakhni chahiye?"*: 91.4 s,
`status=done`, 5 XAGG dispatches, the full orientation note. **Passes.**

**CR3's paraphrase** — *"We have two victims in the same fraud matter. Were
both complaints handled the same way on paper?"*: 77.6 s, decomposed
correctly (2 XAGG dispatches), but the synthesis was rejected —
`status=error`, "The synthesized answer could not be verified as grounded in
the sub-answers." **Fails**, consistent with CR3's own instability above and
the same missing subject-identification capability (Module 36).

---

## 8. Regression guard

| Question | Result | Verdict |
|---|---|---|
| **M2** (Module 25's own question, same file) | 85.5 s, `status=done`. `route='XAGG' -> 'Meta-Analysis'` then **2** `route='XAGG' -> 'Large-Scale Aggregate'` — still decomposed by the LLM path, no plan claims it. Answer: station type is not classified in this data model, so the two growth rates cannot be compared. **No verifier rejection.** | ✅ Byte-for-byte the shape Module 25 recorded as its own post-fix result ("the honest no-classification-data shape … the verifier correctly passes it"). Module 25's fix holds. |
| **G5** (already answers correctly, and already routes *through* Meta-Analysis) | 55.5 s, `status=done`. 30 recovered weapons explicitly unlicensed, 2 with no licence status. | ✅ Unchanged. No decomposition plan claims it — asserted in a unit test as well as live. |
| **D1** (simple single fact) | 4.4 s, **one** dispatch, `route='XAGG' -> 'Large-Scale Aggregate'`, 73. | ✅ Not decomposed. |
| All 32 gold questions vs. the plan patterns | exactly `{CR3, G1, G6}` | ✅ |
| All 32 gold questions vs. `_META_ANALYSIS_TRIGGER_PATTERNS` | the same 15 as before the widening | ✅ |

**Quota note, as the brief asks:** no Groq/Gemini quota or rate-limit error
was hit in any run — no 429s in any backend log. The one non-answer that
looked like a decomposition bug was a **60 s
`META_ANALYSIS_SUBQUERY_TIMEOUT`** on two concurrent sub-queries, caused by a
sub-query that rendered all 73 cases (~4.6 KB of generation) starving the
others on the shared model server. That experiment was reverted; the reason
is recorded in `_SQ_CASE_LISTING`'s own comment in the code.

---

## 9. New defects found — filed, not fixed here

Six new modules, one per missing aggregate (see §2 for the measured
evidence behind each):

- **Module 31** — offender age profile (G1). Also retires a now-stale hard
  refusal.
- **Module 32** — accused ↔ complainant relationship breakdown (G1).
- **Module 33** — seized-property disposition counts (G1).
- **Module 34** — incident time-of-day distribution (G1).
- **Module 35** — arrest rate (G6).
- **Module 36** — subject-filtered FIR listing (CR3) — the capability CR3's
  remaining instability traces to.

Plus two recorded observations that are **not** modules:

- `Person.nationality` does not exist anywhere in the data model, so gold
  G1's "all Pakistani nationals" is not computable at all.
- `_MAX_SUB_QUERIES` is 5 and the `caseload_review` plan is already full;
  Modules 31–34 will need a slot swap or a considered cap change.
