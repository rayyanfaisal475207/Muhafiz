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
- [ ] Large-Scale Aggregate
- [ ] Case Summarization
- [ ] Timeline Building
- [ ] Cross-Case Linkage
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
