# Muhafiz — Gold-QA Failures: Root Causes & Implementation Plan

**Basis.** The team's manual Gold-QA testing report (32 questions, `Muhafiz_Testing_Report.pdf`).
Of those, **10 are Knowledge-Base questions that are *expected* to fail** because the legal
document corpus (CrPC 1898, Police Order 2002, etc.) is not loaded in this deployment —
those are excluded from the modules below (Module 5 only fixes their *error message*,
not their answer). The remaining ~22 database-answerable questions largely failed for
four distinct, code-level reasons, traced to the actual code below.

**Headline verdict on the 22 non-KB questions:** ~1 clean pass (S2), ~3 partial
(D1, CR2, M5), the rest wrong or not-answered — not a measurement artifact, four
concrete defects, all fixable.

**Delivery discipline.** Each module below ships on its own branch, off `main`, reviewed
and merged independently — same convention already used throughout this repo's history
(one focused branch per fix). Do not batch modules into one branch/PR. Author identity
for every commit is `rayyanfaisal475207` (already the configured git identity for this
repo); do not use "Claude" as the commit author.

| # | Module | Branch | Priority | Data dependency |
|---|--------|--------|----------|------------------|
| 1 | XAGG aggregate accuracy | `fix/xagg-accused-total-vs-recurring` | **P0 — worst failure mode** | None for 1a/1b/1c. Gender (1d) needs an ingestion backfill. |
| 2 | Router determinism gaps | `fix/router-station-count-and-legal-text-override` | P1 | None |
| 3 | Cross-case-with-no-case-selected guard | `fix/case-summarization-no-case-selected-guard` | P1 | None |
| 4 | Verifier over-rejecting structured paraphrases | `fix/verifier-structured-aggregate-paraphrase` | P2 | None |
| 5 | "Knowledge base not loaded" messaging | `fix/rag-kb-not-loaded-message` | P3 | None |
| — | Slow multi-hop responses (3–9 min) | *(not in this plan)* | P4, performance-only | None |

Ship in the priority order above — Module 1 alone eliminates the single worst failure
mode (confidently wrong numbers with no caveat), and each later module is independent
of the others, so slipping one doesn't block the rest.

---

## Module 1 — XAGG: stop returning confidently wrong numbers

**Branch:** `fix/xagg-accused-total-vs-recurring` · **File:** [src/pipeline/xagg.py](src/pipeline/xagg.py)

### Problem (report §2.2)
"How many accused persons in total" → **4** (real ≈ 94). "How many of the accused are
women" → the same 4 people, gender silently ignored. "Which district has the most FIRs"
→ "no district-level data," though the field exists upstream.

### Root cause
`run_aggregate()` dispatches on **hardcoded keyword families**. `_PERSON_KEYWORDS`
(line 36) includes `"accused"`, so *any* query containing that word — including a plain
total-count question — reaches `_top_recurring_nodes("Person", ...)` (line 585-587),
which by construction (`if len(cases) > 1` at line 279) returns **only people appearing
in more than one case**. This isn't a fallback misfire; it's the intended
repeat-offender path being hit by a different question shape. Gender has **no aggregate
family at all** — not a bug in the keyword matching, a genuine gap: `gender` exists on
the upstream `psrms.fir_accused`/`fir_witness` tables (`muhafiz_schema.dbml.txt` lines
208, 250) but `_person_mention()` in `src/graph/structured_projection.py` (line 394)
never extracts it — only `cnic`/`father_name`/`address_text`/`phone` are pulled through.
District, by contrast, *is* available today: `District`/`PoliceStation` graph nodes
already exist (`structured_projection.py` — `District`, `PART_OF` edges), so this is a
graph query away, not a schema gap.

### Fix design

**1a — Separate "total accused" from "recurring persons."**
Add `_RECURRENCE_SIGNAL_KEYWORDS` (recurring/multiple cases/more than one case/several
cases/repeat/bar bar/دوبارہ/بار بار) and `_ACCUSED_TOTAL_KEYWORDS` (total/grand
total/how many accused/kul kitne/کل تعداد). In `run_aggregate()`, **before** the
`_PERSON_KEYWORDS` recurrence dispatch, check: query matches `_PERSON_KEYWORDS` AND
matches `_ACCUSED_TOTAL_KEYWORDS` AND does **not** match `_RECURRENCE_SIGNAL_KEYWORDS`
→ dispatch to a new `_total_accused_count()` instead. Implement it as a Cypher count of
**distinct** `Person` nodes with an `INVOLVED_IN {role: "accused"}` edge into an
`Incident` that `BELONGS_TO_CASE` a case in scope (dedup via
`build_canonical_map`/`canon`, same mechanism `_top_recurring_nodes` already uses) —
this reuses the existing entity-resolution machinery in this file rather than adding a
new one, and reports a real cross-case accused headcount instead of a recurrence-only
subset.

**1b — Refuse instead of silently answering the wrong question.**
Add an early check, before any entity-recurrence dispatch, for topics with **no**
data path today: age (`_AGE_KEYWORDS`), officer assignment (`_OFFICER_KEYWORDS`),
trend/reporting-delay (`_TREND_KEYWORDS`). When matched, return
`{"kind": "unsupported_aggregate", "message": <topic-specific string>}` — the
orchestrator/response formatter renders this as a plain, honest refusal instead of
letting the query reach `_station_or_category_counts()`'s generic default, which
would answer a question it was never asked. This is the direct fix for the report's
worst finding (a wrong number, presented as fact, with no caveat).

**1c — District aggregate.**
New `_top_districts_by()`: Cypher over `(Case)-[]->(PoliceStation)-[:PART_OF]->(District)`
(already written by `structured_projection.py`), grouped by district, optionally
filtered to a named entity type (weapon/case count). Dispatch on `_DISTRICT_KEYWORDS`
("district", "zila", "ضلع") the same way `_STATION_KEYWORDS` triggers the existing
station path.

**1d — Gender aggregate (has a real ingestion dependency — see the data-dependency
column above).**
- *Ingestion* (`structured_projection.py`): extend `_person_mention()` to pull `gender`
  (and `age`, same table, same currently-dropped shape) into the mention dict; write it
  as a `Person` node property in `_write_accused()`/`_write_witnesses()`. Requires a
  **re-sync/backfill** of already-ingested FIRs — a schema-forward change alone will not
  retroactively populate the property on existing nodes.
- *Query* (`xagg.py`): new `_gender_breakdown()`, dispatched on `_GENDER_KEYWORDS`
  ("gender", "women", "female", "عورت", "خواتین", "مرد"), counting `Person` nodes by the
  new `gender` property. Until the backfill runs, return
  `{"kind": "gender_breakdown", "unsupported": True, "message": <"not yet synced" string>}`
  rather than an empty/wrong result — an honest "not populated yet," not a silent zero.
- **Land 1a/1b/1c and 1d in the same branch is fine**, but call out in the PR
  description that 1d's results stay "not yet synced" until the separate backfill runs
  — don't let that read as still-broken in review.

### Acceptance criteria
- "How many accused persons in total" returns a real total, distinctly labeled as
  different from "how many people are repeat offenders."
- "How many of the accused are women" returns either a real breakdown (post-backfill)
  or the explicit "not yet synced" message — never the 4-person recurrence list.
- "Which district has the most FIRs" (or most weapons) returns a real ranking.
- Age/officer/trend questions return the explicit unsupported-aggregate message, not a
  crime-category breakdown or any other unrelated number.
- `test_xagg.py` gets new cases for: total-vs-recurring disambiguation, the refusal
  path, district aggregate, gender (both pre- and post-backfill shape).

---

## Module 2 — Router: close the two confirmed deterministic-pre-check gaps

**Branch:** `fix/router-station-count-and-legal-text-override` · **File:** [src/pipeline/router.py](src/pipeline/router.py)

### Problem (report §2.3)
"How many police stations are there" routed to document search, failed. "What does
Section 154 CrPC say" routed to SQL, which has no legal text. The same question (M5)
routed differently across runs.

### Root cause
The deterministic regex layer (`_deterministic_route_override`, lines 39–321) already
covers a lot of ground and already runs the LLM fallback at `temperature=0.0` (line
352) — routing is *already* reproducible for anything the regex layer catches. The
non-determinism the report saw is specifically for queries that still fall through to
the LLM classifier, because two shapes have no regex coverage yet:
- `_XAGG_OVERRIDE_PATTERNS` (line 66) matches `how many ... cases?` — a station-count
  question contains neither "cases" nor any other existing pattern, so it falls to the
  LLM, which (per the report) sends it to document search.
- `_SQL_OVERRIDE_PATTERNS` (line 158) is tuned for *"which section applies"* lookups
  ("PPC section", "cognizable offense"), not *"what does Section N say"* — a
  legal-text-content question that needs the document/RAG route, not SQL.

### Fix design
- Add `\bhow many\b.{0,20}\b(police )?stations?\b` (plus Urdu/Roman-Urdu equivalents,
  matching this file's existing bilingual pattern convention) to the XAGG override list,
  dispatching to a station-count aggregate (reuse `_station_or_category_counts` with
  `group_field` forced to `police_station`, or `_total_count`'s shape for a bare
  number).
- Add a distinct pattern —
  `\b(what does|explain|text of)\b.{0,20}\bsection\b.{0,10}\d+.{0,20}\b(crpc|ppc|say|state)\b`
  (+ Urdu/Roman-Urdu equivalents) — routed to **RAG**, not SQL. This question will still
  come back "not found" until the KB is loaded (Module 5 makes that message honest); the
  defect being fixed here is specifically the *misroute to SQL*, which is a separate,
  fixable problem regardless of KB status.
- No temperature/caching changes needed — already deterministic where regex-covered;
  these two additions just grow that coverage, closing the M5-style flapping for these
  specific shapes.

### Acceptance criteria
- "How many police stations are there" (and Roman/Urdu equivalents) returns a real
  count on every run.
- "What does Section 154 CrPC say" routes to RAG (verify via router logs/tests), not
  SQL — independent of whether it can yet be *answered* (Module 5/KB question).
- Re-run M5 five times; confirm identical routing across runs.

---

## Module 3 — Guard the within-case summarizer against "no case selected"

**Branch:** `fix/case-summarization-no-case-selected-guard` · **File:** [src/pipeline/harness/agents/case_summarization.py](src/pipeline/harness/agents/case_summarization.py)
(plus its dispatcher — locate the call site during implementation; grep the harness
orchestration layer for where this sub-agent is selected).

### Problem (report §2.1)
12/32 questions returned "No sufficiently relevant documents were found" or "No case
documents or case graph data were found to summarize." Six of those (CR3, CR4, G2, G3,
G5, G6) needed **only** the case database and should have worked.

### Root cause
CR3/CR4/G2/G3/G5/G6 are **cross-case, multi-record** questions (compare two cases,
weapon→person→outcome across cases, analytical-over-caseload). The router sends them to
the within-case summarizer (`case_summarization.py`, whose `EMPTY` status +
`"No case documents or case graph data were found to summarize."` caveat, line ~386,
is the exact string the report quotes). With no single `case_id` active (e.g.
"All Cases" selected), that route has nothing to summarize and returns empty — the
retrieval layer is fine, the route is wrong. Reproduced live: the same underlying data
asked as "what weapons appear in the records" (routes to XAGG) returns correct data;
asked as "who was the weapon taken off" (routes to the within-case summarizer) returns
the empty caveat.

### Fix design
At the dispatch point (locate via the harness agent-selection call site), add a guard:
**if the resolved route is the within-case summarizer AND there is no active `case_id`**:
- If the question shape matches a cross-case pattern already covered by Module 2's
  routing categories (XAGG/RAG-all-cases), re-route there instead.
- Otherwise, return a distinct guidance message — *"select a case to summarize, or ask
  this as a cross-case question"* — instead of falling through to the generic empty
  caveat, which currently reads as "no data exists" rather than "wrong route for this
  context." This directly answers the report's own complaint that the current message
  is misleading.

### Acceptance criteria
- CR3, CR4, G2, G3, G5, G6 (asked with no case selected / "All Cases") return either a
  real cross-case answer or the new guidance message — never the generic empty-summary
  caveat.
- The same questions, asked *with* a case selected where that's the intended scope,
  are unaffected (guard only fires on the no-case-id + within-case-summarizer
  combination).

### Follow-up — the actual upstream fix landed (`src/pipeline/router.py`, `prompts/router.txt`)
The guard above patches the *symptom* (a misroute that already happened); it never
re-routes an open-ended, no-case-named question like CR3/CR4/G2/G6 to a real
cross-case answer on its own — those need the router's own classifier to recognize
them as cross-case in the first place. That upstream fix is now done: when
`route_query()` is called with no `case_id`, the LLM classifier's input is prefixed
with an explicit `ACTIVE_CASE: none` line, and `router.txt` gained a new,
narrowly-scoped rule — checked first against the literal presence of that line, so it
never touches the well-tested active-case path — instructing the model that
GRAPH/GRAPH_HYBRID cannot apply with no case active and no case named, and to pick
XAGG/XGRAPH/XNETWORK based on the question's real content instead (a semantic
judgment, not a keyword match — this is what lets it work even when the case_id truly
isn't mentioned anywhere in the query).

Live-verified, same-session, before/after:
- CR4-shaped ("weapon logged as evidence, who was it taken off") — **GRAPH_HYBRID →
  XGRAPH**, the exact misroute this module describes, fixed.
- CR3-shaped (compare two unnamed cases) — **RAG → XNETWORK**.
- G6-shaped (orientation note from the caseload) — **RAG → XNETWORK**.
- A held-out active-case regression check (`case_id` set, "summarize this case") —
  confirmed via git-stash A/B testing that its RAG/GRAPH_HYBRID flakiness is
  **pre-existing** (present identically on the unmodified prompt), not introduced by
  this change; a battery of 7 other within-case and cross-case sanity queries all
  classified correctly after the fix.

Still not fully solved by this: G2/G3/G5's own creative-generation *quality* (a
correct route reaching an open-ended prompt doesn't guarantee XNETWORK's synthesis is
good), and the pre-existing router non-determinism itself (temperature 0 does not
guarantee identical output run-to-run on this local model) — this fix changes what
the classifier is told, not the classifier's own sampling behavior.

---

## Module 4 — Verifier: stop dumping raw JSON for structured-aggregate answers

**Branch:** `fix/verifier-structured-aggregate-paraphrase` · **Files:**
[src/pipeline/verifier.py](src/pipeline/verifier.py) (new `verify_structured_aggregate_paraphrase()`),
[src/pipeline/harness/agents/large_scale_aggregate.py](src/pipeline/harness/agents/large_scale_aggregate.py)

### Problem (report §2.4)
"Has anyone been arrested more than once" → "Entity-graph search found connections
across 6 cases, chain confidence 50%" + raw case IDs, never a plain yes/no answer with
names. "4 matching Person(s) found... showing the raw computed aggregate instead."

### Root cause — corrected during implementation
The XAGG half of this is exactly as diagnosed: `large_scale_aggregate.py` generates an
NL paraphrase of a computed aggregate, runs it through `verify_grounding()` (tuned for
free-text claims grounded in narrative document chunks), and **falls back to the raw
computed aggregate** whenever the free-text judge can't confirm the paraphrase — firing
too often because the numbers are grounded by construction (they came from the query),
not from an LLM claim the strict judge should be scrutinizing.

The XGRAPH half (the literal "chain confidence 50%" example the report quotes, from
`cross_case_linkage.py::_xgraph_summary_line()`) is **not** this defect: that line is
explicitly documented as deterministic and **never run through the Verifier at all** —
a deliberate prior fix (the module's own "verify-log Finding AC") that intentionally
avoids stating graph-resolved identity as flat fact, after an earlier version overclaimed
certainty. Rewriting it into a flat "Yes — the following people..." answer would undo
that fix. This module therefore does **not** touch `cross_case_linkage.py`'s XGRAPH
output; XNETWORK's own `verify_grounding()` calls in that file are a legitimate
narrative-paraphrase-of-retrieved-text shape (community-summary chunks), not the
computed-aggregate shape this module targets, and are also left unchanged.

### Fix design
- New `verify_structured_aggregate_paraphrase()` in `verifier.py`, used only by
  `large_scale_aggregate.py`: keeps the two *security-relevant* deterministic checks
  (cross-case leakage, fabricated case-id citations) unconditionally, and replaces the
  free-text LLM judge with a deterministic numeric-consistency check — every number the
  paraphrase states must appear in the aggregate's own computed source text. Does
  **not** touch `verify_grounding()` or any of its narrative call sites (RAG/GRAPH/SQL/
  WEB/XGRAPH/XNETWORK).
- Citation markers (`[Document N, ...]`) are stripped before the numeric check so a
  citation index or a cited case-id's own digits are never mistaken for a claimed
  figure.

### Acceptance criteria
- "How many of the accused are women" (once Module 1 lands) returns its NL summary
  directly, without the "could not be verified as an accurate paraphrase" caveat
  firing on a deterministic count.
- A paraphrase that invents a number not present in the computed result still fails
  (verified by test — this is a relaxation of the free-text judge, not a removal of
  grounding entirely).
- `verify_grounding()`'s behavior on every other route (RAG/GRAPH/SQL/WEB/XGRAPH/
  XNETWORK) is completely unchanged (regression-test the existing Verifier suite).
- "Has anyone been arrested more than once" (XGRAPH) is **not** in this module's scope
  — its deterministic, unverified summary line is a deliberate, already-fixed design
  choice; a future readability pass on its phrasing (without reintroducing flat
  identity claims) is separate work, not tracked here.

---

## Module 5 — "Knowledge base not loaded" messaging

**Branch:** `fix/rag-kb-not-loaded-message` · **File:** wherever the RAG route's
empty-result message is currently generated (locate during implementation — the
retrieval layer that returns "No sufficiently relevant documents were found").

### Problem (report §2.1, closing note)
The system does not tell the user the legal knowledge base isn't loaded; it reports a
generic retrieval failure that looks like a transient issue rather than missing
content. This affects the 10 genuinely-KB-dependent questions (KB1, KB3, legal-citation
questions) and any general legal question ("What is an FIR").

### Fix design
Gate on the legal-document corpus actually being empty (not a blanket message that
would also fire for a genuinely-missing case document): when a query resolves to the
legal-document/RAG route and that specific corpus has zero ingested documents, return
*"The legal knowledge base (CrPC, Police Order, etc.) is not loaded in this
deployment, so this question can't be answered from statute text yet."* instead of the
generic "no relevant documents found."

### Acceptance criteria
- KB1, KB3, "What does Section 154 CrPC say" (post-Module-2 routing fix), and "What is
  an FIR" all return the distinct KB-not-loaded message.
- A genuinely missing *case* document (unrelated to the legal corpus) still returns the
  existing generic message — this gate must not fire for that case.

---

## Module 6 — XGRAPH's own recurring-entity label keywords missed "accused"/"ملزم"

**Branch:** `fix/graph-retriever-label-keywords-accused` · **File:**
[src/retrieval/graph_retriever.py](src/retrieval/graph_retriever.py) (`_LABEL_KEYWORDS`)

### Problem
G1/S3-style "has anyone been arrested more than once" questions (report §2.1/§2.3)
came back "no cross-case patterns found," even asked directly against the exact
report wording, in both English and Urdu. Initially suspected as a graph-quality/data
gap (this session's own `sync_muhafiz_data.py` run had flagged 31.6% node drift /
52.5% edge drift in community detection) — investigated live before assuming that.

### Root cause
Not a data gap. `_find_recurring_entities_for_query()` (the function `retrieve_graph()`
calls to seed a cross-case recurrence search with no named entity) picks which graph
label(s) to search via `_LABEL_KEYWORDS` — a **separate, hand-maintained keyword list**
from `xagg.py::_PERSON_KEYWORDS`, which already includes "accused"/"recidivist"/
"mulzim"/"shakhs"/"ملزم" (fixed in an earlier session's own regression guard —
see that list's own comment on "لوگ"). `_LABEL_KEYWORDS["Person"]` never got the
same fix — it only had `("person", "people", "suspect", "offender", "شخص", "افراد",
"لوگ")`. "Has any **accused** been arrested more than once" and "کیا کسی **ملزم**
کو..." both name no other label keyword, so `labels` came back empty and the function
returned `[]` unconditionally — never reaching the Cypher query that would have found
the graph's real recurring accused (live-verified: 4 real recurring Person nodes,
matching `xagg.py`'s own `_top_recurring_nodes("Person")` result for the same corpus).

### Fix
Brought `_LABEL_KEYWORDS["Person"]` into parity with `xagg.py::_PERSON_KEYWORDS`.
Live re-verified before/after through the real harness `xgraph_tool()`: both the
English and the report's own exact Urdu phrasing went from 0 seed entities/EMPTY
status to 4 seed entities/OK status with 39 evidence chunks.

### Acceptance criteria
- "Has any accused been arrested more than once" (English and Urdu) returns real
  cross-case results, not "no patterns found."
- Existing Person-recurrence behavior (the everyday-Urdu-word "لوگ" fix, enumeration
  vs. recurrence distinction) is unaffected — regression-tested.

---

## Not in this plan — performance

Report §2.5: G5 ~9 min, KB3 ~5 min, KB1 ~4.5 min — all multi-hop cross-case/analytical
queries, root-caused to sequential tool fan-out on complex routes. Real, but a
performance task orthogonal to the five correctness modules above — track and fix
separately (parallelize independent tool calls, cap retry loops) once the modules above
are in.

## Setting expectations on the full 32-question Gold-QA set

Even with all five modules merged, **10 questions will still not have a correct
answer** — they need the legal knowledge base loaded, which is a data/deployment task,
not a code fix, and is out of scope for this plan. What changes for those 10 is that
the system will say so honestly (Module 5) instead of looking like a transient
failure. The other ~22 questions are the target of full correctness here.
