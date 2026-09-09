# Module 75 — KB8's challans sent to court

**Branch:** `feat/xagg-kb-data-half-aggregates` · **Base:** `main` @ `0bc728d`
**Brief:** Module 75 in `GOLD_QA_REMAINING_FIXES_PLAN.md`
**Filed by:** Module 39.
**Environment, Chroma, out-of-bounds files and the quota check** are
identical to `MODULE74_RESULT.md`'s header and are not repeated: same
session, same worktree, same backend on `:8025`, same read-only shared
Chroma, **0 real quota lines across 38 live runs**.

---

## 1. Root cause

The brief's suspected cause is **confirmed by probe**, and it is exactly the
one-word difference the row predicted.

KB8's gold answer is compound: CrPC s.173 requires the investigation to be
completed without unnecessary delay and an **interim report** to the
magistrate within three days of the fourteenth day **plus** a figure from our
own data — *"adaalat bheja gaya challan (**26 cases**)"* — **plus** an honest
schema gap: *"schema mein kahin interim report ka koi tasavvur nahi aur
challan ko uski tafteesh ke aaghaaz se jorne wala koi field nahi."*

Before this module the canned data-half sub-query for that clause reached
`_CRIMINAL_RECORD_KEYWORDS` and got `criminal_record_court_crosscheck`, which
counts the **33** `criminal_record` rows. That is a correct answer to CR7's
question and a **confidently wrong** one to KB8's, in the specific way that
is hardest to catch: a plausible number in the exact slot gold fills with 26.

### 1.1 The probe — derived independently, before any code

`scratchpad/probe74.py` / `scratchpad/probe75.py`, hand-written Cypher
against the live AGE graph:

```cypher
MATCH (s:StructuredRecord) RETURN s.record_type AS rt
```

Every `StructuredRecord` type in the corpus, counted:

| record_type | rows |
|---|---|
| `fir_zimni_index` | 259 |
| `fir_section` | 218 |
| `fir_position` | 94 |
| `malkhana_register` | 45 |
| `criminal_record` | **33** ← what `xagg.py` returned for KB8 |
| **`chalaan_dispatch`** | **26** ← **what KB8 asks for** |
| `chalaan_outcome` | **20** ← the only challan type `xagg.py` read |
| `pkm_application` | 14 |
| `cms_complaint` | 4 |

```cypher
MATCH (s:StructuredRecord)-[:BELONGS_TO_CASE]->(c:Case)
WHERE s.record_type = 'chalaan_dispatch'
RETURN count(s) AS n, count(DISTINCT c.case_id) AS cases
-- 26 rows, 26 distinct cases, 26 of 26 linked to a Case (0 unlinked)
-- 15 of the 26 carry a `dispatch_datetime`

MATCH (s:StructuredRecord)-[:BELONGS_TO_CASE]->(c:Case)
WHERE s.record_type = 'chalaan_outcome'
RETURN count(s) AS n, count(DISTINCT c.case_id) AS cases
-- 20 rows, 20 distinct cases; 15 carry `challan_reached_court_date`
```

| | gold | Module 39 | **this module** | verdict |
|---|---|---|---|---|
| challans sent to court | **26** | 26 rows / 26 cases | **26 rows / 26 distinct cases** | ✅ exact, three ways |
| what was returned instead | — | 33 criminal records | **33**, reproduced | ✅ cause confirmed |

**The brief's specific question — "is the cause really `chalaan_outcome` vs
`chalaan_dispatch`?" — answered: YES, and it is worse than a two-way
confusion.** There are **three** record types in play and **three different
quantities**: `criminal_record` **33**, `chalaan_dispatch` **26**,
`chalaan_outcome` **20**. `grep -c "chalaan_dispatch" src/pipeline/xagg.py`
was **0** before this module; `chalaan_outcome` was read only inside
`_criminal_record_court_crosscheck()`'s court half. So `xagg.py` had a
challan reader, it read the wrong one of the two challan types, and the
question routed to a family that read neither.

### 1.2 The schema gap is derived, not declared

Gold's third clause is an **absence**, and the brief's judging standard
counts correctly stating a gap, where gold agrees, as a pass. The aggregate
therefore reads the DISTINCT record types actually present and reports the
absence of an interim-report type from that — so the claim stops being made
the moment such a record is ingested, rather than sitting in the renderer as
prose that silently goes stale. Measured today:
`has_interim_report_record=False`.

---

## 2. Change

| File | Why here |
|---|---|
| `src/pipeline/xagg.py` | `_CHALAAN_TERMS` / `_SENT_TO_COURT_TERMS`, `_is_chalaan_dispatch_count()`, `_CHALAAN_DISPATCH_RECORD_TYPE` / `_CHALAAN_OUTCOME_RECORD_TYPE` / `_INTERIM_REPORT_TOKENS`, `_chalaan_dispatch_count()`, `render_chalaan_dispatch_count()`, the `resolve_aggregate_kind()` entry and the mirrored `run_aggregate()` branch. |
| `src/pipeline/harness/tools/xagg.py` | `AggregateKind` Literal entry + renderer import + render branch. |
| `src/pipeline/orchestrator.py` | The other two render sites. |
| `tests/test_xagg.py` | 11 new tests (§3). |

**Dispatch placement**, mirrored identically in `resolve_aggregate_kind()`
and `run_aggregate()`:

- **BELOW** CS4's `criminal_record_local_match_gap`, a two-signal predicate
  over the same records that carries no challan vocabulary.
- **ABOVE, decisively,** `_CRIMINAL_RECORD_KEYWORDS` (CR7) — the family that
  was answering KB8 with 33.

CR7 keeps first claim on its own vocabulary because the new predicate
additionally requires a **challan** term, which CR7's gold text does not
contain. Verified against all 32 (§3).

**The aggregate reports the neighbouring `chalaan_outcome` count alongside
rather than instead of the dispatch one**, and the `XAGG` log line names
both. The whole defect was a silent substitution between two similar
counts; a run that read the wrong one is now visible in the log rather than
merely plausible in the prose.

The Literal entry and all three render sites landed **in the same commit as
the aggregate**, before any live run — §4 is the live proof.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_xagg.py -q
419 passed
```

The tests that carry the argument:

- `test_module75_counts_chalaan_dispatch_not_chalaan_outcome` — **the literal
  defect as a fixture**: 26 dispatch records and 20 outcome records in the
  same graph. The answer is 26, both counts are in the result, and the
  rendering says *"the 26 above is the dispatch figure"*.
- `test_module75_reads_the_dispatch_record_type_by_name` — asserts on the
  **Cypher text**, not the count. If a later edit points this at
  `chalaan_outcome`, the count silently becomes 20 and every numeric
  assertion would have to be edited with it; this one fails first.
- `test_module75_cases_are_distinct_not_row_counts` — two dispatch records on
  one case is 2 challans across 1 case.
- `test_module75_the_schema_gap_is_derived_not_declared` — asserts BOTH
  directions: absent → the gap is stated; an `interim_report` type present →
  it is not.
- `test_module75_emits_its_xagg_log_line_with_both_figures` — Module 55's
  convention, and here the *second* figure is load-bearing.
- `test_module55_every_aggregate_kind_emits_an_xagg_log_line` and
  `test_module55_log_format_strings_stay_ascii` — **both pass** with the new
  kind.
- `test_every_new_aggregate_kind_is_accepted_by_the_harness_tool_result` —
  extended with `chalaan_dispatch_count` (the tenth family).
- `TestChalaanDispatchBoundary` — four challan phrasings reach the family;
  CR7's **literal gold text**, a challan word with no court signal, and a
  court signal with no challan word all do not; CR7 keeps
  `criminal_record_court_crosscheck`.
- `test_module75_no_gold_question_reaches_the_challan_predicate` — the all-32
  narrow control: **none** of the 32 matches, including KB8. That is
  deliberate and is stated in §5.
- `test_module56_all_32_gold_questions_resolve_exactly_as_before` — the
  all-32 **EQUALITY** control is **byte-identical before and after this
  module**. Module 75 moves nothing in the gold set.

**One Urdu bug found by its own test and fixed.** `_SENT_TO_COURT_TERMS`
shipped with full verb forms only, so *"عدالت بھیجے گئے"* missed while
*"عدالت بھیجا"* matched. Changed to verb **stems** (`عدالت بھیج`), which is
the same class of collision this file's comments already record five times.

---

## 4. Live verification

### 4.1 The aggregate, through the real pipeline

The canned data-half sub-query, sent to `/api/chat` on `:8025`:

> *"How many challans have been sent to court, and how many cases do they
> cover, across all cases?"*

| run | route | status | wall | `XAGG <kind>` line in `backend.log` |
|---|---|---|---|---|
| 1 | **XAGG** | done | 1.9 s | `XAGG chalaan_dispatch_count: 26 challan dispatch record(s) across 26 case(s), 15 carrying a dispatch timestamp; 20 chalaan_outcome record(s) across 20 case(s), 15 with a court-reached date; interim-report record type present=False` |
| 2 | **XAGG** | done | 2.1 s | identical |
| 3 | **XAGG** | done | 1.8 s | identical |

**Verbatim answer, run 1 (identical on all three):**

> *"According to the data, 26 challans have been sent to court, covering 26
> cases across all cases [Document 1]."*

3 of 3 · non-empty · `status=done` — the live proof that the
`XAggToolResult` Literal trap is cleared for this kind.

The full deterministic rendering the composition path would cite, from three
in-process runs of the exact `XAggToolInput -> xagg_tool -> XAggToolResult`
sequence (`status=OK`, `kind='chalaan_dispatch_count'`, 924 chars, 3 of 3):

> *"Our case-tracking data records 26 challan(s) sent to court, covering 26
> case(s). 15 of those carry a dispatch timestamp; the rest record the
> dispatch without a date.*
> *A separate, smaller set of 20 challan-outcome record(s) across 20 case(s)
> records what happened next, 15 of them with a date the challan reached
> court. The two are different records and different counts; the 26 above is
> the dispatch figure.*
> *What the data cannot show: there is no interim-report record type anywhere
> in the schema, and no field linking a challan back to the start of its own
> investigation. So the data confirms that a case reached court, but not
> whether any interim reporting duty along the way was met."*

### 4.2 KB8's own gold question

**KB8's data half is still not present live**, and the reason is the same
`rag.py` boundary as Module 74's: the landing step is a one-entry addition to
`_KB_DATA_HALF_PLANS`, and `rag.py` belongs to the live Module 65 track.
Filed as **Module 77**.

Unlike KB3, **KB8 does reach the RAG path**, so that one entry is all it
needs. Measured, 3 of 3, and unchanged by this module:

| run | route | status | wall | data half | statutory half |
|---|---|---|---|---|---|
| 1 | RAG | **error** | 72.9 s | absent | *"The generated answer could not be verified as grounded in the retrieved documents."* |
| 2 | RAG | done | 74.1 s | absent | s.173 final report + the case-diary copy to the court |
| 3 | RAG | done | 65.9 s | absent | identical to run 2 |

**KB8's gold question text does not contain the word "challan" at all** — it
is in gold's *answer*. So KB8 reaches this family only through the canned
sub-query, exactly as KB4/KB5/KB6 reach theirs, which is why §3's all-32
control shows zero movement and why that is the correct result rather than a
gap.

---

## 5. Gold comparison

| | gold | measured | verdict |
|---|---|---|---|
| challans sent to court | **26** | **26** | ✅ exact |
| cases covered | "26 cases" | **26 distinct** | ✅ exact |
| no interim-report concept in the schema | asserted | **derived: absent** | ✅ agrees, and now data-derived |
| no field linking a challan to the start of its investigation | asserted | stated | ✅ agrees |
| what was returned before | — | **33** criminal records | the defect, reproduced |
| CrPC s.173 / 14 days / 3-day interim report | statutory half | not this module's — the RAG path already answers it (§4.2, runs 2 and 3) | n/a |

By the brief's standard the aggregate's rendering **covers every element of
gold's data half**, including the two gaps gold states as absences — which
the standard explicitly counts as a pass. Nothing is tuned: the 26 came from
hand-written Cypher before the aggregate existed, and the 20 and 33 are
reported alongside precisely so a future wrong-metric run is legible.

---

## 6. Non-gold paraphrase

Written before the sweeps, in two languages, neither matching the sub-query's
wording:

| paraphrase | result |
|---|---|
| *"Of our cases, how many have had their challan forwarded to court already?"* (English, different verb) | **`chalaan_dispatch_count`** ✅ |
| *"Hamare kitne cases ka chalaan adaalat tak pohanch chuka hai?"* (Roman-Urdu) | **`chalaan_dispatch_count`** ✅ |
| *"کتنے مقدمات کا چالان عدالت بھیجا جا چکا ہے؟"* (Urdu script) | **`chalaan_dispatch_count`** ✅ |
| *"کتنے چالان عدالت بھیجے گئے؟"* (Urdu script, different verb form) | ✅ **after** the stem fix in §3; ❌ before it |
| *"What property is listed on the challan?"* (challan, no court signal) | correctly **not** this family ✅ |
| *"How many cases have reached court?"* (court signal, no challan) | correctly **not** this family ✅ |

**6 of 6 behaving as intended**, and the two negatives matter as much as the
four positives — they are what shows the two-signal gate is doing work rather
than the word "challan" alone.

---

## 7. Regression guard

Identical sweep to `MODULE74_RESULT.md` §7 (one session, one backend), so the
table is not repeated in full. The results that bear specifically on this
module:

| Q | runs | result |
|---|---|---|
| **CR7** | 2/2 | `route=XAGG`, `XAGG criminal_record_court_crosscheck: 33 criminal record(s), 1 settled / 32 in progress; 1 cross-checked against a court outcome, 1 consistent` — **unchanged**, and the 33 is still where it belongs |
| M4 | 2/2 | `statute_court_stage_join` |
| G3 | 2/2 | `court_readiness_scan` |
| G2 | 2/2 | `case_completeness_scan` |
| G5 | 2/2 | `weapon_compliance_scan` |
| CS4 | resolver | `criminal_record_local_match_gap` |
| KB4 / KB5 / KB6 | 2/2 each | Module 39's data halves all still dispatch — 45 / 8 / 32 |
| KB1 | 2/2 | unchanged |
| KB2 | 2/2 | `status=error`, the pre-existing verifier rejection, both arms |

**All-32 resolver equality: byte-identical.** This module moves nothing.

---

## 8. New defects found

- **Module 77 — the `_KB_DATA_HALF_PLANS` entry for KB8.** One entry: a
  challan/court-report pattern list matching KB8's *question* wording (which
  says *"adaalat ko kuch report karna"*, not "challan"), the sub-query
  `_KB8_SQ_CHALAAN_DISPATCH` from `tests/test_xagg.py`, and
  `expected_kind="chalaan_dispatch_count"`. Not done here because `rag.py`
  belongs to the live Module 65 track.
- **KB8's statutory half cites the wrong s.173 clause 2 of 3 times.** Both
  answering runs reach the *final* report and the 24-hour case-diary copy;
  gold's clause is the **14-day / 3-day interim report**, which Module 39
  measured as present. Not a regression from this module and not fixable in
  `xagg.py` — recorded so the next KB8 run knows it was already so.
- **`status=error` on 1 of 3 KB8 runs**, the verifier's *"could not be
  verified as grounded"*. Same signature Modules 38/52/61 track; recorded
  as another instance, not a new cause.
