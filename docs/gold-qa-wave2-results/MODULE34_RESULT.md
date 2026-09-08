# Module 34 — G1: incident time-of-day distribution

**Question:** G1, element (4) of its gold answer — *"incident times are
fairly flat across the day with only a mild evening lean"*.
**Branch:** `feature/xagg-g1-caseload-profile-aggregates` (Modules 31–34).
**File:** `src/pipeline/xagg.py` (+ the harness wrapper and the two
orchestrator rendering sites).
**Measured:** 2026-09-08, live stack, port 8006, platform-admin, All Cases.

Modules 31–34 share one live G1 run and one set of infrastructure findings,
written up once in **`MODULE31_RESULT.md` §4.0, §4.2 and §8**.

---

## 1. Root cause

No aggregate existed for `Incident.incident_datetime` as a clock time.
Measured before any code was written (`scratchpad/dispatch.py`, 2026-09-08):

```
timeofday -> case_listing
```

The plan predicted this branch correctly: the sub-query ends *"…, across all
cases?"*, which contains the literal `_LIST_ALL_KEYWORDS` entry **"all
cases"**, so it returned the unfiltered **73-row corpus dump**.

**Data probe — and it changed the answer.** `incident_datetime` is Module
22's projection and is present on **64 of 73** Incidents, as the results
README records. Bucketing naively reproduces the plan's figures exactly:

| Band | Naive (all 64) |
|---|---|
| evening 18–24 | 19 |
| afternoon 12–18 | 16 |
| night 00–06 | **15** |
| morning 06–12 | 14 |

But the hour histogram is not flat, it is a spike:

```
{0: 14, 1: 1, 6: 1, 7: 1, 9: 6, 11: 6, 13: 1, 14: 7, 15: 2,
 17: 6, 18: 3, 19: 7, 20: 1, 21: 6, 22: 1, 23: 1}
```

**14 of the 64 record exactly 00:00:00.** A police FIR does not record a
fifth of its incidents at precisely midnight. That value is a **date-only**
timestamp — a date with no clock time, widened to a datetime by the
projection — and 14 of the 15 "night" incidents are it. The only genuine
overnight incident in the entire corpus is one at 01:00.

**Correction to the plan.** The plan's Module 34 section states *"9
incidents record 00:00:00 exactly"*. The live count is **14**; **9** is the
number of Incidents carrying **no** datetime at all. The plan's own
instruction — *"decide and document how those are treated before reporting a
'night' count"* — is what this module did, and the decision reverses one of
gold's two claims.

---

## 2. Change

| File | Change |
|---|---|
| `src/pipeline/xagg.py` | `_incident_time_of_day()` + `render_incident_time_of_day()`; `_TIME_OF_DAY_KEYWORDS` and `_TIME_OF_DAY_BANDS`; new dispatch branch. Reuses the existing `_parse_iso_datetime()`. |
| `src/pipeline/orchestrator.py` | renderer wired at both XAGG rendering sites. |
| `src/pipeline/harness/tools/xagg.py` | renderer wired at the third site; `"incident_time_of_day"` added to `AggregateKind`. |
| `tests/test_xagg.py` | 24 new tests, including the two chain-level guards covering all four modules. |

**The midnight decision, made before the numbers were reported.**
Exact-midnight rows are **excluded** from the distribution and reported as
their own `date_only_count`. The aggregate also returns
`naive_bucket_counts` — the same buckets *with* them — so the difference is
auditable and gold's reading is traceable to the rule that produced it
rather than silently contradicted. The renderer states both:

> 14 of those record exactly 00:00:00, which is a date with no clock time
> rather than a real midnight, and are excluded above. Counting them as
> overnight instead would put 15 in the night (00:00-05:59) band and make
> the day look evenly covered; on the recorded clock times it is not —
> overnight is close to empty.

Only **exact** midnight is treated as date-only; a real 00:30 incident stays
in the night band (`test_incident_time_of_day_keeps_a_real_00_30_incident`).

**Where the fix sits, and why.** The branch is placed **below** M7's
`_is_reporting_speed_comparison()` and Module 23's
`_is_weapon_statute_cooccurrence()` — both are about elapsed time and change
over time, not clock time — and **above** `_TIME_COMPARISON_KEYWORDS` (M1),
`_TREND_KEYWORDS` and `_LIST_ALL_KEYWORDS`.

`_TREND_KEYWORDS` matters as much as `_LIST_ALL_KEYWORDS` here: it contains
`"over time"`, and an hour-of-day question is a **distribution**, not a time
series, so it must not reach that refusal.
`test_m7_still_reaches_the_reporting_speed_aggregate_after_module_34`
asserts M7's literal gold text end to end.

Rows are de-duplicated per case so a FIR whose Incident is reachable twice
is not double-counted, and unparsable values are reported
(`unparsed_count`) rather than dropped.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" -m pytest tests/test_xagg.py
```

186 → **210 passed** at this module's commit; **212 passed** after the live
fixes. Wider run across ten suites: **665 passed, 1 xpassed**, no new
failures. Baseline at the branch point `c797e70` was **132 passed**. The
full `pytest -q` suite was deliberately not run.

**The regression pinned to the literal dispatched text**
(`test_g1_time_of_day_sub_query_no_longer_dumps_the_whole_corpus`):

```python
_G1_SQ_TIME_OF_DAY = (
    "How many cases record an incident time, and at what time of day do "
    "those incidents happen, across all cases?"
)
```

It asserts `kind == "incident_time_of_day"` **and** `kind != "case_listing"`
— the exact pre-fix result.

**Negative control over all 32 gold questions**
(`TestIncidentTimeOfDayBoundary::test_matches_no_gold_question_at_all`)
asserts `matched == []`, an equality, reading
`evaluation/Gold_QA_Dataset_Final32_With_Answers.json` and asserting it
exists and holds 32 items — never the untracked bare
`Gold_QA_Dataset_Final32.json` (PR #21).

**This family needed the most tuning to pass that control**, and both
collisions are pinned by their own test
(`test_the_two_urdu_substring_collisions_stay_excluded`):

- bare Urdu **"رات"** (night) is a substring of **"کراتا"**, which **CR6**
  uses (*"شکایت درج کراتا ہے"*);
- bare Urdu **"شام"** (evening) is a substring of **"شامل"**, which **KB5**
  uses (*"تشدد شامل ہو"*).

Only bound forms ("رات کے وقت", "شام کے وقت", "دن کے کس وقت") are matched.
M7, M1, A7 and "list of all cases" are asserted not to match.

**Two chain-level guards added here, covering all four modules:**

- `test_every_current_g1_plan_sub_query_still_reaches_its_own_aggregate` —
  the five sub-queries `caseload_review` emits **today**, pinned to their
  literal strings (copied from `meta_analysis.py`, which is deliberately not
  imported so drift fails here rather than live), each still reaching
  `case_completeness_scan` / `graph_recurrence` / `weapon_compliance_scan` /
  `time_bucketed_breakdown` / `criminal_record_court_crosscheck`. Four new
  keyword families were inserted into a first-match-wins chain; this is the
  proof none of them stole an existing dispatch.
- `test_the_four_new_keyword_families_are_mutually_exclusive_on_their_own_sub_queries`
  — no sub-query matches more than one new family, so no answer depends on
  chain order alone.

---

## 4. Live verification

Shared setup in `MODULE31_RESULT.md` §4.0.

**Sent** (the literal sub-query, through `/api/chat`):

> How many cases record an incident time, and at what time of day do those incidents happen, across all cases?

**Route events:** `['XAGG']` · **status** `done` · **27.2 s**

**`backend.log` proof:**

```
2026-09-08 08:01:28,345 [INFO] src.pipeline.xagg: XAGG incident_time_of_day:
64 of 73 incident(s) carry a datetime; 14 are date-only 00:00:00 and
excluded; 50 usable -> {'night (00:00-05:59)': 1, 'morning (06:00-11:59)': 14,
'afternoon (12:00-17:59)': 16, 'evening (18:00-23:59)': 19};
peak='evening (18:00-23:59)'
```

**Verbatim answer:**

> According to [Document 1], **64 of 73 incidents** record an incident date/time (coverage: 64/73). Of these, **14 cases** have a timestamp of exactly **00:00:00**, which represents a date with no clock time and is excluded from the time-of-day breakdown below.
>
> For the remaining **50 incidents** with usable clock times:
> - **Night** (00:00–05:59): **1 case** (~2%)
> - **Morning** (06:00–11:59): **14 cases** (~28%)
> - **Afternoon** (12:00–17:59): **16 cases** (~32%)
> - **Evening** (18:00–23:59): **19 cases** (~38%)
>
> The **busiest time band** is **evening** (18:00–23:59) with **19 cases**.
>
> Note: If the **14 cases with 00:00:00** were counted as "overnight," the night band would have **15 cases**, but the document explicitly excludes them from time-of-day analysis as they lack a real clock time.

Both readings and the rule that separates them survived generation intact.

**The shared G1 run:** see `MODULE31_RESULT.md` §4.2. G1 still emits its
original five sub-queries and this aggregate does **not** fire during it.

---

## 5. Gold comparison

Gold G1 element (4): *"incident times are fairly flat across the day with
only a mild evening lean, so there's no strong night-crime pattern to build
a patrol case around"*.

| Gold claim | Naive reading (all 64) | This module's reading (50 with a clock time) | Verdict |
|---|---|---|---|
| "a mild evening lean" | evening 19 of 64 (30%), the largest band | evening 19 of 50 (**38%**), the largest band | **match, and stronger than gold says** |
| "fairly flat across the day" | 19 / 16 / 15 / 14 — flat | 19 / 16 / 14 / **1** — flat by day, empty overnight | **does not hold** |
| "no strong night-crime pattern" | night 15 of 64 | night **1** of 50 | **match, for the opposite reason** |

**Honest verdict: gold's conclusion is right; its stated reason is an
artefact.** "Fairly flat across the day" only holds if 14 date-only
timestamps are read as real midnights. On the incidents that actually record
a clock time, morning / afternoon / evening are indeed fairly even (14 / 16 /
19) with a mild evening lean — but overnight is essentially empty, not flat.

Gold's operational conclusion — *no strong night-crime pattern to build a
patrol case around* — survives, and is in fact better supported by the
corrected reading than by gold's own.

The module publishes **both** readings rather than picking the one that
matches. Tuning here would have been effortless and invisible: simply not
checking `hour == 0 and minute == 0 and second == 0` would have reproduced
gold's four numbers exactly. The check, the exclusion, and the sentence
naming the alternative are each asserted by a test.

**G1's full verbatim answer** is in `MODULE31_RESULT.md` §5. It contains
none of gold's four findings, including this one, for the reason in §8.1
there.

---

## 6. Non-gold paraphrase

> How many cases note the hour a crime happened, and do most happen at night or during the day?

Shares no phrase with the dispatched sub-query. **26.1 s, `route='XAGG'`,
`status=done`**, and the log confirms `incident_time_of_day` ran with the
same 64 / 14 / 50 / evening-19 figures.

The answer was served as the **raw computed aggregate** — the synthesis
verifier rejected the natural-language paraphrase, which is the designed
behaviour for XAGG (machine-computed evidence is correct by construction, so
a paraphrase failure degrades to the figures rather than to an abstention).
All four bands, the peak, the 64/73 coverage line and the full midnight
caveat reached the user.

**Passes** as a capability check. Two further paraphrases (English "hourly
spread", Urdu "واقعات دن کے کس وقت زیادہ ہوتے ہیں؟") are asserted at the
predicate level in `TestIncidentTimeOfDayBoundary`.

---

## 7. Regression guard

M5 · M4 · G5 · G3 · CR7 · G6 were re-run live after all four modules landed.
Full table and verdicts in **`MODULE31_RESULT.md` §7**.

The one this module could most plausibly break is **M7** — the other
time-shaped question in this chain, and the aggregate whose branch sits
immediately above. M7 is not in the required regression list, so it is
covered at the unit level end to end
(`test_m7_still_reaches_the_reporting_speed_aggregate_after_module_34`,
pinned to M7's literal Roman-Urdu gold text), and live via **G6**, whose
`caseload_review`-sibling plan dispatches M7's reporting-speed sub-query:
G6 returned *"15.0 minutes in 2024 … 1401.3 minutes (~23.4 hours) in 2026"*,
unchanged from Module 22's recorded figures.

---

## 8. New defects found

Shared with Modules 31/32/33 and written up in **`MODULE31_RESULT.md` §8**:

- **§8.1** the four aggregates are not yet wired into G1's `caseload_review`
  plan — blocked on `meta_analysis.py` (another track) and the
  `_MAX_SUB_QUERIES = 5` cap. **The single reason G1's answer is unchanged.**
- **§8.2** `AggregateKind` is a hand-maintained `Literal`; a missing entry
  is a silent empty answer.
- **§8.3** `_XGRAPH_OVERRIDE_PATTERNS` swallows any sub-query ending "across
  all cases" — this module's first-draft wording was one of the three
  casualties.
- **§8.4** generation can silently re-filter a rendered aggregate.

Module-specific:

### 8.9 `incident_datetime` conflates "midnight" with "no time recorded"

14 of 73 Incidents carry a timestamp of exactly 00:00:00 because Module 22's
projection widens a date-only source value into a datetime with no marker
distinguishing it from a real midnight. Every consumer of that property has
to re-derive the distinction heuristically, as this module does. The clean
fix is at the projection layer — either a separate `incident_time_recorded`
flag, or leaving `incident_datetime` unset when the source has no clock time
and keeping the date on its own field. That is `structured_projection.py`,
not `xagg.py`. **Proposed as its own module.**

Note the practical consequence: the true "night crime" figure for this
corpus is **1**, not 15, and any future analysis that buckets
`incident_datetime` naively will over-report overnight crime by an order of
magnitude.

### 8.10 The 9 Incidents with no datetime are the same 9 G3 reports

G3's gold answer cites *"9 FIRs record no incident date"* and its live run
still returns 9. This module's coverage line (64 of 73) is the same 9 seen
from the other side. Consistent, and worth noting so a future reader does
not treat them as two independent findings.
