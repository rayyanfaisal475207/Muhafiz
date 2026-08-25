# Findings — Route Sweep (2026-08-24)

Six real, live-verified gaps found during a rigorous, ground-truth-driven
sweep of all 9 routes (DIRECT, RAG, WEB, SQL, GRAPH, GRAPH_HYBRID, XGRAPH,
XAGG, XNETWORK) against the real running Postgres/AGE instance and the real
Muhafiz Data API sync corpus (73 cases, 478 Person nodes, 832 Officer nodes),
plus five follow-up gaps found while answering stakeholder questions:
Module 6 (GraphRAG-style community detection staleness), Module 7 (no
general adaptive multi-method retrieval), Modules 8-9 (Microsoft GraphRAG's
Local Search / Global Search methodologies, requested explicitly by the
team to be incorporated at the agent-harness level), and Module 10 (no
query decomposition / meta-analysis for broad questions, also requested
explicitly). Modules 1-6 were each reproduced against live data, traced to
a concrete root cause in the code; Modules 7-10 are confirmed by direct
code inspection (each module says so explicitly where a live repro wasn't
run). All are independent enough to be picked up as their own branch.

**How to use this document:** work top to bottom by priority, one module at
a time. Each module is scoped to be its own `git checkout -b`, its own
tests, its own live verification, its own merge to `main`. Do not start a
module's implementation without first re-reading its "Root cause" section
against the current code — line numbers will drift as earlier modules land.

**Standing git discipline** (same as every fix landed this session):
```bash
git checkout main && git pull origin main
git checkout -b <module-branch-name>
# implement + tests
python -m pytest -q                     # full suite green
# live-verify against the real running backend (see each module's plan)
git add -A && git commit -m "..."
git push -u origin <module-branch-name>
git checkout main
git merge --no-ff <module-branch-name> -m "Merge ..."
git push origin main
git branch -d <module-branch-name>
git push origin --delete <module-branch-name>
```
If the backend is already running while you test, remember it needs a
**manual restart** after any code change — it was found running without
`--reload` this session, so edits silently don't take effect until you
restart it. Confirm the fix with a fresh `curl .../health` timestamp check
or by killing and relaunching per `RUN.md` before trusting an HTTP-level
verification.

---

## Priority order

| # | Module | Severity | Size | Depends on |
|---|--------|----------|------|------------|
| 1 | [Relationship extraction gap](#module-1-relationship-extraction-gap-associated_with--0) ✅ RESOLVED 2026-08-24 | 🔴 High | Large | — |
| 2 | [Non-matched-attribute evidence gap](#module-2-non-matched-attribute-evidence-gap) ✅ RESOLVED 2026-08-24 | 🟠 Medium-High | Small | — |
| 3 | [Enumeration / list-synthesis refusal](#module-3-enumeration--list-synthesis-refusal) ✅ RESOLVED 2026-08-25 | 🟠 Medium-High | Medium | — |
| 4 | [XAGG entity-type coverage gap](#module-4-xagg-entity-type-coverage-gap-weapon-aggregation) ✅ RESOLVED 2026-08-25 | 🟡 Medium | Medium-Large | — |
| 5 | [SQL extractor phrasing brittleness](#module-5-sql-extractor-phrasing-brittleness) ✅ RESOLVED 2026-08-25 | 🟡 Medium | Medium | — |
| 6 | [Community detection never refreshes for real sync data](#module-6-community-detection-never-refreshes-for-real-sync-data) ✅ RESOLVED 2026-08-25 | 🟠 Medium-High | Small | — |
| 7 | [No general adaptive multi-method retrieval](#module-7-no-general-adaptive-multi-method-retrieval) ✅ RESOLVED 2026-08-25 | 🟡 Medium | Large | — |
| 8 | [Local Search — entity-based reasoning](#module-8-local-search--entity-based-reasoning) ✅ RESOLVED 2026-08-25 | 🟠 Medium-High | Large | — |
| 9 | [Global Search — whole-dataset map-reduce reasoning](#module-9-global-search--whole-dataset-map-reduce-reasoning) ✅ RESOLVED 2026-08-25 (both stages) | 🟠 Medium-High | Large | Module 6 |
| 10 | [Meta-analysis — query decomposition and aggregation](#module-10-meta-analysis--query-decomposition-and-aggregation) | 🟠 Medium-High | Large | Relates to 7, 8, 9 |
| 11 | [Unreviewed name-fallback duplicates poison community detection](#module-11-unreviewed-name-fallback-duplicates-poison-community-detection-plus-a-common-noun-mistagged-as-a-person) ✅ A1/A2 RESOLVED 2026-08-25 (B open) | 🟠 Medium-High | Small-Medium | Discovered via Module 9 |

Modules 1-8 are independent of each other (different files, no shared edit
surface) — they can be done in any order or in parallel across sessions.
Module 9 has one real dependency: it consumes community reports, so do
Module 6 first (or at least re-run a real community refresh) — otherwise
Module 9's live verification would be testing map-reduce over the same
Aug-22-stale data Module 6 exists to fix. Module 10 has no hard dependency
on 7/8/9 (it can dispatch to whichever single-method routes exist today),
but each of those, once built, becomes a stronger tool for Module 10's
sub-queries to call into — see Module 10's own "Relationship to Modules
7-9" note. Module 11 was found live-verifying Module 9's Stage 2 (a
single-case entity-extraction pathology was silently distorting community
detection's real graph shape) — independent of 1-10's own edit surfaces,
but worth doing before ever re-attempting to make Module 9's hierarchy
levels demonstrably useful on real data. The table order is by severity,
not a hard sequencing requirement beyond Module 9's one real dependency.

---

## Module 1: Relationship extraction gap (`ASSOCIATED_WITH` = 0)

### ✅ RESOLVED — 2026-08-24
Fixed on `main` (`241f783`), plus two follow-up gaps found via a post-fix
audit and fixed in the same pass (`cced058`, `f69d1c9`, `a4457f6`) — see
`MODULE1_GAPS_FIX_PROMPT.md` for the full trail. Summary:

- **Option A implemented**: `structured_projection.py`'s `project_fir()`
  now writes `ASSOCIATED_WITH{basis, confidence: 0.5}` between every pair
  of Person nodes (victim/complainant/accused/witnesses, never Officer)
  sharing an Incident.
- **Follow-up gap closed**: `cross_silo_projection.py`'s CMS/PKM people
  (added in a later pass than the FIR itself) now get pulled into that
  same pairing too, via `_pair_with_existing_incident_roster()`.
- **Follow-up gap closed**: no-CNIC re-syncs were stranding orphaned
  Person nodes (entity_resolution mints a fresh random id every call, no
  dedup) — `purge_orphaned_person_nodes_by_source_prefix()` now cleans
  these up automatically as part of every sync.
- **Live-verified end to end**, real Postgres/AGE instance, full
  73-case corpus: `ASSOCIATED_WITH` count 0 → 252, 69/73 cases now reach
  `hop_count >= 1` (the other 4 are genuinely single-person cases), full
  backend test suite green, zero data loss confirmed (73 Case nodes
  intact, no case lost all its people).

### Problem (as originally found — kept for context)
Live query against the real graph:
```cypher
MATCH ()-[r:ASSOCIATED_WITH]->() RETURN count(r) AS n   -- returns 0
```
`ASSOCIATED_WITH` is the **only** edge type `src/retrieval/graph_retriever.py`
follows to hop from one real-world entity to a *different* one (see that
file's own module docstring, "── Why LOCATED_AT/OWNS/REGISTERED_TO never
expand the traversal ──"). With zero such edges anywhere in the graph,
**every GRAPH / GRAPH_HYBRID / XGRAPH / XNETWORK query in production is
permanently `hop_count = 0`** — confirmed by every live trace this session,
none of which ever returned a hop > 0. A question like "who is associated
with طارق in this case" or "who are the co-accused" can only ever return the
seed entity itself, never anyone connected to them, for any of the 73 real
cases.

### Root cause
```
grep -rln "ASSOCIATED_WITH" src/ --include=*.py
```
returns `src/extraction/relationship_extraction.py`, `src/ingestion/service.py`
(its caller, gated behind `run_graph_extraction=True` — only ever set for
admin single-file uploads, per this session's earlier investigation),
`src/pipeline/orchestrator.py`, `src/retrieval/graph_retriever.py`,
`src/graph/community_detection.py`, `src/graph/case_scope.py`,
`src/pipeline/harness/agents/timeline_building.py`. **Neither
`src/graph/structured_projection.py` nor `src/graph/cross_silo_projection.py`
— the two modules that populate the real Muhafiz Data API sync corpus —
ever write this edge type.** The real ingestion pipeline never extracts a
relationship between two people/entities mentioned in the same case; it
only ever writes each entity's own attributes and its `BELONGS_TO_CASE`/
`APPEARS_IN` edges.

Concretely, `fir-233-26` (real case) has 5 distinct `Person` nodes
(طارق محمود, شعیب ارشد, ذیشان بٹ, محمد اسلم, حمزہ طارق) and multiple
`Officer` nodes, all correctly linked to the case — but **zero edges
between any of them**. There is no way, today, for the graph to say these
5 people are co-accused in the same incident, even though the structured
FIR data plainly puts them in it together.

### Design decision needed before implementing
This is a real product/design question, not a mechanical fix — pick one
(or propose another) before writing code:

- **Option A — cheap, structural "same-incident" edge.** In
  `structured_projection.py`'s FIR-writing path, after all of a case's
  Person/Officer/Weapon nodes are written, write `ASSOCIATED_WITH` between
  every pair of Person nodes that share the same `Incident` (mirroring how
  `INVOLVED_IN` already links Person→Incident per M6a/M6b — see that
  module's own comment on why `INVOLVED_IN` itself is deliberately NOT
  traversed, "two different accused both INVOLVED_IN the same Incident
  would otherwise appear connected by hopping through the Incident node").
  Cheapest to build, but risks manufacturing a relationship that isn't
  really stated anywhere (two witnesses in the same FIR aren't necessarily
  "associated" with each other) — would need a `basis`/confidence that
  honestly says "co-mentioned in the same case," not "known associate."
- **Option B — LLM-extracted, evidence-based relationship.** Reuse
  `src/extraction/relationship_extraction.py` (the module that already
  does this correctly for the legacy NER path) inside the real structured
  sync pipeline, running it over each case's narrative text (already
  chunked via `render_fir`/`render_cms`/etc. per M7) to extract genuinely
  stated relationships ("co-accused with", "known associate of") with a
  real `basis` string, same discipline entity resolution's SAME_AS
  candidates already follow. More correct, more expensive (an LLM call per
  case at sync time), and needs its own false-positive review queue
  (`src/api/graph_review.py` already has this pattern for SAME_AS/CITES —
  extend it, don't build a parallel one).
- **Option C — do nothing structurally, change the product surface.**
  Accept that the real corpus has no extractable relationship signal today
  and instead make GRAPH_HYBRID/XNETWORK answers explicitly say "no stated
  relationships found" rather than silently returning hop_count=0 with no
  explanation (XNETWORK already does this correctly today — see the live
  XNETWORK-1 test result: *"no relevant community clusters found for this
  question"* — worth checking whether GRAPH/GRAPH_HYBRID's within-case
  path degrades as gracefully, or just looks like a broken query).

### Suggested scope for a first pass
Recommend starting with **Option A**, scoped narrowly: write
`ASSOCIATED_WITH{basis: "co-mentioned in case <id>'s incident", confidence:
<something honestly lower than 1.0, e.g. 0.5>}` between Person↔Person pairs
sharing an Incident only (not Officer↔Person — an investigating officer
isn't "associated" with the accused in the co-conspirator sense). Cheap,
reversible (it's a `superseded_by`-versioned edge like everything else in
this graph, per `versioning.py`), and immediately unblocks real multi-hop
traversal for the most common real question shape ("who else is connected
to this case").

### Files likely touched
- `src/graph/structured_projection.py` — new edge-writing step, likely near
  wherever the case's Person roster is fully known (after all accused/
  witness/complainant rows are written for one FIR).
- `tests/test_structured_projection.py` (or wherever that module's tests
  live — confirm exact filename) — new tests asserting the edge is written
  for a multi-person FIR, and NOT written for a single-person FIR or
  between two people in different cases.
- `tests/test_graph_retriever.py` — no code change expected, but add an
  end-to-end test proving a real multi-hop traversal now works once the
  edge exists (mirrors the existing `test_associated_with_hop_reaches_a_second_entity_within_case`
  pattern, just with realistic co-accused fixture data).

### Test plan
- Unit: FIR with 3 accused → 3 `ASSOCIATED_WITH` edges (all pairs) written,
  each with the case's own basis text. FIR with 1 accused → 0 edges. Two
  different FIRs' accused → 0 cross-case edges written (never conflate two
  unrelated cases' people).
- Live verification: re-run this session's own `fir-233-26` ground truth —
  after a real sync, `MATCH (a:Person)-[r:ASSOCIATED_WITH]->(b:Person)
  RETURN count(r)` should be > 0 for that case, and a live
  `retrieve_graph()` call seeded on one of its 5 people should now return
  `hop_count >= 1` reaching at least one of the other 4.
- Full backend suite green, as always.

---

## Module 2: Non-matched-attribute evidence gap

### ✅ RESOLVED — 2026-08-24
Fixed on `main` (merged from `feature/graph-evidence-notable-properties`).
Added `_NOTABLE_PROPERTIES` alongside `_MATCHED_PROPERTY_LABELS` in
`src/retrieval/graph_retriever.py`, and extended
`_synthetic_evidence_chunk()` to always append a second clause listing any
notable property (`belt_no`/`phone` for Officer, `cnic`/`phone` for Person,
`plate` for Vehicle) the node carries beyond whichever one justified the
seed match — per this module's own "always include" recommendation, no
query-intent parsing. 4 new unit tests added (Officer-by-name surfaces
belt_no; Person-by-name surfaces cnic+phone; match-only node renders
unchanged; belt_no-seeded Officer doesn't duplicate it). Full backend
suite green.

**Live-verified** against the real running Postgres/AGE instance, same
`fir-401-26`/ذیشان repro: a direct `retrieve_graph()` call now returns
```
"ذیشان appears in fir_structured record fir-401-26 (psrms/fir/fir-401-26#structured), with belt number GEN-0301, and phone number 0306-4000006 recorded there."
```
— GEN-0301 now present, versus the pre-fix text quoted below.

**HTTP-level `/api/chat` verification — initially blocked, then fixed and
completed (2026-08-24, same day).** First attempt hit a pre-existing,
unrelated infra constraint: `route_query()`'s LLM call (fixed
`prompts/router.txt` system prompt, always used once a `case_id` is on the
request — see line 231-232) requested 8,451 tokens against this Groq
account's `on_demand`-tier cap of 8,000 TPM, returning a 413 before
`retrieve_graph()` was ever reached. Reproduced identically twice
(byte-identical 8451/8000), which looked structural but turned out not to
be — root cause was narrower and fixable without touching `router.txt`'s
content at all: this pipeline is local-first by design (Qwen3-14B tried
before any cloud call), and the local model itself classifies fine in
isolation, but the LIVE orchestrated request was exhausting all 3 local
attempts and escalating to the Groq cloud fallback, which then
**guaranteed-failed** for an unrelated reason — `call_llm_json`'s cloud
escalation inherited the same 800-token `max_tokens` set for the LOCAL
branch (sized for Qwen3-14B's hidden thinking trace), and Groq counts
`max_tokens` toward its TPM accounting, pushing every cloud attempt over
the cap regardless of query. Fixed narrowly (`fix/router-cloud-fallback-reasoning-effort`,
merged to `main`): router.py now passes `cloud_max_tokens=300` (the
existing, purpose-built knob for exactly this — no other call site had
ever needed a small cloud budget before) plus a new `reasoning_effort`
parameter threaded through `call_llm`/`call_llm_json`/`_call_groq`, set to
`"low"` for the router's cloud call — `config.GROQ_MODEL`
(openai/gpt-oss-120b) is itself a reasoning model and was silently
burning its entire 300-token budget on a hidden reasoning trace before
`cloud_max_tokens` alone was tried (confirmed live: came back empty).
With `reasoning_effort="low"`, a real router call's reasoning dropped to
82 tokens, completion 151 tokens total, and the whole request landed at
7,802 tokens — under the cap, correct classification returned
(`GRAPH`, `target_entity: "ذیشان"`, matching this module's own original
live trace exactly). 2 new unit tests added; full backend suite green.

**Confirmed live end-to-end over real HTTP** after the fix: router
succeeded (`Route decided: GRAPH`), graph retrieval ran, and the
evaluator step explicitly confirmed *"Document [2] explicitly states
Officer ذیشان's belt number"* — direct proof this module's actual fix
(the notable-properties clause) is reaching the LLM correctly through the
full real pipeline, not just in isolated `retrieve_graph()` calls. Module
2 itself is now fully, completely verified end-to-end.

**New, separate finding surfaced by this fix (not part of Module 2, not
yet triaged as its own module):** despite the evaluator confirming the
belt number is present in evidence, the response-generation step still
produced a wrong number (`3456` — matching neither `GEN-0301` nor the
officer's phone), which `citation_validator` correctly caught as
ungrounded and triggered a regeneration; the regeneration also failed to
produce a grounded answer, so the final response was still the generic
refusal. This is downstream of Module 2 entirely — evidence now correctly
contains the fact, but generation still hallucinated instead of reading
it — and is a distinct gap (response-generation grounding vs.
evidence-availability) worth its own investigation if picked up.

### Problem (as originally found — kept for context)
*"What is Officer ذیشان's belt number in this case?"* (case `fir-401-26`,
real ground truth: belt `GEN-0301`) → router correctly finds the officer
(`target_entity: "ذیشان"`), `retrieve_graph()` correctly seeds
`OFFICER-31926c31fa`, but the final answer is the generic refusal ("cannot
provide a confident answer"). Live-traced the raw evidence:
```
"ذیشان appears in fir_structured record fir-401-26 (psrms/fir/fir-401-26#structured)."
```
The belt number is never mentioned anywhere in the cited text — so the LLM
correctly (from what it can see) can't confirm it.

### Root cause
This is the direct sibling of the bug just fixed in
`feature/graph-evidence-matched-identifier` (merged this session,
`_matched_seed_property()`/`_MATCHED_PROPERTY_LABELS` in
`src/retrieval/graph_retriever.py`). That fix surfaces **the property that
justified the seed match** (e.g. "whose phone number is X"). It
deliberately does **not** surface any *other* property the node happens to
carry — here the seed matched via `canonical_name` (the officer's name was
given), so no clause is added by design (a name-match needs no clause,
per that fix's own reasoning) — but the belt number, which the user is
actually asking about, was never the match key, so it's not in scope of
that fix at all. `_synthetic_evidence_chunk()`'s template is fundamentally
minimal: name + doc reference, nothing else about the node.

### Proposed approach
Extend `_synthetic_evidence_chunk(row, matched_property=...)` (already
threaded through from Module 2's predecessor fix — reuse the same call
site) to also render a SMALL, fixed set of "notable identifying
properties" the seed node carries, beyond just the matched one — e.g. for
an `Officer` node: `belt_no` if present; for a `Person`: `cnic`/`phone` if
present (skipping whichever one was already the matched property, to avoid
"whose phone number is X, whose phone number is X"). Concretely:
```python
_NOTABLE_PROPERTIES: dict[str, tuple[str, ...]] = {
    "Officer": ("belt_no", "phone"),
    "Person": ("cnic", "phone"),
    "Vehicle": ("plate",),
}
```
then in `_synthetic_evidence_chunk()`, after the existing `match_clause`,
append a second clause listing any of `_NOTABLE_PROPERTIES[label]` that are
present on the node and weren't already the matched property — e.g.
`"ذیشان appears in fir_structured record fir-401-26, with belt number
GEN-0301 recorded there."` This needs the node's own `label` and full
`properties` dict, both already available on `row["n"]` — no new plumbing
required beyond what Module 2's predecessor already built.

**Open question to resolve before implementing:** should this apply to
EVERY synthetic chunk for a seed entity (verbose, but always complete), or
only be added when the query text itself asks about that property (e.g.
only mention belt_no if the query contains "belt") — mirroring how
`_matched_seed_property()` only fires for what was actually searched by?
Recommend the simpler "always include notable properties" version first —
it costs a little more text per chunk but removes an entire class of "the
answer exists in the graph but never in the citable text" failures
without needing query-intent parsing.

### Files likely touched
- `src/retrieval/graph_retriever.py` — `_synthetic_evidence_chunk()`, new
  `_NOTABLE_PROPERTIES` table alongside `_MATCHED_PROPERTY_LABELS`.
- `tests/test_graph_retriever.py` — new tests: an Officer seeded by name
  gets belt_no in its text; a Person seeded by name gets CNIC/phone if
  present; a node with no notable properties beyond the match renders
  unchanged (no empty/dangling clause).

### Test plan
- Unit tests as above.
- Live verification: re-run this session's own repro —
  `"What is Officer ذیشان's belt number in this case?"` scoped to
  `fir-401-26` — must now return a confident answer naming `GEN-0301`,
  through the real HTTP endpoint (remember to restart the backend first).
- Full backend suite green.

---

## Module 3: Enumeration / list-synthesis refusal

### ✅ RESOLVED — 2026-08-25
Fixed on `main` (merged from `fix/graph-answer-enumeration-refusal`).
Traced one layer deeper than this module's own "needs more investigation"
note asked for: called `retrieve_graph()` → `cross_rerank()` →
response-generation prompt directly, printed the raw LLM output **before**
`verify_grounding()` ran on it. **Neither of the two original hypotheses
was quite right** — the real chain was three separate, compounding bugs,
none of them in `citation_validator`/`verifier.py` (which was correctly
rejecting bad output the whole time):

- **Bug A (root cause):** GRAPH/GRAPH_HYBRID's response-generation step
  built `system_prompt = _FINAL_PROMPT_TEMPLATE.format(documents=...,
  project_memory=..., history=..., user_context=..., ...)` — but
  `prompts/final_response.txt` had already been refactored (for the RAG
  route's own privacy-refusal fix, see that route's code comment) to carry
  documents/memory/context/history in the **user** turn instead, and no
  longer has placeholders for any of those kwargs. Python's `.format()`
  silently drops unmatched kwargs, so this compiled and ran with no error
  — but `documents_text` never landed anywhere, and the bare, unmodified
  question (no evidence at all) was sent as the user turn. Confirmed live:
  raw LLM output was a full hallucination ("Ahmad Khan", "Sara Bibi",
  "Mohammad Ali" — none real), which the Verifier correctly rejected.
  **Fix:** added `_build_grounded_user_message()` (factored from RAG's
  already-working construction) and used it in both GRAPH and
  GRAPH_HYBRID, matching RAG's contract exactly.
- **Bug B (compounding):** once Bug A was fixed, `config.TOP_K_RERANK`'s
  shared default of 5 cut GRAPH's 12 raw chunks down to 5 **before**
  they reached the prompt — and confirmed live, the 6 byte-identical
  `"(نامزد ASI) appears in fir_structured record fir-233-26..."`
  duplicate-officer chunks (this module's hypothesis 2, and the same
  7-distinct-`Officer`-nodes-for-one-placeholder oddity flagged in this
  session's ground-truth pull) crowded 4 of the 5 real people out of the
  reranked set entirely. **Fix:** added `_dedupe_chunks_by_text()`
  (collapses byte-identical chunk text, applied before the rerank cut)
  and a larger, case-scoped-only rerank budget,
  `_GRAPH_ANSWER_RERANK_TOP_K = 20` (GRAPH/GRAPH_HYBRID's `cross_rerank()`
  calls only — not `config.TOP_K_RERANK`'s global default, not RAG, not
  XGRAPH). `structured_projection.py`/entity resolution itself was
  deliberately **not** touched — the 7-nodes-for-one-placeholder shape is
  still there; this fix only stops its duplicate chunk text from wasting
  the rerank budget.
- **Bug C (found during live verification, expanded scope with
  sign-off):** with A+B fixed, `evaluate_relevance()` (a separate,
  earlier LLM gate, `src/pipeline/evaluator.py`/`prompts/evaluator.txt` —
  never mentioned in this module's original hypotheses) rejected the now-
  complete 5-real-name evidence set as "insufficient" most of the time,
  reasoning that a case-status sentence's generic, unnamed role mentions
  ("متاثرہ بچہ"/affected-child, "دونوں ملزمان"/the-two-accused) meant the
  evidence was incomplete — even though those role-terms describe people
  already named elsewhere in the same evidence. Same class of bug as
  hypothesis 1, one gate earlier than expected. **Fix:** added an
  "enumeration / list every X" rule + worked example to
  `prompts/evaluator.txt`. Also bumped `call_llm()`'s `max_tokens` for
  GRAPH/GRAPH_HYBRID generation from the shared default (1000) to
  `_GRAPH_ANSWER_MAX_TOKENS = 2000` — confirmed live, a genuinely correct,
  complete 5-6-item answer sometimes truncated mid-sentence under the
  1000-token default (non-deterministic; same prompt completed cleanly on
  other attempts).

2 new unit tests added (`test_graph_route_enumeration_evidence_reaches_the_llm`,
`test_dedupe_chunks_by_text_collapses_exact_duplicates`). Full backend
suite green.

**Live-verified end-to-end over real HTTP** (`admin@example.com`,
freshly restarted backend, real Postgres/AGE, real `fir-233-26`): GRAPH
route → evaluator `relevant: True` → `citation_validator` **grounded**
("All claims about individuals, their details, and document citations are
directly supported by the corresponding chunks.") → final answer names
all 5 real people (حمزہ طارق, محمد اسلم, ذیشان بٹ, طارق محمود, شعیب
ارشد), each with its CNIC/phone and `[Document N]` citation, plus an
honestly-labeled placeholder entry for the redacted investigating officer
— not folded in as a 6th real name.

### Problem (as originally found — kept for context)
*"List every person mentioned in this case file."* (case `fir-233-26`,
real ground truth: exactly 5 people — طارق محمود, شعیب ارشد, ذیشان بٹ,
محمد اسلم, حمزہ طارق). Live-traced `retrieve_graph()` directly: it
correctly returns **11 seed entities, 12 chunks**, including all 5 real
people by name, cleanly. But the real HTTP answer was still the generic
"I cannot provide a confident answer... the cited sources do not
sufficiently support a specific claim." The retrieval is complete and
correct; something after it — response generation or `citation_validator`
— fails to synthesize a list from many small, independent evidence chunks.

### Root cause (as originally scoped — needs more investigation before a fix can be scoped)
Unlike Modules 1/2/4/5, this one was **traced to "retrieval is fine, the
failure is downstream" but not yet traced further** — the next step is to
add the same kind of direct-call tracing this session used elsewhere, but
one layer deeper: call the actual response-generation prompt (wherever
GRAPH/GRAPH_HYBRID's "Generating graph-grounded response..." step in
`src/pipeline/orchestrator.py` builds its prompt from `retrieve_graph()`'s
chunks) with this exact evidence set and see what the LLM actually
produces and why `citation_validator` rejects it. Two live hypotheses,
not yet confirmed:
1. The response-generation prompt (or its few-shot examples) is tuned for
   "does this evidence support THIS ONE claim" reasoning, and doesn't
   handle "synthesize a list from N independent single-fact rows" well.
2. The 6 near-identical duplicate `"(نامزد ASI) appears in fir_structured
   record fir-233-26..."` chunks (a real, separate oddity found in this
   session's ground-truth pull — the same placeholder officer name written
   as 7 distinct `Officer` nodes for one case, each producing its own
   near-duplicate chunk) crowd out or confuse the prompt. If this turns
   out to be a real contributing factor, it may deserve its own
   investigation into why `structured_projection.py`/entity resolution
   isn't deduplicating repeated placeholder-named officer mentions within
   one case — noted here for visibility, not scoped as its own module
   since it wasn't part of the original 5 reported findings.

### Suggested first step (before design)
Find the exact prompt template used for GRAPH/GRAPH_HYBRID response
generation (likely `prompts/graph_answer.txt` or similar — confirm the
exact file via `grep -rn "Generating graph-grounded response" src/pipeline/orchestrator.py`
and follow the `call_llm`/`call_llm_json` call immediately after it back to
its system prompt), and reproduce this exact failure with a direct call
(same pattern as this session's `trace_*.py` scripts) — print the raw LLM
output BEFORE `citation_validator` runs, to see whether the LLM itself
already refuses, or whether it produces a good list and
`citation_validator` is the one rejecting it. That determines whether the
fix belongs in the response-generation prompt or in
`citation_validator`'s own claim-matching logic.

### Files touched
- `src/pipeline/orchestrator.py` — `_build_grounded_user_message()`,
  `_dedupe_chunks_by_text()`, `_GRAPH_ANSWER_RERANK_TOP_K`,
  `_GRAPH_ANSWER_MAX_TOKENS`; GRAPH and GRAPH_HYBRID branches updated to
  use all four.
- `prompts/evaluator.txt` — new enumeration/"list every X" rule + example.
- `tests/test_orchestrator.py` — 2 new tests (see above).
- `src/pipeline/verifier.py`/`citation_validator` — **not touched**; it
  was already correctly rejecting the hallucinated/incomplete answers
  Bugs A/B produced.
- `src/graph/structured_projection.py` — **not touched** (out of scope,
  per this module's own note above); the 7-nodes-for-one-placeholder
  shape is unchanged.

### Test plan (completed)
- Unit test feeding a `retrieve_graph()`-shaped multi-chunk enumeration
  result (5 real single-fact chunks + 6 duplicate placeholder chunks,
  fir-233-26's real shape) through the GRAPH route, asserting the actual
  evidence — not the bare question — reaches `call_llm()`'s user turn,
  and that all 5 real names survive the rerank cut.
- Unit test for `_dedupe_chunks_by_text()` directly: 6 duplicates → 1.
- Live verification: re-ran this session's own `fir-233-26` "list every
  person" repro through the real HTTP endpoint (backend freshly
  restarted) — final answer named all 5 real people. See the RESOLVED
  block above for the exact result.
- Full backend suite green.

---

## Module 4: XAGG entity-type coverage gap (weapon aggregation)

### ✅ RESOLVED — 2026-08-25
Fixed on `main` (merged from `feature/xagg-weapon-recurrence`). All three
stacked gaps below re-confirmed against current code before fixing (line
numbers had drifted since Modules 1-3 landed) — none of the three root
causes needed revision, only re-verification:

- **Router**: added `weapons?|firearms?|pistols?|ہتھیار` to all three
  `_XAGG_OVERRIDE_PATTERNS` entity-keyword groups (`src/pipeline/router.py`)
  that already covered vehicles/persons, matching their existing
  narrowness.
- **Dispatch**: added a `"Weapon"` branch to `run_aggregate()`'s
  `graph_recurrence` dispatch (`src/pipeline/xagg.py`).
- **Aggregation shape**: added a genuinely new function,
  `_top_recurring_weapon_types()`, grouping by a normalized weapon-type
  string and counting distinct cases per group — deliberately **not**
  routed through `_top_recurring_nodes("Weapon", ...)`, which (confirmed
  again by re-reading `structured_projection._write_weapons()`) can never
  return anything for this label. Normalization strips the trailing
  ammunition-count clause (`بمعہ N گولیاں`) via a regex verified against
  real sampled `canonical_name` values pulled directly from the live
  graph before implementing:
  ```
  "30 بور پستول"                 (10 cases, unnormalized)
  "30 بور پستول بمعہ 3 گولیاں"   (11 cases, unnormalized)
  "30 بور پستول بمعہ 6 گولیاں"   (11 cases, unnormalized)
  ```
  all three normalize to `"30 بور پستول"`, spanning **30 distinct cases**
  once merged.

**Tests**: `tests/test_router.py` adds 3 weapon-phrasing cases to the
existing deterministic-override parametrize list; the existing XGRAPH
"across cases"/"other cases"/"repeat offender" cases in the same list
were run explicitly as a regression guard and still pass unchanged.
`tests/test_xagg.py` adds weapon-recurrence tests including one that
proves the ammunition-suffix normalization is load-bearing (asserts the
two raw canonical_name strings are distinct pre-normalization and only
converge after). Full backend suite green: 1589 passed, 0 failed, 5
skipped.

**Live-verified** against the real running Postgres/AGE instance over a
real HTTP `/api/chat` call (backend restarted — found running without
`--reload` again this session, same as the standing note above; restored
to `--reload` afterward). *"Which type of weapon appears most often
across all cases?"* now routes **XAGG** (was **XGRAPH**) and returns:
> 30 بور پستول (Weapon): appears in 30 cases — fir-1001-26, fir-201-26,
> fir-202-26, fir-203-26, fir-204-26, fir-210-26, fir-212-26, fir-213-26,
> fir-214-26, fir-217-26, fir-218-26, fir-301-26, fir-401-26, fir-403-26,
> fir-407-26, fir-408-26, fir-409-26, fir-410-26, fir-413-26, fir-420-26,
> fir-421-26, fir-423-26, fir-430-26, fir-431-26, fir-445-26, fir-458-26,
> fir-465-26, fir-466-26, fir-468-26, fir-891-24

— matching a direct Cypher count against the same live data exactly (30
distinct case_ids).

### Problem (as originally found — kept for context)
*"Which type of weapon appears most often across all cases?"* → router
classifies it as **XGRAPH** (wrong — no entity was named, this is a pure
aggregate/ranking question) and returns the nonsensical *"No connections
to other cases were found for this entity."*

### Root cause (three separate, stacked gaps — all three need addressing,
### and the third is bigger than it first looks)
1. **Router pattern gap**, `src/pipeline/router.py`,
   `_XAGG_OVERRIDE_PATTERNS` (~line 82-91): the "which X appeared in
   multiple/across cases" pattern's entity-keyword group is
   `(persons?|people|suspects?|accused|offenders?|vehicles?|mulzim|shakhs)`
   — **"weapon(s)" is not in it.** `_deterministic_route_override()`
   checks `_XAGG_OVERRIDE_PATTERNS` first, then `_XGRAPH_OVERRIDE_PATTERNS`
   (~line 100) — the latter's `\bacross\b.{0,15}\b(multiple |other )?cases\b`
   pattern has an *optional* multiple/other group, so "across **all**
   cases" still matches it even though the query has no named entity at
   all, which is exactly the shape XGRAPH's override should never fire on
   unaccompanied by a real identifier.
2. **Handler dispatch gap**, `src/pipeline/xagg.py`, `run_aggregate()`
   (~line 273-321): even if routed correctly, its `graph_recurrence`
   dispatch only supports `_top_recurring_nodes("Vehicle", ...)` and
   `("Person", ...)` — no `"Weapon"` branch exists. Fixing only the router
   pattern would route correctly but still fall through to
   `_station_or_category_counts()`, producing an unrelated
   station/category breakdown instead of a weapon ranking.
3. **Structural mismatch — confirmed by reading the writer, not just
   inferred.** `_top_recurring_nodes()` (xagg.py ~line 92-132) counts, per
   `entity_id`, how many *distinct cases* that SAME graph node's own
   `BELONGS_TO_CASE` edges touch — it answers "does this one node recur
   across cases," the same model that correctly works for Person (merged
   cross-case via CNIC through `entity_resolution.resolve_and_write()`)
   and Vehicle. **Weapon nodes are structurally incapable of ever
   recurring under this model.** `structured_projection.py`'s
   `_write_weapons()` builds each Weapon's `entity_id` as
   `f"WEAPON-{w.get('id') or w.get('sr_no')}-{fir.fir_id}"` — the FIR's
   own id is baked directly into the entity_id string, and weapons never
   go through `entity_resolution.resolve_and_write()`'s CNIC-style
   cross-case merge tier at all. **Every real Weapon node, by
   construction, belongs to exactly one case, permanently.**
   `_top_recurring_nodes("Weapon", ...)`'s `len(cases) > 1` filter
   (line 131) would therefore return an **empty list every single time**,
   for any real data — not an undercount from name variance (the
   `"بمعہ N گولیاں"` ammunition-count suffix issue is real too, but it's
   the SECOND problem, not the blocking one).

   This means "which weapon type recurs across cases" is not answerable
   by reusing `_top_recurring_nodes` at all — it needs a genuinely
   different aggregation shape: group by a normalized **weapon-type
   string** (not by node identity) and count **distinct cases** per group,
   e.g. `MATCH (w:Weapon)-[:BELONGS_TO_CASE]->(c:Case) RETURN
   normalize(w.canonical_name) AS weapon_type, count(DISTINCT c) AS n
   ORDER BY n DESC` — closer in shape to `_station_or_category_counts()`
   (an existing grouped-count query) than to `_top_recurring_nodes()`.

### Proposed approach
- Router: extend the XAGG keyword group to include
  `weapons?|firearms?|pistols?|ہتھیار` (Urdu), same style as the existing
  list — narrow, not a general catch-all.
- Handler: **do not** call `_top_recurring_nodes("Weapon", ...)` — per
  root cause #3 above, it cannot work for this label. Instead add a new
  function (e.g. `_top_recurring_weapon_types()`) modeled on
  `_station_or_category_counts()`'s grouped-count shape: fetch
  `(Weapon)-[:BELONGS_TO_CASE]->(Case)` pairs, normalize each weapon's
  `canonical_name` (strip trailing "بمعہ N گولیاں"/ammunition-count
  clauses — a simple regex/string-split, not a new dependency), group by
  the normalized string, count **distinct case_ids** per group, return
  top-N descending.
- This is a genuinely new aggregation function, not a one-line dispatch
  addition — size this module accordingly (Medium-Large, not Medium).

### Files likely touched
- `src/pipeline/router.py` — `_XAGG_OVERRIDE_PATTERNS`.
- `src/pipeline/xagg.py` — new `_WEAPON_KEYWORDS` constant + dispatch
  branch, and the new `_top_recurring_weapon_types()`-style grouped-count
  function (do not route this to `_top_recurring_nodes`, see root cause
  #3 above).
- `tests/test_router.py` — "which weapon appears most often" now routes
  XAGG, not XGRAPH; existing XGRAPH "across cases" tests still pass
  unchanged (regression guard).
- `tests/test_xagg.py` (confirm exact filename) — new weapon-type
  grouped-count test, including a case specifically constructed so two
  cases' weapons only match after ammunition-suffix normalization (must
  fail without it, to prove the normalization step is load-bearing, not
  decorative).

### Test plan
- Router unit tests as above.
- XAGG handler unit test: two different cases each with a `"30 بور
  پستول"`-shaped weapon (different ammunition-count suffixes) → counted
  as ONE recurring weapon type across 2 cases, not two separate
  single-case weapons. A weapon type appearing in only one case is
  correctly excluded (mirrors `_top_recurring_nodes`'s existing
  `len(cases) > 1` recurrence bar).
- Live verification: re-run this session's own repro — "Which type of
  weapon appears most often across all cases?" — must route XAGG and
  return an actual ranked answer (real ground truth: the 30-bore pistol
  pattern recurs across at least 10 sampled real cases).
- Full backend suite green.

---

## Module 5: SQL extractor phrasing brittleness

### ✅ RESOLVED — 2026-08-25
Fixed on `main` (merged from `fix/sql-extractor-progressive-relaxation`),
implementing Option A exactly as scoped below:

- `src/data_gateway/direct_backend.py`'s `query_police_reference_data()`
  now extracts the pre-existing exact-match query verbatim into
  `_query_police_reference_data_exact()`, and wraps it in a bounded
  progressive-relaxation loop: on a 0-row full-AND result, retry with
  `subject` dropped (weakest, most free-text signal), then `category`
  too if still empty — `section_ref` is never dropped. Never relaxes
  below a single remaining filter. At most 2 extra queries. A query
  whose full filter set already matches returns from the very first
  call, never entering the loop — the public method's signature and the
  `DataGateway` protocol are unchanged, so `orchestrator.py`'s SQL route
  call site needed no edit.

**Live-verified** against the real Postgres `police_reference_data`
table (direct `DirectGateway` calls, bypassing LLM sampling variance):
- Already-working case (`category='penal_code', subject='Theft'`): the
  relaxed wrapper and the raw exact-match call returned **identical** 5
  rows (379/380/411/457, with 380 appearing twice) — proof relaxation
  never engaged, byte-for-byte unchanged from before this fix.
- Simulated over-specific extraction (`category='penal_code',
  subject='Theft of Movable Property (Unlawful Taking)',
  section_ref='379'`): full 3-filter AND confirmed 0 rows (reproducing
  the bug); the relaxed wrapper found PPC 379 after dropping `subject`
  then `category`.
- Real HTTP round trip via `/api/chat`, both this session's exact
  phrasings: the previously-failing *"What PPC section covers theft of
  movable property and is it cognizable?"* now returns a grounded answer
  citing Section 379 PPC as cognizable (retrieval found 21 rows, up from
  0/fallback-to-RAG before the fix).
- PPC 302 (confirmed absent from the table): stayed empty at every
  relaxation level, including `section_ref='302'` alone — no fabrication.

**Tests**: new `tests/test_sql_extractor_relaxation.py` (9 cases) covers
the drop order, the single-filter/no-filter short circuits (no wasted
queries), the bounded 2-extra-query cap, and a regression guard proving
an already-matching full filter set issues exactly one query. Full
backend suite green.

### Problem (as originally found)
Same underlying fact — PPC 379 (theft of movable property) is present in
`police_reference_data`, confirmed live via direct SQL — gets two
different outcomes depending only on phrasing:
- *"What section of the PPC covers theft and what is its punishment?"*
  (earlier this session) → **succeeded**, correctly named sections 379,
  380, 411, 457.
- *"What PPC section covers theft of movable property and is it
  cognizable?"* (this sweep) → **failed**: "No structured match. Falling
  back to RAG" → RAG also fails → generic "couldn't find sufficient
  information."

### Root cause
`src/pipeline/sql_extractor.py`'s `extract_sql_params()` LLM-extracts
`category`/`subject`/`section_ref`/`date` from the query text (prompt:
`prompts/sql_param_extractor.txt`). `src/data_gateway/direct_backend.py`'s
`query_police_reference_data()` (~line 780) then **ANDs every non-null
extracted filter together** with `ILIKE '%value%'`:
```python
if category:    conditions.append(PoliceReferenceData.category.ilike(f"%{category}%"))
if subject:      conditions.append(PoliceReferenceData.subject.ilike(f"%{subject}%"))
if section_ref:  conditions.append(PoliceReferenceData.section_ref.ilike(f"%{section_ref}%"))
...
stmt = select(PoliceReferenceData).where(*conditions)   # AND, not OR
```
An **extra or differently-worded extracted filter can only narrow the
result set, never broaden it** — a more verbose, more specific-sounding
question (which the LLM extractor may render as a more specific/different
`subject` string, e.g. "theft of movable property" vs. the DB's actual
stored subject text) is *more* likely to zero-match than a shorter one,
even though both are asking about data that genuinely exists. This is the
opposite of what a natural-language interface should do — asking a more
precise question should not make it more likely to fail.

### Design options (pick one before implementing)
- **Option A — progressive relaxation.** If the full-AND query returns 0
  rows, retry with one fewer filter (drop `subject` first, since it's the
  most free-text/variable field; keep `section_ref`/`category` as the
  stronger signals), down to single-filter, before giving up. Simple,
  no prompt changes, bounded number of extra queries (at most 2 retries).
- **Option B — switch AND to OR-ranked.** Query with OR across all
  provided filters, rank results by how many filters matched (more
  matches = higher relevance), return top-N. Bigger change to the
  gateway method's contract (callers currently expect a small, precise
  result set) — needs checking every caller of
  `query_police_reference_data()` before doing this.
- **Option C — tighten prompt extraction instead.** Rework
  `prompts/sql_param_extractor.txt` to extract ONLY `section_ref` when a
  specific section number is named, and prefer the single strongest
  signal (never emit both `category` AND `subject` unless the query
  names both explicitly) — fixes it at the source rather than adding
  query-time relaxation logic, but is more sensitive to the LLM's own
  extraction judgment being consistent, which this exact bug shows it
  currently isn't.

Recommend **Option A** as the least invasive first pass — it doesn't touch
the prompt (no re-verification of prompt/schema drift needed) or change
the gateway method's return contract, just adds a bounded retry loop
around the existing exact-match query.

### Files likely touched
- `src/data_gateway/direct_backend.py` — `query_police_reference_data()`
  (or a new wrapper the SQL route calls instead), implementing the chosen
  relaxation strategy.
- `src/pipeline/orchestrator.py` — the SQL route's retrieval branch (~line
  677 per this session's earlier read), if the relaxation lives at the
  call site rather than inside the gateway method.
- `tests/test_kb_stats_documents.py`-style new test file, or extend
  whichever test file already covers `query_police_reference_data()` —
  confirm exact location first (`grep -rn "query_police_reference_data" tests/`).

### Test plan
- Unit: a query whose full filter set matches nothing but whose
  `section_ref` alone matches a real row → relaxation finds it.
  A query with no matching row at any relaxation level → still correctly
  returns empty (must not fabricate a match).
- Live verification: re-run both this session's real phrasings side by
  side — the previously-failing "theft of movable property... cognizable"
  phrasing must now also find PPC 379/380/411/457, matching the
  already-working phrasing's result. Also re-confirm PPC 302 (genuinely
  absent from the table) still correctly reports "not found" — this fix
  must never fabricate data that isn't there.
- Full backend suite green.

---

## Module 6: Community detection never refreshes for real sync data

### ✅ RESOLVED — 2026-08-25
Fixed on `main` (merged from `feature/community-refresh-real-sync`),
implementing the proposed approach exactly as scoped below:

- `src/ingestion/community_refresh_bg.py` now exposes
  `refresh_if_stale()` — the awaitable "check `get_staleness()`, then
  `detect_communities()`+`summarize_communities()` if stale" core,
  extracted verbatim out of `_run_community_refresh_bg()`.
  `_run_community_refresh_bg()` itself became a thin fire-and-forget
  wrapper (`try: await refresh_if_stale() / except: log-and-swallow`) —
  its signature, its one caller (`src/ingestion/service.py:634`, still
  `asyncio.create_task(...)`), and its observable behavior are all
  unchanged; the 3 pre-existing tests exercising it pass unmodified as a
  regression guard on the extraction itself.
- `scripts/sync_muhafiz_data.py`'s `_run_sync()` now `await
  refresh_if_stale()` directly — once, after every FIR/CMS/PKM/criminal-
  records/roznamcha loop and the cross-version/citation steps, gated
  `if not dry_run`, before `close_pool()` — with its own
  `"-- Community detection refresh --"` status line reporting the
  staleness reason and whether a recompute actually ran.

**Live-verified** against the real running Postgres/AGE instance, Docker
and backend confirmed healthy first (`docker ps` → `muhafiz-postgres`
healthy; `curl localhost:8001/health` → `database_status: ok`):
- **Before**: `community_runs` held exactly one row,
  `RUN-20260822104011`, `computed_at` 2026-08-22 10:40:13, `raw_node_count`
  444 (unchanged since the original repro — nothing had refreshed it).
- A freshly-issued live Cypher count (`MATCH (p:Person) RETURN count(p)`,
  run independently of `get_staleness()`) returned **608** — the real
  current count at verification time, distinct from every previously
  cited figure (444 from the original repro, 478 from this doc's own
  now-stale intro, and 348 from an unrelated Module 1 report referring to
  an even earlier point in time). Absolute counts drift session to
  session; only this module's relative claim — a new row with a newer
  `computed_at` and a `raw_node_count` matching the graph's real count at
  that moment — was ever the thing being verified.
- Ran `python scripts/sync_muhafiz_data.py --full --snapshot
  tests/fixtures/muhafiz_api_snapshot.json` (the real 73-FIR/74-roznamcha/
  4-CMS/14-PKM/33-criminal-record corpus, non-dry-run) — never touching
  `POST /api/admin/community/refresh`. Script's own status line: `stale
  (node drift 36.9%, edge drift 310.9%) — recomputed: 19 attempted, 19
  written, 0 skipped`.
- **After**: a new row appeared, `RUN-20260825074016`, `computed_at`
  2026-08-25 07:40:16 (newer than the prior run), `raw_node_count` 608 —
  an exact match to the independently re-queried live Person count taken
  immediately after the sync, `node_count` 292, `community_count` 19.

**Tests**: `tests/test_community_staleness.py` gained 3 new cases against
`refresh_if_stale()` directly (skip-when-not-stale, run-detect-and-
summarize-when-stale, and — the inverse of the wrapper's own test —
propagates failures rather than swallowing them, proving best-effort now
lives only in the wrapper). `tests/test_sync_muhafiz_data_script.py`
gained a `TestRunSyncCommunityRefresh` class asserting `_run_sync(...,
dry_run=False)` calls the refresh exactly once, after every sync step and
before `close_pool()`, and that `dry_run=True` never calls it (and never
calls `close_pool()` either). Full backend suite green.

### Problem (as originally found)
Found while verifying a stakeholder question about whether GraphRAG-style
"community detection" (Microsoft GraphRAG terminology — clustering entities
into communities + LLM-summarizing each one, feeding XNETWORK's cross-case
pattern queries) is being kept current. Checked the actual data, not just
whether the code exists:
```sql
SELECT run_id, computed_at, node_count, edge_count, community_count,
       raw_node_count, raw_edge_count
FROM community_runs ORDER BY computed_at DESC LIMIT 5;
--  RUN-20260822104011 | 2026-08-22 10:40:13 | 60 | 75 | 18 | 444 | 221
--  (exactly one row)
```
There is exactly **one** community-detection run on record, from **Aug 22**,
covering 444 raw Person nodes. The live graph now has **478** Person nodes
(confirmed live, same session). Every real case synced since Aug 22 has
never been through clustering or summarization at all — any XNETWORK query
touching a newer case's people is working off a stale, partial partition
without any indication to the user that it's incomplete.

### Root cause
`src/graph/community_detection.py`/`community_summarization.py` themselves
are correctly implemented (Louvain clustering, canonicalization through
confirmed SAME_AS, a documented shared-case-projection fallback for sparse
`ASSOCIATED_WITH` — see Module 1, this is the same underlying sparsity
problem, independently rediscovered and mitigated by this module's own
author). The gap is entirely in **when refresh gets triggered**:
```
grep -rn "community_refresh_bg\|_run_community_refresh_bg" src/ scripts/
```
shows `_run_community_refresh_bg()` (`src/ingestion/community_refresh_bg.py`)
is called from exactly one place: `src/ingestion/service.py` (the legacy
single-document admin-upload path, `asyncio.create_task(...)` fired after
each upload's own graph extraction). **It is never referenced anywhere in
`scripts/sync_muhafiz_data.py`** — the actual script that writes the real
73-case corpus's Person/Officer/Weapon/etc. nodes into the graph. (Confirmed
`scripts/sync_muhafiz_cases.py` is irrelevant here — it only provisions
Postgres `cases` rows, never touches the AGE graph at all.) The only way to
refresh the real corpus's communities today is the manual supervisor
endpoint, `POST /api/admin/community/refresh` — nothing runs automatically
after a real sync.

### A second, subtler issue to design around: fire-and-forget doesn't fit a CLI script
`_run_community_refresh_bg()`'s own docstring is explicit that its
fire-and-forget (`asyncio.create_task`, never awaited) shape is deliberate
for its one current caller — a live HTTP request handler where "a failure
here must never fail the ingestion job it rides alongside." **That same
shape would be actively wrong if copied as-is into `sync_muhafiz_data.py`**:
`_run_sync()`'s last lines are
```python
if not dry_run:
    await age_client.close_pool()
```
— if community refresh were fired with `asyncio.create_task()` and never
awaited, the script would tear down the connection pool (and likely exit
entirely, since `main()` returns right after `run()`) before the
background task had any real chance to finish, silently dropping the very
refresh this fix is meant to add. A CLI sync script is a one-shot process,
not a long-lived server — it should **await** the refresh directly and
report whether it happened, not background it.

### Proposed approach
Extract the actual "check staleness, then run detect_communities() +
summarize_communities()" body out of `_run_community_refresh_bg()` into a
plain, directly-awaitable function (e.g.
`community_detection.refresh_if_stale()`, or keep it in
`community_refresh_bg.py` as a non-underscore-prefixed function) — have
the existing `_run_community_refresh_bg()` become a thin wrapper that just
calls it inside its existing try/except-log-and-swallow block (preserving
its current behavior and callers exactly). Then call the extracted
function directly, awaited, from `sync_muhafiz_data.py`'s `_run_sync()`,
once, near the end — after the FIR/CMS/PKM/criminal-records/roznamcha loops
and the cross-version/citation steps, but **before** `close_pool()` — gated
`if not dry_run` (same as everything else in that function: dry-run writes
nothing, so there is nothing new to cluster), with its own printed
status line matching the script's existing per-section reporting style
(`"\n-- Community detection refresh --"` / print the staleness reason and
whether a recompute actually ran).

### Files likely touched
- `src/ingestion/community_refresh_bg.py` — extract the awaitable core out
  of `_run_community_refresh_bg()`.
- `scripts/sync_muhafiz_data.py` — `_run_sync()`, new awaited call near the
  end, gated on `not dry_run`, with its own print/status line.
- `tests/test_sync_muhafiz_data_script.py` — new test asserting the refresh
  function is called exactly once per `--full` run (not once per FIR), and
  NOT called at all for `--dry-run`.
- `tests/test_community_detection.py` (or wherever
  `community_refresh_bg.py`'s existing behavior is tested — confirm exact
  filename) — new test that the extracted core function behaves identically
  to what `_run_community_refresh_bg()` used to do inline (regression
  guard against the refactor changing behavior, not just where it's called
  from).

### Test plan
- Unit: `sync_muhafiz_data.py --full` (mocked/fixture data) calls the
  refresh function exactly once, after all sync steps, never mid-loop.
  `--dry-run` never calls it at all.
- Unit: the extracted refresh core still correctly skips recompute when
  `get_staleness()` says not stale, and still runs
  `detect_communities()`/`summarize_communities()` when it does — same
  behavior `_run_community_refresh_bg()` already has today, just callable
  directly.
- Live verification: run a real (or `--snapshot`-based, non-dry-run) sync,
  then re-query `community_runs` — a new row with a `computed_at` newer
  than Aug 22 and a `raw_node_count` reflecting the current real Person
  count (478 or whatever it's grown to by then) must appear, without
  needing to hit the manual `/api/admin/community/refresh` endpoint at all.
- Full backend suite green.

---

## Module 7: No general adaptive multi-method retrieval

### ✅ RESOLVED — 2026-08-25
Fixed on `main` (merged from
`feature/module7-adaptive-multi-method-retrieval`), implementing Option A
(general adaptive combiner) after a live mini-sweep — the "Suggested first
step" below — was run before picking a direction.

**Mini-sweep first (per the module's own instructions):** 6 real,
ground-truth-driven compound questions were run against the live corpus
before any code was written. Only 1/6 (a GRAPH+XGRAPH officer/repeat-case
shape) was a clean hit of this module's exact gap; the other failures
traced to three separate, unrelated issues (a missing Arms Ordinance
category in `police_reference_data`, an apparent SQL-verifier bug, and an
XGRAPH wrong-entity-resolution bug). Reported to the user with all three
options (A/B/C) restated against this evidence, plus an explicit
"don't build yet" option — the user's call, given the small/mixed sample,
was to build Option A anyway: the measured low hit-rate today is a
snapshot of the current corpus, not a ceiling, and the fusion plumbing is
cheap relative to what it unblocks as the graph/corpus grow and compound-
need questions become more common.

**What was built:**
- `src/pipeline/router.py` / `prompts/router.txt`: `route_query()` gained
  an optional `secondary_methods` field (subset of `SQL`/`GRAPH`/`XGRAPH`/
  `XAGG`, capped at 2, self-reference and unrecognized values dropped) the
  LLM router can set alongside its primary route for a genuinely compound
  question. Only ever honored downstream for a within-case primary route
  (SQL/GRAPH/GRAPH_HYBRID) — XGRAPH/XAGG/XNETWORK's existing structurally-
  separate, never-blended cross-case contract is completely untouched.
  Also fixed a real gap found while building this: the SQL deterministic
  regex fast-path (bypasses the LLM entirely for reliability on
  unambiguous single-intent SQL lookups) was swallowing the SQL HALF of a
  compound question before the LLM router — and `secondary_methods` — ever
  got a chance to run. A narrow exception (`_sql_override_has_compound_signal`)
  now skips the override only when the query also names a case-specific
  "this X" via an explicit conjunction, leaving the override's fast path
  for ordinary single-intent SQL queries completely unaffected.
- `src/pipeline/orchestrator.py`: new `_fetch_secondary_evidence()` helper
  fetches whichever additional methods were flagged and returns pseudo-
  chunks in the same shape every route already builds. Wired into SQL/
  GRAPH/GRAPH_HYBRID, reusing GRAPH_HYBRID's existing fuse-then-cite
  machinery (`_format_documents_for_prompt` + `verify_grounding`) rather
  than building new fusion logic — confirming this module's own root-cause
  correction that the hard part (proven 3-way fusion) already existed.
  Critically, the fetch runs **before** the relevance evaluator, not just
  before generation (a live-caught bug during verification — see below),
  and `verify_grounding()`'s pre-existing `cross_case_ids` parameter (built
  for XGRAPH) is reused unchanged to keep a legitimately-cited cross-case
  supplemental chunk from tripping the leakage check.

**Live-verified**, Docker/backend confirmed healthy, backend restarted
(no `--reload`) after every code change — the same 6 mini-sweep questions
re-run against the real `/api/chat` endpoint:
- **1/6 (the GRAPH_HYBRID+XGRAPH repeat-offender question) now gets a
  full, correct compound answer** where it previously abstained
  completely: the router set `secondary_methods: ["XGRAPH"]`, the fetch
  found 25 cross-case chunks, the evaluator accepted the merged evidence,
  generation correctly hedged the cross-case citations, the verifier
  passed cleanly, and the final answer covered both halves — a full case
  summary AND an honest, evidence-grounded "not a repeat offender"
  conclusion that correctly distinguishes the accused from the *other*
  people who do recur in other cases (matching live ground truth).
- **3/6 (the GRAPH+SQL/XAGG questions) show the wiring working but
  blocked by a separate limiting factor**: the secondary fetch correctly
  ran and found evidence every time, but the evaluator still judged the
  combined evidence insufficient — traced to the primary GRAPH retrieval
  itself being thin (the graph schema has no dedicated "stolen item"
  entity type for these cases), a retrieval-coverage gap, not a fusion
  defect this module's scope covers.
- **1/6 surfaced a genuine pre-existing, unrelated bug**: the secondary
  XGRAPH fetch (seeded with `target_entity: null`) found 0 items, but
  `retrieve_graph()`'s own PRIMARY within-case traversal leaked a chunk
  from a different case anyway — correctly caught by the verifier's
  leakage check (working as designed), but revealing `retrieve_graph()`
  can cross case boundaries even with `cross_case=False`. Filed as its own
  follow-up, out of this module's scope.
- **1/6 remains confounded** by the missing Arms Ordinance reference-data
  category found during the mini-sweep — independent of routing.

**Two bugs were caught and fixed live during this verification pass**,
both real integration gaps, not present in the design as originally
planned:
1. The secondary-evidence fetch originally ran after the relevance
   evaluator — so a compound question whose primary-only evidence looked
   incomplete to the evaluator (exactly the case that most needs the
   second method) fell back to RAG before the fetch ever got a chance.
   Moved the fetch before the evaluator in both GRAPH and GRAPH_HYBRID.
2. The merged prompt had no hedging-word instruction for a low-confidence
   supplemental cross-case citation — `verify_grounding()`'s deterministic
   hedging check discarded an otherwise-correct answer that cited one
   without a hedge word. Added `_CROSS_CASE_HEDGING_RULE`, appended to the
   prompt only when a secondary XGRAPH fetch actually contributed
   cross-case evidence.

**Tests**: `tests/test_router.py` gained coverage for `secondary_methods`
parsing (valid/invalid/self-reference/cap-at-2/absent-defaults-to-empty,
parity on the exception-fallback dict, and the deterministic-override
short-circuit) and the SQL-override compound-exception guard (both clause
orders, plus a guard that an ordinary single-intent SQL query is
unaffected). `tests/test_orchestrator.py` gained compound-merge tests
(SQL+GRAPH, GRAPH+SQL, GRAPH_HYBRID+XGRAPH with `cross_case_ids` reaching
the verifier) and explicit regression guards: a GRAPH route with no
`secondary_methods` key fetches nothing extra, and an XGRAPH primary route
ignores a (should-never-happen) `secondary_methods` value entirely,
preserving its structurally-separate contract. Full backend suite green.

#### Follow-up fixes — 2026-08-25 (same day, `fix/module7-followup-graph-leak-and-xgraph-seeding`)
Two of the gaps this module's own live verification surfaced were picked
up as scoped follow-ups (a third — extending the graph schema with a
"stolen/recovered item" entity type — was deliberately left open as its
own, larger, module-sized project; a fourth — sourcing real Arms
Ordinance 1965 reference data — was deliberately skipped, since
fabricating authoritative legal-lookup content isn't something to do
without a real source):

1. **`retrieve_graph()` cross-case leak, root-caused and fixed.**
   `_fetch_appears_in()` is a general, case-agnostic helper — every
   APPEARS_IN edge for a given entity set, each row correctly tagged with
   its own document's real `case_id`. Correct for `cross_case=True`
   (XGRAPH's whole job), but the within-case (`cross_case=False`) caller
   never filtered the result back down to the active `case_id` —
   contradicting `retrieve_graph()`'s own docstring ("ignored entirely on
   the within-case path, which is always scoped to case_id regardless").
   A single canonical entity genuinely shared across two cases (identity
   resolution had already merged it — no SAME_AS fold involved, a
   different shape from the SAME_AS leak Milestone E2 already closed)
   could legitimately seed a within-case traversal (it DOES belong to the
   active case) and still pull in its OTHER case's evidence chunks
   unfiltered. Fixed with a one-line filter on `appears_in_rows` gated on
   `not cross_case and case_id`; regression test
   `test_single_entity_belonging_to_two_cases_never_leaks_the_other_cases_evidence`
   added to `tests/test_graph_retriever.py`, confirmed to fail without the
   fix and pass with it.
2. **XGRAPH secondary-fetch seeding improved.** The secondary XGRAPH
   fetch used to seed from the router's own `target_entity`, almost
   always `null` for exactly the queries that need this secondary (a
   compound question's cross-case half is phrased descriptively — "the
   accused" — since the router's one extraction, if any, went to the
   PRIMARY route's own need). `_fetch_secondary_evidence()` now falls
   back to the PRIMARY route's own already-resolved `seed_entities`,
   filtered to `type == "Person"` specifically (never the investigating
   officer, a vehicle, or an address also present in a case-wide
   enumeration seed set).

**Live-verified** (Docker/backend confirmed healthy, backend restarted):
re-ran the same 6 mini-sweep questions. Q4 (GRAPH+XGRAPH: officer +
accused's other cases) went from a full abstention (caused by the leak
above tripping the verifier's leakage check) to a complete, ground-truth-
correct compound answer — correctly named the investigating officer,
correctly identified the one accused who genuinely recurs in another case
(matching live Cypher ground truth exactly), and correctly stated the
other accused have no other-case involvement rather than overclaiming.
Q5 fell back to RAG again on this run (the evaluator accepted 25
supplemental items in the pre-follow-up verification pass but rejected 6
different, more targeted ones here) — genuine LLM/evaluator non-
determinism run to run, not a regression, reported as such rather than
claimed fixed. Q1/Q2/Q3/Q6 unchanged, as expected — none of their
blockers (thin primary graph evidence, missing Arms Ordinance reference
data) were in scope for these two fixes. Full backend suite green.

### Note on evidence quality — different from Modules 1-6
The first six modules were each reproduced against live data or a live
query. This one is confirmed a different, but equally certain, way:
by reading `src/pipeline/orchestrator.py`'s dispatch structure directly.
That's deterministic control flow, not LLM-dependent behavior, so it needs
no live repro to be certain — an `if/elif` chain cannot execute two
branches for one request, full stop. (A live compound-query repro was
planned but not run this pass — local Docker/Postgres was down at the
time — see "Suggested first step" below for exactly what to run once
infra is back up.)

### Problem
A genuinely compound question — one that needs two *different* retrieval
methods combined to fully answer — has no route that runs both and merges
them, except one specific hard-coded pair. Example shape: *"What is this
weapon's condition, and what PPC section covers illegal possession of an
unlicensed firearm?"* needs GRAPH (the weapon's own recorded attributes)
**and** SQL (the structured penal-code lookup) together. Today the router
picks exactly one route; whichever method wasn't picked contributes
nothing to the answer.

### Root cause
```
grep -n "route_str ==\|elif route_str" src/pipeline/orchestrator.py
```
confirms a single, mutually-exclusive `if/elif` chain: `DIRECT`, `SQL`,
`WEB`, `GRAPH`, `GRAPH_HYBRID`, `XGRAPH`, `XAGG`, `XNETWORK`, `RAG` — one
branch runs per query, always.

**Correction after reading the `GRAPH_HYBRID` branch's actual body
(orchestrator.py ~line 1007-1110), not just its name:** it combines
**three** methods for one query, not two — vector/semantic search (run
across the rewritten query plus expanded + cross-script variants),
`retrieve_graph()`'s graph traversal, AND a BM25 keyword search over a
GIN-indexed candidate pool — all merged (`combined_semantic = vector_results
+ graph_result["chunks"]`), RRF-fused against the BM25 results
(`rerank_results(...)`), then cross-reranked (`cross_rerank(...)`) before
generation. **The fuse-then-rerank machinery this module's own Option A
would need already exists, is already proven, and already handles 3 of
the 4 methods a general combiner would need** (semantic, keyword, graph).
The only method it never touches is **structured/SQL** — that's still a
fully separate, mutually exclusive route with no fusion path into
`GRAPH_HYBRID` at all today. This changes Option A's actual scope
meaningfully (see that option's own note below) — it is not "build a
fusion system from scratch," it is "make this one route's existing
3-way fusion (a) adaptively triggered instead of tied to one fixed route
name, and (b) extended to also accept SQL/structured results as a 4th
input."

This single fixed pairing is still wired to one specific route, not a
general mechanism — that's the actual gap. The other thing that can look
like combination but isn't: `SQL`/`GRAPH`/`GRAPH_HYBRID` each fall back to
`RAG` **on failure** (empty result or evaluator-judged-irrelevant) — that's
sequential substitution (only one method's output ever reaches the final
answer), never a simultaneous merge of two methods that both succeeded.

The not-yet-live agent harness (see Module 6's context, and the harness
work referenced this week) has a closer relative: `Investigative
Analysis`'s sub-agent already runs RAG+GRAPH+SQL **concurrently** via
`asyncio.gather`. But it's the same shape problem one level up — it's a
**fixed trio**, always run together whenever that one sub-agent is picked,
not an adaptive per-query decision of which subset is actually needed.

### Design options (this module needs a product decision more than the
### others do — how often real compound-need questions actually occur
### should inform which of these is worth building)
- **Option A — general adaptive N-way combiner.** Smaller than it first
  looks, given the root-cause correction above: the fuse-then-cross-rerank
  machinery already exists and already works for 3 of 4 methods (semantic,
  keyword/BM25, graph) — it just needs to (1) stop being tied to one fixed
  route name and become the target of an adaptive per-query decision, and
  (2) accept SQL/structured results as a 4th input to that same fusion,
  which today has no path into it at all. Concretely: change the router's
  output from "pick one route" to "return the set of retrieval types this
  query needs" (e.g. `{"GRAPH", "SQL"}`), run each concurrently — reusing
  `GRAPH_HYBRID`'s existing vector+BM25+graph fusion call for whichever of
  those three are selected, adding a new SQL-results-into-fusion path
  alongside it — then generate one answer citing across all of them. Still
  a real change — the router's schema/prompt, and the orchestrator's
  dispatch model (exclusive `if/elif` → run-selected-subset-then-merge)
  both need rework — but "extend a proven fusion function to a 4th input
  and trigger it adaptively" is a materially smaller lift than "build
  N-way fusion from nothing." Every existing route's own tests would need
  re-verification that single-method classification still works correctly
  as a special case of the new N-way model.
- **Option B — build it once, in the harness, not twice.** Rather than
  rebuilding this in the legacy orchestrator, treat `Investigative
  Analysis`'s existing fixed-trio composition as the seed of the real
  answer: make ITS tool selection adaptive (decide which of its 3 tools
  are actually relevant per query, skip the rest) rather than always
  running all three. Cheaper than Option A (reuses the harness's existing
  concurrent-tool-call plumbing), and avoids building the same adaptive-
  selection logic twice (once for a legacy path being pressure-tested
  toward retirement anyway). Blocked on the harness actually being live
  for real traffic — building product-facing capability into a
  pressure-testing-stage system is a sequencing call, not a technical one.
- **Option C — narrow, incremental, mirrors how GRAPH_HYBRID itself was
  added.** Don't build a general mechanism at all yet. Instead, once real
  compound-question shapes are identified (see "Suggested first step"
  below), add a small number of new fixed pairings for the most common
  ones only — the same way `GRAPH_HYBRID` itself is one specific,
  deliberately-added pairing, not a byproduct of a general system.
  Cheapest and lowest-risk, but doesn't generalize: every new real
  compound-question shape found later needs its own new fixed route.

**No firm recommendation given here** — unlike Modules 1-6, this one
depends on a product judgment call (how common are genuinely compound
questions in real usage, and is the harness's cutover timeline short
enough that Option B is worth waiting for) that the code alone can't
settle.

### Suggested first step (before picking an option)
Run a real, ground-truth-driven mini-sweep — same methodology as this
whole findings.md — of actual compound-need questions against the live
corpus (e.g. "what weapon + what PPC section", "which officer + has this
person been in another case", "case summary + is this a repeat-offender
pattern") and record what today's single-route dispatch actually produces
for each (which half of the question gets dropped, whether the RAG
fallback silently produces a worse answer instead of an honest partial
answer). That turns "this seems structurally possible" into "here's how
often it actually bites, and what it costs users today" — the input this
module actually needs before Option A/B/C can be chosen responsibly.

### Files likely touched (depends entirely on which option is chosen)
- Option A: `src/pipeline/router.py` (schema + prompt), `src/pipeline/orchestrator.py`
  (dispatch model), a new generic fusion function generalizing
  GRAPH_HYBRID's existing RRF logic.
- Option B: `src/pipeline/harness/agents/investigative_analysis.py`,
  `src/pipeline/harness/supervisor.py` (classification feeding it).
- Option C: `src/pipeline/router.py` (one or two new narrow override
  patterns per identified shape), `src/pipeline/orchestrator.py` (one new
  `elif` branch per new pairing, same shape as `GRAPH_HYBRID`'s own).

### Test plan
- Whichever option: the mini-sweep questions from "Suggested first step"
  become the regression suite — each must now cite evidence from every
  retrieval method it genuinely needs, not just whichever one route won.
- Existing single-method routing must be provably unaffected — every
  current route's existing tests (router + orchestrator + graph_retriever)
  green, unchanged, no regressions from whatever dispatch-model change is
  made.
- Full backend suite green.

---

## Module 8: Local Search — entity-based reasoning

### Origin
Requested explicitly by the team (Navaira Rehman, 2026-08-24): incorporate
Microsoft GraphRAG's Local Search methodology, **at the agent-harness
level** — not as a legacy-orchestrator change, per the explicit instruction
that this belongs alongside the agent-harness work already in progress
(Module 7's context). MS GraphRAG's own description of Local Search, as
supplied:
> Given a query, Local Search embeds the query and matches it against
> entity description embeddings to find a set of semantically-related
> entities ("access points" into the graph); fans out from those entities
> to gather candidate text units, community reports, related entities,
> relationships, and covariates; ranks and filters each candidate set down
> to fit a single context window; builds the final prompt from the
> prioritized data plus conversation history, and generates the response.

### Note on evidence quality
Confirmed by direct code inspection (like Module 7), not a live repro —
this describes a capability gap (something that doesn't exist), which a
live query can demonstrate failing but can't "prove absent" any more
precisely than reading the code that would have to implement it.

### Problem — three concrete, confirmed gaps against the description above
1. **No entity is ever embedded.**
   ```
   grep -rln "entity.*embed|embed.*entity|entity_description" src/ --include=*.py
   ```
   returns nothing real (one false-positive hit in an unrelated metrics
   docstring). Entity seeding today (`_find_seed_nodes()` in
   `src/retrieval/graph_retriever.py`) is 100% literal `CONTAINS`
   substring matching against `canonical_name`/`cnic`/`phone`/`plate`/
   `belt_no` — never semantic/embedding-based. **This is not theoretical:
   this session already live-reproduced the exact failure mode this
   would fix.** The "GRAPH within-case officer" test (findings from this
   session's original route sweep) failed specifically because *"the
   investigating officer"* is a role/descriptive reference, not a literal
   name — there was nothing for the literal-substring seeder to match.
   Semantic entity-description matching is precisely the mechanism that
   handles this class of query.
2. **No community-report context ever reaches an entity-centric answer.**
   Confirmed by reading `GRAPH_HYBRID`'s full retrieval body
   (orchestrator.py ~line 1007-1110, see Module 7's root-cause section) —
   it fuses vector search, BM25, and graph traversal, but never queries
   `community_reports`/`community_vector_store.py` at all. Only `XNETWORK`
   — a completely separate, cross-case-only route — ever touches community
   reports today. An entity-centric within-case question gets zero benefit
   from whatever pattern-level context a community summary might add
   (e.g. "this person's community was flagged for a recurring MO").
3. **No "covariates" concept.** GraphRAG's covariates are structured,
   provenanced claims about an entity, distinct from its raw properties.
   The closest existing analogue is Module 2's `_NOTABLE_PROPERTIES` work
   (surfacing an entity's own graph properties in generated evidence text)
   — related in spirit, but that's raw property surfacing, not an
   extracted-claim-with-basis structure. Worth building Module 2 first;
   don't conflate the two.

### Design
Per the explicit instruction, this is a **new harness sub-agent**
(`src/pipeline/harness/agents/`), not a legacy-orchestrator branch —
consistent with how every other capability in that directory is scoped
(one sub-agent, composing a bounded set of tools, per
`SUBAGENT_INTERFACES.md`'s existing contract pattern). Proposed shape,
reusing existing machinery wherever it already exists rather than
rebuilding:
1. **New entity-embedding pipeline** (genuinely new — nothing like it
   exists today): embed each entity's `canonical_name` + its notable
   identifying properties (reusing Module 2's `_NOTABLE_PROPERTIES` table
   as the "what to include in the description text" source) into a new,
   lean Chroma collection — mirror `community_vector_store.py`'s existing
   pattern exactly (it already solved "a second, small, separate
   collection alongside the main document-chunk store," same
   `embed_text`/`embed_texts` functions, same `EMBEDDING_PROVIDER` config
   — do not reinvent that wrapper). Needs a refresh trigger analogous to
   Module 6's community refresh — new/changed entities should re-embed,
   likely piggybacking on the same sync-script call site Module 6 adds.
2. **Semantic access-point matching**: embed the incoming query, query
   this new collection for top-k semantically similar entities — this is
   the part `_find_seed_nodes()` cannot do today (lexical only).
3. **Fan-out**: from each matched entity, reuse `retrieve_graph()`'s
   existing traversal for text units/related entities/relationships
   (already correct — Modules 1/2 improve exactly this path), PLUS a new
   join through `community_membership` (already populated by
   `community_detection.py`, just unused for this purpose today) to pull
   in that entity's community report(s) from `community_reports`.
4. **Rank/filter to context window**: reuse the existing `cross_rerank()`
   machinery already proven in `GRAPH_HYBRID` — do not build a second
   reranking mechanism.
5. **Generate**: same self-contained-prompt-plus-verification convention
   every other harness sub-agent already follows (per
   `AGENT_HARNESS_RECONCILIATION_PROGRESS.md`'s Semantic Search precedent).

### Files likely touched
- New: `src/retrieval/entity_vector_store.py` (mirrors
  `community_vector_store.py`'s structure closely).
- New: `src/pipeline/harness/agents/local_search.py`.
- `src/pipeline/harness/supervisor.py` — classification/dispatch for when
  this sub-agent should be selected (descriptive/role-based entity
  references, per the confirmed GRAPH-officer failure mode above, are the
  clearest trigger signal).
- `src/graph/structured_projection.py` or the sync-script call site Module
  6 adds — trigger entity re-embedding alongside community refresh.
- New test files for the embedding store, the semantic matcher, and the
  sub-agent itself (unit tests with a fake embedding function, same
  `no_network`-guarded pattern this repo's test suite already uses
  throughout).

### Test plan
- Unit: a descriptive query ("the investigating officer in this case")
  with NO literal name/identifier now finds a real seed via semantic
  match, where `_find_seed_nodes()` alone would return empty — direct
  regression test for this session's own confirmed failure.
- Unit: fan-out correctly includes a community report when the matched
  entity's community has one, and correctly omits it (not crashes) when
  the entity belongs to no community (singleton, filtered out by
  `community_detection.py`'s own `MIN_MEMBERS_FOR_SUMMARY` gate).
- Live verification: re-run this session's own "who is the investigating
  officer" repro (case `fir-401-26`) through the new sub-agent — must now
  return a confident, correctly-cited answer where the legacy path
  returned "cannot provide a confident answer."
- Full backend suite green, including the harness's own compliance suite
  (RLS/role-gate parity — this sub-agent must not weaken any guarantee the
  other 8 already enforce).

---

## Module 9: Global Search — whole-dataset map-reduce reasoning

### ✅ Stage 1 RESOLVED — 2026-08-25

### ✅ Stage 2 RESOLVED — 2026-08-25
Fixed on `main` (merged from `feature/harness-global-search-stage2`):
`detect_communities()` now consumes `louvain_partitions()`'s full
generator (finest → coarsest) instead of `louvain_communities()`'s single
flattened result — the exact same underlying algorithm/dependency, not a
new one. `community_membership.level` carries real, distinct per-level
values (was previously always a hardcoded `0`) — additive to the
already-existing column, no schema change. **Semantic note:** post-Stage-2
`level=0` means the FINEST partition; pre-Stage-2 the single stored
partition (always `level=0`) was actually Louvain's COARSEST merge — a
real meaning change for what "level 0" denotes, not silently glossed
over. `community_runs.community_count` keeps recording the finest level's
count. `community_summarization.py` summarizes only the finest
`MAX_LEVELS_TO_SUMMARIZE=3` levels a run actually persists (persisting
every level is cheap; summarizing multiplies LLM cost per level). Stage
1's `hierarchy_level` default-to-middle-level logic
(`run_global_search_query()`, already shipped) becomes meaningful
automatically — no further code change was needed there.

**Tests**: new `detect_communities()` end-to-end test against Zachary's
karate club (34 nodes, real multi-community structure) asserts ≥2
genuinely different Louvain levels get persisted with real distinct
`level` values in `community_membership` — not one partition duplicated
under two level numbers. `tests/test_community_summarization.py` (new, 4
cases) covers the finest-3 summarization cap directly. Full backend suite
green; harness compliance suite green.

**Live-verified** against the real running graph (Docker/Postgres
healthy) — `POST /api/admin/community/refresh`'s own
`detect_communities()`+`summarize_communities()` sequence run directly:
- **Real level counts on the actual graph: `[19]` — exactly one level.**
  Not a Stage 2 defect — independently reconfirmed by reconstructing the
  identical weighted graph outside `detect_communities()` and calling
  `louvain_partitions()` directly: **`[19]` across 4 different seeds
  (42, 1, 7, 123), and with weighting removed entirely.** The real cause:
  the current live graph is extremely dense — 428 nodes, 67,603 edges,
  **74% density** — dominated by one 232+/352-member giant community
  (the same one Module 6's own live-verification already surfaced). At
  that density Louvain's modularity optimization converges in a single
  pass, with no further beneficial coarsening step for the generator to
  yield. The karate-club unit test proves the CODE handles ≥2 real levels
  correctly; the live graph today simply doesn't have multi-level
  structure to expose.
- `community_membership`/`community_reports` both confirmed showing
  exactly `level=0` for all 19 communities/reports on this run, matching
  the `[19]`-only level-count finding.
- **Flagged, not fixed here (out of this module's scope):** a
  74%-density projected shared-case co-occurrence graph, with one
  community absorbing 352 of 428 nodes, is itself a plausible
  community-detection-QUALITY finding worth its own investigation (e.g.
  the shared-case edge-weighting scheme, or Louvain's resolution
  parameter) — Stage 2's own scope was "consume the full
  `louvain_partitions()` generator," not "make the live graph's structure
  more hierarchical," and no such tuning was attempted here.
Fixed on `main` (merged from `feature/harness-global-search-stage1`),
implementing Stage 1 exactly as scoped below — a real map-reduce
sub-agent over the existing flat community level. Stage 2 (real
hierarchy) is a separate, still-open follow-up per this module's own
staging.

- New `src/pipeline/global_search.py::run_global_search_query()` — same
  role-gate-then-RLS-arm-then-fetch shape as `xnetwork.py`'s
  `run_network_query()`, but fetches EVERY community report for a
  hierarchy level directly from Postgres
  (`community_detection.get_community_reports_for_level()`, new), not a
  Chroma top-k similarity cut.
- New `src/pipeline/harness/tools/global_search.py` — thin `ToolResult`
  wrapper, cross-case role-gated, registered into
  `_source_scan.py`'s `TOOL_WRAPPER_MODULE_NAMES` /
  `CROSS_CASE_TOOL_MODULE_NAMES` (enforcement points 2/3/4/5 now cover
  it) plus a dedicated behavioral DENIED test added to
  `test_enforcement_3_cross_case_role_gate.py`.
- New `src/pipeline/harness/agents/global_search.py` — the actual
  map-reduce: caps the fetched report set at 60 (sampled beyond that),
  batches at 5, shuffles each batch, one `call_llm_json()` per batch for
  importance-rated points, reduce step keeps the top 15 points by
  importance, generates the final answer from their distinct backing
  community reports, verified through the existing
  `verify_grounding()` + structural-tier `validate_answer()`.
- `src/pipeline/harness/supervisor.py` — new `GLOBAL_SEARCH` sub-agent
  name, a narrow provisional trigger vocabulary ("top 5 themes", "most
  common patterns", etc.) that overrides ONLY the XNETWORK route's
  default (Cross-Case Linkage's existing "specific network" framing is
  unaffected), and added to the cross-case `case_scope` demotion guard.

**Real current community-report count this sizing was based on: 19**
(confirmed live this session, `RUN-20260825074016`/later re-confirmed
under `RUN-20260825142014` — both newer than the latest case data;
Module 6 was already merged to `main` before this module started, so no
manual dependency-workaround was needed, though the refresh was re-run
once more immediately before live verification as belt-and-suspenders).

**Live-verified** against the real running backend (Docker/Postgres
healthy, backend `/health` → `database_status: ok`), refresh re-run
immediately before verifying (`RUN-20260825142014`, 19 communities, 19
reports):
- Query A (today's existing XNETWORK trigger shape — this session's own
  repro text, "overall picture of associate networks across the robbery
  cases"): old top-5 retrieved 5 of 19 reports
  (`C-...0006/0001/0000/0012/0008`); the new fetch sees all 19 —
  **14 of 19 reports the old top-5 cut never retrieved at all.**
- Query B (the new Global Search trigger shape — "what are the top 5
  themes across all the cases"): old top-5 retrieved a *different* 5 of
  19 reports; again **14 of 19 missed** by the old cut.
- Full sub-agent run on Query B: `status=ok`, `tools_used=["XNETWORK"]`,
  answer cited **all 19** community reports (`[Document 1]`..`[Document
  19]`), producing five real dataset-wide themes (PPC-case prevalence,
  co-accused relationship patterns, PECA 2016 cybercrime, Arms Ordinance
  1965 cases, geographic/police-station diversity) — several of which
  (the PECA and Arms Ordinance themes in particular) cite reports that
  were NOT in Query B's own old top-5 cut, concretely demonstrating
  map-reduce surfacing dataset-wide signal a top-k similarity cut
  drops. Structural-tier validation flagged 4 minor claim/source
  mismatches as non-blocking caveats (working as designed — caveat-only,
  never blocking a verified answer).

**Tests**: `tests/test_harness_tool_global_search.py` (5 cases — OK/EMPTY/
DENIED/FAILED shape, `fallback_to_rag` pinned False),
`tests/test_harness_agent_global_search.py` (13 cases, including the
required Stage 1 test: a query needing signal from 2 reports that would
NOT individually rank in a naive top-5-by-similarity cut still surfaces
both because map-reduce processes every report, batching/shuffling/
cap/reduce-step/partial-batch-failure/verifier-rejection/Supervisor-
dispatch all covered), `tests/test_harness_supervisor.py` (+4 cases for
the new classification override, including the case_scope demotion
guard), `tests/test_community_detection.py` (+6 cases for the two new
fetch helpers). Full backend suite green; harness compliance suite green
(60+ cases, including the new module's own DENIED/RLS/role-provenance/
no-raw-Cypher checks).

### Origin
Same request as Module 8, MS GraphRAG's Global Search description:
> Best for questions needing aggregation across the entire dataset (e.g.
> "what are the top 5 themes in the data?") — queries Baseline RAG performs
> poorly on, since there's no single relevant chunk to retrieve. Map step:
> community reports (from a chosen hierarchy level) are batched and
> shuffled; each batch produces an intermediate response — a list of
> points, each with a numeric importance rating. Reduce step: the most
> important points across all intermediate responses are filtered,
> aggregated, and used as context for the final answer. Hierarchy level
> matters: lower levels (more detailed reports) give more thorough answers
> but cost more time/LLM resources.

### Note on evidence quality
Confirmed by direct code inspection, same caveat as Module 8.

### Problem — read `src/pipeline/xnetwork.py` in full; it is not map-reduce
`run_network_query()` (the entirety of XNETWORK's logic) does exactly one
thing: `query_similar_communities(query_text, top_k=5)` — a single
semantic-similarity top-5 lookup against precomputed community summaries,
handed once to the orchestrator for one generation+verification pass.
**This is standard top-k RAG over a smaller (summary-level) corpus, not
GraphRAG's map-reduce algorithm.** The distinction matters for exactly the
question class GraphRAG's own docs call out: "top 5 themes in the data"
needs signal aggregated across potentially dozens of community reports —
a report that's individually a weak semantic match to the literal query
string but collectively part of a real dataset-wide pattern will never
surface in a top-5-by-similarity cut, no matter how relevant it actually
is in aggregate. This is precisely the failure mode Global Search's
map-reduce shape exists to avoid, and precisely what current XNETWORK
inherits by using top-k instead.

### A real prerequisite gap: community detection is flat, not hierarchical
```python
partition = louvain_communities(g, weight="weight", seed=42)   # community_detection.py line 439
```
`networkx`'s `louvain_communities()` returns exactly one, final, flattened
partition — there is no second level to choose from, so GraphRAG's
"hierarchy level" trade-off (fewer/coarser communities = cheaper/broader,
more/finer = more thorough/expensive) isn't a decision our system can make
today; there is only ever one level. **Verified live, in this repo's own
installed `networkx` (3.6.1), that a fix exists using the SAME algorithm
already in use — not a new dependency:**
```python
>>> from networkx.algorithms.community import louvain_partitions
>>> levels = list(louvain_partitions(G, seed=42))   # generator, finest -> coarsest
>>> [len(lvl) for lvl in levels]
[5, 4]   # confirmed live on a test graph — genuinely multiple levels
```
`louvain_partitions()` is the underlying generator `louvain_communities()`
already calls internally and simply takes the last item from — swapping to
consume the full generator gets real hierarchy from the exact same
algorithm and dependency already in use, not a new clustering approach.

### Proposed approach (staged — Stage 1 alone fixes the main complaint;
### Stage 2 is what makes "hierarchy level" in the original ask meaningful)
- **Stage 1 — map-reduce over the existing (flat) level.** New harness
  sub-agent, per the same explicit instruction as Module 8. Map step: take
  ALL community reports (or a large capped sample, for cost control — see
  design note below), batch them (shuffled, per the algorithm description,
  to avoid position bias), run one `call_llm()` per batch asking for a
  list of importance-rated points relevant to the query. Reduce step:
  collect every batch's points, sort/filter by importance, keep the top-N,
  generate the final answer from those — same
  self-contained-prompt-plus-verification convention as every other
  sub-agent. This alone fixes the "misses distributed signal" problem
  without touching `community_detection.py` at all.
- **Stage 2 — real hierarchy.** Switch `detect_communities()` to consume
  `louvain_partitions()`'s full generator instead of
  `louvain_communities()`'s single flattened result, persist every level
  (the `community_membership` table already has a `level` column —
  confirmed live in this session, currently always written as one
  constant value; this is additive, not a schema change) with real
  per-level community IDs, and `community_summarization.py` summarizes
  each level (cost note: summarizing every level multiplies LLM-summary
  cost by the number of levels — likely worth summarizing only 2-3 levels,
  not every level Louvain happens to produce, since real per-run tests
  this session found levels can degenerate quickly, e.g. 5→4 communities
  on a small test graph). Global Search's sub-agent then takes a
  `hierarchy_level` parameter (default: a middle level, tunable) feeding
  Stage 1's map step with that level's reports specifically.

### Design note — cost control on Stage 1's map step
A real dataset-wide map-reduce means one LLM call per batch of community
reports, every time this route fires — meaningfully more expensive than
today's single top-5-then-generate call. Before implementing, check the
real report count this session's Aug-22 run produced (18 communities, one
run) — at that scale, batching all 18 into e.g. 3-4 batches is cheap; this
calculation needs revisiting once Module 6 keeps community counts current
and the corpus keeps growing (478 Person nodes today, growing).

### Files likely touched
- `src/graph/community_detection.py` — Stage 2's `louvain_partitions()`
  swap and multi-level persistence.
- `src/graph/community_summarization.py` — per-level summarization.
- New: `src/pipeline/harness/agents/global_search.py` — Stage 1's
  map-reduce logic.
- `src/pipeline/harness/supervisor.py` — classification/dispatch for
  "whole-dataset theme/pattern" question shapes (distinct from XNETWORK's
  existing narrower "overall picture of THIS network" trigger — needs its
  own few-shot/pattern examples, not a reuse of XNETWORK's).
- New tests for the map step (batching + importance-rated extraction), the
  reduce step (correct aggregation/ranking), and — for Stage 2 — that
  `detect_communities()` persists genuinely different community sets per
  level, not the same partition duplicated under different level numbers.

### Test plan
- Stage 1 unit: a query whose answer requires signal from 2 reports that
  would NOT individually rank in a naive top-5 similarity cut still
  surfaces both, because map-reduce processes all reports, not just the
  most similar few.
- Stage 1 live verification: re-run this session's own XNETWORK "overall
  picture of associate networks" repro — compare today's top-5 answer
  against the new map-reduce answer using the SAME (refreshed, per Module
  6) community data, checking whether the map-reduce version surfaces
  anything the top-5 version missed.
- Stage 2 unit + live: `detect_communities()` run against the real graph
  produces ≥2 genuinely different levels (not one level duplicated), and
  `community_membership.level` reflects real, distinct level numbers.
- Full backend suite green, harness compliance suite green.

---

## Module 10: Meta-analysis — query decomposition and aggregation

### Origin
Requested explicitly, twice, by the team: *"handling a larger query that
needs to be broken down into smaller tasks, with the results then
aggregated to derive insights from a larger chunk of data... the
possibility of handling this through a separate agent."*

### Note on evidence quality
Confirmed by direct code inspection, same caveat as Modules 7-9 — this is
a capability gap, not a live-reproducible failure in the same sense as
Modules 1-6.

### Problem
No mechanism exists anywhere in this codebase that takes one broad user
question, breaks it into several independently-answerable sub-questions,
runs each, and synthesizes across the results. Confirmed by searching for
any trace of one:
```
grep -rln "decompos|sub_quer|subquery|sub-quer" src/ --include=*.py
```
returns nothing real (the one hit is unrelated HTML-DOM-parsing code, not
query logic). A query like *"summarize the recurring patterns across all
robbery cases handled by this station in the last quarter and flag any
that share a suspect with an unresolved case"* has no route that would
even attempt this today — it would get forced into whichever single route
the classifier judges closest (most likely XNETWORK or XAGG), and
everything the question asked beyond that one route's fixed shape would
simply be dropped, with the user given no indication that only part of
their question was actually answered.

### What already exists, and why none of it is this
- **XAGG's canned aggregates** (`src/pipeline/xagg.py`) — station/category
  counts, recurring-entity rankings. Fixed, pre-built patterns matched by
  keyword, not decomposition of an arbitrary question into sub-questions.
- **`Investigative Analysis`/`Cross-Case Linkage` harness sub-agents**
  (confirmed by reading both directly, per Module 7's investigation) —
  each composes a FIXED, small set of tools **concurrently for one query**
  (e.g. RAG+GRAPH+SQL together, always). That is tool fan-out for a single
  question, not decomposition of a broad question into multiple distinct
  sub-questions with independently-generated sub-answers.
- **Module 9's proposed map-reduce** (Global Search) — closest existing
  relative, but it maps over *community reports* specifically for
  *dataset-wide theme* questions. Meta-analysis here is broader: any
  compound question, decomposed into sub-*questions* (not pre-existing
  report batches), each potentially resolved by any route — GRAPH, SQL,
  XGRAPH, anything.

None of these decompose a *question*; XAGG and the harness sub-agents
decompose (or rather, fix in advance) *which tools to call*, and Module
9's map-reduce decomposes *evidence* (report batches), not the user's
actual ask.

### Relationship to Modules 7, 8, 9
Complementary, not overlapping:
- **Module 7** (multi-method retrieval) operates *within* answering ONE
  question — deciding which retrieval methods that one question needs.
- **Module 10** (this module) operates *above* that — deciding whether
  the user's ask is actually SEVERAL questions, splitting it, and
  synthesizing across independently-produced answers. A single sub-query
  Module 10 produces could itself benefit from Module 7's multi-method
  retrieval, or resolve to a Module 8 (Local Search) or Module 9 (Global
  Search) sub-agent — Module 10 is the outermost layer, dispatching down
  into whichever of those (or the existing 9 routes) fits each sub-query.

### Proposed approach
Per the explicit "separate agent" framing already discussed with the
team: a new harness sub-agent, e.g. `src/pipeline/harness/agents/
meta_analysis.py`, with three stages:
1. **Decompose.** A new, narrow-scope LLM call (own `prompts/*.txt`, own
   strict-JSON schema — same discipline every existing prompt in this
   repo follows) takes the original query and returns a bounded list of
   sub-queries (cap N — see cost note below) PLUS a short "synthesis
   goal" string describing what the final answer needs to accomplish
   across them. Critically, this step must also be able to say "this
   doesn't need decomposition" for an ordinary single-focus question —
   otherwise every query pays the decomposition-call cost. Cheapest place
   for that decision: a narrow set of deterministic trigger patterns
   (mirroring `router.py`'s own `_XAGG_OVERRIDE_PATTERNS`-style approach —
   "summarize... across all", "aggregate... and flag", "recurring...
   and cross-reference") that fast-path INTO decomposition, falling back
   to "no decomposition needed" by default — same asymmetry `router.py`
   already uses (deterministic overrides catch the unambiguous cases
   cheaply; everything else goes through the normal single-route
   classification unchanged).
2. **Dispatch.** Run each sub-query through the EXISTING pipeline —
   literally re-enter `Supervisor.handle()` (or `route_query()` +
   whichever sub-agent it resolves to) for each sub-query, concurrently
   (`asyncio.gather`, the same pattern `Investigative Analysis`/`Cross-Case
   Linkage` already use). This is the module's central design commitment:
   **reuse the full existing routing/retrieval stack recursively, one
   level only — do not build a second, parallel retrieval pipeline.** No
   recursion beyond one level (a sub-query is never itself decomposed
   further) — bounds cost and complexity for a first version.
3. **Aggregate/synthesize.** Collect each sub-query's answer plus its own
   citations, then one final `call_llm()` pass synthesizes across all of
   them into a single coherent answer addressing the original question's
   full "synthesis goal," verified through the same
   `verifier.verify_grounding()` mechanism every other sub-agent already
   uses (may need a small extension to accept multiple independent
   evidence sets rather than one — check its current signature before
   assuming a change is needed).

### Design questions needing a product decision (flagging, not deciding)
- **RBAC**: aggregating "a larger chunk of data" is inherently cross-case
  shaped in most real uses of this — should default to requiring the same
  supervisor-or-higher role gate XAGG/XGRAPH/XNETWORK already enforce
  (`_enforce_cross_case_role_gate`), reused, not reinvented, rather than
  assuming every meta-analysis query is cross-case by default.
- **Cost/latency**: N sub-queries × a full pipeline pass each, plus one
  decompose call and one synthesis call — meaningfully more expensive and
  slower than any single existing route. Needs a hard cap on N (e.g. 5)
  and should surface partial results gracefully if one sub-query's
  pipeline pass fails or times out, rather than failing the whole
  meta-analysis because one of five sub-queries came back empty.
- **Sequencing relative to Modules 8/9**: since Module 10 dispatches down
  into whichever routes/sub-agents exist, it delivers more value once
  Modules 7-9 exist (a sub-query about "the overall picture" can resolve
  to a real Global Search pass instead of today's top-5 XNETWORK). Doesn't
  block starting Module 10 first, but worth sequencing last of the five
  stakeholder-requested modules (7-10) if effort must be spread over time.

### Files likely touched
- New: `prompts/meta_analysis_decomposer.txt` — the decomposition prompt.
- New: `src/pipeline/harness/agents/meta_analysis.py`.
- `src/pipeline/harness/supervisor.py` — new deterministic trigger patterns
  (mirroring `router.py`'s override-pattern style) for when decomposition
  fires.
- Possibly `src/pipeline/verifier.py` — check `verify_grounding()`'s
  current signature for whether it already accepts multiple evidence sets
  or needs a small extension.
- New test files: decomposition prompt/schema tests (same pattern as
  `test_doc_classifier.py`'s enum-drift guard and `test_router.py`'s
  few-shot-schema tests — lock the prompt's JSON schema to whatever
  contract the code expects), dispatch-fan-out tests (N sub-queries really
  do run concurrently against the existing pipeline, not sequentially or
  against a mocked shortcut), and aggregation tests (a good sub-answer
  from one sub-query survives even if another sub-query fails/times out —
  partial-failure graceful degradation, not all-or-nothing).

### Test plan
- Unit: a genuinely compound query (the robbery-pattern example above)
  decomposes into a sensible bounded sub-query set; an ordinary
  single-focus query correctly decomposes into itself (no-op) or is
  correctly classified as "no decomposition needed," never needlessly
  fragmented.
- Unit: one sub-query's pipeline failure/timeout doesn't crash the whole
  meta-analysis — the other sub-queries' results still reach the
  synthesis step, with the failure disclosed, not silently dropped.
- Live verification: construct 2-3 real compound questions against the
  real corpus (following this whole document's ground-truth-driven
  methodology — check real ground truth for each sub-part first), run
  through the new sub-agent, and confirm the final answer actually
  addresses every sub-part with correct, citable evidence — not just the
  one part a single existing route would have picked.
- Full backend suite green, harness compliance suite green (this
  sub-agent must not weaken the RBAC/RLS guarantees the other 8 already
  enforce, especially given the cross-case RBAC question flagged above).

---

## Module 11: Unreviewed name-fallback duplicates poison community detection, plus a common-noun mistagged as a Person

### ✅ A1/A2 RESOLVED — 2026-08-25 (B corrected, not fixed — see below)
Fixed on `main` (merged from `fix/module11-duplicate-person-mentions`):

- **A1** (`src/ingestion/service.py`): added `document_resolved_persons`,
  a document-scoped (not chunk-scoped) `canonical_name -> entity_id`
  cache. A `person` mention whose exact string was already resolved
  earlier in the same ingestion run now reuses that `entity_id` — one
  `APPEARS_IN` edge is still written for the new occurrence's own chunk
  (real provenance preserved), but the node-mint +
  `BELONGS_TO_CASE`-edge + candidate-search + pending-`SAME_AS`-proposal
  steps `resolve_and_write()` would otherwise repeat are skipped
  entirely. `resolved_persons` (the pre-existing, still chunk-scoped
  dict used for relationship extraction) is completely unchanged —
  A1 does not touch which names get passed into
  `_extract_and_write_relationships()` at all, only whether a *node* gets
  re-minted for an exact-string repeat.
- **A2** (new `scripts/collapse_same_document_duplicate_persons.py`):
  a narrow bulk-confirm over the *existing* review-queue machinery
  (`src/api/graph_review.py::confirm_match()` — the same function a
  human clicking "Confirm" calls) for `pending` `SAME_AS` edges where
  BOTH endpoints share the exact same `source_doc_id` AND the exact same
  `case_id`. Confirming (not merging) is sufficient:
  `community_detection.build_canonical_map()` already collapses
  confirmed, non-superseded `SAME_AS` components at READ time, before
  clustering — so this directly fixes the community-detection symptom
  without any new merge machinery. `--dry-run` (default) / `--apply`,
  same convention as `scripts/cleanup_orphaned_person_nodes.py`.

**Correction to the original diagnosis (root cause B):** live-verified
`_is_plausible_person_name("قبضے")` returns `False` *already*, via the
existing single-token rule (`" " not in stripped`) — `قبضے`/`کاشف`/`فیصل`
(324 of the 692 raw mentions) never actually reached
`community_detection.py`'s clustering graph at all; they were filtered
before Stage 2's own diagnosis even ran. The 368-member giant community
is composed entirely of the 5 MULTI-token variants (138+92+46+46+46 =
368 exactly), which correctly pass today's plausibility filter (they
read as real 2-4-word names) and are exactly what A1/A2 target. Root
cause B (`قبضے` reaching the *graph* as a real `Person` node, still true
at the AGE level, just not the thing distorting Module 9) is real but
separate and NOT fixed by this pass — see "B — corrected, not fixed"
below. Adding it to `_NON_NAME_PHRASES` would have been dead code (the
single-token rule already excludes it there), so that patch was
deliberately not made.

**Tests**: `tests/test_ingestion_graph_extraction.py` (+2 cases — a
same-document repeat resolves once and gets its own `APPEARS_IN` edge; a
second, separate document with the same name is unaffected, proving A1
doesn't widen cross-document matching), `tests/test_collapse_same_document_duplicate_persons.py`
(new, 4 cases — dry-run makes no confirm calls, `--apply` confirms every
qualifying edge, per-edge errors don't abort the batch, empty-queue
reports cleanly). Full backend suite green.

**Live-verified** against the real running Postgres/AGE instance:
`--dry-run` found **6,523 qualifying pending edges**, `--apply` confirmed
them via the real `confirm_match()` path (571 unique edges actually
confirmed; the remaining ~5,952 attempts correctly 409'd as
"already reviewed" — the qualifying-edge query itself returns duplicate
rows per edge_id where a person has more than one non-superseded
`BELONGS_TO_CASE` edge to the same case, a pre-existing data-versioning
detail unrelated to this fix; `confirm_match()`'s own idempotency guard
made every duplicate attempt a safe no-op, not a partial failure — the
post-run "remaining: 0" check confirms every qualifying edge really was
resolved). Directly verified: `fir-1001-26`'s 700 raw `Person` nodes now
canonicalize (via confirmed `SAME_AS`, at read time, no physical merge)
down to **36** — not the 1-2 the LLM's own summary implied, but a ~95%
reduction, exactly matching the narrow same-document+same-case safety
bar (residual duplicate name-fallback candidates that were never
proposed as a pending pair against each other in the first place are
correctly left untouched, not force-merged).

**Downstream effect on the graph — dramatic and exactly as predicted**:
re-running `detect_communities()` afterward, the projected graph shrank
from 428 nodes / 67,603 edges to **64 nodes / 81 edges**. The former
368+-member mega-community is completely gone — the largest community
in the new run has **6 members**, matching the healthy 2-6-member
distribution every other (legitimate) case cluster already had. The
graph-density symptom that blocked Module 9 Stage 2's hierarchy is fully
resolved at its actual root cause, not papered over. **Still only one
Louvain level** (`[19]`) on this now-healthy, much smaller (64-node)
graph — this is no longer a duplication-pollution artifact (confirmed:
the pathological cluster is gone), just a small-graph structural
property Module 9 Stage 2's own code already handles correctly whenever
it does arise (proven on the karate-club fixture) — left as an honest,
unforced observation, not something this pass manufactured a second
level for.

**Operational note for any future re-run**: `_ScriptAdmin.id`'s random
`uuid.uuid4()` (mirroring `scripts/verify_milestone_d.py`'s own
documented `_Admin` precedent) is not a row in `users` — this
environment's `audit_logs.user_id` carries a real foreign-key constraint
(stricter than that precedent's own "harmlessly logged and swallowed"
comment anticipated), so every `confirm_match()` call's own audit-log
write failed with a caught `ForeignKeyViolationError`, loud but
non-fatal — the underlying `SAME_AS` confirmation itself still succeeded
every time (verified directly against the graph, not inferred). A future
run wanting a clean audit trail for this action should pass a real
service-account/admin UUID that actually exists in `users`, not a fresh
random one.

**B — corrected, not fixed this pass.** `قبضے` (Urdu: "possession/
custody") still exists as a real `Person` node in AGE — `resolve_and_write()`
writes it unconditionally at ingestion time; only `community_detection.py`'s
own downstream read-time filter happens to exclude single-token noise
from clustering. It can still surface through XGRAPH, XAGG, or any other
consumer that reads `Person` nodes directly without that same filter. A
real fix needs a precision gate at the extraction layer
(`src/extraction/ner.py`/`domain_entities.py`) — a stoplist check or a
minimum-corroboration requirement for a bare single-word `person`
candidate — not a `community_detection.py` blocklist entry (provably a
no-op for single-token strings, given the existing space-check runs
first). Left as an open, documented, NOT-implemented finding.

### Origin
Discovered as a side effect of Module 9 Stage 2's own live verification
(2026-08-25): `detect_communities()`, run against the real live graph,
produced exactly one Louvain level (`[19]`) instead of the ≥2 the same
code reliably produces on a real multi-community fixture graph
(Zachary's karate club, this session's own test). Diagnosing WHY —
rather than tuning Louvain's resolution or the community-detection edge
weights to paper over it — traced to a real, upstream, unrelated bug.

### Note on evidence quality
Fully live-reproduced and root-caused against the real running
Postgres/AGE instance this session (not code inspection alone) — every
number below is a direct query result, not an estimate.

### Problem
One community absorbs 368 of 428 canonicalized graph nodes (86%) and
alone accounts for ~67,528 of the graph's ~67,603 edges (>99.9%) — this
single community IS the graph's density. Tracing it back:

- **All of it is one case, one document.** `fir-1001-26` (an Arms
  Ordinance case) has 692 `Person` nodes belonging to it — every other
  case in the entire corpus has 3-6. 691 of those 692 nodes trace to a
  single `source_doc_id`
  (`psrms_fir_fir-1001-26#narrative_c8bf2613`) — one narrative document.
- **It's 8 distinct strings, wildly over-repeated**, not 692 distinct
  people: `کاشف` (231×), `محمد رمضان` (138×), `بجے فیصل` (92×), `فیصل`
  (47×), `تحت فیصل` (46×), `مدعی فیصل` (46×), `محمد رمضان ساکنہ محلہ`
  (46×), `قبضے` (46×). The community's own LLM-written summary already
  says as much in plain English: *"Muhammad Ramadan is the only named
  individual connected to this case."*
- **One of the 8 isn't a name at all.** `قبضے` is the Urdu word for
  "possession/custody" (a common word in FIR narrative boilerplate,
  e.g. "recovered from his possession") — a pure entity-type
  misclassification, mistagged as a `Person` 46 times.
- **This is essentially the entire graph's un-reviewed backlog.**
  Graph-wide, only 19 of 641 `SAME_AS` edges are `confirmed` (3%); 622
  are still `pending`. Nearly all of that pending backlog belongs to
  this one case's duplicate cluster (per-edge tier
  `flagged_unverified`, basis `"matched on near-identical name + shared
  case"` — the system DID correctly flag these as likely duplicates; a
  human has simply never reviewed them, and there is no bulk-action path
  to clear a same-document repeat-mention cluster this large cheaply).

### Root cause (two separate, stacked gaps)

**A — no within-document/within-case exact-string dedup before minting a
new node.** `src/ingestion/service.py`'s per-chunk loop already scopes
one dedup mechanism to the whole document — `written_pairs`/
`resolved_persons` (line ~298) exist specifically so the same real-world
pair proposed twice within one ingestion doesn't get written twice — but
`resolved_persons` is reset every chunk (`dict` declared *inside* the
`for chunk in chunks:` loop, line 324), and neither it nor anything else
is consulted before `entity_resolution.resolve_and_write()` is called for
a `person` mention. `resolve_and_write()` itself is CNIC-first,
name-fallback (architecture §7.3, `entity_resolution.py`'s own header
comment): a mention with no CNIC is *never* auto-merged, no matter how
strong the name match — by design, to prevent false-positive merges
across genuinely different documents/cases. That design is correct for
its stated purpose, but it means the SAME literal string, mentioned 231
times in flowing narrative prose within *one* document, is treated
identically to 231 independent CNIC-less mentions from unrelated cases —
each mints its own node and, at best, a `pending` `flagged_unverified`
`SAME_AS` edge back to the others. Nothing in the resolution pipeline
recognizes "same exact string, same document, already resolved once this
run" as the safe, cheap collapse it actually is.

**B — no non-name filter upstream of the graph.** `قبضے` reaching the
graph as a confirmed `Person` node is the same failure *class*
`community_detection.py`'s own `_NON_NAME_PHRASES` blocklist exists to
patch (that module's comments document three prior rounds of exactly
this: form-field labels, station names, role titles extracted as
`Person`) — but that blocklist is a downstream, community-detection-only
guard, explicitly scoped in its own comments as "not a fix to the
extraction pipeline itself." `قبضے` isn't in it, and even if it were,
the guard doesn't run upstream of XGRAPH, XAGG, or any other consumer
that reads `Person` nodes directly — only community detection's own
clustering is protected.

### Design options (two independent decisions — pick one per root cause,
not a single combined choice)

**For A (duplicate-mention explosion):**
1. **Root fix — widen `resolved_persons` from chunk-scoped to
   document-or-case-scoped, exact-string only.** Before calling
   `resolve_and_write()` for a `person` mention, check whether the exact
   same `canonical_name` string was already resolved earlier in *this
   same ingestion run* for this document (or case); if so, reuse that
   `entity_id` directly instead of minting a new node and a new
   `SAME_AS` proposal. Exact-string match only (not fuzzy) keeps this as
   safe as the existing CNIC tier's own "structural, not scored"
   discipline — it does not weaken cross-document/cross-case resolution
   at all, which stays exactly as conservative as it is today. Prevents
   recurrence for any *future* ingestion; does not by itself clean up
   `fir-1001-26`'s already-written 692 nodes.
2. **Cleanup — a script (same shape as
   `scripts/cleanup_orphaned_person_nodes.py`'s existing precedent) that
   walks `pending`/`flagged_unverified` `SAME_AS` edges where both
   endpoints share the same `source_doc_id` AND `case_id`, and
   auto-confirms + collapses them.** Narrower and safer than a general
   "bulk-confirm the review queue" tool — same-document, same-case,
   already-flagged-as-near-identical is a materially safer auto-confirm
   condition than an arbitrary pending edge. Needed regardless of
   whether (1) ships, to actually fix `fir-1001-26`'s already-ingested
   state; (1) alone only stops the bleeding for new ingestions.
3. *(Rejected as this module's own fix)* Tuning `community_detection.py`'s
   edge weights or Louvain's `resolution` parameter — treats the
   symptom (a dense projected graph) without touching the actual
   defect (692 nodes that should be a handful), and risks distorting
   every other, correctly-sized community's clustering along with it.

**For B (`قبضے` mistagged as Person):**
1. Add it (and any other common nouns found by the same audit) to
   `_NON_NAME_PHRASES` — cheap, consistent with this module's own
   established (self-documented-as-imperfect) pattern, but leaves the
   bad node in the graph for every OTHER consumer (XGRAPH, XAGG, the
   review queue itself).
2. A precision fix at the extraction layer (`src/extraction/ner.py` /
   `domain_entities.py`) — e.g. a stoplist check or a minimum-
   corroboration gate for a bare single-word `person` candidate with no
   supporting context — closes the gap for every downstream consumer at
   once, not just community detection.

### Files likely touched
- `src/ingestion/service.py` — widen `resolved_persons`'s scope (fix A1).
- New `scripts/collapse_same_document_duplicate_persons.py` or similar —
  the cleanup pass (fix A2), same dry-run/apply convention
  `scripts/cleanup_orphaned_person_nodes.py` already establishes.
- `src/graph/community_detection.py` (`_NON_NAME_PHRASES`) and/or
  `src/extraction/ner.py` — fix B, depending which option is chosen.
- New tests for A1 (same-document repeat mention reuses one `entity_id`,
  cross-document/cross-case mentions are unaffected) and the cleanup
  script (dry-run vs. apply, same-document+same-case guard only).

### Test plan
- Unit: a fixture document with the same `canonical_name` mentioned N
  times in one ingestion run writes exactly one `Person` node, not N —
  and a DIFFERENT document/case with the same name still goes through
  the existing (unchanged) name-fallback path, proving A1 doesn't widen
  cross-document merging.
- Live verification: re-run `detect_communities()` against the real
  graph after A1+A2 land; the `fir-1001-26` community should shrink to a
  small handful of members, graph density should drop sharply, and — the
  actual point of tracing this from Module 9 in the first place —
  `louvain_partitions()` should now have room to produce ≥2 real levels
  on a graph with real per-case rather than one degenerate mega-cluster.
- Full backend suite green, harness compliance suite green.

---

## Not included here (deliberately out of scope)

Two things surfaced during this sweep that were **not** part of the 6
reported findings and are not modules above — noted for visibility only:

- **Duplicate placeholder-officer nodes**: `"(نامزد ASI)"` (an unnamed
  officer placeholder) appears as 6-7 *distinct* `Officer` graph nodes for
  a single case (`fir-233-26`), each with its own entity_id. Possibly
  correct (each really is a separate mention with no stable identifying
  key to merge on) or possibly an entity-resolution gap for placeholder
  names specifically — flagged as a possible contributing factor to
  Module 3, not confirmed as its own bug.
- **Local LLM reliability**: `"Local LLM failed: Local LLM returned empty
  content"` fired repeatedly during this session's live testing, forcing
  fallback to Groq's free tier, which then hit its own 8000 TPM rate limit
  under moderate testing load. An infrastructure/reliability concern, not
  a code bug in this repo.
