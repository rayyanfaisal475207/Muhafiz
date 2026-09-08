# Module 32 — G1: accused ↔ complainant relationship breakdown

**Question:** G1, element (2) of its gold answer — *"where an accused–complainant
relationship is recorded at all, 'stranger' dominates"*.
**Branch:** `feature/xagg-g1-caseload-profile-aggregates` (Modules 31–34).
**File:** `src/pipeline/xagg.py` (+ the harness wrapper and the two
orchestrator rendering sites).
**Measured:** 2026-09-08, live stack, port 8006, platform-admin, All Cases.

Modules 31–34 share one live G1 run and one set of infrastructure findings,
written up once in **`MODULE31_RESULT.md` §4.0, §4.2 and §8**. This file
does not repeat them.

---

## 1. Root cause

Two defects, and the plan is right that the second is as serious as the
first.

**(a) No aggregate, no keyword family.** Nothing in `xagg.py` read the
`Person-[:RELATED_TO {role}]->Person` edges.

**(b) A silent fall-through that answered confidently and wrongly.** Measured
before any code was written (`scratchpad/dispatch.py`, 2026-09-08):

```
relationship -> graph_recurrence  Person
```

A relationship sub-question fell all the way down `run_aggregate()`'s
ordered chain to `_PERSON_KEYWORDS` — it contains "accused" — and was
answered by the person-**recurrence** aggregate: a ranked list of repeat
offenders, with no caveat that it had answered a different question. Live,
before the fix, the same string sent through `/api/chat` came back as a
25-FIR cross-case traversal (`route='XGRAPH'`; see §8.3 of
`MODULE31_RESULT.md` for why the live route differed from the in-process
one).

**Data probe** (hand-written Cypher, before writing the aggregate):

| Probe | Value |
|---|---|
| `RELATED_TO` edges | **24** |
| Distinct `role` values | **6** |
| **اجنبی (stranger)** | **15** |
| محلے دار (neighbour) / ساس (mother-in-law) / سینئر ساتھی کار (senior co-worker) / شوہر (husband) | 2 each |
| بھائی (brother) | 1 |
| Distinct FIRs carrying any relationship | **10** |
| Distinct accused with any relationship | **12** of 92 |

This reproduces the plan's figures exactly, including gold's headline
**15 of 24**.

The direction is confirmed from the writer, not assumed:
`structured_projection._write_related_to()` documents *"Direction is always
accused -> victim/complainant"*, written from
`fir_accused.relationship_to_victim` / `.relationship_to_complainant`. That
is the direction gold's claim is stated in.

---

## 2. Change

| File | Change |
|---|---|
| `src/pipeline/xagg.py` | `_accused_relationship_breakdown()` + `render_accused_relationship_breakdown()` + `_case_id_from_source_doc()`; `_RELATIONSHIP_KEYWORDS` and `_RELATIONSHIP_GLOSS` added; new dispatch branch. |
| `src/pipeline/orchestrator.py` | renderer wired at both XAGG rendering sites. |
| `src/pipeline/harness/tools/xagg.py` | renderer wired at the third site; `"accused_relationship_breakdown"` added to `AggregateKind`. |
| `tests/test_xagg.py` | 15 new tests. |

**Where the fix sits, and why.** The branch is placed **below**
`_COURT_READINESS_KEYWORDS` (G3) and `_COMPLETENESS_KEYWORDS` (G2), and
**above** `_PERSON_KEYWORDS`.

G3 is the load-bearing half of that ordering. Its gold answer is about this
*same* `RELATED_TO` data, read as a completeness gap ("the relationship is
blank in 81 of 94 accused entries"), and G3 scores 1.0 today. It keeps first
claim **structurally**, not because the predicates happen not to overlap —
and `test_g3_still_reaches_the_court_readiness_scan_after_module_32` asserts
it end to end, not at the predicate.

Above `_PERSON_KEYWORDS` is the fix for the defect itself.

**Two counting decisions, both published rather than hidden.**

- **Edges are the headline.** 24 is the denominator gold's own "15 of 24"
  reading uses. But one accused row writes **two** edges when the victim and
  the complainant are the same person, so `distinct_pair_count` (**22** on
  this corpus) is returned alongside it and rendered as an explicit note.
- **Jurisdiction scoping uses the edge's own `source_doc_id`**
  (`psrms/fir/fir-312-26#structured` → `fir-312-26`), not a walk to `Case`.
  Measured: walking `(a)-[:BELONGS_TO_CASE]->(:Case)` turns 24 edges into
  **27 rows**, because an accused in two FIRs belongs to two Cases. That
  over-count is asserted against in
  `test_relationship_breakdown_scopes_by_the_edges_own_source_document`.

**The gloss.** `_RELATIONSHIP_GLOSS` maps the six observed Urdu values to
English for a synthesis model and an evaluator that both read English. It is
display-only: the Urdu original is always rendered alongside, and an
**unmapped value passes through verbatim** — asserted by
`test_render_relationship_breakdown_passes_unmapped_values_through_verbatim`.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" -m pytest tests/test_xagg.py
```

150 → **168 passed** at this module's commit; **212 passed** after Modules
33–34 and the live fixes. Wider run across ten suites: **665 passed,
1 xpassed**, no new failures. The full `pytest -q` suite was deliberately not
run (it empties `muhafiz_entity_descriptions`).

**The regression pinned to the literal dispatched text**
(`test_g1_relationship_sub_query_no_longer_falls_through_to_person_recurrence`):

```python
_G1_SQ_RELATIONSHIP = (
    "How many cases record a relationship between the accused and the "
    "complainant, and which relationship is it, across all cases?"
)
```

It asserts `kind == "accused_relationship_breakdown"` **and**
`kind != "graph_recurrence"` — the exact pre-fix result.

**Negative control over all 32 gold questions**
(`TestAccusedRelationshipBoundary::test_matches_no_gold_question_at_all`)
asserts `matched == []`, an equality. Reads
`evaluation/Gold_QA_Dataset_Final32_With_Answers.json` and asserts it exists
and holds 32 items — never the untracked bare
`Gold_QA_Dataset_Final32.json` (PR #21).

**One collision this control caught and the family is built around.** The
bare Urdu **"تعلق"** is a substring of **"متعلق"** ("regarding"), which KB4
uses (*"مقدمے سے متعلق اشیاء"*). It is excluded; only bound forms
("کیا تعلق", "کا تعلق") are matched, and KB4's literal text is a
parametrised case in `test_neighbouring_families_are_not_captured`.

Also asserted: G5, CR2 and CP1-shaped person-vocabulary questions
(person-recurrence, gender breakdown, bare accused total) do **not** match
this family.

---

## 4. Live verification

Shared setup in `MODULE31_RESULT.md` §4.0.

**Sent** (the literal sub-query, through `/api/chat`):

> How many cases record a relationship between the accused and the complainant, and which relationship is it, across all cases?

**Route events:** `['XAGG']` · **status** `done` · **22.1 s**

**`backend.log` proof — the aggregate that actually ran:**

```
2026-09-08 08:00:45,331 [INFO] src.pipeline.xagg: XAGG
accused_relationship_breakdown: 24 relationship edge(s) across 10 FIR(s),
6 distinct value(s); dominant='اجنبی' 15; coverage 12 of 92 distinct accused
```

That line is the whole point: the SSE stream says only `route='XAGG'` and
would look identical if the person-recurrence branch had run instead.

**Verbatim answer:**

> Across all cases, **10 FIR(s)** record a relationship between the accused and the complainant or victim, with **24 relationship entries** in total. The most frequently recorded relationship is **اجنبی (stranger)**, which appears **15 times** across these 10 FIRs. Other recorded relationships include:
> - **ساس (mother-in-law)**: 2 entries (1 FIR)
> - **سینئر ساتھی کار (senior co-worker)**: 2 entries (1 FIR)
> - **شوہر (husband)**: 2 entries (1 FIR)
> - **محلے دار (neighbour)**: 2 entries (1 FIR)
> - **بھائی (brother)**: 1 entry (1 FIR).
>
> Note that **24 relationship entries** correspond to **22 distinct accused↔other-party pairs**, as some FIRs involve multiple victims/complainants with overlapping relationships [Document 1].

The person-recurrence fall-through is gone: no repeat-offender ranking
appears anywhere in the answer, and no `graph_recurrence` line appears in
the log for this query.

**The shared G1 run:** see `MODULE31_RESULT.md` §4.2. G1 still emits its
original five sub-queries and this aggregate does **not** fire during it.

---

## 5. Gold comparison

Gold G1 element (2): *"where an accused–complainant relationship is recorded
at all, 'stranger' dominates, so this is largely stranger-perpetrated crime
rather than disputes between people who know each other, apart from the
domestic-violence cases"*.

| Gold claim | Measured | Verdict |
|---|---|---|
| "stranger" dominates | **اجنبی 15 of 24**, 62.5%, in 6 of 10 FIRs | **match** |
| "where … recorded at all" | 24 entries, 12 of 92 distinct accused | **match** — gold correctly hedges, and so does the answer |
| "apart from the domestic-violence cases" | شوہر (husband) 2, ساس (mother-in-law) 2 — the domestic values, both present | **match** |
| implied "largely stranger-perpetrated crime" | true of the 12 accused with a record; **80 of 92 have none** | **gold's inference is broader than the data** |

**Honest verdict: three of four, with the fourth explicitly qualified rather
than agreed with.** The renderer states *"a relationship is recorded for only
12 of 92 distinct accused … so this describes only the cases where one was
recorded at all — it is not a profile of the whole caseload."* That line is
asserted by `test_render_relationship_breakdown_glosses_urdu_and_states_coverage`,
so it cannot be dropped to make the answer look more like gold's.

The counts were derived from a hand-written Cypher probe **before** the
aggregate was written, and were not adjusted afterwards.

---

## 6. Non-gold paraphrase

> How many cases show whether the accused and the victim knew each other, or were they strangers?

Shares no phrase with the dispatched sub-query. **14.4 s, `route='XAGG'`,
`status=done`**, and the log confirms `accused_relationship_breakdown` ran
with the same 24/10/15/12 figures.

The answer was served as the **raw computed aggregate** — the synthesis
verifier rejected the natural-language paraphrase (*"could not be verified as
an accurate paraphrase; showing the raw computed aggregate instead"*), which
is the designed behaviour for XAGG: the machine-computed evidence is correct
by construction, so a paraphrase failure degrades to the figures rather than
to an abstention. The full breakdown, the dominant value, the coverage
caveat and the duplicate-pair note all reached the user.

**Passes** as a capability check: a wording sharing nothing with the
gold-derived string reached the right aggregate and returned the right
numbers. Three further paraphrases (English, Roman-Urdu "ajnabi", Urdu
"کیا تعلق") are asserted at the predicate level in
`TestAccusedRelationshipBoundary`.

---

## 7. Regression guard

M5 · M4 · G5 · G3 · CR7 · G6 were re-run live after all four modules landed.
Full table and verdicts in **`MODULE31_RESULT.md` §7**.

The one this module could most plausibly break is **G3**, which reads the
same `RELATED_TO` data. Re-run live it returned `['XAGG','XAGG']` and
*"relationship blank in 82 of 94 accused entries"* — the same figure
`MODULE24_RESULT.md` already recorded, and `_court_readiness_scan()` is
byte-identical on this branch (`git diff c797e70..HEAD -- src/pipeline/xagg.py`
removes exactly four lines, all in the AGE branch). Additionally asserted as
a unit test, end to end rather than at the predicate.

---

## 8. New defects found

Shared with Modules 31/33/34 and written up in **`MODULE31_RESULT.md` §8**:

- **§8.1** the four aggregates are not yet wired into G1's `caseload_review`
  plan — blocked on `meta_analysis.py` (another track) and on the
  `_MAX_SUB_QUERIES = 5` cap. **The single reason G1's answer is unchanged.**
- **§8.2** `AggregateKind` is a hand-maintained `Literal`; a missing entry
  is a silent empty answer. Fourth repeat.
- **§8.3** `_XGRAPH_OVERRIDE_PATTERNS` swallows any sub-query ending "across
  all cases" — this module's first-draft wording was one of the three
  casualties.
- **§8.5** G3 reports 82 where gold says 81; this module's own read (12 of 92
  distinct accused with a relationship) is what makes the one-off
  attributable.

Module-specific, and deliberately left as reported facts rather than
"fixed":

### 8.6 The victim/complainant double-write is a projection artefact

5 of the 24 `RELATED_TO` edges are a second copy of a pair already written,
because `_write_accused()` writes one edge from `relationship_to_victim` and
another from `relationship_to_complainant` and, in these FIRs, the victim and
the complainant are the same person. 24 entries cover 22 distinct pairs.

This is arguably an ingestion defect (`structured_projection.py`), not an
aggregate one. It is **not** deduplicated here, because 24 is the
denominator gold and Module 29 both used and silently changing it would make
every prior figure incomparable. The duplication is instead surfaced in the
answer. A dedup at the projection layer belongs in its own module.
