# Module 35 — G6: arrest rate has no aggregate

**Question:** G6, the caseload-pace element of its gold answer — *"girftari
sirf har no mein se taqreeban ek FIR par darj hai"* (an arrest is recorded
on roughly **1 in 9** FIRs).
**Branch:** `feature/xagg-arrest-rate-and-fir-listing`.
**File:** `src/pipeline/xagg.py` (+ the harness wrapper and the two
orchestrator rendering sites).
**Measured:** 2026-09-08, live stack, port **8010**, platform-admin, All
Cases, `admin@example.com`.

---

## 1. Root cause

**No aggregate existed for arrests at all.** Measured before any code was
written (`scratchpad/dispatch.py`, live stack, 2026-09-08), the sub-query
fell through `run_aggregate()`'s chain to `_PERSON_KEYWORDS` and was
answered by `graph_recurrence` / `Person` — a ranked list of repeat
offenders ("فیصل and طارق both appear in fir-202-26 and fir-401-26"),
presented as the answer to a question about arrest frequency. The plan
predicted exactly this fall-through and it reproduced.

**The data exists, but not as a boolean.** Hand-written Cypher probe
(`scratchpad/probe35b.py`, re-run independently for this writeup on
2026-09-08 against the live AGE graph):

```cypher
MATCH (p:Person)-[r:INVOLVED_IN]->(i:Incident)-[:BELONGS_TO_CASE]->(c:Case)
WHERE r.role = 'accused'
RETURN r.arrest_status AS arrest_status, c.case_id AS case_id, id(p) AS p_id
```

```
Case nodes: 73
accused edges: 94   distinct accused: 92
cases with >=1 accused edge: 68
edges with blank arrest_status: 0
```

`arrest_status` is **free Urdu text**, and **two of its twelve live values
contain گرفتار while meaning the opposite**:

| Bucket | Edges | FIRs | Values (live counts) |
|---|---|---|---|
| contains گرفتار, affirmative | 14 | **11** | `گرفتار` ×11, `موقع پر گرفتار`, `گرفتار، بعد ازاں سزا یافتہ`, `گرفتار، ڈی این اے مطابقت پر` |
| contains گرفتار, **negated** | 2 | 2 | `تاحال مفرور، گرفتار نہیں ہوا`, `نامزد، گرفتاری کی نوبت نہ آئی` |
| contains گرفتار, **earlier case** | 1 | 1 | `پہلے سے کیس 10 میں گرفتار، اس مقدمے میں بھی نامزد` |
| no arrest token | 77 | 55 | `زیر تفتیش` ×73, `مفرور، اشتہاری کارروائی جاری`, `پہلے سے زیر حراست، اس مقدمے میں بھی نامزد`, `مقام معلوم کرنے کی کارروائی جاری`, `نامزد، تفتیش جاری` |

**Correction to the plan.** The plan's Module 35 section states *"13 distinct
FIRs carry a status containing گرفتار … 73/13 ≈ 1 in 5.6"*. Re-derived live,
the naive containment count is **14 FIRs**, not 13, giving **1 in 5.2**. The
substance of the plan's point — that the naive reading is wrong and the
definition is the module's real work — is unaffected.

---

## 2. Change

| File | Change |
|---|---|
| `src/pipeline/xagg.py` | `_classify_arrest_status()` (the published rule), `_arrest_rate()`, `render_arrest_rate()`; `_ARREST_TERMS` / `_ARREST_RATE_SIGNALS` / `_ARREST_RECURRENCE_EXCLUSIONS` and the three-signal `_is_arrest_rate()`; new dispatch branch **above** `_PERSON_KEYWORDS`. |
| `src/pipeline/orchestrator.py` | `render_arrest_rate()` wired at both XAGG rendering sites. |
| `src/pipeline/harness/tools/xagg.py` | wired at the third site; `"arrest_rate"` added to `AggregateKind`. |
| `tests/test_xagg.py` | 36 new tests. |

**Why the dispatch guard is a predicate, not a keyword tuple.** The bare
arrest vocabulary collides with a gold question **outright**: S3 is
`کیا کسی شخص کو ایک سے زیادہ بار گرفتار کیا گیا ہے؟` — it contains گرفتار and
is a person-**recurrence** question answered correctly today. So
`_is_arrest_rate()` requires three signals: an arrest term, a count/rate
shape, and the **absence** of a repeat-arrest shape. Either of the last two
alone excludes S3; both are kept because this family sits above
`_PERSON_KEYWORDS` and a false positive there silently replaces a working
answer.

---

## 3. The classification rule (published, as the plan requires)

Stated in `_classify_arrest_status()`'s docstring, restated inside the
rendered answer, and returned as structured buckets so a reader can audit it:

> **arrested** = the status contains **گرفتار**, does **not** negate it
> (`نہیں`, `نہ آئی`, `نہ ہو`), and does **not** attribute it to an earlier
> different case (`پہلے سے`).

An FIR counts as an arrest FIR if **at least one** of its accused entries is
classified `arrested`.

The other three buckets are reported, never silently folded away:
`not_arrested_explicit`, `arrested_in_another_case`, `no_arrest_recorded`.

**What each candidate rule produces, on 73 FIRs:**

| Reading | FIRs | Rate |
|---|---|---|
| **this rule** (affirmative گرفتار, this case) | **11** | **1 in 6.6** |
| + arrests made in an earlier case | 12 | 1 in 6.1 |
| naive "contains گرفتار" | 14 | 1 in 5.2 |
| EXACT equality with the bare token `گرفتار` | 8 | 1 in 9.1 |

**Only the last row reproduces gold's 1 in 9, and it is not defensible** — it
discards three entries that record an arrest in as many words (`موقع پر
گرفتار`, `گرفتار، بعد ازاں سزا یافتہ`, `گرفتار، ڈی این اے مطابقت پر`). It is
therefore **not** the rule used. It is returned as `bare_token_fir_count` and
stated in the answer so the discrepancy with gold stays attributable rather
than hidden.

---

## 4. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_xagg.py -q
```

`tests/test_xagg.py`: **212 → 248 passed** for Module 35's commit, and **275
passed** on the branch head after Module 36. Exit 0, no failures.

Ten-suite wider run (`test_xagg`, `test_router`, `test_orchestrator`,
`test_pipeline`, `test_harness_tool_xagg`,
`test_harness_agent_meta_analysis`, `test_harness_agent_large_scale_aggregate`,
`test_harness_supervisor`, `test_harness_types`, `test_answer_token_budgets`):
**767 passed, 1 xpassed, 0 failed.**

**The regression pinned to the literal dispatched sub-query text** —
`test_g6_arrest_sub_query_reaches_the_arrest_rate_not_person_recurrence`:

```
_G6_SQ_ARREST_RATE = (
    "How many cases record an arrest of an accused person, and on how many "
    "is no arrest recorded, across all cases?"
)
```

Plus, in `TestArrestRateBoundary`:

* `test_s3_the_repeat_arrest_gold_question_is_not_captured` — asserts S3
  **does** carry the arrest vocabulary (that is the point) and that both
  guards independently exclude it.
* `test_s3_still_reaches_person_recurrence_after_module_35` — end to end with
  S3's **literal gold text**, asserting `graph_recurrence` / `Person`.
* `test_matches_no_gold_question_at_all` — **the all-32 negative control, an
  equality**: `matched == []` over
  `evaluation/Gold_QA_Dataset_Final32_With_Answers.json`, with
  `len(items) == 32` asserted so a truncated file cannot make it vacuous.
* `test_the_arrest_vocabulary_alone_would_have_matched_s3` — asserts the
  naive version **fails** that control (`== ["S3"]`), so the control is
  meaningful.
* `test_each_new_g1_sub_query_deterministically_routes_to_xagg` — extended to
  pin `router._deterministic_route_override(_G6_SQ_ARREST_RATE) == XAGG`.
* `test_every_new_aggregate_kind_is_accepted_by_the_harness_tool_result` —
  extended with `"arrest_rate"`, the trap that has now bitten five module
  families.

---

## 5. Live verification

Backend on **8010**, `/health` ok (`documents_in_store: 7716`), model server
`/health` 200, `grep -c "rate limit" backend.log` = **0** throughout.

### 5.1 The dispatched sub-query

**Q:** *How many cases record an arrest of an accused person, and on how many
is no arrest recorded, across all cases?*

```
route='XAGG'   sub-agent='Large-Scale Aggregate'   status=done   20.0s
```

`backend.log` — the only proof of *which* aggregate ran:

```
2026-09-08 16:14:19,668 [INFO] src.pipeline.xagg: XAGG arrest_rate: 11 of 73
FIR(s) record an arrest (1 in 6.6); 14 accused entr(y/ies) classified
arrested, 2 explicitly not arrested, 1 arrested in an earlier case;
bare-token reading 8 FIR(s) (1 in 9.1)
```

**Verbatim answer:**

> Across all 73 FIRs:
> - **11 FIRs** record an arrest of at least one accused person (1 in 6.6).
> - **62 FIRs** record **no arrest** (the remaining 62 of 73).
>
> This includes 77 accused entries with no arrest recorded (e.g., "under
> investigation," "absconding"), 14 accused entries with an arrest recorded,
> 2 accused entries explicitly marked as "not arrested," and 1 accused entry
> noting an arrest in an earlier case [Document 1].

### 5.2 Urdu paraphrase

**Q:** *کتنی ایف آئی آر میں ملزم گرفتار ہوا؟* — `route='XAGG'`,
`sub-agent='Large-Scale Aggregate'`, 15.2 s, same `XAGG arrest_rate` log line.

> According to [Document 1], an arrest is recorded in **11 of 73 FIR(s)**.
> … The remaining 62 FIRs have no arrest recorded.
>
> Note: While 14 accused entries explicitly mention arrest, **1 of these
> entries** refers to an accused arrested in an earlier, different case, and
> **2 entries** explicitly state "not arrested" (absconding/no arrest made).
> These are excluded from the final count of FIRs with arrests.

The published rule reached the user, in the user's own words.

---

## 6. Gold comparison — an honest mismatch

| | Gold G6 | Measured |
|---|---|---|
| arrest rate | roughly **1 in 9** FIRs | **1 in 6.6** (11 of 73) |

**Verdict: the aggregate is correct and gold's figure is not reproducible
from a defensible rule.** The only reading that yields 1 in 9 is exact
equality with the bare token `گرفتار` (8 FIRs, 1 in 9.1) — which requires
discarding three entries that state an arrest explicitly. That is almost
certainly how gold's figure was produced, and it is a counting artefact, not
a fact about the corpus.

**This was NOT tuned toward gold.** The rule was written and committed with
its numbers before the discrepancy was reported, the bare-token figure is
returned alongside as `bare_token_fir_count`, and the rendered answer states
both, so a reader can see exactly where the 1-in-9 comes from.

G6 is a *Creative Generation* question whose gold answer has ~8 elements;
this module supplies one of them. **G6's own live answer is unchanged by this
PR** — the aggregate is correct standalone but is not yet reached from G6's
decomposition plan, because that wiring is deferred behind Module 41. See §8.

---

## 7. Non-gold paraphrase

**Q:** *What share of our FIRs actually end in someone being taken into
custody?* — no phrase shared with the dispatched sub-query.
`route='XAGG'`, `sub-agent='Large-Scale Aggregate'`, 50.1 s, same log line.

> According to [Document 1], **11 of 73 FIRs** resulted in an arrest being
> recorded, meaning roughly **1 in 6.6 FIRs** … For comparison: a naive count
> of FIRs containing the word "گرفتار" (without qualifiers) would incorrectly
> report **8 FIRs** (1 in 9.1).

One caveat reported honestly: the model computed a percentage (15.07%) that
is arithmetically right but appears in no source line, so the citation
verifier appended *"Claim cites figure(s)/identifier(s) not found in its
source text: 15.07"*. That is a generation-layer artefact, not an aggregate
error — the aggregate never emits a percentage.

A third paraphrase, `کتنی ایف آئی آر میں ملزم گرفتار ہوا؟`, is in §5.2.

---

## 8. Regression guard

All re-run live on 8010 after both modules landed, in one batch
(`scratchpad/live.py gold`, `scratchpad/live_gold.json`).
`grep -c "rate limit" backend.log` = **0** for the whole batch.

| Question | Route | Sub-agents | Outcome |
|---|---|---|---|
| **M5** | XAGG | Large-Scale Aggregate | ✅ `XAGG weapon_statute_cooccurrence: 32 weapon case(s), 2 year bucket(s)`; answered the 2024→2026 shift correctly |
| **M4** | **RAG** | Semantic Search | ❌ *"The generated answer could not be verified as grounded in the retrieved documents"*, 441.9 s. **Pre-existing** — Module 24 recorded M4 as "completed end to end on only 1 of 5 runs", its route is LLM-decided and usually not XAGG. It never reached XAGG on this run, so this branch cannot be the cause. |
| **G5** | XAGG → **Meta-Analysis** | Meta-Analysis, Data-Quality/Extraction-Coverage, LSA ×2 | ❌ **the known Module 41 regression**, exactly the shape the brief predicted: over-decomposed into three sub-questions, one refused for want of a case scope and **two timed out**. `route='XAGG' -> sub-agent='Meta-Analysis'` + a grounding/coverage failure. **Not this branch** — Module 41 is fixing it in another track. |
| **G3** | XAGG | Meta-Analysis, LSA | ✅ answered — 82 of 94 accused entries missing a relationship, 32 weapon records without a licence status, 9 FIRs without an incident date |
| **CR7** | XAGG | Meta-Analysis, LSA ×3 | ✅ answered — 1 completed (FIR 891-24), 32 in progress, court record consistent |
| **G6** | XAGG | Meta-Analysis, LSA ×5 | ✅ answered end to end — **but see the note below** |
| **S2** | XAGG | Large-Scale Aggregate | ✅ *"تھانہ ماڈل ٹاؤن، لاہور with 7 cases"* — **matches gold exactly**. The gold question Module 36's family was most at risk of swallowing. |
| **S3** | XAGG | Large-Scale Aggregate | ✅ person-recurrence retained, and returns **both** of gold's pairs — شہزیب عرف شابی (891/24, 214/26) and عاصم رشید (64/26, 65/26). The gold question **this** module was most at risk of swallowing. |
| **CR3** | XAGG | Meta-Analysis, LSA ×2 | ❌ *"The synthesized answer could not be verified as grounded in the sub-answers"* — the Module 29 instability, **unchanged**, because Module 36's wiring is deferred (see `MODULE36_RESULT.md` §3). |

**Modules 31–34**, re-run in their own batch on the same backend:

| Sub-query | Route | `backend.log` |
|---|---|---|
| M31 age | XAGG | `XAGG offender_age_profile: 17 of 92 distinct accused carry an age (19 of 94 entries); range 24-49, mean 31.5` ✅ |
| M32 relationship | XAGG | ran; its log line was **lost to the cp1252 defect** in §9 ✅ (answer correct) |
| M33 seized property | XAGG | `XAGG seized_property_disposition: 45 entr(ies) across 28 FIR(s); forensic-lab 13 item(s) in 11 FIR(s); heirs 7 item(s) in 7 FIR(s)` — still reproduces gold's 13 / 7 ✅ |
| M34 time of day | XAGG | `XAGG incident_time_of_day: 64 of 73 carry a datetime; 14 date-only excluded; 50 usable; peak='evening (18:00-23:59)'` ✅ |

### ⚠️ G6's own answer is unchanged by this module — the wiring is deferred

G6 completed end to end (174.7 s, five Large-Scale Aggregate sub-calls), but
**`XAGG arrest_rate` did not fire during that run** — `grep -o "XAGG [a-z_]*"
backend.log` over the batch shows `offender_age_profile`,
`accused_relationship_breakdown`, `seized_property_disposition`,
`incident_time_of_day`, `weapon_statute_cooccurrence` and
`filtered_fir_listing`, and **no `arrest_rate`**. G6's answer covered
districts, case mix, reporting delay, accused gender and weapon licensing —
**not arrests**.

That is expected and is stated rather than glossed: adding the sub-query to
`meta_analysis.py`'s `_DECOMPOSITION_PLANS` is out of scope for this PR
because **Module 41 is editing that dispatch path concurrently** (see
`MODULE36_RESULT.md` §3 for the full boundary). The aggregate is live and
correct **standalone** — §5 proves that with the log line and the answer —
but G6's decomposition does not yet ask for it, so **G6's gold score is
unchanged by this PR**. The sub-query string is pinned in
`tests/test_xagg.py` as `_G6_SQ_ARREST_RATE` for whichever module
consolidates the plans after Module 41 lands.

Unit-level guards that ship with this module: S3 end to end (§4), the all-32
equality control (§4), and the ten-suite wider run (§4).

---

## 9. New defects found (tracked, not fixed here)

1. **Urdu log lines are lost on Windows.** When uvicorn's stdout is
   redirected to `backend.log`, the stream encodes as **cp1252**, so any
   `logger.info` whose formatted output contains Urdu raises
   `UnicodeEncodeError` inside `logging.StreamHandler.emit()` and the handler
   prints `--- Logging error ---` plus the **unformatted template**. Observed
   on this branch's first live run for `XAGG filtered_fir_listing` (Module
   36) and for Module 32's `XAGG accused_relationship_breakdown`, which is
   **still affected**. Module 36's own line was made ASCII-safe with
   `ascii()`; **Module 32's was not touched** (not this module's file region
   to churn). The general fix is a UTF-8 stream handler in the logging
   config. Worth its own module — this is the mechanism the wave relies on
   to prove which aggregate ran.
2. **`_filtered_cases()`'s return annotation is stale** — declared
   `-> list[dict]`, actually returns `tuple[list[dict], list[str]]`.
   Pre-existing, harmless at runtime, deliberately not changed here.
