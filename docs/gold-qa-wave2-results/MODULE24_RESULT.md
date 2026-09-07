# Module 24 — M4: statute × court-stage join

**Branch:** `feature/xagg-statute-court-stage-join`
**Question:** M4 (Urdu) — *"ایک طرف یہ دیکھیں کہ لوگوں پر کن دفعات میں مقدمے
بن رہے ہیں، اور دوسری طرف یہ کہ وہ مقدمے عدالت میں کہاں تک پہنچے — کیا دونوں
سے کیس لوڈ کی سنگینی کا ایک ہی اندازہ ہوتا ہے؟"*
**Verified live:** 2026-09-08, backend on `127.0.0.1:8004`, platform-admin
(`admin@example.com`), All Cases, real Postgres/AGE (`muhafiz-postgres`
`Up (healthy)`) + live model-server tunnel (`/health` = 200).

---

## 1. Root cause

**The brief's hypothesis was half right, and the half it got wrong is the
half that decides the module.** Both errors are recorded here rather than
coded around, per the orchestration doc §5.

### 1.1 What the brief said, and what is actually true

> *"After PR #8, M4 gets the statute half right (PPC 61, Arms Ordinance 29)
> but then claims no court-stage data exists."*

The **symptom** is exactly right — reproduced verbatim on the unmodified
`main` before any code of mine ran (raw SSE captured):

> From the data provided, the highest number of cases were registered under
> **PPC (Pakistan Penal Code)** with **61 cases**, followed by **Arms
> Ordinance 1965** with **29 cases** [Document 1]. … However, **no
> information is available about the procedural stages** … [Document 2].

Three things about it are **not** what the brief (or this wave's hand-off)
assumed:

1. **"PPC 61, Arms Ordinance 29" is ACT-level, not section-level.** They are
   `_station_or_category_counts()`'s `counts_by_act` over
   `cases.crime_category`. The hand-off prompt for this module described them
   as section-level; they are not, and gold's M4 answer names **no statute
   counts at all** (it says only *"ایف آئی آر کی دفعات پہلے ہی ظاہر کرتی ہیں
   کہ سنگین جرائم عام ہیں"* — the FIR sections already show serious crime is
   common). Nothing in this module was calibrated to 61/29.
2. **M4's route is not stable, and it is not XAGG.** Five live captures of
   M4's exact gold text: `route='XNETWORK'` on four, `route='XAGG'` on one —
   in every case the supervisor then hands it to **Meta-Analysis**, not
   straight to Large-Scale Aggregate. `_deterministic_route_override()`
   returns `None` for M4 (verified directly), so M4's route is decided by the
   LLM classifier every time. `router.py` was **not touched** (§6 of the
   brief); this is reported, not worked around.
3. **Meta-Analysis decomposes M4 into its two halves before XAGG ever sees
   it.** Captured from the backend log with a temporary probe on
   `run_aggregate()`'s incoming `query_text` (removed before commit), the two
   sub-queries are:
   - *"کن دفعات (قانونی شقوں) کے تحت سب سے زیادہ مقدمے درج کیے گئے ہیں…"* —
     the charging half.
   - *"مقدمات عدالت میں کس درجے تک پہنچے ہیں (مثال: درخواست، پیشی، فیصلہ،
     معطل) اور ان کی frequency کیا ہے؟"* — the court half.

   So the "no court-stage data exists" sentence is **sub-query 2's own
   answer**, produced by a dispatch chain that had nothing for it.

### 1.2 What M4 actually hit inside XAGG, and why

The brief implies M4 reached the statute/category default. It does not. Run
M4's gold text through `run_aggregate()` on unmodified `main` and it lands on
the **person-recurrence** branch: `_PERSON_KEYWORDS` contains the literal
entry `"لوگ"`, and M4's second word is `"لوگوں"` — `_matches_any()` is a
substring test, so `"لوگ"` matches. M4 was therefore answered by a ranked
list of repeat accused. Verified by direct predicate probe against every
keyword tuple in the module:

```
M4 matches _CASE_TYPE_TERMS ['مقدمے', 'دفعات']
M4 matches _PERSON_KEYWORDS ['لوگ']
_is_weapon_statute_cooccurrence -> False
_is_reporting_speed_comparison  -> False
```

**PR #8's fix is intact and confirmed:** M4 does *not* match
`_COURT_READINESS_KEYWORDS`, so it no longer hijacks G3's readiness scan.
That part of the brief needs no correction.

### 1.3 The real missing primitive

Nothing in `xagg.py` joined statutes to court stage, and nothing presented
the two halves *as a comparison*. Both halves' data existed:

| Half | Source | Live count |
|---|---|---|
| Charges | `StructuredRecord{record_type:'fir_section'}` → `BELONGS_TO_CASE` | 218 section entries over 73 cases, 36 distinct sections |
| Court stage | `StructuredRecord{record_type:'criminal_record'}` | 33 records: `Under trial` 30, `Convicted, on bail pending appeal` 1, `Sub judice, external prior case` 1, `Under trial, priority hearing requested` 1 |

Both derived from a hand-written Cypher probe **before** the aggregate was
written (Module 23's standard), and the aggregate then reproduced the probe.

---

## 2. Change

| File | Change |
|---|---|
| `src/pipeline/xagg.py` | `_COURT_TERMS`, `_CASE_PROGRESS_TERMS`, `_is_statute_court_stage_join()`; `_statute_court_stage_join()`, `_STATUTE_RENDER_LIMIT`, `render_statute_court_stage_join()`; one dispatch branch; one stale-comment fix on Module 23's branch |
| `src/pipeline/harness/tools/xagg.py` | `"statute_court_stage_join"` added to the `AggregateKind` Literal + its render branch |
| `src/pipeline/orchestrator.py` | the same render branch at **both** legacy XAGG rendering sites |
| `tests/test_xagg.py` | 26 new tests, **plus** a fix to Module 22's own all-32 control (see §3) |
| `tests/test_harness_tool_xagg.py` | 1 new test (the `literal_error` guard) |

### Which data source for the statutes, and why

**Section-level, from the graph** — `StructuredRecord{record_type:'fir_section'}`
(`act` + `section_code`), through Module 23's `_statute_label()`, **not**
`cases.crime_category`.

M4 asks about **دفعات** (sections). `muhafiz_cases._crime_category()` reduces
every case to a comma-joined **act** list (`"PPC, Arms Ordinance 1965"`),
discarding `section_code` — so the Postgres side can only ever say "PPC 61",
which cannot tell murder (PPC §302) from a bounced cheque (PPC §420). Gold's
whole first-half claim is that *serious* crime is common; the act view is
structurally incapable of supporting or refuting it, and the section view
shows PPC §302 ×10, §365-A ×6, §376 ×2, CNSA §9(c) ×12 alongside the volume
sections. Same source and same labeller as Module 23, so the two aggregates
cannot disagree about what a statute is called.

### Reuse of Module 14's reader — not a second query path

The court half calls **`_criminal_record_court_crosscheck()` as-is** and
renders it through **`render_criminal_record_crosscheck()`**, so the
settled/in-progress rule (`_conviction_is_settled()`), the status breakdown
and the CR7 cross-check wording can never drift from CR7's. Module 14's
reader needed **no extension** — it already returns `total_records`,
`settled_count`, `in_progress_count` and the raw `status_breakdown`, which is
exactly the 33 / 1 / 30 gold asks for. `_fir_key()` (CR7's own normalizer) is
reused for the record→case join.

### The join, and its honest coverage

A criminal record names its case as free text (`source_case_ref`, e.g.
`"FIR 891/24, PS Jhang Road Faisalabad"`). Only **4 of 33** records name any
FIR at all, and all 4 resolve to a case in this corpus. The result reports
that number rather than hiding it — the thinness is itself part of why the
two halves cannot be reconciled case by case.

### The agreement verdict is a derived majority test

```python
agree = bool(total_records) and settled * 2 >= total_records
```

Stated in the function's own comment so it is not mistaken for a threshold
tuned to this corpus: the two views agree only when **most** criminal records
have actually reached a verdict, because only then does a conviction count
describe the same caseload the section counts describe. Nothing in it names a
year, a section or an expected ratio, and a dedicated test
(`test_m4_agreement_verdict_flips_when_the_courts_have_caught_up`) feeds it a
decided court side and asserts the verdict flips to *Yes*.

### Dispatch placement, and why

Inserted **after** Module 23's M5 branch and **before**
`_TIME_COMPARISON_KEYWORDS`:

- **Below `_CRIMINAL_RECORD_KEYWORDS` (CR7) and `_COURT_READINESS_KEYWORDS`
  (G3)** — the two families this one shares court vocabulary with. Both keep
  first claim *structurally*, not by keyword luck. `_is_statute_court_stage_join()`
  additionally matches neither question's text (asserted against all 32 gold
  questions), but the ordering is what guarantees it.
- **Above `_TIME_COMPARISON_KEYWORDS` (M1), `_TREND_KEYWORDS`' refusal and —
  decisively — `_PERSON_KEYWORDS`**, which is what M4 actually hit before
  (§1.2).

The predicate is a **two-signal AND**: a court term **and** a
progression/stage term. The separating signal is *court progression* ("how
far", "what stage", "کہاں تک", "تک پہنچے", "conviction", "verdict"), never the
word "court" on its own — `_COURT_READINESS_KEYWORDS`' own comment records
the live collision where a bare Urdu `"عدالت"` hijacked M4 into G3's scan, and
matching on "court" alone here would simply run that collision backwards.

**`router.py` was not touched.**

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
    -m pytest tests/test_xagg.py -q
  -> 132 passed  (was 105 passed + 1 skipped before this module)

PYTHONPATH=. ... -m pytest tests/test_xagg.py tests/test_harness_tool_xagg.py \
    tests/test_orchestrator.py tests/test_harness_agent_large_scale_aggregate.py \
    tests/test_router.py
  -> 359 passed, 1 xpassed
```

The full suite was **not** run — it empties the real
`muhafiz_entity_descriptions` Chroma collection (568 rows).

**The pre-existing skip is now gone, deliberately.** Module 22's
`test_matches_m7_and_no_other_gold_question` looked for
`Gold_QA_Dataset_Final32.json` at the repo root, which **is not tracked in
this repo**, so it silently skipped and had therefore *never run* since it
shipped. It is re-pointed at `evaluation/Gold_QA_Dataset_Final32_With_Answers.json`
and **asserted** rather than skipped, so it can never go quiet again. It
passes (matches exactly `["M7"]`).

**New tests (27):**

- `test_m4_gold_text_reaches_the_statute_court_stage_join` — **the regression
  pinned to M4's literal Urdu gold text**, asserting it no longer lands on
  `graph_recurrence`.
- `test_m4_aggregate_reports_both_halves_and_an_agreement_verdict` — a
  fixture mirroring the real corpus in miniature; also proves a case charged
  twice under one section counts once.
- `test_m4_agreement_verdict_flips_when_the_courts_have_caught_up` — the
  same fixture with a decided court side; the verdict flips to *Yes*.
- `test_m4_joins_a_criminal_record_to_the_sections_of_its_own_case` — the
  `_fir_key()` join, including that a record naming no FIR is excluded rather
  than guessed at.
- `test_m4_renderer_states_both_halves_and_the_disagreement` — asserts CR7's
  own renderer produced half B (its signature sentence), so the two can never
  drift.
- `test_m4_renderer_caps_the_section_list_without_dropping_the_tail`.
- `test_m4_reports_an_empty_charging_side_instead_of_inventing_one`.
- `test_m4_jurisdiction_case_ids_narrow_the_section_and_case_reads` — the
  criminal-record read is deliberately corpus-wide (CR7's own contract), the
  two case-scoped reads must honour the allow-list.
- `TestStatuteCourtStageJoinBoundary` — the gold text, three non-gold
  paraphrases, five neighbouring shapes that must **not** match (G3's and
  CR7's literal gold text, M1, M5, a progression word with no court term),
  and **`test_matches_m4_and_no_other_gold_question`**: the all-32 negative
  control, asserting the predicate matches **exactly `["M4"]`**.
- `test_g3_still_reaches_the_court_readiness_scan_after_module_24`,
  `test_cr7_still_reaches_the_criminal_record_crosscheck_after_module_24`,
  `test_m5_still_reaches_the_cooccurrence_aggregate_after_module_24` — three
  negative controls end to end rather than at the predicate.
- `test_statute_court_stage_join_renders` (harness) — guards the
  hand-maintained `AggregateKind` Literal, the documented silent-`literal_error`
  crash class Modules 13, 22 and 23 each hit.

---

## 4. Live verification

**Question sent (M4's exact gold text):**

```
ایک طرف یہ دیکھیں کہ لوگوں پر کن دفعات میں مقدمے بن رہے ہیں، اور دوسری طرف یہ کہ وہ مقدمے عدالت میں کہاں تک پہنچے — کیا دونوں سے کیس لوڈ کی سنگینی کا ایک ہی اندازہ ہوتا ہے؟
```

**Route events, from the raw SSE stream** (five runs; the route is
LLM-decided and varies):

```
data: {"step":"supervisor:dispatch","detail":"Classified query as route='XNETWORK' -> sub-agent='Meta-Analysis'"}
data: {"step":"supervisor:dispatch","detail":"Classified query as route='XAGG' -> sub-agent='Large-Scale Aggregate'"}
data: {"step":"supervisor:dispatch","detail":"Classified query as route='XAGG' -> sub-agent='Large-Scale Aggregate'"}
```

The SSE never names which aggregate inside XAGG ran, so — following Module
23 — the new aggregate logs one line, and the backend log carries it on
**every** M4 request:

```
2026-09-08 03:28:14,095 [INFO] src.pipeline.xagg: XAGG statute_court_stage_join:
    73 case(s) carry a recorded section across 36 distinct statute(s);
    33 criminal record(s), 1 settled / 32 in progress;
    4 record(s) join to a case in this corpus; agree=False
```

That is the direct proof M4 reached `_statute_court_stage_join()`.

**Verbatim answer (the run that completed end to end):**

> The severity of the case load cannot be assessed equally through the
> frequency of legal sections and the stages of court proceedings, as these
> metrics reflect different dimensions of the caseload.
>
> First, the frequency of legal sections under which cases are registered
> highlights the **volume and nature of charges**. The Pakistan Penal Code
> (PPC) dominates with **61 cases**, followed by the Arms Ordinance 1965 with
> **29 cases** … [Document 1]. This data emphasizes the **types of crimes**
> driving the caseload but does not address how these cases are progressing
> through the judicial system.
>
> Second, the court proceedings' stages reveal a **significant backlog and
> low resolution rate**. Of 33 tracked cases, **32 remain under trial**
> (97%), with only **1 case reaching a verdict** (convicted but on bail
> pending appeal) [Document 2]. This underscores systemic delays and the
> **limited resolution of cases**, even though the number of charged cases
> (73) far exceeds the small number of convictions (1). The document
> explicitly states that the **severity of the caseload is better reflected
> by the initial FIR/section counts** rather than conviction rates, as the
> latter lag far behind.
>
> Thus, while the legal sections' frequency shows the **scope and focus of
> criminal activity**, the court stages highlight **procedural inefficiencies
> and unresolved cases**, making the two metrics complementary but not
> interchangeable in assessing caseload severity.

**Underlying aggregate output** — the deterministic evidence chunk, captured
by calling `_statute_court_stage_join()` directly against the live graph:

```
Two views of the same caseload, side by side: what people are being charged under, and how far those cases have got in court.

**1. What people are being charged under (FIR sections).**
  73 case(s) carry at least one FIR section - 218 section entries across 36 distinct statutes. Cases charged under each, most-charged first:
  - PPC §34: 40 case(s)
  - Arms Ordinance 1965 §13: 29 case(s)
  - PPC §392: 21 case(s)
  - CNSA 1997 §9(c): 12 case(s)
  - PPC §420: 11 case(s)
  - PPC §302: 10 case(s)
  - PPC §337-A(i): 10 case(s)
  - PECA 2016 §14: 9 case(s)
  - PECA 2016 §21: 9 case(s)
  - PPC §419: 9 case(s)
  - PPC §365-A: 6 case(s)
  - PPC §506: 6 case(s)
  - PPC §379: 5 case(s)
  - PPC §384: 4 case(s)
  - Provincial Act §Punjab Domestic Violence Act: 4 case(s)
  ... and 21 further section(s), each charged in 2 case(s) or fewer.

**2. How far those cases have got in court.**
  Of 33 criminal records, 32 are still in progress and 1 has reached a verdict. Breakdown by recorded status:
    - Under trial: 30
    - Convicted, on bail pending appeal: 1
    - Sub judice, external prior case: 1
    - Under trial, priority hearing requested: 1

  Where a case also has an independent court-outcome record, comparing the two:
    - FIR 891-24 (شہزیب عرف شابی): criminal record says "Convicted, on bail pending appeal"; court outcome says "5 سال قید بامشقت زیر دفعہ 392 ت.پ سزایاب" — consistent.

**3. The overlap between the two.**
  4 of 33 criminal records name an FIR that is also a case in this corpus, so only those can be read on both sides at once:
  - FIR 214-26 (شہزیب عرف شابی) - charged under Arms Ordinance 1965 §13, PPC §302, PPC §34, PPC §392; court stage: "Under trial".
  - FIR 64-26 (عاصم رشید) - charged under PECA 2016 §14, PECA 2016 §21, PPC §419, PPC §420; court stage: "Under trial".
  - FIR 77-26 (زاہد پرویز) - charged under PPC §302, PPC §34, PPC §364-A, PPC §376; court stage: "Under trial, priority hearing requested".
  - FIR 891-24 (شہزیب عرف شابی) - charged under Arms Ordinance 1965 §13, PPC §34, PPC §392; court stage: "Convicted, on bail pending appeal".

**Do the two agree? No.** Every one of the 73 charged case(s) is counted on the charging side, but only 1 of 33 criminal records (3%) has reached a verdict - the rest are still in progress. A conviction count therefore describes 3% of the recorded court caseload while the section counts describe all of it, so the two do NOT give the same impression of severity. Current caseload severity should be read off the FIR/section counts; the conviction counts lag behind them.
```

### The one thing this module does NOT fix, stated plainly

**M4 completed end to end on only 1 of 5 post-change runs.** The aggregate
ran on all five (log line above, every time). The other four failed **above**
XAGG, in Meta-Analysis:

- **3 runs** — `Meta-Analysis completed with status=abstained`, with the
  verifier log line
  `Meta-Analysis: verifier rejected synthesized answer: A claim is attributed
  to Document 1 but is absent from its text and instead appears in Document 2.`
  That is **exactly the defect Module 25 (PR #16, open) diagnosed and fixes**
  — each sub-answer arrives already carrying its own `[Document 1]`, which
  collides with the numbering the synthesis prompt assigns to the pseudo-chunks.
- **1 run** — both decomposed sub-queries timed out on the shared
  model-server tunnel.

Neither is in `xagg.py`, and fixing either would mean editing `router.py`,
`supervisor.py` or `meta_analysis.py` — all outside this module's scope and
two of them owned by concurrent tracks. Flagged in §8 as a new module rather
than silently widened into this one.

---

## 5. Gold comparison

Gold: *"نہیں — دونوں ایک دوسرے سے پیچھے ہیں۔ ایف آئی آر کی دفعات پہلے ہی ظاہر
کرتی ہیں کہ سنگین جرائم عام ہیں، لیکن 33 میں سے صرف 1 کرمنل ریکارڈ حتمی سزا
ہے؛ 30 اب بھی زیرِ سماعت ہیں۔ موجودہ کیس لوڈ کی سنگینی کا اندازہ ایف آئی
آر/دفعات کی گنتی پر لگانا چاہیے، سزاؤں کی گنتی پر نہیں، جو ابھی پیچھے ہے۔"*

| Gold figure / claim | Measured | Verdict |
|---|---|---|
| 33 criminal records | `total_records` = **33** | ✅ exact |
| only 1 is a final conviction | `settled_count` = **1** (`Convicted, on bail pending appeal`) | ✅ exact |
| 30 still under trial | `status_breakdown` → `Under trial`: **30** | ✅ exact |
| the two do **not** agree | derived majority test → `agree = False` | ✅ |
| serious crime is common in the FIR sections | PPC §302 ×10, §365-A ×6, §376 ×2, §364-A ×2, CNSA §9(c) ×12, Arms Ord §13 ×29, §392 ×21, out of 73 charged cases | ✅ supported, section-level |
| judge severity by FIR/section counts, not convictions | rendered verbatim as the closing verdict sentence | ✅ |

**Verdict: gold's 33 / 1 / 30 match exactly, and were not tuned for.** They
came out of a hand-written Cypher probe against `evidence_graph` before the
aggregate existed, and the aggregate then reproduced the probe. Nothing in
`_statute_court_stage_join()` names a status string, a count or a ratio; the
settled/in-progress split is `_conviction_is_settled()`'s existing rule,
untouched since Module 14.

**Two honest mismatches, reported rather than tuned away:**

1. **32 vs 30.** The aggregate says *32 in progress*; gold says *30 under
   trial*. Both are right and they are different quantities: two records read
   `Sub judice, external prior case` and `Under trial, priority hearing
   requested`, which are in progress but not literally the plain `Under
   trial` bucket. The renderer prints the raw status breakdown, so **30
   appears explicitly** alongside the 32. The live generation model then
   flattened it back to *"32 remain under trial"* — and the citation
   verifier **caught that**, appending: *"the source confirms that 32 cases
   are still in progress (97%) and 1 has reached a verdict, but it specifies
   that only 30 are 'under trial'…"*. Correct behaviour on both sides; the
   evidence chunk is precise, the paraphrase was not.
2. **The live answer's charging half is act-level (PPC 61 / Arms Ord 29), not
   the section-level list this aggregate produces.** That is a consequence of
   Meta-Analysis's decomposition, not of the aggregate: sub-query 1 (the
   charging half) still routes to `_station_or_category_counts()`, and only
   sub-query 2 reaches this module's aggregate — which supplies **both**
   halves as `[Document 2]`. The model preferred `[Document 1]`'s act numbers
   for the charging half. When M4's shape reaches XAGG **without**
   decomposition, the section-level list is what the answer uses — see the
   paraphrase in §6.

---

## 6. Non-gold paraphrase

Two paraphrases were tried live. Both results are reported.

**PASS — sent:** *"How many of our cases have actually reached a conviction
in court, and how many sections are people being charged under?"* — plain
English, no Urdu, no phrase shared with M4's gold string.

`route='XAGG' -> sub-agent='Large-Scale Aggregate'`, **no Meta-Analysis
decomposition**, and the backend log records another
`XAGG statute_court_stage_join: 73 case(s) …` line for this request.
**Verbatim answer:**

> According to [Document 1], **1 case** (out of 33 criminal records) has
> reached a conviction in court, specifically "Convicted, on bail pending
> appeal" (FIR 891-24 (شہزیب عرف شابی)).
>
> People are being charged under **36 distinct statutes**, with a total of
> **218 section entries** across **73 cases**. The most frequently charged
> sections include:
> - PPC §34 (40 cases)
> - Arms Ordinance 1965 §13 (29 cases)
> - PPC §392 (21 cases)
> - CNSA 1997 §9(c) (12 cases)
> - PPC §420 (11 cases)
> - PPC §302 (10 cases)
> - … and 21 further sections, each charged in 2 cases or fewer.
>
> The conviction count (1 case) reflects only 3% of the recorded court
> caseload, while the section counts describe the full scope of charges.

Both halves, section-level, and the comparison — from a question sharing no
phrasing with the gold string. This is the cleanest demonstration in this
report that the capability, not the string, is what was fixed.

**Two paraphrases that did NOT reach the aggregate, and why** (reported
because a silent omission here is the curve-fitting failure this check exists
to catch):

- *"Do the sections people are charged under and how far those cases have got
  in court give the same picture of how serious our caseload is?"* — matches
  `_is_statute_court_stage_join()` (unit-tested), but the **router** sent it
  `route='XNETWORK' -> Cross-Case Linkage`; it never reached XAGG.
- *"Across all cases, how far have they reached in court, and does that match
  the sections people are being charged under?"* — `_deterministic_route_override()`
  returns XAGG for this literal string, but the live run came back
  `route='XGRAPH'`, i.e. the **query rewriter** altered the text before the
  override saw it.

Both are `router.py`/rewriter behaviour, which this module is forbidden to
touch. They are the same class of problem as §1.1(2) and are folded into the
new module proposed in §8.

**The brief's own suggested paraphrase** — *"Do the charges we file and the
court outcomes tell the same story about how serious our caseload is?"* —
is deliberately **not** used: it contains `"court outcome"`, a literal
`_CRIMINAL_RECORD_KEYWORDS` entry, so it is claimed by CR7's branch, which is
checked earlier and (per §2 of the brief) must not move.

---

## 7. Regression guard

All re-run live against the same backend, after the change.

**G3** (court-readiness completeness, currently 1.0 — **the mandatory
control**; M4 and G3 share the entire court keyword space):

> عدالت کو حوالگی کے لیے کیس فائل تیار کرتے ہوئے … 1. **مُتَّہَم ↔ شکایت کنندہ
> کے تعلق** کا ذکر 94 میں سے 82 مُتَّہَم … میں غائب ہے … 2. … **32 بحال ہونے
> والے ہتھیاروں** کے ریکارڈ میں [2 کی] ہتھیار کی لائسنس کی حیثیت درج نہیں ہے
> … 3. **9 FIRs** میں واقعہ کی تاریخ درج نہیں ہے …

Unchanged — still `_court_readiness_scan()`, still all three of gold's
findings (accused↔complainant relationship, 2 of 32 weapons with no licence
status, 9 FIRs with no incident date). The `statute_court_stage_join` log
counter did **not** increment on this request: G3 never entered the new
branch. (The `82` vs gold's `81` is pre-existing generation variance in the
same aggregate, unrelated to this module.)

**CR7** (criminal-record × court-outcome cross-check — Module 14's own
question, whose reader this module calls):

> In the criminal record system, **1 case has been marked as completed**
> (specifically, "Convicted, on bail pending appeal") and **32 cases remain
> under investigation or in procedural stages** [Document 1]. Of these, 30
> are under trial, 1 has a priority hearing requested, and 1 is sub judice
> with an external prior case.
>
> Regarding consistency … **only 1 case (FIR 891-24, شہزیب عرف شابی)** has a
> corresponding court outcome record. The criminal record … and the court
> outcome ("5 سال قید بامشقت زیر دفعہ 392 ت.پ سزایاب") are stated to be
> **consistent** [Document 2].

Unchanged — still `_criminal_record_court_crosscheck()`, still matching
gold's 33 / 30 / 1 and the FIR 891-24 consistency finding. New-branch counter
did **not** increment. (A first CR7 run had two of its three decomposed
sub-queries time out on the model-server tunnel; the re-run above is
complete. The timeout is infrastructure latency, not a routing change — the
route events were identical in both runs.)

**M5** (Module 23's question, whose dispatch branch sits immediately above
this module's):

> In 2024, recovered weapons were exclusively linked to **Arms Ordinance 1965
> §13**, **PPC §34**, and **PPC §392** charges, with all 13 cases involving
> **30 بور پستول** … By 2026 … **CNSA 1997 §9(c)** and **PPC §302**, which
> were absent in 2024 …

Unchanged, byte-for-byte the same finding Module 23 recorded, and the backend
log shows `XAGG weapon_statute_cooccurrence` (not the new aggregate) for this
request.

The keyword-level versions of the G3, CR7 and M5 controls are also pinned as
unit tests, plus the all-32 assertion that the predicate matches **only** M4.

---

## 8. New defects found (not fixed here)

1. **M4 never reaches XAGG as a whole question — Meta-Analysis always splits
   it first, and the synthesis then fails 3 runs in 5.** The two failures
   are (a) Module 25's `[Document N]` collision, which PR #16 is open to fix,
   and (b) sub-query timeouts on the shared model server. Module 26 added a
   supervisor guard for exactly this shape (*"XAGG already answers this in
   one call, do not decompose"*) but gated it on `route == "XAGG"`, and M4's
   LLM-decided route is `XNETWORK` on 4 of 5 runs. **Proposed follow-on:** a
   deterministic `_XAGG_OVERRIDE_PATTERNS` entry for the statute×court-stage
   shape plus an extension of Module 26's supervisor guard to cover it.
   Belongs in `router.py`/`supervisor.py` — out of this module's scope, and
   both files are owned by other tracks this wave.
2. **The query rewriter can defeat `_deterministic_route_override()`.**
   `"Across all cases, how far have they reached in court…"` matches an XAGG
   override pattern as typed, but routed `XGRAPH` live, because the override
   runs on the **rewritten** query. This is a general routing-reliability
   hole, not specific to M4.
3. **`_PERSON_KEYWORDS` matches `"لوگ"` as a bare substring**, so *any* Urdu
   question containing `لوگوں`/`لوگو` is a candidate for the
   person-recurrence branch unless an earlier branch claims it first. M4 was
   one; there may be others in future question sets. Not widened here — the
   fix (word-boundary matching for short Urdu tokens) would touch
   `_matches_any()` itself and every keyword family in the file.
4. **`_statute_label()` renders one real row as
   `"Provincial Act §Punjab Domestic Violence Act"`** — the source
   `fir_section` row carries `act="Provincial Act"` and
   `section_code="Punjab Domestic Violence Act"`, i.e. an act name in the
   section field. Pre-existing data shape, inherited from Module 23's
   labeller; cosmetic, and left alone rather than special-cased.
5. **`cases.crime_category` still throws away `section_code`** (Module 23's
   finding #3, re-confirmed here from the other direction: M4's act-level
   "PPC 61" answer is that limitation in production). Whether the
   Postgres-side aggregates should become section-level remains a real
   question and a scope change.

---

**Files:** `src/pipeline/xagg.py`, `src/pipeline/harness/tools/xagg.py`,
`src/pipeline/orchestrator.py`, `tests/test_xagg.py`,
`tests/test_harness_tool_xagg.py`.
