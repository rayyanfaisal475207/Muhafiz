# Module 61 — the grounding verifier refuses a negative inference over a complete listing

**Branch:** `fix/verifier-over-rejection` (two commits).
**Outcome: fixed, and measured before/after on one machine in one session.**
CR3 goes from **4 of 8 answered** to **7 of 8**, and the pre-fix control's
four rejections all carry the FIR 65/26 absence reason — one of them Module
57's verbatim string. The **validation caveat for the identical claim is
gone on 8 of 8**, so the two gates now agree. A hallucinated synthesis is
still rejected, proven both in unit test and live: CR3's one remaining
failure is a *different* claim the verifier correctly refused to serve.

**Backend:** port 8020, this worktree, platform-admin, All Cases.
**Machine state:** no other backend was listening on 8015–8025 at the start
of the batch (`netstat`, empty), so nothing was contended for.
**Provider health:** model server `/health` `{"status":"ok"}` before the
batch. `grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota|UNAVAILABLE|503"`
returns **0** on the pre-fix log and the intermediate log, and **1** on the
final log — that one line is `POST /api/auth/login HTTP/1.1" 200 OK` from
ephemeral **port 62503**, not a provider failure. `Cutover classification
failed`: **0**.

---

## 1. Root cause

The brief's hypothesis was right, and the measurement adds two things it did
not anticipate.

CR3's gold answer asserts a negative — *"64/26 has a matching walk-in
complaint … 65/26 has **none**."* The evidence is a CMS linkage listing
containing 64/26 and not containing 65/26. `prompts/verifier.txt` rule 5
says a claim is unsupported unless a chunk states it, and the judge applied
that rule exactly as written:

> The claim about FIR 65/26's absence from the linkage list is inferred but
> not directly supported by Document 3, which only lists linked cases
> without…

**Rule 5 is right for a narrative chunk and wrong for an exhaustive one.** A
retrieved excerpt is a fragment: "X isn't mentioned here" says nothing about
whether X exists. A complete enumeration is a register, and non-membership
in a register is precisely what the register asserts. That is the whole
distinction, and it is the only thing this module changes.

**What the live control adds to Module 57's account.** Eight runs of CR3's
literal gold text on the pre-fix code, on this machine, this session:
**4 answered, 4 rejected**, and every one of the four rejections is this
same claim, in four wordings:

| # | Verbatim rejection reason |
|---|---|
| 2 | Claims about FIR 65/26's absence from the linkage list and the discrepancy in handling are not supported by any cited chunk. |
| 3 | The claim about FIR 65/26 lacking a complaint linkage relies on absence from Document 3's list, which is not explicitly stated in the chunk. |
| 4 | The claim about FIR 65/26's absence from the linkage list is inferred but not directly supported by Document 3, which only lists linked cases without… |
| 7 | The claim about FIR 65/26 lacking a complaint linkage relies on absence from Document 3's list, which is not explicitly stated in the chunk. |

Row 4 is Module 57's string verbatim. This is a same-day, same-machine
reproduction of the defect, not an inherited number.

**Two things the brief did not name, both found by measurement, both of
which a review-only fix would have shipped broken.**

**(a) The two gates disagree because they check different things, and the
unit matters.** The Validation gate's own flagged unit is the answer
*sentence*. CR3's sentence names a record the listing contains AND one it
does not — *"64/26 has a matching complaint … while 65/26 does not
appear"* — so any rule of the form "every record this claim names must be
absent" can never hold for it. The judge's `reason`, by contrast, states
exactly the one thing it could not confirm. The first shipped attempt
checked the sentence and the caveat survived on **5 of 5** runs; switching
the unit to the reason is what actually reconciles the gates.

**(b) Absence is absence FROM A PARTICULAR REGISTER.** CR3 hands both gates
**three** complete listings at once — a filtered FIR listing naming both
64/26 and 65/26, a CMS linkage listing naming only 64/26, and a
person-recurrence listing. Pooling them made every correct negative look
fabricated, because 65/26 does appear in a sibling listing answering a
different sub-question. The claim has to be checked against the listing it
is about.

**KB4 is a different defect, measured, not assumed.** The brief asked
whether one change addresses both. It does not, and the reason is in
`MODULE38_RESULT.md` §5: KB4's two recorded rejections are *"the answer
incorrectly attributes alignment with the Anti-Rape Act to the property
record system, but no chunk mentions the Anti-Rape Act"* and *"one claim
about property destruction documentation in Register No. 1 lacks direct
support"*. Neither is a negative inference over a listing; both are
over-attribution to a RAG chunk, which is never exhaustive and which this
module deliberately gives no licence to. KB4 measured **3 of 3** and **2 of
2** answering here (§7), better than Module 38's 1-of-3, but nothing in this
change is what did that.

---

## 2. Change

Four files. The fix sits at the **Verifier**, not in Meta-Analysis, because
any XAGG listing served as evidence has this shape.

| File | What changed |
|---|---|
| `src/pipeline/verifier.py` | `EXHAUSTIVE_SCOPE_META_KEY`, the two direction-split absence vocabularies, `_absence_subjects()`, `listings_a_claim_is_about()`, `negative_claim_is_supported_by_exhaustive_listing()`, the `COMPLETE LISTING` marker in the judge prompt, and the deterministic post-pass in `verify_grounding()` |
| `prompts/verifier.txt` | Rule 7 (+ a pointer from rule 5), and worked Examples 5 and 6 — one grounded negative, one fabricated one |
| `src/pipeline/validation.py` | Imports the rule rather than re-deriving it; `_reconcile_exhaustive_negatives()` upgrades a flagged claim on the same test |
| `src/pipeline/harness/agents/meta_analysis.py` | `_sub_answer_is_exhaustive()` and the `exhaustive=` flag on `_pseudo_chunk()` |

**A negative is treated as supported only when all of these hold**, and each
is checked in Python:

1. **The caller declared the chunk a complete enumeration.** Exactly one
   caller does: `meta_analysis.py`, for a sub-answer whose `tools_used` is
   *exactly* `["XAGG"]` — a deterministic aggregate computed over the whole
   corpus by query, not a retrieved sample. A RAG/GRAPH/WEB chunk is never
   exhaustive and there is no path in this code to make it one. A **mixed**
   sub-answer (`{"XAGG","RAG"}`) is not exhaustive either.
2. **The claim is checked against the listing it names.** The judge's own
   "Document 3" reference resolves the register on the Verifier side; the
   Validation gate uses the claim's `document_index` directly. A claim
   pinned to a non-exhaustive document licenses nothing; a claim naming no
   document must be absent from *every* listing.
3. **Every absence phrase resolves to a named, composite identifier**, and
   direction is respected — "…does not mention FIR 65/26" names its record
   *after* the phrase, "FIR 64/26 … does not appear" *before*. Only
   composite ids (`65-26`, `cms-isb-2026-0341`) can be a subject, so
   *"does not mention PECA 2016"* resolves to nothing and keeps its caveat.
4. **Every such record is confirmed absent from that listing's own text.** A
   claim that 64/26 is missing from a listing containing `fir-64-26` is a
   fabricated negative and stays rejected.

**Why the deterministic post-pass exists at all, given the prompt rule.**
Module 57's finding was that the same question passed or failed on a coin
flip; a fix whose only mechanism is another sampled LLM verdict would leave
that property intact. Both halves ship. **Honest note: live, the post-pass
never had to fire** — `Verifier [Module 61]: overturning rejection` appears
**0 times in 22 post-fix runs**, because the prompt rule alone carried the
judge every time. It is proven by unit test, not by a live activation. That
is the same posture `MODULE40_RESULT.md` took about its own second opinion,
and it is reported the same way rather than claimed.

**`xagg.py` was not touched**, as the brief requires. The provenance the fix
needs is already recorded at the sub-agent boundary in
`SubAgentResult.tools_used`, so nothing had to reach into another track's
file.

**Deliberately not done:** no retry loop around the verifier (it would hide
the non-determinism this module exists to remove); no rewrite of the
synthesis prompt to stop CR3 asserting the negative (that would make CR3
pass by making it stop answering the question gold asks); no relaxation of
rule 5 for narrative chunks.

---

## 3. Unit tests

```
PYTHONPATH=. "…/.venv/Scripts/python.exe" -m pytest tests/test_verifier.py \
    tests/test_validation.py tests/test_harness_agent_meta_analysis.py \
    tests/test_pipeline.py tests/test_citation_consistency.py -p no:randomly
```

**260 passed, 0 failed.** 27 of those are new, in `tests/test_verifier.py`.
The three the brief names explicitly:

| Requirement | Test |
|---|---|
| (a) a negative over an exhaustive listing is **accepted** | `test_negative_inference_over_exhaustive_listing_is_accepted` — CR3's exact claim, CR3's exact rejection reason |
| (b) the same claim over a listing **not** marked exhaustive is still **rejected** | `test_same_claim_over_a_non_exhaustive_listing_is_still_rejected` — identical answer, identical judge verdict, identical listing text; the only difference is the declaration |
| (c) a genuinely hallucinated synthesis is **still rejected** | `test_hallucinated_synthesis_is_still_rejected_over_exhaustive_listing` — Module 17's live shape (misattributed names + invented case counts), run against an exhaustive chunk |

Pinned to the question's literal gold text and to live-captured strings:
`test_every_live_captured_absence_reason_resolves_to_the_absent_record`
covers all five absence reasons actually seen on this machine (Module 57's
verbatim string, both pre-fix control wordings, and both live
validation-gate wordings), and `test_reasons_that_must_not_be_rescued`
covers the four that must not be — the two fabricated directions, the live
PECA statute reason, and the live G1 rejection reason.

Also pinned: `test_fabricated_negative_over_exhaustive_listing_is_still_rejected`,
`test_a_negative_about_a_listing_that_contains_the_record_still_fails`,
`test_a_mixed_rejection_is_not_overturned`,
`test_override_never_beats_a_deterministic_pre_check`,
`test_an_off_topic_answer_is_never_overturned`,
`test_a_claim_is_checked_against_the_listing_it_names`, and
`test_a_statute_year_is_never_treated_as_the_absent_record`.

---

## 4. Live verification

**Question (CR3's literal gold text):**

> In the online banking fraud matter involving two separate victims, was
> each victim's case processed and recorded the same way?

Every run: top-level `route='XNETWORK'` → Meta-Analysis, then three
`route='XAGG'` sub-query events, exactly as Module 57 recorded. The captured
aggregate lines, identical on every run:

```
XAGG filtered_fir_listing: filters=['statute: PECA 2016', 'station: سائبر کرائم سرکل…'] -> 2 FIR(s) ['64/26', '65/26']
XAGG cms_fir_linkage: 4 CMS complaint(s), 4 linked to a case, 0 unlinked
XAGG graph_recurrence: entity_type=Person, 4 recurring node(s); …عاصم رشید=2
```

and, on the fixed code, the new provenance line:

```
Meta-Analysis [Module 61]: 3 of 3 sub-answers declared a complete enumeration
(XAGG-only provenance); negative inference over those is supported, not inferred.
```

### 4a. Before / after — CR3, 8 runs each

| # | BEFORE (`HEAD~1`, pre-fix) | AFTER (shipped) |
|---|---|---|
| 1 | 179.4 s — answered | 120.2 s — answered |
| 2 | 138.1 s — **verifier rejection** | 126.5 s — answered |
| 3 | 143.1 s — **verifier rejection** | 119.5 s — answered |
| 4 | 98.0 s — **verifier rejection** | 126.9 s — answered |
| 5 | 135.1 s — answered | 118.6 s — answered |
| 6 | 117.9 s — answered | 228.5 s — answered |
| 7 | 97.5 s — **verifier rejection** | 128.1 s — **verifier rejection (different claim)** |
| 8 | 108.5 s — answered | 121.0 s — answered |
| | **4 of 8 answered** | **7 of 8 answered** |

**All four BEFORE rejections are the negative-inference claim** (§1's table).
**The one AFTER rejection is not:** *"The claim about accused persons is not
supported by any cited chunk…"* — an over-reach about the accused, which the
verifier correctly refused to serve and which this module neither causes nor
rescues. That is the failure mode staying alive, on the exact question.

An intermediate 8-run batch on the first commit answered **8 of 8**, and a
6-run batch on the second answered **5 of 6**. Across all post-fix code:
**20 of 22**. The honest headline number is the one on shipped code:
**7 of 8**, up from 4 of 8.

**Timing matters here.** Module 57 flagged a "fast block" of six consecutive
rejections at a uniform 64–66 s and suspected something temporal. On the
fixed code the first batch answered 8 of 8 with six runs in the **69–83 s**
band — the same fast band, answering. That block was the verifier, not the
clock.

### 4b. The two gates now agree

Before this module, on the runs where the verifier passed, the Validation
gate attached a caveat for the identical claim:

> _A cited claim ([Document 3]) could only be partially confirmed against
> its source: The source confirms FIR 64/26 is linked to a CMS complaint but
> does not mention FIR 65/26 at all, making the claim about FIR 65/26
> unsupported._

Measured, that caveat appeared on **5 of 5** answering runs of the
intermediate build. On shipped code it appears on **0 of 8**, and the log
carries the upgrade instead:

```
Validation [Module 61]: claim upgraded to supported — negative inference over
a complete listing: The source confirms FIR 64/26 is linked to CMS-ISB-2026-0341
but does not mention FIR 65/26 at all…
```

10 such upgrades across the 8 runs. **The gate did not go quiet in general**
— unrelated caveats survive on 6 of the 8 runs (see the verbatim answer
below), which is exactly the evidence that this is a targeted
reconciliation, not a silencing.

### 4c. Verbatim answer (run 2, representative)

> The two cases (FIR 64/26 and FIR 65/26) were **not handled identically**.
>
> - **FIR 64/26** has a matching walk-in complaint recorded in the CMS system (linked to CMS-ISB-2026-0341) [Document 3], whereas **FIR 65/26** does not appear in the CMS linkage list, indicating no corresponding walk-in complaint exists for this case [Document 3].
> - Both FIRs involve the same accused person, **عاصم رشید** [Document 2], and are registered under the **PECA 2016** statute [Document 1]. However, the difference in complaint linkage (present for FIR 64/26 but absent for FIR 65/26) shows they were processed differently.
>
> Thus, the records were **not handled identically** due to the absence of a matching complaint for FIR 65/26.
>
> _A cited claim ([Document 2]) could only be partially confirmed against its source: The source confirms both FIRs involve عاصم رشید but does not mention the PECA 2016 statute, only the PPC statute._

### 4d. KB4, 3 + 2 runs

`route='RAG'` on all five; no run reached the exhaustive path, as expected
for a RAG-only question.

| Batch | # | Elapsed | Status |
|---|---|---|---|
| first commit | 1 | 242.5 s | answered |
| | 2 | 245.4 s | answered |
| | 3 | 275.9 s | answered |
| shipped | 1 | 140.7 s | answered |
| | 2 | 151.7 s | answered |

**5 of 5 answering.** Module 38 measured 1 of 3. **This module is not the
reason** — it never fires on a RAG chunk, and §1 explains why KB4's recorded
rejections are a different defect. The honest reading is that KB4's verifier
rejection is intermittent and this sample did not hit it; the 1-of-3 figure
should not be treated as fixed.

---

## 5. Gold comparison

Gold: *"No — not identically. 64/26 has a matching walk-in complaint (linked
via matching case tag CMS-ISB-2026-0341, complainant سعد الرحمن); 65/26 has
none."*

| Gold element | Answered? |
|---|---|
| "No — not identically" | **Yes**, 7 of 7 answering runs |
| 64/26 has a matching walk-in complaint | **Yes**, 7 of 7 |
| Linked via case tag `CMS-ISB-2026-0341` | **Yes**, 7 of 7, gold's exact tag |
| 65/26 has none | **Yes**, 7 of 7 |
| Complainant سعد الرحمن | **No** — not returned on any run |

**Four of gold's five elements, on every answering run.** The missing
element is the complainant name, which is not in any of the three
sub-answers — the CMS linkage aggregate renders `case_tag → case_id` pairs
and no complainant. That is a completeness gap in the aggregate, not a
verifier one, and it is **not** this module's to fix; it is reported here
rather than papered over. Judged on facts-and-ideas coverage, CR3's answer
is a pass on the runs it serves; the module's contribution is that it now
serves them 7 times in 8 instead of 4.

**Not tuned toward gold.** No wording was changed to match gold, no
synthesis prompt was touched, and the one surviving failure and the one
missing gold element are both reported as they measured.

---

## 6. Non-gold paraphrase

**Two were run, and the first failed for a reason that is not this
module's.**

**Paraphrase 1** — *"Two people were defrauded through their online bank
accounts and both reported it. Were their complaints logged and tracked the
same way, or did one of them get less paperwork?"*

Result: `route='RAG'`, **abstained 3 of 3** — *"No sufficiently relevant
documents were found for this question after retrying with query
refinements."* It never reached Meta-Analysis, so the verifier never ran.
This is **Module 62** reproducing exactly as filed (the deterministic plans'
narrow lexical reach), on a paraphrase that even contains "the same way";
the top-level router sent it to RAG before any plan could match. Reported,
not swapped out.

**Paraphrase 2** — *"For the two victims in the internet banking fraud, were
both of their records handled the same way by the station?"*

Result: `route='XAGG'` (via XNETWORK → Meta-Analysis), **answered 3 of 3**,
93.5–113.9 s, with the negative inference intact on every run:

> The records of the two victims in the internet banking fraud were **not
> handled identically** by the station. **FIR 64/26** (linked to case tag
> **fir-64-26**) has a matching walk-in complaint recorded in the CMS system
> (CMS-ISB-2026-0341) [Document 3]…

**This is the check that separates a capability fix from curve-fitting**:
the question is worded differently from gold, the answer is worded
differently from gold, and the negative survives the verifier 3 times in 3.

---

## 7. Regression guard

Every question below routes through the same shared verifier. Run on the
first commit (3 each) and re-run on shipped code (2 each).

| Question | First commit | Shipped | Verdict |
|---|---|---|---|
| **M2** | 3/3 answered | 2/2 answered | Reproduces Module 44's 9-of-73 from 2-of-19 and the 7→39 vs 3→5 growth split. No change. |
| **G1** | **2/3** answered | 2/2 answered | One rejection on the first commit — *"Two claims lack explicit support in the cited chunks: the alleged data discrepancy and the 73-case total for seized property"* — a numeric over-reach, **not** absence-shaped, so the Module 61 path neither fired nor could. Split out as **Module 67**. Answers reproduce 92 accused / 17 with ages / mean 31.5. |
| **G6** | 3/3 answered | 2/2 answered | Orientation note with the district concentration (فیصل آباد 19, لاہور 18, راولپنڈی 10). No change. |
| **G2** | 3/3 answered | 2/2 answered | 9 FIRs with missing incident dates, listed by id. No change. |
| **G5** | 3/3 answered | 2/2 answered | 30 of 32 weapons recorded without a licence. No change. |
| **KB4** | 3/3 answered | 2/2 answered | §4d. |

**No regression.** 25 of 26 regression runs answered; the single failure is
G1's, is a different mechanism, and is filed rather than absorbed.

The `all-32` equality control was **not** run — the brief scopes this module
to CR3, KB4 and five named regressions, and a full re-run belongs to Module
27.

---

## 8. New defects found

**Module 67 — G1's synthesis verifier rejects an unsupported *numeric*
claim, intermittently.** One of three runs on this branch's first commit
returned `status=error` with *"Two claims lack explicit support in the cited
chunks: the alleged data discrepancy and the 73-case total for seized
property."* This is the verifier working correctly on a claim the synthesis
over-reached on — it is **not** a negative inference and Module 61's rule
correctly declines to rescue it — but it is a live instability on a
previously-passing question and it is the same shape as CR3's one surviving
failure (*"the claim about accused persons is not supported by any cited
chunk"*). Whoever takes it should decide whether the synthesis prompt is
inviting claims the sub-answers do not carry, rather than touching the
verifier again.

**Reported, not filed as new:**

- **The deterministic override has no live activation.** 0 firings in 22
  post-fix runs; the prompt rule carried every one. It is proven only by
  unit test. If a future change makes the judge stricter, this is the layer
  that keeps CR3 stable — but nobody has yet watched it do so on a live
  request.
- **The exhaustiveness declaration is made about a paraphrase.** The text
  marked `exhaustive_scope` is the XAGG sub-agent's NL paraphrase of the
  computed listing, not the raw rendering. Its numbers are already checked
  against the computed source by
  `verify_structured_aggregate_paraphrase()`, but a paraphrase that silently
  dropped a row would make a negative about that row look confirmed. The
  identifier check runs against the text actually served, which is the text
  the user reads, so the failure would at least be visible — recorded here
  because it is the fix's real residual risk.
- **Gold's complainant سعد الرحمن is unreachable from CR3's aggregates**
  (§5). A completeness gap in `cms_fir_linkage`'s rendering, which
  `xagg.py` owns and this branch may not touch.
- **Module 62 reproduced verbatim** (§6): a CR3 paraphrase containing "the
  same way" still routes to RAG and abstains 3 of 3. Already filed.
