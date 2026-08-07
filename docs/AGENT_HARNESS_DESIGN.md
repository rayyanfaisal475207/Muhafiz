# Agent Harness Design

**Status:** First draft — not implemented. For review before any code is written.
**Scope:** Restructures `src/pipeline/orchestrator.py`'s hand-coded `if/elif` control flow
(see the current-pipeline map from the earlier exploration pass) into a two-layer
supervisor/sub-agent/tool architecture. **Every behavior preserved here is a preservation
requirement, not a suggestion** — this document exists specifically to enumerate the things
a restructuring pass could silently break.

**Out of scope:** `src/mcp/client.py` (the standalone `POST /api/admin/mcp-demo` MCP
Postgres path) is unrelated to the SQL tool below and is not touched by this design.

---

## 1. Two-layer architecture

```
Supervisor (routable)
   │
   ├── Sub-agent: Semantic Search
   ├── Sub-agent: Case Summarization
   ├── Sub-agent: Report Drafting
   ├── Sub-agent: Investigative Analysis
   ├── Sub-agent: Timeline Building
   ├── Sub-agent: Cross-Case Linkage
   └── Sub-agent: Large-Scale Aggregate
          │
          └── compose one or more →  Primitives (tools, src/pipeline/harness/tools/)
                                       RAG · GRAPH/GRAPH_HYBRID · XGRAPH · XAGG ·
                                       XNETWORK · SQL · WEB
```

**Primitives are not independently supervisor-routable.** The supervisor only ever selects a
sub-agent; a tool is only ever invoked from inside a sub-agent's own composition logic. This
is the mechanism that keeps case/role scoping and cross-case structural separation (§3, §4)
enforceable — if the supervisor could reach a tool directly, every enforcement point currently
living inside a specific call chain would need to be re-derived at the supervisor level instead.

---

## 2. Layer 1 — Primitives (`src/pipeline/harness/tools/`)

Each tool wraps an **existing function verbatim** — no new retrieval/generation logic, only a
thin adapter that exposes the current behavior as a harness-callable tool. Signatures below
are the underlying functions being wrapped; the tool boundary must not change them.

### 2.1 RAG retrieval

Wraps: `embed_text()` → `query_similar()` + `get_all_chunks()` → `retrieve_bm25()` →
`rerank_results()` (RRF) → `cross_rerank()` (bge-reranker-v2-m3) → `evaluate_relevance()`.

- **Fallback behavior:** N/A — RAG *is* the fallback target for SQL/GRAPH/GRAPH_HYBRID/WEB
  (below). RAG itself has no further fallback; retry is internal (the `MAX_RETRIES` loop,
  evaluator-feedback-driven query rewrite) and exhaustion abstains (`_SAFE_RESPONSE`), it does
  not reach for WEB automatically (that removal was a deliberate scope change, not a bug —
  preserve it: WEB is only reachable via router classification or the explicit
  `enable_web_search` toggle).
- **Scope:** `case_id`-filtered via `where_clause` built by the calling sub-agent, ANDed with
  `project_id`/`is_global` per the existing precedence rules. No role gate — RAG is available
  to every role, scoping is case-assignment-based, not role-based.

### 2.2 GRAPH / GRAPH_HYBRID traversal

Wraps: `retrieve_graph(query_text, target_entity, case_id, cross_case=False, max_hops=2,
user_id=None, user_role="investigator") -> dict`.

- **Fallback behavior: preserve `route_str = "RAG"` reassignment on failure or empty result.**
  Both `GRAPH` (no chunks *and* no conflict chunks) and `GRAPH_HYBRID` (no combined semantic
  result) currently degrade to the RAG tool, and an evaluator "not relevant" verdict on graph
  chunks does the same. The tool wrapper must surface a `fallback_to_rag: bool` (or equivalent)
  signal the calling sub-agent checks and acts on — this is not automatic inside the tool
  itself today (the orchestrator's `elif` chain does it), so the harness needs an explicit
  substitute.
- **Scope:** within-case only at this call shape (`cross_case=False`). No role gate beyond the
  existing case-assignment check — any role that can see the case can traverse its graph.
- **GRAPH_HYBRID** additionally runs vector/BM25 in parallel (via the RAG tool's primitives)
  and merges at RRF before cross-rerank — it is graph discovery + RAG retrieval fused, not a
  separate retrieval mechanism. Compose it as "GRAPH tool + RAG tool, RRF-merged," not as its
  own independent code path, to avoid the current duplication (GRAPH_HYBRID re-implements query
  expansion/cross-script-variant/BM25-pool-fetch inline rather than sharing RAG's version).

### 2.3 XGRAPH (cross-case graph traversal)

Wraps: `retrieve_graph(..., cross_case=True, ...)` — same function as §2.2, different flag.

- **Fallback behavior: preserve NON-fallback.** XGRAPH never reassigns to RAG on failure or
  empty result — cross-case evidence must never blend into a case-scoped RAG stream. An empty
  result with no `unconfirmed_links` returns a literal "no connections found" response; any
  other failure abstains to `_SAFE_RESPONSE`. Neither path may fall through to the RAG tool.
- **Role gate: `supervisor` / `station-admin` / `platform-admin` only.** Checked *inside*
  `retrieve_graph()` itself when `cross_case=True` — raises `PermissionError` and writes an
  `authorization_violation` audit log on denial. **`current_cross_case`/`current_rls_active`
  are armed only after this check passes, never before** — this is the fix for a documented
  historical bug (the RLS cross-case bypass flag used to arm the instant the router classified
  a query as cross-case, before the role check ran, and was never reset on denial). The harness
  tool wrapper must call the role check before doing anything else that touches Postgres under
  cross-case scope, exactly as `retrieve_graph()` does today — do not let a harness-level
  "resolve scope, then dispatch to tool" restructuring reorder this.

### 2.4 XAGG (cross-case aggregate)

Wraps: `run_aggregate(query_text, target_entity, gateway, user_id=None,
user_role="investigator") -> dict`.

- **Fallback behavior: preserve NON-fallback**, same structural-separation rule as XGRAPH.
  Failure abstains to `_SAFE_RESPONSE`; a verifier rejection falls back to the raw deterministic
  aggregate text (not `_ABSTENTION_RESPONSE` — the aggregate evidence is machine-computed and
  correct by construction, so a verifier rejection means the *generation* paraphrase failed,
  not that the evidence is thin. Preserve this distinction; do not unify it with the generic
  abstention path).
- **Role gate:** identical pattern to XGRAPH — checked first, audit-logged on denial,
  `current_cross_case`/`current_rls_active` armed only after the check passes.
- Two canned aggregate families (relational Postgres group-by via `gateway.get_cases()`, or
  graph node-recurrence count via Cypher), dispatched by keyword match — not a general
  text-to-SQL/Cypher system. Preserve that scoping; do not let a harness "make it smarter"
  pass turn this into free-form query generation.

### 2.5 XNETWORK (cross-case network/theme synthesis)

Wraps: `run_network_query(query_text, gateway, user_id=None, user_role="investigator",
top_k=5) -> dict`, backed by `query_similar_communities()` — embedding search over a
**separate, precomputed community-summary Chroma collection** (written offline by
`community_detection.py`/`community_summarization.py`, not at query time). This is a distinct
retrieval mechanism from GRAPH/XGRAPH — no entity seed, no Cypher traversal.

- **Fallback behavior: preserve NON-fallback** — same structural-separation rule.
- **Unique to XNETWORK: one-shot cloud-regeneration retry on verifier rejection, before
  falling back to raw evidence.** On a first verifier rejection, XNETWORK makes exactly one
  `call_llm(..., force_cloud=True)` regeneration attempt and re-verifies; only if that *also*
  fails (or raises, e.g. blocked by `AIR_GAP_MODE`) does it fall back to presenting the raw
  community-summary text directly. This retry is not present on XAGG or XGRAPH — it exists
  because live testing found the local generation model fails this specific task shape
  reliably (3/3 test runs), the same justification already used for
  `community_summarization.py`'s own narrow cloud-escalation opt-in. **Preserve this as
  XNETWORK-specific, do not generalize it to the other cross-case tools** without the same
  kind of live-failure evidence that justified it here.
- **Role gate:** identical pattern to XGRAPH/XAGG.

### 2.6 SQL lookup

Wraps: `extract_sql_params(query) -> dict` (LLM param extraction) →
`gateway.query_police_reference_data(category=None, subject=None, section_ref=None) ->
list[dict]` (direct parameterized Postgres query). **This is the only in-scope SQL path.**
`src/mcp/client.py`'s `execute_query()` (spawns `npx @modelcontextprotocol/server-postgres`
over stdio) is a separate, standalone integration wired only to `POST /api/admin/mcp-demo` —
it is not called from the chat pipeline today and is not part of this harness.

- **Fallback behavior: preserve `route_str = "RAG"` reassignment** on empty result rows or any
  exception during extraction/query.
- **Scope:** `police_reference_data` is reference data, not case evidence — no case-scoping,
  no role gate.

### 2.7 WEB search

Wraps: `perform_web_search(query, max_results=5) -> list[dict]` (Tavily, domain-allowlisted)
→ on failure/empty, `call_gemini_with_search()` (Gemini grounded search, same domain filter
applied post-hoc via `_filter_allowed_domains()`).

- **Fallback behavior: preserve `route_str = "RAG"` reassignment** — only after *both* Tavily
  and the Gemini fallback have failed. Preserve the two-tier fallback-within-a-fallback shape;
  don't collapse it to a single provider check.
- **`AIR_GAP_MODE`:** disabled entirely, at both call sites, before either provider is reached
  — first thing to verify still holds if this tool is re-wrapped.
- **No case scope, no role gate.** Never cited as case evidence (guardrail from the original
  design intent, still true).

---

## 3. Layer 2 — Sub-agents (routable)

Each sub-agent hands the supervisor a **bounded, summarized payload** — never raw retrieved
chunks, raw SQL rows, or full conversation history. The supervisor's context budget is a
harness-level concern this design must protect; a sub-agent that leaks its internal working
set back up defeats the point of having sub-agents at all.

| Sub-agent | Composes | Hands back | Partial-failure behavior |
|---|---|---|---|
| **Semantic Search** | RAG tool only | Top-N cited passages + a short synthesized answer, not the full reranked set | Evaluator "not relevant" after retry exhaustion → abstain (`_SAFE_RESPONSE`-equivalent), same as today's RAG route |
| **Case Summarization** | RAG tool (case-scoped) + GRAPH tool (case-scoped, capped hops) | One structured case summary (status, key entities, key events, open questions) — never the underlying chunks/graph rows | If GRAPH returns empty, degrade silently to RAG-only summary (mirrors current GRAPH→RAG fallback, §2.2) rather than failing the whole sub-agent |
| **Report Drafting** | Consumes **Case Summarization's output** (not raw tools directly) + `src/generation/` (`pdf_builder.py`/`xlsx_builder.py`/`docx_builder.py`) | A generated-file record (matches today's `_generate_file()` shape: file_id, file_name, storage_path) | If Case Summarization degraded (see above), draft from what it returned — never re-invoke tools directly to "fill gaps"; that would bypass the summarization boundary. If file building itself fails, abstain with an explicit file-generation error (matches current `_generate_file()` exception handling) |
| **Investigative Analysis** | RAG + GRAPH + SQL tools composed | One synthesized analytical answer with citations rolled up across all three sources — never three separate raw result sets | Each composed tool degrades independently per its own §2 fallback rule (GRAPH→RAG, SQL→RAG) *before* this sub-agent sees a result — the sub-agent should not need its own duplicate fallback logic; it always receives "final" tool output |
| **Timeline Building** | GRAPH tool, filtered to `OCCURRED_ON` edges + Phase 8 conflict detection (`src/graph/conflict_detection.py` output, e.g. `CONFLICTS_WITH` edges / `_fetch_case_conflicts()`) | An ordered event list with conflict flags attached per event — never raw edge rows | If no `OCCURRED_ON` edges exist for the case, return an explicit empty timeline (not an error) — this is a legitimate "nothing to show" outcome, distinct from a tool failure |
| **Cross-Case Linkage** | XGRAPH tool (named-entity recurrence/traversal) + XNETWORK tool (open-ended thematic/MO similarity) — see §3.1 for the confirmed split | A ranked list of cross-case connections/patterns with per-item confidence and hedge caveats (unconfirmed `SAME_AS` links surfaced as caveats, never fact) — never raw chunks | XGRAPH and XNETWORK each independently never-fallback (§2.3, §2.5); if one returns empty/abstains, present whichever of the two succeeded rather than failing the whole sub-agent. Role gate is enforced twice, independently, once per tool (see §4.3) — do not add a third gate at the sub-agent level, that would be redundant and risks drifting out of sync with the tools' own checks |
| **Large-Scale Aggregate** | XAGG tool only (area/station, time, crime-category grouping) | The computed aggregate (counts/listing) plus its natural-language summary — never the full case row set behind it | Verifier-rejection fallback to raw aggregate text is XAGG's own existing behavior (§2.4) — the sub-agent passes that through unchanged, does not add a second layer of fallback |

### 3.1 Cross-Case Linkage split — confirmed against current tool behavior

The proposed split ("XGRAPH = named-entity, e.g. same vehicle/phone" / "XNETWORK =
pattern/MO similarity, no named entity required") is **directionally correct but needs one
refinement**, confirmed by re-reading both tools:

- **XGRAPH is not purely "named-entity."** When no literal name/CNIC/phone/plate is present in
  the query, it falls back to `_find_recurring_entities_for_query()` — a keyword-hinted search
  for entities (still typed: `Person`/`Vehicle`/`PhoneNumber`/`Organization`) that recur across
  ≥2 cases, or an enumeration of every instance (`min_cases=1`) for "list all X" phrasing. So
  XGRAPH covers **both** "find connections for this specific named entity" **and** "has any
  entity of type X recurred across cases" — both are still structured, graph-typed, entity-based
  queries running through Cypher `BELONGS_TO_CASE`/`ASSOCIATED_WITH` traversal.
- **XNETWORK is categorically different, not just "the no-entity case of XGRAPH."** It doesn't
  touch the graph at all — it's embedding-similarity search over precomputed, free-text
  community summaries. It answers open-ended "what's the overall picture / what pattern
  emerges" questions that have no entity-typed answer shape at all (not "which vehicles
  recurred," but "what's going on across these cases thematically").

**Recommended framing for the sub-agent's internal dispatch logic:** route to XGRAPH when the
query names or implies a specific entity *or type of entity* (person/vehicle/phone/org) to
trace; route to XNETWORK when the query is open-ended thematic/MO synthesis with no entity
type in view. This matches `router.py`'s own existing deterministic pre-classification
(`_XGRAPH_OVERRIDE_PATTERNS` vs. `_XNETWORK_OVERRIDE_PATTERNS`, kept deliberately
non-overlapping — see router.py's own comments on why "network...across cases" phrasing was
excluded from the XNETWORK patterns specifically to avoid colliding with XGRAPH's "map ORG-002's
network across all cases" example). The Cross-Case Linkage sub-agent should reuse this same
distinction rather than inventing new dispatch logic — it is already tuned against live
misclassification failures.

---

## 4. Case/role enforcement — five independent points (no single chokepoint)

There is no unified access-control layer to hook into. Each of the following must be preserved
**independently** — none supersedes another, and a harness restructuring must not accidentally
collapse them into one (that would be a behavior change, not a refactor).

1. **API boundary hard 403** — `main.py`'s `chat_endpoint()` calls
   `gateway.check_case_access(case_id, user_id, current_user.role)` and raises `403` *before*
   `process_query()` (or, post-harness, the supervisor) is ever invoked. **If the harness adds
   any new entry point** (a direct supervisor API, a background/scheduled invocation, anything
   other than the existing chat endpoint), it needs this exact check re-applied at its own
   boundary — it is not inherited automatically.
2. **RLS context arming** — `set_case_scope(case_id)` (`src/auth/rls_context.py`), called once
   in `main.py` immediately after check #1 passes. `process_query()` does not self-arm this
   today and the harness supervisor must not either — it must continue to be the caller's
   (the API layer's) responsibility, per `process_query()`'s own documented contract.
3. **Per-tool cross-case role checks** — inside `graph_retriever.py::retrieve_graph()`,
   `xagg.py::run_aggregate()`, and `xnetwork.py::run_network_query()` independently (not a
   shared function today — each has its own copy of the same pattern). Each raises
   `PermissionError` and writes an `authorization_violation` audit log **before**
   `current_cross_case`/`current_rls_active` are armed. This ordering is load-bearing (§2.3) —
   preserve it verbatim in whatever tool-wrapper shape the harness introduces.
4. **`user_role` provenance** — must always come from `current_user.role` (the real RBAC role
   passed as `process_query()`'s dedicated `user_role` parameter), **never** from
   `user_profile` (which is `{context_text, preferred_language, llm_mode}` only — no role key
   at all). This is a **documented historical bug**: `user_profile.get("role",
   "investigator")` used to silently default every cross-case check to `investigator` for every
   user, regardless of their real role, denying real supervisors/admins their own access. The
   harness's supervisor-to-sub-agent-to-tool call chain must thread the real role through
   explicitly at every hop — do not let a convenience refactor collapse "user profile" and
   "user role" back into one object.
5. **`scoped_cypher()`'s structural guard** — `src/graph/case_scope.py`, the one true
   chokepoint, but **only for within-case Cypher templates**. It refuses (`ValueError`) to
   execute any template that doesn't literally reference `$case_id`. This applies to
   GRAPH/GRAPH_HYBRID's within-case seed lookup, case-wide enumeration, per-hop case filter,
   and conflict lookup — not to XGRAPH/XAGG/XNETWORK's cross-case templates (deliberately
   unscoped by design) or to templates that are correctly cross-case for other reasons (entity
   resolution's CNIC uniqueness check, etc.). If the harness introduces new within-case Cypher
   templates anywhere (e.g. inside Timeline Building), route them through `scoped_cypher()`,
   not raw `age_client.execute_cypher()`.
6. **Verifier's `_check_leakage()` — final independent backstop.** Re-derives each cited
   chunk's `case_id` from `[Document N]` citations in the *generated answer text* and checks
   against `active_case_id ∪ cross_case_ids`. This runs after everything above, catches a
   retrieval-time filtering bug even if #1–5 all worked correctly, and is unaffected by how the
   harness restructures control flow *as long as the Verifier interface decision in §5 preserves
   the flat, indexable chunk-list shape this check parses citations against*.

---

## 5. Verifier interface — decision

**Decision: (a) — every sub-agent flattens its own internal tool composition back into the
existing flat `list[{id, text, metadata}]` shape before handoff. `verify_grounding()`'s
signature is untouched.**

```python
async def verify_grounding(
    answer: str, cited_chunks: list[dict], case_id: Optional[str],
    cross_case_ids: Optional[list[str]] = None, target_date: Optional[int] = None,
) -> dict
```

### Why not (b) — extending the Verifier to a richer multi-source payload

- **The deterministic pre-checks are structurally built around one flat, indexable list.**
  `_check_leakage()` and `_check_hedging()` both parse `[Document N]` citations out of the
  answer text and index directly into `chunks[n-1]` — a positional contract the generation
  prompt (`_format_documents_for_prompt()`) and the Verifier both already depend on. Accepting
  a "richer" multi-source payload (e.g. grouped by originating tool) means either flattening it
  internally anyway (no real gain) or rewriting both deterministic checks and the citation
  scheme end to end — a change to the grounding contract itself, not just its input shape.
- **The Verifier already has no visibility into intermediate steps, by design, for every
  existing route** — including ones that already compose multiple retrieval passes internally
  (GRAPH_HYBRID merges graph + vector + BM25 before the Verifier ever sees it today). Composing
  multiple tools inside Investigative Analysis or Cross-Case Linkage is the same shape of
  problem GRAPH_HYBRID already solves today, just with different tools — there's no new
  category of composition here that justifies a new interface.
- **Fail-closed behavior depends on the input being "the thing the generator was actually
  shown."** `cited_chunks` must always be exactly what got formatted into the generation
  prompt (this invariant already holds for every current route — SQL/XAGG/XNETWORK all
  construct synthetic single/multi-chunk wrappers specifically to preserve it). A multi-source
  payload risks the generator and Verifier drifting out of sync about what evidence was
  actually shown, which is a correctness regression, not a feature.

### Tradeoff accepted

The Verifier loses structural knowledge of *which tool* a given chunk came from — it sees one
merged evidence list, not "this came from RAG, that came from GRAPH." This is not fully lost,
however: each flattened chunk's `metadata` dict is already free-form (it already carries
`case_id`, `graph_confidence`, `conflict_basis`, etc. per-source today). A sub-agent composing
multiple tools should tag each chunk with a `metadata["source_tool"]` field on the way in —
this preserves provenance for logging/debugging and for `_format_documents_for_prompt()`'s
citation display, without requiring `verify_grounding()`'s signature or deterministic checks to
change at all. This is the same pattern already used for `graph_confidence`
(GRAPH-sourced) and `conflict_basis` (conflict-detection-sourced) chunks today — extending it
by one more field, not inventing a new mechanism.

---

## 6. Logging

**Preserve both of the following exactly, at each meaningful state transition, regardless of
how control flow is restructured:**

1. **The `event()` SSE dict shape** — `{"step", "status", "detail", "ms"?, "sources"?,
   **kwargs}`, yielded directly to the client as `data: {json}\n\n`. This is what the chat
   UI's live trace panel renders in real time and never touches a database. Whatever emits
   pipeline progress in the harness (supervisor dispatch, sub-agent start/end, tool
   fallback-triggered) must continue producing this same shape at the same granularity — one
   event per meaningful transition (`active` → `done`/`error`/`skipped`), not collapsed into a
   single "sub-agent ran" event, or the live trace panel becomes less informative than it is
   today.
2. **The Postgres `pipeline_steps`/`log_step` call pattern** — `gateway.log_step(run_id,
   step_name, step_order, status, duration_ms, output_summary)`, fired alongside every `event()`
   call today via the same `_spawn()` fire-and-forget wrapper. This is what the admin
   dashboard's Run History page reads back (`GET /api/admin/runs/{run_id}/steps` →
   `gateway.get_run_steps()`) — it has no other source. Status values must stay within the
   existing four-value Postgres CHECK-constraint vocabulary (`success`/`skipped`/`retry`/
   `failed`) via the same `_STEP_STATUS_MAP` remapping from SSE's five-value vocabulary
   (`active`/`done`/`error`/`retry`/`skipped`).

**Dropped: `pipeline_logger.py`'s SQLite writes.** Confirmed no live reader — `get_session_stats()`
and `get_ingested_files_summary()`/`delete_ingested_file()` (the SQLite versions specifically;
same-named but unrelated Postgres-backed methods exist on `DataGateway` and *are* used by the
admin API) have zero callers anywhere in `src/`, `tests/`, or `scripts/`, and `PgPipelineLogger`
is exercised only by one isolated test. The code's own comment (`main.py:132–135`) confirms this
is a "write-only side-log... nothing reads case/user/RBAC data from it."

**Note for the doc record:** dropping this removes the only ad-hoc, no-Postgres-required way to
locally inspect per-step timings and LLM call previews (`data/pipeline_logs.db`, browsable with
any SQLite tool) during local development or offline debugging. Nothing in the live product
depends on it, but anyone who has been opening that file manually loses that specific,
low-friction inspection path — the Postgres `pipeline_steps` table via the admin dashboard (or
direct `psql`) is the replacement, and it requires a running Postgres instance where the SQLite
file did not.

---

## 7. Open items for the next draft (not blocking this review)

- Exact tool-wrapper interface (a common `Tool` protocol/base class under
  `src/pipeline/harness/tools/`) — deferred until the composition model above is confirmed.
- Supervisor routing logic (how a query gets classified to one of the seven sub-agents) — likely
  reuses `router.py`'s existing classification + `_deterministic_route_override` patterns
  rather than a new classifier, but not decided here.
- Whether `GRAPH_HYBRID`'s current inline duplication of RAG's retrieval steps (§2.2) gets
  cleaned up as part of this restructuring or left as a follow-up — flagged, not decided.
- **Per-chunk `confidence` conflates "checked, and it's low" with "never computed" — affects the
  Verifier's hedging check.** Surfaced while resolving the interface contracts in
  `docs/SUBAGENT_INTERFACES.md` (review of its RESOLVED-5). That doc settled the identical
  ambiguity one layer up: a timeline's conflict flag became three-state (`conflict`/`none`/
  `unknown`) because a bool couldn't distinguish "checked, no conflict" from "the check never
  ran," so a failed check silently rendered as an all-clear. The same shape exists here, on
  chunk metadata: a `confidence` of `None` currently means *both* "this tool computes no
  confidence" (flat retrieval — legitimately absent) *and* "confidence computation failed"
  (unknown). `_check_hedging()` reads this field to decide whether an answer must hedge, so the
  two cases are not interchangeable — a genuinely low-confidence graph chain that failed to
  score would read as "no confidence signal present" and could pass unhedged, which is the
  hedging check's exact failure mode. **Not resolved, and `ChunkMetadata` deliberately left
  unchanged** — unlike the timeline case this touches the Verifier's own input contract (§5) and
  every tool that emits chunks, so it needs its own pass rather than being folded into the
  interface extraction. Worth deciding before the Verifier's hedging behavior is relied on in
  the harness.
