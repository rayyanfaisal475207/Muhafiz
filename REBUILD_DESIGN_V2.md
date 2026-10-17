# Muhafiz — Rebuild II: how a question becomes a query

**Status:** design, not implemented. Nothing here is built yet.
**Date:** 15 October 2026.
**Scope:** the planning layer. Everything else is explicitly kept — see §3.

This does not supersede `REBUILD_DESIGN.md` (23 September 2026). That rebuild
diagnosed a real root cause — documents were chunked but never parsed — and it
worked. The corpus now holds **2,216 assertions and 12,760 spans over 7,658
documents in 73 cases**, and a count is a SQL count rather than a graph
traversal. Rebuild I is done.

This document addresses the layer Rebuild I did not touch: the step between a
question and a query.

---

## 0. The root cause this design exists to fix

Measured 15 October 2026. Asked *"how many complainants are recorded"*,
gpt-5.5's first plan was:

```
count_entities(person) where `complainant of` exists
```

That is the correct plan. It returns 71. It is the same plan that answers
*"how many people are recorded as accused"* → 92.

It was **rejected**. The repair ladder then spent all seven rungs
re-proposing it, and the run ended `unavailable` after 104 seconds and 8
planning calls.

The rejection came from `unused_terms`, and the reason was this:

```python
"complainants" in "... complainant of ..."   # False
```

The plural has an `s` where the predicate label has a space. The same check
passed for *accused* for no better reason than `"accused" in "accused in"`
happening to be `True`.

That specific bug is fixed. **It is a symptom.** The architecture is
*generate-then-check*:

1. the model emits a complete typed plan in one shot, including UUIDs it must
   transcribe;
2. seven post-hoc heuristics inspect the finished artifact and infer whether
   it means the right thing;
3. a rejection appends correction text and asks again, up to seven times.

### Three structural consequences, all measured in this codebase

**(a) The checks are forced to be heuristic.** They reconstruct intent from a
finished artifact, so they can only pattern-match. Seven of them do:

```
reach_through_relation   plan_cannot_match   outcome_is_not_an_answer
unused_terms             name_to_recorded    tidy_outputs    label_to_id
```

Before 15 October 2026, **zero of the seven had a unit test.**

**(b) A wrong check is invisible and expensive.** It rejects correct work; the
ladder cannot distinguish *"the plan is wrong"* from *"the checker is wrong"*,
so it repairs the wrong thing; and the run dies after ~100 seconds on a
question the system answers correctly. Measured across 5 identical runs of the
complainants question: 3 rejected, 2 asked for clarification, **0 answered**.

**(c) The model is required to invent identifiers.** UUID hallucination — 8
characters copied, 28 invented — needed `label_to_id` to repair after the
fact. The instinct *"why are we transcribing the UUID from the model"* was
right. The answer is that generate-then-check requires it: the model cannot
pick from a set it was never handed.

### The evidence that this is architecture and not the model

Same 7-question battery, same corpus, same day:

| Planner | Score |
|---|---|
| Groq `gpt-oss-120b` | 4/7 |
| OpenAI `gpt-5.5` | 4/7 |
| OpenAI `gpt-5.5`, after one 6-character fix to `_same_word` | **5/7** |

A frontier model bought nothing. Six characters of comparison logic bought a
question. The ceiling is not the model.

---

## 1. The shift: constructive planning

> Replace *"emit a plan, then check it"* with *"build a query out of bounded
> choices over measured candidate sets."*

The model never writes an identifier. At each step it receives a numbered list
the store computed from the actual records, and it picks. When the only
available choices are ones that **can** match, there is nothing left to
reject.

| | Generate-then-check (today) | Constructive (proposed) |
|---|---|---|
| What the model emits | a whole typed plan, with UUIDs | an index into a measured list |
| Where validity comes from | heuristics run afterwards | the shape of the choice itself |
| A wrong model output | rejected, repaired, maybe 7× | impossible, or a clarification |
| A wrong *checker* | silently kills correct plans | there is no checker |
| Cost of one failure | up to 8 calls × ~7k tokens | one re-offer of one slot |

### Failure classes this removes by construction

Every row is a failure measured in this system, not a hypothetical:

| Measured failure | Why it cannot occur |
|---|---|
| UUID hallucinated (8 copied, 28 invented) | the model emits an index, never an id |
| `search(text=None)` — search with no text | text is a required field of the search choice |
| *"FIRs registered in June 2026"* → bare `list(FIR)` | a time-classified question has a time slot that must be filled |
| plan filters a predicate its subject does not carry | candidates come from `carriers()`; a non-carrying pair is never offered |
| correct plan rejected on `complainants`/`complainant of` | no string test exists; the term *selected* the concept |
| plan ignores the question's terms (the 50-summary dump) | slots are filled *from* the grounded terms |

---

## 2. Stage design

```
0  scope         measured: case records reachable under this scope's RLS
1  read          the question → language, parts, terms          [keep as-is]
2  ground        each term → concepts and values that MEASURABLY occur
3  shape         choose one of the 12 algebra operations
4  slots         fill each required slot by bounded choice
5  execute       compile and run                                [keep as-is]
6  compose       draft + review, local only                     [keep as-is]
7  narrate       render with span citations                      [keep as-is]
```

The inversion is at stages 3–4: **the candidate set is computed first and the
model chooses second.** Today the model guesses first and we check second.

### Stage 2 — ground

Unchanged in purpose, promoted in authority. Today grounding is advisory and
the planner may ignore it; a trace line reads *"occurrence is a retrieval
signal, not semantic validation"*. In the new design grounding **produces the
candidate sets**, so a term that grounded to `complainant of` has already
chosen that concept — no later step re-derives the link from strings.

### Stage 3 — shape

The 12 operations stay exactly as they are (§3). What changes: the model is
offered only the operations the grounded terms can support, each with the
slots it would then have to fill. A `count_entities` offered for a question
whose terms ground to no entity type is not offered at all.

### Stage 4 — slots

Each slot is one bounded choice over a measured list:

- **population** — entity types that exist, with their row counts
- **predicate** — only predicates that `carriers()` says this subject carries
- **value** — only values `where_values_occur()` found, with occurrence counts
- **time** — required when stage 1 marked the question time-bounded; the
  corpus's own date range and grain are offered
- **search text** — required and non-empty when the shape is `search`

A slot with exactly one candidate is filled without asking the model. A slot
with none is the answer: *that is measured absence*, which is a finding, not a
failure. A slot whose candidates are genuinely ambiguous becomes a
clarification — which is what the repair ladder was being used to avoid.

---

## 3. What survives, unchanged

A rebuild that discards what is measured to work is not a rebuild, it is a
restart. These stay:

- **The closed query algebra**, all 12 operations: `search, list,
  count_entities, count_assertions, sum, average, average_duration, timeline,
  traverse, network, difference, ratio`. The vocabulary is right. What failed
  was how a plan gets *built* in it.
- **4-arm retrieval fused by RRF (k=60)** — lexical `tsquery`, semantic
  (e5/Chroma), trigram fuzzy, graph 1-hop.
- **RLS on every `evidence.*` table**, `document_allowed(...)` via
  `set_config`. Non-negotiable.
- **Span-level citation**: a claim quotes a passage or it does not exist
  (`span_id` is `NOT NULL`).
- **Local-only Draft and Review.** Case text never leaves the machine. The
  cloud sees a plan, never a record.
- **The whole extraction layer** from Rebuild I.
- **`roman_skeleton()` + devowelling** for Urdu/English/Roman-Urdu matching.
- **The trace.** This is what made the diagnosis in §0 possible at all.
- **The provider seam** (`EVIDENCE_PLANNER_PROVIDER`) and its self-discovering
  request dialect.
- **Both caches**: the response cache, and the provider prefix cache
  (measured: 39% of input tokens discounted cold, 66% warm).
- **~4,440 passing tests.**

## 4. What is deleted

- the seven post-hoc gates;
- the seven-rung repair ladder — a constructively-built query has nothing to
  repair, and a genuine ambiguity becomes a clarification;
- `label_to_id`, `name_to_recorded`, `tidy_outputs` — every one of them exists
  to repair an identifier the model should never have been asked to write.

---

## 5. Cost

More round trips, each far smaller, and no repair storms.

| | Today | Proposed |
|---|---|---|
| Happy path | 1 call × ~7k in | 2–4 calls × ~1–2k in |
| Failing path (measured) | **11 calls, ~90s, `unavailable`** | one re-offer, or a clarification |
| Prefix cacheable | 39% (catalog is trimmed per question, so the prefix moves) | most of each request — every stage's prompt is fixed |

The prefix-cache gain is not incidental. Today the catalog is trimmed per
question (`concepts_sent=27, concepts_dropped=8`), so even the same corpus
produces a different prefix for every question. Fixed per-stage prompts make
the stable part genuinely stable.

---

## 6. Migration — without losing the corpus or the suite

**The seam for this already exists in the code.** `runtime_registry.build_runtime()`
selects a runtime by `EVIDENCE_WORKFLOW_VERSION`; 2.0 is a SHA-256-sealed
frozen kernel and 2.1 is current.

So: build the new planner as **3.0 beside 2.1**, same store, same corpus, same
tests for everything shared, selectable per request. Run both against the same
battery and compare. Cut over when 3.0 wins on measurement, not on argument.

No big bang, no frozen corpus, nothing discarded before its replacement is
measured better.

---

## 7. How we will know it worked

The battery first (7), then the 20, then the 60. Per question: status, model
calls, tokens in/out/cached, seconds — and one new metric:

> **was a correct plan ever rejected?**

That is the number that matters, because it is the failure that cost us the
most and the one nobody was measuring. Generate-then-check cannot measure it.
Constructive planning should make it meaningless.

Current baseline on the 7, for comparison: **Groq 4/7, gpt-5.5 5/7.**

---

## 8. Open questions — these need your answer before any code

1. **Scope.** This design covers the query/answer layer, which is what is
   failing. "Build the complete app" may also mean frontend, ingestion and
   admin. Those are not implicated in §0 — do you want them in, and if so
   what is wrong with them that needs changing?
2. **The frozen 2.0 kernel.** Its seal has been broken by git line-ending
   normalization since before this session. Re-seal it, or restore
   `runtime_v2_0.py` from history? I have deliberately not done either.
3. **Done.** What score on the 60-question set is acceptable to ship?
4. **Temperature.** gpt-5.5 refuses any temperature but its default, so the
   planning ladder has no diversity lever on it. Keep the lever by planning on
   `gpt-5.4-mini`, or drop temperature as a mechanism? Constructive planning
   needs it much less.
5. **Case access for end-to-end testing.** Only three accounts hold the 73
   cases. Browser-path testing needs either your login or permission to assign
   cases to a test account.
