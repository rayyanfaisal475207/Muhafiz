# Module 76 — KB9's per-section FIR count, and gold's 8

**Branch:** `feat/xagg-kb-data-half-aggregates` · **Base:** `main` @ `0bc728d`
**Brief:** Module 76 in `GOLD_QA_REMAINING_FIXES_PLAN.md`
**Filed by:** Module 39, which wired `statute_court_stage_join` for KB9 anyway
and measured what the wrong grain costs.
**Environment, Chroma, out-of-bounds files and the quota check** are identical
to `MODULE74_RESULT.md`'s header: same session, worktree
`D:/Rapids AI/muhafiz-m74`, backend `:8025`, read-only shared Chroma,
**0 real quota lines across 38 live runs**.

---

## 1. Root cause

**It is a grain failure, not a data failure**, and the brief's framing is
confirmed.

KB9's gold answer is compound: CrPC s.174 obliges the officer in charge to
investigate an apparently suspicious death and report the apparent cause
**plus** two figures from our own data — *"**8 FIRs** qatl ki dafa (PPC 302)
ka hawala dete hain, aur **kai zabt shuda property entries** meyyat ke
warisaan ko wapas karne ke liye nishaanzad hain"* — **plus** the honest gap
that no inquest, post-mortem or cause-of-death record exists.

The only aggregate in this system publishing a per-section case count was
`statute_court_stage_join`: a whole-caseload, two-view report whose statute
list is capped at `_STATUTE_RENDER_LIMIT = 15` rows and which carries
`PPC §302: 10 case(s)` as **row six**, alongside a court-stage half KB9 never
asked about. Module 39 wired it anyway and recorded the cost: one live run
named the section without its count, and another **read past the row and
asserted that no listed section pertains to death** — factually wrong, from a
chunk that held the right row.

### 1.1 The probe — re-derived independently, not inherited

The brief explicitly asked for an independent re-derivation rather than a
re-use of Module 39's. `scratchpad/probe74.py`, hand-written Cypher against
the live AGE graph:

```cypher
MATCH (s:StructuredRecord)-[:BELONGS_TO_CASE]->(c:Case)
WHERE s.record_type = 'fir_section'
RETURN s.act AS act, s.section_code AS section_code, c.case_id AS case_id
```

218 rows across **73 distinct cases**, 36 distinct `act §section` labels.
The top of the table:

| section | FIRs |
|---|---|
| PPC §34 | 40 |
| Arms Ordinance 1965 §13 | 29 |
| PPC §392 | 21 |
| CNSA 1997 §9(c) | 12 |
| PPC §420 | 11 |
| **PPC §302** | **10** |
| PPC §337-A(i) | 10 |
| PECA 2016 §14 | 9 |

The ten FIRs citing PPC 302: `fir-202-26`, `fir-213-26`, `fir-214-26`,
`fir-218-26`, `fir-340-25`, `fir-409-26`, `fir-421-26`, `fir-431-26`,
`fir-466-26`, `fir-77-26`.

Two independent checks that 10 is not an artefact:

- **No double counting.** There are exactly **10 rows** with
  `section_code = '302'` and **10 distinct case_ids** among them — one row
  per case, so counting rows and counting cases give the same number.
- **No section-code variant is being missed.** The only section code in the
  whole corpus containing the substring `302` is `302` itself.

| | gold | Module 39 | **this module** | verdict |
|---|---|---|---|---|
| FIRs citing PPC 302 | **8** | 10 | **10** | ❌ **gold diverges** |

### 1.2 Verdict on the 10-vs-8 divergence

**The corpus says 10. Gold says 8. Nothing in this module is tuned toward
8.**

10 vs 8 is a **25 % gap**. The brief's judging standard is explicit that
tolerance is for phrasing, not magnitude — numbers compare by size at roughly
5–10 %, and 25 % is outside that. This is now the **third independent
derivation** of 10 (Module 39 probed it twice by different routes, this
module a third time from a fresh script), against a single unsupported 8 in
the gold answer. **Recommendation: correct gold's KB9 answer to 10.** Modules
34, 35 and 43 each reported a divergence of this shape and two gold answers
have already been corrected as a result; `evaluation/` is out of bounds for
this module, so the correction is filed rather than made.

The aggregate reports **10** and will keep reporting whatever the corpus
holds.

### 1.3 KB9's second data element — does this module change it?

**No, and it does not need to.** Gold's *"kai zabt shuda property entries
meyyat ke warisaan ko wapas karne ke liye nishaanzad hain"* is
`seized_property_disposition`'s own heirs figure, which **already exists**
and which Module 39 already reaches for KB4. So KB9 does not need a better
aggregate for that half; it needs a **two-aggregate plan**, and
`_KB_DATA_HALF_PLANS` entries are single-aggregate by construction. That is a
`rag.py` shape change, filed as **Module 79**. This module leaves that
element exactly where Module 39 left it: computable, unreached.

(Noted while probing: `malkhana_register.disposition` is `None` on all 45
rows — `_seized_property_disposition()` reads a different, populated field
and its own tests pin the heirs figure, so this is an observation about the
schema, not a defect in that aggregate.)

---

## 2. Change

| File | Why here |
|---|---|
| `src/pipeline/xagg.py` | `_FIR_SECTION_COUNT_TERMS` / `_FIR_SECTION_TERMS`, `_is_fir_section_case_count()`, `_QUERY_SECTION_RES` + `_section_code_in_query()`, `_fir_section_case_count()`, `render_fir_section_case_count()`, the `resolve_aggregate_kind()` entry and the mirrored `run_aggregate()` branch. |
| `src/pipeline/harness/tools/xagg.py` | `AggregateKind` Literal entry + renderer import + render branch. |
| `src/pipeline/orchestrator.py` | The other two render sites. |
| `tests/test_xagg.py` | 16 new tests (§3). |

The aggregate answers the per-section question **and nothing else**, counted
**per FIR** (a case charged twice under one section counts once — the same
denominator rule `_statute_court_stage_join()` uses, and the one gold's "8
FIRs" is expressed in). When the query names a section it **leads** with that
section's count and the case ids, then gives the table for context; when it
names none it gives the table alone. A named-but-absent section produces an
explicit *"No FIR in this corpus cites section X"* rather than silently
vanishing into the table.

Section extraction is **generic** — any act, any code, English / Roman-Urdu /
Urdu script — with nothing special-cased to 302, which is only the section
gold's KB9 happens to name.

### 2.1 The dispatch placement is load-bearing, not cosmetic

Placed **IMMEDIATELY BELOW** `_is_statute_court_stage_join()`.

That is the single most important line in this module. Module 39 already
ships a KB9 plan whose sub-query is *"How many cases are charged under each
FIR section, and how far have those cases got in court?"* with
`expected_kind="statute_court_stage_join"`, and
`rag.py::_run_kb_data_half()` **DROPS** a data half whose aggregate family is
not the one its plan named. Had the new predicate captured that string, KB9
would have gone from Module 39's measured **2 of 3** to **0 of 3** — with
every unit test in this repo still green, and no log line saying why. It is
pinned as `test_module39s_shipped_kb9_sub_query_keeps_m4s_family`.

Above it, the entry sits over `_TIME_COMPARISON_KEYWORDS` (M1),
`_TREND_KEYWORDS`' refusal, `_PERSON_KEYWORDS` and `_LIST_ALL_KEYWORDS`.

The Literal entry and all three render sites landed **in the same commit as
the aggregate**, before any live run.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_xagg.py -q
419 passed
```

The tests that carry the argument:

- `test_module76_counts_firs_per_section_and_focuses_on_the_named_one` — the
  focused shape end to end, asserting *"10 of the 40 FIR(s)"* and
  *"cite PPC §302"* in the rendering.
- `test_module76_counts_per_fir_not_per_row` — 3 rows, 2 FIRs, answer 2.
- `test_module76_a_named_but_absent_section_is_said_so_not_hidden`.
- `test_module76_with_no_section_named_it_reports_the_whole_table`.
- `test_module76_section_extraction` — six phrasings including
  `337-A(i)`, `dafa 302` and `دفعہ 302`, plus the None case. Nothing is
  special-cased to 302.
- `test_module76_emits_its_xagg_log_line_with_the_focused_figure` — Module
  55's convention, and here the **focus** is the load-bearing part: the whole
  difference between this family and M4's is the grain, so
  `focus=302 -> 10 FIR(s)` has to be in the line.
- `test_module55_every_aggregate_kind_emits_an_xagg_log_line` and
  `test_module55_log_format_strings_stay_ascii` — **both pass** with the new
  kind (the `§` characters travel as `%s` arguments; the format string is
  ASCII).
- `test_every_new_aggregate_kind_is_accepted_by_the_harness_tool_result` —
  extended with `fir_section_case_count` (the eleventh family).
- `TestFirSectionCountBoundary` — the per-section shape reaches the family in
  three languages; **Module 39's shipped KB9 sub-query keeps
  `statute_court_stage_join`**; M4's **literal gold text** keeps it too; and
  a single signal on its own is not enough in either direction.
- `test_module76_no_gold_question_reaches_the_section_count_predicate` — of
  the 32 gold questions, **none** matches. Deliberate; see §5.
- `test_module56_all_32_gold_questions_resolve_exactly_as_before` — the
  all-32 **EQUALITY** control is **byte-identical before and after this
  module**.

---

## 4. Live verification

### 4.1 The aggregate, through the harness

Three in-process runs of the exact
`XAggToolInput -> xagg_tool -> XAggToolResult` sequence
`rag.py::_run_kb_data_half()` takes — which is the path that matters, and
the one where the `XAggToolResult` Literal trap lives:

| run | status | `aggregate_kind` | chars | `XAGG <kind>` line |
|---|---|---|---|---|
| 1 | `ToolStatus.OK` | `fir_section_case_count` ✅ | 829 | `XAGG fir_section_case_count: 218 section entr(ies) over 73 FIR(s) and 36 distinct section(s); focus=302 -> 10 FIR(s); top=PPC §34=40, Arms Ordinance 1965 §13=29, PPC §392=21, CNSA 1997 §9(c)=12, PPC §420=11` |
| 2 | `ToolStatus.OK` | `fir_section_case_count` ✅ | 829 | identical |
| 3 | `ToolStatus.OK` | `fir_section_case_count` ✅ | 829 | identical |

**Verbatim rendering (identical on all three):**

> *"10 of the 73 FIR(s) that carry a recorded section cite PPC §302.*
> *  - fir-202-26, fir-213-26, fir-214-26, fir-218-26, fir-340-25,
> fir-409-26, fir-421-26, fir-431-26, fir-466-26, fir-77-26*
> *For context, the sections most often cited:*
> *  - PPC §34: 40 FIR(s) … - PPC §302: 10 FIR(s) …"*

3 of 3, non-empty, correct kind — the Literal trap is cleared for this kind.

### 4.2 …and a routing finding, reported rather than worked around

Sent through `/api/chat` on `:8025` as a free-text question, **all three
wordings of the sub-query are classified `SQL` by the LLM router**, not XAGG:

| probe | route | status | wall |
|---|---|---|---|
| *"How many FIRs cite PPC section 302, across all cases?"* | **SQL** | error | 77.9 s |
| *"How many cases cite PPC section 302 in their FIR sections, and how many FIRs cite each section, across all cases?"* | **SQL** | done | 93.3 s |
| *"Aggregate the FIR sections across all cases: how many FIRs cite each section, and how many cite PPC 302?"* | **SQL** | error | 86.3 s |

**This does not affect the composition path**, and that is measured rather
than assumed: a `_KB_DATA_HALF_PLANS` sub-query is handed **straight to
`xagg_tool()`** and never sees the router — Module 39 §2.1 states this as a
deliberate design property ("they cost zero router calls and cannot be stolen
by an override"), and §4.1 above exercises exactly that path. It does mean
this family is unreachable to a user typing the question, which is a
`router.py` matter and out of bounds here. Filed as **Module 80**.

### 4.3 KB9's own gold question

**KB9's data half is still not present live.** Measured 3 of 3, and unchanged
by this module:

| run | route | status | wall | `XAGG <kind>` line | data half |
|---|---|---|---|---|---|
| 1 | **XAGG** | done | 17.5 s | `XAGG graph_recurrence: entity_type=Person, 4 recurring node(s); فیصل=2, طارق=2, شہزیب عرف شابی=2, عاصم رشید=2` | absent |
| 2 | **XAGG** | done | 19.9 s | identical | absent |
| 3 | **XAGG** | done | 13.5 s | identical | absent |

**Verbatim answer (identical on all three):**

> *"The system does record information about deaths in cases, including the
> investigation of the cause of death when a person is found in suspicious
> circumstances. Based on the provided data [Document 1], there are 4
> individuals who appear in multiple cases, some of which may involve deaths.
> However, the specific details about the cause of death or the investigation
> process are not included in the given document…"*

Two things are wrong there and neither is fixable in this module:

1. **KB9 never reaches RAG on this machine today** — it routes to XAGG 3 of
   3. Module 39 measured KB9 on RAG with its data-half plan firing 2 of 3, so
   a `_KB_DATA_HALF_PLANS` entry would not fire even once it exists, and the
   CrPC s.174 statutory half Modules 30/38/52 built for it is lost. Filed
   with KB3's identical finding as **Module 78**.
2. **On the XAGG route KB9 lands on `graph_recurrence_person`** — the bare
   entity-recurrence tier that `xagg.py`'s own comments twice name as a
   wrong-answer bug ("we recognised a noun", not "we recognised the
   question"). It answers with four repeat accused, which is nothing KB9
   asked, and the generation then hedges it into a claim about deaths.

**This module deliberately does NOT widen its predicate to capture KB9's gold
text.** KB9's question names no section and asks no count — matching it would
need signals like the Roman-Urdu word for *death*, which is tuning one
predicate toward one gold question, is exactly what the all-32 control exists
to catch, and would move KB9 off a route it should not be on in the first
place. The right fix is Module 78's routing one plus Module 77's plan entry.

---

## 5. Gold comparison

| | gold | measured | verdict |
|---|---|---|---|
| FIRs citing PPC 302 | **8** | **10** | ❌ **divergence, 25 %, reported not tuned** |
| double counting? | — | none — 10 rows, 10 distinct cases | ✅ checked |
| variant section codes? | — | none — `302` is the only code containing "302" | ✅ checked |
| property entries marked for return to a deceased's heirs | "kai" (several) | not this aggregate's — `seized_property_disposition` already computes it (§1.3) | unchanged by this module |
| no inquest / post-mortem / cause-of-death record | asserted | agreed — no such record type exists (`chalaan_dispatch_count`'s own type scan lists all nine, none of them an inquest) | ✅ agrees |
| CrPC s.174 | statutory half | the RAG path's, and today unreachable (§4.3) | see Module 78 |

**Verdict on the divergence, stated plainly:** gold's 8 is wrong and 10 is
right. Three independent derivations, two of them by different query routes,
and both of the ways an inflated count could arise were checked and
excluded. The right correction is to gold, not to this aggregate — the same
call Modules 34, 35 and 43 made, twice successfully.

---

## 6. Non-gold paraphrase

Written before the sweeps. Neither matches the sub-query's wording, and
neither says "PPC", so the section extractor's non-act branches are exercised
too:

| paraphrase | resolved family | section extracted |
|---|---|---|
| *"Across the caseload, how many FIRs cite dafa 302?"* (Roman-Urdu) | **`fir_section_case_count`** ✅ | `302` ✅ |
| *"کتنے ایف آئی آر میں قتل کی دفعہ 302 درج ہے؟"* (Urdu script) | **`fir_section_case_count`** ✅ | `302` ✅ |
| *"How many first information reports cite section 302 of the Pakistan Penal Code across the whole caseload?"* | **`fir_section_case_count`** ✅ | `302` ✅ |
| *"کتنی ایف آئی آر میں دفعہ 302 کا حوالہ ہے؟"* | **`fir_section_case_count`** ✅ | `302` ✅ |
| *"Which sections have reached the trial stage?"* (section, no count) | correctly **not** this family ✅ | — |
| *"How many FIRs were registered last year?"* (count, no section) | correctly **not** this family ✅ | — |

**6 of 6 behaving as intended**, positives and negatives both. `337-A(i)` is
covered by the extraction test rather than a paraphrase, to prove the
generality claim on a section gold never mentions.

---

## 7. Regression guard

Identical sweep to `MODULE74_RESULT.md` §7 (one session, one backend). The
results that bear specifically on this module:

| Q | runs | result |
|---|---|---|
| **M4** | 2/2 | `route=XAGG`, `XAGG statute_court_stage_join` — **unchanged**. The nearest neighbour and the one this module's placement is designed around |
| **Module 39's KB9 sub-query** | resolver | still `statute_court_stage_join`, pinned as a test — the invisible regression that did not happen |
| CR7 | 2/2 | `criminal_record_court_crosscheck` |
| G3 | 2/2 | `court_readiness_scan` |
| G2 | 2/2 | `case_completeness_scan` |
| G5 | 2/2 | `weapon_compliance_scan` |
| KB4 / KB5 / KB6 | 2/2 each | Module 39's data halves all still dispatch — **45 / 8 / 32** |
| KB1 | 2/2 | unchanged (CrPC ss.154/155) |
| KB2 | 2/2 | `status=error`, the pre-existing verifier rejection, both runs |
| M1, M5 | resolver | `statute_mix_by_year`, `weapon_statute_cooccurrence_by_year` — both carry statute vocabulary, both unmoved |

**All-32 resolver equality: byte-identical.** This module moves nothing.

---

## 8. New defects found

- **Module 77 — the `_KB_DATA_HALF_PLANS` entry for KB9, re-pointed.** Its
  current entry names `statute_court_stage_join`; the better answer is
  `fir_section_case_count` with `_KB9_SQ_FIR_SECTION_COUNT` from
  `tests/test_xagg.py`. Deliberately NOT done here: `rag.py` belongs to the
  live Module 65 track, and the runtime family check makes a half-done
  re-point a silent regression.
- **Module 78 — KB9 (and KB3) never reach the RAG path.** KB9 routes to XAGG
  3 of 3 and lands on `graph_recurrence_person`; KB3 routes to XNETWORK 3 of
  3. Both lose the statutory half and both would fail to fire a data-half
  plan once one exists. Module 39 measured both on RAG.
- **Module 79 — a KB question can need TWO aggregates.** KB9's gold answer
  carries a charging-side figure *and* a property-disposition one.
  `_KbDataHalfPlan` is single-aggregate by construction. Both figures already
  exist; only the plan shape is missing.
- **Module 80 — `"How many FIRs cite PPC section 302…"` routes to SQL.**
  Three wordings, three SQL classifications, 2 of 3 `status=error`, 77–93 s
  each. Harmless to the composition path (which bypasses the router) and
  fatal to a user typing the question. `router.py`, out of bounds here.
- **Gold's KB9 figure should be corrected from 8 to 10** (§1.2, §5).
  `evaluation/` is out of bounds for this module.
