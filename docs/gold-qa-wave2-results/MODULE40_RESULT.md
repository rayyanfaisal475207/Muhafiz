# Module 40 — M4's verifier rejection: **closed as superseded**

**Branch:** `fix/meta-analysis-reliability-53-57-40` (third of three commits).
**Outcome: superseded. No source change is taken from
`fix/meta-analysis-m4-synthesis-verifier-rejection`.**

The brief's instruction was explicit: *"Cherry-pick or merge that branch's
source changes in, judge them on their merits, and verify against CR3/G6. If
they turn out not to help, close Module 40 as superseded and say so."* They
were merged in, judged, verified against CR3 and G6 over **8 live runs plus a
direct offline measurement of the verifier itself**, and they do not help.
This file is that measurement.

Nothing is lost: branch `fix/meta-analysis-m4-synthesis-verifier-rejection`
remains on origin with its two commits, its 530-line result file and its 410
lines of tests. This module's finding is that its **premise** — that CR3's and
G6's instability is the same citation-attribution problem M4 had — is false,
and that has now been measured rather than assumed.

---

## 1. Root cause

**Two independent premises had to hold for this module's work to be worth
taking. Neither does.**

### Premise A — "M4 is gone, so re-target at CR3/G6". Half wrong, in the
### direction that matters less.

The tracker records Module 41 as having made M4 skip decomposition entirely
(`resolve_aggregate_kind` → `statute_court_stage_join`,
`skips_decomposition=True`). That resolution still holds, re-derived on this
branch. **But M4 does not skip decomposition live.** Run 4 times on this
branch, byte-identical every time:

```
routes = ['XNETWORK', 'XGRAPH', 'XGRAPH']
answer = "No information was found for any part of this question. Checked:
          What is the breakdown of cases by legal section ...; What is the
          breakdown of case progression stages ... in court across all cases?."
```

Module 41's guard is deliberately conditional on `route == "XAGG"`
(`supervisor.py:745`); the live router classifies M4 as **XNETWORK**, so the
guard is never consulted. Re-measured after merging Modules 38/55/56 in, M4
now varies — 2 of 3 runs decompose into Urdu sub-queries that reach XAGG and
RAG and produce a partial answer, 1 of 3 reproduces the empty result above —
but **it never skips decomposition, 7 runs of 7 across both code states.** See
`MODULE53_RESULT.md` §7 for the full table. Module 41's own §8 says as much — *"pinned by the
all-32 negative control, **assuming an XAGG route**"*, and *"M4 was **not
re-run live here**"*. Full evidence in `MODULE53_RESULT.md` §7.

So M4 *is* still reaching Meta-Analysis, and it *is* still failing — with the
exact decomposer output Module 40's own commit message documented in
September. Module 40's decomposer/plan half would very likely fix it.

**It is still not taken, and the reason is structural.** A matched
deterministic plan **vetoes** the Module 41 guard outright:
`_xagg_answers_in_one_call()` returns `False` the moment
`_match_decomposition_plan()` matches (`supervisor.py:653`, and that function's
own comment says "Module 29's deterministic plans win"). Adding
`statute_vs_court_stage` would therefore make M4 decompose on **every** run,
including the runs where the router *does* say XAGG and Module 41's one-call
aggregate is the better answer. That trades a measured failure for a
regression of a merged module, on a question this branch was explicitly told
to regression-guard rather than change. The right fix is one of the three
options listed in `MODULE53_RESULT.md` §8, all of which need `router.py` or
`supervisor.py` — both off-limits here. **Filed for the user; not decided
unilaterally.**

### Premise B — "CR3/G6 fail the same way M4 did". Measured false.

This is the premise the whole re-scope rests on, and it is the one that kills
the module.

M4's failure, per Module 24 and Module 40's own branch, was **citation
attribution**: *"A claim is attributed to Document 1 but is absent from its
text and instead appears in Document 2."* Nothing hallucinated; only the index
wrong. `interchangeable_chunks` exists to give that one case an
attribution-blind second opinion.

CR3's failure is **not that**. Captured verbatim, 6 times consecutively:

> The claim about FIR 65/26's absence from the linkage list is inferred but
> not directly supported by Document 3, which only lists linked cases without…

That is a **negative inference over a complete listing** — "65/26 is not in
this list, therefore it has none" — which is precisely the proposition CR3's
gold answer states. No index is wrong. Blinding the judge to attribution
cannot touch it, and §4b shows directly that it does not.

G6 did not fail at all in this module's runs (4 of 4 answered), so it offered
no rejection to test against either.

---

## 2. Change

**None taken.** For the record, exactly what was evaluated and what happened
to each piece of the branch:

| Piece of `fix/meta-analysis-m4-synthesis-verifier-rejection` | Size | Verdict |
|---|---|---|
| `verifier.py` — `interchangeable_chunks` flag + `_INTERCHANGEABLE_CHUNKS_RULE` + the attribution-blind second opinion | +111 | **Merged in, measured, reverted.** Works as designed; solves a problem CR3/G6 do not have. §4. |
| `tests/test_verifier.py` — 4 tests incl. the hallucination guard | +191 | Reverted with it. Well-written; they pass. |
| `meta_analysis.py` — `interchangeable_chunks=True` at the one call site | ~10 | Reverted with the flag. |
| `meta_analysis.py` — synthesis prompt rule "3" (check the fact is in the sub-answer numbered N) | ~7 | **Merged in, ablated, reverted.** §4c shows it made no measurable difference. |
| `meta_analysis.py` — the `statute_vs_court_stage` deterministic plan for M4 | ~90 | **Not taken.** Would veto the Module 41 guard — §1, Premise A. |
| `prompts/meta_analysis_decomposer.txt` — removing the self-contradiction | ~20 | **Not taken.** Out of this branch's declared scope, and its value is bound to the plan above. |

The branch applied cleanly onto `main` @ `9942db9` (`git apply --3way`, no
conflicts), so "it did not rebase" is not the reason for any of this.

**Why closing beats keeping it "just in case".** The flag is opt-in and cheap,
and that is a real argument for keeping it. Against it: Module 40 was held
back in the first place *because it was never live-verified*, and it has now
been live-verified as **not firing once in 8 runs** and, when forced, **not
changing the verdict**. Shipping it anyway would put the module back in
exactly the state that made it untrustworthy, while adding a second LLM judge
call on every rejection. Module 21 established closing-as-superseded as a
legitimate outcome; this is that.

---

## 3. Unit tests

While the branch was merged in, the full set passed:

```
PYTHONPATH=. python -m pytest tests/test_harness_agent_meta_analysis.py \
    tests/test_verifier.py tests/test_harness_agent_large_scale_aggregate.py \
    tests/test_pipeline.py -q
```

**200 passed, 0 failed** with Module 40's four verifier tests included
(`test_module40_swapped_citation_is_rejected_without_the_opt_in`,
`..._passes_with_interchangeable_chunks`,
`..._hallucinated_claim_is_still_rejected_with_interchangeable_chunks`,
`..._blind_recheck_cannot_rescue_a_deterministic_pre_check_failure`).

**196 passed, 0 failed** on the shipped state, with Module 40 reverted.

The brief's requirement that *"a genuinely hallucinated synthesis must still
be rejected"* is satisfied on the shipped code by
`test_hallucinated_synthesis_is_still_rejected` (pre-existing, Module 25), and
was additionally confirmed **live** against the real LLM judge — see §4b,
where a synthesis inventing a case transfer and a "128 cases" statistic was
rejected 3 times out of 3, with and without Module 40's flag.

---

## 4. Live verification

### 4a. CR3 and G6, 4 runs each, with Module 40 fully applied

Port 8016, platform-admin, All Cases, literal gold text, serialised.

| # | Question | Route → sub-queries | Dispatched → returned | Verifier | Blind re-check fired? | Status |
|---|---|---|---|---|---|---|
| 1 | CR3 | `XNETWORK` → `XAGG`×3 | 3 → 3 | passed 1st pass | **no** | answered, 104.3 s |
| 2 | CR3 | `XNETWORK` → `XAGG`×3 | 3 → 3 | passed 1st pass | **no** | answered, 219.1 s |
| 3 | CR3 | `XNETWORK` → `XAGG`×3 | 3 → 3 | passed 1st pass | **no** | answered, 135.3 s |
| 4 | CR3 | `XNETWORK` → `XAGG`×3 | 3 → 3 | passed 1st pass | **no** | answered, 126.5 s |
| 5 | G6 | `XNETWORK` → `XAGG`×5 | 5 → 5 | passed 1st pass | **no** | answered, 145.2 s |
| 6 | G6 | `XNETWORK` → `XAGG`×5 | 5 → 5 | passed 1st pass | **no** | answered, 158.1 s |
| 7 | G6 | `XNETWORK` → `XAGG`×5 | 5 → 5 | passed 1st pass | **no** | answered, 214.6 s |
| 8 | G6 | `XNETWORK` → `XAGG`×5 | 5 → 5 | passed 1st pass | **no** | answered, 180.8 s |

**8 of 8 answered — and the change under test never executed.** The
attribution-blind pass only runs when the first pass has already rejected;
the first pass passed all eight times, so `grep "attribution-blind"
backend.log` returns nothing.

**The control that settles it.** Module 40 was then reverted entirely and CR3
re-run 4 more times under identical conditions:

| # | Code | Status |
|---|---|---|
| 9 | M40 reverted | answered, 160.6 s |
| 10 | M40 reverted | answered, 143.2 s |
| 11 | M40 reverted | **verifier rejection**, 94.0 s |
| 12 | M40 reverted | answered, 132.7 s |

**3 of 4 without it, 4 of 4 with it — a difference of one run, on a question
`MODULE57_RESULT.md` §1 shows swinging between 0/4 and 4/4 on unchanged code.**
There is no effect here to attribute.

### 4b. The direct measurement — does the second opinion rescue CR3's actual rejection?

Because the trigger condition stopped occurring live, the verifier was
measured **directly**, off the live path, against the exact rejection shape
CR3 produced 6 times. Three inputs, each judged twice — flag off, flag on —
against the real LLM judge (`temperature=0.0`), repeated 3 times:

| Input | `interchangeable_chunks=False` | `interchangeable_chunks=True` |
|---|---|---|
| **Gold-shaped**: "64/26 has CMS-ISB-2026-0341 [Document 3]; 65/26 does not appear in the CMS linkage list, so it has no walk-in complaint [Document 3]" | `grounded=False` — *"The claim about FIR 65/26's absence from CMS linkage is not explicitly confirmed by Document 3"* | **`grounded=False`** — *"…not directly stated in any chunk"* |
| **Swapped attribution** (the M4 shape the flag was built for) | `grounded=False` — *"the accused's linkage to FIRs is **cited to the wrong chunk**"* | `grounded=False`, and the reason **no longer mentions attribution at all** |
| **Hallucinated**: invents a transfer to "the Karachi cybercrime circle" and "a total of 128 cases" | `grounded=False` | **`grounded=False`** — *"Two claims (FIR 65/26 handling and 128-case statistic) are not supported by any chunk"* |

**3 runs of 3, identical verdicts.** Three things follow, and they should be
read together because two of them are in Module 40's favour:

1. **The flag does what it claims.** On the swapped-attribution input the
   strict pass objects to the citation index in so many words, and the blind
   pass's objection moves off attribution entirely. The mechanism is sound and
   the branch's reasoning about it was correct.
2. **It cannot rescue CR3.** CR3's rejection is not about which chunk a claim
   is credited to; it is about whether a complete listing licenses a negative
   claim. Module 40's own rule says so explicitly — *"A claim that appears in
   NO chunk at all is still unsupported"* — and that is exactly the verdict it
   returns here. This is the module working as designed, on a problem it was
   not designed for.
3. **The hallucination guard holds.** The brief required that a genuinely
   hallucinated synthesis still be rejected. It is, under both settings, live,
   3 of 3.

### 4c. Ablation — was the synthesis prompt rule doing the work instead?

CR3 went 2/8 before Module 40 and 4/4 after, which looked like an effect. To
find out which half of Module 40 caused it, the synthesis prompt's rule "3"
was removed while keeping the verifier change, and CR3 re-run 4 times:

| Arm | CR3 runs | Answered | Blind re-check fired |
|---|---|---|---|
| Module 53 only (earlier block) | 8 | 2 | n/a |
| Module 53 + full Module 40 | 4 | 4 | 0 |
| Module 53 + verifier change only, prompt rule ablated | 4 | **4** | 0 |
| Module 53 only (control, later block) | 4 | 3 | n/a |

Removing the prompt rule changed nothing (4/4 either way), and reverting the
whole of Module 40 gave 3/4. **The 2/8 block was temporal, not causal** — all
six of its rejections came back in 64.0–65.7 s against 94–219 s for every
other run in this module, a cluster `MODULE57_RESULT.md` §8 files as its own
observation. Attributing the recovery to Module 40 would have been the exact
mistake this wave's standard exists to prevent.

---

## 5. Gold comparison

Since no change ships, the gold comparison is the shipped code's, not Module
40's. Full tables in `MODULE53_RESULT.md` §5 and `MODULE57_RESULT.md` §5.

**Verbatim, CR3 with Module 40 applied (run 1)** — for the record that its
answers were not *worse*:

> The two cases (FIR 64/26 and FIR 65/26) were **not handled identically**.

matching gold's *"No — not identically"* and, in the body, gold's exact
`CMS-ISB-2026-0341`. **Verbatim, G6 with Module 40 applied (run 1):**

> For the officer joining this caseload, here is a concise orientation note:

with district spread, case mix by year, arrest rate, reporting speed and
weapon licence status — the same five of gold's seven elements Module 50
wired, unchanged. **Module 40 neither improved nor degraded content.** It is
being closed for having no measurable effect, not for causing harm.

---

## 6. Non-gold paraphrase

Both CR3 and G6 were run on paraphrases sharing no distinctive wording with
gold, on the shipped code.

**CR3, inside `record_consistency`'s pattern reach — 2 runs of 2 answered:**

> Were the two online banking fraud FIRs handled the same way in the records,
> or was one processed differently?

`route='XNETWORK'` → Meta-Analysis → 3 sub-queries. Verbatim, run 1:

> The two online banking fraud FIRs (FIR 64/26 and FIR 65/26) were **not
> handled identically** in the records.

and run 2:

> The two FIRs (fir-64-26 and fir-65-26) were **not handled identically** in
> the records.

**G6, likewise — 1 run of 1 answered:**

> Write a brief orientation note for an officer newly posted here — what
> should they expect from the current case load?

`route='XNETWORK'` → Meta-Analysis → 5 sub-queries. Verbatim opening:

> For a newly posted officer, the current caseload is concentrated in **فیصل آباد** (Faisalabad: 19 cases), **لاہور** (Lahore: 18 cases), and **راولپنڈی** (Rawalpindi: 10 cases), with smaller numbers in other districts … The case mix has shifted significantly: while **PPC** (13 cases) and **Arms Ordinance 1965** (13 cases) dominated in 2024, by 2026 **PPC**…

**Neither question's capability is tied to its gold string.** But both plans
have a narrow lexical reach, measured and reported in `MODULE57_RESULT.md` §6
and §8: a CR3 paraphrase asking whether the records are "equally complete"
matches no plan and never reaches Meta-Analysis at all, and the same boundary
exists for G6.

---

## 7. Regression guard

Because nothing ships from this module, the regression surface is Module 53's,
re-run live and reported in `MODULE53_RESULT.md` §7:

| Question | Result |
|---|---|
| **M2** | ✅ unchanged — `route='XAGG'`, `station_caseload_by_specialisation` |
| **G2** | ✅ unchanged — `route='XAGG'`, single dispatch |
| **G5** | ✅ unchanged — `route='XAGG'`, single dispatch |
| **M4** | ⚠️ still does **not** skip decomposition, 7 runs of 7 across this branch's base and current `main` — pre-existing, unchanged by this branch, and the direct reason `statute_vs_court_stage` was not taken. §1. |
| **G1** | 3 of 4 answered, 5 of 5 aggregates on every run |
| **G6** | 4 of 4 answered |
| **CR3** | 7 of 14 answered on the shipped code |

The most important line here is M4's: it is the one question a *wrong* Module
40 decision would have regressed, and it was checked both statically
(`_match_decomposition_plan()` → `None`, so the Module 41 guard is reachable)
and live.

---

## 8. New defects found

1. **M4 reaches Meta-Analysis live on 7 runs of 7**, across this branch's
   base and current `main`. Its route is XNETWORK; Module 41's guard only
   fires on XAGG. On the base it answered "No information was found" every
   time; on current `main` it answers on 2 of 3 runs with gold's statute half,
   losing the court-stage half to a RAG sub-query timeout.
   Three candidate fixes, all needing files outside this branch's scope, are
   set out in `MODULE53_RESULT.md` §8. **This is the module that should
   inherit Module 40's branch** — the decomposer-prompt and
   `statute_vs_court_stage` work is aimed squarely at it, and the only reason
   it was not taken here is the Module 41 interaction, which that module will
   have to decide anyway. **Filed as Module 60.**

2. **CR3's rejection reason — negative inference over a complete listing — is
   unowned after this module closes.** Module 53 removed the timeouts,
   Module 57 identified the reason, Module 40 is measured incapable of fixing
   it. Filed as Module 61, in `MODULE57_RESULT.md` §8, with the note that
   the *validation* gate already hedges the identical claim rather than
   refusing it, so the two gates hold inconsistent standards.

3. **`fix/meta-analysis-m4-synthesis-verifier-rejection` should not be
   deleted.** Its verifier work is measured here as sound and applies cleanly
   to current `main`; it is being left unmerged because its *target* moved,
   not because it is wrong. Whoever takes defect 1 above should start from
   that branch rather than from scratch, exactly as this module was told to.
   That is Module 60.
