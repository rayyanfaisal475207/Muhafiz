# Module 50 — G1/G6/CR3 consolidation: the aggregates existed but nothing called them

**Branch:** `fix/meta-analysis-wire-g1-g6-aggregates` (off `main` @ `687c87a`)
**Questions:** G1 (0.1), G6 (0.3), CR3 (unstable)
**Files:** `src/pipeline/harness/agents/meta_analysis.py`,
`tests/test_harness_agent_meta_analysis.py`
**Measured:** 2026-09-08, live stack, port **8013**, platform-admin
(`admin@example.com`), All Cases (no `case_id`).

---

## 1. Root cause

**The brief's diagnosis was correct in every particular, and nothing had to be
re-derived.** Modules 31–36 each built, unit-tested and live-verified an
XAGG aggregate, and each one deferred the wiring because
`harness/agents/meta_analysis.py` was owned by another track throughout.
Module 31's own result file says it outright: *G1's answer is unchanged*.

The defect is therefore not a wrong answer, a bad route, or a bad aggregate —
**it is an absent call.** Six correct aggregates sat behind three
`sub_queries` tuples that never named them.

Confirmed before any code was written, by dispatching each of the six pinned
sub-query strings through `/api/chat` individually on the **base commit**
(`687c87a`). Every one reached its aggregate and returned gold's numbers:

| Module | Sub-query dispatched alone | Latency | `XAGG <kind>` log line |
|---|---|---|---|
| 31 | offender age | 19.7 s | `offender_age_profile: 17 of 92 distinct accused carry an age; range 24-49, mean 31.5` |
| 32 | accused↔complainant relationship | 26.1 s | `accused_relationship_breakdown: 24 relationship edge(s) across 10 FIR(s); dominant='اجنبی' 15` |
| 33 | seized-property disposition | 12.1 s | `seized_property_disposition: 45 register entr(ies) across 28 FIR(s); forensic-lab 13 item(s); heirs 7 item(s)` |
| 34 | incident time-of-day | 21.7 s | `incident_time_of_day: 64 of 73 carry a datetime; 14 date-only 00:00:00 excluded; 50 usable; peak='evening'` |
| 35 | arrest rate | 14.1 s | `arrest_rate: 11 of 73 FIR(s) record an arrest (1 in 6.6)` |
| 36 | filtered FIR listing | 16.2 s | `filtered_fir_listing: filters=['statute: PECA 2016', ...] -> 2 FIR(s) ['64/26', '65/26']` |

All six correct, all six unreachable from G1/G6/CR3. **`grep -c "rate limit"`
= 0** across this and every run below.

### The one thing the brief did not anticipate

The brief framed step 2 as *"raising the cap is legitimate; raising it without
measuring is not."* Measuring it showed the cap is not really a **cost**
control at all — it is a **timeout** control, and the binding constraint is
much harder than the brief's "latency and model calls per question" framing.
See §2.

---

## 2. Change

One file of production code. `xagg.py`, `supervisor.py`, `router.py`,
`xnetwork.py` and `prompts/evaluator.txt` were **not touched**, per scope.
`prompts/meta_analysis_decomposer.txt` was in scope but needed no change —
the deterministic plans bypass the decomposer LLM entirely.

### 2a. The six sub-queries, copied verbatim

Added to `meta_analysis.py` as named constants, **byte-identical** to the
strings Modules 31–36 pinned in `tests/test_xagg.py`
(`_G1_SQ_ACCUSED_AGE`, `_G1_SQ_RELATIONSHIP`, `_G1_SQ_SEIZED_PROPERTY`,
`_G1_SQ_TIME_OF_DAY`, `_G6_SQ_ARREST_RATE`, `_CR3_SQ_FIR_LISTING`). A new
test asserts the two copies stay equal in both directions, so a reword in
either file fails loudly instead of quietly detaching a plan from its
aggregate.

Not rewording them is load-bearing, for the reason Modules 31–34 caught
**live**: `router.py`'s `_XGRAPH_OVERRIDE_PATTERNS` contains
`across.{0,15}cases`, which steals any sub-query whose *"…, across all
cases?"* suffix is the first override to match. Each string leads with *"How
many cases …"* so `_XAGG_OVERRIDE_PATTERNS` wins outright — zero router LLM
calls per dispatch, and no drift onto the entity-linkage path.

### 2b. Where each one went

| Plan | Question | Sub-queries after this module |
|---|---|---|
| `record_consistency` | CR3 | **`_SQ_FIR_LISTING_CYBER`** (M36), `_SQ_PERSON_RECURRENCE`, `_SQ_CMS_LINKAGE` |
| `orientation_note` | G6 | `_SQ_DISTRICT_SPREAD`, `_SQ_CASE_MIX_BY_YEAR`, **`_SQ_ARREST_RATE`** (M35), `_SQ_REPORTING_SPEED`, `_SQ_WEAPON_LICENCE` |
| `caseload_review` | G1 | **`_SQ_ACCUSED_AGE`** (M31), **`_SQ_RELATIONSHIP`** (M32), **`_SQ_SEIZED_PROPERTY`** (M33), **`_SQ_TIME_OF_DAY`** (M34), `_SQ_PERSON_RECURRENCE` |

CR3's listing is placed **first** so it lands as `[Document 1]` and the
synthesis goal can point at it by number. All three `synthesis_goal` strings
were rewritten to describe the sub-answers the plan now actually produces —
a stale goal is not cosmetic here, it is the only instruction the synthesis
model gets.

**`caseload_review` was RE-COMPOSED, not extended.** This is the substantive
judgement of the module. Gold's G1 states its own method in its first line —
*"profile the accused, the victims, the property and the timing across the 73
FIRs"* — and its four findings are exactly Modules 31–34's four aggregates.
Module 29's five scans answer a legitimate but **different** question, which
its own result file graded honestly as *"a real analytical answer, but not
gold's analytical answer"*. The contested fifth slot went to
`_SQ_PERSON_RECURRENCE` on one criterion: it is the only one of Module 29's
five that **no other gold question already asks in its own right**.
`_SQ_COMPLETENESS` is G2's literal question and `_SQ_WEAPON_LICENCE` is G5's,
and Module 41 now has both answering correctly on their own.

### 2c. `_MAX_SUB_QUERIES` — settled at 5, with the measurement

**The decision: keep 5.** Split into two constants so the two reasons can
move independently — `_MAX_SUB_QUERIES` (5) bounds the **untrusted** list the
decomposer LLM returns; the new `_MAX_PLAN_SUB_QUERIES` (5) bounds a
hand-authored, code-reviewed plan.

The split is not bookkeeping. Before it, a plan was truncated by the LLM-path
cap, so appending Modules 31–34's four sub-queries to `caseload_review`'s
existing five would have produced a **nine-entry plan of which only the first
five ever dispatched**, with nothing anywhere saying so — a wiring module
whose wiring is invisibly discarded. A unit test now asserts no plan exceeds
its cap, so that is a build-time error rather than a runtime surprise.

**The measured cost, and why it is not what the brief expected.** The
constraint is not model spend. It is that
`META_ANALYSIS_SUBQUERY_TIMEOUT` (60 s) is a **single wall-clock deadline
shared by every sub-query in the fan-out** — `asyncio.gather()` starts all N
at once, and each gets `wait_for(..., timeout=60)` from that same instant.
The sub-queries then **do not overlap**: the shared model server serialises
them into a staircase. So the deadline effectively measures **queue
position**, not sub-query cost.

Measured on this branch, port 8013, from `backend.log` timestamps relative to
the `deterministic decomposition plan '<name>' matched` line:

| N | Plan | Sub-answer completion times | Timeouts | Synthesis |
|---|---|---|---|---|
| 3 | `record_consistency` | +11.2 +16.5 **+25.0** | 0 | grounded |
| 3 | `record_consistency` | +15.5 +20.8 **+29.2** | 0 | grounded |
| 5 | `caseload_review` (baseline) | +25.0 +33.4 +44.3 +50.9 **+56.7** | 0 | grounded |
| 5 | `caseload_review` (baseline) | +21.2 +32.6 +39.2 +45.1 **+53.5** | 0 | grounded |
| 6 | `orientation_note` | +10.9 … **+41.1** | 0 | grounded |
| 6 | `orientation_note` | +27.5 … **+57.7** | 0 | grounded |
| 6 | `orientation_note` | +27.6 +34.1 +45.8 +51.4 **+58.1** | **1** | **NOT grounded** |
| 9 | `caseload_review` | +15.4 +21.9 +28.7 +44.6 **+55.4** | **4** | grounded, 4 findings missing |

Read straight off that table:

- **N=9 costs what it was added to buy.** Four of the nine died at the wall,
  and two of the four were `_SQ_ACCUSED_AGE` and `_SQ_TIME_OF_DAY` — the very
  aggregates the wiring exists to reach. Total latency 191.9 s vs 136.8–159.7 s
  at N=5.
- **N=6 is at the cliff, not past it.** Three runs: two completed with 18.9 s
  and 2.3 s of margin, the third lost a sub-query **and** produced an
  unverifiable synthesis. A 1-in-3 failure rate is not a margin.
- **N=3 is comfortable**, which is why CR3 is the most reliable of the three.

Raising the cap therefore does not buy coverage. It buys the silent loss of
whichever aggregate is served last. **5 is kept, and the measurement is
pinned in `test_module50_the_plan_cap_decision_is_five_and_holds` so raising
it is a deliberate act.**

**The cost of holding the line, stated plainly:** `orientation_note` had to
drop `_SQ_GENDER` to fit Module 35's arrest rate. Gold's *"zyada tar mulzim …
mard hain"* is an element G6 no longer computes. It lost the slot on
gradeable specificity — the arrest rate is a number gold states, the gender
split is a soft descriptor — but it is a real loss, not a free win.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_harness_agent_meta_analysis.py tests/test_harness_supervisor.py \
            tests/test_xagg.py tests/test_harness_tool_xagg.py tests/test_router.py
→ 613 passed

PYTHONPATH=. .../python.exe -m pytest tests/test_harness_*.py \
            tests/test_orchestrator*.py tests/test_pipeline*.py tests/test_verifier*.py
→ 795 passed, 1 xpassed
```

The full `pytest -q` suite is deliberately **not** run — it empties the
`muhafiz_entity_descriptions` Chroma collection.

### New tests (`tests/test_harness_agent_meta_analysis.py`)

| Test | What it pins |
|---|---|
| `..._wired_sub_queries_are_byte_identical_to_the_pinned_strings` | all six strings equal to `tests/test_xagg.py`'s copies, both directions |
| `..._each_aggregate_is_wired_into_the_plan_that_needs_it` | the module's whole point, per plan (G1 ×4, G6 ×1, CR3 ×1) |
| `..._no_plan_exceeds_the_plan_cap_so_none_is_silently_truncated` | the silent-truncation failure mode, plus no duplicate sub-query |
| `..._the_plan_cap_decision_is_five_and_holds` | the cap decision **with its measured table in the docstring** |
| `..._every_wired_sub_query_still_routes_deterministically_to_xagg` | the "How many cases …" wording rule, in the file that changed it |
| `..._wiring_changes_no_plan_boundary` | negative control — no `patterns` tuple was touched, so which questions decompose is unchanged |
| `..._module41_guard_still_lets_all_three_reach_meta_analysis` | the Module 41 interaction (§8 of the brief's step 3) |
| `..._each_question_dispatches_its_whole_plan` | end-to-end dispatch counts: G1→5, G6→5, CR3→3 |

### One existing test changed premise, not assertion

`test_module29_literal_sub_query_decomposition_is_pinned` pins the literal
composition of all three plans — which is exactly what this module changes.
Its three expected tuples were updated and annotated; its **purpose** (*"a
future edit that changes one silently is a behavioural change"*) is unchanged
and still enforced. This is the same distinction Module 41 drew for the two
tests it had to swap.

---

## 4. Live verification

Backend on port **8013**, `admin@example.com` / `MuhafizAdmin2026!`, All
Cases. Whole SSE stream parsed, not the last route event — Meta-Analysis
emits one route event per sub-query, which is what proves the dispatch.

### 4.0 The measurement environment — read this before the numbers

Partway through the live runs, **three other backends came up on ports 8012,
8014 and 8015** and began their own live verifications against the same
shared model server. Free RAM ~3.7 GB. This is visible in the data: the
baseline runs at 17:19 had staircase tails at +53–57 s and **zero** timeouts;
from 18:00 onward the same N=5 plan started losing sub-queries at the 60 s
wall.

**This is contention, not a defect in the wiring, and the log separates the
two cleanly.** The `XAGG <kind>` lines show every wired aggregate computing
successfully on **every single run**:

| Plan | Runs | `XAGG <kind>` fired |
|---|---|---|
| `caseload_review` | 7 | `offender_age_profile` **7/7**, `accused_relationship_breakdown` **7/7**, `seized_property_disposition` **7/7**, `incident_time_of_day` **7/7** |
| `orientation_note` | 6 | `arrest_rate` **6/6** |
| `record_consistency` | 3 | `filtered_fir_listing` **3/3** |

What fails under contention is the *downstream* step — the Large-Scale
Aggregate sub-agent's LLM paraphrase + verifier round trip, which queues
behind its siblings and blows the shared 60 s deadline **after** the
aggregate has already computed. Those log lines are reliable now because PR
#30 fixed the Urdu-log-record bug.

### 4.1 G1 — 3 runs (plus 1 extra)

| Run | Latency | Status | Timeouts | Gold findings covered |
|---|---|---|---|---|
| 1 | 190.9 s | **error** | 1 | — synthesis not grounded |
| **2** | 255.1 s | done | **0** | **all four, plus recurrence** |
| 3 | 258.4 s | done | 2 | (3) and (4) |
| 4 | 297.5 s | done | 3 | (3) and (4) |

**Run 2 verbatim** — the answer this module exists to produce:

> The current caseload reveals several patterns and anomalies warranting
> attention. First, **age data for accused individuals is highly
> incomplete**: of 92 distinct accused, only 17 have recorded ages (**24–49
> years, mean 31.5**), while 75 have no age data [Document 1]. …
>
> Second, **relationships between accused and complainants are predominantly
> with strangers**: **15 of 24** recorded relationships (across 10 FIRs)
> categorize the accused as **“اجنبی” (stranger)** … [Document 2].
>
> Third, **seized property handling shows two distinct paths**: **13 items
> were sent to a forensic laboratory, and 7 are held for deceased heirs** …
> [Document 3].
>
> Fourth, **incident timing clusters in the evening**: **19 of 50** incidents
> with usable timestamps occurred between 18:00–23:59 (38%), followed by the
> afternoon (32%) [Document 4]. Notably, **14 cases have timestamps of
> 00:00:00**, which are excluded from time-of-day analysis … [Document 4].
>
> Finally, **four accused individuals recur across multiple FIRs**: فیصل,
> طارق, شہزیب عرف شابی, and عاصم رشید each appear in **2 cases** … [Document 5].

Route trace: `XNETWORK -> Meta-Analysis`, then `XAGG -> Large-Scale
Aggregate` ×5.

### 4.2 G6 — 4 runs

| Run | Latency | Status | Arrest rate in the answer |
|---|---|---|---|
| 1 | 256.8 s | done | **yes** — "11 of 73 FIRs record at least one arrest; 62 … no arrests" |
| 2 | 235.6 s | **error** | no — arrest-rate sub-query timed out, synthesis not grounded |
| 3 | 312.0 s | done | **yes** — "11 of 73 FIRs record at least one arrest (**1 in 6.6**)" |
| 4 | 202.7 s | done | **yes** — "Only 11 of 73 FIRs record at least one arrest" |

Module 35's aggregate now reaches the user in **3 of 4 runs**. Before this
module it reached the user in **none** — Module 35 confirmed live that G6
completed while its aggregate never fired, because the plan did not ask.

### 4.3 CR3 — 3 runs

| Run | Latency | Status | Verdict | Companion FIR | Case tag |
|---|---|---|---|---|---|
| 1 | 172.6 s | done | "not handled identically" | `fir-65-26` ✅ | **CMS-ISB-2026-0341** ✅ |
| 2 | 198.2 s | done | "No, not identically." | `fir-65-26` ✅ | **CMS-ISB-2026-0341** ✅ |
| 3 | 136.1 s | **error** | — synthesis not grounded | — | — |

**Run 2 verbatim:**

> No, not identically. The two FIRs involved in the online banking fraud
> matter — **FIR 64/26** (linked to CMS-ISB-2026-0341) and **FIR 65/26**
> (unlinked to any CMS complaint) — were not processed identically. **FIR
> 64/26** has a matching walk-in complaint recorded in the CMS system
> ([Document 3]), while **FIR 65/26** does not appear in the linkage list,
> indicating no corresponding complaint was recorded ([Document 3]).

---

## 5. Gold comparison — honest verdict

### G1 — all four gold findings now computable and, on a clean run, all four reported

| Gold finding | Live (run 2) | |
|---|---|---|
| (1) accused all **24–49**, average **~31** | 24–49, **mean 31.5** | ✅ |
| (1) "all Pakistani nationals" | not stated | ❌ **not in the data model** — Module 31 established this; not fixable here |
| (2) "'stranger' dominates" where recorded | **اجنبی, 15 of 24**, across 10 FIRs | ✅ |
| (3) **13** forensic-lab / **7** heirs | **13** / **7** | ✅ **exact** |
| (3) "alongside 8 murder-section FIRs" | not stated | ❌ not asked by any wired sub-query |
| (4) "fairly flat across the day, mild evening lean" | 50 usable: night 1, morning 14, afternoon 16, **evening 19**; 14 date-only 00:00:00 excluded | ⚠️ **deliberately not matched** — see below |

**On finding (4), this module reports against gold on purpose.** Module 34
established that gold's "fairly flat" is a **date-only artefact**: 14 of the
64 incidents carrying a datetime sit at exactly 00:00:00. Excluding those,
the distribution is not flat — it leans evening (19) over afternoon (16),
morning (14), night (1). The live answer states both the distribution and the
exclusion, which is the better-supported reading. **No wording was tuned
toward gold here**, and the plan's synthesis goal deliberately does not ask
for "flat".

**Coverage: 3 of gold's 4 findings reproduced exactly, the 4th corrected with
its evidence.** Two sub-clauses ("all Pakistani nationals", "8 murder-section
FIRs") remain outside what any wired aggregate computes.

Before this module, per Module 29's own measurement, **none** of gold's four
findings was reachable at all.

### G6 — the arrest-rate element now lands; gold's own figure is wrong for this data

| Gold element | Live | |
|---|---|---|
| 73 FIRs across 9 districts | 9 districts enumerated, Faisalabad 19 / Lahore 18 / Rawalpindi 10 | ✅ |
| heavier load in Faisalabad and Lahore | ✅ | ✅ |
| case mix shifted from armed robbery to narcotics/cyber/fraud | PPC 39 (from 13), Arms 16, CNSA 12, PECA 9, PDVA 4, IDA 2 | ✅ |
| mostly men | **not reported** | ❌ **`_SQ_GENDER` dropped for the cap** (§2c) |
| aged 25–40 | not reported | ❌ not wired into G6 (it is in G1's plan) |
| typically strangers to the complainant | not reported | ❌ not wired into G6 (in G1's plan) |
| **arrest recorded on ~1 in 9 FIRs** | **11 of 73, 1 in 6.6** | ⚠️ **reported, and gold's figure disputed** |
| most matters pending in court | not reported | ❌ needs Module 24's court-stage join — not wired, cap-bound |
| fraud/cyber reported long after the event | 15.0 min (2024) → 1401.3 min (2026) | ✅ |
| weapon recovery common, almost always unlicensed | 30 of 32 unlicensed, 2 no status | ✅ |

**5 of 9 elements covered, up from 4** (the arrest rate is new; the gender
element was traded away for it). On the arrest rate itself, Module 35's
published rule measures **1 in 6.6**, not gold's 1 in 9 — reported as a
mismatch, not tuned.

### CR3 — now correct, including the identifier Module 29 got wrong

| Gold | Live (runs 1 and 2) | |
|---|---|---|
| "No — not identically." | "No, not identically." | ✅ |
| 64/26 has a matching walk-in complaint | ✅ | ✅ |
| 65/26 has none | ✅ | ✅ |
| case tag **CMS-ISB-2026-0341** | **CMS-ISB-2026-0341** | ✅ **fixed** — Module 29 quoted `CMS-KHI-2026-0417` |
| complainant سعد الرحمن | not stated | ❌ not computable — `_cms_fir_linkage()` returns a CNIC, not a name |

**Module 36's filtered listing did exactly what it was built for.** Module
29's instability had one identified cause — nothing said which FIRs the
question was about, so the synthesis model guessed. With
`filtered_fir_listing` as `[Document 1]`, both good runs named the pair
directly and picked the right tag out of the linkage list.

---

## 6. Non-gold paraphrase

*"As the on-duty analyst, look over everything currently open and tell me
what's worth a second look"* — Module 29's own G1 paraphrase, four runs.

| Run | Reached Meta-Analysis? | Outcome |
|---|---|---|
| 1 | **no** | fell to the legacy orchestrator — **upstream `503 UNAVAILABLE`** on the router classification call |
| 2 | **yes** — `caseload_review`, 5 dispatches | all sub-queries timed out under contention |
| 3 | **no** | fell to the legacy orchestrator — **upstream `429 RESOURCE_EXHAUSTED`, quota exceeded** |
| 4 | **yes** — `caseload_review`, 5 dispatches | 2 timeouts, synthesis not grounded |

**The paraphrase reaches the right plan** — proven live on runs 2 and 4
(`route='XNETWORK' -> Meta-Analysis` then 5 × `route='XAGG' -> Large-Scale
Aggregate`) and in `test_module50_wiring_changes_no_plan_boundary`. **I could
not capture a clean synthesised paraphrase answer**, and I am not going to
claim one. Both failure modes were environmental and are documented above; on
a quiet machine this is the same dispatch path as G1's run 2.

**Explicit quota note, as the brief asked for.** Two runs failed on the
provider side, not in this code:

```
18:38:34 WARNING src.main: Cutover classification failed, falling back to orchestrator.py:
         503 UNAVAILABLE ... 'This model is currently experiencing high demand.'
18:47:28 WARNING src.main: Cutover classification failed, falling back to orchestrator.py:
         429 RESOURCE_EXHAUSTED ... 'Quota exceeded for metric:
         generativelanguage.googleapis.com/generate_content_free_tier_requests,
         limit: 20, model: gemini-2.5-flash'
```

**`grep -c "rate limit"` returned 0 for both**, because neither message
contains that phrase. The brief's suggested check does not catch this failure
mode — see §8.

---

## 7. Regression guard

All single runs, same backend, same session.

| Question | Result | |
|---|---|---|
| **M2** (Module 25/41's question) | 107.4 s, `done`, **1 dispatch, not decomposed**. "The system's data model does not currently classify police stations as general-purpose vs. specialized … so it is not possible to compare caseload growth rates" | ✅ the honest-refusal shape Modules 25 and 41 established, unchanged |
| **G2** (Module 41) | 20.2 s, `done`, **1 dispatch**. 9 FIRs with no incident date (named), 52 with no investigation status | ✅ Module 41's guard still fires; still not decomposed |
| **G5** (Module 41) | 27.2 s, `done`, **1 dispatch**. 30 of 32 recovered weapons unlicensed, 2 with no status | ✅ unchanged |
| **S2** (Module 36's at-risk question) | 17.9 s, `done`. "ماڈل ٹاؤن، لاہور with 7 cases" | ✅ the filtered listing did not swallow it |
| **S3** (Module 35's at-risk question) | 35.4 s, `done`. Four persons each in 2 FIRs, named with their FIRs | ✅ arrest-rate family did not swallow it |
| **D1** (simple single fact) | 15.7 s, `done`, **1 dispatch**. 73 | ✅ not decomposed |
| Modules 31–36's six sub-queries, dispatched individually | §1 table | ✅ all six correct on the base commit; unchanged by this module, which does not touch `xagg.py` |

Unit-test regression: Module 29's and Module 41's suites pass in full (613
tests across the five directly relevant files, 795 across the broader harness
sweep).

---

## 8. New defects found — left unfixed, tracked separately

### 8.1 `META_ANALYSIS_SUBQUERY_TIMEOUT` measures queue position, not sub-query cost — **proposed as Module 53**

The defect behind every degraded run in §4, and the reason
`_MAX_PLAN_SUB_QUERIES` cannot go above 5.

`asyncio.gather()` starts all N sub-queries at once and each gets
`wait_for(..., timeout=60)` **from that same instant**, but the shared model
server serialises them. A sub-query is therefore killed for being *served
last*, not for being slow — and the measured staircase (§2c) shows the last
slot consuming 53–58 s of the 60 s budget even at N=5, before any contention.
Under contention (§4.0) it fails outright.

The aggregate has **already computed** when this happens — the `XAGG <kind>`
line is in the log for every timed-out sub-query. What is thrown away is only
the LLM paraphrase of a result the system already has.

Two candidate fixes, neither attempted here (both are dispatch-semantics
changes, out of this module's scope, and `config.py` is not in it either):
dispatch in bounded batches so each batch gets a fresh window; or serve the
raw aggregate for a sub-query whose paraphrase times out, which
`large_scale_aggregate.py` already does for a *verifier* rejection.

**This is worth more than one more sub-query slot.** Fixing it would make the
cap decision in §2c reconsiderable on its merits.

### 8.2 Two provider-failure gaps — **proposed as Module 54** (half already fixed by Module 46)

Both of §6's paraphrase failures were provider-side, and neither was caught
by the check every module brief prescribes,
`grep -c "rate limit" backend.log` — it returned **0** for both.

**Half of this was already fixed while this module was running, and the
credit belongs there.** Module 46 (prompted by Module 42 hitting the same
transient Gemini 429) widened the eval preflight to
`grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota"`, having established
that *"Groq says `rate limit`; Gemini says `RESOURCE_EXHAUSTED`"*. Module 50
reproduced that failure independently and by a different route, which is
useful corroboration — but **that half needs no further work.**

The residuals:

1. **`503 UNAVAILABLE` is in neither grep.** Different cause (capacity, not
   quota), same consequence, same invisibility.
2. **A failed cutover classification is invisible in the SSE stream.** Both
   failures hit `src/main.py`'s `Cutover classification failed, falling back
   to orchestrator.py` path, which **changes which sub-agent answers**. The
   G1 paraphrase went from Meta-Analysis to a Cross-Case Linkage
   relevance-gate refusal purely because of this, and nothing in the stream
   said so — indistinguishable from a routing regression without reading
   `backend.log`. `WAVE2_ORCHESTRATION_PROMPT.md` also still prescribes the
   bare `grep -c "rate limit"` for *live module verification*, separately
   from the eval preflight Module 46 fixed.

### 8.3 Three of gold's nine G6 elements have no aggregate reachable within the cap

`_SQ_GENDER` (dropped, §2c), the accused age and relationship profile (wired
into G1's plan, not G6's), and "most matters pending in court" (Module 24's
court-stage join, never wired anywhere). All four fit only if 8.1 (Module 53) is fixed
first. **Not a reason to raise the cap now** — at N=6 the measured failure
rate is 1 in 3, and a timed-out element scores the same as a missing one
while also costing the run its groundedness.

### 8.4 Concurrency discipline was not held during this module's live runs

Four backends (8012–8015) ran concurrently against one shared model server,
against `WAVE2_ORCHESTRATION_PROMPT.md` §2.4's cap of two, only one of which
may hold a live run. Not this module's to fix, but it materially degraded
§4's numbers and would degrade any other track measuring at the same time.
The clean baseline runs (17:19, zero timeouts) and the contended runs (18:00+)
are the same code.
