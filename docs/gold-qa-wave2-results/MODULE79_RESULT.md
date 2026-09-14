# Module 79 — A compound question can need two aggregates, and every plan structure is single-aggregate by construction

**Branch:** `feat/chained-aggregate-plans` (worktree `D:/Rapids AI/muhafiz-m79`), from
`origin/main` @ `50862d6` (Module 144 merged); rebased onto `01ddc18` (Modules
150/151 filed, Module 143 merged) before the PR, with the suites re-run there.
**Date:** 2026-09-14.
**Verdict:** the plan shape now exists and works, in both plan structures,
and the live example is answered correctly **in-process 6 of 6** — *"PPC §34
… 40 FIR(s) … فیصل آباد 12, لاہور 10, راولپنڈی 4, …"* — on the local model,
with the chained step costing **0.18–0.54 s** against a 150 s per-sub-query
deadline. **It is not yet live for the live example**, and that is a
measured dependency, not a gap in the mechanism: reaching a Meta-Analysis
plan from the XAGG route needs a `supervisor.py::_META_ANALYSIS_TRIGGER_PATTERNS`
entry, which is Module 145's file (§1.3, §4.2). The two-aggregate form IS
live on the RAG route: KB9's data half now carries gold's second element
(the heirs figure) alongside the first (§5.3). Every pre-existing plan is
byte-identical in what it dispatches (§3, §7). Two premises of the brief did
not survive contact with the code and are stated rather than worked around
(§1.2, §8).

---

## 1. Root cause — the live example, traced

### 1.1 What ran, and why it stopped at one step

*"How is the most frequently cited offence pattern distributed across
districts?"*, traced deterministically through the same functions the live
path calls (`module79_probe_dispatch.py`, both trees):

| layer | pre-module | what it means |
|---|---|---|
| `router._deterministic_route_override()` | `None` → the LLM router; live it said **XAGG** 3/3 | cross-case aggregate, correctly |
| `supervisor.classify_to_subagent()` → `_xagg_answers_in_one_call()` | `_match_decomposition_plan()` → **None**; `resolves_to_specific_aggregate()` → **True** | "XAGG has a purpose-built one-call answer for this text — skip Meta-Analysis" |
| sub-agent | **Large-Scale Aggregate** (one `xagg_tool()` call) | — |
| `xagg.resolve_aggregate_kind()` | **`top_districts_by`** (`_DISTRICT_KEYWORDS`, the bare word "districts") | cases per district, whole caseload |
| answer | *"The provided document … does not specify the most frequently cited offence pattern … It only lists the total number of cases recorded in various … districts: فیصل آباد: 19, لاہور: 18, …"* — live, **3 of 3** (§4.1) | honest, and not the question |

The question is two steps — (1) which section is cited by the most FIRs,
(2) that section's FIRs by district — and both existed as single-call
aggregates: `fir_section_case_count` (Module 76) already logs
`top=PPC §34=40`, and `district_breakdown` is G6's own first sub-query. What
did not exist was any plan that runs (1) and feeds its output into (2):

- `rag.py::_KbDataHalfPlan` has one `sub_query` and one `expected_kind`;
- `meta_analysis.py::_DecompositionPlan` fans out N sub-queries with
  `asyncio.gather()` — **independent by construction**; no sub-query's
  output reaches another;
- and the question never reached Meta-Analysis anyway, because the one-call
  skip guard fired (row 3 above).

### 1.2 A premise of the brief that is false, measured before any code

The brief said step (2) "is now *district_breakdown with section=PPC §34*,
which works" after Module 144. It does not. Module 144's
`FILTERABLE_AGGREGATE_KINDS` is exactly `total_count`,
`station_or_category_counts`, `fir_section_case_count`,
`total_accused_count`, `offender_age_profile` — **`top_districts_by` is not
in it**. Run in-process against the live graph (`scratchpad/m79_premise.py`):

```
"How many cases citing section 34 are registered in each district, across all cases?"
  resolve_aggregate_kind -> top_districts_by      extract_aggregate_filters -> section=34
  result kind district_breakdown, "filters_applied" in result: False
  - فیصل آباد: 19 case(s)  - لاہور: 18 case(s)  - راولپنڈی: 10 ...   <- the UNFILTERED table, nothing saying so
```

So a chained step could not be "run step (2) with the section in its text".
The chain narrows the second aggregate through the parameter **every**
aggregate already accepts — Milestone E1's `jurisdiction_case_ids` — with
the allow-list produced by Module 144's own `resolve_filter_case_ids()`.
That makes the second aggregate unaware it is being chained, keeps
`xagg.py` untouched, and works for any second aggregate, filterable or
not. The standalone defect (a district breakdown silently ignores a section
in the question) is filed, not fixed (§8, **157**).

### 1.3 Why the live example still cannot reach the plan live

`_xagg_answers_in_one_call()` consults `_match_decomposition_plan()` first,
so a matched plan vetoes the one-call skip. But the veto only sends the
query to the `elif allow_meta_analysis and any(_META_ANALYSIS_TRIGGER_PATTERNS)`
branch, and with no trigger matching it falls to `else` →
`_ROUTE_TO_SUBAGENT["XAGG"]` = Large-Scale Aggregate again. The same trace
on this tree:

```
LIVE   | kind: top_districts_by | plan: most_cited_section_by_district | one_call: False | sub-agent on XAGG: Large-Scale Aggregate
```

Module 29 built the plans and the triggers as two parallel lists (CR3/G1/G6
each have an entry in both). The trigger list is in `supervisor.py`, which
Module 145 owns this session, so it is not edited here. The plan is verified
in-process through the whole sub-agent (§4.2), exactly as the brief
anticipated, and the one-line trigger is recorded as the dependency (§8,
**158**).

---

## 2. The change, and what was not changed

### 2.1 Files touched

| file | change |
|---|---|
| `src/pipeline/harness/tools/chained_aggregate.py` | **new.** `ThenStep` (the second aggregate: `sub_query`, `expected_kind`, optional `take` / `filter_field` / `lead`), `run_aggregate_chain()`, `pluck()`, `filters_for()` |
| `src/pipeline/harness/agents/meta_analysis.py` | `_ChainedSubQuery`; `_DecompositionPlan.chained: tuple = ()` (**defaulted**, appended after `sub_queries` in `_decompose()`); `_dispatch_one()` takes a one-line branch to `_dispatch_chained()` for a chained member; two new plans, `most_cited_section_by_district` and `busiest_district_by_section`, declared **last** in `_DECOMPOSITION_PLANS`; two dispatch-string constants |
| `src/pipeline/harness/tools/rag.py` | `_KbDataHalfPlan.then: Optional[ThenStep] = None` (**defaulted**); `_run_kb_data_half()` takes a two-line branch to `_run_kb_data_half_chain()` when `then` is set; KB9's entry gains `then=` (KB4's property string, verbatim) |
| `tests/test_module79_chained_plans.py` | 34 tests, §3 |
| `scripts/module79_inprocess_runs.py`, `scripts/module79_live_runs.py`, `scripts/module79_kb9_bisect.py` | runners |
| `docs/gold-qa-wave2-results/module79_*` | run records, controls, the pre-written paraphrases |
| `GOLD_QA_REMAINING_FIXES_PLAN.md` | row 79 closed; 152–154 filed |

**Not touched:** `src/pipeline/xagg.py` — not `resolve_aggregate_kind()`,
not its predicates, not `FILTERABLE_AGGREGATE_KINDS`, no new aggregate
function; `src/pipeline/harness/supervisor.py`; `src/pipeline/orchestrator.py`
(no rendering twin needed — the chain renders through the existing
`harness/tools/xagg.py::_render_aggregate_text()`, once per half);
`src/pipeline/retry_gate.py` and `semantic_search.py` (Module 143's);
`src/pipeline/aggregate_filters.py` (read, not edited); every existing plan
entry's `patterns`, `sub_queries` and `synthesis_goal`.

### 2.2 The mechanism

```
run_aggregate_chain(first_query, first_kind, then, gateway, user_id, user_role):
    first  = await run_aggregate(first_query, ...)              # role gate + audit inside, as always
    guard: first["kind"] == first_kind, else drop (rag.py's defence-in-depth rule)
    if then.take:                                              # e.g. "sections.0.section_code"
        value, record = pluck(first, then.take)                # "34", {key: "PPC §34", fir_count: 40, ...}
        allow, _ = await resolve_filter_case_ids(filters_for(then.filter_field, value))   # Module 144's resolver
    second = await run_aggregate(then.sub_query, ..., jurisdiction_case_ids=sorted(allow))  # Milestone E1's parameter
    guard: second["kind"] == then.expected_kind, else drop
    text = render(first) + lead-line(record) + "Narrowed to the 40 case(s) under section 34 ..." + render(second)
```

`take` is optional. With it, the step is *"top-N of X, then break that down
by Y"*; without it, the step is a plain two-aggregate plan (KB9's shape: a
charging-side figure and a property one, no dependency). `filter_field` is
any of Module 144's six parameters, so the same mechanism feeds a district
(`busiest_district_by_section`, `counts.0.district`), a section, a date or
an age into the second aggregate. Nothing names PPC §34 or any other value.

**Why the two aggregates are called directly, not as two Supervisor
passes.** A Meta-Analysis sub-query is a full Supervisor pass — route, XAGG
agent, a paraphrase LLM call, a verifier verdict — under one shared
wall-clock deadline that today's run showed five of G6's sub-queries
falling out of (Module 150). Two of those in series would cost two model
round trips for a rendering the synthesis step re-paraphrases anyway. The
chain calls `run_aggregate()` twice and hands the deterministic rendering
on as the sub-answer — the same correct-by-construction text Module 53's
salvage already serves when a paraphrase is lost to the deadline, for the
same reason. Measured cost: **0.18–0.54 s** for the whole chain (§4.2).

**Why every existing plan is byte-identical.** `chained` and `then` are
defaulted fields at the end of frozen dataclasses. `_decompose()` returns
`[*plan.sub_queries[:cap], *plan.chained]`, which for every pre-existing
plan is `list(plan.sub_queries)` — asserted for all three (§3). The string
branch of `_dispatch_one()` is unchanged; the chained branch is an
`isinstance` check on the first line. `_run_kb_data_half()`'s single-step
path is the pre-module code; the chain is a two-line early return above it.

### 2.3 Rendering — what the synthesis / evaluator sees

```
73 FIR(s) carry a recorded section, 218 section entr(ies) across 36 distinct section(s). ...
  - PPC §34: 40 FIR(s)
  - Arms Ordinance 1965 §13: 29 FIR(s)
  ...

PPC §34 is the section cited by the most FIRs: 40 of the 73 FIR(s) that carry a recorded section.
Narrowed to the 40 case(s) under section 34 (the value read from the first result), the second aggregate gives:

- فیصل آباد: 12 case(s)
- لاہور: 10 case(s)
- راولپنڈی: 4 case(s)
- کراچی ایسٹ: 4 case(s)
- چنیوٹ: 3 case(s)
- حیدر آباد: 3 case(s)
- اسلام آباد: 2 case(s)
- کراچی وسطی: 2 case(s)
```

(12+10+4+4+3+3+2+2 = 40; the eight districts are the nine minus Multan,
which has no §34 FIR.) The log line — the only evidence of what a live run
computed — is `XAGG chained '…': fir_section_case_count -> take
sections.0.section_code='34' -> district_breakdown narrowed to 40 case(s); 0.18s`.

---

## 3. Unit tests — `tests/test_module79_chained_plans.py`, 34 tests, both directions

Run: `PYTHONPATH=. python -X utf8 -m pytest tests/test_module79_chained_plans.py`.

| group | what it pins | cases |
|---|---|---|
| pure pieces | `pluck()` returns `(value, parent record)` and `(None, None)` for anything missing; `ThenStep` refuses `take` without `filter_field` and an unknown field; `filters_for()` fills exactly one field (district → stored name + display name) | 8 |
| runner | **the central pin**: step two receives `jurisdiction_case_ids == sorted(allow-list)` from `resolve_filter_case_ids(AggregateFilters(section="34"))`, both halves and the lead are in the text, the role reaches both calls; no-`take` runs step two unnarrowed; family mismatch on **either** side drops the chain; an empty first result serves the first half alone and does not run step two; `PermissionError` → `denied`, an upstream exception → `failed`, never raises | 5 |
| Meta-Analysis | the live example, both pre-written paraphrases, an Urdu rewording and the swapped shape each match the intended plan; five one-call questions (CP1's shape, "most common section" with no Y, a plain per-district count, Module 148's shape, "which station has the most") match **none**; the two new plans match **none of the 32 gold questions** and the all-32 match set is still exactly `{CR3, G1, G6}`; **every pre-existing plan (`record_consistency`, `caseload_review`, `orientation_note`) decomposes to exactly `list(plan.sub_queries)`, all strings, `chained == ()`** — the "existing single-step plan is unchanged" assertion the brief asked for; every chained dispatch string resolves to the family the step names; a chained member is dispatched directly and a string member through `Supervisor.handle` (each forbidden for the other); `denied`/`mismatch` outcomes degrade like a string sub-query's; end-to-end `meta_analysis()` synthesises over the chained text under the step's label with no Supervisor pass and no LLM decomposer call | 18 |
| RAG | KB9's plan carries `then` with `expected_kind == "seized_property_disposition"`, no `take`, and a string equal to entry (2)'s; every other plan has `then is None`; a single-step plan still takes the unchanged `xagg_tool` path (chain runner forbidden) and returns the byte-identical chunk dict; KB9's two-step plan takes the chain (`xagg_tool` forbidden) and returns one chunk with the same id/shape; a non-ok chain → `None` | 3 |

**Before / after, measured** (`scratchpad/pre` = `git archive origin/main`):

- pre-module tree: the file **fails at collection** (`ModuleNotFoundError:
  src.pipeline.harness.tools.chained_aggregate`); with only the runner file
  dropped in, still collection (`cannot import name '_ChainedSubQuery'`);
- pre-module tree with the imports guarded so the plan-side assertions run:
  **16 failed, 18 passed** — every plan-match, decomposition, dispatch and
  RAG test fails; the runner's own tests and the negative controls pass, as
  they should (`scratchpad/before_unit_guarded.txt`);
- this tree: **34 passed**.

Broader suites on this tree, all green (named files only, never a bare
`pytest tests/`): `test_harness_agent_meta_analysis.py`,
`test_harness_tool_rag.py`, `test_kb_statute_retrieval.py`,
`test_harness_supervisor.py`, `test_xagg.py`, `test_harness_tool_xagg.py`,
`test_harness_agent_large_scale_aggregate.py` — **971 passed** on the
branch point, and **1,015 passed** (with `test_retry_gate.py`) after the
rebase onto Module 143 — including Module 29's all-32 plan control, Module
41's supervisor guard tests and Module 144's 71.

---

## 4. Live verification — the live example, with wall time per run

Model server `/health` → 200 before every arm. **Every run below that
touched the chain answered on the local model server** (Qwen3 via
`…ngrok…/llm`); `Falling back to groq` lines are counted per run. The
machine was shared for the whole session: `muhafiz-m143`'s backend on
`:8143` was running a resumed gold-32 pass against the same model server,
so absolute times carry that contention.

### 4.1 Live, `/api/chat` — before (main checkout @ `50862d6`, `:8079`) and after (this worktree, `:8079`)

| arm | run | route | sub-agent evidence | `XAGG` line | answer served | model | s |
|---|---|---|---|---|---|---|---|
| before | 1 | XAGG | no plan line (one-call skip) | `district_breakdown: entity=Case, 9 district(s); فیصل آباد=19, لاہور=18, …` | *"The provided document [Document 1] does not specify the most frequently cited offence pattern or its distribution across districts. It only lists the total number of cases … فیصل آباد: 19 …"* | local | 88.9 |
| before | 2 | XAGG | same | same | same text | local | 53.6 |
| before | 3 | XAGG | same | same | *"… shows the number of cases across districts but does not specify the most frequently cited offence pattern …"* | local | 39.0 |
| **after** | 1 | XAGG | **no plan line** — Large-Scale Aggregate, one call | same 9-district table | **same text as before** | local | 47.6 |

The after row is the dependency of §1.3, reproduced live rather than
asserted: the plan matches (unit test, and the dispatch trace in
`module79_probe_dispatch.py`), the one-call skip is vetoed, and the query
still lands on the XAGG agent because no supervisor trigger sends it to
Meta-Analysis. The before and after answers are the same because the code
that ran is the same code.

### 4.2 In-process, the whole Meta-Analysis sub-agent (`scripts/module79_inprocess_runs.py`)

`meta_analysis()` called directly with a real platform-admin execution
context: plan match → chained step against the live graph → synthesis on
the model server → grounding verifier → validation. Records:
`module79_inprocess_after_run1.json`, `module79_inprocess_after.json`.

| run | status | chain wall (from its own log line) | whole sub-agent | model | `Falling back` | answer served |
|---|---|---|---|---|---|---|
| 1 | OK | **0.54 s** | 39.1 s | local | 0 | *"The most frequently cited offence pattern is **PPC §34**, mentioned in **40 FIR(s)** [Document 1]. Across districts … فیصل آباد: 12 · لاہور: 10 · راولپنڈی: 4 · کراچی ایسٹ: 4 · چنیوٹ: 3 · حیدر آباد: 3 · اسلام آباد: 2 · کراچی وسطی: 2"* |
| 2 | OK | **0.49 s** | 35.0 s | local | 0 | same figures, same wording |
| 3 | OK | **0.19 s** | 46.2 s | local | 0 | same figures, same wording |

**Against the Meta-Analysis deadline.** `META_ANALYSIS_SUBQUERY_TIMEOUT` is
150 s (`config.py`), shared by the fan-out from the moment it starts. The
chained step — two sequential aggregate calls plus the allow-list
resolution — completes in **0.18–0.54 s** (six measurements across §4.2 and
§6; the three cold-ish calls of `module79_chain_inprocess.json` were 0.50,
0.18, 0.18 s and 0.18, 0.17, 0.18 s for the swapped plan). That is three
orders of magnitude inside the deadline, and it is the reason the design
calls the aggregates directly (§2.2): the brief's concern — "a chained step
is two sequential aggregate calls" — would have been real had each been a
Supervisor pass at ~45 s on the 14B model (Module 150's measurement), and
is not real for two Cypher round trips. The 35–46 s the whole sub-agent
takes is the synthesis call and the verifier, i.e. the same cost any
one-sub-query Meta-Analysis pays.

**Caveat on the deadline finding.** These numbers are for a plan whose
fan-out is ONE chained step. A chained step added to an eight-member plan
like `orientation_note` would add ~0.5 s to that plan's fan-out, not a
ninth model call — but no existing plan was given one, and none should be
without re-measuring that plan (the same rule Module 110 applied to the cap).

---

## 5. Gold comparison — G1, G6, KB9, live, before and after

Same two backends as §4.1, one run each per arm. Both arms ran under the
`:8143` contention described above.

| id | arm | route | plan line | aggregates dispatched | `Falling back` | status | s | answer |
|---|---|---|---|---|---|---|---|---|
| **G1** | before | XAGG | `caseload_review` | the same 5 `XAGG` lines (`accused_relationship_breakdown`, `offender_age_profile`, `seized_property_disposition`, `incident_time_of_day`, `graph_recurrence` Person) | 1 | done | 221.7 | served as Module 71's deterministic sub-answer composition (synthesis not verified on that run) — age 24–49 mean 31.5 of 17/92; stranger 15 of 24 in 10 FIRs; … |
| **G1** | after | XAGG | `caseload_review` | **the same 5 lines** | 1 | done | 187.7 | synthesised: *"Of 92 distinct accused, 17 have recorded ages (mean 31.5, range 24–49), but 75 … no age data [Document 1] … 15 entries (62.5%) involve اجنبی (stranger) [Document 2] …"* |
| **G6** | before | XAGG | `orientation_note` | the same 8 `XAGG` lines | **4** | done | 195.8 | Faisalabad 19 / Lahore 18 / Rawalpindi 10; case mix 2024 vs 2026; 11 of 73 arrests (1 in 6.6); 15 min → ~23.4 h; 32 weapons; … |
| **G6** | after | XAGG | `orientation_note` | **the same 8 lines** | **4** | done | 192.2 | same eight findings, same figures, all nine districts listed |
| **KB9** | before | RAG | data-half plan `death_investigation_charging` → `fir_section_case_count` (829 chars) | 1 | 0 | **error** (grounding verifier — Module 151's class) | 148.6 | *"could not be verified as grounded"* |
| **KB9** | after | RAG | data-half plan → **`fir_section_case_count` + `seized_property_disposition` (2200 chars, 0.15 s)** | 2 | 0 | **error** (retrieval loop stopped at the 360 s cap; every evaluator attempt `relevant=False` on that run) | 360.1 | *"Document search was still retrying after 360 seconds …"* |

G1 and G6: **the plans dispatch exactly what they dispatched before** (same
`XAGG` lines, same count, same Groq fallback count — 1 and 4 — which is
Module 150's deadline mechanism and not this module's), and the all-32
control (§7) pins the dispatch lists as identical. The difference in G1's
served shape (composition vs synthesis) is the verifier's run-to-run
variance on a Groq-answered sub-query, seen in both directions on this
codebase before (Module 71).

### 5.3 KB9's heirs element — the bisect the live pair could not settle

The two live KB9 runs failed for two different pre-existing reasons, so the
question "does the second aggregate change KB9's outcome?" was answered
in-process instead, with the real `semantic_search()` sub-agent (real
retrieval, reranker, evaluator, generation, verifier), alternating arms so
contention hits both equally (`scripts/module79_kb9_bisect.py`,
`module79_kb9_bisect.json`). `then` = the plan as shipped; `single` = the
same plan with `then=None` (pre-module).

| arm | run | evaluator per attempt | verifier | served | s | heirs figure in answer | model |
|---|---|---|---|---|---|---|---|
| then | 1 | False, **True** | grounded | **OK** | 221.2 | **yes** — *"13 items sent to forensic labs, 7 items held for heirs"* alongside *"10 FIRs cite PPC §302"* | local |
| single | 1 | **True** | grounded | OK | 143.6 | no (not computed) | local |
| then | 2 | False, True | **rejected** — *"claims about system documentation gaps … lack direct support"* (Module 151's absence class) | abstained | 218.8 | — | local |
| single | 2 | False, True | grounded | OK | 196.2 | no | local |
| then | 3 | False, True | grounded | **OK** | 165.1 | no — the model listed §302 and the gap and dropped the property line | local |
| single | 3 | False ×6 (both scopes) | — | **abstained** at the loop cap | 505.4 | — | local |

Read plainly: served **2 of 3 in both arms**; the evaluator's first-attempt
verdict is noisy in both arms (its `then`-arm reason names the property
text — *"document 6 … mention FIR statistics and property disposition but
do not clarify …"* — so the second aggregate does cost the first attempt
more often, 3/3 vs 2/3, at n=3); the verifier rejected once in the `then`
arm and never in `single`, and the rejection reason is Module 151's, not
the property figure. **Gold's second element is now in the served answer
1 of 2 times it was served, and 0 times before.** That is the honest size of
the gain at this sample: the mechanism delivers the figure to the model;
the model keeps it about half the time. KB9's entry is shipped with `then`
because nothing measured says it costs a pass, and something measured says
it gains an element; if a larger sample shows the extra retry costs more
than the element is worth, the `then=` line is the one to remove.

---

## 6. Non-gold paraphrases — written before running (`module79_paraphrases.json`)

All four were pinned by unit test to their plan before any model ran, then
run through the whole sub-agent in-process (§4.2's runner; they cannot
reach the plan live for the reason in §1.3 — their dispatch trace on both
trees is in `module79_probe_dispatch.py`'s output, §1.3).

| id | question | plan matched | chain | whole sub-agent | model | answer served |
|---|---|---|---|---|---|---|
| P1_EN | Which offence section comes up most often in the FIRs, and which districts are those FIRs spread across? | `most_cited_section_by_district` | 0.18 s | 41.5 s | local | *"The offence section cited most often in FIRs is **PPC §34**, appearing in **40 FIR(s)** … فیصل آباد: 12 · لاہور: 10 · راولپنڈی: 4 · کراچی ایسٹ: 4 · چنیوٹ: 3 · حیدر آباد: 3 · اسلام آباد: 2 · کراچی وسطی: 2"* ✅ |
| P2_RU (Roman-Urdu) | Sab se zyada istemal hone wali dafa kaun si hai aur us ke cases kin zilon mein hain? | `most_cited_section_by_district` | 0.18 s | 68.2 s | local | *"The section cited by the most FIRs is **PPC §34**, with **40 FIR(s)** … distribution of these 40 cases across districts …"* (same eight rows) ✅ — answered in English because the in-process runner sets no `preferred_language`; live, the caller's language would drive the synthesis role as for G6 |
| P3_SWAP — a *different* top-then-breakdown shape (X and Y swapped) | Which district has the most FIRs, and what sections are those FIRs charged under? | `busiest_district_by_section` | 0.20 s | 92.4 s | local | *"فیصل آباد (Faisalabad) is the district with the most registered FIRs, with **19 case(s)** … PPC §34: 12 · Arms Ordinance 1965 §13: 9 · PPC §392: 7 · PPC §337-A(i): 5 · CNSA 1997 §9(c): 4 · …"* ✅ — the first aggregate's top row (a stored Urdu district name) fed Module 144's district filter, and the 19-case allow-list narrowed `fir_section_case_count` |

Pre-module, the same four resolve to `top_districts_by` ×3 and
`station_or_category_counts` (P2_RU) — one call each, the wrong grain each
time. The shape generalises with **no code beyond the plan entry**: P3's
plan is eleven lines of declaration and reuses the same two dispatch strings
in the other order.

---

## 7. Regression — all 32 gold questions

**Deterministic layers, before vs after, exact equality**
(`module79_all32_controls.py`; `…_before.json` from `git archive
origin/main`, `…_after.json` from this tree). Per question: the
Meta-Analysis plan matched and its full dispatch list, the RAG data-half
plan matched and its sub-query, whether that plan has a second aggregate,
and `router._deterministic_route_override()`. **32 of 32 rows equal on
every field except one cell:** KB9's `kb_then`, `None` → the property
string — the change this module makes, and the only one.

- routes: identical on all 32;
- Meta-Analysis plan match: identical — `{CR3: record_consistency, G1:
  caseload_review, G6: orientation_note}`, the other 29 `None`; the two new
  plans match none; the three existing plans' dispatch lists are
  byte-identical (no `chained` member);
- RAG data-half plan match: identical — KB1/KB3/KB4/KB5/KB6/KB8/KB9 to the
  same seven plans, the other 25 `None`.

**Aggregate layer.** `xagg.py` is not in the diff, so `run_aggregate()` +
`_render_aggregate_text()` over the 32 gold texts is the pre-module code
path by construction; Module 144's `_M144_DISPATCH_BASELINE` snapshot test
still passes on this tree.

**Answers.** G1, G6 and KB9 — the three gold questions that touch a plan
structure this module edited — were run live before and after (§5): same
dispatch, same figures for G1/G6; KB9 measured in-process 3 v 3 (§5.3). A
full 32-question live pass was not run, for Module 144's reason: an
hour-plus of contended model time whose only information beyond the exact
deterministic equality above would be paraphrase wording on questions whose
code path did not change.

---

## 8. New defects — filed, not fixed

Numbering re-checked at filing time against `origin/main` (`01ddc18`,
highest **151**), `muhafiz-m143` (151) and `muhafiz-m145` (149). Filed as
**152–154** in `GOLD_QA_REMAINING_FIXES_PLAN.md`.

| # | defect | evidence |
|---|---|---|
| **157** | **A district breakdown silently ignores a section (or date, age) in the question.** *"How many cases citing section 34 are registered in each district?"* → `top_districts_by`, `extract_aggregate_filters()` returns `section=34`, and the unfiltered nine-district table is served with nothing saying the filter was dropped, because `top_districts_by` is not in `FILTERABLE_AGGREGATE_KINDS`. The brief's premise that Module 144 made this work is false. `_top_districts_by()` already takes `jurisdiction_case_ids`, so the fix is one set-membership entry plus the filter line in the three renderers | §1.2, in-process against the live graph |
| **158** | **A matched decomposition plan cannot reach Meta-Analysis on its own.** `_xagg_answers_in_one_call()` lets a plan match veto the one-call skip, but `classify_to_subagent()` then requires a separate `_META_ANALYSIS_TRIGGER_PATTERNS` hit to select Meta-Analysis; the two lists have been maintained in parallel since Module 29. So `most_cited_section_by_district` matches live and the query still lands on the XAGG agent (§4.1 after-arm, 1/1). The fix is one clause in `supervisor.py` — "a matched plan IS a trigger" — which is Module 145's file this session. **Until it lands, this module's live example is fixed in-process only** | §1.3, §4.1, `module79_probe_dispatch.py` |
| **159** | **The relevance evaluator reads a compound data-half chunk as off-topic on the first attempt.** With KB9's two-aggregate data half, the evaluator's first verdict was `relevant=False` 3/3 (2/3 with the single aggregate), and its stated reason names the property text as not answering a death-investigation question; the retry then passes. A machine-computed chunk that the plan put there on purpose should not be able to cost a retrieve-evaluate cycle (~60–75 s). Same family as Module 143's cost finding, on the other side of the gate | §5.3 |

**Observations that are not defects, recorded so they are not re-found:**

- The synthesis keeps district names in the stored Urdu (`فیصل آباد`) and
  sometimes adds the English in brackets, exactly as G6's per-district
  sub-answer does; the chain's lead line carries `{value_display}` for a
  plan that wants the English form up front (`busiest_district_by_section`
  uses it).
- `resolve_aggregate_kind()` names the dispatch branch (`top_districts_by`)
  and the result dict names the family (`district_breakdown`); the step's
  `expected_kind` is the latter, and the test maps one to the other. Every
  other family spells both the same.
- P2_RU's answer came back in English in-process only because the runner
  passes no `preferred_language`; not a chain property.
