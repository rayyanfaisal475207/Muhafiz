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
- [x] Investigative Analysis (parallel execution) *(complete — see §9)*
- [x] **Report Drafting, including Citation-Consistency module** *(complete — see §9; §5/§8's implied split between "Report Drafting" and a later separate "Citation-Consistency module" line was resolved via AskUserQuestion to build both together this phase — see the Phase 8 entry)*
- [x] Data-Quality / Extraction-Coverage *(complete — see §9)*
- [x] **Verifier module** — pre-existing (`src/pipeline/verifier.py`, predates the harness), reused
  UNCHANGED by every sub-agent through Phase 9 per design §5's decision (a) — not new
  harness-authored code, so there was never a "build" step here. Checked off because a real,
  confirmed gap in it *was* found and fixed this session (its own hedging check never actually read
  the harness's `ChunkMetadata.confidence`/`confidence_status` shape — see §9's fix entry) — flagged
  rather than left as a silently-stale unchecked box implying unbuilt work that was never in scope.
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

Branch `feature/harness-phase-6-cross-case-linkage`, merged to main via merge commit `1decf57`.

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

### Contract Amendment — SubAgent on_event threading (pre-Phase-7): COMPLETE

Branch `feature/harness-phase-7-investigative-analysis` (folded into the same branch as the Phase 7
sub-agent below, not a separate branch — see §12 for the exact type/signature change).

**Built/changed, mirroring §10/§11's pattern:**
- `src/pipeline/harness/types.py` — `SubAgent` Protocol's `__call__` widened to accept a new
  keyword-only `on_event: Optional[OnEventCallback] = None` (new alias, same
  `Callable[[PipelineEvent], None]` shape `Supervisor.handle()` has accepted since Phase 1). See
  §12 for the full rationale and exact code.
- `src/pipeline/harness/supervisor.py` — `Supervisor.handle()`'s existing `handler(agent_input)`
  call site becomes `handler(agent_input, on_event=on_event)`, forwarding its own parameter
  (possibly `None`) unchanged.
- The 5 already-shipped sub-agent modules (Semantic Search, Large-Scale Aggregate, Case
  Summarization, Timeline Building, Cross-Case Linkage) — each retrofitted to accept-and-ignore
  the new kwarg (additive, `None` default), each with its own docstring note on why it has nothing
  granular to emit yet (single-tool sub-agents) or why emitting per-source events is tracked future
  work per §2.1.4's "Generalization" note (Case Summarization, Cross-Case Linkage) rather than done
  in this amendment.
- `tests/test_harness_supervisor.py` — `_mock_sub_agent()`'s stub handler updated to accept
  `on_event` and record what it received; two new tests assert `Supervisor.handle()` forwards a
  given callback unchanged and forwards `None` when none is given.

**Why this was needed, confirmed from the code before proposing it (not assumed from the docs
alone):** `Supervisor.handle()` has accepted an `on_event` sink since Phase 1, but `SubAgent.__call__`
took only `agent_input` — there was no channel for a sub-agent to emit its own mid-execution
`PipelineEvent`s back through it. SUBAGENT_INTERFACES.md §2.1.4/RESOLVED-4a requires exactly that
for Investigative Analysis (Phase 7, below). Surfaced to the user via `AskUserQuestion` before any
code was written, per this session's explicit instruction not to invent a delivery mechanism to
paper over a real contract gap — resolved as a full contract amendment now (touching all 5
already-shipped sub-agents, not scoped to Investigative Analysis alone), the user's own choice
between the two options offered.

**Verification:** full existing test suite — 950 passed, 4 skipped (the same pre-existing,
unrelated docling/PDF `std::bad_alloc` environment failure documented present on main since
Phase 0's own merge — confirmed still present identically, not a regression), 0 failed. Compliance
suite — 51/51 checks still passing, unchanged count from Phase 0. Confirmed the one ordering
hazard directly: running an arbitrary hand-picked SUBSET of harness test files together (as
opposed to the full `tests/` collection) reproduces two failures from real-repo cross-file registry
pollution (`test_module_level_register_and_unregister` unregistering `SEMANTIC_SEARCH` after
collection-time self-registration) — confirmed present IDENTICALLY on unmodified `main` with the
same subset, i.e. pre-existing and unrelated to this amendment, not a regression it introduced.

### Phase 7 — Investigative Analysis: COMPLETE

Branch `feature/harness-phase-7-investigative-analysis`, merged to main via merge commit
`a7fb27c`.

**Built:**
- `src/pipeline/harness/agents/investigative_analysis.py` — composes the Phase 0 RAG, GRAPH, and
  SQL tool wrappers, run CONCURRENTLY via `asyncio.gather` on every call (this session's own build
  note, plan §4 row 6: "Runs the three tool calls in parallel ... cutting response time"). None of
  the three carries a role gate, so `SubAgentStatus.DENIED` is never built as an output path here,
  same reasoning as Case Summarization's identical precedent.
- **Fallback-substitution question, resolved from the tool code before writing any code (not
  assumed from the docs), per this session's explicit instruction.** Read `tools/rag.py`,
  `tools/graph.py`, `tools/sql.py` directly: nothing inside `graph_tool()`/`sql_tool()` invokes the
  RAG tool — `ToolResult.fallback_to_rag`'s own docstring is explicit that the tool only reports a
  fallback is warranted, the caller acts on it, matching Case Summarization's already-merged
  precedent of the calling sub-agent performing the substitution itself. Resolution implemented:
  because RAG is already one of the three tools this sub-agent runs on every call (not
  conditionally, on a sibling's say-so), the single already-in-flight `rag_tool()` call IS the
  substitution — no second RAG invocation, no explicit dedup step needed. This falls directly out
  of `ToolResult`'s own contract ("chunks non-empty iff status is OK"): a degraded GRAPH/SQL's
  `.chunks` is already `[]`, so flattening "whichever tool returned OK" is correct and complete by
  construction.
- **RESOLVED-4's uniform `tools_used`/`degraded_from` rule, applied here as its origin case.**
  `tools_used`/`degraded_from` computed once, in one fixed RAG/GRAPH/SQL order, from the same
  three per-tool outcomes the live events (below) are built from. A call where GRAPH and SQL both
  fall back to RAG reports `tools_used=["RAG"]`, `degraded_from=["GRAPH","SQL"]`, never three —
  verified explicitly (`test_graph_and_sql_both_fall_back_to_rag_dedup_to_one_rag_entry`).
  `>=1` tool contributing -> `PARTIAL` (or `OK` if none degraded); all three failed/empty ->
  `ABSTAINED` — applied LITERALLY per this row's own explicit RESOLVED-4 text, deliberately NOT
  mirroring Case Summarization's "both empty -> EMPTY" convention (this sub-agent's own row says
  ABSTAINED for the zero-contributor case, and RESOLVED-4 is written against this sub-agent as its
  origin case — no ambiguity to resolve by analogy elsewhere).
- **RESOLVED-4a live per-source trace events, wired in via the contract amendment above.** One
  `PipelineEvent` per source-tool outcome (`step="analysis:rag"|"analysis:graph"|"analysis:sql"`),
  emitted AS EACH TOOL RESOLVES — implemented by wrapping each tool call in its own small
  coroutine that emits its event the instant its own `await` completes, before `asyncio.gather()`
  as a whole returns, so the live trace shows whichever tool actually finishes first, not an
  artificially serialized order. Status mapping onto the five-value SSE vocabulary (a resolved
  decision, since §2.1.4's text illustrates only three examples, not every case): `status==OK` ->
  `"done"`; `status!=OK` with `fallback_to_rag=True` -> `"retry"`; `status!=OK` with
  `fallback_to_rag=False` (RAG's own case by construction, since `RagToolResult.fallback_to_rag`
  is pinned `False` — RAG has no fallback target of its own — plus the defensive
  uncaught-exception branch for GRAPH/SQL) -> `"error"`, matching §2.1.4's own "SQL fails
  outright -> error" example. `"skipped"` never fires — all three tools are always attempted,
  unconditionally, every call.
- **Documented, deliberate divergence between the live events and the final roll-up on a Verifier
  rejection.** Every prior sub-agent that generates `answer_text` treats a `verify_grounding()`
  rejection as its own `ABSTAINED` path with `tools_used`/`degraded_from` reset to `[]` — an
  answer that fails verification is never served, and nothing about it (including which tools
  backed the discarded draft) is asserted as fact. This sub-agent follows that same established
  convention. Consequence, flagged explicitly in the module docstring and tested directly
  (`test_verifier_rejection_abstains_and_resets_tools_used_despite_done_events`): a tool whose
  live event said `"done"` can still be absent from the final `tools_used` if the synthesized
  answer built from its data was rejected and discarded — this does not violate RESOLVED-4a's
  "events and roll-up must agree" rule, which governs the roll-up actually returned for a result
  that stands, not a discarded, never-served draft.
- **Defensive exception handling**, same asymmetry Case Summarization's Phase 4 entry already
  flagged: `rag_tool()`/`sql_tool()` both catch their own retrieval exceptions internally;
  `graph_tool()` does not (confirmed again by re-reading `tools/graph.py` this session). This
  sub-agent catches around all three calls regardless, treating a caught exception as a
  non-contributing outcome with no fallback signal available (`"error"` event).
- **Flattening (design §5)**, same discipline as Case Summarization: canonical RAG-then-GRAPH-
  then-SQL order, only the tools that actually contributed, concatenated before the generation
  prompt is built — the exact same list/order is what `verify_grounding()` sees.
- **Bounded payload:** one synthesized answer with citations rolled up across all three sources —
  never three separate result sets, never raw chunks. `caveats` names any degraded source (via
  `SOURCE_TOOL_DISPLAY_LABELS`) when `status=PARTIAL`.
- `src/pipeline/harness/supervisor.py::register()` — a real `InvestigativeAnalysis`-equivalent
  callable (`investigative_analysis`) registers itself under `INVESTIGATIVE_ANALYSIS` at import
  time, the same pattern every prior sub-agent module used.
- `tests/test_harness_agent_investigative_analysis.py` — 13 tests: full success (all three
  contribute), two degradation/dedup shapes (both siblings falling back to RAG; one sibling
  degrading while the other two contribute), a defensive-exception-treated-as-degraded case,
  all-three-failure -> `ABSTAINED` (both via empty/fallback results and via raised exceptions),
  the live per-source event sequence (asserting the full five-value-vocabulary mapping including
  the RAG-specific "error, not retry" case), `on_event` being optional, the Verifier-rejection
  roll-up-reset divergence (asserting the per-tool events still said `"done"` while the final
  roll-up resets), a generation-failure case, module self-registration, and a
  `Supervisor.handle()` -> real Investigative Analysis -> real `rag_tool()`/`graph_tool()`/
  `sql_tool()` integration test via the SQL route, asserting the Supervisor's own two events plus
  this sub-agent's three per-source events (5 total) all arrive through one forwarded sink.

**Classification reachability — stated plainly, per this session's explicit instruction.** Read
`src/pipeline/harness/supervisor.py`'s `_ROUTE_TO_SUBAGENT` table before writing any code:
`"SQL": INVESTIGATIVE_ANALYSIS` is present (set in Phase 1, unrelated to this session's own work),
so this sub-agent IS reachable via real classification today — but ONLY through the SQL route.
`router.py`'s RAG and GRAPH/GRAPH_HYBRID routes map to Semantic Search and Case Summarization
respectively (Phase 1's own table), not to this sub-agent; there is no route whose classification
intent is "deep synthesis across RAG+GRAPH+SQL at once." **PARTIALLY reachable** — not gapped like
Timeline Building (zero routes), not fully reachable like Cross-Case Linkage (both its routes map
to it). No new classification keyword/pattern was invented to close this partially, per Phase 1/5's
own precedent — that would be new classification logic layered on top of, not reuse of, router.py's
tuned classifier, and needs the same live-failure evidence XAGG/XGRAPH/XNETWORK's own overrides had.

**One contract gap found and resolved before writing any code, not guessed — see the "Contract
Amendment — SubAgent on_event threading" entry immediately above.** Confirmed, per this session's
own instruction, against `supervisor.py`/`types.py` directly: `Supervisor.handle()` had an
`on_event` sink with nothing to hand it to. Resolved via `AskUserQuestion` as the full-contract-
amendment option (the user's own choice over a scoped, Investigative-Analysis-only alternative),
mirroring §10/§11's pattern.

**No other deviations.** Bounded payload holds throughout — no `EvidenceChunk`/raw chunks/raw rows
ever cross this module's boundary; only `answer_text`, `citations`, `tools_used`, `degraded_from`,
and `caveats` are populated. Validation gate explicitly not wired this session, same as every
prior sub-agent — a `# TODO(validation-gate)` marker is left at its intended insertion point,
flagged in the module's own docstring as MANDATORY full-semantic-check tier once `validation.py`
exists (plan §5's table — same tier as Cross-Case Linkage / Report Drafting, not the lighter
structural-only check some other sub-agents get).

**Verification:** full existing test suite (`pytest`, `testpaths = tests`) — 950 passed, 4 skipped
(the same pre-existing, unrelated docling/PDF `std::bad_alloc` environment failure documented
present on main since Phase 0's own merge — confirmed still present identically, not a regression),
0 failed (937 baseline + this session's 13 new tests, accounting for the 2 new supervisor tests
counted in the contract-amendment entry above). Compliance suite
(`src/pipeline/harness/compliance/`, run separately per `pytest.ini`'s `testpaths = tests` scoping)
— 51/51 checks still passing, unchanged count from Phase 0. Nothing in `main.py`, `orchestrator.py`,
or `router.py`'s existing behavior was touched — still not wired into live traffic, per §6.

---

### Contract Amendment — `ExecutionContext.session_id` & `SubAgent` `gateway` threading (pre-Phase-8): COMPLETE

Branch `feature/harness-contract-amendment-session-gateway`, merged to main via merge commit
`a0b8896`. Full spec in §13.

**Two gaps confirmed against the repo before writing any Phase 8 code, not guessed** — no harness
type carried a `session_id` anywhere (needed to match `_generate_file()`'s existing
`gateway.log_generated_file(session_id, user_id, case_id, ...)` call, since `user_id`/
`active_case_id` already lived on `CallerContext` but `session_id` did not), and no sub-agent built
through Phase 7 touched `DataGateway` at all. Resolved via `AskUserQuestion` as a full contract
amendment (over a scoped, no-DB-persistence alternative also offered), the same amend-once-for-
everyone approach §10/§11/§12 already used.

**Built:** `ExecutionContext.session_id: Optional[str] = None` (additive, same shape as §10's
`project_id`/`workspace_id`). `SubAgent.__call__`/`Supervisor.handle()` both widened with a new
keyword-only `gateway: Optional[DataGateway] = None`, forwarded unchanged at `Supervisor.handle()`'s
one dispatch call site — mirrors `on_event`'s own §12 amendment exactly. `DataGateway`
(`src/data_gateway/base.py`) is already an abstract `Protocol`, not a concrete implementation type,
so this does not violate the interfaces doc's "no implementation types cross a boundary" rule.
All 6 already-shipped sub-agents retrofitted to accept-and-ignore the new kwarg.
`tests/test_harness_supervisor.py` — `_mock_sub_agent()`'s stub now records `gateway`; 2 new tests
assert forwarding (a real object, and `None`), mirroring the existing `on_event` forwarding tests.

**No other deviations.** Additive on `ExecutionContext`; zero effect on any already-shipped
sub-agent's behavior — every one of them still ignores `gateway` exactly as they already ignore
`on_event` where they don't use it.

**Verification:** full existing test suite (`pytest`, `testpaths = tests`) — exit code 0, same 4
pre-existing skips, no regressions. Compliance suite — 51/51, unchanged. Nothing in `main.py`,
`orchestrator.py`, or `router.py`'s existing behavior was touched.

---

### Phase 8 — Report Drafting: COMPLETE

Branch `feature/harness-phase-8-report-drafting`, merged to main via merge commit `8c5fbb7`.

**Built:**
- `src/pipeline/harness/agents/report_drafting.py` — composes Case Summarization's own
  `SubAgentResult` (`case_summarization()` invoked directly as a function, never through the
  Supervisor, never re-invoking RAG/GRAPH tools — design §3's explicit PRESERVE, confirmed by
  reading `case_summarization.py` before writing this module) and the existing, unmodified
  `structure_for_file()`/`build_pdf()`/`build_xlsx()`/`build_docx()` pipeline `_generate_file()`
  already uses (confirmed by reading all four before assuming any signature/return shape).
- `src/pipeline/citation_consistency.py` — new this session (see the discrepancy note below for
  why). A deterministic, non-LLM check reusing `verifier.py`'s own `[Document N]` citation-marker
  regex as the one canonical pattern, run **before** the Verifier per plan §5's exact placement.

**Two discrepancies confirmed against the repo before writing any code, both resolved via
`AskUserQuestion` rather than guessed:**

1. **`citation_consistency.py` didn't exist.** SUBAGENT_INTERFACES.md §2.1.3's own fixed 5-step
   ordering contract doesn't name a citation-consistency step at all (Verifier → build →
   disclosure); only plan §5's trust-layer table and §4 row 7's build note require it, and plan §8
   listed it as its own separate, later, unchecked item. Offered defer-with-TODO (matching how
   `validation.py` has been deferred every prior phase) vs. build-now. **User chose build-now.**
   §8's checklist updated above to reflect both being delivered together this phase.
2. **`GeneratedFileRef.file_id` had no path to a real, persisted id.** No sub-agent touched
   `DataGateway`, and no harness type carried `session_id`, both needed for
   `gateway.log_generated_file()`. Offered local-uuid4-no-persistence vs. thread-gateway-through.
   **User chose thread-gateway-through** — resolved as its own prerequisite contract amendment,
   merged to main before this branch was cut (see the entry immediately above).

**The core design question this session had to resolve, not left implicit:** SUBAGENT_INTERFACES.md
§2.1.3 step 3 requires a real Verifier run with a real fail branch, which only makes sense if step 2
("compose the report's evidentiary content") produces something NEW that could fail grounding — a
straight passthrough of Case Summarization's already-verified text would leave that fail branch dead
code. But `Citation` (unlike `EvidenceChunk`) carries no chunk text, and Report Drafting — per design
§3 — only ever receives Case Summarization's bounded `SubAgentResult`, never its internal chunks.
**Resolved:** Report Drafting generates ONE new drafted report body via a single LLM call, in a
single-synthetic-citation regime (`[Document 1]` only, standing for the whole of Case Summarization's
evidentiary text as one trusted source) — the same "synthetic chunk wrapper" pattern design §5
already documents for SQL/XAGG/XNETWORK, not a new mechanism. `check_citation_consistency()` then has
real, well-scoped teeth: with exactly one valid index, any `[Document N]` with `N != 1` the redraft
invents is caught deterministically before the Verifier ever runs.

**§2.1.3's exact 5 steps, with citation-consistency inserted before step 3 per plan §5:** Case
Summarization's payload → strip any inherited GRAPH-only disclosure (detected **structurally** via
`degraded_from == ["RAG"]`, Case Summarization's own exact signal — never by string-matching the
still-`[PROVISIONAL — PENDING PRODUCT SIGN-OFF]` wording) → draft via one LLM call → citation-
consistency check (fails → `ABSTAINED`, no document built) → `verify_grounding()` over the drafted
text against the one synthetic chunk (fails → `ABSTAINED`, no document, no disclosure) → document
assembled via `structure_for_file()` + the appropriate builder (any exception → `ABSTAINED` with an
explicit file-generation error, matching `_generate_file()`'s own `except Exception` shape verbatim —
a distinct failure mode from a Verifier rejection) → if `status == PARTIAL`, disclosure injected per
the suppression rule (RESOLVED-2a) and `disclosure_rendered = True` set.

**Suppression rule, implemented structurally rather than by string-matching:** `degraded_from ==
["RAG"]` (Case Summarization's own GRAPH-only branch) → the gap is already disclosed in the inherited
text; propagate the **same** `GRAPH_ONLY_SUMMARY_DISCLOSURE` string forward, never a second,
differently-worded statement of the same gap. `degraded_from == ["GRAPH"]` (RAG-only branch, no
upstream disclosure per §2.1.2) → Report Drafting is the first to disclose; inject
`PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE` with `{unavailable_sources}` routed through
`SOURCE_TOOL_DISPLAY_LABELS` (plan §7.4's own explicit requirement — never the raw enum value).

**A real gap found and fixed while wiring disclosure injection, not assumed away:** `build_xlsx()`
only ever renders a payload's first `"table"` section or, absent one, its `description` — confirmed
by reading `xlsx_builder.py` before writing the injection code — it never reads `"paragraph"`
sections at all, unlike `pdf_builder.py`/`docx_builder.py`. A leading paragraph section alone would
silently drop the disclosure from any xlsx report whose drafted content happens to structure into a
table, making `disclosure_rendered=True` an empty promise for that one format.
`_inject_disclosure_into_payload()` now also writes into `payload["description"]` and, when a table
section exists, inserts a leading disclosure row into that same table — so the field's own contract
("must never be set optimistically") holds for all three formats, not just the two that support
arbitrary paragraphs.

**File-id persistence:** `_persist_generated_file()` calls `gateway.log_generated_file()` (matching
`_generate_file()`'s existing shape) only when `gateway`, `session_id`, and `user_id` are all
present; otherwise falls back to a locally generated, unpersisted `uuid4()` with a logged warning —
never crashes on an absent optional collaborator, matching every prior sub-agent's own posture
toward missing pieces it doesn't strictly require to produce a result.

**Validation gate explicitly not wired this session**, same as every prior sub-agent —
`validation.py` does not exist yet. A `# TODO(validation-gate)` marker is left at its intended
insertion point (after the Verifier passes, before the document is assembled), flagged explicitly
in-line that plan §5 requires the **full semantic tier** here (same as Cross-Case Linkage /
Investigative Analysis), not the lighter structural-only check some other sub-agents get — so a
future session doesn't default to the cheaper tier by omission.

**No other deviations.** Bounded payload holds throughout — no `EvidenceChunk`/raw chunks ever cross
this module's boundary. `Supervisor.classify_to_subagent()` confirmed unchanged: `output_format in
{file_pdf, file_xlsx, file_docx}` still overrides to Report Drafting regardless of route, so this
sub-agent is reachable via real, unmodified classification today — verified with an integration test
through `Supervisor.handle()`, not assumed, and not bypassed the way Timeline Building's own
integration test had to be.

**Built:**
- `tests/test_citation_consistency.py` — 7 tests over the new deterministic check (in-range,
  out-of-range, zero-index, no-citations-is-consistent, deduplicated/sorted invalid indices,
  zero-valid-citations, case-insensitive marker matching).
- `tests/test_harness_agent_report_drafting.py` — 16 tests: the disclosure-ordering contract
  (`OK`/no-disclosure, inherited-disclosure suppression, fresh-disclosure injection, xlsx
  table-row survival), citation-consistency-failure-aborts-before-the-Verifier,
  Verifier-rejection-aborts-before-any-document-is-built, file-build failure, all three of Case
  Summarization's terminal statuses (`EMPTY`/`ABSTAINED`/`DENIED`) propagating untouched, invalid
  `output_format` handled without crashing, `_persist_generated_file()`'s gateway/no-gateway/
  missing-session_id fallback behavior, module self-registration, and the
  `Supervisor.handle()` → real classification → Report Drafting integration test above.

**Verification:** full existing test suite (`pytest`, `testpaths = tests`) — exit code 0, same 4
pre-existing skips (the unrelated docling/PDF `std::bad_alloc` environment failure, confirmed still
present identically, not a regression) plus this session's 23 new tests (7 + 16), all passing.
Compliance suite (`src/pipeline/harness/compliance/`, run separately per `pytest.ini`'s
`testpaths = tests` scoping) — 51/51 checks still passing, unchanged count from Phase 0. Nothing in
`main.py`, `orchestrator.py`, or `router.py`'s existing behavior was touched — still not wired into
live traffic, per §6.

---

### Contract Amendment — `SubAgentResult.metrics` / `DataQualityMetric` (pre-Phase-9): COMPLETE

Branch `feature/harness-contract-amendment-data-quality-metrics`, merged to main via merge commit
`357fb48`. Full spec in §14.

`docs/SUBAGENT_INTERFACES.md` §2.1 confirmed, before any Phase 9 code was written, to genuinely have
no `SubAgentResult`-shaped contract for Data-Quality/Extraction-Coverage at all — its own module
docstring in `supervisor.py` already flagged the discrepancy, but flagging is not the same as
resolving it, and this session's brief was explicit that closing it was this session's own first
real design task. Resolved via `AskUserQuestion` (new typed field vs. prose in `answer_text` — see
§14) as a full contract amendment, merged before Phase 9's own branch was cut, the same
amend-once-for-everyone approach §10/§11/§12/§13 already used.

**Built:** `DataQualityReadiness` (ready/thin/unavailable/unknown — `thin` reserved, unused for MVP;
`unknown` a fourth state mirroring RESOLVED-5's `ConflictState`, distinct from `unavailable`),
`DataQualityMetric` (one metric group's bounded payload), `SubAgentResult.metrics` (additive,
default-empty). 5 new tests in `tests/test_harness_types.py`.

**No other deviations.** Additive on `SubAgentResult`; zero effect on any already-shipped sub-agent,
same as §11's own `events`/`links` amendment.

**Verification:** full existing test suite (`pytest`, `testpaths = tests`) — 0 failures, same 4
pre-existing skips (the unrelated docling/PDF `std::bad_alloc` environment failure, confirmed still
present identically, not a regression) plus this session's 5 new tests. Compliance suite — 51/51,
unchanged. Nothing in `main.py`, `orchestrator.py`, or `router.py`'s existing behavior was touched.

### Phase 9 — Data-Quality/Extraction-Coverage: COMPLETE

Branch `feature/harness-phase-9-data-quality`, merged to main via merge commit `8ab44aa`.

**Built:**
- `src/pipeline/harness/agents/data_quality.py` — composes THREE backends directly (Postgres, AGE,
  Chroma), none of them `DataGateway`-mediated and none of them the Phase 0 tool wrappers, mirroring
  Timeline Building's own precedent of bypassing `graph_tool()` where the tool wrapper's shape
  genuinely cannot serve the need. Confirmed before writing any code, per this session's explicit
  instruction: `get_kb_stats()` (`src/data_gateway/direct_backend.py`) is GLOBAL, not case-scoped
  (no `case_id` filter anywhere in its own query), so it is NOT reused — `documents`
  (Postgres, case-scoped via its own `case_id` column) is queried directly instead.
- **A confirmed, real schema gap, not guessed around:** `ingestion_jobs` carries NO `case_id`
  column at all, and the only live document-upload path (`POST /api/admin/kb/upload`) always
  ingests `is_global=True` — case-scoped `Document` rows are written by offline scripts calling
  `ingest_file(case_id=...)` directly, which never create an `IngestionJob` row. Consequence:
  "failed/quarantined count" (part of plan §7.3's "document coverage" row) cannot be attributed to
  a specific case anywhere in the current schema. Reported honestly: `document_coverage`'s `counts`
  carries only what is actually case-attributable, plus a fixed, always-present caveat naming the
  gap.
- **Six metric groups, per §7.3's table**, each independently fetched and independently
  failure-isolated (one group's query raising does not prevent the other five from computing —
  this sub-agent's entire purpose is diagnostic, so an infrastructure hiccup in one backend must not
  silence a still-healthy reading from another). Entity-extraction node labels
  (Person/Vehicle/PhoneNumber/Address/Organization/Weapon) confirmed against the REAL graph
  vocabulary (`entity_resolution.py::TYPE_TO_LABEL`, `graph_retriever.py::_SEED_LABELS`,
  `ingestion/service.py`'s weapon-node write path) before hardcoding §7.3's prose list, not copied
  verbatim — §7.3 says "phone"/"address"/"org"/"weapon", the actual node labels are
  `PhoneNumber`/`Address`/`Organization`/`Weapon`. Identity-health's three literal `SAME_AS.status`
  values (`pending`/`confirmed`/`rejected`) confirmed the same way, against
  `entity_resolution.py`/`community_detection.py`/`graph_retriever.py`'s own module docstring
  ("SAME_AS IDENTITY IS CONFIRMED-ONLY... status in {pending, confirmed, rejected}"), not assumed
  to exist as literal values.
- **New within-case Cypher templates, ALL routed through `scoped_cypher()`** per design §4.5,
  the same instruction Timeline Building's own session followed. Aggregation
  (`GROUP BY`-shaped queries) deliberately avoided in favor of one small query per discrete value
  (one per entity label, one per `SAME_AS` status) — AGE's aggregate/group-by support has no proven
  precedent anywhere in this codebase, and given the documented history of live AGE Cypher-support
  surprises in this exact area (`conflict_detection.py`'s own OPTIONAL-MATCH-ordering bug and
  missing-`columns` bug), this session does not introduce an unproven construct into new production
  code.
- **Readiness rule, decided and documented per this session's explicit instruction (§14.1):** each
  group has one defining raw count; `UNAVAILABLE` iff that count is exactly 0, `READY` iff > 0,
  `THIN` never constructed this session (no calibration data exists to set a threshold), `UNKNOWN`
  iff the group's own query raised.
- **Whole-result status, decided and documented:** no active case → `EMPTY` (mirrors Timeline
  Building's identical precedent). All six groups `UNKNOWN` → `ABSTAINED`, reusing RESOLVED-4's
  "all attempted sources failed" vocabulary (its origin case, Investigative Analysis) for
  consistency across the harness even though nothing generated here needs "safe" withholding in the
  hallucination sense that status exists for elsewhere. Any other mix → `PARTIAL` (with a caveat
  naming which groups); all six computed → `OK`.
- **`tools_used`/`degraded_from` decided to stay `[]` unconditionally, not guessed either way:**
  `SourceTool` has no member for Postgres or Chroma, and this sub-agent spans three heterogeneous
  backends where only one group is AGE-sourced — tagging only the graph-sourced groups `"GRAPH"`
  while leaving the Postgres/Chroma groups untagged would misattribute. The actual granular
  degradation signal for this sub-agent is each `DataQualityMetric.readiness`/`.error`, not
  `tools_used`/`degraded_from`.
- **No role gate**, confirmed not assumed: none of the six metric groups touches a cross-case tool
  or cross-case Cypher template. `SubAgentStatus.DENIED` is never built as an output path here —
  same precedent as Case Summarization/Timeline Building.
- **No Verifier call anywhere** — nothing generated, nothing to verify; `answer_text` is
  deterministic string formatting over already-computed counts, the same "structurally cannot
  hallucinate" property plan §7.3 calls out explicitly as worth preserving deliberately, extended
  here to the whole sub-agent (Timeline Building's own session established the identical property
  for one field; this extends it to all of it).
- **Validation gate: confirmed structurally exempt, not merely unwired this session.** Plan §5's
  table does not list Data-Quality among the "full semantic tier" sub-agents — confirmed here as
  "non-generative, no `verify_grounding()` boundary to insert a check after," the same reasoning
  Timeline Building's own session already established, not left to a future session to rediscover.
  No `# TODO(validation-gate)` marker is left, since (like Timeline Building) there is no insertion
  point to mark.
- `src/pipeline/harness/supervisor.py::register()` — a real `data_quality` callable registers
  itself under `DATA_QUALITY` at import time, the same pattern every prior sub-agent module used.
- `tests/test_harness_agent_data_quality.py` — 13 tests: `_run_metric()`'s readiness classification
  at its boundary conditions (count==0 → `UNAVAILABLE`, count>0 → `READY`, fetch exception →
  `UNKNOWN`, a defensive missing-primary-key case), whole-result status (all `READY`/`UNAVAILABLE`
  mix → `OK`, some `UNKNOWN` → `PARTIAL` with a caveat naming which, all six `UNKNOWN` →
  `ABSTAINED`), no active case → `EMPTY`, a full success case wired through the REAL fetch functions
  with Postgres/AGE/Chroma stubbed at their own boundary (not `_FETCHERS` itself) proving the actual
  query construction for each backend, one group's failure isolated from a sibling sharing the same
  underlying graph data, structural caveats always present, no role gate across all four `Role`
  values, module self-registration, and a `Supervisor.handle()` → real Data-Quality → real fetch
  functions integration test.

**Classification reachability — stated plainly, not closed this session, same as Timeline
Building's own precedent.** Confirmed by reading `supervisor.py`'s `_ROUTE_TO_SUBAGENT` table
before writing any code: no route maps to `DATA_QUALITY` — the same gap Phase 1's own progress-log
entry already named for this exact sub-agent ("no route was ever built for 'how much evidence
exists for this case'... a NEW capability per plan §7.3, with no predecessor in `orchestrator.py`
at all"). No new trigger keywords/patterns were invented to close this, per the same instruction
every prior phase that hit this gap followed — new classification logic needs the same
live-failure evidence basis XAGG/XGRAPH/XNETWORK's own deterministic overrides had, not guessed
patterns. Built, registered, and tested via direct dispatch and a Supervisor integration test that
bypasses `route_query()`'s real classification (monkeypatching `classify_to_subagent()`).

**No other deviations.** Bounded payload holds throughout — no raw Postgres rows or graph rows ever
cross this module's boundary; only `answer_text`, `metrics`, and `caveats` are populated.

**Verification:** full existing test suite (`pytest`, `testpaths = tests`) — 0 failures, same 4
pre-existing skips (the unrelated docling/PDF `std::bad_alloc` environment failure, confirmed still
present identically, not a regression) plus this session's 13 new tests. Compliance suite
(`src/pipeline/harness/compliance/`, run separately per `pytest.ini`'s `testpaths = tests` scoping)
— 51/51 checks still passing, unchanged count from Phase 0. Nothing in `main.py`, `orchestrator.py`,
or `router.py`'s existing behavior was touched — still not wired into live traffic, per §6.

---

### Fix — Verifier confidence_status/hedging gap (post-Phase-9, pre-Validation): COMPLETE

Branch `fix/verifier-confidence-status-hedging`, merged to main via merge commit `17b2785`.

**A real, live gap, confirmed by reading the code, not guessed — the exact one
`AGENT_HARNESS_DESIGN.md` §7 named and left deliberately open:** `src/pipeline/verifier.py`
predates the harness and is reused UNCHANGED by every sub-agent per design §5's decision — but its
`_check_hedging()`/`_format_chunks_for_verifier()` only ever read a top-level `chunk["graph_confidence"]`
key, the LEGACY shape `orchestrator.py`'s own pre-harness GRAPH/XGRAPH routes still pass (confirmed
against `graph_retriever.py` before changing anything — `retrieve_graph()` really does set
`graph_confidence` directly on the chunk dict, not nested under `metadata`, so this path had to be
preserved exactly). Every harness sub-agent, however, flattens an `EvidenceChunk` to
`{"id", "text", "metadata": chunk.metadata.model_dump()}` per SUBAGENT_INTERFACES.md §2's "Verifier
boundary" note — confidence lives at `metadata["confidence"]`/`metadata["confidence_status"]`, a key
`_check_hedging()` never read at all. Consequence: the hedging check has been a silent no-op for
every harness-sourced, GRAPH-derived chunk through Phase 9 (Case Summarization, Cross-Case
Linkage's XNETWORK path, Investigative Analysis) — `semantic_search.py`'s own module docstring had
already flagged the gap in passing ("does not need solving here: RAG never computes a per-chunk
confidence... regardless of how that bridge eventually gets wired") but no session had actually
wired it, and design §7's own closing line calls this out directly: "Worth deciding before the
Verifier's hedging behavior is relied on in the harness." Closed now, before Validation (which
reuses the Verifier's own citation-parsing machinery) is built on top of it.

**Built:**
- `_effective_confidence(chunk)` — new helper resolving `(confidence, status)` from EITHER shape:
  legacy top-level `graph_confidence` (checked first, preserved exactly, treated as
  `confidence_status="computed"`) OR the harness's `metadata.confidence`/`metadata.confidence_status`.
  Backward-compatible by construction — no existing (pre-harness) caller's behavior changes.
- `_check_hedging()` — now requires hedging when `confidence_status == "check_failed"`
  UNCONDITIONALLY (regardless of `confidence` itself, normally `None` for that status), in addition
  to the existing `confidence < 0.85` rule. **[AMENDMENT]** This is the fix design §7 asked for by
  name: a chunk whose confidence computation raised must be treated at LEAST as cautiously as a
  known-low score, never as "no signal, proceed unhedged" — the same shape of false-all-clear fix
  RESOLVED-5's `ConflictState` already applied one layer up, for a different field.
- `_format_chunks_for_verifier()` — displays `"confidence: unknown (check failed)"` for
  `check_failed` chunks (so the LLM judge sees the same unresolved-risk signal the deterministic
  check acts on), and reads confidence through the same shared helper for the `computed` case;
  `not_computed` chunks show nothing, unchanged from before.
- `tests/test_verifier.py` — 9 new tests: the harness chunk shape's `computed`/`not_computed`/
  `check_failed` states through both `_check_hedging()` and `_format_chunks_for_verifier()`,
  including `check_failed` requiring a hedge unconditionally and passing once one is present. All 34
  pre-existing tests (the legacy `graph_confidence` shape) pass unchanged — confirming the fix is
  additive, not a behavior change to the still-live pre-harness path.

**No other deviations.** `verify_grounding()`'s signature and every other check
(`_check_temporal`/`_check_leakage`/`_check_refusal`/`_check_no_citation`) untouched.

**Verification:** full existing test suite (`pytest`, `testpaths = tests`) — 0 failures, same 4
pre-existing skips (the unrelated docling/PDF `std::bad_alloc` environment failure, confirmed still
present identically, not a regression) plus this session's 9 new tests. Compliance suite — 51/51,
unchanged. Nothing in `main.py`, `orchestrator.py`, or `router.py`'s existing behavior was touched —
the legacy `graph_confidence` path's behavior is bit-for-bit identical to before this fix.

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

---

## 12. Contract amendment — SubAgent `on_event` threading (pre-Phase-7)

`Supervisor.handle()` (Phase 1) has accepted an `on_event: Optional[Callable[[PipelineEvent], None]]`
keyword parameter since it was first built, but only ever used it to emit its own two events
(classification, then outcome). `SubAgent.__call__` (§2.0, `types.py`) took only `agent_input` — no
sub-agent had a channel back to that sink. SUBAGENT_INTERFACES.md §2.1.4/RESOLVED-4a requires
Investigative Analysis to emit one `PipelineEvent` per source-tool outcome as it resolves — this
surfaced the gap concretely for the first time, the same way Case Summarization's project-scoping
need surfaced §10's gap and Timeline Building's per-item payload need surfaced §11's.

**Resolved, via `AskUserQuestion`, as the full-contract-amendment option** (over a
scoped/investigative-analysis-only alternative also offered) — the same amend-once-for-everyone
approach §10/§11 already used, rather than a growing pile of per-sub-agent workarounds.

### 12.1 `SubAgent.__call__` — widened, not replaced

```python
OnEventCallback = Callable[["PipelineEvent"], None]


class SubAgent(Protocol):
    name: str

    async def __call__(
        self,
        agent_input: SubAgentInput,
        *,
        on_event: Optional[OnEventCallback] = None,
    ) -> SubAgentResult: ...
```

`on_event` is additive and keyword-only, defaulting to `None` — every sub-agent's own signature
must accept it (even if it ignores it), but nothing about `SubAgentInput`, `SubAgentResult`, or any
existing call with no `on_event` argument changes behavior. **[PRESERVE, extends design §6/
SUBAGENT_INTERFACES.md §2.2]** This reuses the exact `PipelineEvent` type and callback shape that
already exists — no webhook, callback registry, subscription mechanism, or message bus is
introduced. A sub-agent with nothing granular to report (every single-tool sub-agent) simply never
calls it.

### 12.2 `Supervisor.handle()` — forwards its own parameter unchanged

The existing `handler(agent_input)` call site becomes `handler(agent_input, on_event=on_event)` —
`on_event` is `Supervisor.handle()`'s own parameter (already existed, possibly `None`), passed
straight through, not reconstructed or wrapped.

### 12.3 Retrofit scope — what already-shipped code this touches

- `src/pipeline/harness/types.py` — add `OnEventCallback`, widen `SubAgent.__call__`.
- `src/pipeline/harness/supervisor.py` — one call-site change in `Supervisor.handle()`.
- `src/pipeline/harness/agents/{semantic_search,large_scale_aggregate,case_summarization,
  timeline_building,cross_case_linkage}.py` — each accepts-and-ignores the new kwarg.
- `tests/test_harness_supervisor.py` — `_mock_sub_agent()`'s stub handler accepts and records
  `on_event`; two new tests assert forwarding (both with a real callback and with `None`).

### 12.4 What this does not do

Sub-agents composing more than one tool that do NOT yet emit per-source events (Case
Summarization, Cross-Case Linkage) are **not** retrofitted to emit them in this amendment —
SUBAGENT_INTERFACES.md §2.1.4's "Generalization" note observes they are candidates, but doing so
is out of this amendment's own scope (it widens the *contract*, Investigative Analysis is the
first and only *user* of it this session). Tracked as future work, same as §10/§11's own reserved,
unused fields being deliberately inert until a later session needs them.

---

## 13. Contract amendment — `ExecutionContext.session_id` & `SubAgent` `gateway` threading (pre-Phase-8)

Report Drafting needs to persist a generated file record the same way `orchestrator.py`'s existing
`_generate_file()` does — via `gateway.log_generated_file({session_id, user_id, case_id, file_type,
file_name, file_size_bytes, storage_path})`, which returns the real, durable `file_id`. Two gaps
surfaced when checking this against the settled contract before writing any Report Drafting code:

- `user_id` and `active_case_id` already live on `CallerContext`, but **no harness type carries a
  `session_id` anywhere.**
- **No sub-agent built through Phase 7 touches `DataGateway` at all** — there was no channel to
  reach it from inside a sub-agent's own `__call__`.

Resolved, via `AskUserQuestion`, as a full contract amendment (over a scoped, no-DB-persistence
alternative also offered) — the same amend-once-for-everyone approach §10/§11/§12 already used.

### 13.1 `ExecutionContext.session_id` — additive field

```python
class ExecutionContext(BaseModel):
    ...
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "The chat session this query belongs to. Needed by Report Drafting to "
            "persist a generated file via DataGateway.log_generated_file(), which "
            "requires session_id alongside user_id (already carried on `caller`) — "
            "mirrors _generate_file()'s existing session_id/user_id/case_id shape. "
            "None where no session exists (e.g. a standalone harness invocation or "
            "most existing tests) — every sub-agent besides Report Drafting is "
            "unaffected."
        ),
    )
```

Additive, default-`None` — zero effect on any already-shipped sub-agent or its tests, same shape as
§10's `project_id`/`workspace_id` additions to the same model.

### 13.2 `SubAgent.__call__` / `Supervisor.handle()` — widened with `gateway`, mirrors §12's `on_event`

```python
class SubAgent(Protocol):
    name: str

    async def __call__(
        self,
        agent_input: SubAgentInput,
        *,
        on_event: Optional[OnEventCallback] = None,
        gateway: Optional[DataGateway] = None,
    ) -> SubAgentResult: ...
```

`gateway` is additive and keyword-only, defaulting to `None` — every sub-agent's signature must
accept it (even if it ignores it), same "widen, not replace" treatment `on_event` got in §12.
`DataGateway` (`src/data_gateway/base.py`) is already an abstract `Protocol`, not a concrete
implementation type, so this does not violate SUBAGENT_INTERFACES.md's "no implementation types
cross a boundary" stability rule — it is the same kind of interface `main.py`/`orchestrator.py`
already pass around today. `Supervisor.handle()` gains the identical `gateway` parameter and
forwards it unchanged at its one dispatch call site, exactly like `on_event`.

### 13.3 Retrofit scope — what already-shipped code this touches

- `src/pipeline/harness/types.py` — add `ExecutionContext.session_id`; widen `SubAgent.__call__`
  with `gateway`; import `DataGateway`.
- `src/pipeline/harness/supervisor.py` — `Supervisor.handle()` gains `gateway`, forwarded unchanged
  at its one `handler(...)` call site.
- `src/pipeline/harness/agents/{semantic_search,large_scale_aggregate,case_summarization,
  timeline_building,cross_case_linkage,investigative_analysis}.py` — each accepts-and-ignores the
  new kwarg, same as every one of them already does for `on_event`.
- `tests/test_harness_supervisor.py` — `_mock_sub_agent()`'s stub handler accepts and records
  `gateway`; two new tests assert forwarding (both with a real object and with `None`), mirroring
  the existing `on_event` forwarding tests exactly.

### 13.4 What this does not do

This amendment only widens the contract; it does not wire a real `DataGateway` instance into any
live call path — no sub-agent module is invoked from `main.py`/`orchestrator.py` yet (§6). It also
does not add `session_id` anywhere except `ExecutionContext` — `SubAgentInput`, `CallerContext`, and
every tool-level `ToolInput` are untouched, since no tool needs it. Report Drafting (Phase 8) is the
first and, this session, only consumer of both `session_id` and `gateway`; every other sub-agent's
behavior is unchanged.

---

## 14. Contract amendment — `SubAgentResult.metrics` / `DataQualityMetric` (pre-Phase-9)

`docs/SUBAGENT_INTERFACES.md` §2.1 titles its table "the seven sub-agents" and never defines a
`SubAgentResult`-shaped contract for Data-Quality/Extraction-Coverage at all — this plan's own §4
row 8 and §7.3 add it as an eighth, with a "final shape" (six metric groups, raw counts, a
per-capability readiness state) but no field on `SubAgentResult` to actually carry them. Confirmed
against both docs directly before writing any Phase 9 code, per this session's explicit instruction
not to assume a discrepancy already flagged elsewhere (`supervisor.py`'s own module docstring) was
also already resolved.

Resolved via `AskUserQuestion` — offered as prose-in-`answer_text` (Case Summarization's precedent)
vs. a new typed field (the `events`/`links`/`generated_file` precedent). **User chose the new typed
field.** Six groups' worth of `{name, readiness, counts, explains, error}` is machine-legible
tabular data, not a narrative — the same shape argument that justified `events`/`links` for Timeline
Building/Cross-Case Linkage rather than folding them into prose.

### 14.1 `DataQualityReadiness` — four states, not three

```python
class DataQualityReadiness(str, Enum):
    READY = "ready"              # Checked; the group's defining raw count is > 0.
    THIN = "thin"                # RESERVED. Unused for MVP — no calibration data exists
                                  # yet to set a threshold correctly (plan §7.3).
    UNAVAILABLE = "unavailable"  # Checked; the group's defining raw count is exactly 0.
    UNKNOWN = "unknown"          # The group's own underlying query raised. Assert nothing.
```

`UNKNOWN` mirrors RESOLVED-5's `ConflictState`/the `confidence`/`confidence_status` split
(`docs/AGENT_HARNESS_DESIGN.md` §7): a query that raised must never silently read as "checked,
nothing there" — that is the identical false-all-clear defect those two precedents already exist to
prevent, applied here to a whole metric group instead of one chunk/event.

### 14.2 `DataQualityMetric` — one metric group

```python
class DataQualityMetric(BaseModel):
    name: Literal[
        "document_coverage", "entity_extraction", "timeline_readiness",
        "identity_health", "conflict_coverage", "embedding_coverage",
    ]
    label: str
    readiness: DataQualityReadiness
    counts: dict[str, int] = Field(default_factory=dict)  # empty iff readiness is UNKNOWN
    explains: str  # plan §7.3's own "Explains" column, static per `name`
    error: Optional[str] = None  # present iff readiness is UNKNOWN
```

No raw Postgres rows or graph rows cross the boundary — `counts` is a small, already-aggregated
dict, consistent with the "no raw rows" discipline `docs/SUBAGENT_INTERFACES.md` establishes
everywhere else, even though this sub-agent has no contract written for it there at all.

### 14.3 `SubAgentResult.metrics` — additive

```python
class SubAgentResult(BaseModel):
    ...
    metrics: list["DataQualityMetric"] = Field(
        default_factory=list,
        description="Set only by Data-Quality/Extraction-Coverage. The six metric groups, per plan §7.3.",
    )
```

Additive, default-empty — zero effect on any already-shipped sub-agent, same shape as §11's
`events`/`links` amendment. Resolved via the same forward-ref + `SubAgentResult.model_rebuild()`
pattern `events`/`links` already use (`DataQualityMetric` is defined near `CrossCaseLink`, below
`SubAgentResult`, in `types.py`).

### 14.4 Retrofit scope

- `src/pipeline/harness/types.py` — add `DataQualityReadiness`, `DataQualityMetric`,
  `SubAgentResult.metrics`.
- `tests/test_harness_types.py` — 5 new tests (default-empty, `name` restricted to the canonical
  six, all four readiness states constructible, `UNKNOWN` carries `error` + empty `counts`,
  round-trip through `SubAgentResult`).

No retrofit of any already-shipped sub-agent's code — additive, default-empty, same as §11.
