# Agent Harness — Implementation Plan

**Status:** Ready to build. Companion to [`AGENT_HARNESS_DESIGN.md`](AGENT_HARNESS_DESIGN.md) and
[`SUBAGENT_INTERFACES.md`](SUBAGENT_INTERFACES.md), which remain the source of truth for the type
contracts. This doc is the single, consolidated build plan: what gets built, in what order, by
which module, and what each piece actually does.

**Scope:** 11 modules total — the shared foundation, the Supervisor, 8 routable sub-agents, and 3
trust-layer checks that sit inside those sub-agents' flow.

---

## 1. The Supervisor — what it is and what it does

The Supervisor is the single entry point every question goes through. It does three things and
nothing else:

1. **Classifies** the incoming question against the 8 sub-agents below (reuses the existing
   classification logic in `router.py` rather than reinventing it).
2. **Carries identity/scope through unchanged** — who is asking, what role they hold, which case
   they're scoped to — passed to whichever sub-agent it dispatches to, untouched.
3. **Receives one bounded result back** from that sub-agent and returns it. It never talks to more
   than one sub-agent per question, never touches a tool directly, and never sees raw evidence —
   only the sub-agent's already-summarized answer.

It is deliberately "dumb" — all the actual intelligence (search, summarization, verification) lives
one layer down, inside the sub-agents. This keeps the Supervisor simple and auditable: if something
went wrong, you know it's in a specific sub-agent, not in a tangle of shared dispatch logic.

```
                     Supervisor (classify + dispatch, nothing else)
                            │
     ┌───────┬───────┬───────┬───────┬───────┬───────┬───────┬───────┐
     ▼       ▼       ▼       ▼       ▼       ▼       ▼       ▼
 Semantic  Large-  Case    Timeline Cross-  Investig. Report  Data-
 Search    Scale   Summar. Building Case    Analysis  Drafting Quality
           Aggreg.                  Linkage
     │       │       │       │       │       │       │       │
     └───────┴───────┴───────┴───┬───┴───────┴───────┴───────┘
                                  ▼
                    Tools (RAG · GRAPH · XGRAPH · XAGG · XNETWORK · SQL · WEB)
                                  │
                                  ▼
              Trust layer, invoked by the sub-agents above (not by the Supervisor):
              Verifier → Citation-Consistency (Report Drafting only) → Validation
```

---

## 2. Foundation layer — build first, everything else depends on it

| Module | Path | What it does |
|---|---|---|
| Shared types | `src/pipeline/harness/types.py` | The standard shapes every module speaks: evidence chunks, tool results, sub-agent results, statuses. Includes the confidence field split — `ChunkMetadata.confidence` (a number, only when actually computed) plus `confidence_status` (`computed` / `not_computed` / `check_failed`), so a missing confidence score can never be mistaken for "checked and fine." |
| Tool wrappers | `src/pipeline/harness/tools/*.py` | One thin wrapper per existing retrieval mechanism — RAG, GRAPH/GRAPH_HYBRID, XGRAPH, XAGG, XNETWORK, SQL, WEB. No new logic: each just packages an already-working function into the standard shape above. |
| Compliance test suite | `src/pipeline/harness/compliance/` | Automated checks for the 5 security rules every tool/sub-agent must respect (case-role checks fire before cross-case data is touched, no template skips its case filter, role always comes from the real account not a profile object, etc.). Every module below must pass this before it's considered done. |

---

## 3. Supervisor module

| Module | Path | Depends on |
|---|---|---|
| Supervisor / dispatcher | `src/pipeline/harness/supervisor.py` | Foundation layer |

---

## 4. The 8 sub-agents — build in this order

| # | Sub-agent | Path | Uses | What it does | Build notes |
|---|---|---|---|---|---|
| 1 | **Semantic Search** | `agents/semantic_search.py` | RAG | Finds and cites relevant passages for a question | Simplest slice — build and prove the whole pipeline works end to end before anything else |
| 2 | **Large-Scale Aggregate** | `agents/large_scale_aggregate.py` | XAGG | Area/time/crime-type rollups and counts | Machine-computed by construction; Validation only needs its cheap structural check here (does the reported count match the raw rows) |
| 3 | **Case Summarization** | `agents/case_summarization.py` | RAG + GRAPH | One structured summary per case: status, key entities, key events, open questions | Degrades symmetrically either direction (document-only or graph-only) rather than failing outright; a graph-only summary discloses that fact in its own text so it's never mistaken for a full one |
| 4 | **Timeline Building** | `agents/timeline_building.py` | GRAPH (dated events) + conflict detection | Ordered event list per case, with each event flagged for contradictions | Conflict status is three-state (found / none / not-checked) — never silently claims "no conflicts" when the check itself failed |
| 5 | **Cross-Case Linkage** | `agents/cross_case_linkage.py` | XGRAPH + XNETWORK | Finds shared entities (vehicle, phone, person) and shared patterns/MO across cases | Restricted to supervisor-role-and-above; unconfirmed identity matches are always shown as caveats, never asserted as fact; Validation's full semantic check is mandatory here — this is the highest-stakes claim type in the system |
| 6 | **Investigative Analysis** | `agents/investigative_analysis.py` | RAG + GRAPH + SQL | Deep synthesis answer pulling from all three sources at once | Runs the three tool calls **in parallel**, not one after another, cutting response time; reports honestly which sources actually contributed vs. which quietly fell back to another (so "3 sources agree" is never claimed when it was really 1); Validation mandatory |
| 7 | **Report Drafting** | `agents/report_drafting.py` | Case Summarization's output + document builders (PDF/XLSX/DOCX) | Produces a formal, downloadable case report | Never re-queries tools directly — only drafts from what Case Summarization already produced, so the report can't drift from what a user already saw. Runs a citation-consistency check before verification (confirms citation numbers didn't shift when the text was recomposed), and if the underlying summary was incomplete, prints a plain disclosure line inside the document itself — not just buried in a status field. Validation mandatory |
| 8 | **Data-Quality / Extraction-Coverage** | `agents/data_quality.py` | Postgres + graph metadata | Reports how much evidence actually exists for a case (documents ingested, entities resolved, dated events found) | New capability — explains *why* another agent's answer was thin, instead of leaving it unexplained |

---

## 5. Trust layer — the checks that run inside the sub-agents above

| Module | Path | Runs on | What it catches |
|---|---|---|---|
| **Verifier** | `verifier.py` | Every sub-agent that produces `answer_text` | Is this answer actually grounded in the evidence shown — real citations, no case-boundary leakage, low-confidence evidence properly hedged. Fails closed: an answer that doesn't pass is never shown; a safe "can't answer" is shown instead. |
| **Citation-Consistency** | `citation_consistency.py` | Report Drafting, before the Verifier runs | Did citation numbers stay correctly pointed at the right evidence when Report Drafting recomposed Case Summarization's output |
| **Validation** | `validation.py` | After the Verifier passes, on all 8 sub-agents (full semantic re-check mandatory on Cross-Case Linkage / Investigative Analysis / Report Drafting; lighter structural-only check elsewhere) | Does each claim actually say what its cited evidence says (not just technically cited to it), and are numeric/date/confidence results internally consistent |

---

## 6. Rollout strategy

**Decision: build alongside the current `orchestrator.py`, switch on one sub-agent at a time.**
`orchestrator.py` keeps serving live traffic throughout. Each sub-agent, once built and passing the
compliance suite, is wired in behind a route/flag and only takes over its slice of traffic once
proven — never a single big-bang cutover. This means:

- The Supervisor can coexist with the old `if/elif` router during the transition — questions not
  yet routed to a finished sub-agent keep going through the existing path.
- Each of the 8 sub-agents in §4 gets its own go-live moment, not one shared one. Order of build
  (§4's table) can double as order of cutover, but doesn't have to — a sub-agent can be built and
  sit behind its flag for a while before being switched on.
- `orchestrator.py` is only retired once every sub-agent it currently handles has a proven
  replacement live.

---

## 7. Resolved decisions (MVP target: on-premise, air-gapped-by-default deployment)

These four items were open after §4–§5 were first written. Resolved below, reasoned against the
actual deployment target — Muhafiz runs inside police infrastructure, so air-gapped is the
**default** operating mode, not an edge case to special-case around.

### 7.1 Validation's semantic-check mechanics

- **Unit of checking = the Verifier's own citation parse, reused as-is.** The Verifier already
  splits `answer_text` on `[Document N]` markers to run its deterministic checks. Validation reuses
  that exact split — each cited sentence/sentence-group paired with the chunk its marker points at
  — rather than inventing a second segmentation method. This guarantees the Verifier and Validation
  never disagree about what text maps to what evidence. Uncited text is the Verifier's problem, not
  Validation's.
- **Method: three-way entailment per pair** — `SUPPORTED` / `PARTIALLY_SUPPORTED` / `NOT_SUPPORTED`
  + one-line reason. Three-state for the same reason `ConflictState` and `confidence_status` are:
  a binary can't distinguish "checked and fine" from "couldn't tell."
- **One batched call per answer**, all pairs at once, structured JSON out (same pattern as
  `json_extract.py`) — not one call per claim.
- **Fails OPEN, not closed — the opposite posture from the Verifier, and deliberately so.** If the
  Validation call itself errors, the answer has *already* passed full grounding verification;
  blocking it on a flaky second-opinion service would push users toward workarounds. Correct
  behavior: serve the answer, `validation_status="not_run"`, attach a caveat.
  **Exception: Report Drafting.** A generated document outlives the session — there, a failed
  Validation run renders as a disclosure line in the document body (reusing the existing
  §2.1.1–§2.1.3 disclosure mechanism), not just a silent status field.

### 7.2 Air-gap mode for Validation's LLM call

- **Local-only, permanently. No cloud-escalation path — not even as an opt-in.** If Validation only
  works well with cloud access, the safety gate is degraded in the platform's actual primary
  deployment mode. XNETWORK earned its cloud-escalation exception with measured evidence (3/3 local
  failures on an open-ended synthesis task); entailment classification is a materially easier task
  shape (constrained labels, short inputs), so local should hold up — but that's an expectation,
  not yet a fact.
- **Pre-work required before `validation.py` ships:** hand-build ~30 (claim, cited-chunk) pairs from
  real case data with known labels, including deliberately overstated claims of the kind this check
  exists to catch, and run the local model against them.
  - Passes reliably → ship local-only as designed.
  - Doesn't → **narrow the check's scope** (e.g. numeric/name/date contradiction detection only)
    rather than adding cloud escalation. The air-gap guarantee is non-negotiable for this feature;
    the check's ambition is the variable to give up first.

### 7.3 Data-Quality agent — final shape

- **Pure structured stats. No LLM narration for MVP.** This is the one agent in the system that
  structurally cannot hallucinate, because it never generates prose — worth preserving deliberately.
- **Metrics** (named by capability, not by table, so this survives the PRSM schema swap):

  | Metric group | Reports | Explains |
  |---|---|---|
  | Document coverage | Ingested count by type; failed/quarantined count | Thin Semantic Search results |
  | Entity extraction | Node counts by type (person/vehicle/phone/address/org/weapon) | Sparse graph traversal |
  | Timeline readiness | Count of dated incident events | Empty Timeline Building output |
  | Identity health | Pending vs. confirmed vs. rejected identity matches | Heavy caveats on cross-case links |
  | Conflict coverage | Whether conflict detection has run for this case | `UNKNOWN` conflict states |
  | Embedding coverage | Chunks embedded vs. documents ingested | Retrieval gaps |

- **No absolute "enough" thresholds for MVP** — no calibration data exists yet to set them
  correctly, and a wrong threshold is worse than none. Report raw counts plus a per-capability
  readiness state: `ready` / `thin` / `unavailable`.
- **Routable-only for MVP.** Auto-attaching "here's why this was thin" to other agents' degraded
  outputs is valuable but adds coupling on every degraded path across all 8 sub-agents — parked,
  not rejected.

### 7.4 Disclosure strings

Proposed wording, to give product sign-off something concrete to react to:

> **Graph-only summary:** "This summary was generated from case record links only — no document
> text was available for this case. It may omit details contained in case documents."

> **Partial evidence:** "This report was generated from incomplete evidence. The following could
> not be retrieved: {sources}. Findings should be treated as provisional."

Two implementation requirements surfaced while drafting these, both easy to miss:

- **`{unavailable_sources}` must route through `SOURCE_TOOL_DISPLAY_LABELS`**
  (`SUBAGENT_INTERFACES.md` §1.3.1), never the raw `degraded_from` enum values. Unmapped, the
  document would literally read "unavailable: GRAPH" — meaningless to an investigator.
- **Needs a pre-reviewed translation table, not runtime translation.** `preferred_language` is
  threaded through every route, so a report can be produced in Urdu — but the interfaces doc's own
  rule is that these strings are fixed and human-reviewed, which rules out translating them with an
  LLM at generation time. Sign-off needs to cover one approved string per supported language, not
  just the English sentence.
- **Still genuinely open:** the actual wording sign-off itself, and §7.2's local-model test result.
  Everything else above is now a decision, not a question.

---

## 8. Build checklist

- [x] Foundation — types.py, tool wrappers, compliance suite *(complete — see §9)*
- [x] Supervisor *(complete — see §9)*
- [x] Semantic Search *(complete — see §9)*
- [x] **Contract retrofit — ExecutionContext & ConversationContext (see §10)** *(complete — see §9)*
- [x] Large-Scale Aggregate *(complete — see §9)*
- [x] Case Summarization *(complete — see §9)*
- [x] **Contract amendment — SubAgentResult.events/.links, ConflictState, TimelineEvent (see §11)** *(complete — see §9)*
- [x] Timeline Building *(complete — see §9)*
- [x] Cross-Case Linkage *(complete — see §9)*
- [ ] Investigative Analysis (parallel execution)
- [ ] Report Drafting (citation-consistency check)
- [ ] Data-Quality / Extraction-Coverage
- [ ] Verifier module
- [ ] Citation-Consistency module
- [ ] Validation module
- [ ] Wire compliance suite into CI as a merge gate

---

## 9. Progress log

### Phase 0 — Foundation layer: COMPLETE

Branch `feature/harness-phase-0-foundation`, merged to main via merge commit `6e495a4`.

**Built:**
- `src/pipeline/harness/types.py` — shared types transcribed from `SUBAGENT_INTERFACES.md`,
  including the §1 confidence-field-split amendment (`ChunkMetadata.confidence` +
  `confidence_status`, with a validator enforcing the two stay consistent).
- `src/pipeline/harness/tools/*.py` — one wrapper per tool (RAG, GRAPH/GRAPH_HYBRID, XGRAPH, XAGG,
  XNETWORK, SQL, WEB), each wrapping its existing function verbatim. All `[PRESERVE]` behaviors
  from the design doc confirmed intact (fallback rules, permanently-`False` `fallback_to_rag` on
  cross-case tools, role-gate ordering left inside the wrapped functions rather than hoisted,
  WEB's two-tier fallback, `AIR_GAP_MODE` gating).
- `src/pipeline/harness/compliance/` — pytest suite covering all 5 enforcement points from design
  §4. 51 checks, all passing.

**Two documented deviations from the literal doc text** (low-risk, made because the literal reading
risked a crash/leak — flagged in each file's own docstring, not silent):
1. `XAggToolResult.aggregate_kind` (`AggregateKind` literal) additively includes a real
   `"total_count"` kind that the interfaces doc's `Literal["graph_recurrence",
   "relational_aggregate", "case_listing"]` didn't enumerate — XAGG's existing behavior produces
   this kind and the literal-type would have rejected valid output.
2. `RagToolInput` scoping uses only `CallerContext.active_case_id` / `include_global` — the
   contract carries no `project_id`, so RAG's existing project-scoping precedence rule couldn't be
   threaded through as literally described. **Anyone building a sub-agent that relies on
   project-scoped RAG behavior should check this wrapper before assuming full parity with the
   pre-harness RAG path.**

**Verification:** full existing test suite passes unchanged (one pre-existing, unrelated
docling/PDF `std::bad_alloc` environment failure confirmed present identically on main *before*
this merge). New compliance suite passes in full. `orchestrator.py`/`router.py` untouched — nothing
wired into live traffic yet, per §6.

### Phase 1 — Supervisor: COMPLETE

Branch `feature/harness-phase-1-supervisor`, merged to main via merge commit
`d41d759`.

**Built:**
- `src/pipeline/harness/supervisor.py`:
  - `classify_to_subagent()` — deterministic translation table from
    `router.py::route_query()`'s 9 tool-level routes to the 8 sub-agent
    names. `route_query()` itself is called completely unchanged — no
    reimplementation, no new regex/keyword logic layered on top of it.
  - `register()` / `unregister()` / `get_registered()` / `registered_names()`
    — a module-level, name-keyed sub-agent registry that future sub-agent
    modules populate at import time. `register()` validates the name
    against `SUB_AGENT_NAMES` (the canonical 8) so a typo fails loudly at
    registration time, not silently at dispatch time.
  - `Supervisor.handle()` — classifies, looks up the registered handler,
    threads `agent_input` (and therefore `CallerContext`) through
    completely unchanged, and returns the sub-agent's `SubAgentResult`
    exactly as received, with no reformatting/unwrapping. Accepts an
    optional `registry` override (for test isolation) and an optional
    `on_event` callback that receives the two `PipelineEvent`s emitted
    per dispatch (classification, then outcome).
  - Unregistered-route handling: returns a typed `SubAgentResult` with
    `status=ABSTAINED` and `error.kind="upstream_failure"` — never a
    crash, never a silent fallback to `orchestrator.py` (explicitly out
    of scope this phase per §6).
- `tests/test_harness_supervisor.py` — 22 tests: classification dispatch
  (parametrized over all 9 routes + file-output override), `CallerContext`
  threaded through by object identity, unregistered-route behavior,
  `PipelineEvent` shape/granularity, and the module-level
  `register()`/`unregister()` mechanism.

**One deviation from a literal reading of the brief, resolved with the
user before writing any code (not guessed):** the brief says the
Supervisor classifies against the 8 sub-agent names "by reusing the
existing classification logic in router.py." Read literally that's not
directly possible — `router.py::route_query()` classifies into 9
*tool-level* routes, not sub-agent names, and never did; `orchestrator.py`
was checked too and confirmed to have no sub-agent-shaped branching either.
Resolved via `AskUserQuestion` as the "minimal deterministic map" option:
`route_query()` runs completely unchanged, and a small explicit table in
`supervisor.py` (`_ROUTE_TO_SUBAGENT`) bridges its 9 routes to 8 sub-agent
names using the least-ambiguous available reading, with `output_format`
overriding to Report Drafting. **Consequence, documented in
`supervisor.py`'s module docstring, not hidden:** Timeline Building and
Data-Quality/Extraction-Coverage are not reachable via classification in
this phase — router.py has no signal for either query shape, and adding
new triggers for them was out of scope (that would be new classification
logic, not reuse of the tuned existing logic). Both names are still valid
`register()` targets; wiring real classification for them is future work,
to be done the same evidence-driven way XAGG/XGRAPH/XNETWORK's own
deterministic overrides were — against live-confirmed misclassification
failures, not guessed patterns.

**Discrepancy flagged (not silently resolved):** `SUBAGENT_INTERFACES.md`
§2.1 titles its table "The seven sub-agents" and never lists Data-Quality/
Extraction-Coverage; `AGENT_HARNESS_IMPLEMENTATION_PLAN.md` §4/§7.3 adds it
as an eighth. This module follows the plan's 8-name scope (this session's
explicit brief) — the interfaces doc has not been updated to match.

**Verification:** full existing test suite passes — 873 tests, 0 failures,
0 errors, 4 skipped (the pre-existing, unrelated docling/PDF
`std::bad_alloc` environment failure already documented as present on main
before Phase 0's own merge — confirmed still present identically, not a
regression). Phase 0's compliance suite: 51/51 checks still passing,
unchanged. Nothing in `orchestrator.py`, `router.py`'s existing behavior,
or `main.py`'s live `chat_endpoint` was touched — the Supervisor is not
wired into live traffic, per §6.

### Phase 2 — Semantic Search: COMPLETE

Branch `feature/harness-phase-2-semantic-search`, merged to main via merge
commit `6298d04`.

**Built:**
- `src/pipeline/harness/agents/semantic_search.py` — composes the Phase 0
  RAG tool wrapper (`src/pipeline/harness/tools/rag.py::rag_tool`) only,
  called as-is, never re-wrapped or re-implemented. On a successful RAG
  result it generates one answer via `call_llm()` over the tool's own
  (already bounded/reranked) chunks, prompting for `[Document N]`
  citations, then runs the **existing, unchanged**
  `src.pipeline.verifier.verify_grounding()` (design §5 decision (a)'s
  flat `list[{id, text, metadata}]` shape) before returning anything.
  Returns the standard `SubAgentResult` — bounded to `answer_text` +
  `citations` (no chunk text, no `EvidenceChunk`, no raw rows).
- Partial-failure mapping, per this phase's explicit brief: RAG `FAILED` →
  `ABSTAINED` (error propagated); RAG `EMPTY` with
  `evaluator_verdict=="not_relevant"` (retry loop ran, never got
  "relevant") → `ABSTAINED`, no `answer_text` served; RAG `EMPTY` with no
  evaluator ever run (nothing in scope to search at all) → `EMPTY`, not an
  error; a generated answer that fails `verify_grounding()` →
  `ABSTAINED`, `answer_text` stays `None` even though retrieval itself
  succeeded. `degraded_from` is always `[]` for this sub-agent — it
  composes exactly one tool, and RAG is the fallback *target* for every
  other tool, not a tool with a fallback of its own (design §2.1), so
  there is nothing here for it to degrade *from*.
- `src/pipeline/harness/supervisor.py::register()` — a real `SemanticSearch`
  instance (`semantic_search.name = SEMANTIC_SEARCH`) registers itself into
  the module-level registry at import time, the exact pattern
  `supervisor.py`'s own Phase 1 docstring documented for future sub-agent
  modules.
- `tests/test_harness_agent_semantic_search.py` — successful search with
  citations, evaluator-rejection → `ABSTAINED`, empty-result handling
  (both the evaluator-rejection and nothing-in-scope shapes, kept
  distinct), verifier-rejection of a generated answer → `ABSTAINED`, RAG
  `FAILED` → `ABSTAINED` with the tool's error propagated, and a
  `Supervisor.handle()` → real Semantic Search → real `rag_tool()` →
  `SubAgentResult` integration test (router's `route_query()` and
  `rag_tool`'s own wrapped pipeline functions stubbed to deterministic
  test data, not live infra) proving Phase 1 and Phase 2 actually connect.

**Validation gate — explicitly not wired this phase, per this session's
brief.** `validation.py` (§5/§7.1) does not exist yet, and confirmed
against `src/pipeline/harness/types.py` before writing this module:
`SubAgentResult` has no `validation_status` field — the §7.1 amendment was
proposed but was never actually added to `types.py` in Phase 0. A
`# TODO(phase-3)` marker is left in `semantic_search.py` at its intended
insertion point (after the Verifier passes, before the result is
returned — matching §7.1's ordering and §5's Verifier →
Citation-Consistency [Report Drafting only] → Validation chain). No ad hoc
validation logic was invented to fill the gap.

**One deviation, flagged not guessed:** the interfaces doc's Semantic
Search row and this session's brief specify composing RAG and generating +
verifying an answer, but neither `SubAgentInput` nor the design docs
specify a generation prompt template for sub-agents (orchestrator.py's own
`_FINAL_PROMPT_TEMPLATE` takes parameters — project memory, attached-file
context, full conversation history — that `SubAgentInput` doesn't carry;
see SUBAGENT_INTERFACES.md §2.0). Rather than reuse that template with
padded-in fake values (or partially reimplement orchestrator.py's own
prompt assembly here), this module uses a smaller, self-contained,
sub-agent-scoped system prompt that still enforces the same `[Document N]`
citation contract `verify_grounding()`'s deterministic checks depend on.
Flagged in `semantic_search.py`'s own module docstring.

**Verification:** full existing test suite passes — 882 tests, 0 failures,
0 errors, 4 skipped (the same pre-existing, unrelated docling/PDF
`std::bad_alloc` environment failure already documented present on main
before Phase 0's own merge — confirmed still present identically, not a
regression). Phase 0's compliance suite (51 checks) and Phase 1's
Supervisor suite (22 tests) both still pass unchanged. Nothing in
`main.py`, `orchestrator.py`, or `router.py` was touched — the FastAPI
endpoint still does not call the Supervisor, per §6.

### Contract Retrofit — ExecutionContext & ConversationContext: COMPLETE

Branch `feature/harness-contract-retrofit-execution-context`, merged to main via merge commit
`72ffe99`.

**Built/changed, per §10's exact type definitions:**
- `src/pipeline/harness/types.py` — added `ExecutionContext` (wraps `CallerContext`, adds
  `project_id`/`workspace_id`/`organization_id`/`feature_flags`, the last three reserved and
  inert) and `ConversationContext` (`summary`/`project_memory`/`attachment_refs`), both
  transcribed verbatim from plan §10.1/§10.2 including their `[PRESERVE]` docstrings.
  `ToolInput.caller: CallerContext` -> `ToolInput.execution: ExecutionContext` and
  `SubAgentInput.caller: CallerContext` -> `SubAgentInput.execution: ExecutionContext`, both
  renames not additions. `SubAgentInput.conversation_context` upgraded
  `Optional[str]` -> `Optional[ConversationContext]`, same field/slot.
- All 7 tool wrappers (`src/pipeline/harness/tools/*.py`) — every `tool_input.caller.*` read
  became `tool_input.execution.caller.*`. `rag.py`'s `_build_where()` gained a third
  `project_id: Optional[str] = None` parameter (default preserves old behavior for every
  existing caller): case still wins outright, then `execution.project_id` (when set) narrows
  before falling back to global-only, closing the exact gap flagged in this doc's Phase 0
  entry. `graph.py`'s GRAPH_HYBRID composition of `_build_where()` was left passing no
  project scope — it stays within-case only per design §2.2, so case always wins there anyway
  and threading `project_id` through would be a no-op; not done, to keep this session
  mechanical. `sql.py`/`web.py` needed no change — neither ever read `.caller` (no case/role
  scoping, per design §2.6/§2.7).
- `src/pipeline/harness/supervisor.py` — `Supervisor.handle()` already only forwarded
  `agent_input` opaquely (no direct `.caller` reads in code, only in docstrings/comments), so
  the only changes here are documentation — updated to describe `execution`/`.execution.caller`
  threading unchanged, same `[PRESERVE]` rule that applied to `caller` before.
- `src/pipeline/harness/agents/semantic_search.py` — `agent_input.caller` ->
  `agent_input.execution.caller`; the RAG tool call now passes `execution=execution` instead of
  `caller=caller`. `conversation_context` usage updated for the type upgrade: reads
  `agent_input.conversation_context.summary` (the one field this sub-agent ever used) instead of
  treating the field as a raw string — flagged in-file as the minimal, behavior-preserving
  adaptation; `.project_memory`/`.attachment_refs` are new fields this sub-agent does not yet
  consume, composing them is out of this session's scope.
- Every construction site across Phases 0/1/2's test files (`tests/test_harness_types.py`,
  `tests/test_harness_supervisor.py`, `tests/test_harness_agent_semantic_search.py`,
  `tests/test_harness_tool_{rag,graph,sql,web,xagg,xgraph,xnetwork}.py`) plus the compliance
  suite's own `CallerContext`-constructing tests
  (`src/pipeline/harness/compliance/test_enforcement_3_cross_case_role_gate.py`,
  `test_enforcement_4_role_provenance.py`) — every bare `CallerContext` passed into a
  `ToolInput`/`SubAgentInput` construction now goes through an `ExecutionContext` wrapping it
  (`caller=ctx` -> `execution=ExecutionContext(caller=ctx)`, generally via a small `_execution()`
  test helper placed next to each file's existing `_caller()` helper). No existing assertion was
  weakened — assertions that read `.caller.role`/`.caller.active_case_id`/etc. on a captured
  `SubAgentInput` now read the same values through `.execution.caller.*` instead. Added a few new
  smoke tests alongside the existing ones: `ExecutionContext` construction/defaults in
  `test_harness_types.py`, and `_build_where()`'s new project_id-vs-global precedence in
  `test_harness_tool_rag.py`.

**One caught-and-fixed authoring slip, flagged not hidden:** a first pass at the bulk
find/replace in the compliance suite's `test_enforcement_3_cross_case_role_gate.py` rewrote
`_denied_execution()`'s own body (`ExecutionContext(caller=_denied_caller())`) into
`ExecutionContext(execution=_denied_execution())` — a self-referential replacement that caused
infinite recursion. Caught by the verification test run (RecursionError, not a silent pass);
fixed before merge, called out here per this session's "an easy rename to get subtly wrong"
warning.

**No deviations from the plan's §10.1/§10.2 type text** — `ExecutionContext`/`ConversationContext`
were transcribed as given. `workspace_id`/`organization_id`/`feature_flags` remain present but
unused, exactly as §10.1 specifies; nothing in this session's diff branches on them.

**Verification:** full existing test suite (`pytest`, `testpaths = tests`) — 886 passed, 4 skipped
(the same pre-existing, unrelated docling/PDF `std::bad_alloc` environment failure documented
present on main since Phase 0's own merge — confirmed still present identically, not a
regression), 0 failed. Compliance suite (`src/pipeline/harness/compliance/`, run separately since
`pytest.ini` scopes default collection to `tests/`) — 51/51 checks still passing, unchanged count
from Phase 0. Nothing in `main.py`, `orchestrator.py`, `router.py`'s existing behavior, or any
`workspace_id`/`organization_id`/`feature_flags` branch was touched — still not wired into live
traffic, per §6; the reserved fields are still inert, per §10.1.

### Phase 3 — Large-Scale Aggregate: COMPLETE

Branch `feature/harness-phase-3-large-scale-aggregate`, merged to main via merge commit
`04c6f10`.

*(Naming note: "Phase 3" here is this session's own label for §4 row 2, Large-Scale Aggregate —*
*the next unchecked §8 item — NOT the Verifier/Citation-Consistency/Validation trust layer, which*
*is a separate, later checklist item and was explicitly out of scope this session.)*

**Built:**
- `src/pipeline/harness/agents/large_scale_aggregate.py` — composes the Phase 0 XAGG tool wrapper
  (`src/pipeline/harness/tools/xagg.py::xagg_tool`) only, called as-is, using `ExecutionContext`
  throughout (post-retrofit). XAGG's own role gate (supervisor/station-admin/platform-admin only,
  design §4.3) is checked *inside* `run_aggregate()`; this sub-agent does not duplicate it, only
  translates the tool's `ToolStatus` — the same relationship `xagg_tool()` itself has to
  `run_aggregate()`'s `PermissionError`.
- Status mapping: `DENIED` propagates as its own `SubAgentStatus.DENIED`
  (**[RESOLVED-6, SUBAGENT_INTERFACES.md]** "applies to any future cross-case sub-agent, not only
  [Cross-Case Linkage]" — confirmed applied here, the first sub-agent after Cross-Case Linkage's
  own row to actually need it); `FAILED` → `ABSTAINED`, tool's error propagated; `EMPTY` → `EMPTY`
  (kept as a defensive branch — see deviation note below); `OK` → paraphrase via `call_llm()` +
  `verify_grounding(case_id="cross_case", cross_case_ids=tool_result.case_ids_touched)` (matching
  `orchestrator.py`'s own XAGG verifier-call convention), citing the tool's one synthetic chunk as
  `[Document 1]`.
- **Verifier-rejection status decision — made explicitly, not silently picked (this session's
  brief asked for this by name).** On a Verifier rejection of the paraphrase, this sub-agent serves
  `tool_result.raw_summary_text` as `answer_text` with **`SubAgentStatus.OK`** (not `ABSTAINED`,
  not `PARTIAL`) plus a `caveats` entry naming the raw/unparaphrased format. Full reasoning is in
  the module's own docstring; summary: `SubAgentResult.answer_text`'s "never serve a failed-
  verification answer" rule is written for the risk of presenting an *unconfirmed, possibly
  fabricated* LLM claim as fact — `raw_summary_text` isn't that; it's a different, deterministic
  string that was never itself submitted to the Verifier and has nothing to hallucinate, the same
  reasoning that already exempts `PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE` from verification
  (SUBAGENT_INTERFACES.md §2.1.1). `PARTIAL` was rejected because that status/`degraded_from` pair
  is defined around tool-level degradation (RESOLVED-4) — no tool degraded here, XAGG itself fully
  succeeded, so there is nothing to list in `degraded_from`; overloading `PARTIAL` to also mean
  "generation quality was lower" would invent a second, undocumented meaning for a field the
  interfaces doc gives exactly one meaning. `OK` also matches `orchestrator.py`'s own pre-harness
  XAGG route, which already serves `aggregate_text` on verifier rejection without treating the
  response as degraded in its logged `response_type`.
- `degraded_from` stays `[]` unconditionally — `XAggToolResult.fallback_to_rag` is pinned
  `Literal[False]` (`CrossCaseToolResult`); XAGG never falls back to another tool, so there is
  nothing for this sub-agent to record as attempted-and-fell-back, regardless of which branch above
  is taken.
- `src/pipeline/harness/supervisor.py::register()` — a real `LargeScaleAggregate` instance
  registers itself into the module-level registry at import time, the same pattern Semantic Search
  used.
- `tests/test_harness_agent_large_scale_aggregate.py` — 8 tests: successful aggregate + verified
  paraphrase, verifier-rejection → raw-summary fallback served as `OK` (asserting the documented
  status choice, not just "some status"), `DENIED` propagation (asserted distinct from both
  `ABSTAINED` and `EMPTY`), `FAILED` → `ABSTAINED`, `EMPTY` handling, a generation-exception case
  (distinct from a verifier rejection — no candidate text exists to fall back to, so this stays
  `ABSTAINED`), module self-registration, and a `Supervisor.handle()` → real Large-Scale Aggregate
  → real `xagg_tool()` integration test (router's `route_query()` and `xagg.py`'s own
  `get_gateway()`/`run_aggregate()` stubbed to deterministic test data, not live infra).

**One deviation, flagged not guessed — the `EMPTY` branch is defensive-only, same shape as Phase
2's defensive `DENIED`-from-RAG branch.** As of this session, `xagg_tool()`/`run_aggregate()` never
actually produce `ToolStatus.EMPTY`: every canned aggregate family resolves to `status=OK` with a
`"(no matching cases found)"` rendering inside `raw_summary_text` even when zero rows match (see
`xagg.py::_render_aggregate_text()`). The branch is implemented anyway, per this session's explicit
instruction and because `ToolStatus.EMPTY` is part of the tool's declared contract (`types.py`)
regardless of what the current implementation happens to reach — so an unexpected future change to
`xagg_tool()` (e.g. a canned family with legitimately nothing to compute over) can't silently fall
through unhandled.

**No other deviations.** Bounded payload holds (never `tool_result.chunks`/`EvidenceChunk`, never
the underlying case rows `run_aggregate()` computed over — only `answer_text` + one `Citation` per
SUBAGENT_INTERFACES.md §2.1's table). Validation gate explicitly not wired this session (see the
naming-disambiguation note above) — a `# TODO(validation-gate)` marker is left at its intended
insertion point, matching Semantic Search's precedent; that module's own marker was renamed from
`# TODO(phase-3)` to `# TODO(validation-gate)` in this same session specifically to remove the
naming collision this session's brief warned about.

**Verification:** full existing test suite (`pytest`, `testpaths = tests`) — 894 passed, 4 skipped
(the same pre-existing, unrelated docling/PDF `std::bad_alloc` environment failure documented
present on main since Phase 0's own merge — confirmed still present identically, not a regression),
0 failed (886 baseline + this session's 8 new tests). Compliance suite
(`src/pipeline/harness/compliance/`, run separately per `pytest.ini`'s `testpaths = tests` scoping)
— 51/51 checks still passing, unchanged count. Nothing in `main.py`, `orchestrator.py`, or
`router.py`'s existing behavior was touched — still not wired into live traffic, per §6.

### Phase 4 — Case Summarization: COMPLETE

Branch `feature/harness-phase-4-case-summarization`, merged to main via merge commit `582e5f2`.

**Built:**
- `src/pipeline/harness/agents/case_summarization.py` — composes the Phase 0 RAG tool wrapper
  (case-scoped) and the Phase 0 GRAPH tool wrapper (case-scoped, `hybrid=False`, `max_hops`
  explicitly capped at `DEFAULT_HOPS=2` — NOT `MAX_HOPS=3` — per this session's brief: "this is a
  summary, not deep traversal"), both called as-is via `ExecutionContext` throughout. Neither tool
  carries a role gate (case-assignment-based scoping only), so `SubAgentStatus.DENIED` is not built
  as an output path here, per this session's explicit instruction. RAG and GRAPH are invoked
  concurrently (`asyncio.gather`) — an efficiency choice, not a preservation requirement.
- **Flattening (design §5), concrete for the first time.** Whenever both tools contribute usable
  chunks, they are concatenated into ONE ordered list — RAG chunks first, then GRAPH chunks —
  before the generation prompt is built, and the exact same list/order is handed to
  `verify_grounding()` afterward, so `[Document N]` citations and the Verifier's positional
  `chunks[n-1]` indexing agree by construction. Each tool wrapper's own `metadata.source_tool`
  tagging is preserved untouched — this module never re-tags a chunk.
- **Symmetric degradation (RESOLVED-2/RESOLVED-2a), both directions:**
  - GRAPH empty/failed, RAG usable → ordinary RAG-based summary, `status=PARTIAL`,
    `degraded_from=["GRAPH"]`, `tools_used=["RAG"]`, **no in-text disclosure** (§2.1.2: this is the
    default-shape summary a user expects).
  - RAG empty/failed, GRAPH usable → follows SUBAGENT_INTERFACES.md §2.1.2's ordering exactly:
    compose evidentiary content from GRAPH chunks only → `verify_grounding()` over that content
    ONLY → on pass, PREPEND `GRAPH_ONLY_SUMMARY_DISCLOSURE` (types.py) to `answer_text`, never
    re-verified/regenerated/paraphrased → return `status=PARTIAL`, `degraded_from=["RAG"]`,
    `tools_used=["GRAPH"]`. On Verifier rejection at that step: `ABSTAINED`, no summary served, no
    disclosure produced.
  - Both empty/failed → `status=EMPTY`, not an error.
  - Both usable → combined RAG+GRAPH summary, `status=OK`, `tools_used=["RAG", "GRAPH"]`,
    `degraded_from=[]`; Verifier rejection on the combined evidentiary content → `ABSTAINED`, same
    generic rule every other sub-agent's Verifier boundary already follows.
  - No branch ever produces `ABSTAINED` while real evidence exists on the surviving side — only a
    genuine Verifier rejection of the actual evidentiary content aborts to `ABSTAINED`.
- **`GRAPH_ONLY_SUMMARY_DISCLOSURE` wording updated in `types.py`**, per this session's explicit
  instruction: replaced the bare `[PLACEHOLDER]` stand-in (identical to SUBAGENT_INTERFACES.md's
  own placeholder) with the provisional sentence AGENT_HARNESS_IMPLEMENTATION_PLAN.md §7.4
  proposes, still clearly marked `[PROVISIONAL — PENDING PRODUCT SIGN-OFF]` — §7.4 itself lists the
  actual wording sign-off as still genuinely open. Value-only change to a constant
  SUBAGENT_INTERFACES.md §2.1.2 already designates as Case Summarization's own; no field/shape
  change, committed separately before the sub-agent itself for a clean diff.
- **Bounded payload:** one structured summary — status / key entities / key events / open
  questions — rendered as organized prose/sections inside `answer_text` via a generation-prompt
  instruction, not a schema change. No new typed field was added to `SubAgentResult` — the brief
  was explicit that there is no precedent for one and none should be invented unilaterally; no
  reason to add one surfaced during this build.
- `src/pipeline/harness/supervisor.py::register()` — a real `CaseSummarization` instance registers
  itself into the module-level registry at import time, the same pattern the two prior sub-agents
  used.
- `tests/test_harness_agent_case_summarization.py` — 14 tests: full success with both tools
  contributing (flattened RAG-then-GRAPH citation order, positionally matching what the Verifier
  was shown), RAG-only degradation via both GRAPH `EMPTY` and GRAPH `FAILED` and a raised exception
  (no disclosure text in any case), GRAPH-only degradation via both RAG `EMPTY` and RAG `FAILED`
  (disclosure present, verified to be prepended strictly after verification and never included in
  what the Verifier was actually shown), both-empty and both-exception → `EMPTY`, Verifier
  rejection on all three evidentiary paths (GRAPH-only, RAG-only, combined) → `ABSTAINED` with no
  disclosure produced, a generation-exception case, module self-registration, and a
  `Supervisor.handle()` → real Case Summarization → real `rag_tool()`/`graph_tool()` integration
  test (router's `route_query()` and both tools stubbed to deterministic test data, not live
  infra).

**One defensive addition, flagged not silent — `graph_tool()` has no `try/except` around its own
`retrieve_graph()` calls.** Confirmed by reading `src/pipeline/harness/tools/graph.py` before
writing this module: `rag_tool()` has an explicit `status=FAILED` path for retrieval-infrastructure
exceptions (`rag.py`'s own `_retrieve_candidates` try/except), but `graph_tool()` has no equivalent
— an infrastructure exception from `retrieve_graph()` propagates uncaught out of `graph_tool()` as
of this session's read. Composing two tools whose failure shapes are asymmetric (one always returns
a typed `FAILED` result, the other can raise) means this sub-agent must catch a stray exception from
either tool call itself to implement graceful, exhaustive degradation without crashing — implemented
via a small internal (non-harness-type, does not cross this module's boundary) `_ToolOutcome`
normalization in `case_summarization.py`, tested explicitly
(`test_graph_tool_raising_is_treated_as_degraded`). This is a defensive addition on this module's
own side, not a fix to the already-merged Phase 0 `tools/graph.py` — modifying it was out of this
session's scope, and is called out here as a candidate for a future Phase 0 hardening pass.

**No other deviations.** Bounded payload holds throughout (never `EvidenceChunk`/raw chunks/raw
rows handed to the caller — the flattened chunk list stays inside this function's stack frame,
exactly as in Semantic Search and Large-Scale Aggregate). Validation gate explicitly not wired this
session — a `# TODO(validation-gate)` marker (the renamed convention from the Large-Scale Aggregate
session, not the earlier `phase-3` naming) is left at each of its three intended insertion points
(combined, RAG-only, and GRAPH-only branches — after the Verifier resolves, before the result is
returned).

**Verification:** full existing test suite (`pytest`, `testpaths = tests`) — exit code 0, no `F`/`E`
markers anywhere in the run, the same 4 pre-existing skips already documented present on main since
Phase 0's own merge (the unrelated docling/PDF `std::bad_alloc` environment failure — confirmed
still present identically, not a regression) plus this session's 14 new tests, all passing.
Compliance suite (`src/pipeline/harness/compliance/`, run separately per `pytest.ini`'s
`testpaths = tests` scoping) — 51/51 checks still passing, unchanged count from Phase 0. Nothing in
`main.py`, `orchestrator.py`, or `router.py`'s existing behavior was touched — still not wired into
live traffic, per §6.

### Contract Amendment — SubAgentResult.events / SubAgentResult.links (pre-Timeline-Building): COMPLETE

Branch `feature/harness-contract-amendment-events-links`, merged to main via merge commit
`c70ce6a`.

**Built/changed, per §11's exact type text:**
- `src/pipeline/harness/types.py` — added two fields to `SubAgentResult`: `events: list[TimelineEvent]
  = []` ("Set only by Timeline Building. Ordered event list, each carrying its own conflict_state.")
  and `links: list[CrossCaseLink] = []` ("Set only by Cross-Case Linkage. Ranked cross-case
  connections."), transcribed verbatim from §11. `ConflictState`/`TimelineEvent`/`CrossCaseLink`
  themselves needed no changes — confirmed by reading `types.py` before editing, not assumed: all
  three were already transcribed verbatim from SUBAGENT_INTERFACES.md §2.1 in Phase 0's
  forward-declaration (the file's own "PHASE 0 BUILD NOTE" explains why the §2 sub-agent shapes were
  included early). Both new fields reference `TimelineEvent`/`CrossCaseLink` as forward refs (both
  are defined below `SubAgentResult` in the file) — `SubAgentResult.model_rebuild()` added
  immediately after `CrossCaseLink`'s own definition, mirroring the existing
  `EvidenceChunk.model_rebuild()` precedent for the same forward-ref pattern (`EvidenceChunk` ->
  `ChunkMetadata`).
- `tests/test_harness_types.py` — 5 new tests: `events`/`links` default to `[]` (additive,
  zero-effect on every already-shipped sub-agent), `TimelineEvent.conflict_state` defaults to
  `UNKNOWN` (not `NONE`) per RESOLVED-5, all three `ConflictState` values are constructible,
  `CrossCaseLink.source_tool` rejects anything outside `{XGRAPH, XNETWORK}`, and a round-trip
  `model_dump()` test proving the forward-ref `model_rebuild()` actually resolved (not just that
  construction succeeded in-process).

**No deviations.** Additive, default-empty on both new fields — no retrofit of any already-shipped
sub-agent's code was needed or done, unlike §10's `ExecutionContext`/`ConversationContext` retrofit
(which touched 3 already-merged modules). Semantic Search, Large-Scale Aggregate, and Case
Summarization continue to construct `SubAgentResult` exactly as before; neither new field is set by
any of them.

**Verification:** full existing test suite (`pytest`, `testpaths = tests`) — exit code 0, no `F`/`E`
markers anywhere in the run, the same 4 pre-existing skips already documented present on main since
Phase 0's own merge (the unrelated docling/PDF `std::bad_alloc` environment failure — confirmed
still present identically, not a regression) plus this session's 5 new tests, all passing.
Compliance suite (`src/pipeline/harness/compliance/`, run separately per `pytest.ini`'s
`testpaths = tests` scoping) — 51/51 checks still passing, unchanged count from Phase 0. Nothing in
`main.py`, `orchestrator.py`, or `router.py`'s existing behavior was touched — still not wired into
live traffic, per §6.

### Phase 5 — Timeline Building: COMPLETE

Branch `feature/harness-phase-5-timeline-building`, merged to main via merge commit
`97f223d`.

**Built:**
- `src/pipeline/harness/agents/timeline_building.py` — composes two NEW, dedicated, case-scoped
  Cypher templates, both routed through `src.graph.case_scope.scoped_cypher()` per design §4.5's
  own anticipation of exactly this need ("If the harness introduces new within-case Cypher
  templates anywhere (e.g. inside Timeline Building), route them through `scoped_cypher()`, not
  raw `age_client.execute_cypher()`"): `_fetch_dated_incidents()` (Incident nodes with a live,
  non-superseded `OCCURRED_ON` edge — the literal date-bearing-edge filter) and
  `_fetch_conflict_bases()` (live `CONFLICTS_WITH` edges, projected to `entity_id -> [basis, ...]`).
  Neither the Phase 0 `graph_tool()` wrapper nor `retrieve_graph()` is touched or even called by
  this sub-agent — see the deviation below for why. `ExecutionContext` is used throughout (only
  `execution.caller.active_case_id` is actually read; no role gate applies, matching Case
  Summarization's precedent — `SubAgentStatus.DENIED` is never built as an output path here).
- **RESOLVED-5, honored exactly.** No date-bearing edges → `status=EMPTY`, not an error (checked
  before conflict detection even runs — nothing to attach a conflict state to). Conflict-detection
  fetch failing while the date-edge fetch resolved fine → every `TimelineEvent.conflict_state`
  set to `UNKNOWN` (never `NONE` — enforced by construction: `_build_events(..., conflict_bases=
  None)` has no code path that ever writes `NONE`), `status=PARTIAL`, with a caveat naming
  conflict detection as the degraded component. `NONE` is only ever set on an incident absent
  from a *successfully returned* conflict-bases mapping (checked, nothing found) — never as a
  fallback for an unchecked event.
- **Deterministic, graph-derived `TimelineEvent.description` and `answer_text` — this session's
  brief asked for this decision to be made and documented, not silently picked.** Adopted the
  recommended option: `description` is copied verbatim from `Incident.description` (the graph
  node property `src/extraction/domain_entities.py` writes at ingestion), never regenerated or
  paraphrased through an LLM — avoiding per-event hallucination risk entirely, the same reasoning
  that already exempts XAGG's `raw_summary_text` from the Verifier (§9's Phase 3 entry). `
  answer_text` (event count, date span, conflict counts) is likewise built via plain deterministic
  string formatting over already-computed `events`, never model-generated. Consequence:
  `verify_grounding()` is never called anywhere in this sub-agent — there is nothing generated
  that could hallucinate, matching the same "nothing to cite, nothing to verify" property the
  interfaces doc's own disclosure-string exemptions (§2.1.1) already establish for a different
  kind of fixed/deterministic text.
- `src/pipeline/harness/supervisor.py::register()` — a real `TimelineBuilding`-equivalent callable
  (`timeline_building`) registers itself under `TIMELINE_BUILDING` at import time, the same
  pattern every prior sub-agent module used.
- `tests/test_harness_agent_timeline_building.py` — 7 tests: successful timeline with mixed
  conflict states (`CONFLICT`/`NONE`) and chronological reordering (input rows deliberately
  out of date order), no-date-edges → `EMPTY`, conflict-detection-failure → all-`UNKNOWN` +
  `PARTIAL` (RESOLVED-5's exact named scenario), date-edge-fetch exception → `ABSTAINED` with
  the error propagated, no-active-case → `EMPTY` (not an exception — see deviation note below),
  module self-registration, and a `Supervisor.handle()` → real Timeline Building → real
  `scoped_cypher()` call-site integration test that bypasses `route_query()`'s real classification
  by monkeypatching `classify_to_subagent()` directly (see the classification-reachability note
  below for why this is a deliberate bypass, not an oversight).

**Two deviations from a literal reading of this session's brief, both resolved with the user via
`AskUserQuestion` before any code was written — not guessed:**

1. **Data source is two new dedicated Cypher templates, not "the Phase 0 GRAPH tool wrapper."**
   The brief's literal wording ("composes the Phase 0 GRAPH tool wrapper, filtered to date-bearing
   (OCCURRED_ON) edges") is not achievable as written: `graph_tool()`/`retrieve_graph()` performs
   `ASSOCIATED_WITH` entity traversal and returns document-text `EvidenceChunk`s that carry no
   `occurred_on` date field anywhere, and its existing conflict-chunk path
   (`_fetch_case_conflicts()`) does not preserve which `Incident.entity_id` each returned chunk
   belongs to — there is no way to assemble `TimelineEvent.occurred_on` from what `graph_tool()`
   returns today, and no other exported function already returned "Incident + its OCCURRED_ON
   date" (the only place that query shape existed before this session was inlined, unexported,
   inside `conflict_detection.py::detect_conflicts()`). Design §4.5 itself anticipates a new
   within-case Cypher template being needed "e.g. inside Timeline Building" — this session's
   two new templates are exactly that, mirroring `conflict_detection.py`'s and
   `_fetch_case_conflicts()`'s own already-proven MATCH shapes rather than inventing new Cypher
   patterns. Neither `tools/graph.py` nor `retrieval/graph_retriever.py` (Phase 0, already-merged,
   depended on by other sub-agents) was touched. `tools_used`/`degraded_from` still tag this data
   as `"GRAPH"` (`SourceTool`) throughout — the classification is about the retrieval mechanism
   class (Cypher/AGE-sourced), not about which specific wrapper function ran.
2. **`degraded_from` stays `[]` when conflict detection alone fails, per RESOLVED-5.** RESOLVED-5's
   text says a conflict-detection-only failure should have `degraded_from` "recording the conflict
   check" — but `degraded_from: list[SourceTool]` is a closed Literal with no conflict-detection
   member (conflict detection is Phase 8 machinery, not one of the eight tool primitives), and
   writing `"GRAPH"` there would collide with `tools_used=["GRAPH"]` (the date-edge fetch
   genuinely succeeded) in violation of §2.0's own stated invariant that a tool never appears in
   both lists for one call. Resolved with the user: `degraded_from=[]` in this branch; the
   degradation is instead carried by `status=PARTIAL`, every event's `conflict_state=UNKNOWN`, and
   an explicit `caveats` entry naming conflict detection by name. No `SourceTool`/`types.py` change
   was made or proposed.

**One additional, smaller defensive branch, flagged not silent:** a query with no
`execution.caller.active_case_id` returns `status=EMPTY` (not an exception) — `scoped_cypher()`
itself would raise `ValueError` on an empty/None `case_id`, and since this sub-agent is entirely
within-case, "no active case" is treated as a legitimate "nothing to build a timeline for" rather
than an infrastructure failure being reported as one.

**Classification reachability — stated plainly, not closed this session.** `router.py` has no
classification signal for Timeline Building (documented in Phase 1's own progress-log entry and
reiterated in §11's contract-amendment entry above) — this session does not invent new trigger
keywords/patterns to close that gap, per this session's explicit instruction that new
classification logic needs the same evidence-driven basis XAGG/XGRAPH/XNETWORK's own deterministic
overrides had (live-confirmed misclassification failures), not guessed patterns. This sub-agent is
built, registered, and tested via direct dispatch and a Supervisor integration test that bypasses
`route_query()`'s real classification (monkeypatching `classify_to_subagent()` to force the route).
`Supervisor.handle()`'s real classification path (`route_query()` -> `_ROUTE_TO_SUBAGENT`) still
cannot reach `TIMELINE_BUILDING` today; this is a real, tracked gap, not a silent one.

### Phase 6 — Cross-Case Linkage: COMPLETE

Branch `feature/harness-phase-6-cross-case-linkage`, merge commit recorded once merged to main
below.

**Built:**
- `src/pipeline/harness/agents/cross_case_linkage.py` — composes the Phase 0 XGRAPH tool wrapper
  (`src/pipeline/harness/tools/xgraph.py::xgraph_tool`) and XNETWORK tool wrapper
  (`src/pipeline/harness/tools/xnetwork.py::xnetwork_tool`) CONCURRENTLY on every invocation
  (`asyncio.gather`), matching Case Summarization's Phase 4 precedent, per this session's explicit
  instruction — this sub-agent never tries to pick one tool over the other per query.
  `ExecutionContext` used throughout; `XGraphToolInput.target_entity` is left `None` (no
  structured, pre-extracted entity name is available on `SubAgentInput`, the same gap Large-Scale
  Aggregate's Phase 3 session hit for `XAggToolInput.target_entity` — same resolution, not a new
  one). Neither tool's role gate is duplicated at this sub-agent's level (design §4.3,
  SUBAGENT_INTERFACES.md §2.1's Cross-Case Linkage row's explicit "do not add a third gate" — this
  sub-agent only translates the two tools' resulting `ToolStatus`).
- **Status mapping**, five named outcomes from this session's brief, all implemented and tested:
  both `DENIED` -> `SubAgentStatus.DENIED` [RESOLVED-6], never collapsed into `ABSTAINED`/`EMPTY`;
  both a definite "no connections" result (`XGraphToolResult` `EMPTY` with no `unconfirmed_links`
  AND `XNetworkToolResult` `EMPTY`) -> `SubAgentStatus.EMPTY`, presented as a real finding
  [PRESERVE — XGRAPH's own module docstring / design §2.3]; both `FAILED` -> `ABSTAINED`; one
  contributes real data, the other doesn't -> `PARTIAL` with `tools_used`/`degraded_from` split
  accordingly (RESOLVED-4's uniform rule); both contribute -> `OK`,
  `tools_used=["XGRAPH", "XNETWORK"]`.
- **A sixth, brief-uncovered combination, resolved and flagged, not silently picked:** neither
  tool is guaranteed to fail/succeed together (unlike `DENIED`, which shares one role gate, XGRAPH
  hits Postgres/AGE and XNETWORK hits a separate Chroma collection — genuinely independent failure
  modes). One `FAILED` + the other a definite `EMPTY` is reachable and matches none of the five
  named buckets (not "both FAILED," not "both definite-empty"). Resolved by falling back to
  RESOLVED-4's general, project-wide rule: any attempted-and-non-contributing tool belongs in
  `degraded_from`, and non-empty `degraded_from` implies `PARTIAL` — so this combination returns
  `PARTIAL`, `tools_used=[]`, `degraded_from=["XGRAPH", "XNETWORK"]`, a deterministic "no confirmed
  cross-case connections were found" `answer_text`, and a caveat naming which side errored. Tested
  explicitly (`test_xgraph_failed_xnetwork_definite_empty_returns_partial_no_tools_used`).
- **`unconfirmed_links` fully wired — this session's brief's single highest-consequence
  correctness requirement.** Every `XGraphToolResult.unconfirmed_links` entry becomes ONE
  `CrossCaseLink(is_unconfirmed=True)` AND contributes a matching `SubAgentResult.caveats` entry —
  tested both in isolation and alongside a confirmed XGRAPH connection in the same result
  (`test_unconfirmed_links_become_caveated_crosscaselinks`,
  `test_unconfirmed_links_alongside_confirmed_connection`). Per
  `_unconfirmed_same_as_links()`'s actual dict shape (`src/retrieval/graph_retriever.py`, confirmed
  by reading it — `{"entity", "candidate", "tier", "confidence", "status"}`, no per-link case IDs),
  `CrossCaseLink.case_ids=[]` for these entries: the tool's aggregate `case_ids_touched` was not
  used as a substitute, since that would misattribute an unconfirmed pairing to specific cases it
  was never shown to actually span.
- **Description-generation decision, made and documented, same as Timeline Building's Phase 5
  precedent for `TimelineEvent.description`:** XGRAPH-derived `CrossCaseLink.description` strings
  are deterministic/structured — composed from `case_ids_touched`/`hop_count`/`chain_confidence`
  (and `target_entity`, when supplied), never LLM-generated or paraphrased, and never run through
  `verify_grounding()` (nothing generated, nothing to verify — the same property Timeline
  Building's Phase 5 entry already established for its own deterministic content). Rationale: a
  cross-case identity/vehicle/pattern-recurrence claim is the highest-stakes claim type in this
  system, so avoiding LLM generation for it avoids hallucination risk entirely rather than
  mitigating it after the fact, the same reasoning already applied to Phase 3's
  `XAggToolResult.raw_summary_text` and Phase 5's `TimelineEvent.description`. XNETWORK-derived
  content uses whatever the tool wrapper enables per this session's explicit instruction: the
  synthesized `answer_text` narrative IS generated-and-verified (see next item), but each
  per-community `CrossCaseLink.description` is the tool's own already-grounded community-summary
  text verbatim, never re-paraphrased per item.
- **XGRAPH tool-contract discrepancy, confirmed against the code before writing any code, resolved
  with the user via `AskUserQuestion` — not guessed.** This session's brief characterized
  XNETWORK's "verify -> one-shot cloud-regeneration -> raw-text fallback" behavior as "already
  built into the Phase 0 tool wrapper." Reading `src/pipeline/harness/tools/xnetwork.py` before
  writing this module showed the opposite, stated explicitly in that file's own module docstring:
  `xnetwork_tool()` only calls `run_network_query()` and translates its result/`PermissionError` —
  no `call_llm()`, no `verify_grounding()` anywhere in it, and the docstring says outright that
  "the XNETWORK-specific cloud-retry behavior belongs to whichever sub-agent (Cross-Case Linkage)
  eventually composes this tool with the Verifier — do not implement it here." Cross-checked
  against `orchestrator.py`'s own pre-harness XNETWORK route (~line 1387-1528): the same
  verify -> `force_cloud=True` one-shot regeneration -> re-verify -> raw-text-fallback sequence
  lives at the route level there too, not inside `run_network_query()`. The tool-level code is
  internally consistent; the brief's framing of it was not. Per the design docs' own instruction
  ("if anything is ambiguous, or the docs/code disagree, stop and ask rather than guessing"), this
  was surfaced to the user before any code was written rather than resolved either way silently.
  **Resolution:** this sub-agent implements the full XNETWORK-specific retry sequence itself
  (`_generate_xnetwork_text()`), mirroring `orchestrator.py`'s existing route and `xnetwork.py`'s
  own docstring instruction — a materially larger, higher-stakes piece of this session's build than
  the brief's original framing implied.
- **Bounded payload:** `SubAgentResult.links` (never raw chunks) carries every XGRAPH/XNETWORK
  connection; `answer_text` is a short synthesized narrative combining the deterministic XGRAPH
  section (when it contributes) and the generated/verified (or raw-fallback) XNETWORK section
  (when it contributes); `Citation` entries are populated for XNETWORK's generated content only
  (`Citation.document_index` is defined against `[Document N]` markers in generated text, which the
  deterministic XGRAPH section never uses — matching Timeline Building's Phase 5 precedent of no
  citations for deterministic content).
- `src/pipeline/harness/supervisor.py::register()` — a real `CrossCaseLinkage`-equivalent callable
  (`cross_case_linkage`) registers itself under `CROSS_CASE_LINKAGE` at import time, the same
  pattern every prior sub-agent module used.
- `tests/test_harness_agent_cross_case_linkage.py` — 15 tests: both tools contribute (`OK`), one
  `EMPTY`/other contributes (`PARTIAL`, both directions), both definite-empty (`EMPTY`), both
  `DENIED` (`DENIED`, asserted distinct from `ABSTAINED`/`EMPTY`), both `FAILED` (`ABSTAINED`), the
  sixth brief-uncovered `FAILED`+definite-`EMPTY` combination, `unconfirmed_links` -> caveats wiring
  (in isolation and alongside a confirmed connection), XNETWORK's full retry sequence (cloud retry
  passing, cloud retry also rejected -> raw fallback, cloud retry raising under a simulated
  `AIR_GAP_MODE`-style refusal -> raw fallback, and a first-call generation exception treated as
  degraded rather than a whole-sub-agent abstention), module self-registration, and a
  `Supervisor.handle()` -> real Cross-Case Linkage -> real `xgraph_tool()`/`xnetwork_tool()`
  integration test.

**Classification reachability — CONFIRMED, unlike Timeline Building.** Read
`src/pipeline/harness/supervisor.py`'s `_ROUTE_TO_SUBAGENT` table before writing any code, per this
session's explicit instruction not to assume: both `"XGRAPH"` and `"XNETWORK"` already map to
`CROSS_CASE_LINKAGE` (set in Phase 1, unrelated to this session's own work). This sub-agent is
therefore reachable through real classification today —
`Supervisor.handle()` -> `route_query()` -> `_ROUTE_TO_SUBAGENT["XGRAPH"|"XNETWORK"]` ->
`cross_case_linkage()` — confirmed by the Supervisor integration test above, which drives the real
`_ROUTE_TO_SUBAGENT` mapping (only `route_query()` and the two tools' underlying
`retrieve_graph()`/`run_network_query()` calls are stubbed, not the classification path itself).

**No other deviations.** Bounded payload holds throughout — no `EvidenceChunk`/raw chunks/raw graph
rows ever cross this module's boundary; only `answer_text`, `citations`, `links`, and `caveats` are
populated. Validation gate explicitly not wired this session, same as every prior sub-agent — a
`# TODO(validation-gate)` marker is left at its intended insertion point. Per
AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 5 and §5's table, Validation's FULL semantic re-check
will be MANDATORY here once `validation.py` exists (not the lighter structural-only check some
other sub-agents get) — noted in the module's own docstring and repeated here, not left implicit.

**Verification:** full existing test suite (`pytest`, `testpaths = tests`) — exit code 0, no `F`/`E`
markers anywhere in the run (935 dot-markers + 4 skip-markers across the progress bar, the same 4
pre-existing skips already documented present on main since Phase 0's own merge — the unrelated
docling/PDF `std::bad_alloc` environment failure — confirmed still present identically, not a
regression), comprising the prior baseline plus this session's 15 new tests. Compliance suite
(`src/pipeline/harness/compliance/`, run separately per `pytest.ini`'s `testpaths = tests` scoping)
— 51/51 checks still passing, unchanged count from Phase 0. Nothing in `main.py`, `orchestrator.py`,
or `router.py`'s existing behavior was touched — still not wired into live traffic, per §6.

**No other deviations.** Bounded payload holds throughout — `SubAgentResult.events` (already
present in `types.py` per the §11 amendment, not redefined here) is the only non-generic field
this sub-agent populates; no raw graph rows, no `EvidenceChunk`, ever cross this module's
boundary. Validation gate explicitly not wired this session, same as every prior sub-agent — moot
here in one respect (there is no generated evidentiary text to validate, per the deterministic-
description decision above), but a `# TODO(validation-gate)`-equivalent note is not needed since
there is no Verifier-boundary insertion point in this sub-agent at all, unlike every prior one.

**Verification:** full existing test suite (`pytest`, `testpaths = tests`) — exit code 0, 848
passed, 4 skipped (the same pre-existing, unrelated docling/PDF `std::bad_alloc` environment
failure already documented present on main since Phase 0's own merge — confirmed still present
identically, not a regression), 0 failed, comprising the prior baseline plus this session's 7 new
tests. Compliance suite (`src/pipeline/harness/compliance/`, run separately per `pytest.ini`'s
`testpaths = tests` scoping) — 51/51 checks still passing, unchanged count from Phase 0 (this
sub-agent is not a "tool wrapper" per `_source_scan.py::TOOL_WRAPPER_MODULE_NAMES`, so it is
correctly out of enforcement-point-5's static scan scope; it never calls `age_client.execute_cypher()`
directly either way, only `scoped_cypher()`, so the underlying security property holds regardless).
Nothing in `main.py`, `orchestrator.py`, or `router.py`'s existing behavior was touched — still not
wired into live traffic, per §6.

---

## 10. Contract amendment — ExecutionContext & ConversationContext (post-Phase-2)

Two deviations surfaced independently in Phase 0 and Phase 2 turned out to be the same underlying
gap, recurring: the shared contract has no room for scope/context beyond a single case, so each
sub-agent phase was starting to invent its own narrow workaround. Resolved as one contract change,
**decided before Large-Scale Aggregate starts** so the next sub-agent is built against the settled
shape rather than becoming an eighth ad hoc deviation.

### 10.1 `ExecutionContext` — wraps `CallerContext`, does not replace it

```python
class ExecutionContext(BaseModel):
    """
    The environment a query executes in. Threaded through supervisor -> sub-agent
    -> tool in place of a bare CallerContext.

    [PRESERVE] Wraps CallerContext rather than replacing or flattening it — every
    existing [PRESERVE] rule on CallerContext (SUBAGENT_INTERFACES.md §0: role
    must originate from the authenticated user's real RBAC role, never a
    profile/preferences object; construction does not grant access) applies
    unchanged to the nested `caller` field. This type only adds scope that sits
    ABOVE a single case.

    [PRESERVE, extends §4.4] Threaded unchanged at every hop, exactly like
    CallerContext was before it — not reconstructed, not merged with any
    profile/preferences object, not partially copied.
    """
    caller: CallerContext
    project_id: Optional[str] = Field(
        default=None,
        description=(
            "Project-level scope. Restores the project/global precedence rule "
            "RAG's Phase-0 tool wrapper had to drop for lack of a carrier (§9, "
            "Phase 0 entry). None = no project scoping applied — same behavior "
            "as the Phase 0/1/2 code shipped with."
        ),
    )
    workspace_id: Optional[str] = None
    organization_id: Optional[str] = None
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    # workspace_id / organization_id / feature_flags are RESERVED, UNUSED for MVP.
    # Nothing may branch on them until a real spec exists for each — the point of
    # adding them now is to avoid a second signature-wide change later, not to
    # start building against them today.
```

`ToolInput.caller: CallerContext` and `SubAgentInput.caller: CallerContext` are both **renamed** to
`execution: ExecutionContext` — not additive; every existing call site that reads `.caller.role`,
`.caller.active_case_id`, etc. becomes `.execution.caller.role`, `.execution.caller.active_case_id`.

### 10.2 `ConversationContext` — upgrades the existing field's type, not a new field

```python
class ConversationContext(BaseModel):
    """
    Bounded, pre-summarized conversation/session context for a sub-agent.

    [PRESERVE, carried forward from the original conversation_context field]
    Bounding is the supervisor's job — a sub-agent never receives full history,
    full project memory, or raw attachment bytes. This object being richer than
    a bare string does not relax that rule; every field on it must already be
    pre-bounded/summarized by the time it reaches a sub-agent.
    """
    summary: Optional[str] = None
    project_memory: Optional[str] = None
    attachment_refs: list[str] = Field(default_factory=list)
```

`SubAgentInput.conversation_context` changes type from `Optional[str]` to
`Optional[ConversationContext]` — same field name, same field slot, richer shape. No new field
added; this is a type upgrade of the field that already existed.

### 10.3 Retrofit scope — what already-shipped code this touches

- `src/pipeline/harness/types.py` — add both new types; change `ToolInput.caller` →
  `ToolInput.execution`, `SubAgentInput.caller` → `SubAgentInput.execution`, upgrade
  `conversation_context`'s type.
- `src/pipeline/harness/tools/*.py` (all 7, Phase 0) — every wrapper's internal reads of
  `tool_input.caller.*` become `tool_input.execution.caller.*`. RAG's wrapper additionally starts
  reading `tool_input.execution.project_id` for the scoping it previously couldn't do.
- `src/pipeline/harness/supervisor.py` (Phase 1) — `Supervisor.handle()` threads `execution`
  unchanged, same as it threaded `caller` before; the "threaded unchanged" tests need updating to
  construct/assert against `ExecutionContext`, not `CallerContext`, directly.
- `src/pipeline/harness/agents/semantic_search.py` (Phase 2) — same rename at its call sites.
- All three phases' existing test files construct `CallerContext` directly today — every one of
  those construction sites needs updating to build an `ExecutionContext` wrapping it instead.

### 10.4 Why this doesn't wait until it's needed

Retrofitting 3 already-merged modules now is cheaper than retrofitting 8. Every sub-agent from
Large-Scale Aggregate onward is built directly against whichever shape is settled at the time — do
this now, once, or repeat the Phase-0/Phase-2 pattern of one more ad hoc workaround per phase until
someone eventually forces the same rename across a much larger surface.

---

## 11. Contract amendment — SubAgentResult.events / SubAgentResult.links (pre-Timeline-Building)

`SUBAGENT_INTERFACES.md` §2.1 defines `TimelineEvent` and `CrossCaseLink` as dedicated Pydantic
types — described in their own docstrings as "Timeline Building's per-event payload element" and
"Cross-Case Linkage's per-item payload element" — but `SubAgentResult` (§2.0) was never given a
field to carry either. The doc already has exactly this pattern for a different sub-agent
(`generated_file: Optional[GeneratedFileRef] = Field(default=None, description="Set only by Report
Drafting.")`); this amendment applies the same precedent to the two that were missed:

```python
class SubAgentResult(BaseModel):
    ...
    events: list["TimelineEvent"] = Field(
        default_factory=list,
        description="Set only by Timeline Building. Ordered event list, each carrying its own conflict_state.",
    )
    links: list["CrossCaseLink"] = Field(
        default_factory=list,
        description="Set only by Cross-Case Linkage. Ranked cross-case connections.",
    )
```

Additive, default-empty — zero effect on any already-shipped sub-agent (Semantic Search, Large-Scale
Aggregate, Case Summarization use neither field). No retrofit of existing code required, unlike §10.

**Also relevant to Timeline Building specifically:** Phase 1's progress log (§9) flagged that
`router.py` has no classification signal for Timeline Building — it is registerable but
unreachable via `Supervisor.classify_to_subagent()` today. That gap is **not** resolved by this
amendment and is explicitly not to be closed by guessing new trigger keywords this session (per
Phase 1's own note: new classification logic needs the same evidence-driven basis
XAGG/XGRAPH/XNETWORK's overrides had, not invented patterns). Build and test Timeline Building via
direct dispatch/registration and integration tests that bypass `route_query()`'s classification —
real end-user reachability stays a tracked, separate gap.
