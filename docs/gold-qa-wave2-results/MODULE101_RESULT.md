# Module 101 — the grounding verifier rejects a composed KB answer after the pipeline has already handed it the correct figure

**Branch:** `fix/verifier-composed-evidence` · **Question:** KB9 (primary), with
five other KB questions and three pre-registered paraphrases as the class arm.
**Merge base:** `origin/main` @ `92129ac` (rebased; measured on `d9bf286`, which
differs from `92129ac` only in `xagg.py` / `structured_projection.py` —
neither is on this module's path).

---

## 0. The filed diagnosis was wrong, and that is the first result

The brief's leading hypothesis was that *"a KB data-half answer's second half
cites a chunk that `xagg_tool()` composed in memory, not one the retriever
returned. If `verify_grounding()` grounds only against retrieved chunks, then
every composed answer is unverifiable by construction."*

**It does not. The composed chunk is already first-class evidence, and was
before this module.** `rag.py::_run_kb_data_half()` returns the aggregate in the
raw retrieval-chunk shape and folds it into `reranked`; `_to_evidence_chunk()`
converts it like any other; `semantic_search()` passes **every** chunk to
`verify_grounding()`. Measured, not read: on a live KB9 run the verifier's own
input carries seven chunks and chunk **7** is
`kb-data-half:death_investigation_charging`, 829 characters, its full rendered
text — *"10 of the 73 FIR(s) that carry a recorded section cite PPC §302"* plus
the ten case ids and the section table. `_format_chunks_for_verifier()`
truncates nothing. Module 85's own committed window table shows the same thing
for KB4 (`kb-data-half:property_register` at position 7).

So Phase 2's preferred fix — *"make the composed chunk first-class evidence"* —
had nothing to do: it already is. The real mechanism is below, it is not about
composition at all, and the exposure class is **wider** than the data-half
questions.

---

## 1. Root cause

### 1.1 The rejection is a deterministic pre-check, not the LLM judge

`verify_grounding()` runs `_check_no_citation()` before the judge. It rejects
any *substantial* answer that contains no `[Document N]` token, on the stated
reasoning that such an answer *"reads as avoiding the provided evidence rather
than using it."* When it fires it is merged as `refusal_issue`, which sets
`off_topic=True` and `grounded=False`, and Semantic Search's `[PRESERVE]`
contract then discards the **entire** answer.

Captured verbatim from a live KB9 run (`module101_before.json`, run 1), the
verifier's own returned dict:

```json
{"grounded": false, "off_topic": true, "leaked_case_id": null,
 "unsupported_claims": [],
 "reason": "Answer is substantial (long, or a multi-item list) but cites no
            [Document N] source at all, despite the evaluator already
            confirming relevant chunks exist ..."}
```

**`unsupported_claims` is empty.** The LLM judge found nothing wrong with the
answer. Nothing about grounding failed.

### 1.2 The answer it threw away was correct, and attributed by name

Verbatim, the discarded answer (abridged; full text in
`module101_before.json`):

> ### Legal Requirement:
> According to the **Code of Criminal Procedure (Pakistan)** … the police are
> required to conduct an inquiry into the cause of death … **Section 174** …
> Additionally, **Section 176** … the nearest magistrate empowered to hold
> inquests shall conduct an inquiry into the cause of death.
>
> ### Case Records Summary:
> According to **our own case records (cross-case aggregate)**, **10 of the 73
> FIR(s)** that carry a recorded section cite **PPC §302** …
>
> However, the summary does **not** provide specific information on whether the
> police conducted detailed inquiries into the cause of death …

That is CrPC s.174, s.176, gold's corrected figure (10 FIRs citing PPC §302),
and gold's honest schema gap. Its only fault is *how* it attributes: it names
its sources instead of numbering them. And the two strings it names —
`Code of Criminal Procedure (Pakistan)` and `our own case records (cross-case
aggregate)` — are the **`source` labels of chunks 1/3/4 and 7 of that very
window**. The answer was not avoiding the evidence. It was citing it by name.

### 1.3 The intermittency is the generator, not the judge

The brief asks whether the 1-in-3 flap is LLM variance or a real difference
between runs. It is measurable both ways and the answer is unambiguous.

**Resampled on a FIXED (answer, window) pair** — the exact rejected pair from
run 1, replayed straight into `verify_grounding()` with retrieval and
generation held constant (`scripts/module101_verifier_resample.py`):

| arm | verdict |
|---|---|
| pre-change code | **rejected 8 of 8** |
| this branch | **served 8 of 8**, and the LLM judge says *"All claims are directly supported by cited chunks"* on all 8 |

The verdict on a fixed input is **deterministic**. There is no judge sampling
here at all. What varies run to run is upstream: **whether Qwen3-14B emits
`[Document N]` markers.** Across six live KB9 runs on the pre-change code the
correlation is exact — every rejection is a run whose answer contains no
`Document` token, every pass is a run whose answer contains one:

| run | generator | `[Document N]` in answer | verdict |
|---|---|---|---|
| 1 | local | **no** | **rejected** |
| 2 | local | yes | served |
| 3 | local | **no** | **rejected** |
| 4 | local | yes | served |
| 5 | local | yes | served |
| 6 | local | yes | served |

This is Module 71 §8's finding, on a different route: it measured the same
check rejecting Meta-Analysis' G6 synthesis on **10 of 12** runs and recorded
that the rate is *"prompt-length-sensitive"* — the citation rule *"is held only
by proximity to the end of the prompt."* Module 82 §8d then saw it fire
unforced on shipped code. Module 71's own prescription was *"make the
`[Document N]` markers unnecessary by carrying provenance out of band."* That
is what §2 does, on the Semantic Search side.

### 1.4 A measurement trap that has to be reported

The first eight in-process KB9 runs of this module showed **0 rejections** —
and were worthless. `call_llm()` is local-first and falls back to Groq/Gemini
on *any* local failure; under the contended shared model server the local call
raised an empty-message `httpx.ReadTimeout` at the 60 s budget and a **cloud**
model wrote the answer. Cloud models cite: all 8 carried `[Document N]`, so the
check never fired. That arm is retained, labelled, as
`module101_cloud_confounded_arm.json`. Every count in this document comes from
runs where the runner **recorded** that the local model answered
(`"generation": ["local"]` on every row of `module101_before.json` /
`module101_after.json`).

**This is a candidate explanation for Module 85's null result**, which measured
KB4 across 29 shipped-code runs and found zero rejections. Module 85's own
quoted KB4 answer carries *"(Document 1)"*. Offered as a hypothesis, not a
finding: that arm's generator provenance was not recorded and cannot be
recovered.

### 1.5 Which questions are exposed

**Not the data-half class.** `_check_no_citation()` is on the path of *every*
substantial RAG answer; the composed chunk is irrelevant to it. The data-half
questions are *more* exposed for Module 71's reason — `_COMPOUND_ANSWER_RULE`
adds ~90 words between the citation instruction and the documents — but they
are not the boundary.

For completeness the plan enumeration was still done offline against
`rag.py::_match_kb_data_half_plan()` over all 32 gold questions:

| gold | plan | fires? |
|---|---|---|
| KB1 | `fir_register_completeness` | yes |
| KB3 | `officer_role_pair` | matches, but Modules 65/74/78 measure KB3 routing **XNETWORK/XAGG** |
| KB4 | `property_register` | yes |
| KB5 | `violence_against_women` | yes |
| KB6 | `weapon_register` | yes |
| KB8 | `chalaan_dispatch` | yes |
| KB9 | `death_investigation_charging` | yes |
| CR8 | `violence_against_women` | pattern matches, **`_is_legal_kb_intent()` is False**, so it never fires |

The other 24 resolve to `None`. The live class arm (§7) measures six of these
plus KB2 (RAG, no plan) and finds the omission rate is a **generation** property
that varies by question and language, exactly as §1.3 predicts.

---

## 2. The change

Two files, 171 added lines, 3 removed.

### 2.1 `src/pipeline/verifier.py` — attribution by source name

`_check_no_citation()` is **unchanged**. What changed is that it is no longer
merged unconditionally. It is split out of `refusal_issue`, and after the judge
has answered, its finding is dropped if and only if **both** of:

* **(a)** the answer contains, verbatim, the normalised `source` /
  `source_file` label of a chunk it was actually given
  (`_answer_names_a_cited_source()`); and
* **(b)** the LLM judge independently cleared it — `grounded`, not
  `off_topic`, `unsupported_claims` empty, no leakage, no `_check_refusal()`
  finding and no other deterministic pre-check outstanding.

Normalisation (`_normalise_source_label()`) drops the extension, folds `_`/`-`
to spaces, lower-cases, and strips **leading** all-digit tokens (the corpus
index and the statute year a filename carries: `1_1898_Code_of_Criminal_
Procedure_(Pakistan).pdf` → `code of criminal procedure (pakistan)`). A label
under 3 tokens or 12 characters is refused outright, so `unknown` and
`entity_graph` can never satisfy (a).

When the exemption applies, `citation_format_degraded: True` and the matched
`named_source` are returned. `pre_check_failed` — Module 61's guard on its
exhaustive-negative override — still counts the no-citation finding exactly as
before, because whether it will be exempted is not known until the judge has
answered.

### 2.2 `src/pipeline/harness/agents/semantic_search.py` — the caveat

The exemption is never silent: an exempted answer is served carrying
`CITATION_FORMAT_DEGRADED_CAVEAT`, which says the sources are named rather than
numbered and that individual claims cannot be traced to one. `citations` is
unaffected — it is built positionally from `chunks`, never parsed out of the
answer text — so the source list is still complete; it is the claim-to-source
mapping that is lost, and the reader is told so.

### 2.3 What was deliberately NOT changed

| not changed | why |
|---|---|
| `_check_no_citation()` itself, `_SUBSTANTIAL_ANSWER_LEN`, `_DOCUMENT_CITATION_RE`, `_LIST_ITEM_RE` | The check is not loosened. Its finding still stands on its own everywhere condition (a) or (b) fails |
| The LLM judge, `prompts/verifier.txt` | Untouched (`git diff origin/main -- prompts/` is empty). Module 82 measured this judge catching fabrications three ways; nothing here touches its threshold, its prompt or its rules |
| `_check_refusal()` | Evaluated separately and **never** exempted. A refusal that names a source still fails |
| `_check_leakage`, `_check_fabricated_case_ids`, `_check_temporal`, `_check_hedging` | Security- and validity-relevant, and every one still overrules the exemption (§3, §7.1) |
| Module 61's exhaustive-negative override | Untouched, including its `pre_check_failed` guard |
| `rag.py`, `xagg.py`, `router.py`, `supervisor.py`, `validation.py`, `meta_analysis.py`, `citation_consistency.py`, `orchestrator.py` | Not on this defect's path, and the last three belong to other live tracks. `git diff origin/main` over all of them is empty |
| The composed chunk's metadata | The filed hypothesis's fix. It needed nothing — §0 |
| Meta-Analysis' caveat wiring | The verifier-level exemption applies there too (it will reduce Module 71's G6 rejections), but that agent's caveat plumbing is another track's file. `CITATION_FORMAT_DEGRADED_CAVEAT` is exported for it and the gap is filed as **Module 120** (§8) |

**Files touched:** `src/pipeline/verifier.py`,
`src/pipeline/harness/agents/semantic_search.py`, `tests/test_verifier.py`,
`scripts/module101_{inprocess_runs,verifier_probe,verifier_resample,forced_hallucination_control,equality_control}.py`,
`docs/gold-qa-wave2-results/MODULE101_RESULT.md` + eight data files,
`GOLD_QA_REMAINING_FIXES_PLAN.md`.

---

## 3. Unit tests

Ten new tests in `tests/test_verifier.py`. **Both directions were checked.**

The three positive tests cannot be run against the pre-change module — its
top-level `from src.pipeline.verifier import CITATION_FORMAT_DEGRADED_KEY`
fails collection outright — so the before/after direction was proved with the
same assertions spelled against the literal key, on the stashed code:

```
===== BEFORE (origin/main code) =====     ===== AFTER (this branch) =====
grounded      = False                     grounded      = True
off_topic     = True                      off_topic     = False
degraded flag = None                      degraded flag = True
reason        = Answer is substantial ... reason        = All claims are supported.
```

| test | asserts | before |
|---|---|---|
| `..._kb9_answer_is_served_instead_of_discarded` | the live KB9 answer, verbatim, is served | **fails** |
| `..._the_exemption_is_never_silent` | `citation_format_degraded` reaches the caller | **fails** |
| `..._finds_the_named_source_across_separator_differences` | helper resolves all three real labels | **fails** (no helper) |
| `..._normalises_an_ingest_filename_to_the_form_prose_writes` | `1_1898_…pdf` → `code of criminal procedure (pakistan)` | **fails** (no helper) |
| `..._a_label_too_short_or_generic_is_refused` | `unknown` / `entity_graph` / `a_b_c.pdf` → None | **fails** (no helper) |
| `..._an_answer_naming_no_cited_source_is_not_attribution` | an unrelated statute name is not attribution | **fails** (no helper) |
| **`..._a_fabrication_is_still_rejected_even_when_it_names_a_source`** | Module 82's fabrication shape, rewritten to satisfy (a), is still rejected | passes both — a guard |
| `..._an_evasive_answer_naming_nothing_is_still_rejected` | condition (a) holds | passes both — a guard |
| `..._a_genuine_refusal_is_never_exempted` | `_check_refusal()` is outside the exemption | passes both — a guard |
| `..._a_deterministic_pre_check_still_overrules_the_exemption` | a temporal finding beats it | passes both — a guard |
| `..._a_cited_answer_is_not_flagged_as_degraded` | ordinary answers are untouched | passes both — a guard |

The four "guard" tests are stated as passing in **both** directions on purpose:
they pin behaviour this module must not move, and a test that only passes after
would not do that.

`tests/test_verifier.py` 124 passed. Also green, unchanged:
`test_harness_agent_semantic_search.py`, `test_harness_tool_rag.py`,
`test_kb_statute_retrieval.py`, `test_validation.py`,
`test_module82_generation_findings.py`, `test_router.py`, `test_xagg.py`.
No bare `pytest tests/` was ever run.

---

## 4. Live verification — KB9, six runs each arm

All twelve runs use the real `rag_tool()` (shared Chroma, read-only; reranker
live), the real relevance evaluator, the real generation with the **local**
model recorded per run, and the real `verify_grounding()`. Run in-process
rather than through `/api/chat`: the machine-wide two-backend budget was held
by other tracks for this module's entire measurement window, and neither the
SSE stream nor `backend.log` carries the verifier's chunk corpus or its
itemised `unsupported_claims` — which is what Phase 1 asked to see.

| | before (`module101_before.json`) | after (`module101_after.json`) |
|---|---|---|
| runs | 6 | 6 |
| generator | local 6/6 | local 6/6 |
| **`status=abstained` (answer discarded)** | **2 of 6** | **0 of 6** |
| answers with no `[Document N]` | 2 | 1 |
| exemption fired, answer served with caveat | — | **1 of 6** |
| verifier rejections of any kind | 2 | **0** |
| elapsed | 53–110 s | 53–100 s |

A single good run would prove nothing; the before arm's two rejections and the
after arm's zero are the counts. Note also the *pass* runs: 4 of 6 before and 5
of 6 after never reach this code at all, because the model happened to cite —
the change is not what makes those pass.

Module 78's live `/api/chat` arm on the earlier merge base recorded the same
defect at **6 of 9** across the KB9 family (KB9 2/3, P-KB9 1/3, P2-KB9 3/3).
This module's in-process before arm reproduces it at 2 of 6. Reported as
measured: the two rates are not the same number, the harnesses differ, and no
claim is made that they should match.

### 4.1 The answer the exemption served (after, run 3)

> ### Legal Requirement:
> Under the **Code of Criminal Procedure (Pakistan)**, specifically **Section
> 174**, the police are required to inquire and report on the cause of death …
> This requirement is further elaborated in **Punjab Police Rules-III**, **Rule
> 25.31**, which mandates that the officer in charge … must immediately inform
> the nearest magistrate authorized to hold inquests …
>
> ### Case Records Summary:
> According to **our own case records (cross-case aggregate)**, **10 of the 73
> FIR(s)** that carry a recorded section cite **PPC §302** …
>
> However, the records do **not** provide specific information on whether the
> police conducted the required investigation and prepared the detailed report
> …

served with:

> This answer attributes its sources by name rather than with [Document N]
> markers, so individual claims cannot be traced to a specific source; every
> claim was still checked against the retrieved evidence.

Before this module, that answer was replaced by *"The generated answer could
not be verified as grounded in the retrieved documents."*

---

## 5. Gold comparison — KB9's corrected gold

Judged on substance. Gold: CrPC **s.174** (inquiry into a suspicious death, a
report on the apparent cause with marks of violence, forwarded to the nearest
magistrate), **10 FIRs citing PPC §302**, property marked for a deceased's
heirs, **no inquest / post-mortem / cause-of-death record in the schema**.
Module 78's committed answer text also credits PPR **25.31**.

| gold element | after, 6 runs |
|---|---|
| CrPC s.174, inquiry into cause of death | **6 of 6** |
| CrPC s.176 (magistrate's inquest, custodial death) | 3 of 6 |
| PPR 25.31 | 2 of 6 |
| **10 FIRs cite PPC §302** | **6 of 6** |
| the 73 denominator | **6 of 6** |
| the schema gap stated honestly | **6 of 6** |
| property marked for a deceased's heirs | **0 of 6** |

Before: the same substance on 4 runs, and **nothing at all** on 2.

**The heirs element is out of scope and stays open.** It needs a *second*
aggregate (`seized_property_disposition`'s heirs count) composed into the same
answer, and `_KbDataHalfPlan` carries exactly one `sub_query`. That is **Module
79**, not this module. Reported as remaining exposure: KB9 cannot reach full
gold coverage until a data-half plan can dispatch two aggregates, and no run in
either arm named it.

---

## 6. Non-gold paraphrases

Three rewordings, committed to
`docs/gold-qa-wave2-results/module101_paraphrases.json` with
`"written_before_any_run": true` **before the after arm was run**, and not
edited afterwards. Each was checked only against
`_match_kb_data_half_plan()` for plan resolution — never against a gold answer
and never against a score.

| id | language | plan resolved | runs | served | notes |
|---|---|---|---|---|---|
| **P1** | English | `death_investigation_charging` | 3 | **3/3** | states the 10/73 and the gap |
| **P2** | **Roman-Urdu** | `death_investigation_charging` | 3 | **3/3** | — |
| **P3** | Urdu | `death_investigation_charging` | 3 | **2/3** | run 3 rejected by the **LLM judge** (`"The claims in the answer are not supported by the provided chunks"`, two Urdu claims itemised) — a grounding rejection, not this module's check, and not exempted. See §8 |

All nine runs carried `[Document N]`, so the exemption never fired on any of
them: the paraphrases are a route/plan and no-regression measurement here, not
a demonstration of the fix.

---

## 7. Regression guard

### 7.1 The exhaustive old-vs-new equality control

`scripts/module101_equality_control.py` loads `origin/main`'s `verifier.py`
alongside this branch's, drives **both** with the same stubbed judge over a
144-cell matrix crossing every property the exemption is gated on — answer
cited / uncited, names a source / names none, substantial / short / empty /
refusal; window statute-only / with the composed data-half chunk / anonymous
source; judge clears / flags a claim / calls it off-topic; a temporal
pre-check outstanding / not — and diffs the returned dicts.

```
cells compared: 144
cells that MOVED (exemption fired, rejection -> served): 3
  + uncited_named_long        | statute_only            | judge=clears | pre=none
  + uncited_named_long        | statute_plus_data_half  | judge=clears | pre=none
  + uncited_named_agg_long    | statute_plus_data_half  | judge=clears | pre=none
cells that regressed: 0
```

Three cells move; all three are the case the module exists for; **141 are
byte-identical**. This is a stronger statement than a sampled re-run: outside
those three shapes the function's output is unchanged by construction.

### 7.2 The forced-hallucination control — the brief's non-optional proof

`scripts/module101_forced_hallucination_control.py`, two arms, three runs each,
over a real live KB4 window. Nothing in `src/` is modified.

**Arm A** is Module 82's control, its `_FABRICATED` answer and `_MARKERS`
**imported** from `scripts/module82_forced_hallucination_control.py` so the
fabrication cannot drift: an invented rule number (`27.41(3)`), a retention
period on the wrong subject, an invented `FIR 512/26`, an unsupported *"fully
compliant"*.

**Arm B exists because Arm A alone is not a sufficient control for this
module, and saying so is the point.** Module 82's fabrication carries
`[Document 2]`/`[Document 7]`, so `_check_no_citation()` never fired on it and
the exemption is never reached — Arm A proves the change is *inert* on that
input, not that it is *safe*. Arm B strips the markers and names a real cited
source (`Punjab Police Rules-III`) instead, so the fabrication **satisfies
condition (a)** and the LLM judge is the only thing left. Nothing else is
softened.

| | Arm A (Module 82 verbatim) | Arm B (uncited, names a source) |
|---|---|---|
| control fired | 3 of 3 | 3 of 3 |
| **verifier rejected the fabrication** | **3 of 3** | **3 of 3** |
| `status` | `abstained` 3/3 | `abstained` 3/3 |
| exemption applied | never | **never** |
| fabricated markers in the served text | **0 of 3** | **0 of 3** |
| elapsed | 55–85 s | 59–67 s |

Arm A's rejection reason is **verbatim identical to the one Module 82
recorded**:

```
Semantic Search: verifier rejected answer: The claims about rule 27.41(3) and
the audit of FIR 512/26 are not supported by any of the cited chunks.
```

Arm B is rejected with the no-citation reason — which is the design working:
the judge flagged two claims, so condition (b) failed, the exemption was not
applied, and the finding was merged exactly as it would have been before this
module. **6 of 6 fabrications rejected, 0 markers served.**

### 7.3 All 32 gold questions

**Routes cannot move and this is structural, not sampled.** `router.py`,
`supervisor.py`, `rag.py`, `xagg.py` and `prompts/` are untouched
(`git diff origin/main` over each is empty), so nothing this module changes is
reachable before a route is chosen. The offline all-32 pins that guard dispatch
(`test_module56_all_32_gold_questions_resolve_exactly_as_before` and the
`_match_kb_data_half_plan` equality control in `tests/test_kb_statute_retrieval.py`)
pass unchanged.

**Answers can move only where the exemption fires**, and §7.1 bounds that to
three cells of a 144-cell surface. Since `citation_format_degraded` is recorded
per run, the accounting is complete rather than statistical: **any run where
the flag is absent is byte-identical to the pre-change code**, because the new
branch is unreachable.

The live arm covers the questions actually at risk — every RAG-route KB
question, plus the three paraphrases — 33 runs:

| id | runs | route/plan | answers with no `[Document N]` | exemption fired | `status != ok` |
|---|---|---|---|---|---|
| KB2 (RAG, **no** plan) | 3 | RAG, none | 0 | 0 | 0 |
| KB4 | 3 | RAG, `property_register` | 0 | 0 | 0 |
| KB5 | 3 | RAG, `violence_against_women` | **1** | **0** | **1** |
| KB6 | 3 | RAG, `weapon_register` | 0 | 0 | 0 |
| KB8 | 3 | RAG, `chalaan_dispatch` | 0 | 0 | 0 |
| KB9 | 6 | RAG, `death_investigation_charging` | 1 | **1** | 0 |
| P1 / P2 / P3 | 9 | RAG, `death_investigation_charging` | 0 | 0 | 1 (P3, judge) |

**Nothing moved that was not predicted, and one thing predicted did not move.**
KB5 run 2 produced a citation-less answer and was **still rejected** — the
exemption correctly declined it (§8a). That is a partial-coverage finding, not
a regression: its pre-change outcome was the same rejection.

---

## 8. New defects found — filed, not fixed

Numbering: the highest number in use on `main`'s
`GOLD_QA_REMAINING_FIXES_PLAN.md` at the time of writing is **118**, and it moved twice while this module was being written — the brief
said to start at 102, the table carried rows to 113 when the fix was committed,
and 114-118 landed on `main` before the PR was opened. These start at **119**
and were renumbered once already; if they collide again the cause is the same
race, not this module.

### 8a. Module 119 — a source label mangled by OCR defeats attribution-by-name, and Urdu answers name nothing

KB5 run 2 (`module101_class_and_paraphrases.json`) is the same defect as KB9's
— a substantial, citation-less answer carrying gold's own figure (*8 domestic
violence reports, 4 matched to an FIR*) — and this module's exemption does
**not** rescue it, on two independent counts:

1. The window's statute chunks carry the label
   `7_Anti-Rape (lnvestigation and Trial) Act. 2021 & Rules, 2022 (Amendments
   upto date).pdf`. Note **`lnvestigation`** — a lowercase L for a capital I,
   an OCR artefact in the ingest filename. The answer writes *"Anti-Rape
   (Investigation and Trial) Act, 2021"*, which is not a substring of the
   normalised label, and it stops well short of *"& Rules, 2022 (Amendments
   upto date)"*.
2. The answer is in **Urdu**, and renders the data-half chunk's English source
   label as *"ہمارے ڈیٹا کے مطابق"* — a translation, not the label.

Both are correctly out of reach of a verbatim string test, and **loosening
condition (a) to fuzzy matching is the wrong fix** — it is the half of the test
that keeps an evasive answer out. The right fixes are upstream and separable:
repair the OCR artefact in the ingest label, and give the composed chunk a
`source` the generator can carry across languages. Evidence:
`module101_class_and_paraphrases.json`, KB5 run 2, with the window's seven
source labels captured alongside.

### 8b. Module 120 — Meta-Analysis serves an exempted answer with no caveat

`verify_grounding()` is called by eleven agents; only Semantic Search was
wired to surface `citation_format_degraded`. Meta-Analysis is the agent Module
71 measured this same check rejecting **10 of 12** times on G6, so the
exemption will now fire there — and its answer will be served without the
caveat that says the claims are not individually traceable.
`CITATION_FORMAT_DEGRADED_CAVEAT` is exported from `verifier.py` for exactly
this. Not done here because `meta_analysis.py`'s synthesis path is a live
parallel track's file. Low severity (the answer is judge-cleared either way),
but it is a silent difference and should not stay one.

### 8c. Module 121 — `_check_fabricated_case_ids()` is blind on any answer without `[Document N]`

Found while writing §3's pre-check guard test. That check only inspects
`[Document N, CASE-ID]`-shaped citations, so on an answer that carries no
`[Document N]` marker at all it can **never** fire — an invented FIR number in
a citation-less answer is invisible to it. Before this module that did not
matter, because such an answer was discarded by `_check_no_citation()` anyway;
now some of them are served, so the blind spot becomes reachable. The exemption
is gated on the LLM judge precisely because of this, and Arm B (§7.2) shows the
judge holding 3 of 3 against exactly this shape — but a deterministic check
should not depend on a sampled one. The fix is to teach
`_check_fabricated_case_ids()` to scan bare identifiers in prose, not only
inside citation brackets. Evidence:
`tests/test_verifier.py::test_module101_a_deterministic_pre_check_still_overrules_the_exemption`,
whose docstring records why it had to use a temporal pre-check instead.

### 8d. Reported, not filed as new

- **The filed diagnosis for this module was wrong** (§0). Row 101's description
  — *"the aggregate has already returned gold's figure … nothing failed except
  the check"* — is right about the symptom and wrong about the mechanism. The
  row is corrected rather than closed silently.
- **The local/cloud generation confound** (§1.4) is a measurement hazard for
  every module on this programme, not a code defect. Any arm that does not
  record which model answered is not comparable with one that does.
- **P3 run 3** is a genuine LLM-judge grounding rejection of an Urdu answer,
  with two itemised Urdu claims. It is the judge doing its job on a language
  the corpus is thin in, adjacent to Module 92's family. One run of three; not
  enough to file.

---

## 9. Artefacts

- `module101_before.json` — KB9 × 6, pre-change code, local generation recorded.
- `module101_after.json` — KB9 × 6, this branch.
- `module101_cloud_confounded_arm.json` — the discarded 8-run arm, retained and labelled (§1.4).
- `module101_resample_before.json` / `module101_resample_after.json` — the fixed pair, 8 replays each.
- `module101_control.json` — §7.2's two forced-hallucination arms, 6 runs.
- `module101_class_and_paraphrases.json` — §7.3's 24 runs.
- `module101_paraphrases.json` — §6's pre-registered rewordings.
- `scripts/module101_{inprocess_runs,verifier_probe,verifier_resample,forced_hallucination_control,equality_control}.py`.
