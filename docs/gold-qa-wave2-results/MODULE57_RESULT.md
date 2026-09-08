# Module 57 — CR3 is non-deterministic at three layers

**Branch:** `fix/meta-analysis-reliability-53-57-40` (second of three commits).
**Outcome: no code change of its own.** Of CR3's three failure layers, one was
removed outright by **Module 53**, one did not reproduce in **20** consecutive
runs, and the third — the synthesis verifier — is real, is now the only one
left, and is **not** fixed by Module 40 either (see `MODULE40_RESULT.md`,
which closes as superseded on a direct measurement). CR3 remains unstable.
This file is the measurement that establishes all of that, and the reason
Module 57 ships no change rather than a speculative one.

**Backend:** port 8016, platform-admin, All Cases, CR3's **literal gold text**.
**Machine state:** a sibling worktree's backend (Module 38) held port 8015
throughout. No provider `429`/`503`/quota/`UNAVAILABLE` line appeared in any
capture, and no `Cutover classification failed` line either.

---

## 1. Root cause

Module 44 filed this from three consecutive CR3 runs that failed three
different ways:

1. `status=error` — "The synthesized answer could not be verified as grounded
   in the sub-answers."
2. `status=error` — "Could not answer sub-question (timed out)" for **both**
   of `record_consistency`'s sub-queries.
3. `route=None` — the XNETWORK relevance gate, "nearest cluster found was
   distance 0.156 against a relevance cutoff of 0.145".

The brief's instruction was to fix Module 53 first and **re-measure before
assuming Module 57 needs its own change**, because "it may shrink to the
routing instability alone". It shrank — but to the *opposite* layer.

Measured across **20 consecutive runs** of CR3's literal gold text on one
backend, serialised: 2 on the base commit, 12 on the shipped Module 53 code,
and 8 on two Module 40 variants (kept in the table because they bear on
layers 2 and 3 identically — Module 40 touches neither).

| Layer | Module 44's finding | Measured here | Verdict |
|---|---|---|---|
| **2. Sub-query timeouts** | both sub-queries timed out on 1 of 3 runs | **0 of 20.** All 3 sub-queries dispatched and returned on every run. | **Removed by Module 53**, exactly as the brief predicted |
| **3. `route=None` (relevance gate)** | 1 of 3 runs | **0 of 20.** `route='XNETWORK'` every time, followed by 3 `route='XAGG'` sub-query events every time. | **Did not reproduce.** Not fixed — *not observed*. See §8. |
| **1. Synthesis not grounded** | 1 of 3 runs | **8 of 20** overall; **7 of 14** on the shipped code | **The whole of the residue, and still open** |

**CR3's dispatch is completely stable, and its instability is entirely at the
last step.** Module 44 recorded CR3's route as unstable because it saw XAGG
twice and `None` once in three runs. Twenty runs show the same top-level
route, the same plan match (`record_consistency`), the same three sub-queries
and the same `filtered_fir_listing` aggregate (Module 36's, placed first by
Module 50) every single time. That is a real narrowing: two of the three
layers Module 44 described are gone from the measurement.

**The residue has one reason, not several.** Every rejection carries
essentially one verbatim verifier reason:

> The claim about FIR 65/26's absence from the linkage list is inferred but
> not directly supported by Document 3, which only lists linked cases without…

That is the verifier rejecting **the exact proposition CR3's gold answer
states**. Gold reads: *"No — not identically. 64/26 has a matching walk-in
complaint (linked via matching case tag CMS-ISB-2026-0341, complainant سعد
الرحمن); 65/26 has none."* The sub-answer is a CMS linkage listing containing
64/26 and not 65/26; concluding "65/26 has none" is a **negative inference
over a complete listing**. The strict judge treats it as unsupported because
no chunk says it in words.

**This is a grounding-judge strictness problem about negative inference** — not
decomposition, not routing, not a timeout, and (measured, not assumed) **not
citation attribution**, which is what Module 40's second opinion arbitrates.
See `MODULE40_RESULT.md` §4b: with Module 40's `interchangeable_chunks` flag
ON, this exact claim is still rejected, 3 times out of 3.

**A timing observation that matters for anyone re-running this.** The
rejections are not uniformly distributed. Six consecutive rejections landed in
one block, all at **64.0–65.7 s** end to end — a tight, anomalous cluster
against the 94–219 s every other run took. Runs before and after that block,
on identical code, answered 1 of 2 and 3 of 4. Whatever produced the fast
block (most plausibly the shared model server returning a degraded or
truncated synthesis quickly) is temporal, not textual. **A four-run sample of
CR3 can therefore land anywhere between 0/4 and 4/4 on unchanged code**, which
is precisely the property Module 44 filed this module to name, and it is still
true.

---

## 2. Change

**None.** No file was modified for this module.

Four things were deliberately **not** done:

- **`xnetwork.py` and its 0.145 cutoff were not touched.** The brief forbids
  it, and independently the measurement gives no grounds: layer 3 did not
  occur once in 20 runs. Module 12 set that threshold from measured distances
  and Modules 21 and 41 both re-confirmed it; moving a threshold to chase a
  failure that will not reproduce is exactly the tuning this wave rules out.
- **No retry loop was added around the synthesis verifier.** That would paper
  over layer 1 rather than judge it, and it would hide the very
  non-determinism this module exists to characterise. It would also convert a
  disagreement about evidential standards into extra latency.
- **The synthesis prompt was not rewritten to avoid the negative claim.**
  Telling the model to stop saying "65/26 has none" would make CR3 pass by
  making it stop answering the question gold asks. That is tuning toward the
  scorer, not a capability fix.
- **`record_consistency`'s sub-queries were not re-composed.** Module 50 wired
  them, they fire 20 of 20, and when the answer survives the verifier it is
  correct — see §4/§5. Nothing about the plan is what fails.

Module 44's own ruling-out was re-checked rather than trusted: every `_SQ_*`
constant still resolves to its original family, CR3's two Module-29
sub-queries still resolve to `graph_recurrence_person` and `cms_fir_linkage`,
and CR3's gold text still resolves to `station_or_category_counts`. Confirmed
by resolving each through `resolve_aggregate_kind()` on this branch.

---

## 3. Unit tests

No new tests, because there is no new code. The suites covering this module's
subject were run on the shipped state and pass:

```
PYTHONPATH=. python -m pytest tests/test_harness_agent_meta_analysis.py \
    tests/test_verifier.py tests/test_harness_agent_large_scale_aggregate.py \
    tests/test_pipeline.py -q
```

**196 passed, 0 failed.**

The regression test pinned to CR3's **literal gold text** already exists and
still passes — Module 29 wrote it and Module 50 extended it:
`test_module29_broad_synthesis_questions_decompose_deterministically` and
`test_module29_literal_sub_query_decomposition_is_pinned` assert that CR3's
exact gold string matches `record_consistency` and dispatches its three
sub-queries. That is the property this module needed to hold, and it holds in
unit test and on all 20 live runs.

---

## 4. Live verification

CR3's literal gold question, sent 20 times:

> In the online banking fraud matter involving two separate victims, was each
> victim's case processed and recorded the same way?

Every run: `route='XNETWORK'` at the top level (→ Meta-Analysis), then three
`route='XAGG'` sub-query events. 3 dispatched → 3 returned, 0 timed out, on
all 20.

| # | Code under test | Elapsed | Status |
|---|---|---|---|
| 1 | base (`main` @ `9942db9`) | 98.5 s | **verifier rejection** |
| 2 | base | 167.8 s | answered |
| 3 | **+M53 (shipped)** | 147.9 s | answered |
| 4 | +M53 | 110.9 s | answered |
| 5 | +M53 | 65.4 s | **verifier rejection** |
| 6 | +M53 | 65.7 s | **verifier rejection** |
| 7 | +M53 | 64.0 s | **verifier rejection** |
| 8 | +M53 | 65.4 s | **verifier rejection** |
| 9 | +M53 | 65.0 s | **verifier rejection** |
| 10 | +M53 | 65.5 s | **verifier rejection** |
| 11 | +M53 +M40 (verifier + prompt rule) | 104.3 s | answered |
| 12 | +M53 +M40 | 219.1 s | answered |
| 13 | +M53 +M40 | 135.3 s | answered |
| 14 | +M53 +M40 | 126.5 s | answered |
| 15 | +M53 +M40 verifier ONLY (prompt rule ablated) | 129.6 s | answered |
| 16 | +M53 +M40 verifier only | 124.6 s | answered |
| 17 | +M53 +M40 verifier only | 133.1 s | answered |
| 18 | +M53 +M40 verifier only | 126.7 s | answered |
| 19 | **+M53 only (control, M40 reverted)** | 160.6 s | answered |
| 20 | +M53 only (control) | 143.2 s | answered |
| 21 | +M53 only (control) | 94.0 s | **verifier rejection** |
| 22 | +M53 only (control) | 132.7 s | answered |

(22 rows: the two base runs plus twenty on this branch's code. "20 consecutive
runs" above refers to the twenty post-base runs.)

Rows 11–18 are why Module 40 could not be credited: with it installed the
first verifier pass simply **passed**, so its attribution-blind second opinion
never ran — 0 activations in 8 runs — and rows 19–22 then answered 3 of 4 with
it reverted. See `MODULE40_RESULT.md`.

**Verbatim answer, run 3** — representative of the successful runs:

> The two cases (FIR 64/26 and FIR 65/26) were **not handled identically**.
>
> - **FIR 64/26** has a matching walk-in complaint recorded in the CMS system (linked to CMS-ISB-2026-0341) [Document 3], whereas **FIR 65/26** does not appear in the CMS linkage list, indicating no corresponding walk-in complaint exists for this case [Document 3].
> - Both FIRs involve the same accused person, **عاصم رشید** [Document 2], and are registered under the **PECA 2016** statute [Document 1]. However, the difference in complaint linkage (present for FIR 64/26 but absent for FIR 65/26) shows they were processed differently.
>
> Thus, the records were **not handled identically** due to the absence of a matching complaint for FIR 65/26.

**Verbatim answer, run 4:**

> No, not identically. The two FIRs (fir-64-26 and fir-65-26) involved in the online banking fraud matter were not processed and recorded the same way.
>
> - **FIR 64/26** has a matching walk-in complaint recorded in the CMS system (CMS-ISB-2026-0341) [Document 3], while **FIR 65/26** does not appear in the CMS linkage list [Document 3].
> - Both FIRs are registered under the PECA 2016 statute and involve accused persons (including عاصم رشید) [Document 2], but the absence of a CMS complaint linkage for FIR 65/26 indicates a difference in processing.
>
> Thus, the records differ in terms of complaint linkage, even though they share the same accused and statutory framework.

**Verbatim served text on every rejecting run** — identical each time:

> The synthesized answer could not be verified as grounded in the sub-answers.

---

## 5. Gold comparison

Gold: *"No — not identically. 64/26 has a matching walk-in complaint (linked
via matching case tag CMS-ISB-2026-0341, complainant سعد الرحمن); 65/26 has
none."*

Judged on thematic coverage, not wording:

| Gold element | Run 3 | Run 4 |
|---|---|---|
| the verdict "no, not identically" | ✅ leads with it | ✅ leads with it |
| 64/26 has a matching walk-in complaint | ✅ | ✅ |
| linked via case tag **CMS-ISB-2026-0341** | ✅ exact | ✅ exact |
| complainant سعد الرحمن | ❌ not named | ❌ not named |
| 65/26 has none | ✅ | ✅ |

**When CR3 answers, it is right** — four of gold's five elements including
gold's exact CMS tag, which Module 29 got wrong (`CMS-KHI-2026-0417`) and
Module 50 fixed. The missing element is the complainant's name, which no
wired sub-query computes.

**The honest verdict is about the rate, not the content.** On the shipped
code: **7 answered of 14**. That is better than the pre-Module-53 picture in
the sense that the *reason* is now singular and identified, and two of the
three failure layers are gone — but a question that refuses half the time
still cannot be scored once and believed, which is the sentence Module 44
filed this for. **Module 57 does not close CR3; it closes the diagnosis.**

**A caveat that sharpens the diagnosis.** Even on successful runs the
validation gate attaches partial-confirmation caveats — verbatim from run 3:

> _A cited claim ([Document 3]) could only be partially confirmed against its source: The source confirms the CMS linkage for FIR 64/26 but does not mention FIR 65/26 or its absence from the CMS list._

That is the *same* objection the verifier makes on the failing runs, arriving
through a different gate. So this is not a flaky judge misreading text — it is
two independent checks consistently identifying a genuinely inferential claim
and disagreeing, run to run, about whether it is fatal. Whether a complete
listing licenses a negative claim over it is a real evidential question, and
the answer this codebase currently gives is "sometimes".

---

## 6. Non-gold paraphrase

Two paraphrases, chosen to sit on either side of `record_consistency`'s
pattern reach. Both run live on the shipped code; full text in
`MODULE40_RESULT.md` §6.

**(a) Inside the reach — the capability check that matters.**

> Were the two online banking fraud FIRs handled the same way in the records,
> or was one processed differently?

`route='XNETWORK'` → Meta-Analysis → `record_consistency`, 3 sub-queries,
**2 runs of 2 answered**, both with gold's verdict:

> The two online banking fraud FIRs (FIR 64/26 and FIR 65/26) were **not
> handled identically** in the records.

Not one content word of gold's own phrasing ("involving two separate
victims", "each victim's case processed and recorded the same way") survives,
and the answer is still right. **CR3's capability is not curve-fitted to the
gold string.**

**(b) Outside the reach — and this is a finding, not a pass.**

> Take the two online banking fraud FIRs with different complainants — are
> their records equally complete, or does one have supporting paperwork the
> other lacks?

`_match_decomposition_plan()` → **`None`**. Live, 2 runs of 2, it never
reaches Meta-Analysis at all: `route='XAGG'`, one dispatch, and the answer is
a statute breakdown followed by an honest refusal —

> The provided document [Document 1] does not include details about the
> completeness of individual FIR records, supporting paperwork, or
> complainant-specific information for any cases.

The same holds for G6: a paraphrase that keeps "orientation note" and
"officer newly posted" matches `orientation_note` and answers correctly
(1 run of 1, district spread and case mix by year); one phrased as "a
constable has just transferred in — brief them on the pile of open cases"
matches nothing and lands on a single XAGG dispatch.

So the deterministic plans Modules 29/50 installed have a **narrow lexical
reach**: they are not tied to the gold string, but they are tied to a small
neighbourhood around it. That is the same class of defect Module 41 filed for
M4 ("reachable only from the literal gold phrasing"), now measured for CR3
and G6 too. **Filed in §8.** It is not a Module 57 fix — widening a pattern
list to catch a paraphrase this module chose would be curve-fitting of a more
elaborate kind — but it bounds what any of these questions' pass rates mean.

---

## 7. Regression guard

Module 57 changed no code, so its regression surface is Module 53's. Re-run
live and reported in full in `MODULE53_RESULT.md` §7: **M2** ✅ unchanged,
**G2** ✅ unchanged, **G5** ✅ unchanged, **G1/G6** covered by that module's
12-run table (G6 4/4, G1 3/4), and **M4** — which does **not** skip
decomposition live, a correction to the tracker recorded there and repeated
in §8 below.

---

## 8. New defects found

1. **CR3's synthesis-verifier rejection is still open, and now has a name:
   negative inference over a complete listing.** Neither Module 53 nor
   Module 40 addresses it, both measured. The fix is a judgement call nobody
   has made yet — should the grounding judge accept "X is not in this list"
   when the chunk is an exhaustive listing? — and it reaches beyond
   Meta-Analysis, since the same reasoning applies to any XAGG listing served
   as evidence. **Filed as its own module.** Whoever takes it should note
   §5's observation that the *validation* gate already hedges this exact claim
   rather than refusing it, so the two gates have inconsistent standards.

2. **The XNETWORK `route=None` layer could not be reproduced and is therefore
   unresolved, not fixed.** 0 occurrences in 20 runs; Module 44 saw it once in
   three. It may be load-dependent — Module 44's run was during "a long live
   session" by its own account — and Module 42 established that a comparable
   `route=None` was in fact a client-side timeout mis-read as a gate refusal.
   `xnetwork.py` was correctly left alone. **Recorded as an open observation**:
   if it recurs, check first whether it is a timeout, as Module 42 found.

3. **A six-run block of rejections at a uniform 64–66 s, against 94–219 s
   everywhere else.** Strongly suggests a transient degraded response from the
   shared model server rather than anything in the pipeline, but nothing in
   the SSE stream or `backend.log` says which slot served those syntheses.
   This is adjacent to **Module 54**'s point (2) — a silently-invisible
   provider fallback — and is more evidence for it. **Not fixed here.**

4. **The deterministic decomposition plans have a narrow lexical reach.**
   Measured in §6: a CR3 paraphrase keeping "online banking fraud" and
   "handled the same way" matches `record_consistency` and answers correctly
   2/2, while one asking whether the records are "equally complete" matches
   nothing and never reaches Meta-Analysis; the same boundary exists for G6's
   `orientation_note`. Generalises Module 41's M4 finding to CR3 and G6.
   **Filed as a new module** — the fix is not "add more patterns", it is
   deciding whether a deterministic plan should be selected by pattern at all
   or by the same aggregate-resolution mechanism Module 41 used for the guard.

5. **M4 does not skip decomposition live** (route `XNETWORK` 4/4, so Module
   41's XAGG-conditional guard never fires; it answers "No information was
   found" every time). Found while regression-guarding this branch. Full
   evidence and the three candidate fixes are in `MODULE53_RESULT.md` §7–§8.
   **Filed as a new module.**
