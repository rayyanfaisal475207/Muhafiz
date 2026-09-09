# Module 56 — M2's dispatch vocabulary is narrower than its new family

**Found by:** Module 44 · **Branch:** `fix/xagg-log-lines-and-m2-vocabulary`
· **Date:** 2026-09-09

---

## 1. Root cause

M2's dispatch rests on `_STATION_TYPE_KEYWORDS`. Module 13 wrote that tuple to
gate an **honest refusal** — at the time, no station-type dimension existed
anywhere in the data model. Module 44 changed only the **kind** it returns,
from that refusal to `station_caseload_by_specialisation`, a real aggregate
that derives the specialisation from station names. The vocabulary was left
exactly as it was.

**That asymmetry is the defect.** A narrow trigger list is the *safe* default
for a refusal — a missed match just means the question falls through to a
generic answer, which is no worse than the refusal it missed. It is the
*wrong* default for a real aggregate: a missed match now means the question
silently gets the plain per-station count that the refusal was written to
prevent, and gets it with no signal that anything went wrong.

Reproduced on the pre-fix tree (`resolve_aggregate_kind()`, commit `9942db9` +
Module 55):

| Query | Resolved to |
|---|---|
| *"Do the specialist units handle a bigger share of our cases than the ordinary police stations?"* | `station_or_category_counts` ❌ |
| *"Are our specialised station types taking on more cases than the general purpose stations?"* | `station_caseload_by_specialisation` ✅ |
| *"Which is busier relative to its size: a station set up for one specific type of crime, or a normal thana?"* | `station_caseload_by_specialisation` ✅ |

The first row is M2's own question in ordinary words. It matched none of the
eight English entries, so it fell all the way to `_STATION_KEYWORDS` a few
rungs lower and got the bare per-station ranking.

The brief's hypothesis was correct as stated and required no revision.

---

## 2. Change

`src/pipeline/xagg.py` — `_STATION_TYPE_KEYWORDS` only. Nothing else in the
dispatch chain, and no aggregate, was touched.

Module 13's original eight entries are kept verbatim. Added:

- **The specialised side.** `specialist unit / station / thana / police`,
  `specialised|specialized unit / thana / police`, `dedicated unit / station /
  thana`, `crime-specific station`, `single type of crime`, `one type of
  crime`. `specialist police` and `specialised|specialized police` are
  deliberately the shorter *stems*, so they also catch "… police station(s)"
  and "… police units" without a separate entry each.
- **The general-purpose side.** `ordinary station / police / thana`,
  `normal station / police station / thana`, `regular station / police station
  / thana`, `general-purpose|general purpose thana`, `general-purpose|general
  purpose unit`.
- **Roman Urdu.** `makhsoos thana|thanay|thane`, `khaas thana|thanay|thane`,
  `aam thana|thanay|thane`, `aam police station`.
- **Urdu.** `خصوصی تھانہ`, `خصوصی تھانے`, `خصوصی یونٹ`, `مخصوص تھانہ`,
  `مخصوص تھانے`, `عام تھانہ`, `عام تھانے`, `عام پولیس اسٹیشن`.

**The safety rule, stated once and applied to every entry: the qualifier and
the station word always travel together.** No entry is a bare station word,
and no entry is a bare qualifier. `station` on its own is S2's question
(*"Which police station handles the most cases?"*); `تھانے` on its own is
CR6's opening clause (*"جب کوئی شخص تھانے آ کر شکایت درج کراتا ہے…"*); `عام`
on its own appears in KB5 qualifying a **case**, not a station (`عام مقدمے`).
`aam`/`khaas`/`makhsoos` are never entered bare — the `عام`-class word is
exactly the substring-collision family this file has been bitten by five times
(تعلق inside متعلق, رات inside کراتا, شام inside شامل, لوگ inside لوگوں, and
"cyber crime circle" containing "cyber crime").

**Mirroring into `resolve_aggregate_kind()`:** nothing to mirror. Module 41
already made that function the single source of dispatch truth, and it is the
only reader of `_STATION_TYPE_KEYWORDS`; `run_aggregate()` consumes its return
value. A new AST test now **pins** that property, so a second inline check
added to `run_aggregate()` later fails the suite instead of drifting silently.
The existing purity test (no `await`, no gateway in
`resolve_aggregate_kind()`) is unaffected — a keyword tuple is pure.

---

## 3. Unit tests

```
PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/test_xagg.py -q
→ 332 passed, 0 failed, 0 skipped
```

Adjacent suites (supervisor, router, orchestrator, meta-analysis agent, XAGG
agent + tool): **431 passed, 0 failed**.

**The all-32 EQUALITY control** —
`test_module56_all_32_gold_questions_resolve_exactly_as_before`. The full
id → resolved-kind map was captured from `resolve_aggregate_kind()` **before a
single keyword was added** and is pinned in the test file as
`_GOLD32_RESOLVED_KINDS_BEFORE_MODULE56`. The test asserts dict equality over
all 32.

Equality, not absence, and the difference is load-bearing: an
absence-only control ("no extra question reaches M2's family") would pass
even if a widened keyword pushed CR3 or KB4 sideways into some *third* family.
The dataset is `evaluation/Gold_QA_Dataset_Final32_With_Answers.json`; a
missing file is a **failure**, never a skip.

Supporting tests:

- `test_module56_only_m2_reaches_the_station_type_vocabulary` — exactly one of
  the 32 may match the tuple directly: `["M2"]`.
- `test_module56_m2_literal_gold_text_still_reaches_its_own_family` — pinned
  to M2's literal gold text.
- `test_module56_widened_vocabulary_reaches_the_specialisation_family` — 8
  parametrised positives, including the measured failure verbatim, plus Roman
  Urdu and Urdu-script phrasings.
- `test_module56_widened_vocabulary_does_not_swallow_plain_station_questions`
  — 7 parametrised negatives: S2's literal gold text, its Roman-Urdu and Urdu
  equivalents, CR6's bare `تھانے` clause, KB5's `عام مقدمے`, and a plain
  per-station breakdown in two languages.
- `test_module56_dispatch_change_lives_only_in_resolve_aggregate_kind` — the
  AST guard described above.

---

## 4. Live verification

**Backend/SSE verification was DEFERRED FOR CONTENTION.**

Ports **8015** (Module 38) and **8016** (Modules 53/57/40) were both LISTENING
and both actively posting to the shared model server — log lines timestamped
within seconds of the check — with **4.2 GB of 16 GB** RAM free. Module 50
measured that extra concurrent backends alone cause sub-query timeouts.
Bringing up a third on 8017 would have degraded their measurements as well as
mine, so it was not started.

**What was run instead:** `run_aggregate()` end to end, in-process, against
the **live** `DirectGateway` (Postgres, 73 cases) and the **live** AGE graph —
i.e. the real dispatch chain and the real aggregate, with only the
uvicorn/SSE and LLM-rendering layers absent.

M2's literal gold text:

> *"Is caseload growing faster at our general-purpose stations, or at the
> handful set up for one specific type of crime?"*

```
XAGG station_caseload_by_specialisation: 19 station(s), 73 FIR(s);
  crime_type_specialised=2 station(s)/9 FIR(s);
  other_specialised=2 station(s)/10 FIR(s);
  general_purpose=15 station(s)/54 FIR(s);
  9 FIR(s) with no resolvable incident year, 0 case(s) at an unknown station
→ kind = station_caseload_by_specialisation
```

The previously-failing paraphrase:

> *"Do the specialist units handle a bigger share of our cases than the
> ordinary police stations?"*

```
XAGG station_caseload_by_specialisation: 19 station(s), 73 FIR(s);
  crime_type_specialised=2 station(s)/9 FIR(s);
  other_specialised=2 station(s)/10 FIR(s);
  general_purpose=15 station(s)/54 FIR(s); ...
→ kind = station_caseload_by_specialisation
```

Identical result, from a question that returned the wrong family an hour
earlier. Note that this line is itself a Module 55 artefact — the aggregate
naming itself is what makes "which family answered?" answerable without
reading prose, which is exactly why 55 was done first.

What is **not** claimed: that M2 was observed reaching this family through a
live SSE request with a rendered natural-language answer. That is deferred.
Module 44 already recorded three consecutive live SSE runs of M2's literal
gold text landing on this family, and no code between the router and the
aggregate changed on this branch — only the keyword tuple widened.

---

## 5. Gold comparison

M2's gold answer cites caseload concentrated in the crime-type specialised
stations: **9 of 73 FIRs (~12%) from just 2 of 19 stations**.

| | gold | measured |
|---|---|---|
| stations | 19 | 19 ✅ |
| FIRs | 73 | 73 ✅ |
| crime-type specialised stations | 2 | 2 ✅ |
| FIRs at those stations | 9 (~12%) | 9 (~12.3%) ✅ |

Reproduced exactly, which is the expected result: Module 44 established these
figures and Module 56 changed **only which phrasings reach the aggregate**,
not what it computes.

Module 44's honest dissent from gold stands unchanged and is repeated here so
it is not quietly lost: gold frames M2 as a **growth** question, and the
corpus has usable incident years for only two buckets (2024 and 2026) with 9
FIRs carrying no resolvable year at all. The aggregate reports the
concentration gold cites and publishes the year split alongside, rather than
asserting a growth rate the data cannot carry. Under the judging standard —
correctly stating that the data lacks something, where that is true, is a pass
— that is the right answer, not a hedge.

---

## 6. Non-gold paraphrase

The whole module *is* a paraphrase test, so the separation between "capability
fix" and "curve-fitting" is unusually sharp here: not one of the eight
positive phrasings is gold text, and gold's own wording was already passing
before the change.

| Paraphrase | Resolves to |
|---|---|
| "Do the specialist units handle a bigger share of our cases than the ordinary police stations?" | `station_caseload_by_specialisation` ✅ |
| "Do dedicated units carry more caseload than regular police stations?" | ✅ |
| "Is a normal thana busier than a station set up for one type of crime?" | ✅ |
| "How does the caseload at our specialist police stations compare with the ordinary ones?" | ✅ |
| "Kya makhsoos thanay aam thanay se zyada cases handle kar rahe hain?" | ✅ |
| "Khaas thane ka caseload aam police station se kitna mukhtalif hai?" | ✅ |
| "کیا خصوصی تھانے عام تھانے سے زیادہ مقدمات سنبھال رہے ہیں؟" | ✅ |
| "کیا مخصوص تھانے پر کیس لوڈ عام تھانے کے مقابلے میں زیادہ ہے؟" | ✅ |

All three languages the gold set is written in, all reaching the family.

---

## 7. Regression guard

- **All 32 gold questions**: resolved kinds byte-identical before and after.
  Verified twice — once by diffing the captured maps directly, once by the
  pinned equality test.
- Explicit negatives re-run: **S2** (`station_or_category_counts`, its
  literal gold text plus Roman-Urdu and Urdu forms), **CR6**
  (`cms_fir_linkage`), **KB5** (`gender_breakdown`), **CR3**
  (`station_or_category_counts`), **KB1/KB3/KB4/KB8/G6**
  (`station_or_category_counts` — the family sitting directly below this one
  in the chain, and the one a careless widening would have raided).
- The five named regression questions, at the dispatch **and** aggregate
  layer, against live data: **G2** → `case_completeness_scan` (73 scanned,
  9 missing an incident date, 52 missing a status); **G3** →
  `court_readiness_scan` (94 accused edges, 82 with no relationship, 2 of 32
  weapons with no licence status, 9 of 73 cases with no incident date);
  **G5** → `weapon_compliance_scan` (32 weapons, 30 unlicensed, 2 with no
  status); **CR7** → `criminal_record_court_crosscheck` (33 records, 1
  settled, 32 in progress, 1 cross-checked and consistent); **M2** →
  `station_caseload_by_specialisation` (above).
- `tests/test_xagg.py` 332 passed; adjacent suites 431 passed. Full `pytest
  -q` deliberately not run — it empties `muhafiz_entity_descriptions`.

---

## 8. New defects found

**One, recorded and deliberately not fixed here.**

`_STATION_TYPE_KEYWORDS` is now a **58-entry substring list**, and the
protection against it swallowing a neighbour is entirely the all-32 equality
control. That control is only as broad as the 32 questions in it. Every
station-type question outside the gold set is protected by nothing but the
"qualifier and station word travel together" rule and reviewer judgment — and
the rule has one soft spot already visible: `single type of crime` and `one
type of crime` are the two entries that do **not** name a station at all. They
are in because M2's own gold text is phrased that way ("set up for one
specific type of crime"), and none of the 32 collides with them, but a future
question like *"how many cases involve one type of crime only?"* would be
pulled into this family wrongly.

The structurally right fix is the one `_is_arrest_rate()`,
`_is_criminal_record_local_gap()` and `_is_weapon_statute_cooccurrence()`
already use elsewhere in this file: replace the flat tuple with a
**multi-signal predicate** — a station/unit signal AND a specialisation-
contrast signal — so no single phrase can carry the dispatch alone. That is a
real behaviour change with its own regression surface and does not belong in a
module whose brief is "widen the vocabulary".

**Raised as a new module: Module 58 — `_STATION_TYPE_KEYWORDS` should be a
two-signal predicate, not a 58-entry substring list.** Tracked in
`GOLD_QA_REMAINING_FIXES_PLAN.md`.
