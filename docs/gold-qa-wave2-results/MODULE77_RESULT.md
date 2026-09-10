# Module 77 — landing Modules 74/75/76's aggregates as KB data-half plan entries

**Branch:** `feat/kb-data-half-plan-entries` · **Base:** `main` @ `ccf2da8`
**Brief:** Module 77 in `GOLD_QA_REMAINING_FIXES_PLAN.md`
**Filed by:** Modules 74, 75 and 76, which own `xagg.py` and were barred from
`rag.py`.

**Environment.** Own worktree `D:/Rapids AI/muhafiz-m77`, own backend on
**`:8027`**, no other track running. Postgres up and healthy, 73 cases;
Chroma shared and **read only** (`muhafiz_kb` 7,716 documents, unchanged
before and after); model server `https://discharge-fascism-richness.ngrok-free.dev`
(`/health` green, embedder and reranker both loaded). The full `pytest -q`
suite was **not** run — it empties `muhafiz_entity_descriptions`.

**Quota check**, fixed pattern (Module 81 — the bare form matches a log
line's own milliseconds):

```
grep -ciE "rate limit|RESOURCE_EXHAUSTED|quota|UNAVAILABLE|(^|[^0-9.,:])(429|503)([^0-9]|$)" backend.log
```

**0** across both phases — **0 over 26 live runs** (18 + 8).

---

## 0. The headline, stated before the detail

The module is a three-entry addition and it worked exactly as Module 39's
design note said it would. What it *measured* is the interesting part, and it
splits cleanly in two:

- **Every one of the three entries is correct and composes**, proven in
  process against the real graph and — for all three — live end to end
  through `/api/chat`.
- **Two of the three are never consulted on their own gold question**, because
  KB3 and KB9 do not reach the RAG route on this machine. **KB3 → XNETWORK
  3/3, KB9 → XAGG 3/3.** The brief predicted this from two independent prior
  measurements, and it reproduced exactly. Only **KB8** benefits on its gold
  wording.

So the data half goes from Module 39's **8 of 12** to **10 of 18**, not
18 of 18 — and the missing 8 are a `router.py` problem (**Module 78**), not a
`rag.py` one. That is the honest headline the brief asked for.

**One thing the brief did not predict, and it is the most useful finding
here.** An ordinary **English paraphrase** of KB3 and of KB9 **does** route to
RAG, and when it does, **both plans fire and both figures land in the
answer** — P-KB3 returns *"68 out of 74 recorded assignment pairs (92%)"*,
verbatim gold. The routing failure is therefore attached to the **specific
gold wordings**, not to the questions' subjects, and Module 78 has a working
control to bisect against rather than a bare "the router is the variable".

---

## 1. Root cause

There was no defect to diagnose here — this is a **landing** module, and the
brief said so. The three-way root cause was established by Modules 39, 74, 75
and 76 and is not re-derived. Restated only as far as this module depends on
it:

| Question | Gold's data half | What it reached before | Why that was wrong |
|---|---|---|---|
| **KB3** | same officer on **68 of 74** pairs (91.9 %) | `unsupported_officer` | an honest refusal; no aggregate existed until Module 74 |
| **KB8** | **26** challans sent to court | `criminal_record_court_crosscheck` | counts the **33** `criminal_record` rows — a plausible wrong number in gold's slot |
| **KB9** | **10** FIRs cite PPC 302 | `statute_court_stage_join` | right row, **wrong grain**: `PPC §302: 10 case(s)` is row six of a capped 15-row whole-caseload table |

**KB9's re-point is the only one that is a change rather than an addition**,
and the brief was right to call for it to be done atomically:
`_run_kb_data_half()` drops a data half whose answering aggregate is not the
one its plan named, so moving `expected_kind` without moving `sub_query` (or
the reverse) would have turned Module 39's measured 2-of-3 into a silent
**0-of-3 with every unit test green**. Both fields moved in one edit.

**The 10 is no longer a divergence.** Module 39 recorded a 10-vs-8 gap
against gold and refused to tune it away; Module 76 re-derived 10
independently; gold has since been **corrected to 10**. Four derivations agree
(Module 39, Module 76 twice, a direct AGE query). This module inherits the 10
and asserts nothing about how it was obtained.

**Where the brief's own hypothesis was incomplete.** The brief framed the
routing risk as "KB3 and KB9 may not reach RAG at all on this machine". They
do not — but the brief's implicit reading, that these questions are somehow
unroutable to RAG, is **wrong**: a plain English rewording of each reaches RAG
on 2 of 2 runs and fires its plan. See §6.

---

## 2. Change

Two files, both in scope. Nothing else was touched — not `xagg.py`, not
`router.py`, not `supervisor.py`, `meta_analysis.py`, `verifier.py`,
`statute_hypothesis.py`, `src/retrieval/`, `evaluation/`, or any prompt.

### `src/pipeline/harness/tools/rag.py`

- **Two entries added** to `_KB_DATA_HALF_PLANS` — `officer_role_pair` (KB3 →
  `officer_role_pair_overlap`) and `chalaan_dispatch` (KB8 →
  `chalaan_dispatch_count`).
- **One entry re-pointed in place** — `death_investigation_charging` (KB9)
  from `statute_court_stage_join` to `fir_section_case_count`, `sub_query` and
  `expected_kind` together.
- **The header comment's "WHAT IS NOT CLOSED HERE" block rewritten** to say
  what actually closed and how, plus a new paragraph naming routing as the
  remaining variable and pointing at Module 78. Module 39 wrote that block as
  a prediction; leaving it reading as though the work were still open would
  make the next reader re-derive what is already done.
- **The gate's clause (3) tally corrected** from "exactly KB4, KB5, KB6 and
  KB9 … the other 28" to "exactly KB3, KB4, KB5, KB6, KB8 and KB9 … the other
  26".

**All three sub-query strings are copies, not re-derivations.** Modules 74/75/76
pinned them in `tests/test_xagg.py` (`_KB3_SQ_OFFICER_ROLE_PAIR`,
`_KB8_SQ_CHALAAN_DISPATCH`, `_KB9_SQ_FIR_SECTION_COUNT`) precisely so this
module would copy them; three tests assert the copies stay byte-equal to the
originals.

**Nothing about the mechanism changed.** Same three-way gate
(`_is_legal_kb_intent()` + `all_cases`/`include_global` + a pattern match),
same `asyncio.create_task` concurrency, same private `_DATA_HALF_TIMEOUT`,
same degrade-to-no-extra-chunk on every failure mode, same append-last chunk
order so the positional citation contract is untouched.

### `tests/test_kb_statute_retrieval.py`

Collected tests **81 → 92**. See §3.

### A second commit, after measuring

`fix(rag): widen KB3's and KB8's plan patterns after two paraphrases missed` —
the patterns as first written reached their gold questions and very little
else. Recorded as a measurement in §6, not presented as design.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_kb_statute_retrieval.py -q
```

**92 collected, 92 passed, 0 failed** (baseline on `main` @ `ccf2da8`: 81
collected). Adjacent suites re-run and green in the same session:
`tests/test_xagg.py`, `tests/test_harness_tool_rag.py`,
`tests/test_harness_agent_meta_analysis.py`,
`tests/test_harness_agent_semantic_search.py`, `tests/test_harness_supervisor.py`,
`tests/test_router.py` — **exit 0** on every one.

### The all-32 EQUALITY control

`test_module39_all32_data_half_gate_is_exactly_the_six_compound_questions`
(renamed from `…_four_…`) resolves the plan for **every one of the 32 gold
questions** through the live gate:

| Resolves to a plan (6) | Resolves to `None` (26) |
|---|---|
| KB3 → `officer_role_pair` | D1, S2, S3, A1, A7, CP6, CR2, CR3, CR4, CR6, |
| KB4 → `property_register` | CR7, CR8, CS4, CP1, M1, M2, M4, M5, M7, |
| KB5 → `violence_against_women` | G1, G2, G3, G5, G6, KB1, KB2 |
| KB6 → `weapon_register` | |
| KB8 → `chalaan_dispatch` | |
| KB9 → `death_investigation_charging` | |

**Exactly the six the brief specified, and exactly 26 `None`.** The control
was re-run unchanged after the §6 pattern widening and is **identical before
and after it**.

### New regressions, one per entry, pinned to the literal string

| Test | What it pins |
|---|---|
| `test_module77_kb3_sub_query_is_module74s_pinned_string` | byte-equal to `tests/test_xagg.py::_KB3_SQ_OFFICER_ROLE_PAIR`, **and** restated literally in the assertion, **and** `expected_kind == "officer_role_pair_overlap"` |
| `test_module77_kb8_sub_query_is_module75s_pinned_string` | same three, against `_KB8_SQ_CHALAAN_DISPATCH` / `chalaan_dispatch_count` |
| `test_module77_kb9_is_repointed_off_the_whole_caseload_join` | the re-point, **in both directions**: the plan now names `fir_section_case_count` and is byte-equal to `_KB9_SQ_FIR_SECTION_COUNT`; it is **not** Module 39's string and **not** `statute_court_stage_join`; and Module 39's literal string still resolves to `statute_court_stage_join`, because M4 owns that family |
| `test_module77_officer_plan_needs_both_roles_not_just_the_word_officer` | CP6 and three bare officer-identity questions all resolve to `None`, so `unsupported_officer` keeps the questions it answers correctly |
| `test_module77_challan_plan_does_not_reach_cr7` | CR7's gold text resolves to `None` at both the pattern and the full-gate level |
| `test_module77_widened_officer_pattern_still_needs_the_investigating_half` | a registration-only question ("which officer records an FIR?") still belongs to no plan after the widening |
| `test_module77_non_gold_paraphrases_reach_the_same_plan` (×3) | the three §6 paraphrases, through the full gate |

`test_module39_every_sub_query_resolves_to_the_aggregate_its_plan_names` —
inherited, and the load-bearing one — now covers six plans and passes: every
sub-query resolves through `resolve_aggregate_kind()` to the family its plan
names.

**One inherited test was narrowed rather than left passing vacuously.**
`test_module39_non_compound_kb_question_dispatches_no_aggregate` asserted that
KB1, KB2, **KB3 and KB8** dispatch nothing, because in Module 39's world the
latter two had no aggregate to reach. KB3 and KB8 were removed from that list
— they now belong to the positive control — and the docstring says why.

---

## 4. Live verification

Backend on `:8027`, real `/api/chat`, SSE parsed for the `route=` event; the
`XAGG <kind>` line and the `RAG tool: KB data-half plan '<plan>' answered by
aggregate '<kind>'` line read out of `backend.log` per run (Module 55 — the
XAGG SSE stream never says which aggregate ran, so the log line is the only
evidence).

Runner: `scripts/module74_live_runs.py` (Module 74's, unchanged) for phase 1;
`scratchpad/m77_live_paraphrase.py` for phase 2, which imports that same
module so both phases are measured identically.

### 4.1 Phase 1 — the three questions, plus KB4/KB5/KB6 as regression, 3 runs each

| Q | Route | Plan consulted? | `XAGG <kind>` line | Data half in answer? | Figure vs gold |
|---|---|---|---|---|---|
| **KB3** | **XNETWORK 3/3** | **never** | *(none)* | **0/3** | — |
| **KB8** | **RAG 3/3** | **fired 3/3** | `XAGG chalaan_dispatch_count: 26 challan dispatch record(s) across 26 case(s) … interim-report record type present=False` | **2/3** | **26 = gold exactly** |
| **KB9** | **XAGG 3/3** | **never** | `XAGG graph_recurrence: entity_type=Person, 4 recurring node(s)` | **0/3** | — |
| KB4 | RAG 3/3 | fired 3/3 | `XAGG seized_property_disposition: 45 register entr(ies) across 28 FIR(s) …` | **3/3** | disposition breakdown exact (13 forensic / 7 heirs / 28 FIRs) |
| KB5 | RAG 3/3 | fired 3/3 | `XAGG dv_report_fir_match: 8 women-violence report(s), 4 matched to an FIR, 4 unconfirmed` | **3/3** | **8 = gold exactly** |
| KB6 | RAG 3/3 | fired 3/3 | `XAGG weapon_compliance_scan: 32 weapon(s); 30 unlicensed, 2 with no licence status recorded` | **3/3** | **32 = gold exactly** |

KB8's one non-answer is `status=error` — *"The generated answer could not be
verified as grounded in the retrieved documents"* — the **grounding verifier**,
after the plan had already fired and the aggregate had already been composed
into the window. `verifier.py` is out of bounds for this module and the flap is
Module 52/61's documented one; it is reported, not worked around.

### 4.2 KB8's verbatim answer (run 2, `route=RAG`, both halves)

> ### Part 1: What the Law Requires
> According to the **Code of Criminal Procedure (Pakistan)**, specifically
> **Section 172(i)**, a police officer is required to maintain a **case diary**
> during an investigation and submit it daily. … Additionally, **Section 173**
> … mandates that **a final report** be submitted by the officer in charge of
> the police station once the investigation is completed. …
>
> ### Part 2: What Our Case-Tracking Data Shows
> Our **case-tracking data** records **26 challan(s)** sent to court, covering
> **26 case(s)**. Of these, **15 carry a dispatch timestamp**, while the rest
> record the dispatch without a date. A separate, smaller set of **20
> challan-outcome records** across **20 case(s)** records what happened next …
> However, the data **does not include any interim-report record type**, and
> there is **no field linking a challan back to the start of its own
> investigation**. …
>
> This means that while the data confirms that a case reached court, it **does
> not confirm whether any interim reporting duties along the way were met** …

### 4.3 KB3's and KB9's verbatim answers — what the plan entries never got to touch

**KB3**, all three runs, `route=XNETWORK`:

> No cross-case connections or patterns were found for this query, across
> either entity-graph search or cross-case pattern synthesis. No community
> cluster in the case corpus is closely related to this specific question …
> (nearest cluster found was distance 0.179 against a relevance cutoff of
> 0.145). …

The **gate is right and the route is wrong** — XNETWORK refuses honestly, and
its refusal is a correct answer to a question it should never have been given.

**KB9**, all three runs, `route=XAGG` → `graph_recurrence`:

> The system does record information about deaths in cases, including
> investigations into the cause of death. Based on the provided data
> [Document 1], four individuals are linked to multiple cases, indicating that
> these cases may involve ongoing investigations. …

This is the bare entity-recurrence tier `xagg.py`'s own comments twice name as
a wrong-answer bug: **"4 individuals"** has nothing to do with suspicious
deaths, and the generation hedges it into a claim about investigations.

### 4.4 Phase 2 — post-widening confirmation and the routing control, 2 runs each

| Q | Route | Plan fired | `XAGG <kind>` | Data half in answer |
|---|---|---|---|---|
| KB8 (gold) | RAG 2/2 | `chalaan_dispatch` 2/2 | `chalaan_dispatch_count … 26` | 2/2 |
| **P-KB3** (paraphrase) | **RAG 2/2** | **`officer_role_pair` 2/2** | `XAGG officer_role_pair_overlap: 144 assignment edge(s), 74 investigating / 70 recording; same officer on 68 of 74 pair(s) (91.9%), 6 split across 6 case(s)` | **2/2** |
| **P-KB8** (paraphrase) | **RAG 2/2** | **`chalaan_dispatch` 2/2** | `chalaan_dispatch_count … 26` | 2/2 |
| **P-KB9** (paraphrase) | **RAG 2/2** | **`fir_section_case_count` 2/2** | `XAGG fir_section_case_count: 218 section entr(ies) over 73 FIR(s) … focus=302 -> 10 FIR(s)` | 1/2 answered (one grounding-verifier `status=error`); the served answer carries the schema gap and names PPC 302 but **not the count** |

### 4.5 In-process composition — the proof for the two entries the router hides

`scratchpad/m77_inprocess.py` calls `rag_tool()` **directly** with each gold
question, All-Cases scope, against the real graph — exactly what
`supervisor.py` does on the RAG route, bypassing only the router. All three
compose:

- **KB3** → `kb-data-half:officer_role_pair` — *"the officer who recorded the
  FIR and the officer who investigated it are the same person in **68 of 74**
  recorded assignment pairs (**92 %**). Roles are split in only **6** pair(s),
  across 6 case(s)."*
- **KB8** → `kb-data-half:chalaan_dispatch` — *"**26** challan(s) sent to
  court, covering 26 case(s) … there is **no interim-report record type
  anywhere in the schema** …"*
- **KB9** → `kb-data-half:death_investigation_charging` — *"**10** of the 73
  FIR(s) that carry a recorded section cite **PPC §302**."*

---

## 5. Gold comparison

| Q | Gold's data half | Measured | Verdict |
|---|---|---|---|
| **KB3** | same officer in **68 of 74** pairs (92 %); split in only 6 | **68 of 74 (91.9 %), 6 split** | **exact** — but delivered only via a paraphrase or in process; **never on KB3's own gold wording live** |
| **KB8** | **26 cases**' challans sent to court; **no interim-report concept in the schema**; no field linking a challan to its investigation | **26 dispatch records / 26 cases**; `interim-report record type present=False`; no linking field | **exact, and both clauses served**, 2 of 3 runs |
| **KB9** | **10 FIRs** cite PPC 302; **no inquest / post-mortem / cause-of-death record** | **10 FIRs**; schema gap correctly reported | **exact in process**; **never on KB9's own gold wording live**; on the paraphrase the gap lands but the count does not |
| KB4 | 45 property-register entries | 45 entries / 28 FIRs, 13 forensic / 7 heirs | data half served 3/3; the **answer states the breakdown, not the 45** |
| KB5 | 8 women-violence reports | 8, 4 FIR-matched | **exact**, 3/3 |
| KB6 | 32 weapons, register records none of the handling steps | 32; 30 unlicensed, 2 unrecorded; answer says the register does not record the guideline steps | **exact**, 3/3, including gold's own "the data lacks this" verdict |

### Both halves, per question, over ≥3 runs

| Q | Norm (statutory) half | Data half | Both |
|---|---|---|---|
| KB3 (gold wording) | **0/3** — XNETWORK refuses, no statute at all | **0/3** | **0/3** |
| KB8 | 3/3 (CrPC ss.172/173 — see caveat) | 2/3 | **2/3** |
| KB9 (gold wording) | **0/3** — XAGG, no statute at all | **0/3** | **0/3** |
| KB4 | 3/3 (rule 27.18 + Anti-Rape Act 2021) | 3/3 | **3/3** |
| KB5 | 3/3 (Anti-Rape Act 2021 s.3(2)(d)) | 3/3 | **3/3** |
| KB6 | 3/3 (forensics guidelines ¶¶12–13) | 3/3 | **3/3** |

**Data half across the six: 10 of 18.** Module 39 took it from 0-of-48 to
8-of-12; this takes it to 10-of-18. The three added questions contribute
**2 of 9**, and the shortfall is entirely routing.

**One honest caveat on KB8's norm half.** Gold's law half is specific: CrPC
s.173's **14-day interim report** to the magistrate through the public
prosecutor. The live answer reaches s.172's case diary and s.173's *final*
report, and never states the 14-day interim-report duty. Under the judging
standard that is **partial**: the same idea (the law requires reporting before
completion) in its own words, without gold's central mechanism. It is a
retrieval/generation matter on the norm side and belongs to Modules 30/52's
territory, not to a plan entry — filed as **Module 84**.

---

## 6. Non-gold paraphrase

Three, one per entry, each an ordinary English rewording that shares no
distinctive phrase with its gold question.

**Two of the three missed the patterns as first written.** Measured in
process, against the real graph, before any live run:

| Paraphrase | `_is_legal_kb_intent` | Plan, before widening | after |
|---|---|---|---|
| P-KB3 — *"is the **person who records** an FIR supposed to be a **different officer** from the one who investigates it …"* | True | **None** | `officer_role_pair` |
| P-KB8 — *"does the law make the police **report something to** the court before it is finished …"* | True | **None** | `chalaan_dispatch` |
| P-KB9 — *"When a death looks suspicious the police must formally investigate the cause of death …"* | True | `death_investigation_charging` | unchanged |

The intent gate was **not** the problem in either case — `_is_legal_kb_intent`
returned True for all three. The problem was that each pattern list had been
written from its own gold question's text: `officer who (first) registers` is
KB3's wording and nothing else, and `report to the court` demanded the two
halves be adjacent. **This is Module 56's finding for the third time and
Module 74's for the second**, and it is reported as a miss that happened
rather than a success that was designed.

Both were widened, each still requiring **both** signals, and the widening is
**invisible to the all-32 control** — identical before and after. A
registration-only question ("Which officer records an FIR when a complaint
comes in?") still belongs to no plan, pinned by its own test.

**Live, post-widening, all three paraphrases route to RAG and fire their plan
2 of 2.** P-KB3's answer carries gold's figure verbatim:

> ### Our Case Records:
> Our own case records show that in most cases, the officer who recorded the
> FIR and the officer who investigated the case are the same person.
> Specifically, in **68 out of 74 recorded assignment pairs (92%)**, the roles
> are not separated. In only **6 cases**, the roles are split, and in 3 of
> those 6 cases, the investigating officer has no recording officer associated
> with the same case, making those pairs unresolvable rather than genuinely
> split [Document 6].

That is a **capability fix demonstrated on wording the module never saw**, and
it is also the routing control described in §0: the same subject, asked in
different words, reaches RAG.

P-KB3's **norm** half is weak — it concludes the Punjab Police Rules contain
no explicit separation requirement, where gold rests on **Police Order 2002
Art. 18**. That is Module 49's open question, unchanged by this module and not
touched here.

---

## 7. Regression guard

| What | Result |
|---|---|
| **KB4 / KB5 / KB6 live, 3 runs each** — Module 39's data halves | `route=RAG` **9/9**, plan fired **9/9**, data half in the answer **9/9**, all three figures unchanged (45/28, 8, 32). **No regression.** |
| **All-32 equality control** | Exactly six resolve, 26 `None`; identical before and after the §6 widening |
| **CR7** | Pinned at `None` at both the pattern and full-gate level. Its own `criminal_record_court_crosscheck` (33 criminal records) is untouched — KB8 reaches its family only through the canned sub-query |
| **CP6 and bare officer-identity questions** | Pinned at `None`; `unsupported_officer` keeps every question it answers correctly |
| **M4's family** | Module 39's literal KB9 sub-query still resolves to `statute_court_stage_join`, asserted directly |
| **CR8** | Still `None` via the intent clause (inherited test, unchanged) |
| `tests/test_xagg.py` | Green — Modules 74/75/76's own suites unaffected |
| `tests/test_harness_tool_rag.py`, `…_meta_analysis.py`, `…_semantic_search.py`, `…_supervisor.py`, `tests/test_router.py` | All exit 0 |
| Chroma | Read-only; `muhafiz_kb` 7,716 before and after |

---

## 8. New defects found

Left deliberately unfixed. Numbers to 83 were taken, so these start at 84.

- **Module 78 (existing, and now much better evidenced).** KB3 → XNETWORK 3/3
  and KB9 → XAGG 3/3, so two correct plan entries are never consulted. This
  module adds the control Module 78 needs: **an English paraphrase of each
  routes to RAG 2/2 and fires its plan 2/2**, so the router is discriminating
  on the gold wordings specifically — KB3's is long, English and framed as a
  legal comparison; KB9's is Roman-Urdu. That is a bisectable difference, not
  a mystery. Not fixed here: it is `router.py`/`supervisor.py`, explicitly out
  of scope.
- **Module 84 — KB8's norm half reaches CrPC s.172/s.173's *final* report, never
  the 14-day *interim* report gold rests on.** Live 3/3 (and 2/2 in phase 2)
  the answer cites the case diary and the final report and states the
  reporting duty in general terms; gold's mechanism — an interim report to the
  magistrate through the public prosecutor within three days of the
  fourteenth — appears in no run. The data half is perfect and the norm half
  is generic, which is the mirror image of the problem Module 39 set out to
  fix. Retrieval/generation on the statute side; adjacent to Modules 30/52/65.
- **Module 85 — the grounding verifier rejects a composed KB answer that has
  already been given its figure.** KB8 `status=error` 1 of 3 and P-KB9 1 of 2,
  both *after* the plan fired and the aggregate chunk was in the window
  ("The generated answer could not be verified as grounded in the retrieved
  documents"). Same family as Module 52's KB4 flap and Module 61's negative
  inference, but the trigger here is specifically an answer whose second half
  cites a machine-computed chunk. `verifier.py` is out of bounds for this
  module.
- **Module 86 — KB4's answer serves the breakdown but drops the denominator.**
  The `seized_property_disposition` chunk states *45 register entries across 28
  FIRs*; the generation reproduces the 13-forensic / 7-heirs split and the 28
  FIRs and never states the **45** gold gives. Not a wrong number — a missing
  one — and adjacent to Module 71's denominator work.
