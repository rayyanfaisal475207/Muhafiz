# Module 36 — CR3: no subject-filtered FIR listing

**Question:** CR3 — *"In the online banking fraud matter involving two
separate victims, was each victim's case processed and recorded the same
way?"*
**Branch:** `feature/xagg-arrest-rate-and-fir-listing`.
**File:** `src/pipeline/xagg.py` (+ the harness wrapper and the two
orchestrator rendering sites).
**Measured:** 2026-09-08, live stack, port **8010**, platform-admin, All
Cases, `admin@example.com`.

---

## 1. Root cause

**Confirmed as the plan described it, and the capability gap is exact.**
After Module 29 CR3 decomposes correctly and both sub-answers carry the facts
gold needs, but **nothing in either says which FIRs the question is about**,
so the synthesis model has to infer the pair — across Module 29's runs it
sometimes refused, and once paired `fir-64-26` with the wrong FIR entirely.

No aggregate returned FIR numbers filtered by statute, station or crime type:

* `_station_or_category_counts()` returns **counts only**;
* `_filtered_cases()` can filter by act, but the `case_listing` branch that
  returns per-FIR rows is deliberately guarded **against** act keywords;
* the only unfiltered listing is the **whole 73-row corpus**, which Module 29
  tried as a `record_consistency` sub-query and **reverted** — ~4.6 KB of
  generation starved its two concurrent siblings into the 60 s
  `META_ANALYSIS_SUBQUERY_TIMEOUT`.

Measured before any code was written (`scratchpad/dispatch.py`, live stack,
2026-09-08): the intended sub-query returned **`relational_aggregate` grouped
by `police_station`** — nine PECA cases counted per station, with **no FIR
number anywhere in the result** and **the station filter never applied at
all**.

**Ground-truth probe, hand-written, before the aggregate existed**
(`scratchpad/probe36b.py`, `DirectGateway` over live Postgres):

```
cases: 73
PECA 2016: 9   ·   سائبر کرائم سرکل (Islamabad 5 + Rawalpindi 4): 9
investigation_status non-empty: 21 of 73
```

| | FIR | Station | Acts | Status |
|---|---|---|---|---|
| ∩ | `fir-64-26` | 64/26 | سائبر کرائم سرکل، اسلام آباد | PECA 2016, PPC | `ملزم جسمانی ریمانڈ پر، مزید برآمدگی کے لیے` |
| ∩ | `fir-65-26` | 65/26 | سائبر کرائم سرکل، راولپنڈی | PECA 2016, PPC | `ملزم جسمانی ریمانڈ پر، مقدمہ نمبر 10 کا وہی ملزم` |

PECA 2016 ∩ سائبر کرائم سرکل = **exactly those two**, matching the plan's
stated ground truth and S3's gold answer (عاصم رشید appears in 64/26 and
65/26).

---

## 2. Change

| File | Change |
|---|---|
| `src/pipeline/xagg.py` | `_filtered_fir_listing()` + `render_filtered_fir_listing()`; `_FIR_IDENTIFICATION_KEYWORDS`, `_is_filtered_fir_listing()`, `_STATION_ALIASES`, `_STATION_NAME_HINTS`, `_station_segments()`, `_station_matches()`, `_mask_station_aliases()`, `_UNRECOGNIZED_STATION`, `_FIR_LISTING_RENDER_LIMIT`; new dispatch branch. |
| `src/pipeline/orchestrator.py` | renderer wired at both XAGG rendering sites. |
| `src/pipeline/harness/tools/xagg.py` | wired at the third site; `"filtered_fir_listing"` added to `AggregateKind`. |
| `tests/test_xagg.py` | 27 new tests. |

**Bounded by construction, for the measured reason.** Statute/status/crime-type
filtering is delegated to `_filtered_cases()` unchanged, so this family cannot
drift from the counting path beside it. The station filter is this function's
own and is **data-driven** — matched against the `police_station` values the
corpus actually holds, never a hardcoded station list. And the family

* **refuses to list anything when no filter was recognised** — it reports the
  corpus size and the filters available instead of dumping 73 rows, and
* caps rendered rows at `_FIR_LISTING_RENDER_LIMIT = 15` (~1 KB), comfortably
  inside the sub-query budget Module 29's experiment blew.

**Dispatch placement**, in both directions: **below** every subject-specific
family (G5's weapon+compliance scan scores 1.0 today and keeps first claim on
a question carrying both vocabularies) and below `_STATION_TOTAL_KEYWORDS`;
**above** `_DISTRICT_KEYWORDS`, `_LIST_ALL_KEYWORDS`, `_TOTAL_KEYWORDS` and
the `_station_or_category_counts()` fallback that this sub-query used to hit.

### 2.1 Two defects found in review of the in-progress implementation

The Module 36 code existed uncommitted in the worktree when this session
resumed. It was **kept and revised**, not discarded. Two live-reproduced
defects were fixed before it was committed:

**(a) A statute/station substring collision — the fifth of its kind in this
module.** PECA 2016's keyword tuple contains `"cyber crime"`; the station
alias is `"cyber crime circle"`. So *"Which FIR numbers are registered at a
cyber crime circle station?"* — which names **no statute** — silently
acquired a `statute: PECA 2016` filter and answered **2** FIRs where the
corpus holds **9** cyber-circle FIRs. Same class as the four Urdu collisions
already paid for (`تعلق`/`متعلق`, `رات`/`کراتا`, `شام`/`شامل`, `لوگ`/`لوگوں`).

Fixed by `_mask_station_aliases()`: station-alias phrases are blanked out of
the text used for **statute** matching (and passed on to `_filtered_cases()`
in that masked form, so label and rows can never disagree), while the station
half still matches the original text. CR3's own sub-query survives — it says
"under the **cybercrime** act at a **cyber crime circle** station", so the
one-word form remains after the phrase is removed.

**(b) An Urdu question naming the station by its real name did not match.**
Station names are Urdu-only in the data, and **neither of the two families
that matter carries `تھانہ` at all** (`سائبر کرائم سرکل، اسلام آباد`,
`موٹروے پولیس اسٹیشن ایم ٹو، لاہور`). `_STATION_KEYWORDS`
(`station`/`thana`/`تھانہ`/`چوکی`) therefore saw no filter signal in
*کون سی ایف آئی آر سائبر کرائم سرکل میں درج ہیں؟* and it fell through to the
grouped-count default. Fixed with `_STATION_NAME_HINTS` — a **signal only**;
the filter itself stays data-driven, so a hint naming no real station still
yields `_UNRECOGNIZED_STATION` rather than a wrong listing.

---

## 3. Scope boundary — wiring deliberately deferred

The plan's Module 36 section says to verify by adding the sub-query to
`meta_analysis.py`'s `record_consistency` plan. **That was not done, and
deliberately: Module 41 is editing that dispatch path concurrently in another
track, and `meta_analysis.py` / `supervisor.py` were out of scope for this
one.** `router.py`, `xnetwork.py` and `prompts/evaluator.txt` were likewise
not touched.

Instead — the same approach Modules 31–34 took — **each aggregate was
verified standalone** by sending its intended sub-query text directly through
`/api/chat`, and **the literal sub-query strings are pinned in tests** so a
later consolidation module can wire them in unchanged:

```python
_CR3_SQ_FIR_LISTING = (
    "How many cases are registered under the cybercrime act at a cyber "
    "crime circle station, and what are their FIR numbers and current status?"
)
_G6_SQ_ARREST_RATE = (            # Module 35
    "How many cases record an arrest of an accused person, and on how many "
    "is no arrest recorded, across all cases?"
)
```

Both lead with **"How many cases …"** on purpose:
`_XGRAPH_OVERRIDE_PATTERNS`' `\bacross\b.{0,15}\bcases\b` would otherwise
steal them, and
`test_each_new_g1_sub_query_deterministically_routes_to_xagg` pins
`router._deterministic_route_override(...) == XAGG` for both.

**Consequence, stated plainly:** CR3's own end-to-end instability is **not
fixed by this PR**. The capability it needed now exists and is live-correct;
connecting it to CR3's decomposition is the deferred step.

---

## 4. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_xagg.py -q
```

`tests/test_xagg.py`: **248 → 275 passed**, exit 0, no failures.
Ten-suite wider run: **767 passed, 1 xpassed, 0 failed.**

Highlights:

* `test_cr3_sub_query_returns_exactly_the_two_ground_truth_firs` — the
  regression pinned to the **literal** sub-query text, asserting
  `["64/26", "65/26"]` against a fixture built from the real corpus rows
  (real Urdu station strings, not sanitised stand-ins).
* `test_a_station_only_question_is_not_silently_narrowed_to_peca` — pins
  defect (a) above at 9 FIRs and an empty statute filter.
* `test_urdu_question_naming_the_circle_by_its_real_name_is_answered` — pins
  defect (b).
* `test_a_recognised_station_word_naming_no_real_station_refuses_to_dump` —
  pins the Module 29 budget rule: no filter resolved ⇒ no rows listed.
* `test_the_rendered_listing_is_capped_for_the_module_29_budget` — 23 matches
  render 15 rows plus a "+8 further" line.
* `TestFilteredFirListingBoundary::test_matches_no_gold_question_at_all` —
  **the all-32 negative control, an equality**: `matched == []`, with
  `len(items) == 32` asserted.
* `test_the_filter_half_alone_would_have_matched_five_gold_questions` —
  asserts the naive version **fails** that control
  (`== ["CP1", "CR3", "CR8", "M2", "S2"]`), so the control is meaningful.
* `test_s2_the_station_ranking_gold_question_is_not_captured` and
  `test_s2_still_reaches_the_station_ranking_after_module_36` — S2 (*"Which
  police station handles the most cases?"*) is the gold question this family
  is most at risk of swallowing; asserted at both the predicate level and end
  to end with its literal gold text.
* `test_every_new_aggregate_kind_is_accepted_by_the_harness_tool_result` —
  extended with `"filtered_fir_listing"`, the sixth family to depend on that
  hand-maintained `Literal`.

---

## 5. Live verification

Backend on **8010**, `/health` ok, model server `/health` 200,
`grep -c "rate limit" backend.log` = **0** throughout.

### 5.1 The dispatched sub-query

**Q:** *How many cases are registered under the cybercrime act at a cyber
crime circle station, and what are their FIR numbers and current status?*

```
route='XAGG'   sub-agent='Large-Scale Aggregate'   status=done   10.9s
```

`backend.log` (Urdu escaped — see §8):

```
[INFO] src.pipeline.xagg: XAGG filtered_fir_listing:
filters=['statute: PECA 2016', 'station: \u0633\u0627\u0626\u0628\u0631 …']
-> 2 FIR(s) ['64/26', '65/26']
```

**Verbatim answer:**

> According to [Document 1], **2 cases** are registered under the **PECA
> 2016** statute at **سائبر کرائم سرکل، اسلام آباد** and **سائبر کرائم سرکل،
> راولپنڈی**. Their details are as follows:
>
> 1. **FIR 64/26** | Case ID: **fir-64-26** | Station: **سائبر کرائم سرکل،
>    اسلام آباد** | Status: **ملزم جسمانی ریمانڈ پر، مزید برآمدگی کے لیے**
> 2. **FIR 65/26** | Case ID: **fir-65-26** | Station: **سائبر کرائم سرکل،
>    راولپنڈی** | Status: **ملزم جسمانی ریمانڈ پر، مقدمہ نمبر 10 کا وہی ملزم**
>
> No additional cases or details are reported in the source.

### 5.2 The collision regression, live

**Q:** *Which FIR numbers are registered at a cyber crime circle station?*
— `route='XAGG'`, `sub-agent='Large-Scale Aggregate'`, 175.8 s.
Log: `filters=['station: …'] -> 9 FIR(s) ['1001/26', '204/26', '410/26',
'421/26', '424/26', '435/26', '457/26', '64/26', '65/26']`. **No statute
filter**, all nine cyber-circle FIRs returned — the pre-fix behaviour was 2.

---

## 6. Gold comparison

| | Ground truth | Measured |
|---|---|---|
| PECA 2016 at a سائبر کرائم سرکل station | `fir-64-26`, `fir-65-26` | `fir-64-26` (64/26), `fir-65-26` (65/26) — **exact** |
| their statuses | both `ملزم جسمانی ریمانڈ پر…` | both returned verbatim |
| all cyber-circle FIRs | 9 | 9 |
| all PECA 2016 FIRs | 9 | 9 |
| all Arms Ordinance 1965 FIRs | 29 | 29 |

**Verdict: exact match on every filter probed.** CR3's *gold answer* itself —
that 64/26 has a matching walk-in complaint (`CMS-ISB-2026-0341`, complainant
سعد الرحمن) and 65/26 has none — is **not** what this aggregate answers; it
answers the record-identification half that Module 29 found missing. Because
the wiring is deferred (§3), CR3's end-to-end score is unchanged by this PR.
That is reported, not glossed.

---

## 7. Non-gold paraphrases

Three, none sharing a phrase with the dispatched sub-query. All
`route='XAGG'`, `sub-agent='Large-Scale Aggregate'`.

1. ***Which FIRs are registered under the Arms Ordinance, and what are their
   FIR numbers and status?*** — 39.5 s.
   Log: `filters=['statute: Arms Ordinance 1965'] -> 29 FIR(s)`. The renderer
   listed 15 rows, `(+14 further matching FIR(s), not listed here)`, and
   *"26 of these 29 FIR(s) record no investigation status at all."*
   The generation layer declined to paraphrase it (*"The natural-language
   summary could not be verified as an accurate paraphrase; showing the raw
   computed aggregate instead"*) and showed the aggregate verbatim — which is
   the correct fallback, and the numbers were right.
2. ***کون سی ایف آئی آر سائبر کرائم سرکل میں درج ہیں؟*** — 23.9 s, 9 FIRs,
   all nine listed with station, acts and status; *"Of these, 7 FIRs have no
   investigation status recorded at all."* Correct.
3. ***Which FIR numbers are registered at a cyber crime circle station?*** —
   §5.2.

---

## 8. Regression guard

**The full table — all ten re-run questions with routes, sub-agents and the
`XAGG …` log lines — is in `MODULE35_RESULT.md` §8.** The two modules share
one branch and one live batch, so it is recorded once rather than duplicated.

Headline, for this module specifically:

* **S2** (*"Which police station handles the most cases?"*) — the gold
  question this family was most at risk of swallowing — still answers
  **"تھانہ ماڈل ٹاؤن، لاہور with 7 cases"**, matching gold exactly, via the
  grouped station ranking. Asserted live **and** end to end in unit tests.
* **M5, G3, CR7, G6** ✅; **M31–M34** all still fire their own aggregates ✅.
* **M4** ❌ and **G5** ❌ are pre-existing/known-elsewhere failures —
  M4's documented instability (route was RAG, never reached XAGG) and the
  Module 41 Meta-Analysis over-decomposition regression respectively.
* **CR3** ❌ — *"The synthesized answer could not be verified as grounded in
  the sub-answers."* **Unchanged**, because the wiring is deferred (§3). This
  PR builds the capability CR3 needs; it does not yet connect it.

Unit-level guards shipping with this module: S2 end to end, the all-32
equality control, the naive-version control, and the ten-suite wider run.

---

## 9. New defects found (tracked, not fixed here)

1. **Urdu log lines are lost on Windows** — `backend.log` is written through a
   **cp1252** stream, so a `logger.info` whose formatted output contains Urdu
   raises `UnicodeEncodeError` inside `logging.StreamHandler.emit()` and the
   handler prints `--- Logging error ---` plus the **unformatted template**.
   Three of this module's first four live calls logged
   `filters=%s -> %d FIR(s) %s` verbatim and the figures were simply lost.
   This module's own line was made ASCII-safe with `ascii()`; **Module 32's
   `XAGG accused_relationship_breakdown` is still affected and was left
   alone.** The general fix — a UTF-8 stream handler in the logging config —
   deserves its own module, because this log line is the wave's only proof of
   which aggregate ran.
2. **CR3's wiring is outstanding** (§3) — the sub-query string is pinned in
   `tests/test_xagg.py` as `_CR3_SQ_FIR_LISTING` for whichever module
   consolidates the `record_consistency` plan after Module 41 lands.
3. **`_filtered_cases()`'s return annotation is stale** — declared
   `-> list[dict]`, actually returns `tuple[list[dict], list[str]]`.
   Pre-existing; not changed here.
