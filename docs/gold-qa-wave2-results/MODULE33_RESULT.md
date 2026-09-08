# Module 33 — G1: seized-property disposition counts

**Question:** G1, element (3) of its gold answer — *"13 items sent to a
forensic lab and 7 held for return to a deceased's heirs"*.
**Branch:** `feature/xagg-g1-caseload-profile-aggregates` (Modules 31–34).
**File:** `src/pipeline/xagg.py` (+ the harness wrapper and the two
orchestrator rendering sites).
**Measured:** 2026-09-08, live stack, port 8006, platform-admin, All Cases.

Modules 31–34 share one live G1 run and one set of infrastructure findings,
written up once in **`MODULE31_RESULT.md` §4.0, §4.2 and §8**.

---

## 1. Root cause

No aggregate existed for the malkhana (property-store) register.

**The plan's hypothesis was wrong about the branch, and this is worth
recording.** The plan states the sub-question *"falls through to
person-recurrence"*. Measured before any code was written
(`scratchpad/dispatch.py`, 2026-09-08):

```
property -> case_listing
```

It hit `_LIST_ALL_KEYWORDS`, not `_PERSON_KEYWORDS` — because the sub-query
ends *"…, across all cases?"* and that contains the literal
`_LIST_ALL_KEYWORDS` entry **"all cases"**. The result was the **unfiltered
73-row corpus dump**, labelled as if it answered the question. Same class of
defect as the plan describes — a confident answer to a question nobody asked
— different branch. The plan's Module 33 section is corrected accordingly.

**Data probe** (hand-written Cypher, before writing the aggregate):

| Disposition (`StructuredRecord.condition`, `record_type='malkhana_register'`) | Items | FIRs |
|---|---|---|
| ضبط شدہ (confiscated) | 14 | 13 |
| سیل بند، نمونہ فرانزک لیبارٹری بھجوایا گیا (sealed, sample **sent to the forensic laboratory**) | **13** | **11** |
| ورثاء کے حوالے کیا جائے گا (**to be handed to the heirs**) | **7** | **7** |
| مدعی کے حوالے کے لیے محفوظ (held for return to the complainant) | 6 | 6 |
| مالخانہ میں مہر بند · محفوظ · فرانزک شواہد کے طور پر محفوظ · مالخانہ میں مہر بند، فرانزک معائنہ مطلوب · ضبط شدہ، فرانزک جانچ کے بعد محفوظ | 1 each | 1 each |
| **Total** | **45** | **28 distinct** |

Gold's two figures — **13** and **7** — reproduce exactly.

One probe detail that changed the implementation: `s.source_case_ref` is
**empty on every malkhana row** (0 distinct values), so the FIR attribution
must come from the `BELONGS_TO_CASE` edge, not from that field. The node
does carry `fir_display_code`, but the edge is the authoritative link and is
what the aggregate uses.

---

## 2. Change

| File | Change |
|---|---|
| `src/pipeline/xagg.py` | `_seized_property_disposition()` + `render_seized_property_disposition()`; `_SEIZED_PROPERTY_KEYWORDS`, `_DISPOSITION_GLOSS`, `_FORENSIC_DISPATCH_TOKEN` / `_FORENSIC_ANY_TOKEN` / `_HEIRS_TOKEN`, `_DISPOSITION_RENDER_LIMIT`; new dispatch branch. |
| `src/pipeline/orchestrator.py` | renderer wired at both XAGG rendering sites. |
| `src/pipeline/harness/tools/xagg.py` | renderer wired at the third site; `"seized_property_disposition"` added to `AggregateKind`. |
| `tests/test_xagg.py` | 18 new tests. |

**Where the fix sits, and why.** The branch is placed **below** G5's
weapon+compliance scan and CR4's weapon-attribution chain, and **above**
`_LIST_ALL_KEYWORDS`.

Seized property and recovered weapons are adjacent subjects and a question
can easily carry both vocabularies; G5 scores 1.0 today and must not move,
so it keeps first claim structurally.
`test_g5_still_reaches_the_weapon_compliance_scan_after_module_33` asserts
that end to end with G5's literal gold text.

Above `_LIST_ALL_KEYWORDS` is the fix for the measured defect.

**The classification rule, published rather than tuned.** This is the part
of the module that could most easily have been curve-fitted to gold's 13.

- **13** entries record the item as literally *dispatched* to the lab
  (`فرانزک لیبارٹری`).
- **16** entries mention a forensic process in *some* wording — the extra
  three being *held as forensic evidence*, *forensic examination required*,
  and *confiscated, held after forensic testing*, none of which is a
  dispatch.

Both are returned. The headline uses the **literal** reading — gold's — and
the renderer says so in the answer:

> Counting rule: 16 entries mention a forensic process in some wording …
> but only 13 record the item as actually sent to the laboratory. The figure
> above uses the literal 'sent to the lab' reading.

`test_seized_property_separates_dispatched_from_merely_forensic` asserts
that sentence exists.

**Items vs FIRs.** Both are returned per disposition and they differ — 13
lab items across **11** FIRs, because two FIRs sent two items each.
Conflating them is the easiest way to report a wrong number here;
`test_seized_property_groups_by_disposition_with_item_and_fir_counts`
asserts the 3-items-in-2-FIRs case explicitly. Gold's "13 items" is the
**item** count.

Entries with a blank disposition are counted separately
(`unrecorded_disposition_count`, **0** on this corpus) rather than folded
into a bucket.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" -m pytest tests/test_xagg.py
```

168 → **186 passed** at this module's commit; **212 passed** after Module 34
and the live fixes. Wider run across ten suites: **665 passed, 1 xpassed**,
no new failures. The full `pytest -q` suite was deliberately not run.

**The regression pinned to the literal dispatched text**
(`test_g1_seized_property_sub_query_no_longer_dumps_the_whole_corpus`):

```python
_G1_SQ_SEIZED_PROPERTY = (
    "How many cases record seized property, and what happens to it — how "
    "many items were sent to a forensic laboratory or held for a deceased's "
    "heirs, across all cases?"
)
```

It asserts `kind == "seized_property_disposition"` **and**
`kind != "case_listing"` — the exact pre-fix result — and
`kind != "graph_recurrence"`.

**Negative control over all 32 gold questions**
(`TestSeizedPropertyBoundary::test_matches_no_gold_question_at_all`) asserts
`matched == []`, an equality, reading
`evaluation/Gold_QA_Dataset_Final32_With_Answers.json` and asserting it
exists and holds 32 items — never the untracked bare
`Gold_QA_Dataset_Final32.json` (PR #21).

**Two collisions this control caught and the family is built around.**

- **KB6** says *"forensics guidelines"*. The family therefore matches only
  `"forensic lab"` / `"forensic laboratory"`, never a bare `"forensic"`.
  KB6's literal text is a parametrised case in
  `test_neighbouring_families_are_not_captured`.
- **KB4** is the seized-property *standards* question and contains
  *"پراپرٹی ریکارڈ"*. English `"property register"` is kept; a bare Urdu
  "پراپرٹی" is not in the family.

G5's literal gold text and the weapon-licence sub-query are also asserted
not to match.

---

## 4. Live verification

Shared setup in `MODULE31_RESULT.md` §4.0.

**Sent** (the literal sub-query, through `/api/chat`):

> How many cases record seized property, and what happens to it — how many items were sent to a forensic laboratory or held for a deceased's heirs, across all cases?

**Route events:** `['XAGG']` · **status** `done` · **21.0 s**

**`backend.log` proof:**

```
2026-09-08 08:01:07,330 [INFO] src.pipeline.xagg: XAGG
seized_property_disposition: 45 register entr(ies) across 28 FIR(s),
9 distinct disposition(s); forensic-lab dispatch 13 item(s) in 11 FIR(s)
(16 mention forensic in any wording); heirs 7 item(s) in 7 FIR(s);
0 entr(ies) record no disposition
```

**Verbatim answer:**

> Across 28 FIR(s), 45 seized property items were recorded, with the following dispositions:
> - **13 items** in **11 FIR(s)** were **sent to a forensic laboratory** (literal "dispatched" action).
> - **7 items** in **7 FIR(s)** are **held for return to deceased persons' heirs**.
>
> These figures are derived directly from the case database [Document 1]. Note that 16 entries involve forensic-related processes (e.g., held as evidence, examination required), but only 13 explicitly record dispatch to the lab.

The 73-row `case_listing` dump is gone, and the counting rule survived
generation intact.

**The shared G1 run:** see `MODULE31_RESULT.md` §4.2. G1 still emits its
original five sub-queries and this aggregate does **not** fire during it.

---

## 5. Gold comparison

Gold G1 element (3): *"seized-property records point to more fatalities than
the charge sheet alone — 13 items sent to a forensic lab and 7 held for
return to a deceased's heirs, alongside 8 murder-section FIRs"*.

| Gold claim | Measured | Verdict |
|---|---|---|
| 13 items sent to a forensic lab | **13** items, in 11 FIRs | **exact match** |
| 7 held for return to a deceased's heirs | **7** items, in 7 FIRs | **exact match** |
| 8 murder-section FIRs | not computed by this aggregate | **out of scope** — PPC 302 counts come from the statute aggregates (Module 23/24); G6's live run reports the PPC breakdown |
| the inference: "points to more fatalities than the charge sheet alone" | not asserted by the aggregate | **left to the reader** — an inference, not a count, and the aggregate does not make it |

**Honest verdict: both countable claims reproduce exactly.** They were
derived from a hand-written Cypher probe *before* the aggregate was written
and were not adjusted afterwards. The one place tuning was possible — "is it
13 or 16?" — is resolved by a published rule, with both numbers in the
answer, rather than by picking the one that matched.

**G1's full verbatim answer** is in `MODULE31_RESULT.md` §5. It contains
none of gold's four findings, including this one, for the reason in §8.1
there.

---

## 6. Non-gold paraphrase

> How many cases have items in the malkhana, and where does that property end up?

Shares no phrase with the dispatched sub-query. **20.7 s, `route='XAGG'`,
`status=done`.**

**The aggregate was correct.** Its log line for this run reads identically
to §4's: `45 register entr(ies) across 28 FIR(s) … forensic-lab dispatch 13
item(s) in 11 FIR(s) … heirs 7 item(s) in 7 FIR(s)`.

**The generated answer was not.** It reported:

> According to [Document 1], there are **2 FIR(s)** involving items in the malkhana (property store) …

The model keyword-filtered the rendered 9-row breakdown down to the two
dispositions whose Urdu text literally contains **"مالخانہ"**, because the
question used that word. The claim verifier caught it and appended:

> _A cited claim ([Document 1]) could not be confirmed against its source: Claim cites figure(s)/identifier(s) not found in its source text: 2._

**Verdict: the routing and the aggregate pass; the synthesis failed and was
flagged.** This is reported, not tuned away — see §8.4 of
`MODULE31_RESULT.md`. It is a generation-layer defect (a model re-filtering
a rendered aggregate on a word from the question), not an aggregate defect,
and fixing it would mean touching generation, which is outside this module's
scope. Three further paraphrases (English "malkhana", English "case property
register by disposition", Urdu "مالخانہ میں رکھی اشیاء") are asserted at the
predicate level in `TestSeizedPropertyBoundary`.

---

## 7. Regression guard

M5 · M4 · G5 · G3 · CR7 · G6 were re-run live after all four modules landed.
Full table and verdicts in **`MODULE31_RESULT.md` §7**.

The one this module could most plausibly break is **G5**, whose vocabulary
(recovered items, register, record-keeping) is adjacent. Re-run live it
returned `['XAGG','XGRAPH','XAGG','XAGG']` and *"30 recovered weapons across
the 32 reviewed entries … without a licence (94%) … 2 entries lack any
licence status"* — unchanged and correct. Also asserted as a unit test with
G5's literal gold text, end to end rather than at the predicate.

---

## 8. New defects found

Shared with Modules 31/32/34 and written up in **`MODULE31_RESULT.md` §8**:

- **§8.1** the four aggregates are not yet wired into G1's `caseload_review`
  plan — blocked on `meta_analysis.py` (another track) and the
  `_MAX_SUB_QUERIES = 5` cap. **The single reason G1's answer is unchanged.**
- **§8.2** `AggregateKind` is a hand-maintained `Literal`; a missing entry
  is a silent empty answer.
- **§8.3** `_XGRAPH_OVERRIDE_PATTERNS` swallows any sub-query ending "across
  all cases".
- **§8.4** generation can silently re-filter a rendered aggregate — **this
  module's paraphrase run is the worked example** (§6).

Module-specific:

### 8.7 `malkhana_register.source_case_ref` is empty on every row

All 45 rows have no `source_case_ref`, so the field is unusable for FIR
attribution and this aggregate uses the `BELONGS_TO_CASE` edge instead.
Other readers in this codebase (`_criminal_record_court_crosscheck()` via
`_fir_key()`) *do* rely on `source_case_ref` for their own record types,
where it is populated. Worth checking whether the malkhana projection is
supposed to write it; if so, it is an ingestion gap. Not investigated here —
the edge is authoritative and gives the right answer.

### 8.8 The "13 or 16" ambiguity is a data-quality signal, not just a counting choice

Three malkhana entries record a forensic process in a wording that is
neither "dispatched" nor "returned" (*forensic examination required*, *held
as forensic evidence*, *held after forensic testing*). Whether those items
ever reached a lab is not recorded anywhere. That is a real gap in the
register's own vocabulary, and it means the true "sent to a lab" figure is
somewhere in 13–16 and is not knowable from this data. The answer says which
reading it used; nothing here can narrow it further.
