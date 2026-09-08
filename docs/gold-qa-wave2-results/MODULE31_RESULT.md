# Module 31 — G1: offender age profile

**Question:** G1 (`Acting as a crime analyst, review our current caseload and
flag anything that looks unusual or worth monitoring.`), element (1) of its
gold answer — the offender profile.
**Branch:** `feature/xagg-g1-caseload-profile-aggregates` (Modules 31–34
together, one commit each).
**File:** `src/pipeline/xagg.py` (+ the harness wrapper and the two
orchestrator rendering sites).
**Measured:** 2026-09-08, live stack, port 8006, platform-admin, All Cases.

Modules 31–34 share one live G1 run and one set of infrastructure findings.
They are written up **once**, here, in §4.0 and §8, and cross-referenced
from `MODULE32_RESULT.md` / `MODULE33_RESULT.md` / `MODULE34_RESULT.md`.

---

## 1. Root cause

`run_aggregate()` answered **every** age question with a hard refusal:

```
Age-based aggregates are not available: accused/witness age is not
currently extracted into this system's data model.
```

**That statement is false, and had been since Module 1d.**
`structured_projection._write_accused()` resolves every accused mention
through `resolve_structured_person()`, which projects `age` onto the
`Person` node. Probed live before any code was written:

| Probe | Value |
|---|---|
| `Person` nodes carrying an `age` | **19** (of 430) |
| Range / mean over those 19 | **24 – 49, mean 31.84** |
| Accused `INVOLVED_IN` edges | **94** |
| …of which carry an age | **19** |
| Distinct accused persons | **92** |
| …of which carry an age | **17** → range **24 – 49, mean 31.47** |

The brief's hypothesis was right and is confirmed: the refusal is stale.
The one correction to the plan's framing is the **denominator**. The plan
quotes mean **31.8**, which is the corpus-wide `Person` reading; the
accused-scoped reading — the one gold's claim is actually about — is
**31.5** over 17 distinct accused. Both round to gold's "~31"; this module
publishes the accused-scoped figure as the headline and returns the
edge-level denominator alongside it, because the A1 and G3 gold answers are
both expressed in accused **entries** (94), not distinct people (92), and an
answer that used only one of the two would be unreadable against them.

**Measured pre-fix dispatch** (`scratchpad/dispatch.py`, 2026-09-08):

```
age -> unsupported_aggregate  "Age-based aggregates are not available: ..."
```

### The data-model gap this module deliberately did NOT close

Gold G1 also asserts the accused are **"all Pakistani nationals"**. There is
**no nationality field anywhere in this data model** — `Person` carries
name / cnic / gender / age / father_name / address_text / entity_id
(`structured_projection._person_mention()`), and no Postgres column supplies
one. Module 29 recorded this as a data-model gap. Rather than let a
synthesis model infer nationality from Urdu names, `_NATIONALITY_NOT_MODELED`
states the absence outright inside the answer, so the gap travels with the
figures it sits next to.

---

## 2. Change

| File | Change |
|---|---|
| `src/pipeline/xagg.py` | `_offender_age_profile()` + `render_offender_age_profile()` + `_coerce_age()`; `_AGE_KEYWORDS` widened; `_UNSUPPORTED_AGE` reworded and demoted; `_NATIONALITY_NOT_MODELED` added; the `_AGE_KEYWORDS` dispatch branch now calls the aggregate. |
| `src/pipeline/orchestrator.py` | renderer wired at both XAGG rendering sites. |
| `src/pipeline/harness/tools/xagg.py` | renderer wired at the third site; `"offender_age_profile"` added to `AggregateKind`. |
| `tests/test_xagg.py` | 19 new tests; one pre-existing test rewritten. |

**Where the fix sits, and why.** `_AGE_KEYWORDS` is checked **first** in
`run_aggregate()`, ahead of every entity family. That precedence is
deliberate and is **kept**: an age question naming "accused" would otherwise
be swallowed by `_PERSON_KEYWORDS`' recurrence branch. Only the *body* of
the branch changed — refusal → aggregate. `_UNSUPPORTED_AGE` survives inside
the aggregate as the data-driven empty-corpus fallback, the same shape
`_GENDER_NOT_YET_POPULATED` uses: a corpus that later gains ages self-heals
with no code change.

`_coerce_age()` treats a free-text or out-of-range age exactly like a
missing one — it lands in the coverage caveat, never in the mean.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" -m pytest tests/test_xagg.py
```

| Point | Count |
|---|---|
| Baseline at `c797e70` (branch point) | **132 passed** |
| After Module 31 | **150 passed** |
| After Modules 31–34 and the live fixes | **212 passed** |

Wider run (xagg · harness-xagg-tool · orchestrator · router · supervisor ·
large-scale-aggregate · meta-analysis · cross-case-linkage · xgraph ·
xnetwork): **665 passed, 1 xpassed**, no new failures. The full `pytest -q`
suite was deliberately **not** run — it empties the real
`muhafiz_entity_descriptions` Chroma collection.

**The regression pinned to the literal dispatched text**
(`test_g1_age_sub_query_reaches_the_age_profile_not_person_recurrence`):

```python
_G1_SQ_ACCUSED_AGE = (
    "How many cases involve an accused person, and what is their age range "
    "and average age, across all cases?"
)
```

taken verbatim from the plan's Module 31 section. Pre-fix it returned
`{"kind": "unsupported_aggregate"}`; it now returns `offender_age_profile`,
and the test asserts it is neither `graph_recurrence` nor
`unsupported_aggregate`.

**Negative control over all 32 gold questions**
(`TestOffenderAgeProfileBoundary::test_matches_no_gold_question_at_all`)
asserts `matched == []` — an equality, not a containment. `_AGE_KEYWORDS`
must match **no** gold question directly: G1 reaches XAGG only through
Meta-Analysis decomposition, so a direct hit on any gold text would mean the
family had grown too wide. The test reads
`evaluation/Gold_QA_Dataset_Final32_With_Answers.json` and asserts the file
exists and holds 32 items — the bare `Gold_QA_Dataset_Final32.json` is not
tracked in this repo and a test pinned to it silently skips (PR #21).

Two further guards, both added after the live run found what they now
prevent — see §8:

- `test_each_new_g1_sub_query_deterministically_routes_to_xagg`
- `test_every_new_aggregate_kind_is_accepted_by_the_harness_tool_result`

---

## 4. Live verification

### 4.0 Shared setup (referenced by Modules 32–34)

Postgres `muhafiz-postgres` **healthy**; model server `/health` **200**;
Chroma shared read-only (`muhafiz_kb` 7,716). Backend started from the
worktree with `PYTHONPATH=.` on **port 8006**, log to `backend.log`, stopped
afterwards. Free RAM at start ~3.8 GB, one backend only.

XAGG's SSE says only `route='XAGG'` and never which aggregate ran, so each
of the four aggregates emits one `logger.info` line — the convention Modules
23 and 24 established. Those lines are the proof below.

### 4.1 Module 31, live

**Sent** (the literal sub-query, through `/api/chat`):

> How many cases involve an accused person, and what is their age range and average age, across all cases?

**Route events:** `['XAGG']` · **status** `done` · **23.4 s**

**`backend.log` proof:**

```
2026-09-08 08:00:22,245 [INFO] src.pipeline.xagg: XAGG offender_age_profile:
17 of 92 distinct accused carry an age (19 of 94 accused entries);
range 24-49, mean 31.5
```

**Verbatim answer:**

> Across the caseload, there are **92 distinct accused** individuals. Of these, **17 have recorded ages**, which range from **24 to 49 years**, with a **mean age of 31.5**. This data does not reflect the total number of cases, as multiple accused may be involved in a single case. Additionally, **75 of the 92 accused have no age recorded**, meaning the age range and average apply only to a minority of the accused. Nationality data is entirely absent from the dataset [Document 1].

Both caveats survived generation: the coverage gap **and** the nationality
absence.

### 4.2 The shared G1 run

G1's exact gold text was sent end to end (134.4 s, `status=done`):

**Route events:** `['XNETWORK', 'XAGG', 'XAGG', 'XAGG', 'XAGG', 'XAGG']` —
Meta-Analysis matched its deterministic plan and fanned out five XAGG
sub-queries:

```
2026-09-08 08:02:43,084 [INFO] ...meta_analysis: Meta-Analysis:
deterministic decomposition plan 'caseload_review' matched.
```

**No `XAGG offender_age_profile` / `accused_relationship_breakdown` /
`seized_property_disposition` / `incident_time_of_day` line appears anywhere
in G1's section of `backend.log`.** The five sub-queries `caseload_review`
emits today are completeness, person-recurrence, weapon-licence,
case-mix-by-year and criminal-record-vs-court — the same five as before this
PR. G1's verbatim answer is in §5.

**This is the honest headline of the whole PR: G1's answer is unchanged.**
The four aggregates exist, are correct, and are reachable — but wiring them
into `caseload_review`'s `sub_queries` tuple requires editing
`harness/agents/meta_analysis.py`, which is **owned by another track this
wave** and which this PR was explicitly instructed not to touch. Two further
obstacles are recorded in §8 rather than worked around: `_MAX_SUB_QUERIES`
is 5 and that plan is already full.

---

## 5. Gold comparison

Gold G1 element (1): *"the offender profile is unusually uniform — every
accused is between 24 and 49 (average ~31), none younger or older, and all
are Pakistani nationals"*.

| Gold claim | Measured | Verdict |
|---|---|---|
| range 24–49 | **24 – 49** | **match** |
| average ~31 | **31.5** (17 distinct accused) / 31.8 (19 `Person` nodes) | **match** |
| "every accused", "none younger or older" | ages exist for **17 of 92** accused (18%) | **gold overclaims** |
| "all are Pakistani nationals" | field does not exist in the data model | **not derivable** |

**Honest verdict: two of four.** The range and the mean reproduce exactly.
The other two do not, and the aggregate is written to say so rather than to
agree:

- The renderer states *"75 of 92 accused record no age at all, so this range
  describes only the minority that do — it is not evidence that no accused
  is younger or older."* Gold's "unusually uniform" is an inference from an
  18%-coverage sample. The tuned answer here would have been to omit the
  denominator; it is asserted by a test instead.
- Nationality is reported as absent, per §1.

**G1's full verbatim answer** (the shared run, §4.2):

> The current caseload reveals several critical anomalies and trends warranting immediate attention:
>
> 1. **Severe record incompleteness** undermines case tracking and timelines: 9 FIRs lack incident dates entirely [Document 1], and 52 FIRs have no recorded investigation status, leaving their lifecycle untraceable and risking stagnation.
> 2. **Repeat offenders** appear across multiple cases, suggesting potential ongoing criminal networks: **فیصل** and **طارق** both appear in **fir-202-26** and **fir-401-26** [Document 2]. **شہزیب عرف شابی** is linked to **fir-214-26** and **fir-891-24**, while **عاصم رشید** appears in **fir-64-26** and **fir-65-26** [Document 2].
> 3. **Weapons recovered without licences** are rampant: 30 of 32 reviewed weapon-register entries lack licence records, and 2 entries have no licence status at all, raising compliance and safety concerns [Document 3].
> 4. **Case-type diversification** since 2024 shows a sharp rise in **PPC cases** (39 in 2026 vs. 13 in 2024) and the emergence of new categories like **Punjab Domestic Violence Act** (4 cases) and **PECA 2016** (9 cases), indicating shifting legal priorities [Document 4].
> 5. **Court outcome discrepancies** are extreme: only 1 of 33 criminal records (FIR 891-24, **شہزیب عرف شابی**) matches its court outcome, suggesting systemic mismatches in conviction status tracking [Document 5].
>
> These findings highlight urgent gaps in record-keeping, potential repeat offender networks, and evolving case dynamics requiring targeted oversight.

Against gold's four findings — age profile, stranger relationships, seized
property, incident timing — **G1 shares none of them**, exactly as before
this PR. It answers a different, also-true set of findings. This is the
measured position, not an estimate.

---

## 6. Non-gold paraphrase

> How many cases involve someone we have charged, and how old are they on average?

Shares no phrase with the dispatched sub-query. **5.7 s, `route='XAGG'`,
`status=done`:**

> Based on [Document 1], there are 92 distinct accused individuals in the caseload. For 17 of these, ages are recorded, ranging from 24 to 49 years, with a mean age of 31.5. However, 75 of the 92 accused have no recorded age, so this average applies only to the 17 with available data. The document explicitly warns that this range does not reflect the full age distribution of all accused. Nationality data is entirely absent from the dataset.

**Passes.** Same figures, same caveats, from wording that shares nothing with
the gold-derived string — a capability, not a curve fit. Three further
paraphrases (English, Roman-Urdu "umar", Urdu "اوسط عمر") are asserted at the
predicate level in `TestOffenderAgeProfileBoundary`.

---

## 7. Regression guard

Re-run live, on the same backend, after all four modules landed. Full
transcripts in §4's log; verdicts here.

| Q | Owner | Routes | Result | Verdict |
|---|---|---|---|---|
| **M5** | Module 23 | `['XAGG']` | 2024: Arms §13 with PPC 34/392 only; 2026: adds CNSA §9(c) and PPC 302 | **unchanged, correct** |
| **G5** | Module 15 | `['XAGG','XGRAPH','XAGG','XAGG']` | 30 of 32 unlicensed (94%), 2 with no status | **unchanged, correct** |
| **G3** | Module 15/16 | `['XAGG','XAGG']` | relationship blank in **82 of 94**; 2 of 32 weapons; 9 FIRs with no incident date | **unchanged** — 82 is the figure `MODULE24_RESULT.md` already recorded (gold says 81); `_court_readiness_scan()` is byte-identical on this branch |
| **CR7** | Module 14 | `['XAGG'] ×4` | 1 of 33 completed, 32 ongoing (30 under trial); FIR 891-24 consistent | **unchanged, correct** |
| **G6** | Module 29 | `['XNETWORK','XAGG' ×5]` | full orientation note: 9 districts, 73 FIRs, case-mix shift, 15.0 → 1401.3 min reporting delay, 67 M / 24 F, 30 of 32 unlicensed | **unchanged, correct** |
| **M4** | Module 24 | `['XNETWORK','XGRAPH','XGRAPH']`, then `['XNETWORK','XAGG','RAG']` ×2 | statute half only (PPC 61, Arms Ordinance 29); no court-stage half | **pre-existing instability, not a regression** |

**On M4.** `MODULE24_RESULT.md` and the plan's Module 24 section already
record that M4 "completed end to end on only **1 of 5** post-change runs"
and that "M4's route is LLM-decided … which M4 usually is not [XAGG]". Three
runs here reproduce that exactly. The proof this is not a Module 31–34
regression is twofold: `git diff c797e70..HEAD --name-only` touches only
`src/pipeline/xagg.py`, `src/pipeline/orchestrator.py`,
`src/pipeline/harness/tools/xagg.py` and `tests/test_xagg.py` — **not**
`router.py`, `supervisor.py` or `meta_analysis.py`, which decide M4's route;
and `test_m4_gold_text_reaches_the_statute_court_stage_join` still passes,
so M4's behaviour *inside* `run_aggregate()` is unchanged.

`git diff c797e70..HEAD -- src/pipeline/xagg.py` removes exactly **four**
lines, all of them the old `_AGE_KEYWORDS` tuple and the
`_UNSUPPORTED_AGE` refusal branch. Every other aggregate in the file is
byte-identical.

---

## 8. New defects found

Recorded, not folded in.

### 8.1 The four aggregates are not yet wired into G1's plan — **blocked, by design**

`caseload_review` in `harness/agents/meta_analysis.py` still emits its
original five sub-queries, so G1 gains nothing from this PR (§4.2). Adding
them needs that file, owned by another track this wave, **and** a decision
about `_MAX_SUB_QUERIES = 5`: the plan is full, and Module 29 already
recorded a sixth candidate (Module 24's statute×court-stage join) waiting on
the same cap. Module 29's own report names this cap decision as something
"Modules 31-34 need". It is a plan/decomposition change, not an aggregate
change. **Proposed as its own module.**

The four canonical sub-query strings are pinned in `tests/test_xagg.py`
(`_G1_SQ_ACCUSED_AGE`, `_G1_SQ_RELATIONSHIP`, `_G1_SQ_SEIZED_PROPERTY`,
`_G1_SQ_TIME_OF_DAY`) and are each asserted to route deterministically to
XAGG, so whoever wires them can copy them across unchanged.

### 8.2 `AggregateKind` is a hand-maintained `Literal` — fourth repeat of the same silent crash

All four new kinds were missing from `XAggToolResult.aggregate_kind`, so the
harness wrapper raised `pydantic_core.ValidationError: literal_error` and
`main.py`'s top-level handler swallowed it into an **empty answer with
`status=None` and no user-visible error**. Every unit test was green
throughout. Fixed here, and a membership test added — but the underlying
design (a hand-copied Literal that no test forced anyone to update) has now
bitten Modules 13, 22, 23/24 and 31–34. **Deriving it from `run_aggregate()`
is proposed as its own module.**

### 8.3 `_XGRAPH_OVERRIDE_PATTERNS` swallows any sub-query ending "across all cases"

`\bacross\b.{0,15}\bcases\b` matched three of the four first-draft sub-query
strings and beat XAGG, so the aggregates were never reached and the answer
was a cross-case traversal that answered nothing. Worked around by leading
each string with "How many cases …" (which `_XAGG_OVERRIDE_PATTERNS` matches
outright) rather than by widening `router.py`, which is owned by another
track. **The trap is generic** — every future XAGG sub-query in
`meta_analysis.py` written in the natural "…, across all cases?" house style
is one "How many" away from silently landing on XGRAPH. Worth a router-side
fix in its own module.

### 8.4 Generation can silently re-filter a rendered aggregate

Module 33's paraphrase run ("How many cases have items in the malkhana …")
computed the correct 45 items / 28 FIRs — its log line proves it — but the
answer reported **"2 FIR(s)"**, having keyword-filtered the rendered list
down to the two dispositions whose Urdu text literally contains "مالخانہ".
The claim verifier caught it (*"Claim cites figure(s)/identifier(s) not found
in its source text: 2"*), so it did not ship silently. This is a
generation-layer defect, not an aggregate defect, and is not fixed here.
Detail in `MODULE33_RESULT.md` §6.

### 8.5 G3's relationship-completeness figure is 82, gold says 81

`_court_readiness_scan()` reports "blank in 82 of 94", unchanged by this
branch and already recorded at 82 in `MODULE24_RESULT.md`. Module 32's own
read finds **12** distinct accused with a `RELATED_TO` edge (94 − 12 = 82);
gold's 81 implies 13. The two functions count slightly different sets — G3's
counts any `Person` with an `INVOLVED_IN` edge, not only accused. Not
touched here (G3 scores 1.0 and must not move), but the one-off is now
attributable rather than mysterious.
