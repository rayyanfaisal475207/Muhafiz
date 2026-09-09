# Module 58 — `_STATION_TYPE_KEYWORDS` should be a predicate, not a 58-entry list

**Found by:** Module 56, which is the module that made it 58 entries ·
**Branch:** `fix/m4-routing-and-station-predicate` · **Date:** 2026-09-09 ·
**Backend:** `127.0.0.1:8022`

---

## 1. Root cause

Module 56 widened M2's trigger vocabulary from 10 entries to 58 and proved,
with an all-32 equality control, that nothing else moved. **That control was
the only thing protecting the widening, and it is exactly as broad as those 32
questions.**

The soft spot was visible in the tuple itself. Two of the 58 entries —

```python
"single type of crime", "one type of crime",
```

— **name no station at all.** They are there because M2's gold is phrased that
way (*"the handful set up for one specific type of crime"*), and none of the
32 collides with them.

Reproduced on the pre-fix tree, `resolve_aggregate_kind()`:

| Query | Resolved to |
|---|---|
| *"How many cases involve one type of crime only?"* | `station_caseload_by_specialisation` ❌ |
| *"How many FIRs cover a single type of crime?"* | `station_caseload_by_specialisation` ❌ |

Both are plain count questions. The family they were captured into is checked
**second in the whole chain**, above every entity family, so a false positive
there silently replaces a working answer — and **no test in the repository
would have noticed**, because the only guard was the 32.

The brief's diagnosis was correct as stated and required no revision. Two
things it did not name were found while building the replacement, and both
changed the design:

- **A naive `station-word AND qualifier-word` AND is not safe.** KB9 —
  *"…to **police** ko maut ki wajah ki baaqaida tehqeeqaat karni hoti hai —
  **khaas** tor par jab hamare itne cases mein maut shamil hai?"* — satisfies
  it, and would be hijacked out of `graph_recurrence_person`.
- The same is true in English: *"Which station has the most **ordinary** theft
  cases?"* carries a station word and a qualifier and is not this family's
  question.

---

## 2. Change

`src/pipeline/xagg.py`. `_STATION_TYPE_KEYWORDS` is **deleted** and replaced by
`_is_station_specialisation()`, in the shape `_is_arrest_rate()`,
`_is_criminal_record_local_gap()` and `_is_weapon_statute_cooccurrence()`
already use in this file:

```python
def _is_station_specialisation(query_lower: str) -> bool:
    if not _STATION_UNIT_NOUN_RE.search(query_lower):
        return False
    if _matches_any(query_lower, _STATION_KIND_TERMS):
        return True
    return bool(_STATION_QUALIFIER_ON_NOUN_RE.search(query_lower))
```

**Signal 1 — the station concept (`_STATION_UNIT_NOUN_RE`), always required.**
`police station(s)`, `station(s)`, `thana`/`thanay`/`thane`/`thanon`,
`\bunits?\b`, `پولیس اسٹیشن`, `تھانوں`/`تھانے`/`تھانہ`, `یونٹ`, `چوکی`.

- A bare **`police` / `پولیس` is deliberately NOT a station signal.** A station
  is a station; an organisation is not. This is the line that keeps KB9 in its
  own family, and it is pinned by its own test.
- `unit` is word-bounded, because it is a substring of `opportunity`,
  `impunity` and `immunity` — the same collision class as the five Urdu ones
  this file has already been bitten by (تعلق in متعلق, رات in کراتا, شام in
  شامل, لوگ in لوگوں, "cyber crime" in "cyber crime circle").

**Signal 2 — the specialisation contrast, in two tiers.** The tier split is
the part that is measured rather than stylistic.

- **Tier 1, `_STATION_KIND_TERMS`** — phrases that name a station *kind* or a
  crime-specialisation contrast outright: `station type`, `type of station`,
  `specialist`, `specialised`/`specialized`, `crime-specific`,
  `general-purpose`, `single/one/specific type of crime`, plus Roman-Urdu and
  Urdu equivalents. Safe anywhere in the sentence — they still need signal 1.
- **Tier 2, `_STATION_QUALIFIER_ON_NOUN_RE`** — the ordinary-language
  qualifiers (`ordinary`, `normal`, `regular`, `dedicated`, `\baam\b`,
  `khaas`, `makhsoos`, `عام`, `خصوصی`, `مخصوص`). These qualify anything, so
  they count **only when they sit directly on the station noun** (0–1
  intervening words). That is the same pairing Module 56's tuple encoded by
  hand as `ordinary station`, `aam police station`, `خصوصی تھانے` — now
  generated rather than enumerated, so it covers combinations nobody typed
  out, and *only* those combinations.

**Dispatch is mirrored into `resolve_aggregate_kind()` and nowhere else.**
Module 41 made that function the single source of dispatch truth and Module
56's AST test pins its purity; that test is re-pointed at the predicate and
now **also asserts `_STATION_TYPE_KEYWORDS` is gone**, not merely unused — a
leftover tuple would be a second, silently-diverging vocabulary.

`_station_caseload_by_specialisation()` — the aggregate itself — was not
touched, and neither was any other family in the chain.

---

## 3. Unit tests

`tests/test_xagg.py`. All three assigned suites pass, plus the harness
suites as blast-radius checks. **0 failures.**

Module 56's four tests are kept and re-pointed at the predicate — including
its **all-32 equality control**, which is the load-bearing one and is re-run
unchanged in substance.

| Test | What it pins |
|---|---|
| `test_module56_all_32_gold_questions_resolve_exactly_as_before` | **all-32 EQUALITY**, against the map captured before Module 56 |
| `test_module56_only_m2_reaches_the_station_type_vocabulary` | exactly one of the 32 matches the predicate: `M2` |
| `test_module56_widened_vocabulary_reaches_the_specialisation_family` (8 cases) | every phrasing Module 56's widening earned still lands |
| `test_module58_out_of_gold_phrasings_are_not_pulled_into_the_family` (14 cases) | **the new guarantee** — adversarial phrasings in none of the 32 |
| `test_module58_predicate_keeps_every_phrasing_module56_earned` (8 cases) | Module 58 is a shape change, not a narrowing |
| `test_module58_both_signals_are_required_neither_is_sufficient` | the multi-signal contract, stated directly |
| `test_module58_tier2_qualifiers_must_sit_on_the_station_noun` | the tier split |
| `test_module58_bare_police_is_not_a_station_signal` | KB9's measured shape |
| `test_module58_unit_is_word_bounded` | `opportunity`/`impunity` |
| `test_module56_dispatch_change_lives_only_in_resolve_aggregate_kind` | AST purity, re-pointed, **plus the tuple is gone** |

The out-of-gold negatives, in full — every one of these resolved to
`station_caseload_by_specialisation` **before** the change where marked ❌:

| Phrasing | Before | After |
|---|---|---|
| "How many cases involve one type of crime only?" | ❌ hijacked | `total_count` |
| "How many FIRs cover a single type of crime?" | ❌ hijacked | `total_count` |
| "Are most of our cases a single type of crime, or a mix?" | ❌ hijacked | not this family |
| "کیا ہمارے زیادہ تر مقدمات ایک ہی قسم کے جرم سے متعلق ہیں؟" | ok | not this family |
| "Which station has the most ordinary theft cases?" | ok | `station_or_category_counts` |
| "Is it normal for a case to take this long at any station?" | ok | `station_or_category_counts` |
| "What is the regular procedure a station follows after an FIR is registered?" | ok | `station_or_category_counts` |
| "Do we hold a dedicated register of recovered weapons at each station?" | ok | `graph_recurrence_weapon` |
| "کیا تھانے میں عام شکایات درج ہوتی ہیں؟" | ok | `station_or_category_counts` |
| KB9's full Roman-Urdu text (`police` + `khaas tor par`) | ok | `graph_recurrence_person` |
| S2's gold text, and its two paraphrases | ok | `station_or_category_counts` |
| "How many cases per police station?" / "Har thane mein kitne cases hain?" | ok | unchanged |

Three of these were live defects the 32-question control could not see. The
rest are the boundary the *new* shape could have broken and does not.

---

## 4. Live verification

**Infrastructure checked first:** model server `/health` -> `{"status":"ok"}`;
backend 8022 `/health` -> ok, 7716 documents; quota grep over every log of this
session -> **0**.

### M2's gold text — 3 runs

| Run | Route events | Sub-agent | `XAGG <kind>` log line | Seconds |
|---|---|---|---|---|
| 1 | `XAGG` | Large-Scale Aggregate | `station_caseload_by_specialisation` | 116.1 |
| 2 | `XAGG` | Large-Scale Aggregate | `station_caseload_by_specialisation` | 93.2 |
| 3 | `XAGG` | Large-Scale Aggregate | `station_caseload_by_specialisation` | 110.5 |

**3 of 3 — one route event, one dispatch, the right family every time.** The
aggregate line is byte-identical on all three:

```
XAGG station_caseload_by_specialisation: 19 station(s), 73 FIR(s);
crime_type_specialised=2 station(s)/9 FIR(s);
other_specialised=2 station(s)/10 FIR(s);
general_purpose=15 station(s)/54 FIR(s);
9 FIR(s) with no resolvable incident year, 0 case(s) at an unknown station
```

### The filed defect, live — 2 runs

*"How many cases involve one type of crime only?"* — the phrasing Module 58
was filed for, which **before this change resolved to
`station_caseload_by_specialisation`**:

| Run | Route | `XAGG <kind>` | Answer |
|---|---|---|---|
| 1 | `XAGG` | `total_count`: 73 case(s) | *"The provided document does not specify how many of the 73 total cases involve only one type of crime..."* |
| 2 | `XAGG` | `total_count`: 73 case(s) | *"...additional data categorizing cases by the number of crime types involved would be required."* |

**2 of 2 an honest, correct limitation** instead of a station-specialisation
breakdown answering a question nobody asked. Under this wave's judging
standard, correctly stating the data lacks something is a pass — and it is
strictly better than the fluent, on-topic, confidently wrong answer the tuple
would have produced.

---

## 5. Gold comparison

M2's gold: *"Specialized cybercrime units already carry real load: 9 of 73
FIRs (~12%) from just 2 of 19 stations — disproportionate given they're
single-purpose."*

| Gold element | Present |
|---|---|
| the specialised stations are the **cybercrime** ones | **3 / 3** — سائبر کرائم سرکل، اسلام آباد (5 FIRs) and سائبر کرائم سرکل، راولپنڈی (4), named by station |
| **9 of 73 FIRs (~12%)** | **2 / 3** |
| from **2 of 19 stations** | **2 / 3** |
| disproportionate for single-purpose stations | **2 / 3** — served as "11% of the stations carry ~12.3% of the caseload" |

Run 2 served only the growth half (*"7 in 2024 to 39 in 2026 ... 3 to 5"*) and
dropped the concentration figure. **The aggregate produced the right numbers on
all three runs** — the log line in §4 is identical every time — so this is the
paraphrase/verifier layer above XAGG, not dispatch, and it is the same 2-of-3
prose variance Modules 44 and 56 already recorded for M2. Module 58 changed
dispatch only, and dispatch is 3 of 3. Filed as Module 70 below.

Module 44's honest dissent from gold's *growth* framing also stands: the
general-purpose stations are growing **faster** (7->39 against 3->5); the
specialisation story is one of concentration, not growth. The answers say so.

---

## 6. Non-gold paraphrase

*"Do the specialist units handle a bigger share of our cases than the ordinary
police stations?"* — Module 56's own measured failure case, which resolved to
`station_or_category_counts` before Module 56 and must keep working under
Module 58's changed shape. It is the sharpest live test of the tier split:
`specialist` is tier 1, `ordinary police stations` is tier 2, and `units` is
the word-bounded station noun.

| Run | Route | `XAGG <kind>` | Seconds |
|---|---|---|---|
| 1 | `XAGG` | `station_caseload_by_specialisation`, identical figures | 79.7 |
| 2 | `XAGG` | `station_caseload_by_specialisation`, identical figures | 77.7 |

**2 of 2.** Eight further non-gold phrasings across English, Roman Urdu and
Urdu script are pinned statically in
`test_module58_predicate_keeps_every_phrasing_module56_earned`.

---

## 7. Regression guard

### Static — all 32, EQUALITY

`resolve_aggregate_kind()` over every gold question, against the map captured
**before Module 56**: **identical, 32 of 32.** Nothing moved. This is the same
control Module 56 shipped, re-run against a completely different dispatch
mechanism, which is the strongest available evidence for a shape change.

The narrower half holds too: **exactly one** of the 32 matches
`_is_station_specialisation()` — `M2`. S2 (`station`, `police`) and CR6
(`تھانے`) carry the bare station word and do not; KB5 and M5 carry `عام` and
do not; **KB9 carries `police` + `khaas` and does not**, which a naive AND
would have got wrong.

### Static — out-of-gold, the part the 32 cannot see

14 adversarial phrasings, listed in full in §3. **Three were live false
positives before this change** (`one type of crime only`, `a single type of
crime`, `a single type of crime, or a mix`) and are now correct. The other
eleven are the boundary the *new* shape could have broken — `ordinary theft`,
`normal`/`regular` procedure questions, `dedicated register`, `عام شکایات`,
and KB9's full text — and it does not.

### Live — 2 runs each, same backend, this branch

| Question | Runs | Route | Aggregate reached | Verdict |
|---|---|---|---|---|
| **G3** | 2/2 | `XAGG` | `court_readiness_scan` | gold's three findings present |
| **CR7** | 2/2 | `XAGG` | `criminal_record_court_crosscheck` | 33 / 30 under trial / 1 convicted, court record agrees |
| **G2** | 2/2 | `XAGG` | `case_completeness_scan` | 9 missing incident dates present |
| **G5** | 2/2 | `XAGG` | `weapon_compliance_scan` | 30 of 32 unlicensed present |
| **M5** | 2/2 | `XAGG` | `weapon_statute_cooccurrence` | 2024 vs 2026 shift present |
| **M4** | 7/7 | `XAGG` | `statute_court_stage_join` | see `MODULE60_RESULT.md` |

No question moved family and none decomposed. Across the shared batch — 22
live runs on this branch — there were **zero** route or dispatch changes
outside the two this branch intends.

---

## 8. New defects found — filed, not folded in

### Module 69 — tier 2's proximity window is a judgement, not a measurement

`_STATION_QUALIFIER_ON_NOUN_RE` allows **0-1 intervening words** between the
qualifier and the station noun. That window covers the forms Module 56 had
enumerated by hand (`ordinary police stations`, `aam police station`,
`regular police thana`) and survives 14 adversarial phrasings — but the number
itself was chosen, not fitted. A gap of 2 would admit *"ordinary, everyday
police stations"*; it would also admit *"normal for a case at the station"*.
**No corpus of real user phrasings was available to fit it against**, and one
should exist before the window is widened.

**Verify:** collect station-question phrasings from outside the 32 — ideally
from live logs rather than written for the test — and measure precision and
recall at gaps 0, 1 and 2.

### Module 70 — M2's concentration figure is dropped above XAGG on ~1 run in 3

**Observed here, caused elsewhere.** The aggregate emits identical figures on
3 of 3 runs; the served answer carries gold's "9 of 73 from 2 of 19" on only 2
of 3. The loss is in the paraphrase/verifier layer, which Modules 44 and 56
both saw and neither owns. Same class as Module 61's verifier finding, on a
different question — and it means **any single-run M2 score is a coin flip on
gold's headline number**.

**Verify:** M2 x10 on unchanged code, counting how often the concentration
sentence survives; then whether the drop correlates with a verifier rejection
and the raw-aggregate fallback.

### Not a defect, recorded

`_is_station_specialisation()` deliberately does **not** accept a bare
`police`/`پولیس` as its station signal, so *"Do specialist police handle more
cases than ordinary police?"* — with no `station`, `thana` or `unit` anywhere
— does not reach the family. That is a real, accepted narrowing relative to
Module 56's `specialist police` entry. It was taken knowingly: admitting the
bare organisation word is what hijacks KB9, and every phrasing in Module 56's
own positive set contains a station or unit noun anyway.
