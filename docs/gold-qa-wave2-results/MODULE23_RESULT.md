# Module 23 — M5: weapon × statute co-occurrence join

**Branch:** `feature/xagg-weapon-statute-cooccurrence`
**Question:** M5 (Urdu) — *"ہتھیار عام طور پر کس نوعیت کے مقدمات میں سامنے
آتے ہیں، اور کیا 2024 کے مقابلے میں اب یہ نوعیت بدل گئی ہے؟"*
**Verified live:** 2026-09-08, backend on `127.0.0.1:8001`, platform-admin
(`admin@example.com`), All Cases, real Postgres/AGE + live model-server
tunnel (`/health` = 200).

---

## 1. Root cause

**The brief's hypothesis was correct, and is confirmed first-hand.**

`xagg.py`'s dispatch is an ordered chain of `_matches_any(query_lower, _X_KEYWORDS)`
checks, first match wins. M5 contains the literal substring **"کے مقابلے میں"**,
which is an entry in `_TIME_COMPARISON_KEYWORDS`, so M5 matched M1's branch
and was answered by `_statute_mix_by_year()`.

That aggregate is right for M1 and structurally incapable of answering M5:

- it has **no weapon dimension at all** — it counts statutes over *every*
  case in the corpus, weapon-bearing or not;
- it groups by **act**, not section, because its statute source is
  `cases.crime_category`, which `muhafiz_cases._crime_category()` builds as
  the comma-joined `act` list ("PPC, Arms Ordinance 1965") and which
  therefore cannot tell robbery (PPC 392) from murder (PPC 302) — the exact
  distinction M5's gold answer turns on.

I read the whole dispatch chain end to end to confirm the brief's claim that
**no existing aggregate joins weapons to their case's statute set**. It does
not exist: `_top_recurring_weapon_types()` has the weapon dimension but
neither year nor statute; `_weapon_recovery_rate_by_district()` has weapon ×
district; `_weapon_compliance_scan()` (G5) reads `Weapon.license_status`
only. The failure is a **missing primitive**, not a mis-tuned one.

**One thing the brief did not say, and which decides the whole module:**
section-level statutes *are* available in the graph. They are
`StructuredRecord{record_type: 'fir_section'}` nodes carrying `act` +
`section_code`, each with a `BELONGS_TO_CASE` edge — projected by
`structured_projection.py` from `psrms.fir_section`. Verified live against
`evidence_graph`:

```
MATCH (r:StructuredRecord)-[e]->(x) WHERE r.record_type='fir_section'
RETURN type(e), label(x), count(*)
  -> "APPEARS_IN"      "Document"  218
  -> "BELONGS_TO_CASE" "Case"      218
```

Without that node type, gold's "§13 / §392 / §9(c) / §302" granularity would
not have been derivable at all and this module would have had to report a
data-availability gap. It was, so it did not.

---

## 2. Change

| File | Change |
|---|---|
| `src/pipeline/xagg.py` | `_CASE_TYPE_TERMS`, `_CHANGE_OVER_TIME_TERMS`, `_is_weapon_statute_cooccurrence()`; `_WEAPONS_ACT_TOKENS`, `_statute_label()`, `_is_weapons_act()`, `_weapon_statute_cooccurrence_by_year()`, `render_weapon_statute_cooccurrence()`; one dispatch branch |
| `src/pipeline/harness/tools/xagg.py` | `"weapon_statute_cooccurrence"` added to the `AggregateKind` Literal + its render branch |
| `src/pipeline/orchestrator.py` | the same render branch at **both** legacy XAGG rendering sites |
| `tests/test_xagg.py` | 20 new tests |
| `tests/test_harness_tool_xagg.py` | 1 new test (the `literal_error` guard) |

**Where the dispatch check sits, and why.** Immediately **before**
`_TIME_COMPARISON_KEYWORDS` — the branch that was swallowing M5 — and
therefore also before `_TREND_KEYWORDS`' refusal, `_DISTRICT_KEYWORDS`
(Module 1c's district+weapon path) and the bare `_WEAPON_KEYWORDS` recurrence
branch, which are the three prior collision sites this chain's own comments
name. It sits **after** G5's weapon+compliance check and M7's
`_is_reporting_speed_comparison()`, both of which keep first claim on their
own shapes.

**The predicate is a three-signal AND** — weapon term **and**
case-type/statute term **and** change-over-time term — following the
precedent `_is_reporting_speed_comparison()` set in Module 22. Any narrower
combination collides with a neighbour this chain already serves: weapon alone
is the recurrence aggregate's, weapon+compliance is G5's, case-type+change is
M1's.

**Result shape.** Per incident year: `statutes` (what every weapon-bearing
case that year was charged under), `cooccurring` (among weapon-bearing cases
that also carry a *weapons-law* charge, which **other** statutes appear
alongside it), `weapon_types`, and the `pairs` weapon-type × statute
cross-tab. Year bucketing reuses `_statute_mix_by_year()`'s own
`Incident-[OCCURRED_ON]->Date` query verbatim so the two aggregates can never
disagree about which year a case is in; weapon names go through the existing
`_normalize_weapon_type()`.

The renderer's closing "what changed" line is a **set difference between the
first and last bucket's `cooccurring` keys**, not a narrative. A dedicated
test (`test_m5_renderer_does_not_claim_a_change_that_did_not_happen`) feeds it
two identical years and asserts it says *unchanged*.

**`router.py` was not touched.** M5 reaches XAGG on its own, confirmed by the
live `route=` capture below — so Track A/B independence with Module 28 holds.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
    -m pytest tests/test_xagg.py -q
  -> 105 passed, 1 skipped

PYTHONPATH=. ... -m pytest tests/test_harness_tool_xagg.py tests/test_orchestrator.py \
    tests/test_harness_agent_large_scale_aggregate.py tests/test_router.py -q
  -> 218 passed, 1 xpassed
```

(The one skip is pre-existing: `TestReportingSpeedComparisonBoundary::test_matches_m7_and_no_other_gold_question`
looks for `Gold_QA_Dataset_Final32.json` at the repo root, which is not
tracked in this checkout.)

The full suite was **not** run — it empties the real
`muhafiz_entity_descriptions` Chroma collection (568 rows).

**New tests (21):**

- `test_m5_gold_text_routes_to_the_weapon_statute_cooccurrence_aggregate` —
  **the regression pinned to M5's literal Urdu gold text**, asserting it no
  longer lands on `time_bucketed_breakdown`.
- `test_m5_aggregate_counts_statutes_per_year_for_weapon_cases_only` — a
  fixture mirroring the real corpus in miniature; also proves a case with
  statutes and a year but **no** Weapon never leaks in.
- `test_m5_cooccurrence_view_is_scoped_to_cases_carrying_a_weapons_law_charge`
- `test_m5_weapon_type_variants_fold_into_one_pairing` — ammunition-count
  suffix normalization.
- `test_m5_renderer_states_the_widening_it_actually_measured` /
  `test_m5_renderer_does_not_claim_a_change_that_did_not_happen`.
- `test_m5_jurisdiction_case_ids_narrow_every_one_of_the_three_reads` — a
  filter applied to two of three reads would silently over-count.
- `TestWeaponStatuteCooccurrenceBoundary` — the gold text, three non-gold
  paraphrases, six neighbouring shapes that must **not** match (M1, G5, bare
  weapon recurrence, both district+weapon paths, M7), and
  **`test_matches_m5_and_no_other_gold_question`**: the all-32 negative
  control, asserting the predicate matches **exactly `["M5"]`** across the
  whole gold set.
- `test_m1_still_reaches_the_statute_mix_aggregate_after_module_23` and
  `test_g5_still_reaches_the_weapon_compliance_scan_after_module_23` — the
  two negative controls, end to end rather than at the predicate.
- `test_weapon_statute_cooccurrence_renders` (harness) — guards the
  hand-maintained `AggregateKind` Literal, whose omission is the documented
  silent-`literal_error` crash class Modules 13 and 22 each hit.

---

## 4. Live verification

Infrastructure checked first, in the runbook's order: `muhafiz-postgres`
`Up (healthy)`; model-server tunnel `/health` → **200**; backend `/health` →
`{"status":"ok","vector_store_status":"ok","database_status":"ok","documents_in_store":7716}`.

**Question sent (M5's exact gold text):**

```
ہتھیار عام طور پر کس نوعیت کے مقدمات میں سامنے آتے ہیں، اور کیا 2024 کے مقابلے میں اب یہ نوعیت بدل گئی ہے؟
```

**Route event, from the raw SSE stream:**

```
data: {"step": "supervisor:dispatch", "detail": "Classified query as route='XAGG' -> sub-agent='Large-Scale Aggregate'"}
data: {"step": "supervisor:dispatch", "detail": "Large-Scale Aggregate completed with status=ok"}
```

The SSE stream only ever exposes `route='XAGG'` — it does not name which
aggregate inside XAGG ran, which is precisely what this module had to prove.
So the new aggregate logs one line, and the backend log for this request
carries it:

```
2026-09-08 02:37:39,264 [INFO] src.pipeline.xagg: XAGG weapon_statute_cooccurrence:
    32 weapon case(s), 2 year bucket(s), 0 with no resolvable incident date
```

That is the direct proof the question reached `_weapon_statute_cooccurrence_by_year()`
and **not** `_statute_mix_by_year()`. It is corroborated by the answer's own
vocabulary: section-level statutes (`§13`, `§392`, `§302`, `§9(c)`), weapon
types, and the weapons-law-charge framing are things `_statute_mix_by_year()`
cannot emit at all — it only ever produces bare act names.

**Verbatim answer (run 1):**

> According to [Document 1], recovered weapons are most commonly associated
> with specific legal charges. In **2024**, all 13 cases with recovered
> weapons were charged under **Arms Ordinance 1965 §13**, **PPC §34**, and
> **PPC §392**. The weapon type recovered was **30 بور پستول (13)**, and all
> cases carried a weapons-law charge alongside these sections.
>
> In **2026**, the 19 cases with recovered weapons involved a broader range
> of charges, including **Arms Ordinance 1965 §13 (16)**, **PPC §34 (10)**,
> **CNSA 1997 §9(c) (8)**, **PPC §302 (8)**, and **PPC §392 (8)**, among
> others. Weapon types recovered included **30 بور پستول (17)**, **خالی خول
> (فائرنگ کا شاہد)**, **جائے وقوعہ سے برآمد (1)**, and **عام لکڑی کی چھڑی،
> ایک عدد (1)**.
>
> A key change from **2024 to 2026** is the inclusion of **CNSA 1997 §9(c)**
> and **PPC §302** as charges associated with weapons-law cases, which were
> not present in 2024.

**Verbatim answer (run 2, after adding the log line and restarting):**

> In 2024, recovered weapons were exclusively linked to **Arms Ordinance 1965
> §13**, **PPC §34**, and **PPC §392** charges, with all 13 cases involving
> **30 بور پستول** (30-bore pistol) as the weapon type [Document 1].
>
> By 2026, the legal context expanded: **16 of 19 cases** with recovered
> weapons included charges under **CNSA 1997 §9(c)** and **PPC §302**, which
> were absent in 2024. Weapon types also diversified, including **30 بور
> پستول** (17 cases), **خالی خول** (empty casing, firearm evidence), **جائے
> وقوعہ سے برآمد** (1 case, "seized at the scene"), and **general wooden
> stick** (1 case) [Document 1].
>
> Thus, both the **legal charges** and **weapon types** have broadened since
> 2024.

**Underlying aggregate output** (the deterministic evidence chunk the answer
is generated from, captured by calling `_weapon_statute_cooccurrence_by_year()`
directly against the live graph):

```
32 case(s) recorded a recovered weapon. What those cases were charged under, by incident year:

**2024** — 13 case(s) with a recovered weapon:
  - Arms Ordinance 1965 §13: 13
  - PPC §34: 13
  - PPC §392: 13
  Weapon types recovered: 30 بور پستول (13).
  Of these, 13 carry a weapons-law charge; the sections it appears alongside: PPC §34 (13), PPC §392 (13).

**2026** — 19 case(s) with a recovered weapon:
  - Arms Ordinance 1965 §13: 16
  - PPC §34: 10
  - CNSA 1997 §9(c): 8
  - PPC §302: 8
  - PPC §392: 8
  - Illegal Dispossession Act 2005 §3: 2
  - PPC §148: 2
  - PPC §149: 2
  - PPC §342: 2
  - PPC §506: 2
  - PPC §328A: 1
  Weapon types recovered: 30 بور پستول (17), خالی خول (فائرنگ کا شاہد)، جائے وقوعہ سے برآمد (1), عام لکڑی کی چھڑی، ایک عدد (1).
  Of these, 16 carry a weapons-law charge; the sections it appears alongside: CNSA 1997 §9(c) (8), PPC §302 (8), PPC §34 (8), PPC §392 (8).

Change 2024 to 2026: the weapons charge now also appears with CNSA 1997 §9(c), PPC §302, which it did not in 2024.
```

---

## 5. Gold comparison

Gold: *"In 2024 Arms Ordinance §13 appears only alongside the robbery
sections (PPC 34 and 392, both 13/13). In 2026 it also appears with narcotics
(CNSA §9(c), 8 cases) and murder (PPC 302, 8 cases), alongside the same
robbery sections (8 each) — weapons are no longer confined to robbery."*

| Gold figure | Measured (co-occurrence view) | Verdict |
|---|---|---|
| 2024 — §13 with PPC 34 | 13 | ✅ exact |
| 2024 — §13 with PPC 392 | 13 | ✅ exact |
| 2024 — §13 with anything else | *(nothing else)* | ✅ exact |
| 2026 — §13 with CNSA §9(c) | 8 | ✅ exact |
| 2026 — §13 with PPC 302 | 8 | ✅ exact |
| 2026 — §13 with PPC 34 | 8 | ✅ exact |
| 2026 — §13 with PPC 392 | 8 | ✅ exact |
| Qualitative point (weapons no longer confined to robbery) | derived set difference names exactly CNSA §9(c) and PPC 302 | ✅ |

**Verdict: exact match, and not tuned for.** Nothing in the aggregate names a
year, a section number or a statute — `_is_weapons_act()` matches on the
*act* name so any section of the ordinance counts, and the change sentence is
a set difference over whatever buckets the data produces.

**One number in the answers is NOT a gold match, and is not meant to be.**
The `statutes` view for 2026 reports **PPC §34: 10**, where gold says 8. That
is a different quantity, deliberately: `statutes` counts *all* weapon-bearing
cases, `cooccurring` counts only those that also carry a weapons-law charge.
Two 2026 cases have a recovered weapon and PPC §34 but **no** Arms-Ordinance
section, so they are in one view and not the other. Both are reported, and
the renderer labels which is which. Gold's figure corresponds to the
`cooccurring` view, which matches it exactly.

Also worth stating plainly: **the raw data reproduces gold before any code of
mine touched it.** The same numbers came out of a hand-written Cypher probe
against `evidence_graph` (`m23_rows.txt`, 100 rows, 32 weapon cases) before
the aggregate was written; the aggregate then reproduced the probe.

---

## 6. Non-gold paraphrase

**Sent:** *"Are guns turning up in different types of cases than they used
to?"* — plain English, no Urdu, no statute vocabulary, and no keyword shared
with M5's literal phrasing.

`route='XAGG'`, and the backend log records a second
`XAGG weapon_statute_cooccurrence: 32 weapon case(s), 2 year bucket(s)` line
for this request. **Verbatim answer:**

> According to [Document 1], guns (specifically 30 بور پستول) are now
> associated with different legal charges and weapon types compared to 2024.
>
> In **2024**, all 13 cases with recovered weapons were charged under **Arms
> Ordinance 1965 §13**, **PPC §34**, and **PPC §392**, with no other charges
> or weapon types recorded.
>
> By **2026**, 19 cases with recovered weapons included additional charges
> such as **CNSA 1997 §9(c)** and **PPC §302**, which did not appear in 2024.
> Weapon types also expanded to include **خالی خول (فائرنگ کا شاہد)** and
> **جائے وقوعہ سے برآمد** (scene-of-crime recovery), alongside general items
> like **اک عدد (1)** and **عام لکڑی کی چھڑی**.
>
> This indicates that guns are now linked to a broader range of legal
> sections and weapon categories than in 2024.

**Pass.** Same aggregate, same numbers, from a question that shares no
phrasing with the gold string.

---

## 7. Regression guard

Both re-run live against the same backend, after the change.

**G5** (weapon-register compliance, currently 1.0 — shares the entire weapon
keyword space):

> Reviewing the 32 weapon-register entries reveals two compliance-related
> flags: (1) **30 of 32 recovered weapons (94%) are recorded without a
> licence** … (2) **2 entries carry no licence status at all** …

Unchanged, still `_weapon_compliance_scan()`, still matching gold's 30/32 and
94%. The backend-log counter for `weapon_statute_cooccurrence` did **not**
increment on this request — G5 never entered the new branch.

**M1** (year-over-year statute mix, fixed by Module 26 and merged):

> In **2024**, the primary cases were under **PPC** and **Arms Ordinance
> 1965**, each with **13 cases**. By **2026**, **PPC cases surged to 39**,
> while **Arms Ordinance 1965 cases decreased slightly to 16**. Additionally,
> new case categories emerged in 2026, including **CNSA 1997** (12), **PECA
> 2016** (9), **Punjab Domestic Violence Act** (4), **Illegal Dispossession
> Act 2005** (2).

Unchanged — act-level `_statute_mix_by_year()` output, and again the new
aggregate's log counter did not increment. Gold M1 cites Arms Ord ×16 and
CNSA §9(c) ×12 for 2026; both still present.

The keyword-level version of both controls is also pinned as a unit test, plus
the all-32 assertion that the predicate matches **only** M5.

---

## 8. New defects found (not fixed here)

1. **Generation layer splits Urdu weapon names on their internal commas.**
   `Weapon.canonical_name` values such as
   `"خالی خول (فائرنگ کا شاہد)، جائے وقوعہ سے برآمد"` are one weapon type;
   the LLM rendered them as **two** list items in both the M5 and paraphrase
   answers ("**خالی خول …**, **جائے وقوعہ سے برآمد (1)**"). The aggregate is
   correct — it reports 3 distinct weapon types in 2026, and the counts in
   parentheses are right — but the presented list looks like 4. Affects any
   answer that lists Urdu canonical names, not just M5. Not fixed here: the
   fix belongs in the generation/rendering layer, not in `xagg.py`.

2. **Run-2 phrasing overstated a subset as the whole.** Run 2's answer said
   *"16 of 19 cases … included charges under CNSA 1997 §9(c) and PPC §302"*.
   The correct reading is: 16 of 19 carry a weapons-law charge, and of those,
   8 co-occur with CNSA §9(c) and 8 with PPC §302. The evidence chunk states
   both numbers separately and run 1 rendered them correctly, so this is
   generation variance, not an aggregate error — but it is the kind of
   conflation a numeric verifier could catch and currently does not.

3. **`cases.crime_category` throws away `section_code`.**
   `muhafiz_cases._crime_category()` keeps only the `act` per `fir_section`
   row, so every Postgres-side statute aggregate (`_statute_mix_by_year()`,
   `_station_or_category_counts()`'s `counts_by_act`, the
   `_LEGAL_CODE_ACT_KEYWORDS` filter) is permanently act-level, while the
   graph holds the section. This module worked around it by reading the graph
   instead. Whether M1's own answer should also be section-level is a real
   question and a scope change — flagged, not taken.

---

**Files:** `src/pipeline/xagg.py`, `src/pipeline/harness/tools/xagg.py`,
`src/pipeline/orchestrator.py`, `tests/test_xagg.py`,
`tests/test_harness_tool_xagg.py`.
