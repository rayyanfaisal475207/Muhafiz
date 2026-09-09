# Module 116 — the route baseline was never a route baseline

**Branch** `fix/route-stability-cr3-g1-g6` · **worktree** `D:/Rapids AI/muhafiz-m116` · branched from `origin/main` @ `9b55e10` (Module 92 included).

**Headline, and it contradicts the filed diagnosis.** CR3, G1 and G6 are not
riding a coin flip. Their top-level route is `XNETWORK`, measured **14 runs of
14 each at temperature 0** — 11 through a route-only probe (§1.1) and 3 more
end-to-end through the served pipeline (§4) — unanimous, plus an independent
Module 92 measurement of the same quantity that agrees. All 32 gold questions
are unanimous over 3 runs each. And **6 of 6 rewordings, in three languages,
keep the same route** (§6).

The `XAGG` in `evaluation/gold32_pass{1,2,3}_outputs.json` is not a stale
route. It is **a different quantity**: the route of the *last* sub-query that
`meta_analysis.py` decomposed the question into. Module 27 and Module 92 never
disagreed — they measured two different things and both were right.

**No router change ships in this module.** No new regex, no prompt edit, no
override. The defect is in the recorder and in the SSE event it scrapes.

---

## 1. Root cause

### 1.1 Measured rates — there is no instability to quantify

`evaluation/module116_route_probe.py` calls `route_query()` and nothing else:
no retrieval, no sub-agent, no backend process. That is exactly the quantity
Module 92 measured and exactly the quantity Module 27's outputs file does not
contain.

| Question | Runs | Route | Unanimous | Decided by |
|---|---|---|---|---|
| CR3 | 11 | `XNETWORK` | yes | LLM classifier |
| G1  | 11 | `XNETWORK` | yes | LLM classifier |
| G6  | 11 | `XNETWORK` | yes | LLM classifier |

Raw: `docs/gold-qa-wave2-results/module116_route_probe.json` (8 runs each) and
`module116_all32_routes_before.json` (3 runs × all 32).

The classifier's own `reason` field — the field Module 78 found diagnostic — is
stable and, on its own terms, *correct* in all 33 runs:

* **CR3** — "Asks to compare how two separate (unnamed) cases were handled — a
  synthesis across specific case records, not a single-entity lookup (XGRAPH)
  or a cou…" (byte-identical on all 11).
* **G1** — "Asks for a synthesized review of the entire caseload to identify
  unusual or noteworthy patterns — this is an open-ended cross-case network
  analysis, n…" (byte-identical on all 11).
* **G6** — "A request for an orientation note for a newly posted officer
  covering what they should know about the current caseload — this is an
  open-ended synthes…" (10 of 11 byte-identical; the 11th is the same claim
  reworded, same route).

Nothing here resembles Module 78's KB3/KB9 failure, where the `reason` plainly
stated it was classifying the second half of a compound question. These three
are classified on what they actually ask, and they ask for open-ended
cross-case synthesis. `XNETWORK` is the route `router.txt` defines for that.

Confirmed offline as the brief describes: `_deterministic_route_override()`
returns `None` for CR3, G1 and G6 (and for A1 — Module 67's recorded, still-open
exposure). The other 28 are decided deterministically, which is why they cannot
move at all.

### 1.2 Where the `XAGG` in the baseline comes from

`src/pipeline/harness/supervisor.py::Supervisor.handle()` emits one
`supervisor:dispatch` event per dispatch:

```
Classified query as route='XNETWORK' -> sub-agent='Meta-Analysis'
```

`meta_analysis.py::_dispatch_one()` calls **back into** `Supervisor.handle()`
once per decomposed sub-query, so the same event is emitted again for each
sub-query — and Meta-Analysis's decomposer is built to emit *aggregate-shaped*
sub-queries, so those are `XAGG` by construction.

Until this module the only difference between the two kinds of event lived in
English prose inside `detail`. `evaluation/gold32_run.py::parse()` — the
recorder behind `gold32_pass{1,2,3}_outputs.json` — regex-scraped that prose
and kept the **last** match:

```python
if "route='" in str(det):
    m = re.search(r"route='([^']*)'", det)
    if m: route = m.group(1)          # last match wins
```

The last match is a sub-query. And because `meta_analysis.py` dispatches its
sub-queries **concurrently** (`asyncio.gather`), *which* sub-query lands last is
not even deterministic — the recorded value is the winner of a race.

**Proof, from data already on `main`, no new run required.** Module 92's own
captured trace, `module92_live/module92_inprocess_before.json`, on unchanged
shipped code:

```
CR3 (gold):  supervisor:dispatch  route='XNETWORK' -> Meta-Analysis
             supervisor:dispatch  route='XAGG'     -> Large-Scale Aggregate   × 3
G6 (gold):   supervisor:dispatch  route='XNETWORK' -> Meta-Analysis
             supervisor:dispatch  route='XAGG'     -> Large-Scale Aggregate   × 5
```

Run `parse()`'s last-match rule over either of those and you get `XAGG` — the
exact value Module 27 recorded, on all three passes, for exactly the questions
that decompose. The baseline was reproducing itself faithfully; it was
recording the wrong field.

(Aside, and it belongs to Module 110, not here: G6's five sub-dispatches are
the same five findings Module 110 records as covering five of gold's seven.)

### 1.3 Attribution — nothing merged today caused this

`git log --oneline -- src/pipeline/router.py src/prompts/router.txt` shows two
commits since Module 27: `3ff2112` (Module 78) and `462c242` (Module 92).

* **Module 78** added the `_is_legal_kb_question()` override, placed *last*, and
  its gate is False for CR3/G1/G6 — they are not KB questions. It moved KB3 and
  KB9 (see §7), and nothing else.
* **Module 92** changed only the **cloud** prompt path. The local classification
  path is untouched, and its own before-arm measurement (`local.json`,
  `*::gold` rows) records CR3/G1/G6 as `XNETWORK` — i.e. the routes were already
  `XNETWORK` before Module 92 merged, on the code Module 92 measured as "before".

Modules 88, 89, 90/91, 95 and 83 touch none of the routing layer.

There is therefore **no regression to attribute**, and equally no pre-existing
coin flip to paper over. The recorded baseline never described the router.

### 1.4 Which route is correct — checked, not inherited

The brief warns against assuming `XAGG` is right because Module 27 recorded it,
and against assuming it is right because CR3 scores 1.00 and G1 0.97 "under
XAGG". That second argument inverts once §1.2 is established:

**those scores were earned on `XNETWORK`.** A question only produces `XAGG`
sub-dispatch events if it went to Meta-Analysis, and it only goes to
Meta-Analysis if its top-level route was `XNETWORK`. The presence of `XAGG` in
the recording is *itself the evidence* that the served answer came from the
XNETWORK/Meta-Analysis path. The strongest empirical case for any route here is
a case for `XNETWORK`.

Independently, on content:

* **G1**'s gold answer is a four-part analyst review (offender age profile,
  stranger-relationship skew, seized-property/fatality signal, time-of-day
  flatness). That is a multi-finding synthesis, not one aggregate. Module 67
  already excluded `case_completeness_scan` from the XAGG route override *by
  name* on precisely this reasoning.
* **G6**'s gold answer is a seven-finding orientation note. Same shape.
* **CR3** asks whether two unnamed cases were handled the same way — a
  comparison across specific case records. `XAGG` has no aggregate for "compare
  these two"; `resolve_aggregate_kind()` returns nothing for it, which is why no
  XAGG override fires.

`XNETWORK` is correct for all three, on the content and on the scores.

---

## 2. The change

Three halves of one fix: the event carries the facts, the adapter forwards
them, the recorder reads them.

**`src/pipeline/harness/supervisor.py`** — the `supervisor:dispatch` event now
carries `route`, `sub_agent` and `nested` alongside its `detail`. `detail` is
**byte-identical** to what it has always emitted, so nothing that reads the
prose changes behaviour. `nested` is derived from `allow_meta_analysis`, not
from a new parameter: `meta_analysis.py` is the only caller that passes `False`
(its own one-level recursion guard, findings.md Module 10), so that flag
*already is* "this dispatch is a decomposed sub-query" and the two cannot drift.

**`src/pipeline/harness/cutover.py`** — forwards `PipelineEvent`'s extras onto
the SSE dict. `PipelineEvent` has been `extra="allow"` all along, and this
adapter dropped every extra on the floor; that is *why* the route had to be
scraped out of prose in the first place. Forwarded generically (loop over
`model_extra`) rather than by naming `route`/`nested`, and guarded with
`if _k not in out` so it can never overwrite `step`/`status`/`detail`/`ms`/
`sources`.

**`evaluation/gold32_run.py`** — `parse()` now reads the machine-readable
top-level route (`nested` False), records the sub-query routes separately as
`subquery_routes`, and records `sub_agent`. The `detail` regex survives only as
a fallback for a stream from a backend older than this change — and it now takes
the **first** match, which is the top-level dispatch.

**`evaluation/gold32_score.py`** — carries `route`, `last_dispatch_route` and
`subquery_routes` side by side. Deliberately *not* coalesced with `or`:
silently substituting one for the other is exactly how CR3/G1/G6 came to be
recorded `XAGG`.

**The baseline itself.** `evaluation/gold32_route_baseline.json` (new) is the
authoritative route baseline: all 32 top-level routes, measured, with runs,
method, and Module 92's independent corroboration recorded in the file. And in
the eight historical artefacts below, the misleading key `route` is **renamed
to `last_dispatch_route`** — which is precisely, accurately what those values
are. A consumer that asks for `route` now gets nothing, loudly, instead of a
wrong `XAGG` quietly:

`gold32_pass{1,2,3}_outputs.json`, `gold32_pass{1,2,3}_results.json`,
`gold32_results.json`, `gold32_pipeline_outputs.json`.

**New files:** `evaluation/module116_route_probe.py`,
`evaluation/module116_live_run.py`, `tests/test_module116_route_recording.py`,
`evaluation/gold32_route_baseline.json`,
`docs/gold-qa-wave2-results/module116_paraphrases.json`, and the three
measurement artefacts under `docs/gold-qa-wave2-results/`.

### What I did NOT change, and why

* **`src/pipeline/router.py` — untouched. Not one line.** There is nothing to
  fix there. The three questions reach the route their content calls for, 11/11.
* **No deterministic override for CR3/G1/G6.** The brief forbids three gold-worded
  regexes, and beyond that they would be *wrong*: an override would pin a route
  the classifier already produces unanimously, and would take these three off the
  only layer that generalises to rewordings at all. The module ships a test —
  `test_module116_the_three_questions_still_have_no_deterministic_override` —
  that fails if a later module adds one, so this reasoning has to be revisited
  rather than quietly lost.
* **No prompt edit.** Module 92 measured the local classifier failing 11 of
  `router.txt`'s own worked examples; a few-shot addition is not a mechanism.
  And there is no misclassification here to correct.
* **`meta_analysis.py`'s concurrent dispatch — left alone.** The race it creates
  in event *ordering* is real (filed as defect 120) but the fix here does not
  depend on ordering: `nested` is on the event.
* **`deepeval_results.json`** — a different gold set (`gold_set.py`) with
  *expected* routes declared per row, not scraped. Not affected, not touched.

---

## 3. Unit tests

`tests/test_module116_route_recording.py`, 10 tests. **Checked in both
directions**: the suite was run against the pre-change tree (fix stashed as
`m116-fix-for-before-check`, SHA `60f029a`, test file restored from the stash's
untracked commit) and against the post-change tree.

| Test | Before | After |
|---|---|---|
| `…parse_records_the_question_route_not_the_last_subquery` | **FAIL** (`XAGG` != `XNETWORK`) | pass |
| `…parse_keeps_the_subquery_routes_separately` | **FAIL** (key absent) | pass |
| `…parse_is_unaffected_for_a_single_dispatch_question` | **FAIL** (key absent) | pass |
| `…parse_falls_back_to_the_first_detail_match_on_a_legacy_stream` | **FAIL** (`XAGG` != `XNETWORK`) | pass |
| `…supervisor_dispatch_event_carries_route_and_nesting` | **FAIL** | pass |
| `…cutover_forwards_pipeline_event_extras_to_the_sse_dict` | **FAIL** | pass |
| `…recorded_route_baseline_matches_the_measured_top_level_routes` | **FAIL** (file absent) | pass |
| `…the_three_questions_still_have_no_deterministic_override` × 3 | pass | pass |

The last three are guards, not defect reproductions: they pass before because
the property they protect is already true, and they exist so it stays true.

Named-file regression runs (never a bare `pytest tests/`):
`tests/test_router.py`, `tests/test_harness_supervisor.py`,
`tests/test_harness_cutover.py`, `tests/test_harness_types.py`,
`tests/test_harness_agent_meta_analysis.py`, plus this module's own —
**458 passed, 0 failed, 6.2 s.**

---

## 4. Live verification

**No backend was started.** Two were already listening (`127.0.0.1:8001`,
`:8064`) and both belong to other tracks; the brief's cap is 2 machine-wide.
Verification ran **in process** through `evaluation/module116_live_run.py`,
which reuses `module92_inprocess_run.py::run_one()` verbatim — the same two
stages `main.py::chat_endpoint()` uses (`route_query()`, then
`run_cutover_query()` through the Supervisor). The one thing it does not
inherit is Module 92's runner's `.env` load: that one loads the main checkout's
`.env`, whose `CHROMA_PERSIST_DIR` is **relative** and would have resolved
against this worktree and silently built an empty vector store.

Raw: `docs/gold-qa-wave2-results/module116_live/module116_inprocess_after.json`.

| Run | Route | `supervisor:dispatch` events | Reached Meta-Analysis | Answer carries gold's content |
|---|---|---|---|---|
| CR3 gold x3 | `XNETWORK` 3/3 | 4 each (1 top-level + 3 sub) | yes 3/3 | **yes 3/3** |
| G1 gold x3 | `XNETWORK` 3/3 | 6 each (1 + 5) | yes 3/3 | **yes 3/3** |
| G6 gold x3 | `XNETWORK` 3/3 | 6 each (1 + 5) | yes 3/3 | **2/3** — see below |

Combined with §1.1's probe, that is **14 runs of 14 `XNETWORK` for each of the
three questions**, across two independent harnesses.

The dispatch counts are the defect made visible: every one of these runs emits
one top-level `XNETWORK` event followed by 3 or 5 `XAGG` sub-events. Under the
old last-match rule, all nine would have been recorded `XAGG`. Under the new
rule they record `route="XNETWORK"`, `sub_agent="Meta-Analysis"`,
`subquery_routes=["XAGG", ...]`.

**A route that is stable and wrong is worse than one that is unstable and
sometimes right — so, the answers.** CR3 and G1 carry gold's substance on 3 of
3 (§5). G6 carries it on **2 of 3**, and the run that does not is lost to an
already-filed defect, not to routing: on all three runs the Meta-Analysis
synthesis **collapsed into a repetition loop** (Module 83's guard fired: *"758
tokens, unique-token ratio 0.024, one 4-gram repeated 370 times"*). Runs 2 and 3
recovered on the temperature-0.4 regeneration and answered in Roman-Urdu with
gold's figures. Run 1's regeneration **also** collapsed, provenance recovery
failed, and the fallback served the five raw sub-answers — **in English, to a
Roman-Urdu question**. Filed as defect 122; it is downstream of everything this
module touches, and it happens identically before and after the change.

---

## 5. Gold comparison

Judged on substance, not wording.

**CR3** — gold: *"No — not identically. 64/26 has a matching walk-in complaint
(linked via matching case tag CMS-ISB-2026-0341, complainant سعد الرحمن); 65/26
has none."*

All three runs open with *"No, not identically"*, name **FIR 64/26** and **FIR
65/26**, and state that 64/26 has a matching walk-in complaint via a case tag
in the CMS while 65/26 does not appear in the linkage list. **The claim, the
direction and both FIR numbers are right on 3 of 3.** Not carried: the literal
tag `CMS-ISB-2026-0341` and the complainant's name — runs 1 and 3 say "a shared
case tag" without quoting it. That is a detail-level gap consistent with the
1.00 this question already scores, not a substantive miss.

**G1** — gold asserts four things. All three runs carry all four:

| Gold finding | Served |
|---|---|
| offender profile uniform, 24–49, avg ~31 | "ranging from 24 to 49 years old, and an average age of 31.5" ✅ |
| 'stranger' dominates the recorded relationships | "اجنبی (stranger) … appears in 15 of the 24 entries" ✅ |
| seized property points to more fatalities — 13 to a forensic lab, 7 held for a deceased's heirs | "13 items were sent to a forensic laboratory, and 7 items are held for return to the deceased person's heirs" ✅ |
| incident times fairly flat with a mild evening lean | "the busiest time of day is the evening (18:00–23:59), with 19 incidents … approximately 38%" ✅ |

The served answer is in one respect **more careful than gold**: it states that
only 17 of 92 accused have a recorded age, so the profile rests on a minority —
gold asserts flatly that *"every accused is between 24 and 49"*. It also adds a
fifth finding gold does not have (four accused recurring across two FIRs each).
Extra correct material is a pass under this programme's standard.

**G6** — gold asserts seven things. The two recovered runs carry the district
spread (Faisalabad 19, Lahore 18, then 10/5/5/5/5/5/1 — gold's *"9 zilon mein
phaili … zyada bojh Faisalabad aur Lahore"*), the 2024→2026 case-mix shift from
almost-all armed robbery to a narcotics/cyber/fraud/kidnapping/murder mixture
(named by statute: CNSA 1997, PECA 2016, Punjab Domestic Violence Act, Illegal
Dispossession Act 2005), and the FIR-count growth 13 → 51 — **in Roman-Urdu**,
gold's own language. The accused profile and the court-pendency claim are
**absent**, exactly as **Module 110** records: G6's `orientation_note` plan
computes five of gold's seven findings and the other two are not reachable from
above. That is Module 110's, not this module's, and this run reproduces it
unchanged.

---

## 6. Non-gold paraphrase

Six rewordings, two per question, **written before any of them was run and not
adjusted afterwards** — committed as
`docs/gold-qa-wave2-results/module116_paraphrases.json`. Four non-English
(two Urdu-script, one Roman-Urdu). None reuses Module 92's existing
`gold32_paraphrases.json` entries.

**Route and answer reported separately, because they diverge sharply.**

| Paraphrase | Route | Reached Meta-Analysis | Answer |
|---|---|---|---|
| CR3 · en · *"We had two people cheated through internet banking — did the force deal with both of their complaints in the same way on paper?"* | `XNETWORK` ✅ | yes (4 dispatches) | **Full gold content** — "No, not identically", 64/26 linked, 65/26 not |
| CR3 · ur · *"آن لائن بینکنگ فراڈ کے دو متاثرین …"* | `XNETWORK` ✅ | **no** (1 dispatch) | **Refused** — "No cross-case connections or patterns were found", nearest cluster 0.149 vs 0.145 cutoff |
| G1 · en · *"Put your crime-analyst hat on and tell me what … looks off or deserves a closer eye."* | `XNETWORK` ✅ | **no** (1) | **Refused** — nearest cluster 0.197 vs 0.145 |
| G1 · roman_ur · *"Aik crime analyst ki hesiyat se hamare mojooda cases ka jaiza lein …"* | `XNETWORK` ✅ | yes (6) | **Full gold content**, in Roman-Urdu — age profile, stranger skew, all of it |
| G6 · en · *"Write a brief starter note for an officer who has just been posted here …"* | `XNETWORK` ✅ | **no** (1) | **Refused** — nearest cluster 0.200 vs 0.145 |
| G6 · ur · *"یہاں نئے تعینات ہونے والے افسر کے لیے ایک مختصر تعارفی نوٹ لکھیں …"* | `XNETWORK` ✅ | yes (6) | **Gold content in Urdu** — district spread, 2024→2026 statute mix |

**Route: 6 of 6 correct.** The layer this module was sent to investigate
generalises perfectly across all three languages on all six rewordings. That is
a strong negative result for the filed diagnosis: the router is not the brittle
layer here.

**Answer: 3 of 6.** The three that fail are refused *after* correct routing, and
the discriminator is not language — G6's **Urdu** rewording answers while its
**English** one refuses; G1's **Roman-Urdu** answers while its **English**
refuses. The mechanism is `supervisor.py::classify_to_subagent()`, which gates
Meta-Analysis on `_META_ANALYSIS_TRIGGER_PATTERNS` matching the **query text**,
independently of the route. Miss the literal list and an `XNETWORK` question
falls through to Global Search / Cross-Case Linkage, whose relevance gate then
correctly refuses a question it was never meant to answer.

This is **Module 111's finding, generalised**: Module 111 recorded it for G6 and
read it as a G6-specific literal-phrase brittleness. Measured here, **CR3 and G1
have the identical failure**, and the split is by trigger-pattern match, not by
question and not by language. Filed as defect 119, not fixed — it is
`supervisor.py`'s dispatch gate, a different layer from this module's scope, and
Module 111 already owns G6's half.

---

## 7. Regression guard

**Routes — all 32 gold questions, 3 runs each.** Every question unanimous over
its 3 runs; CR3/G1/G6 unanimous over 14.
(`docs/gold-qa-wave2-results/module116_all32_routes_before.json`,
`evaluation/gold32_route_baseline.json`.)

* **28 of 32 take a deterministic override** and structurally cannot move.
* **4 are decided by the LLM classifier** — A1 (`XAGG`, Module 67's recorded,
  still-open exposure) and CR3/G1/G6 (`XNETWORK`). All four unanimous.
* **Independent corroboration:** Module 92's own all-32 measurement of the same
  quantity (`module92_harness_cache/local.json`, the `*::gold` rows, taken on a
  different day by a different harness) agrees on **32 of 32. Zero
  disagreements.**

**Against the historical passes.** Exactly **five** rows of
`gold32_pass{1,2,3}_outputs.json` differ from the measured baseline, identically
on all three passes, and every one is accounted for — recorded per question in
`gold32_route_baseline.json`'s `historical_reconciliation` block:

| Q | Recorded | Measured | Cause |
|---|---|---|---|
| CR3 | `XAGG` | `XNETWORK` | recording defect (§1.2) |
| G1 | `XAGG` | `XNETWORK` | recording defect (§1.2) |
| G6 | `XAGG` | `XNETWORK` | recording defect (§1.2) |
| KB3 | `XNETWORK` | `RAG` | **genuine, intended** — Module 78 (`3ff2112`) merged after Module 27's passes |
| KB9 | `XAGG` | `RAG` | **genuine, intended** — Module 78 |

The other **27 agree on all three passes**. No question moved unexplained, and
nothing is waved through.

**Answers.** This module changes **six lines of production code**, none of them
on a decision path:

```
supervisor.py   route=route_result.get("route")     # 3 lines: extra fields on an
                sub_agent=sub_agent_name            #          already-emitted event
                nested=not allow_meta_analysis
cutover.py      for _k, _v in (evt.model_extra or {}).items():   # 3 lines: stop
                    if _k not in out:                            #  dropping extras
                        out[_k] = _v
```

`detail` is byte-identical, no route, sub-agent, tool, prompt or retrieval
behaviour is touched, and the forwarding loop cannot overwrite
`step`/`status`/`detail`/`ms`/`sources`. The claim "the answers cannot change"
is a structural one, and it was checked rather than asserted:

* **15 live in-process runs** (9 gold + 6 paraphrase) on the changed tree, all
  serving normally.
* **458 unit tests** over `tests/test_router.py`,
  `tests/test_harness_supervisor.py`, `tests/test_harness_cutover.py`,
  `tests/test_harness_types.py`, `tests/test_harness_agent_meta_analysis.py`
  and this module's own — **0 failures**. Named files only; no bare
  `pytest tests/` was ever run.
* **Frontend**: `chatStore.ts::applyEventToSteps()` reads named fields off the
  event and ignores anything else, so the new keys are inert in the UI.
* **`evaluation/gold32_score.py --summary`** re-run over the renamed artefacts:
  FactualCorrectness mean **0.684 over 32/32**, AnswerRelevancy **0.931 over
  31/32**, pass rate **22/32** — the figures on `main`, unchanged. The `route`
  column was never an input to scoring, which is why the rename costs nothing.

**Note on scope.** The brief anticipated a router change and warned that the
regression guard would be broad by construction. It is broad anyway — but the
router is untouched, so the blast radius is the SSE event dict and the eval
recorder, not "the layer every query passes through".

---

## 8. New defects found

Highest number in use before this module: **118**. Starting at **119** as
instructed. All four are **filed, not fixed**.

### 119 — Meta-Analysis is gated on a literal trigger list, and it costs CR3 and G1 too, not just G6

`supervisor.py::classify_to_subagent()` reaches Meta-Analysis only when
`_META_ANALYSIS_TRIGGER_PATTERNS` matches the **query text**, independently of
the route. An `XNETWORK` question that misses the list falls through to Global
Search / Cross-Case Linkage, whose relevance gate then refuses it.

**Evidence** (§6, six paraphrases written before running): route correct 6/6,
Meta-Analysis reached **3/6**. The three refusals are CR3-Urdu (nearest cluster
0.149 vs a 0.145 cutoff), G1-English (0.197) and G6-English (0.200). The three
that pass are CR3-English, G1-Roman-Urdu and G6-Urdu.

**Why this is new rather than Module 111.** Module 111 recorded this for G6 and
framed it as G6's rewordings failing, all three languages. Measured here it is
**not G6-specific and not language-specific**: G6's *Urdu* rewording passes
while its *English* one fails, and G1 and CR3 show the identical split. The
variable is trigger-pattern membership. Any fix must be a predicate — Module
78's `_is_legal_kb_intent()` precedent — not a fourth word list; this is the
same disease Modules 92, 106 and 111 each recorded one layer up.

### 120 — a nested `supervisor:dispatch` event cannot be attributed to its sub-query

`meta_analysis.py::_dispatch_one()` runs its sub-queries under `asyncio.gather`,
and the dispatch event carries no sub-query text or index. CR3's trace is three
byte-identical `route='XAGG' -> Large-Scale Aggregate` lines; G6's is five.
Nothing in the stream says which sub-query each belongs to, and their **order is
a race**. Module 116 fixed the consequence (`nested` is on the event, so the
recorder no longer depends on ordering) but not the cause: a trace reader still
cannot tell which decomposed question produced which answer. Cheap to fix — add
the sub-query text or index to the event — but it changes what Meta-Analysis
emits, not what the router decides, so it is out of this module's scope.

### 121 — one gold question costs up to **seven** router classification calls

`main.py::chat_endpoint()` classifies once, `Supervisor.handle()` classifies
again (documented as a "KNOWN, ACCEPTED INEFFICIENCY" in `cutover.py`), and then
`Supervisor.handle()` runs once more **per decomposed sub-query**. G6 decomposes
into five, so one G6 request makes **1 + 1 + 5 = 7** `route_query()` calls
against the local Qwen3-14B with `prompts/router.txt` — which Module 92 measured
at **13,003 request tokens**. That is roughly 91,000 prompt tokens of routing
for one question. The accepted-inefficiency note covers only the first
duplicate; the per-sub-query multiplication is undocumented. Measured
indirectly: G6 gold runs took 105–118 s each in process.

### 122 — G6's synthesis collapsed 3 of 3, and one fallback served English to a Roman-Urdu question

On all three G6 gold runs, Module 83's guard fired with an identical signature —
*"758 tokens, unique-token ratio 0.024, one 4-gram repeated 370 times"*. Runs 2
and 3 recovered on the temperature-0.4 regeneration. **Run 1's regeneration also
collapsed**, `_attach_provenance()` then ran and failed (*"synthesis cited
nothing and no sentence had unique support; provenance NOT recovered"*), and the
fallback served the five raw sub-answers **in English** — to a question asked in
Roman-Urdu, whose gold answer is Roman-Urdu.

Two things here are not yet on the tracker. First, the collapse rate: **3 of 3**
on the gold wording, with a byte-identical signature every time, which reads
like a deterministic failure rather than the sampling accident Module 112 filed
it as. Second, the fallback path has **no language contract** — serving raw
sub-answers silently drops the user's language. Module 113 records
`_attach_provenance()` firing 0 times in 20 runs and asks whether it is dead
code; here it fired and failed, which is evidence against deleting it and for
fixing it.
