# Module 145 — semantic dispatch under the phrase lists

**Branch** `feat/semantic-dispatch` · **worktree** `D:/Rapids AI/muhafiz-m145` · branched from `origin/main` @ `50862d6` (Module 144 included).

**Headline, stated before the detail, because two parts of it contradict the
brief:**

1. **The three live queries did not die where the brief said.** Measured
   before any code was written (3 runs each, local classifier, temperature
   0): the officer question routes **RAG**, not XAGG — `resolve_aggregate_kind()`
   is never consulted for it — and the two entity-recurrence questions route
   **XGRAPH** offline (XNETWORK live, per the filing). A semantic layer that
   lived only in front of `resolve_aggregate_kind()` would have closed
   nothing. It had to feed the **route**, through the mechanism Module 60/67
   already built for exactly that (`router._resolved_xagg_override_kind()`),
   which is where it now sits: after every deterministic override, before
   the LLM classifier.
2. **The e5 `/embed` design the brief specified has no usable threshold, and
   that is a measured result, not a guess.** Against full-sentence
   descriptions, e5 cosine puts the lowest true positive at **0.789** and the
   highest hazard at **0.870** — a *legal-procedure question about FIR
   registration* landing on the FIR-register-completeness aggregate: same
   topic, opposite question type. Bi-encoder similarity measures topic;
   dispatch needs intent. The `/rerank` cross-encoder (bge-reranker-v2-m3,
   already serving retrieval) scores (question, description) jointly and
   separates them: highest hazard **0.351**, lowest fired true positive
   **0.462**, **margin +0.111** at the shipped threshold **0.40**, over 102
   corpus items that reach the layer. That is what shipped.
3. **What it closes, measured:** the filing example (L1) reaches
   `officer_role_pair_overlap` **3 of 3** live and answers with the real
   numbers in 9–15 s instead of abstaining after ~230 s; Module 100's
   rewordings 2 of 3; and **10 more of the 63 XAGG paraphrases** reach
   their gold kind (16 → 26). **What it does not close:** the two
   entity-recurrence live queries (scores 0.002 and 0.218 — below any safe
   threshold), every **Roman-Urdu** paraphrase (the cross-encoder scores
   Roman-Urdu at ≤ 0.10 against everything, so roman-ur reach is 4 → 4),
   and the Meta-Analysis trigger list in `supervisor.py` (Modules 111/123),
   which this module did not touch. **Modules 92, 111 and 123 stay open;
   100 is partially closed; 106 is closed by construction** — the table in
   §6 and the tracker say which is which.
4. **The fast path is untouched and the 32 gold questions are byte-identical**
   — three ways: the phrase chain is unchanged in body and order; the
   all-32 equality control with the layer *force-armed* on every gold
   question moves 0 of 32; and the shipped chokepoint, run live with the
   real cross-encoder, fires on 0 of 32 and leaves all nine load-bearing
   `route_query()` fields equal (§5, §7).

---

## 1. Root cause — three live examples, traced through the gates

All three were asked live on 2026-09-14 and filed as row 145. Traced on
`origin/main` @ `50862d6` before any change.

**L1 — *"How often is the officer who registers an FIR also the officer who
investigates the case?"***

| gate | result | why |
|---|---|---|
| `router._deterministic_route_override()` | miss | no SQL/legal/XNETWORK/XAGG/XGRAPH/legal-KB pattern matches |
| `router._resolved_xagg_override_kind()` → `resolve_aggregate_kind()` | `station_or_category_counts` (generic) | `_is_officer_role_pair_comparison()` needs three lists: *registering* ("who registers" ✓), *investigating* ("who investigates" ✓), *sameness* ✗ — "also" is not on `_OFFICER_ROLE_SAMENESS_TERMS` |
| LLM classifier (local Qwen3-14B) | **RAG**, `within_case`, 3 of 3 | — |
| `classify_to_subagent()` | Semantic Search | route RAG |
| RAG tool | abstains after three retrieve-evaluate-rewrite cycles | the answer was never in a document |

The brief's trace ("falls through to a generic count, then to Semantic
Search") is half right: the generic count is what `resolve_aggregate_kind()`
returns, but nothing dispatches on it — the router's XAGG override only
routes on the five Module 67 kinds, so a generic kind is a routing miss, and
the LLM then sends the question to RAG. `muhafiz-m143/backend.log` for the
live run shows exactly that: the RAG tool, three evaluator rejections, three
query rewrites, no XAGG line at all.

**L2 — *"Is there anyone who used one of our citizen services who is also
under investigation?"*** and **L3 — *"Is anyone convicted in one matter but
still being sought as a fugitive in another?"***

Same first two gates (miss; generic — "anyone" is not in `_PERSON_KEYWORDS`),
then the LLM routes both **XGRAPH** 3 of 3 offline (the filing recorded
XNETWORK live), `classify_to_subagent()` → Cross-Case Linkage, which needs a
named entity or a cluster and correctly refuses. The aggregate that answers
CR2's shape — `graph_recurrence_person` — is never in the path.

**The mechanism, once.** Every gate is a literal phrase list, so a capability
is reachable only from a neighbourhood of its gold wording. Module 92 filed
it at the router regex, 100 at `_is_officer_role_pair_comparison()`, 106 at
`resolve_aggregate_kind()`'s vocabulary, 111 and 123 at the Meta-Analysis
trigger list. One fix can address the first three because they share a
chokepoint — the router consults `resolve_aggregate_kind()` — and this
module builds that. The fourth layer (`supervisor.py`) has a different
chokepoint and a different capability set (Meta-Analysis plans, owned by
`muhafiz-m79`); it is not built here and 111/123 stay open (§8).

---

## 2. The change, and what did not change

**New: `src/pipeline/semantic_dispatch.py`.**

- `CAPABILITY_DESCRIPTIONS` — one plain sentence per aggregate kind, all 38
  the chain can return, written from each aggregate's docstring and gold
  question, never from its trigger words (a test rejects any entry under six
  words). Plus two **absorbing** entries for neighbouring routes' capabilities
  — XGRAPH's named-entity network and the legal KB's norm lookup — so a
  question of that shape has a *correct* nearest neighbour instead of the
  least-wrong aggregate. The three generic catch-alls and the two honest
  refusals are described and absorbing too: a question nearest to *"how many
  cases or FIRs there are in total"* is not pulled anywhere.
- `prepare(text)` (async) scores the question against every description in
  **one `/rerank` request** (~1.0–1.6 s), caches the decision by exact text,
  and logs it — `SEMANTIC-DISPATCH <kind>: score=… runner_up=…(…)
  threshold=0.40 …s | <question>` when it fires, `SEMANTIC-DISPATCH no-fire:
  best=…` when it does not. `lookup(text)` (sync, pure) returns the cached
  decision if it fires. Any scorer error disables the layer for 60 s and the
  question falls through; it never blocks dispatch for more than 8 s.
- `SEMANTIC_DISPATCH_THRESHOLD = 0.40`, measured (§6).

**`src/pipeline/xagg.py`.** The body of `resolve_aggregate_kind()` — every
branch, in order, unchanged — is now `phrase_aggregate_kind()`.
`resolve_aggregate_kind()` calls it and, **only when the result is one of
`_GENERIC_AGGREGATE_KINDS`**, consults `semantic_dispatch.lookup()`. Still
pure, still total, still synchronous. `prepare_semantic_dispatch()` is the
async half: a no-op unless the phrase chain lands generic. `run_aggregate()`
deliberately does **not** call it (§6, "the robustness check").

**`src/pipeline/router.py`.** `route_query()` gains one step between the
deterministic overrides and the LLM: `_semantic_xagg_override()`, which
applies the same active-case guard the cross-case overrides use, calls
`prepare_semantic_dispatch()`, and routes `XAGG`/`cross_case` when it fires,
with the kind and both scores in `reason`. It is allowed to route on kinds
Module 67 excluded because it cannot reproduce Module 67's failure: it fires
only when *no* phrase list matched, and it never sees a gold question (§5).

**`src/config.py`** — `SEMANTIC_DISPATCH_ENABLED` (default on).
**`tests/conftest.py`** — sets it off before `src.config` is imported, so the
unit suite stays offline. **`tests/test_xagg.py`** — one pin updated: the
"single user of `_is_station_specialisation()`" AST check now names
`phrase_aggregate_kind` (the chain moved, its one caller did not multiply).

**Evaluation:** `evaluation/module145_semantic_probe.py` (the offline
measurement, both scorers), `module145_chokepoint_control.py` (the shipped
path, live), `module145_route_dict_equality.py`, `module145_inprocess_run.py`;
corpora `docs/gold-qa-wave2-results/module145_{hazards,targets}.json` (written
before any score was seen), outputs `module145_probe*.json`,
`module145_rerank_cache.json` (every cross-encoder score, keyed by the
description-table hash), `module145_chokepoint_control.json`,
`module145_live/`. The e5 vector cache (6.8 MB of 1024-dim floats) is not
committed; `module145_probe_e5.json` carries every per-item e5 score and
`--scorer e5` re-embeds on demand.

### What did NOT change

- **No phrase list.** Not `_OFFICER_ROLE_SAMENESS_TERMS`, not
  `_PERSON_KEYWORDS`, not `_XAGG_OVERRIDE_PATTERNS`, not
  `_META_ANALYSIS_TRIGGER_PATTERNS`. "also" is still not a sameness word.
- **Not the LLM router.** The layer sits before it and only for questions
  every override declined; below threshold, the LLM is called exactly as
  before. Its prompt is untouched.
- **Not `supervisor.py`, `retry_gate.py`, `semantic_search.py`, `rag.py`,
  `meta_analysis.py`** — the files the two concurrent tracks own.
- **No description was written to fit any of the six target questions.**
  The table was revised twice after the first measurement, both times on
  hazards, both times recorded (§6, "three rounds").

---

## 3. Unit tests

`tests/test_semantic_dispatch.py` — **33 tests**, every decision planted with
`seed_for_tests()` at its *measured* score, no network. **Both directions:**
on `origin/main` with the module file present but unwired (so the file
imports), **10 fail**:

```
test_every_kind_the_chain_can_return_has_a_description
test_absorbing_classes_mirror_xagg_generic_and_refusal_sets
test_the_live_query_resolves_to_its_aggregate_once_prepared
test_module_100_rewordings_reach_kb3s_aggregate[×2]
test_router_routes_a_semantic_hit_to_xagg_before_the_llm
test_router_semantic_override_respects_the_active_case_guard
test_prepare_scores_logs_and_caches
test_prepare_never_runs_the_scorer_when_the_phrase_chain_resolves
test_all_32_gold_questions_never_consult_the_semantic_layer_or_do_not_move
```

(without the module file, the whole file fails at import). The other 23 pass
in both states by design — the threshold pin, the fire rule, the absorbing
classes, and the **hazard pins**: the three highest-scoring hazards in the
corpus at their measured scores (*"Put your crime-analyst hat on…"* 0.351,
*"Mujhe sab se naye case ke baare mein batao"* 0.104, *"Summarize the FIR for
this case."* 0.061) must not dispatch, and three questions whose nearest
description is an absorbing class (*"How many cases in total?"* 0.994 →
`total_count`; S2's shape 0.996 → `station_or_category_counts`; *"What is the
procedure for registering an FIR?"* 0.626 → `legal_norm_lookup`) must stay on
the phrase result. `test_the_probe_records_zero_gold_moves_and_zero_hazard_fires`
re-asserts the committed measurement and fails if the description table
drifts from the one that was measured.

Suites run on this branch: `test_xagg`, `test_router`,
`test_harness_supervisor`, `test_harness_agent_meta_analysis`,
`test_harness_tool_xagg`, `test_harness_agent_large_scale_aggregate`,
`test_orchestrator`, `test_pipeline`, `test_config` — **1,114 passed, 0
failed** — plus the new file, **33 passed**. Re-run after rebasing onto
`origin/main` @ `01ddc18` (Module 143 merged underneath) with
`test_harness_agent_semantic_search` and `test_retry_gate` added: 1,165
passed, **2 failed — and the same 2 fail identically on pristine
`origin/main` in the same combination** (`test_semantic_search_is_registered_under_its_own_name`,
`test_supervisor_dispatches_to_real_semantic_search_and_real_rag_tool`; both
pass 13/13 when their file runs alone). A pre-existing registry-state
ordering interaction between suites, not this branch; filed as **167**. (No
bare `pytest tests/`.)

---

## 4. Live verification

**No backend was started.** Both machine-wide slots were taken
(`127.0.0.1:8079`, `:8143`, other tracks). Runs are in-process through
Module 92's runner — `route_query()` then `run_cutover_query()` through the
Supervisor, the same two stages `main.py::chat_endpoint()` uses — against the
live graph, vector store and model server. **"Before" is pristine
`origin/main` @ `c52bfd7` in a separate worktree; "after" is this branch.**
The model that answered is recorded per run from `src.llm.client`'s own
`Falling back to …` line: **every run below answered on the local model; no
run fell back to Groq.** Raw: `docs/gold-qa-wave2-results/module145_live/module145_inprocess_{before,after}.json`.

| query | before (route → outcome, s) | after (route → outcome, s) | semantic-dispatch line (after) |
|---|---|---|---|
| L1 officer registers/investigates | RAG → *"No sufficiently relevant documents were found…"* ×3 (224.5 / 240.6 / 159.7 s) | **XAGG → `officer_role_pair_overlap`** ×3: *"In 68 of 74 recorded assignment pairs (92%), the officer who recorded the FIR and the officer who investigated the case are the same person…"* (13.5 / 15.3 / 9.0 s) | `SEMANTIC-DISPATCH officer_role_pair_overlap: score=1.000 runner_up=unsupported_officer(0.314) threshold=0.40 1.84s` ×3 |
| L2 citizen services / under investigation | XGRAPH → refused ×3 (38.0 / 31.8 / 32.1 s) | XGRAPH → refused ×3 (51.8 / 54.4 / 48.8 s) — **unchanged** | `SEMANTIC-DISPATCH no-fire: best=graph_recurrence_person(0.002) runner_up=xgraph_named_entity_network(0.000)` ×3 |
| L3 convicted / fugitive | XGRAPH → refused ×3 (20.8 / 21.5 / 43.3 s) | XGRAPH → refused ×3 (36.4 / 35.6 / 55.2 s) — **unchanged** | `SEMANTIC-DISPATCH no-fire: best=graph_recurrence_person(0.218) runner_up=graph_recurrence_weapon(0.011)` ×3 |

L1's after-answer is KB3's gold data half verbatim in substance (68 of 74,
92%, the six split cases named), produced by the aggregate's own `XAGG
officer_role_pair_overlap:` line. The 108 s the filing recorded was the m143
backend; in-process on today's shared model server the same abstention took
~230 s.

L2 and L3 are **not closed** and the log line says why for each: the
cross-encoder does not read L2 as person-recurrence at all (0.002 — "used one
of our citizen services" is the PKM/CMS applicant side, which no description
mentions because no aggregate joins it), and reads L3 as person-recurrence
only weakly (0.218), below the 0.351 the highest hazard reaches. Lowering the
threshold to catch L3 would fire on a G1 rewording (§6). Filed, §8.

---

## 5. Gold comparison — the all-32 dispatch equality control

`resolve_aggregate_kind()` must be byte-identical on all 32. Checked three
ways, each stronger than the last:

1. **Structural.** 25 of 32 resolve by phrase to a non-generic kind and never
   consult the layer: a *fake* full-score match planted for each changes
   nothing (`test_all_32_gold_questions_never_consult_the_semantic_layer_or_do_not_move`).
2. **Force-armed.** The 7 that land on the generic tier — D1, S2, CR3, G6,
   KB1, KB4, KB8 — with their *measured* cross-encoder decisions planted:
   **moved 0 of 32** (`module145_semantic_probe.py`). KB1's nearest
   description is `dv_report_fir_match` at 0.068; the rest are ≤ 0.007
   except D1/S2, which land on their own absorbing catch-all at 0.985/0.999.
3. **Shipped path, live.** `module145_chokepoint_control.py` runs the real
   deterministic overrides, then the real `_semantic_xagg_override()` with
   the real cross-encoder, then `resolve_aggregate_kind()`, for all 32:
   **resolved kind byte-identical on 32/32; semantic override fired on
   0/32.** Five of the seven generic-tier gold questions never even reach the
   scorer — D1/S2 are claimed by the XAGG phrase override and KB1/KB4/KB8 by
   Module 78's legal-KB override, both of which sit above this layer.

Under the e5 design, for the record, the same control moves **1 of 32**
(KB1 → `fir_register_completeness` at 0.870). That alone would have failed
the module; it is one of the reasons e5 was rejected.

---

## 6. The 96-paraphrase corpus, the hazards, and the threshold

### 6.1 What was measured

Every item scored once against every description, cached to disk
(`module145_rerank_cache.json`, keyed by a hash of the description table so an
edited table can never reuse a stale score). 256 items:

| set | n | role |
|---|---|---|
| gold 32 | 32 | must not move |
| Module 92 paraphrases | 96 | 63 XAGG-gold → target = gold's phrase kind; 33 non-XAGG → must not fire |
| Module 116 paraphrases | 6 | XNETWORK → must not fire |
| Module 144 count paraphrases | 13 | filtered counts → must not fire |
| `prompts/router.txt` few-shot exemplars | 74 | documented route; non-XAGG → must not fire |
| `module145_hazards.json` | 24 | pre-written; must not fire |
| `module145_targets.json` | 6 | the three live queries + Module 100's three rewordings |

An item **reaches the layer** when its phrase kind is generic *and* no
deterministic router override claims it — the shipped condition. 102 do.
Among them a **true positive** is an item whose best description is its
wanted kind; a **hazard** is an item whose best description is a dispatchable
kind it should not go to.

### 6.2 e5 cosine — the brief's design, rejected

| | lowest TP | highest hazard | strict margin | at best threshold |
|---|---|---|---|---|
| e5, descriptions as documents | 0.789 | **0.870** (*"What is the procedure for registering an FIR?"* → `fir_register_completeness`) | **−0.081** | 0.86: 7/19 TP fire, 2 hazards fire, KB1 gold moves |
| e5, symmetric (both as queries) | 0.795 | 0.906 | −0.110 | — |

The whole distribution sits in 0.77–0.92; the runner-up gap (0.001–0.06) is
more discriminative than the score but a dual threshold tuned on 19
positives has ~0.008 slack on each axis. Roman-Urdu collapses onto one
"hub" description (`weapon_evidence_chain`) regardless of content. Module
92's finding — cross-lingual but not discriminative — reproduces with full
sentences: better than route labels, still not usable. The e5 measurement is
kept in `module145_probe_e5.json` and `module145_semantic_probe.py --scorer e5`.

### 6.3 The cross-encoder — shipped

Scores are a relevance probability, bimodal:

```
true positives (21): 1.000 .999 .992 .991 .979 .953 .933 .902 .683 .568 .483 .463 .462 | .218 .158 .049 .043 .031 .002 .001 .001
hazards (50):        .351 .104 .061 .050 .041 .041 .026 .023 .017 .011 .011 .008 … (38 more ≤ .007)
```

| threshold | TP fired / 21 | hazards fired / 50 |
|---|---|---|
| 0.20 | 14 | 1 |
| 0.35 | 13 | 1 |
| **0.40** | **13** | **0** |
| 0.45 | 13 | 0 |
| 0.50 | 10 | 0 |
| 0.70 | 8 | 0 |

**Lowest fired true positive 0.462, highest hazard 0.351: margin +0.111.**
0.40 sits in that gap with 0.049 of slack on the hazard side and 0.062 on the
true-positive side. **The strict margin — lowest true positive of all versus
highest hazard — is negative (0.001 − 0.351)**: eight wanted items score
below any safe threshold and simply fall through as they do today. That is
the honest shape of the result: the layer recognises 13 of 21 rewordings it
could recognise, and refuses to guess on the rest.

The highest hazard, 0.351, is Module 116's English G1 rewording (*"Put your
crime-analyst hat on and tell me what in the pile of cases … looks off"* →
`statute_mix_by_year`) — a Meta-Analysis question that would be sent to a
one-call aggregate. It is the reason the threshold cannot go below ~0.36 and
the reason L3 (0.218) is not caught.

**Three rounds of the description table, recorded.** Round 1 (the table as
first written) already gave margin +0.111 on the 69 items that reached the
layer. Two hazards then surfaced on items that *don't* reach it today and
were fixed by rewriting descriptions, not by changing any threshold: (a) the
robustness check below found *"How many cases are there in total?"* scoring
0.941 against the accused-count description — the count descriptions now say
"cases or FIRs" versus "people, not cases", and it scores 0.997 against
`total_count`; (b) two router.txt phone-number XGRAPH exemplars scored
0.63–0.65 against *"a vehicle or number plate…"* — the vehicle description
now names cars, motorcycles and registration plates, the XGRAPH absorbing
entry names phone numbers and CNICs, and both score ≤ 0.48 on the absorbing
entry. Round 3 is what is committed and measured above; every number in this
file is from it.

**The robustness check.** 54 generic-tier items are claimed by a router
override before this layer runs (D1/S2, the Module 144 counts, router.txt's
XAGG/XGRAPH/SQL examples). Scored anyway, the layer would fire on **1** of
them: *"Map ORG-002's network across all the cases it appears in"* → 0.743
against `graph_recurrence_weapon`. It is unreachable today (the XGRAPH
override claims it) and is filed as residual exposure (§8). This check is
also why `run_aggregate()` does not call `prepare()`: an XAGG-overridden
grand total would have been re-scored there.

### 6.4 Reach over the 96 paraphrases

**At this module's layer** — does the paraphrase resolve to its gold
question's aggregate kind? (63 XAGG-gold paraphrases; S2's kind is the
generic group-by, so its paraphrases count as reached when the layer leaves
them alone.)

| language | before | after | gained |
|---|---|---|---|
| English | 5/21 | **9/21** | S3, CP6, M1, G3 |
| Roman-Urdu | 4/21 | **4/21** | — |
| Urdu | 7/21 | **13/21** | S3, CP6, CR2, CR7, M1, G3 |
| **total** | **16/63** | **26/63** | +10, all to the correct kind, 0 to a wrong one |

**At the route** — route XAGG *and* the right kind ("reaches the right
place"), with Module 92's cached local-LLM route standing in when no
override or semantic hit decides: **15/63 → 25/63**. Four of the ten are
route changes the LLM used to get wrong (S3-en XGRAPH, CR2-ur XGRAPH — Module
88's symptom, M1-ur RAG, G3-en GRAPH_HYBRID); six were already XAGG by
override or LLM and only the kind was wrong.

**Route-level over all 96**, the quantity Module 92's 55/96 measured:
computed the same way on today's `main` the baseline is **61/96** (the
deterministic layer has grown since Module 92 — 78, 116, 144), and this
module makes it **65/96** (en 22→24, roman-ur 17→17, ur 22→24). The 55 → 61
is not this module's; the 61 → 65 is. **Non-XAGG paraphrases pulled to XAGG:
0 of 33. XAGG paraphrases sent to a wrong kind: 0.**

**Roman-Urdu is the language this layer does nothing for.** The highest
score any Roman-Urdu item in the corpus reaches against any description is
**0.104**; the cross-encoder treats Latin-script Urdu as noise. Urdu script
scores 0.90–0.99 on the same questions. Filed (§8).

### 6.5 What this supports on the tracker

| module | verdict | evidence |
|---|---|---|
| **106** (`resolve_aggregate_kind()` vocabulary; no tracker row) | closed by construction | the resolver now has a non-vocabulary path; 16→26 of 63 |
| **100** (KB3's family rejects rewordings) | **partially closed** | M100a 0.998 and M100c 0.992 reach `officer_role_pair_overlap`; M100b *"Is the registering officer usually the investigating officer?"* still resolves by phrase to the `unsupported_officer` refusal — a non-generic kind, so the layer never sees it |
| **92** (paraphrases lose their route) | **stays open** | +4 of 96 at the route; Roman-Urdu +0; the LLM classifier's ~30% miss rate is untouched |
| **111 / 123** (Meta-Analysis trigger list) | **stay open** | `supervisor.py` not touched; Module 116's six rewordings behave exactly as before (5 reach this layer; none fires; the G1-en one is the top hazard) |

Rows updated accordingly in `GOLD_QA_REMAINING_FIXES_PLAN.md`. None of the
five is closed on the strength of the design.

---

## 7. Regression — all 32 gold questions

**Routes, all nine load-bearing fields, before and after**
(`module145_route_dict_equality.py`; before on the pristine `origin/main`
worktree, after on this branch; the four LLM-decided questions — A1, CR3,
G1, G6 — called live on the local model):

```
  route              moves on 0/32
  case_scope         moves on 0/32
  target_entity      moves on 0/32
  output_format      moves on 0/32
  target_year        moves on 0/32
  station            moves on 0/32
  district           moves on 0/32
  secondary_methods  moves on 0/32
  aggregate_kind     moves on 0/32

ALL LOAD-BEARING FIELDS EQUAL on all 32
```

(Only `reason`, free text the model writes fresh each call, differs on G1
and G6 — advisory, as Module 92 classified it. Two earlier "before" captures
had one question each come back `ERROR:ClientError`: the shared local model
timed out under other tracks' load, Groq was exhausted, and the Gemini
fallback 404'd on `gemini-2.5-flash`, which `.env` and `config.py`'s default
still pin — filed as **166**. Neither error is on this branch's path and
both captures were repeated until the four LLM-decided questions answered.)

**Aggregate dispatch and rendered text**, all 32 through `run_aggregate()`
against the live graph (`module144_all32_inprocess.py`, unchanged, run on
both trees, `module145_live/module145_all32_aggregates_{before,after}.json`):
**dispatch kind and rendered text identical on 32 of 32.**

Answers were not re-run end-to-end for all 32, and the reason is stated
rather than assumed: the semantic layer fires on none of them (§5.3), every
routing field is equal (above), and every aggregate's rendered text is equal
— so the inputs to everything downstream are identical, and 64 more
end-to-end runs on the shared model server would measure pipeline
nondeterminism, not this change. The one gold-shaped path that *could* have
moved — KB3's data half, whose plan sub-query resolves by phrase — was
checked: `rag.py`'s `_KbDataHalfPlan` sub-queries are pre-resolved by phrase
and never reach the layer.

---

## 8. New defects, filed not fixed

Highest number in use at filing time: **156** — Module 143 (merged as PR #93 while this module ran) filed 152–156 in `MODULE143_RESULT.md` §8, so these start at **160**. Its 156 is a cousin of this module: an officer-role proportion paraphrase that already reaches XAGG and is served a raw *"(no matching cases found)"*.

**157 — The cross-encoder is blind to Roman-Urdu.** Every Roman-Urdu item in
the 256-item corpus scores ≤ 0.104 against every capability description
(Urdu script: 0.90–0.99 on the same questions). Semantic dispatch therefore
does nothing for one of the three product languages: Roman-Urdu paraphrase
reach is 4 → 4. e5 does read Roman-Urdu but collapses it onto a hub
description (§6.2), so neither scorer helps. A Roman-Urdu → Urdu-script
transliteration before scoring is the obvious probe; not measured here.

**158 — L2 has no aggregate to reach.** *"anyone who used one of our citizen
services who is also under investigation"* needs a person join across the
PKM-applicant silo and the accused roster. `graph_recurrence_person` counts
`BELONGS_TO_CASE` recurrence, which is not that join; the cross-encoder's
0.002 is correct, not a miss. The capability is missing, not misrouted.

**159 — L3 scores 0.218: real but below the safe threshold.** *"convicted in
one matter but still being sought as a fugitive in another"* is CR2's shape
and the cross-encoder ranks `graph_recurrence_person` first, at 0.218 — under
the 0.351 a G1 rewording reaches. Catching it needs either a description that
names conviction/fugitive status (not done: that would be writing to the
test case) or a hazard-side fix for the G1 rewording (Modules 111/123's
layer).

**160 — Residual exposure: an organisation-network question one regex miss
from the weapon-recurrence aggregate.** router.txt's *"Map ORG-002's network
across all the cases it appears in"* scores 0.743 against
`graph_recurrence_weapon`. Unreachable today (the XGRAPH override claims
"across all the cases"), reachable by a rewording that dodges it.

**161 — The supervisor chokepoint is not built.** Modules 111/123's layer —
`_META_ANALYSIS_TRIGGER_PATTERNS` in `classify_to_subagent()` — has the same
disease and a ready-made target set (Module 116's six rewordings, 3 of 6
reaching Meta-Analysis). The same mechanism applies (describe the three
decomposition plans, score with the cross-encoder, fire above a measured
threshold), but the plan definitions live in `meta_analysis.py`, owned by
`muhafiz-m79` at the time of this module, and its hazard set would be the
inverse of this one (aggregate-shaped questions must *not* be decomposed).
Not attempted here; the G1-en rewording being this module's top hazard is a
preview of its difficulty.

**162 — M100b is blocked at the refusal tier, not the generic tier.** *"Is
the registering officer usually the investigating officer?"* hits
`_OFFICER_KEYWORDS` ("investigating officer") and resolves to the
`unsupported_officer` refusal, which is a purpose-built kind, so the semantic
layer never runs. The cross-encoder scores it 0.975 for
`officer_role_pair_overlap`. Whether refusals should also be a "miss" for the
semantic layer is a design question with its own hazard set (every "which
officer" question), not a threshold change.

**163 — The dead Gemini model is still pinned by configuration.** PR #89
unpinned the `gemini-2.5-flash` literal in `src/llm/client.py`, but
`config.GEMINI_MODEL`'s default and the checked-out `.env` still name it, so
the cloud-of-last-resort returns `404 NOT_FOUND … no longer available to new
users` whenever it is reached. Observed twice in this module's "before"
captures (A1 once, CR3 once): local model timed out under load → Groq
exhausted → Gemini 404 → `route_query()` raised. On a loaded day this turns a
slow classification into a hard error.

**164 — Two `test_harness_agent_semantic_search` tests fail by suite order.**
`test_semantic_search_is_registered_under_its_own_name` and
`test_supervisor_dispatches_to_real_semantic_search_and_real_rag_tool` pass
13/13 when the file runs alone and fail when it runs after `test_xagg`,
`test_router`, `test_harness_supervisor`, `test_harness_agent_meta_analysis`,
`test_harness_tool_xagg`, `test_harness_agent_large_scale_aggregate`,
`test_orchestrator`, `test_pipeline` and `test_config` — identically on
pristine `origin/main` @ `01ddc18` and on this branch. Some earlier suite
leaves the sub-agent registry (or a monkeypatched module attribute) in a
state the registration assertion does not expect. Not investigated here.
