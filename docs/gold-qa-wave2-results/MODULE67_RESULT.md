# Module 67 — the router classifies a cross-case aggregate as plain RAG, and the pipeline then burns ~8 minutes before failing hard

**Found by:** Module 60's own before-arm · **Branch:**
`fix/router-rag-misclassification` · **Date:** 2026-09-09 · **Backend:**
`127.0.0.1:8023` · **Branch point:** `main` @ `ae6ea3c`

**Both halves were fixed, and they are separable.** (1) the
misclassification — Module 60's resolver-gated override is widened from one
aggregate kind to a named allow-list of five, moving four more questions
(M2, M7, CS4, CP1) off the LLM classifier; (2) the cost of being wrong — a
measured wall-clock ceiling on the Semantic Search dispatch, which fired
live and is confirmed in the log.

---

## 1. Root cause

### 1a. The brief's hypothesis, and where it was wrong

The brief inherited two claims from the tracker. One held, one did not.

**Held.** The pathology is real and reproduces on a question other than M4.
`KB3`, run live on the pre-fix tree, produced this:

| KB3, before arm | Route | Sub-agent | Seconds | Outcome |
|---|---|---|---|---|
| run 1 | `RAG` | Semantic Search | **468.1** | `status=error` |
| run 2 | `RAG` | Semantic Search | **347.5** | `status=error` |
| run 3 | `RAG` | Semantic Search | 176.7 | `status=error` |
| run 4 | `RAG` | Semantic Search | 178.4 | answers (2400 chars) |

468.1 s and a hard failure, on an unchanged question, one run in four. That
is Module 60's 456 s / 487 s shape, on a different question, on a different
day.

**Did not hold — the exposure count.** The tracker says *"the 11 gold
questions with no deterministic override — all eight KB questions plus G1, G6,
CR3 — are exposed."* Measured directly with
`_deterministic_route_override()` over all 32 on the branch point, the
override-less set is **sixteen**, not eleven. The five the tracker missed
are **A1, CS4, CP1, M2 and M7** — and those five are the ones that matter
most here, because unlike the KB questions (for which RAG is the *correct*
route) each of them is a cross-case aggregate that RAG structurally cannot
answer. The tracker's eleven were the questions exposed to a slow failure;
the five it omitted are the questions exposed to a *wrong answer*.

### 1b. How often the router actually gets it wrong

`route_query()` was probed in-process, **8 consecutive calls on each of the
16 override-less gold questions — 128 calls**, on the branch-point router:

| Question | Routes over 8 calls | `confidence` |
|---|---|---|
| A1 | XAGG 8 | medium 8 |
| CS4 | XAGG 8 | medium 8 |
| CP1 | XAGG 8 | medium 8 |
| M2 | XAGG 8 | medium 8 |
| M7 | XAGG 8 | medium 8 |
| G1 | XNETWORK 8 | medium 8 |
| G6 | XNETWORK 8 | medium 8 |
| CR3 | XNETWORK 8 | medium 8 |
| KB1 | RAG 8 | high 8 |
| KB2 | RAG 8 | high 8 |
| **KB3** | **RAG 6 / XAGG 2** | **medium 8** |
| KB4 | RAG 8 | high 8 |
| KB5 | RAG 8 | medium 8 |
| KB6 | RAG 8 | high 8 |
| KB8 | RAG 8 | medium 8 |
| KB9 | RAG 8 | medium 7 / high 1 |

**One question of sixteen is unstable on the raw gold text today: KB3, 2 of 8
in the wrong direction** (a legal-KB question sent to the cross-case
aggregate). The other fifteen are 8-of-8 stable. This is an honest
correction to the brief's framing: on this machine, today, the LLM router is
**not** flaky for most of these questions when handed the literal gold text.
The instability Module 60 measured for M4 (6 XAGG / 2 XNETWORK) did not
reproduce for any of the five aggregate questions.

That does **not** make the defect theoretical, for two reasons the live arm
settles: the live pipeline feeds the router a *rewritten* string that differs
run to run (Module 60's own §1 note), and the second half of the defect —
the cost — is independent of how often the classification is wrong.

### 1c. Why the cost is the larger half

`tools/rag.py`'s loop is `while retry_count <= config.MAX_RETRIES` (2 here,
so three attempts), each attempt a retrieval + a cross-encoder rerank + an
evaluator LLM call, with the retry rewriter on top. Nothing in that loop
watches a clock. On the KB corpus in All-Cases scope a question pays that
loop twice (a KB-only pass and a mixed-pool fallback), which is how 456 s,
468 s and 487 s happen. The abstention it eventually returns is *correct* —
what is wrong is that reaching it costs longer than most per-question
budgets, so at a 32-question run it is recorded as a **timeout**, not as a
misroute. That is precisely the artefact class Module 42 had to unpick (a
300 s client ceiling recorded as `route=None`).

---

## 2. Change

Two files, both owned by this module. **Nothing was touched in
`meta_analysis.py`, `rag.py`, `statute_hypothesis.py`, `verifier.py`,
`validation.py`, `src/retrieval/`, `xnetwork.py`, `evaluation/`,
`prompts/evaluator.txt`, or `xagg.py`** — `xagg.py` is read, never written.

### 2a. `src/pipeline/router.py` — the misclassification

Module 60 shipped a resolver-gated override hard-coded to one aggregate kind,
and **measured and rejected** the broad form (`resolves_to_specific_aggregate()`)
because it moves seven of the 32 including KB5. This module re-measured those
same questions and found **the split is at the level of the aggregate KIND,
not the question**. So the rule becomes an explicit allow-list of kinds:

```python
_XAGG_ROUTE_OVERRIDE_KINDS = frozenset({
    "statute_court_stage_join",            # M4  — Module 24/60 (already shipped)
    "station_caseload_by_specialisation",  # M2  — Module 44/56/58
    "incident_to_report_minutes_by_year",  # M7  — Module 22/43
    "criminal_record_local_match_gap",     # CS4 — Module 44
    "weapon_recovery_rate_by_district",    # CP1 — Module 55
})
_XAGG_ROUTE_OVERRIDE_EXCLUDED_KINDS = frozenset({
    "gender_breakdown", "case_completeness_scan",
})
```

**This is not the broad form.** The broad form's blast radius is eight
questions; this takes five of them and refuses three, and the refusals are by
**named kind**, with the reason written next to the constant:

- **`gender_breakdown` — refused.** KB5 (a legal-KB question about violence
  against women) and A1 (a genuine gender count) both resolve here. KB5 is the
  resolver's false positive; A1 is not. They are **indistinguishable at this
  kind**, so neither is routed. A1 therefore keeps the LLM route — recorded
  in §8 as remaining exposure rather than quietly fixed.
- **`case_completeness_scan` — refused.** G1 resolves here. Including the kind
  buys nothing (G2, which legitimately *is* this aggregate, already reaches
  XAGG through `_XAGG_OVERRIDE_PATTERNS`) while pinning a deterministic route
  onto a question with an open defect (Module 71). Measured rather than
  assumed: G1's live route is **already XAGG on 2 of 2 runs** and it still
  dispatches to Meta-Analysis, because `_xagg_answers_in_one_call(G1)` is
  `False` — so capturing it would probably be harmless. *Probably harmless and
  useless* is not a reason to widen a router.

Three properties Module 60 established are preserved unchanged: the gate is
`resolve_aggregate_kind()`, not a new regex (so it inherits every precedence
rule above each named predicate — G3's `court_readiness_scan` and CR7's
`criminal_record_court_crosscheck` resolve earlier and can never reach it);
the import is lazy, so a broken `xagg` leaves routing exactly as it was; and
the block sits **after** the XNETWORK loop and **after** the active-case
short-circuit.

`_resolves_to_statute_court_stage_join()` is kept as a thin wrapper so every
Module 60 test still exercises the property it was written for.

### 2b. `src/pipeline/harness/supervisor.py` — the cost

A wall-clock ceiling on the **Semantic Search dispatch only**:

```python
SEMANTIC_SEARCH_DEADLINE_S: float = float(os.getenv("SEMANTIC_SEARCH_DEADLINE_S", "360"))
```

On expiry the dispatch is cancelled and the Supervisor returns
`ABSTAINED` with `error.kind == "timeout"`, an explicit caveat, and a
`supervisor:dispatch` / `status="error"` **PipelineEvent naming the deadline**
— so a bounded failure is never again indistinguishable from a misroute or a
client timeout.

**Why here and not in the retry loop.** The real budget is
`config.MAX_RETRIES` inside `tools/rag.py`, which is owned by another live
track (Module 39) and was not touched. It is also the wrong lever: lowering
`MAX_RETRIES` takes a retry away from the runs that legitimately need it
(Modules 30/38/52 built the KB path's later attempts on purpose), whereas a
wall-clock ceiling only ever fires on a run already far outside the
distribution. The Supervisor is where an unbounded sub-agent becomes an
unbounded *request*, so that is where the bound belongs.

**Why Semantic Search only.** It is the one sub-agent whose cost is an
open-ended retry loop with no internal deadline. Meta-Analysis already has
`config.META_ANALYSIS_SUBQUERY_TIMEOUT` (Module 53); Large-Scale Aggregate is
one deterministic call. Applying this to every sub-agent would be a blanket
request timeout — the Module 42 mistake, not the fix for it.

**Where the number came from, and its honest limit.** It must sit above the
slowest RAG run that legitimately *succeeds* and below the doomed ones. The
slowest known success is KB6 at 247.1 s (Module 42); the doomed population is
347–487 s. 360 s sits between them with ~45 % headroom. But the two
populations **overlap** in this module's own data — KB3 abstained at 176.7 s
and answered at 178.4 s — so no threshold can separate "doomed" from "slow but
right". 360 s bounds the tail; it does not classify. A tighter 300 s would
have bounded one more of the four measured KB3 runs at the cost of dropping to
1.2× the slowest known success, and that trade was not taken.

---

## 3. Unit tests

```
PYTHONPATH=. python -m pytest tests/test_router.py tests/test_harness_supervisor.py
288 passed in 3.57s
```

Blast-radius sweep, all passing (**488 passed**): `test_xagg.py`,
`test_harness_agent_meta_analysis.py`,
`test_harness_agent_large_scale_aggregate.py`, `test_harness_tool_xagg.py`,
`test_harness_cutover.py`, `test_case_scope.py`,
`test_harness_agent_semantic_search.py`.

| Test | What it pins |
|---|---|
| `test_module67_all_32_gold_questions_route_exactly_as_measured` | **the all-32 EQUALITY negative control**, stated against a captured map, naming every question that moves and asserting M4/G3/CR7/G2/G5/M5 unchanged and KB5/G1/A1/CR3/G6 still un-overridden |
| `test_module67_each_moved_question_moves_via_its_own_named_aggregate_kind` | each moved question reaches XAGG through the specific family built for it, not merely "XAGG" |
| `test_module67_the_two_excluded_kinds_stay_excluded` | `gender_breakdown` and `case_completeness_scan` excluded **by name**, with KB5/A1/G1 asserted uncaptured and G2 asserted still routed by its own pattern list |
| `test_module67_is_not_the_broad_form_module60_rejected` | the allow-list is a **strict subset** of `resolves_to_specific_aggregate()`; replacing it with the broad predicate fails here |
| `test_module67_no_kb_question_is_ever_captured` | none of the eight KB questions may be routed to an aggregate |
| `test_module67_an_active_case_still_short_circuits_the_wider_override` | the widened rule still sits below the active-case short-circuit |
| `test_module67_gold_question_variants_route_exactly_as_before` | the questions' own paraphrase variants, same bar |
| `test_module67_a_broken_xagg_import_leaves_routing_unchanged` | the lazy import's fallback survives the widening |
| `test_module67_semantic_search_is_bounded_and_abstains_honestly` | the ceiling fires, cancels the handler, returns `ABSTAINED` + `error.kind="timeout"`, and emits an `error` event **naming the deadline** |
| `test_module67_a_fast_semantic_search_is_untouched` | a healthy RAG run passes through byte-for-byte (`result is expected`), no extra event |
| `test_module67_the_ceiling_applies_to_semantic_search_only` | **not** a blanket request timeout — XAGG is not cut; fails if a later module widens it |
| `test_module67_the_ceiling_can_be_disabled` | `0` disables it |
| `test_module67_the_default_deadline_sits_between_the_measured_populations` | the number itself, pinned to the two measurements it was derived from |

Two Module 60 tests were **amended, not deleted**, because Module 67
supersedes their arithmetic: the all-32 control now allows Module 60's move
plus Module 67's four (taken from a single shared declaration), and the
broad-form test now asserts the residue `["A1", "G1", "KB5"]` — the
load-bearing half. While doing so it recorded a correction: **Module 60's
prose says the broad form moves "seven" and then names eight**; the count was
the typo, the list was right.

---

## 4. Live verification

**Infrastructure checked before each batch:** model server `/health` →
`{"status":"ok"}` (checked at the start of both arms); backend 8023 `/health`
→ ok, **7716 documents**; port 8021 (Module 39's track) up and left alone;
8020/8022 idle. Quota grep over both backend logs: **0 real hits** (two
matches in `backend_after.log` are the pattern's `429` matching a millisecond
timestamp, not a rate limit).

Both arms are the **same backend, same corpus, same runner**. The only
difference is `src/pipeline/router.py` and
`src/pipeline/harness/supervisor.py`, checked out at `ae6ea3c` for the before
arm and restored for the after arm, with a backend restart between.

### 4a. The four questions whose route changed — 4 runs each, both arms

| Question | Before: route (n-of-N) | Before wall-clock | After: route (n-of-N) | After wall-clock |
|---|---|---|---|---|
| **M2** | `XAGG` → Large-Scale Aggregate **4/4** | 45.7 / 60.4 / 54.0 / 50.4 s (mean **52.6**) | `XAGG` → Large-Scale Aggregate **4/4** | 9.5 / 15.7 / 16.5 / 17.3 s (mean **14.8**) |
| **M7** | `XAGG` → Large-Scale Aggregate **4/4** | 50.8 / 54.8 / 64.2 / 49.9 s (mean **54.9**) | `XAGG` → Large-Scale Aggregate **4/4** | 15.8 / 9.2 / 9.9 / 18.5 s (mean **13.3**) |
| **CS4** | `XAGG` → Large-Scale Aggregate **4/4** | 52.9 / 59.7 / 51.2 / 46.3 s (mean **52.5**) | `XAGG` → Large-Scale Aggregate **4/4** | 11.0 / 8.4 / 13.4 / 16.3 s (mean **12.3**) |
| **CP1** | `XAGG` → Large-Scale Aggregate **4/4** | 47.8 / 51.8 / 55.2 / 53.5 s (mean **52.1**) | `XAGG` → Large-Scale Aggregate **4/4** | 13.6 / 27.4 / 6.8 / 18.5 s (mean **16.6**) |

**The honest headline: the route did not change live, 16 runs of 16 in each
arm.** The LLM classifier already said XAGG for all four on this machine
today. What changed is *how* the route is reached and what it costs:

- **The classification is now deterministic** rather than a 16-of-16 sample
  from a distribution whose tail Module 60 measured directly (2 of 6 runs to a
  route that fails after eight minutes). This is insurance against a measured
  pathology, not a repair of one observed today, and it should be read that
  way.
- **Wall-clock falls by ~3.7×**, mean **53.0 s → 14.3 s** across the four —
  because the deterministic override short-circuits the router LLM call
  entirely (the probe in §1b measures that call at 15–30 s on its own).

### 4b. KB3 — the pathology itself, 4 runs each arm

| Arm | Run | Route | Sub-agent | Seconds | Outcome |
|---|---|---|---|---|---|
| before | 1 | `RAG` | Semantic Search | **468.1** | `status=error` |
| before | 2 | `RAG` | Semantic Search | **347.5** | `status=error` |
| before | 3 | `RAG` | Semantic Search | 176.7 | `status=error` |
| before | 4 | `RAG` | Semantic Search | 178.4 | answers |
| after | 1 | `RAG` | Semantic Search | 238.9 | answers |
| after | 2 | `RAG` | Semantic Search | 384.7 | answers |
| after | 3 | `RAG` | Semantic Search | **406.7** | `status=error` — **deadline fired** |
| after | 4 | `RAG` | Semantic Search | 242.7 | answers |

**The ceiling fired live, and the log says so:**

```
2026-09-09 14:45:43,730 [WARNING] src.pipeline.harness.supervisor:
Supervisor: Semantic Search exceeded its 360s deadline (360.0s spent) —
abstaining rather than letting the request hang.
```

Two things this table shows that are easy to overstate, so they are stated
plainly:

1. **The bound works and is now proven end-to-end**, not only in a unit test.
   The worst before-arm run (468.1 s) would be cut; Module 60's 456 s and
   487 s runs would be cut.
2. **The win is ~20 %, not ~95 %.** `SEMANTIC_SEARCH_DEADLINE_S` bounds the
   *sub-agent*, and a request also pays the rewriter, `main.py`'s own
   classification call and the Supervisor's, so a 360 s ceiling shows up as a
   ~406 s request. "Fails in 20 s instead of 480 s" is **not** what was
   achieved here and is not achievable at this layer: it would require
   lowering the retry budget inside `tools/rag.py`, which is another track's
   file and would cost the KB path attempts it demonstrably needs.
3. KB3's answered/errored split moved 1-of-4 → 3-of-4 between arms. That is
   **KB3's own non-determinism**, not an effect of this change — nothing here
   touches retrieval — and it is reported only so the table is not read as a
   quality claim.

---

## 5. Gold comparison

Judged on facts and ideas, in the answer's own words; numbers by magnitude.

| Question | Gold's claim | After-arm result |
|---|---|---|
| **M7** | 2024 mean 15.0 min; 2026 mean 1401.3 min (~23.4 h); much slower now | **4 / 4** — *"15.0 minutes across 13 FIRs in 2024, compared to 1401.3 minutes (~23.4 hours) across 51 FIRs in 2026"*. Exact on both figures |
| **CS4** | Exactly one — وقاص, CNIC 00000-9000020-1 — in criminal records with no matching local FIR; expected, because that system is external | **4 / 4** — names **وقاص (00000-9000020-1)**, `criminal_record:CR-C106-1`, and gives gold's own explanation (external/federal source, 31 of 32 do match) |
| **CP1** | Hyderabad highest 3/5 = 60 %, then Faisalabad 10/19 ≈ 53 %, Lahore 8/18 ≈ 44 %, Rawalpindi 3/10 = 30 %, Multan 0/1 | **4 / 4** on gold's headline (**حیدر آباد 3 of 5, 60 %**). 2 of 4 also carry Faisalabad 53 %, Lahore 44 % and Multan 0 %; the other 2 give the leader only. Rawalpindi appears in neither arm |
| **M2** | 9 of 73 FIRs (~12 %) from just 2 of 19 stations | **3 / 4** carry `9 of 73` **and** `2 of 19` verbatim. The 4th gives a growth framing (7→39 vs 3→5) and drops the concentration figure |
| **M4** (regression) | No — 33 criminal records, only 1 convicted, ~30 still under trial; read severity off sections, not convictions | **7 / 7** — every element present on every run |

**M2's one miss is Module 70, not this module.** Module 70 is already filed
for exactly this: *"M2's headline concentration figure is dropped ABOVE XAGG
on ~1 run in 3."* The before arm here happened to carry it 4 of 4 and the
after arm 3 of 4; with n=4 per arm that is one draw from a known ~2-in-3
distribution, not a regression, and the aggregate itself emitted the figure on
every run in both arms.

**CP1's partial listings** appear in both arms and are the same
paraphrase-layer variance; the gold headline (the highest-rate district and
its rate) is present on 4 of 4.

---

## 6. Non-gold paraphrase

Phrasings written for this section and **not** adjusted afterwards. Every row
goes through `resolve_aggregate_kind()`, so the router and XAGG agree by
construction.

| Paraphrase | Override | Resolved kind |
|---|---|---|
| "Which is picking up more work — the ordinary thanas or the ones that only handle one kind of crime?" | **XAGG** | `station_caseload_by_specialisation` |
| "Are victims taking longer to walk into the station now than they used to?" | *none* | `station_or_category_counts` |
| "Is there anybody on the criminal-history system we have never actually charged ourselves?" | *none* | `station_or_category_counts` |
| "Per case registered, which district pulls in the most weapons?" | *none* | `top_districts_by` |
| "What does the law say about recovering a weapon during investigation?" | *none* | `graph_recurrence_weapon` |
| "Are our cases actually ready to go to court?" | *none* | `station_or_category_counts` |
| "Kya log ab pehle se zyada der baad FIR darj karwa rahe hain?" | *none* | `station_or_category_counts` |

**One of four target paraphrases reaches the override; the two deliberately
adjacent non-targets are correctly refused.** That is the honest bound and it
is the same shape Module 60 reported: this override is **exactly as wide as
the resolver's own predicates and no wider**. Widening those predicates is
Module 62's problem; doing it here would change the blast radius the negative
control measures, which is the one thing this module must not do quietly. The
one row that does pass proves the capability is not tied to a gold string —
not one content word of M2's gold survives in it.

---

## 7. Regression guard

### Static — the all-32 equality control

`_deterministic_route_override()` over all 32, branch point vs. this branch:

**Exactly four questions changed, all from *no deterministic override* to
`XAGG`: `M2`, `M7`, `CS4`, `CP1`.** The other 28 are byte-identical,
including all six that already had an XAGG override, all eight KB questions,
and G1/A1/CR3/G6, which stay un-overridden. The 32 questions' own
`question_variants` were swept: none newly reaches the override.

### Live — this branch, same backend

| Question | Runs | Route → sub-agent | Wall-clock | Gold |
|---|---|---|---|---|
| **M4** (Module 60's 7-of-7 must hold) | **7 / 7** | `XAGG` → Large-Scale Aggregate | 8.5–21.6 s | **7/7** — 33 records, 1 verdict, 32 in progress, "No — the two do not agree", read severity off sections |
| **G3** (mandatory — shares M4's keyword space; PR #8's collision) | 2 / 2 | `XAGG` → Large-Scale Aggregate | 12.8 / 14.9 s | present |
| **CR7** | 2 / 2 | `XAGG` → Large-Scale Aggregate | 11.1 / 16.8 s | present |
| **G2** | 2 / 2 | `XAGG` → Large-Scale Aggregate | 8.3 / 22.2 s | present |
| **G5** | 2 / 2 | `XAGG` → Large-Scale Aggregate | 12.9 / 14.3 s | present |
| **M5** | 2 / 2 | `XAGG` → Large-Scale Aggregate | 22.3 / 22.4 s | present |
| **KB5** (the broad form's false positive — must stay RAG) | **2 / 2 `RAG`** → Semantic Search | 198.4 / 210.0 s | answers, not captured by the override |
| **A1** (shares `gender_breakdown` with KB5) | 2 / 2 | `XAGG` → Large-Scale Aggregate (from the **LLM**, not the override) | 49.1 / 55.6 s | unchanged |
| **G1** (the second excluded kind) | 2 / 2 | `XAGG` → **Meta-Analysis** → Large-Scale Aggregate | 133.5 / 181.6 s | still decomposes; unchanged |

**M4's 7-of-7 holds.** **KB5 stays on RAG.** **G1 still decomposes.** Those
three rows are the whole risk this module was told to bound, and all three
are measured, not argued.

**Not re-run live:** the 23 gold questions outside this set. They are covered
by the static all-32 equality control, which for a routing change is the
stronger instrument — it is exhaustive, where a live sweep of 32 × N is not
affordable and is noisier.

---

## 8. New defects found — filed, not folded in

### Module 72 — `A1` and `KB5` are indistinguishable at `gender_breakdown`

`resolve_aggregate_kind()` sends both A1 (*"how many of the accused are women"*
— a genuine cross-case count) and KB5 (a legal-KB question about violence
against women) to `gender_breakdown`. One is a true dispatch, the other is a
vocabulary false positive, and **nothing at the kind level tells them apart**.
That is the sole reason A1 could not be given a deterministic route by this
module: capturing the kind would drag KB5 into a cross-case aggregate, which
is the exact mistake Module 60 refused. A1 is therefore still exposed to the
LLM classifier (8-of-8 XAGG on the probe, 2-of-2 live — stable today, unpinned
tomorrow). **Verify:** whether `_is_gender_breakdown()` can be made to require
a caseload-counting signal that KB5's statutory phrasing does not carry,
negative-controlled against all 32 and against KB5's variants.

### Module 73 — the request cost of a doomed RAG run is larger than the sub-agent's

`SEMANTIC_SEARCH_DEADLINE_S` bounds the Semantic Search dispatch, and it fires
(§4b). But the measured *request* still took 406.7 s against a 360 s ceiling:
the rewriter, `main.py`'s own cutover classification call and the Supervisor's
second `route_query()` all sit outside the bound, and the router call alone is
15–30 s (§1b). Two of those three are the **same classification computed
twice**. **Verify:** whether `main.py`'s cutover classification can be handed
to `Supervisor.handle()` instead of recomputed, and what the end-to-end
saving is. Owner is `main.py`/`cutover.py`, neither of which this module may
touch.

### Recorded, not filed — the tracker's exposure count

Module 67's tracker row says eleven gold questions have no deterministic
override. Measured, it is **sixteen**: the row omits **A1, CS4, CP1, M2 and
M7**. Four of the five are closed by this module; A1 is Module 72 above. The
tracker row is corrected in this PR.

### Module 68 — measured here, deliberately NOT fixed

The brief allowed taking Module 68 if a principled fix fell out of this work.
It did not, and none was invented. What this module *can* contribute is the
measurement the tracker asks for, from §1b's 128 calls:

- `confidence` is **not** a constant: `high` on KB1/KB2/KB4/KB6 (each 8-of-8
  stable), `medium` on ten questions, and KB9 flipped `medium 7 / high 1` on
  an identical string, which is itself a small finding — the field is not even
  stable for a stable classification.
- `confidence` **does not flag the one question that is actually unstable**.
  KB3 (6 RAG / 2 XAGG) reports `medium` on all eight calls — including both
  wrong ones — which is exactly what A1, CS4, CP1, M2 and M7 report while
  being 8-of-8 stable. Combined with Module 60's eight M4 calls (6/2 split,
  `medium` on all eight), a reader cannot use `medium` to tell a coin flip
  from a certainty.
- It is surfaced, not inert: `orchestrator.py` puts it on the SSE
  `router done` event as `confidence=`. Nothing *branches* on it, which is
  why this is a defect and not an incident — but it is displayed.

The tracker's two options — "make it correlate with stability, or stop
emitting it" — are both larger than they look. Correlating it means measuring
run-to-run stability, which the router cannot observe from a single call.
Removing it means changing an SSE field's contract. Neither belongs in a PR
that is already changing routing, so **Module 68 is left open** with this
measurement attached to its tracker row.
