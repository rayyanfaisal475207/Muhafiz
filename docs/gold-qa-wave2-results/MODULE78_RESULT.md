# Module 78 — KB3 and KB9 never reach RAG, because the classifier reads their data clause and drops their law clause

**Branch:** `fix/router-paraphrase-generalisation` · **Base:** `main` @ `6cf89fb`
**Brief:** Module 78 in `GOLD_QA_REMAINING_FIXES_PLAN.md`; **Module 63** closed
in the same causal chain; **Module 68** measured again and deliberately left
open.
**Filed by:** Modules 74 and 76 (which own `xagg.py`), sharpened by Module 77
(which owns `rag.py`'s plan entries) — both were barred from `router.py`.

**Environment.** Own worktree `D:/Rapids AI/muhafiz-router`, own backend on
**`:8030`**. Model server `https://discharge-fascism-richness.ngrok-free.dev`,
`/health` 200 with embedder and reranker loaded. Chroma shared and read only.
The full `pytest -q` suite was **not** run — it empties
`muhafiz_entity_descriptions`.

**Quota check**, Module 81's fixed pattern:

```
grep -ciE "rate limit|RESOURCE_EXHAUSTED|quota|UNAVAILABLE|(^|[^0-9.,:])(429|503)([^0-9]|$)" backend.log
```

**0**, over **42 live runs**.

**One infrastructure failure, named because it is in the artefacts.** Docker
stopped mid-regression and KB8's second run returned `HTTP 500` in 4.1 s — the
traceback is `ConnectionRefusedError` inside `get_current_user`, i.e. Postgres,
before any routing happened. Docker was restarted and KB8 re-run twice; the
failed row is left in `scratchpad/m78_regression.json` rather than deleted.

---

## 0. The headline

**KB3 and KB9 now reach RAG on their own gold wordings, 3 runs of 3 each, and
their Module 77 plan entries fire on every one of those runs.** KB3's answer
carries gold's own figure — *"the same person in 68 of 74 recorded assignment
pairs (92%). Roles are split in only 6 pair(s)"* — on **3 of 3**. KB9 reaches
CrPC s.174, s.176 and Punjab Police Rules 25.31 and states gold's **10 FIRs
citing PPC §302** on 1 of 3; the other two runs are the grounding verifier's
`status=error` **after** the aggregate was already composed into the window —
Module 85's open flap, in `verifier.py`, which is out of bounds here.

**The classifier was the variable, and the rewriter was not.** That had to be
measured, not assumed: `orchestrator.py` routes on `rewritten_query`, so "the
router discriminates on the gold wording" was not established until the
rewriter was excluded. It returns both gold strings **byte-identical 3 of 3**.

**The fix is Module 60/67's resolver-gated pattern, one route over**, and it is
checked **last** — so it can only fire where this file previously had no
opinion at all. All-32 equality control: **eight questions move from "no
override" to a deterministic RAG, and they are exactly the eight KB questions.
Only two of them change their live route: KB3 and KB9.** The other 24 are
byte-identical, and all ten mandated regression questions re-measured unchanged
live.

**What is honest about the paraphrase bar:** 8 of 8 KB paraphrases pass the
gate, **7 of 8 reach RAG**, and a **new** paraphrase of KB3 that this module
had never seen routes RAG 3/3 but **does not fire its plan** — that is
`rag.py`'s pattern reach, not routing, and it is Module 56's finding for the
fourth time. Both are reported as misses that happened, not designed away.

---

## 1. Root cause

### 1.1 What was measured, and in what order

The brief supplied a control rather than a cause: Module 77 proved an English
paraphrase of each question routes to RAG and fires its plan, so the defect
attaches to the two gold wordings. Two variables sit between the typed
question and `route_query()`, and only one of them had ever been looked at.

`scratchpad/m78_bisect.py`, 3 runs per question, in process, temperature 0:

| Question | `rewrite_query()` output | `route_query(gold)` | `route_query(rewritten)` | confidence |
|---|---|---|---|---|
| **KB3** (gold) | **byte-identical, 3/3** | **XNETWORK 3/3** | XNETWORK 3/3 | `medium` |
| **KB9** (gold) | **byte-identical, 3/3** | **XAGG 3/3** | XAGG 3/3 | `medium` |
| P-KB3 (Module 77's paraphrase) | byte-identical, 3/3 | **RAG 3/3** | RAG 3/3 | `high` |
| P-KB9 (Module 77's paraphrase) | byte-identical, 3/3 | **RAG 3/3** | RAG 3/3 | `high` |

**The rewriter is excluded.** It returns all four strings unchanged, and the
route is the same whether the classifier is handed the gold text or the
rewriter's output.

**The classifier is the variable, and it says why itself.** Its own `reason`
field, identical across all three runs:

> **KB3** — *"Asks for a comparison between legal expectations and actual data
> patterns across cases — an **open-ended synthesis of cross-case data**, not a
> single-case or entity lookup"*
>
> **KB9** — *"This is a **cross-case aggregate** question about case metadata
> … and **specifically mentions that there are many such cases**"*

### 1.2 The mechanism, stated plainly

Both questions are compound: a **legal-norm clause** and a **data clause**. In
both, the data clause carries a whole-caseload scope cue —

- KB3: *"…and does that match what actually happens in **our data**?"*
- KB9: *"…**khaas tor par jab hamare itne cases mein maut shamil hai**"*
  (especially when so many of our cases involve death)

— and the classifier weighs that scope cue **above** the norm clause. It
classifies the second half of the question and drops the first. XNETWORK is
even the *right* route for "an open-ended synthesis of cross-case data"; the
error is upstream of that judgement, in which clause is being judged.

Module 77's paraphrases route RAG for exactly the complementary reason: they
carry no whole-caseload cue (*"what does our own data actually show about
that?"*, *"does our system record that anywhere?"*). That is the bisect closed:
the discriminator is a specific clause shape, not the questions' subjects and
not day-to-day instability. 3/3, 3/3, 3/3, 3/3, at temperature 0.

### 1.3 Where the brief was right, and where it was incomplete

The brief was right that this is a wording defect and right that Module 77's
pair is a controlled A/B. It was incomplete in one way worth recording: it
framed the candidate layers as "the router prompt, the deterministic override,
or the intent gate", as alternatives. **Two of the three were needed** — the
deterministic override for the route, and the intent gate for the paraphrase
class (Module 63, §2.2), because after this change the gate *is* the routing
decision and its Roman-Urdu blind spot became a routing blind spot.

### 1.4 Why not the router prompt

The instinct is a `prompts/router.txt` few-shot. `router.py`'s own opening
comment records that being tried and failing for other shapes — the local
Qwen3-14B defaults past prompt instructions *"including the router prompt's OWN
literal few-shot example verbatim"*. A prompt edit is also unmeasurable in the
all-32 equality control the brief requires, because every one of the 32 routes
would then depend on an LLM call. A confirmed, reproducible misclassification
class is what the deterministic layer exists for; that is the entire
justification for the five override lists already in the file.

---

## 2. Change

Two source files. **Not touched:** `verifier.py`, `validation.py`,
`meta_analysis.py`, `evaluation/`, `xagg.py`, `src/retrieval/`, any prompt, and
`supervisor.py` — see §2.3 for why the last one needed no change.

### 2.1 `src/pipeline/router.py` — the override (Module 78)

`_is_legal_kb_question()`, a lazily-imported wrapper over
**`rag.py`'s own `_is_legal_kb_intent()`**, plus one check at the **end** of
`_deterministic_route_override()` returning `route: "RAG"`.

**Why that predicate and not a sixth pattern list.** This is Module 60/67's
shape, one route over. Module 60 routes to XAGG by asking `xagg.py`'s dispatch
chain *"would you answer this?"*; this asks `rag.py`'s KB gate *"would you
treat this as a legal-KB question?"* — the same predicate that already decides
whether the RAG tool searches the KB-only corpus (Module 8c), generates statute
hypotheses (Module 30), renders the query into English (Module 52) and runs a
`_KB_DATA_HALF_PLANS` entry (Modules 39/77). Routing to RAG exactly when that
gate is True cannot drift from what RAG will do with the question, and it
brings no vocabulary of its own — which is the failure Modules 41, 56, 62, 74
and 77 have now each recorded independently, where a pattern list written from
one gold string reaches that string and very little else.

**Why last, and why that is load-bearing.** Placed after every other override
loop, it can only fire where the file currently has **no opinion at all** — it
is structurally incapable of outranking an existing override, whatever the KB
gate later grows to accept. G5's weapon-compliance scan, G3's court-readiness
scan, CR7's cross-check and M4's statute/court join keep their entries **by
construction**, not because the gate happens to say False for them today.
Pinned by `test_module78_the_override_is_checked_last_and_cannot_outrank_an_earlier_one`,
which asserts on a synthetic query that is in **both** sets, because a control
that can only pass is not a control. It also sits below the
`case_id`/`_ACTIVE_CASE_RE` short-circuit, so a KB-shaped question asked inside
a case-scoped chat is still the within-case path's.

**One regression this module caused, caught by the existing suite and fixed
rather than re-baselined.** `rag.py`'s `_CASE_ANCHOR_RE` knows `CASE-009`, a
bare `891/24`, "this case" and "in case" — but not **"this weapon"**. It never
had to: that gate runs inside a tool that already knows its own scope. Used as
a **routing** signal it does, and findings.md Module 7's own live-tested
compound example — *"What is this weapon's condition, and what PPC section
covers illegal possession of an unlicensed firearm?"* — trips
`_LEGAL_KB_INTENT_PATTERNS` on the bare token "PPC", was pulled to RAG, and
silently dropped the `secondary_methods` half only the LLM call can populate.
Guarded with the router's own `_SQL_OVERRIDE_COMPOUND_THIS_X_RE`, already
written one override up for exactly this purpose, so the two stay in lockstep
instead of drifting as two copies.

### 2.2 `src/pipeline/harness/tools/rag.py` — the gate (Module 63)

Module 63 was open, filed by Module 52, and it is in this module's causal
chain: after §2.1 the gate decides a **route**, so a False here is a whole
wrong path, not just a wrong corpus.

**Re-measured before touching it, and it is worse than the tracker said** —
one ordinary paraphrase per KB question, and the gate returns False on **four
of eight**, not one: KB2, KB4, KB5 **and** KB6's Roman-Urdu rewordings. The
cause is surface form, not concept: `\busool\b` cannot see **"usoolon"**, the
ordinary oblique plural and the actual word in Module 52's KB6 paraphrase.

Three widenings, each keeping the existing two-signal AND:

1. **`_NORM_SIGNAL_RE`** — the Roman-Urdu group goes from seven fixed word
   forms to their inflected forms, plus "muqarrara"/"baqaida" (the Roman-Urdu
   twin of the Urdu-script `(باقاعدہ|مقررہ)` pattern already in the list) and
   "miyaar".
2. **Pattern (c)**, witness/accused statement recording — the Roman-Urdu and
   Urdu-script forms of the same question (gawah/mulzim + bayan/kaha).
3. **Pattern (d)**, named corpora — "forensics ke usoolon" and its Urdu-script
   form, requiring the corpus name **and** its principles/rules word.

**One over-widening was caught by this module's own negative control and
narrowed rather than shipped.** Bare "tareeqa" is both *procedure* and
*manner*: *"cases **kis tareeqe se** station ke hisaab se bante hain"* is a
plain data question and passed the AND with "hamare record" in the same
sentence. The interrogative form is now excluded by lookbehind and the
declarative kept, with the failing string pinned as a test. It is reported here
because it happened, not because it was foreseen.

### 2.3 `supervisor.py` — measured, and deliberately unchanged

`classify_to_subagent()` was probed with `route="RAG"` for all eight gold KB
questions and both Module 77 paraphrases: **Semantic Search, 10 of 10.** No KB
question trips `_META_ANALYSIS_TRIGGER_PATTERNS` on the RAG route, so nothing
decomposes away from the KB path. Module 60's guard, whose `route == "XAGG"`
precondition the brief protects, is untouched — this module never widens it.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_router.py tests/test_harness_supervisor.py \
            tests/test_kb_statute_retrieval.py tests/test_harness_tool_rag.py -q
```

**431 collected, 431 passed, 0 failed** (baseline on `main` @ `6cf89fb`: 417).
`tests/test_router.py` 129 → 142; `tests/test_harness_tool_rag.py` 41 → 46.
Adjacent suites re-run green in the same session: `tests/test_xagg.py`,
`tests/test_harness_agent_semantic_search.py`,
`tests/test_harness_agent_meta_analysis.py` — **507 passed**.

### 3.1 The all-32 EQUALITY control

`test_module78_all_32_gold_questions_route_exactly_as_measured`, asserted as
equality against the map captured on `main` @ `6cf89fb`, naming **every**
question whose deterministic route changes:

| Question | Before | After | Live route before (Module 27, 3 passes) | Live route after |
|---|---|---|---|---|
| **KB3** | *(none)* | **RAG** | **XNETWORK 3/3** | **RAG 3/3** |
| **KB9** | *(none)* | **RAG** | **XAGG 3/3** | **RAG 3/3** |
| KB1 | *(none)* | RAG | RAG 3/3 | unchanged |
| KB2 | *(none)* | RAG | RAG 3/3 | unchanged |
| KB4 | *(none)* | RAG | RAG 3/3 | **RAG 2/2** |
| KB5 | *(none)* | RAG | RAG 3/3 | **RAG 2/2** |
| KB6 | *(none)* | RAG | RAG 3/3 | **RAG 2/2** |
| KB8 | *(none)* | RAG | RAG 3/3 | **RAG 3/3** |
| **the other 24** | — | — | — | **byte-identical** |

Eight rows move in the *deterministic* map; **two change route**. The six that
do not are not cosmetic either — they stop being a coin flip. Module 68
measured this same classifier returning **6 RAG / 2 XAGG over eight calls** on
one of these strings.

Three inherited controls were amended, each with its reason recorded in the
test itself rather than silently re-baselined:

| Test | Amendment |
|---|---|
| `test_module60_all_32_gold_questions_route_exactly_as_before_except_m4` | expected map layered with Module 78's eight, the same way Module 67 layered its four |
| `test_module67_all_32_gold_questions_route_exactly_as_measured` | same; and `KB5` moves from "`None`" to "`RAG`, and never a cross-case route", which is what that line was protecting |
| `test_module67_no_kb_question_is_ever_captured` | second assertion restated as its actual claim — whatever this file decides for a KB question, it is never XGRAPH/XAGG/XNETWORK |
| `test_module60_broad_form_was_rejected_for_a_measured_reason` | residue filter is now "this file does not route it to XAGG" rather than "has no opinion", so KB3 and KB5 stay **in** the residue instead of silently dropping out of it and asserting nothing |

### 3.2 New regressions

| Test | What it pins |
|---|---|
| `test_module78_kb3_and_kb9_literal_gold_text_reaches_rag` | **the brief's requirement** — KB3's and KB9's literal gold bytes, restated in the test *and* asserted byte-equal to the dataset's copy, both returning `route="RAG"` |
| `test_module78_only_kb3_and_kb9_actually_change_their_live_route` | the six unchanged questions, against Module 27's measured routes |
| `test_module78_every_kb_paraphrase_reaches_rag_too` | one paraphrase per KB question: gate 8/8, route 7/8, **P-KB1's miss pinned at its measured value** with the reason |
| `test_module78_no_non_kb_gold_question_or_paraphrase_is_claimed` | the 24 non-KB gold questions **and** nine non-KB paraphrases, all False |
| `test_module78_the_override_is_checked_last_and_cannot_outrank_an_earlier_one` | a synthetic query in both sets stays XAGG; a SQL-shaped one stays SQL |
| `test_module78_a_case_specific_this_x_compound_is_never_claimed` | findings.md Module 7's compound example — with `_is_legal_kb_intent()` asserted **True**, so the test states the trap rather than hiding it |
| `test_module78_an_active_case_still_short_circuits_the_kb_override` | `case_id="CASE-009"` → no override |
| `test_module78_a_broken_rag_import_leaves_routing_unchanged` | the lazy import, same guard Module 60/67 wrote for `xagg` |
| `test_module63_*` (×5) | the gate: 8 paraphrases True, Module 52's own string, 9 non-KB paraphrases False, the all-32 gate equality control, and the case-anchor refusal |

---

## 4. Live verification

Backend `:8030`, real `/api/chat`, SSE parsed for `route=`; the
`XAGG <kind>` line and the `KB data-half plan '<plan>' answered by aggregate
'<kind>'` line read out of `backend.log` per run (Module 55). Runner:
`scratchpad/m78_live.py`, which **imports** `scripts/module74_live_runs.py`
unchanged, so this is measured by the same code Modules 74 and 77 used.
Artefacts: `scratchpad/m78_runs.json`, `scratchpad/m78_regression.json`,
`scratchpad/m78_regression2.json`, `scratchpad/m78_bisect.json`.

### 4.1 KB3 and KB9, gold wording, 3 runs each

| Q | Route | Plan consulted? | `XAGG <kind>` line | Answer status | Gold's figure in the answer |
|---|---|---|---|---|---|
| **KB3** | **RAG 3/3** *(was XNETWORK 3/3)* | **fired 3/3** | `XAGG officer_role_pair_overlap: 144 assignment edge(s), 74 investigating / 70 recording; same officer on 68 of 74 pair(s) (91.9%), 6 split across 6 case(s)` | done 3/3 | **68 of 74 — 3 of 3** |
| **KB9** | **RAG 3/3** *(was XAGG 3/3)* | **fired 3/3** | `XAGG fir_section_case_count: 218 section entr(ies) over 73 FIR(s) … focus=302 -> 10 FIR(s)` | done 1/3, `error` 2/3 | **10 + PPC 302 — 1 of 3** |

KB9's two `status=error` runs are the same 82-character string both times —
*"The generated answer could not be verified as grounded in the retrieved
documents"* — emitted **after** the plan fired and the aggregate chunk was in
the window. That is **Module 85**, in `verifier.py`, out of bounds here, and it
is now the only thing between KB9 and its gold answer.

### 4.2 KB3, verbatim (run 1, `route=RAG`)

> ### Governing Provision:
> The law does not explicitly require the officer who first registers a case to
> be the same one who investigates it. According to [Document 4], a police
> officer may investigate a non-cognizable case only with the order of a
> Magistrate … Additionally, [Document 7] states that an officer in charge of a
> station may be deputed to investigate …
>
> ### Case Records:
> In our case records, the two roles (the officer who recorded the FIR and the
> officer who investigated it) are mostly NOT separated: the officer who
> recorded the FIR and the officer who investigated it are **the same person in
> 68 of 74 recorded assignment pairs (92%)**. Roles are split in only **6
> pair(s), across 6 case(s)**. Of those 6, 3 of them carry an investigating
> officer with no recording officer on the same case at all … This indicates
> that while the law allows for the roles to be separate, in practice, they are
> often combined.

Compare what this question returned on all three of Module 27's passes:

> No cross-case connections or patterns were found for this query … (nearest
> cluster found was distance 0.179 against a relevance cutoff of 0.145).

### 4.3 KB9, verbatim (run 3, `route=RAG`, both halves)

> ### Part 1: What the Law Requires
> According to the **Code of Criminal Procedure (Pakistan)**, when a person
> dies … under circumstances that suggest unnatural or suspicious death, the
> police are required to conduct an inquiry into the cause of death. This is
> outlined in **Section 174** … Additionally, **Section 176** … Furthermore,
> **Punjab Police Rules-III (Rule 25.31)** states that upon receiving
> information of the sudden or unnatural death of any person … the officer in
> charge must immediately inform the nearest magistrate authorized to hold
> inquests …
>
> ### Part 2: What Our Case Records Actually Show
> According to **our own case records** (Document 7), out of **73 FIRs** that
> carry a recorded section cite, **10** of them cite **PPC §302** … However,
> the records do not explicitly state whether the police conducted detailed
> investigations into the cause of death in these cases, as required by the
> law …

Its pre-fix answer, all three of Module 27's passes, was `XAGG` →
`graph_recurrence`: *"four individuals are linked to multiple cases"* — a
number with nothing to do with suspicious deaths.

---

## 5. Gold comparison

| Q | Gold's law half | Gold's data half | Measured | Verdict |
|---|---|---|---|---|
| **KB3** | Police Order 2002 **Art. 18** — the law expects **separation** | same officer in **68 of 74** pairs (92 %); split in only 6 | law half reaches CrPC/deputation text and concludes the law **does not require** separation; data half **68 of 74 (92 %), 6 split, 3 of them unresolvable** | **data half exact, 3/3 — the first time it has ever appeared on KB3's own wording.** Law half **contradicts gold's conclusion** and is **Module 49**, open and untouched here |
| **KB9** | CrPC **s.174** — inquiry and report on the apparent cause of death, to the magistrate | **10 FIRs** cite PPC 302; **no inquest / post-mortem / cause-of-death record** in the schema | s.174 **and** s.176 **and** Punjab Police Rules 25.31; **10 FIRs cite PPC §302**; the schema gap stated | **both halves, 1 of 3** — and on that run it is a clean pass, including gold's "the data lacks this" verdict. The other 2 of 3 are Module 85 |

### Both halves, per question, over 3 runs — before and after

| Q | Norm half before → after | Data half before → after | Both |
|---|---|---|---|
| KB3 | **0/3 → 3/3** (statute text, wrong conclusion) | **0/3 → 3/3** | **0/3 → 3/3** with gold's figure; conclusion still Module 49's |
| KB9 | **0/3 → 3/3** retrieved; served 1/3 | **0/3 → 3/3** composed; served 1/3 | **0/3 → 1/3**, the 2 losses in the verifier |

**Nothing was tuned toward gold.** No prompt was edited, no wording compared,
no threshold moved. The only figures asserted anywhere in this module's code or
tests are Module 27's measured routes.

---

## 6. Non-gold paraphrase

The acceptance bar the brief set: both the gold wording **and** a non-gold
paraphrase, for every question touched.

### 6.1 The gate — 8 of 8

One ordinary rewording per KB question, in that question's own language,
sharing no distinctive phrase with its gold text. Before this module: **4 of 8
False** (KB2, KB4, KB5, KB6 — all Roman-Urdu). After: **8 of 8 True**.
Nine non-KB paraphrases and all 24 non-KB gold questions stay False.

### 6.2 The route — 7 of 8, and the eighth is named

| Paraphrase | Gate | Route |
|---|---|---|
| P-KB2, P-KB3, P-KB4, P-KB5, P-KB6, P-KB8, P-KB9 | True | **RAG** |
| **P-KB1** — *"What rule decides when a written **complaint** has to be turned into a formal **FIR** …"* | True | **XAGG** |

P-KB1 collides with Module 15's CR6 entry in `_XAGG_OVERRIDE_PATTERNS`
(`complaint` within 60 characters of `FIR`), which is checked earlier and
therefore wins. This is the cost of the "checked last" placement, stated
plainly: the property that makes this override safe also lets an earlier list
claim a KB paraphrase. **KB1's own gold text is unaffected** — it says "report
of a crime", not "complaint" — and still routes RAG. Pinned at its measured
value in the test rather than removed from the battery; filed as **Module 88**.

### 6.3 Live, on wordings this module never saw

Three runs each, real `/api/chat`:

| Probe | Route | Plan fired | Data half in answer |
|---|---|---|---|
| **P-KB3** (Module 77's) | **RAG 3/3** | `officer_role_pair` **3/3** | **68 of 74 — 3/3** |
| **P-KB9** (Module 77's) | **RAG 3/3** | `death_investigation_charging` **3/3** | answered 2/3; the served answers carry s.174 and the schema gap, **not the count** |
| **P2-KB3** — *"Is the officer who writes up an FIR meant to be a different person from the one who investigates the case, and do our own case records bear that out across the whole caseload?"* (new) | **RAG 3/3** | **never** | **0/3** |
| **P2-KB9** — *"Agar maut mashkook lage to kya police par maut ki wajah maloom karne ki qanooni zimmedari hai, aur kya hamare record mein aisi tehqeeqat kahin darj hoti hai?"* (new) | **RAG 3/3** | `death_investigation_charging` **3/3** | `status=error` 3/3 (Module 85) |

**P2-KB3 is a real, reported miss.** Routing is fixed for it — 3 of 3 — and it
still returns no data half, because `rag.py::_KB_DATA_HALF_PLANS`'
`officer_role_pair` pattern does not match "writes up an FIR" / "bear that out".
That is **Module 56's finding for the fourth time** (Modules 56, 74, 77, now
this one) and it is a *different layer* from the one this module fixes: the
route is right, the plan's vocabulary is narrow. Filed as **Module 89** rather
than widened here, because widening that pattern without re-running Module 77's
own all-32 plan-resolution control is exactly the mistake this project keeps
recording.

**So the honest verdict on the acceptance bar: routing generalises (4 of 4 new
and old paraphrases route RAG, plus 7 of 8 in the gate battery); the layers
below it do not fully — one paraphrase loses its plan, one loses its answer to
the verifier.** Neither is a router defect and neither is claimed as fixed.

---

## 7. Regression guard

Every question the brief named, live on `:8030`, 2 runs each unless noted:

| Q | Route before | Route after | Result |
|---|---|---|---|
| **M4** | XAGG | **XAGG 4/4** | both halves — statute mix (PPC §34=40, Arms §13=29, PPC §392=21) **and** court stage (33 records, 1 verdict, 30 under trial) with gold's "the two do not agree" conclusion. `XAGG statute_court_stage_join` in the log every run — Module 60's fix holds |
| **G3** | XAGG | **XAGG 2/2** | 82 of 94 missing relationships, 2 of 32 weapons without licence status, 9 FIRs without an incident date |
| **CR7** | XAGG | **XAGG 2/2** | 33 records, 1 verdict / 32 in progress, the four-status breakdown, and the FIR 891-24 consistency example |
| **G2** | XAGG | **XAGG 2/2** | 9 of 73 FIRs without an incident date, named; 52 of 73 without an investigation status |
| **G5** | XAGG | **XAGG 2/2** | 30 of 32 unlicensed (~94 %), 2 with no status |
| **M5** | XAGG | **XAGG 2/2** | 2024 vs 2026 weapon/statute mix, with the CNSA §9(c) and PPC §302 additions |
| **KB5** | RAG | **RAG 2/2** | plan `violence_against_women` 2/2; **8 = gold exactly**, 2/2 |
| **KB4** | RAG | **RAG 2/2** | plan `property_register` 2/2; 28 FIRs / 13 forensic / 7 heirs, 2/2 — **the 45 is still missing, which is Module 86, unchanged** |
| **KB6** | RAG | **RAG 2/2** | plan `weapon_register` 2/2; **32 = gold exactly**, 2/2 |
| **KB8** | RAG | **RAG 3/3** | plan `chalaan_dispatch` 3/3; **26 = gold exactly on 2 of 3** — the same 2-of-3 Module 77 measured, unchanged |

**No question moved.** The all-32 equality control shows nothing outside the
eight KB rows moving deterministically, and these ten confirm it live. The
M4/G3 collision the brief names as the precedent (PR #8) cannot recur here by
construction: this override is checked after both of their pattern entries.

Also re-checked: `_resolved_xagg_override_kind()` still returns `None` for
every KB question, so Module 67's allow-list is untouched; Module 60's
supervisor guard and its `route == "XAGG"` precondition are not modified.

---

## 8. New defects found

Numbers to 87 are taken, so these start at 88. Both are left deliberately
unfixed.

- **Module 88 — Module 15's CR6 pattern claims an ordinary KB1 paraphrase.**
  *"What rule decides when a written **complaint** has to be turned into a
  formal **FIR**, and do our own records follow it?"* passes `rag.py`'s legal-KB
  gate but is intercepted first by `_XAGG_OVERRIDE_PATTERNS`' `complaint … FIR`
  entry and routed to XAGG's `cms_fir_linkage`, which has no statute to give
  it. KB1's own gold text is unaffected. The fix is a discriminator on CR6's
  pattern (its own question asks whether a complaint *links to* an FIR record,
  not what the law requires), and it needs live measurement of CR6, which this
  module has none of. Pinned at its measured value in
  `test_module78_every_kb_paraphrase_reaches_rag_too`.
- **Module 89 — `_KB_DATA_HALF_PLANS`' `officer_role_pair` pattern misses a
  third KB3 paraphrase.** *"Is the officer who **writes up** an FIR meant to be
  a different person from the one who investigates the case, and do our own
  case records **bear that out** across the whole caseload?"* routes to RAG 3 of
  3 after this module and fires **no** plan, so the answer has no data half.
  Module 74 widened this vocabulary once and Module 77 widened it again, each
  after its own paraphrases missed; this is the fourth instance of Module 56's
  finding and suggests the plan patterns want the same treatment the *route*
  just got — a predicate, not a word list. `rag.py`'s plan table is Module 77's
  and widening it needs that module's all-32 plan-resolution control re-run.

**Two existing modules were re-confirmed rather than re-filed.** **Module 85**
(the grounding verifier rejecting a composed KB answer that already has its
figure) now costs KB9 two of three runs and P2-KB9 three of three — it is the
single largest remaining loss on this question. **Module 49** (KB3's law half
reaching CrPC deputation text and concluding the opposite of gold's Police
Order Art. 18) is untouched and is now the only thing between KB3 and a full
pass.

### Module 68, re-measured and deliberately not fixed

The brief allowed taking `route_query()`'s `confidence` field only if a real
fix fell out. It did not, and the evidence is worth recording either way:

- In this module's own bisect the field **did** separate correct from
  incorrect: `medium` on both wrong classifications (KB3, KB9), `high` on both
  right ones (P-KB3, P-KB9), 12 of 12.
- That is **not** the same claim Module 67 tested and it does not overturn it.
  Module 67 measured KB3 over 8 calls at **6 RAG / 2 XAGG, reporting `medium`
  on all eight** — including the two correct ones. A field that is `medium`
  for a coin flip *and* `medium` for a stable-but-wrong answer *and* `high` for
  a stable-right one carries some signal and not enough to branch on, which is
  exactly what Module 67 concluded.
- What **has** changed is the exposure: eight questions no longer consult the
  LLM classifier at all, so `confidence` is now a deterministic `"high"` for
  them, which is honest. The remaining override-less set shrinks from 16 to 8.
- Correlating the field properly still needs run-to-run stability the router
  cannot see from one call, and removing it still changes an SSE contract
  (`orchestrator.py` puts it on the `router done` event). Neither belongs in a
  PR that is already changing routing. **Left open, unchanged.**
