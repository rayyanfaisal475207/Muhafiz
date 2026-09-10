# Module 71 — G1's synthesis intermittently makes an unsupported numeric claim

**Branch:** `fix/verifier-numeric-overreach`.
**Outcome: root cause identified exactly and fixed on the generation side —
and the headline live number is a null result reported as one.** The
fabricated `73` is the **incident-time-of-day sub-answer's denominator**,
and it is the only one of G1's five sub-answers that states 73 at all. The
defect **did not reproduce in 25 pre-fix live G1 runs on this machine**
(Module 61 saw it 1 in 3), so no before/after improvement on G1's own
answer rate can honestly be claimed. What *is* measured, on a forced live
control that makes the rare path certain:

| Same fabricated synthesis, same real verifier | Pre-fix | Shipped |
|---|---|---|
| Runs | 3 | 4 |
| Verifier rejected the fabrication | **3 of 3** | **4 of 4** |
| User outcome | **`status=error` 3 of 3** | **`status=done` 4 of 4** |
| Gold's four findings present | **0 of 3** | **4 of 4 findings, 4 of 4 runs** |
| Fabricated text served | never | never |

**A change was also built, measured, found harmful, and deleted** — a
per-document figure roster in the synthesis prompt. It cost G6 **0 of 4 →
7 of 8** verifier rejections. It is reported in §7 rather than shipped.

**Backend:** port 8026, this worktree, platform-admin, All Cases.
**Machine state:** Module 65 live on 8024 throughout; 4.1 GB free RAM at
the start of the first batch; nothing else on 8025–8030.
**Provider health:** model server `/health` returned
`{"cuda":{"available":true,…},"models_loaded":{"embedder":true,"reranker":true}}`
before the first batch. Module 81's corrected quota grep
(`gold32_score.py`'s `LOG_GREP_PATTERN`) returns **0** on every one of the
six backend logs this module produced, and `Cutover classification failed`
is **0** on all six.

---

## 1. Root cause

**The brief's first hypothesis is right, and the measurement names the
specific number.**

G1 fans out to five XAGG aggregates. Their live renderings on this machine,
captured from `backend.log` on every run:

```
XAGG offender_age_profile: 17 of 92 distinct accused carry an age (19 of 94 accused entries); range 24-49, mean 31.5
XAGG accused_relationship_breakdown: 24 relationship edge(s) across 10 FIR(s), 6 distinct value(s); dominant='اجنبی' 15; coverage 12 of 92 distinct accused
XAGG seized_property_disposition: 45 register entr(ies) across 28 FIR(s), 9 distinct disposition(s); forensic-lab dispatch 13 item(s) in 11 FIR(s); heirs 7 item(s) in 7 FIR(s)
XAGG incident_time_of_day: 64 of 73 incident(s) carry a datetime; 14 are date-only 00:00:00 and excluded; 50 usable -> {...}; peak='evening (18:00-23:59)'
XAGG graph_recurrence: entity_type=Person, 4 recurring node(s); فیصل=2, طارق=2, شہزیب عرف شابی=2, عاصم رشید=2
```

**`73` appears in exactly one of them — `incident_time_of_day`, as the
denominator of "64 of 73 incidents".** The seized-property aggregate
computes 45 entries across 28 FIRs. So Module 61's recorded rejection —
*"the 73-case total for seized property"* — is a number lifted from
[Document 4] and attached to [Document 3]'s subject. It is not an
invention out of nothing and it is not arithmetic: it is a **cross-
denominator borrow**, and the verifier was right to refuse it.
`test_module71_the_live_rejection_number_belongs_to_exactly_one_sub_answer`
asserts that ownership rather than narrating it.

**Why the existing prompt rule could not catch it — the part the brief did
not anticipate.** `_SYNTHESIS_SYSTEM_PROMPT_TEMPLATE` already carried a
numeric rule, added by Module 29 after G6 summed per-district counts:

> "2. Every number you state appears literally in a sub-answer above."

**73 satisfies that rule.** It appears literally in a sub-answer above —
just not in the one the claim is about. Every G1 sub-query is phrased
"How many cases … across all cases?", so the page presents five counts over
five different populations and offers a menu of plausible-looking totals.
The rule was scoped to the *page*; the property it needed to enforce is
scoped to the *document*.

**And G1's own synthesis goal actively invited it.** The `caseload_review`
plan ended its coverage instruction at:

> "Where a sub-answer states how much of the caseload its figure is based
> on, carry that coverage across too"

spoken over five sub-answers with five different denominators, with no
statement that a coverage figure belongs only to the finding it came from.

**Module 61's account is confirmed, not revised:** this is not a negative
inference over a listing, the exhaustive-listing rule neither fires on it
nor could rescue it, and nothing in `verifier.py` or `prompts/verifier.txt`
was touched.

---

## 2. Change

**One production file**, `src/pipeline/harness/agents/meta_analysis.py`,
plus its test file and two scripts under `scripts/`. `verifier.py`,
`validation.py` and `prompts/verifier.txt` are **deliberately untouched** —
the brief's central instruction, and Modules 17/25/40/61's shared posture.

| What | Why there |
|---|---|
| **Synthesis rule 2, rescoped** — "every number you state appears literally in THE SUB-ANSWER YOU CITE FOR IT, not merely somewhere above", plus an explicit ban on carrying a total, denominator or coverage figure from one `[Document N]` onto another's subject. The old rule's derived-number half survives verbatim as rule 3. | The rule the fabricated 73 satisfied. Scoping it to the document is the minimal change that makes the actual claim false under the rule. |
| **`caseload_review`'s synthesis goal** — coverage travels "onto that finding and no other", and "if a sub-answer does not say how many records its figure covers, report the figure without a denominator". | The plan-level instruction that invited the blend, on the one question that shows it. |
| **`figures_in()` + `_misattributed_figures()`** — deterministic detection of a figure attached to a document that does not state it while a sibling does. Logged, never branched on. | Makes the mechanism countable live rather than hypothesised. Deliberately narrow: an outright invention (a figure NO sub-answer states) is left to the verifier, so a firing means "cross-document borrow" and nothing else. |
| **Verifier-rejection fallback** — the branch returns `PARTIAL` carrying a deterministic composition of the contributing sub-answers instead of a bare `ABSTAINED`. | §3 of the brief, and the user-visible half. |

**The fallback, precisely.** `cutover.py` (line ~478) renders
`ABSTAINED`-or-`answer_text is None` as `status=error`, so a rejected
synthesis discarded five correctly computed aggregates to punish one
sentence. The new branch:

* **drops the rejected text.** It never appears in `answer_text`, which is
  composed with **no second LLM call** from the sub-answers alone
  (`test_module71_the_fallback_makes_no_second_llm_call`). AGENT_HARNESS_
  DESIGN's "an answer that failed verification is NEVER served" holds
  unchanged — what is served is the evidence underneath it, each piece
  already past its own sub-agent's verifier and validation gate.
* is **`PARTIAL`, not `OK`**, because the cross-cutting narrative the user
  asked for is genuinely missing, and the caveat says so in the user's
  sight: *"The combined answer could not be verified as grounded in the
  sub-answers, so each verified sub-answer is shown as computed, without a
  synthesis across them."*
* keeps `citations`, `tools_used` and `degraded_from` intact, 1:1 with the
  documents.

This is deliberately the **same shape as two existing precedents**, not a
third invention: `large_scale_aggregate.py` serves `raw_summary_text` when
the verifier rejects its paraphrase, and Module 53 serves the raw aggregate
when a sub-query paraphrase is cancelled.

**No retry loop.** A regenerate-on-rejection would hide exactly the
non-determinism this module exists to measure — Module 61's own stated
posture — and is pinned closed by a test.

---

## 3. Unit tests

```
PYTHONPATH=. ".../.venv/Scripts/python.exe" -m pytest \
    tests/test_harness_agent_meta_analysis.py tests/test_verifier.py \
    tests/test_validation.py tests/test_pipeline.py \
    tests/test_citation_consistency.py tests/test_harness_supervisor.py \
    -p no:randomly
```

**430 passed, 0 failed.** 16 are new, all in
`tests/test_harness_agent_meta_analysis.py`. The three the brief names:

| Requirement | Test |
|---|---|
| a synthesis inventing a cross-denominator total is **still rejected** | `test_module71_a_rejected_synthesis_serves_the_sub_answers_not_an_error` — the live rejection reason verbatim; asserts `"73-case total" not in answer_text` |
| the same, for the pre-existing hallucination case | `test_hallucinated_synthesis_is_still_rejected` (Module 25's test, **kept and strengthened**): status changed ABSTAINED → PARTIAL, and the contract it exists for did not — `"previously convicted" not in answer_text`, both sub-answers present |
| the fallback | `test_module71_..._serves_the_sub_answers_not_an_error`, `..._the_fallback_makes_no_second_llm_call`, `..._an_off_topic_synthesis_also_falls_back_rather_than_erroring`, `..._nothing_changes_on_a_synthesis_the_verifier_accepts` |

Pinned to the live-captured shape rather than an invented one:
`test_module71_the_live_rejection_number_belongs_to_exactly_one_sub_answer`
(73 is the time-of-day sub-answer's and nobody else's, over the five
aggregates' real renderings),
`test_module71_a_cross_denominator_total_is_detected`,
`test_module71_a_correctly_attributed_answer_is_not_flagged`,
`test_module71_an_outright_invention_is_left_to_the_verifier`,
`test_module71_the_synthesis_rules_scope_a_number_to_its_own_document`
(a wording lock on the old rule 2, whose return would silently reopen the
defect), `test_module71_g1s_synthesis_goal_forbids_lending_a_denominator`,
and `test_module71_the_sub_answers_section_carries_no_extra_document_marker`
(§7's deleted roster, pinned out).

**Two of the new tests exist because a first draft was wrong and the live
data said so**, not because they were foreseen:
`test_module71_an_identifier_is_not_a_figure` and
`..._the_recurring_accused_sub_answer_does_not_trip_the_detector`. Run
offline against the eleven pre-fix live answers captured at that point, the
first detector flagged `24` and `64` on three of them — both pulled out of
`fir-891-24` / `fir-64-26`, where they are case numbers. The corrected rule
flags **0 of 25** pre-fix live answers. Its first correction then over-shot
and read `73-case` as an identifier too, discarding the one number the
module exists to catch; both directions are now pinned.

---

## 4. Live verification

**Question (G1's literal gold text):**

> Acting as a crime analyst, review our current caseload and flag anything
> that looks unusual or worth monitoring.

Every run: `Meta-Analysis: deterministic decomposition plan 'caseload_review'
matched.` → five `XAGG` sub-query events → `Meta-Analysis [Module 61]:
5 of 5 sub-answers declared a complete enumeration`. The SSE `route=` the
runner records is the last sub-query's, `XAGG`, exactly as Module 61
recorded for CR3.

### 4a. G1 before / after — n-of-N

Gold coverage is computed per run by `scripts/module71_live_runs.py` from
the captured answer: age range/mean, `اجنبی`/stranger dominance, forensic-lab
and heirs disposition, time-of-day distribution, plus numeric probes for
~31.5, 15-of-24 and 13/7.

| Arm | Code | N | answered | `status=error` | verifier rejections | all four gold findings |
|---|---|---|---|---|---|---|
| **Before** | pre-fix (`main` @ `7f9e636`) | **25** | **25** | **0** | **0** | **25 of 25** |
| **After** | shipped | **8** | **8** | **0** | **0** | **8 of 8** |
| (intermediate, roster build) | not shipped | 16 | 16 | 0 | 0 | 16 of 16 |

**The defect did not reproduce.** Module 61 measured 1 of 3; this module
measured 0 of 25 on the same question against pre-fix code, on this
machine, this session. Runs are also much faster than Module 61's era
(31.6–53.9 s, against its 120 s CR3 band), so the intervening merges have
changed this path materially. **No improvement in G1's answer rate is
claimed, because none was measurable.** What the fix does for G1 is (a)
remove the invitation that produced the blend and (b) make the failure
survivable when it happens — proven in 4c.

**Honest note on how the "before" reached 25.** The first three batches
after the intended cutover were still served by the pre-fix process: a
`Stop-Process` on the port's owner reported success but did not kill it,
which a log-handle check caught (`backend.after.log` stopped at the
startup banner while the old process kept writing). Those 11 runs are
counted where they belong, in the BEFORE arm. Every AFTER number below
comes from a process whose log carries the new
`Meta-Analysis [Module 71]` lines.

### 4b. Verbatim answer (shipped, run 1 of 8)

> Based on the current caseload, several patterns and anomalies stand out that warrant further monitoring and analysis:
>
> - The age range and average age of the accused are based on a minority of the total accused individuals. Specifically, only 17 out of 92 accused have recorded ages, with those aged between 24 and 49, and an average age of 31.5 [Document 1]…
> - The most frequently recorded relationship between the accused and the complainant is "اجنبی (stranger)," which appears in 15 of the 24 relationship entries and is documented in 6 FIRs [Document 2]…
> - Seized property is recorded in 28 FIRs, with a total of 45 items across these cases. Of these, 13 items were sent to a forensic laboratory, and 7 items were held for return to the deceased person's heirs [Document 3]…
> - The time of day when incidents occur is unevenly distributed. Of the 64 incidents with recorded times, the busiest time of day is the evening (18:00-23:59), with 19 incidents [Document 4]…
> - Certain accused individuals appear in more than one FIR. Specifically, **فیصل** and **طارق** each appear in 2 cases… and **عاصم رشید** appears in **fir-64-26**, **fir-65-26** [Document 5]…

**Every denominator is attached to its own finding**: 92 to the accused, 24
to relationships, 28/45 to property, 64 to incidents. No 73 anywhere near
seized property.

### 4c. The forced-hallucination control — the proof the brief calls not optional

The defect is rare enough that waiting for it proves nothing, so it was
**forced**. `scripts/module71_forced_hallucination_control.py` starts the
ordinary backend with exactly one boundary replaced — `meta_analysis`'s
`call_llm` returns a fixed fabricated synthesis instead of generating one.
Everything downstream is real: the five aggregates are really computed, the
**real** `verify_grounding()` judges the fabricated text, the real fallback
composes what is served. Nothing in `src/` is modified; the patch lives in
the script. Same shape as Module 53's forced-deadline control.

The injected claim, the live one:

> "Across the caseload, the seized-property register covers a **73-case
> total**, and that figure does not reconcile with the property entries
> actually recorded, which is a data discrepancy worth flagging
> [Document 3]. The accused are aged 24-49 [Document 1]."

| | Pre-fix, 3 runs | Shipped, 4 runs |
|---|---|---|
| Verifier rejected it | **3 of 3** | **4 of 4** |
| `status` | **`error` 3 of 3** | **`done` 4 of 4** |
| Gold's four findings reaching the user | **0 of 3** | **4 of 4** |
| The fabricated 73 in the served text | never | never |
| Elapsed | 19.2–22.4 s | 20.4–22.4 s |

The verifier's own words, unprompted and near-verbatim to Module 61's
recorded string:

```
Meta-Analysis: verifier rejected synthesized answer: The claim about the
seized-property register covering a 73-case total and the discrepancy is not
supported by Document 3, which states that 28 FIRs record seized property…
```

and on the shipped code, immediately after it:

```
Meta-Analysis [Module 71]: 1 figure(s) attached to a document that does not
state them, though a sibling sub-answer does: 73 -> [Document 3]
Meta-Analysis [Module 71]: serving 5 verified sub-answer(s) instead of
erroring out.
```

**The detector named the exact borrow — `73 -> [Document 3]` — on 4 of 4
runs, before the verifier had spoken.** That is the root-cause claim of §1
confirmed by the running system, not by reading.

What the user receives on the shipped code, in place of an error
(opening of run 1, abbreviated):

> **How many cases involve an accused person, and what is their age range and average age, across all cases?**
> Across all cases, there are 92 distinct accused individuals. Of these, 17 have a recorded age, with the ages ranging from 24 to 49, and the average age is 31.5…
>
> **How many cases record a relationship between the accused and the complainant…**
> …The most frequently recorded relationship is "اجنبی (stranger)," which appears in 15 of the 24 entries…
>
> **How many cases record seized property…**
> …28 FIRs record seized property, with a total of 45 items… 13 items were sent to a forensic laboratory. 7 items were held for return to the deceased person's heirs.
>
> **How many cases record an incident time…**
> …64 of the 73 incidents record an incident date/time…

All four of gold's findings, correct denominators, no synthesis. That is
the difference between "the system could not produce a grounded answer" and
a usable answer.

### 4d. An unforced live activation, on a different question

The fallback also fired **unforced** during the intermediate (roster) build:
G6's synthesis was rejected on 10 of 12 runs with *"Answer is substantial …
but cites no [Document N] source at all"*, and every one of those runs
returned `status=done` carrying the five sub-answers, including gold's
district concentration (فیصل آباد 19, لاہور 18, راولپنڈی 10). Pre-fix,
each would have been `status=error`. That the roster **caused** those
rejections is §7's subject and the reason it was deleted — but the runs are
also the clearest evidence that the fallback does its job on a question
this module was not aimed at.

---

## 5. Gold comparison

Gold's G1: *"(1) every accused is between 24 and 49 (average ~31); (2)
'stranger' dominates; (3) 13 items sent to a forensic lab and 7 held for a
deceased's heirs; (4) incident times are fairly flat across the day with
only a mild evening lean."*

| Gold element | Shipped, 8 runs |
|---|---|
| Offender age 24–49, average ~31.5 | **8 of 8**, gold's own range and mean |
| 'Stranger' (اجنبی) dominates the recorded relationships | **8 of 8**, with 15 of 24 |
| 13 forensic-lab items / 7 held for heirs | **8 of 8**, gold's exact pair |
| Time-of-day distribution | **8 of 8**, evening peak with the four-band split |
| "across the 73 FIRs" as the framing | present as the incidents denominator, correctly |
| 8 murder-section FIRs (gold's fatality corroboration) | **0 of 8** — not in any sub-answer |

**Four of gold's five elements on every run, judged on facts and ideas.**

**Two honest divergences, neither tuned away.** (a) Gold says times are
*"fairly flat … with only a mild evening lean"*; the answer says the
distribution is uneven with an evening peak. Module 34 established that
gold's flatness is a **date-only artefact** — 14 of 64 incidents sit at
exactly 00:00:00 — and that module's decision not to steer the wording
toward "flat" is preserved here, unchanged. (b) Gold's *"8 murder-section
FIRs"* is unreachable from G1's five aggregates; Module 76 separately
measured the true figure as **10**, not 8. Neither is this module's to fix.

**Not tuned toward gold.** No wording was changed to match gold; the two
divergences above are reported as they measured, and the one change to a
synthesis goal removes an instruction rather than adding a target.

---

## 6. Non-gold paraphrase

Two, written before the fix and not adjusted afterwards, three runs each on
both arms.

**Paraphrase 2 (Roman Urdu)** — *"Mujhe poore caseload ka jaiza chahiye -
ghair mamooli kya hai? Mulzimon ki umar, unka complainant se taluq, zabt
shuda saman ka kya bana, aur waqia kis waqt hota hai?"*

`route='XAGG'` via Meta-Analysis, **answered 3 of 3 on both arms**,
32.0–34.3 s shipped, and **all four gold findings on 3 of 3** shipped
(2 of 3 pre-fix — the third stated the relationship finding without the
15-of-24 pair). Every denominator correctly placed:

> …only 17 have their ages documented, with those aged between 24 and 49,
> and an average age of 31.5… "اجنبی (stranger)," which appears in 15 of the
> 24 entries… 28 FIRs record such items, with 13 items dispatched to a
> forensic laboratory and 7 items held for return to the heirs… 64 of the
> 73 incidents record an incident date/time…

Different question wording, different answer wording, the same four
findings and the same correct denominators: a capability, not curve-fitting.

**Paraphrase 1 (English)** — *"Take a look across everything we currently
have on the books and tell me what stands out about the offenders, the
property we are holding and when these incidents actually happen."*

`route='XNETWORK'` **3 of 3 on both arms**, 7.1–7.6 s, answered with a
generic cross-case entity traversal and **none** of gold's four findings.
It never reaches `caseload_review`, so the plan — and therefore this
module — never runs. **This is Module 62 reproducing verbatim** (the
deterministic plans' narrow lexical reach), on a paraphrase that contains
"across everything", "offenders", "property" and "when … happen". Reported
as it measured rather than swapped for one that passes.

---

## 7. Regression guard

Shipped code, four runs each (G6 eight), against pre-fix runs on the same
machine in the same session.

| Question | Pre-fix | Shipped | Verdict |
|---|---|---|---|
| **CR3** | 4 of 4 answered | **4 of 4** answered, 0 rejections, 22.0–22.7 s | Module 61's 7-of-8 **holds** — here 4 of 4. Reproduces gold: *"No, not identically… fir-64-26 has a matching walk-in complaint, while fir-65-26 does not appear…"* |
| **G6** | 4 of 4 answered, 0 rejections | **8 of 8** answered, **0 rejections**, 31.9–33.7 s | Reproduces the district concentration (فیصل آباد 19, لاہور 18, راولپنڈی 10). See below — this is the number that caught the roster. |
| **M2** | 4 of 4 | **4 of 4**, 2.6–2.8 s | Unchanged: 15 general-purpose stations 7→39 FIRs vs 2 specialised 3→5, i.e. Module 44's 9-of-73 from 2-of-19 |
| **G2** | 4 of 4 | **4 of 4**, 4.4–4.7 s | Unchanged: 9 FIRs with no incident date, listed by id; 52 of 73 with no investigation status |
| **G5** | 4 of 4 | **4 of 4**, 2.6–3.0 s | Unchanged: 30 of 32 weapons recorded without a licence |

**40 of 40 regression runs answered, 0 verifier rejections, on shipped
code.** M2/G2/G5 short-circuit above Meta-Analysis (Module 41's guard) and
never see this change at all, which their 2–5 s timings confirm.

### A change that was built, measured, found harmful, and deleted

The brief's second direction — "whether the sub-answers carry their own
denominators explicitly enough" — was implemented as a **per-document
figure roster**: a deterministic line under each sub-answer in the
synthesis prompt listing exactly the figures it states, so rule 2's "the
sub-answer you cite for it" became a lookup rather than a recollection. It
reads well, it is cheap, and it is **not shipped**, because G6 measured it:

| Build | G6 runs | verifier rejections | elapsed |
|---|---|---|---|
| pre-fix | 4 | **0** | 34.0–53.1 s |
| roster, naming `[Document N]` | 4 | **3** | 33.3–92.5 s |
| roster, reworded to avoid the marker | 8 | **7** | 34.1–67.7 s |
| **roster removed (shipped)** | 8 | **0** | 31.9–33.7 s |

Every one of the ten rejections is *"Answer is substantial (long, or a
multi-item list) but cites no `[Document N]` source at all"* — precisely
the failure Module 29 filed for G6 and fixed by restating the citation rule
**after** the sub-answers. Five extra lines interleaved into that section
put it back.

The first hypothesis was Module 25's stray-marker finding, since the roster
named `[Document N]`; rewording it to say "this sub-answer" instead **did
not recover G6** (7 of 8 again), so the cost is the **interleaving itself**,
not the wording. Removing the roster recovered G6 completely and
immediately, 8 of 8 at the pre-fix timing.

Against that cost there was **no measured benefit**: the defect the roster
prevents did not occur in 25 pre-fix G1 runs, so nothing was there to
improve. `figures_in()` survives because `_misattributed_figures()` uses it
to *measure* the same property on the produced answer, which costs the
prompt nothing — and that detector is what named `73 -> [Document 3]` on
4 of 4 control runs.

The measurement is written into `_format_subanswers_for_prompt`'s docstring
and pinned by
`test_module71_the_sub_answers_section_carries_no_extra_document_marker`,
so the next module to have this idea finds the answer before spending the
runs.

**The all-32 equality control was not run** — this module changes one
sub-agent's prompt and one plan's synthesis goal, reaches no router or
resolver, and a full re-run belongs to Module 27.

---

## 8. New defects found

**Module 82 — Meta-Analysis' synthesis intermittently cites nothing at all,
and the rate is prompt-length-sensitive.** G6's *"cites no [Document N]
source at all"* rejection is Module 29's, still live: 0 of 4 pre-fix here,
but **10 of 12** once five short lines were added to the sub-answers
section. The citation rule is evidently held only by proximity to the end
of the prompt, which makes every future addition to that section a coin
flip nobody will think to re-measure. The durable fix is structural — put
the citation requirement somewhere length cannot dilute it, or make the
`[Document N]` markers unnecessary by carrying provenance out of band —
rather than another restatement. Filed rather than absorbed: this module
recovered G6 by deleting its own change, which is a workaround, not a fix.

**Reported, not filed as new:**

- **The defect this module was filed for did not reproduce in 25 pre-fix
  live runs.** Module 61's 1-in-3 was measured on a different build, and
  this path has changed materially since (G1 now runs in ~35 s, not ~120 s).
  The generation-side fix is therefore justified by the root-cause analysis
  and the forced control, **not** by a before/after improvement on G1's
  answer rate. If it recurs, the `Meta-Analysis [Module 71]` detector line
  will name the borrowed figure and the document it was attached to.
- **`_misattributed_figures()` has no unforced live activation.** 0 firings
  in 24 post-fix G1/G6 runs; its only live firings are the 4 control runs
  where the fabrication was injected. Same posture, and the same
  disclosure, `MODULE61_RESULT.md` took about its own post-pass.
- **Module 62 reproduced verbatim** (§6): an English G1 paraphrase routes
  to XNETWORK 3 of 3 and never reaches the plan. Already filed.
- **Gold's "8 murder-section FIRs" is unreachable from G1's aggregates**
  (§5), and Module 76 has separately measured the real figure as 10.
- **A percentage the synthesis derives itself survives the verifier.**
  Three pre-fix runs contain *"accounting for approximately 38% of the
  incidents"*, computed from 19 of 50 — exactly what rule 3 forbids, passed
  by the judge each time. Harmless here (the arithmetic is right), but it
  shows rule 3 is advisory in practice, and it is the same class of gap as
  the one this module was filed for.
