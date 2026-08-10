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
- **DEVIATION FROM LEGACY, DELIBERATE: the harness RAG tool no longer crashes the turn when the
  relevance evaluator errors.** Recorded here because it is a **behaviour change**, not a
  like-for-like port, and it should be an explicit decision rather than something discovered later
  by reading the adapter.

  *What legacy does today, and it is inconsistent between routes:* `orchestrator.py`'s **RAG**
  route calls `evaluate_relevance()` with **no error handling at all** (≈ lines 1069 and 1813 in
  the retry loop) — an evaluator exception propagates and takes down the entire pipeline turn.
  The **GRAPH** route (≈ line 898) wraps the identical call in `try/except` and fails open with
  `{"relevant": True, "reason": "Evaluator failed, proceeding"}`. Same call, two different
  behaviours, in one file.

  *What the harness tool does:* follows GRAPH's fail-open shape rather than RAG's crash, so a
  flaky local evaluator endpoint degrades the answer instead of destroying the request — **but
  makes the degradation visible**, which neither legacy route does. `evaluator_verdict` becomes
  `'unavailable'` (distinct from both real verdicts and from `None`), a user-facing caveat is
  attached, and the composing sub-agent reports `PARTIAL`. See
  `docs/SUBAGENT_INTERFACES.md` RESOLVED-7 for the contract.

  *Why visibility is load-bearing here:* the Verifier does **not** backstop relevance. It checks
  grounding — whether claims trace to their cited chunks — so a well-grounded answer built
  entirely from off-topic evidence passes it cleanly. Relevance screening has no second line of
  defence, so "the screen did not run" must reach the user rather than being absorbed.

  *Also fixed in passing (a defect, not a design choice):* the adapter's malformed-verdict default
  now **fails closed** (`.get("relevant", False)`), matching legacy GRAPH. An earlier draft
  defaulted to `True`, silently admitting evidence the gate never actually judged.

  **Open question for a later pass:** whether legacy RAG's crash-on-evaluator-error should itself
  be brought in line with GRAPH's fail-open. Not changed here — `orchestrator.py` is deliberately
  untouched while the harness runs alongside it — but the two routes disagreeing about the same
  call is worth resolving when the legacy path is retired or revisited.

- **`caveats`/`disclosure_rendered` are user-facing by design but persist only in an
  admin-scoped table.** [RESOLVED-2a] exists precisely because metadata does not travel with the
  text a reader sees — yet with the per-query trace work scoped as it is, these fields land in
  `pipeline_steps.output_summary`, readable through the admin Run History page
  (`/api/admin/runs/*`, platform-admin only) and nowhere else. An investigator cannot review why
  their *own* past answer was flagged partial.

  **Scoped deliberately, per direct product guidance:** the requirement is a reviewable
  per-query trace for operators, not an investigator-facing "why was my answer partial" surface
  in their own chat history. `pipeline_steps` + Run History is the correct and complete
  destination for that requirement.

  **What a future requirement would need:** a second write path, most likely on the `messages`
  table, so the caveat is durably attached to the answer the investigator actually received
  rather than to the operational record of how it was produced. Not built. Flagged because the
  gap is a deliberate scope boundary rather than an oversight, and because the shape of the
  fix differs from the trace work — the trace records *what the system did*, a message-level
  caveat records *what the user was told*, and those are not the same fact.

- **The contract/doc parity guard compares structure, not string values — a demonstrated gap,
  not a hypothetical one.** `tests/harness/test_contract_doc_parity.py` diffs
  `docs/SUBAGENT_INTERFACES.md` against `contracts.py` at name level (a declared type or
  constant that was never transcribed) and field level (a class missing a field the doc
  declares). It does **not** compare the literal *values* of shared constants.

  **How this actually bit:** 7bda725 finalized both disclosure strings in `contracts.py` and
  left the doc showing the old placeholder text. The two disagreed about a string delivered
  verbatim to investigators, and the guard passed the whole time — the constants existed under
  the right names, so structurally nothing was wrong. Caught by hand and fixed in 1ac6b30, not
  by the mechanism built to catch exactly this class of drift.

  **What extending it would take:** parse the doc's Python blocks (the guard already does this)
  and, for module-level constants that exist in both, compare `ast.literal_eval`'d values. Scope
  it to constants specifically — comparing whole class bodies would fail constantly on
  docstrings and field descriptions that are legitimately prose-edited in one place first.

  **Not urgent, and no current drift** — the two disclosure strings are verified byte-identical
  as of 1ac6b30. Worth doing before the next constant is added that both files carry, since the
  failure mode is silent by construction: the guard reports green while the spec and the shipped
  string say different things.

- **SQL- and WEB-routed queries have no sub-agent of their own — a documented coverage gap, not
  a clean fit.** The classifier maps both to Semantic Search, which composes only the RAG tool.
  So a pure penal-code lookup or a guarded web search degrades to document search rather than
  reaching its intended tool.

  **Why it is mapped that way anyway:** no sub-agent composes SQL or WEB alone. Investigative
  Analysis uses SQL only alongside RAG and GRAPH, and nothing composes WEB at all. Semantic
  Search is the closest available home because RAG is already the DECLARED fallback target for
  both (§2.6, §2.7) — so a SQL- or WEB-routed query degrades here exactly as it would on the
  legacy path, which reassigns `route_str = "RAG"` on an empty SQL result or a failed web
  search. The behaviour matches legacy; the coverage does not.

  **What it costs today:** a query the router confidently classified as SQL never reaches
  `sql_tool` through the harness. Its answer comes from document retrieval instead, which for
  penal-code questions often works — the ingested FIRs restate section numbers — but works by
  accident of corpus overlap rather than by design, and would simply fail on a corpus without it.

  **Options if this needs closing:** either a thin sub-agent per tool (Reference Lookup, Web
  Lookup), or extend Semantic Search to compose SQL/WEB alongside RAG with its own fallback
  rules. The first is more honest to the contract's one-sub-agent-per-use-case shape; the second
  avoids two near-trivial sub-agents. Not decided. **Tracked so it is not mistaken for solved** —
  every route maps to something, which makes the gap invisible from the mapping table alone.

- **Watch item: "report on the case" may be misclassified as a document request.** The router
  prompt maps *"a report"* to `file_pdf` (prompts/router.txt:45), and the classifier routes any
  file `output_format` to Report Drafting. In police usage, "report on the case" often means
  prose — *tell me about it* — not *generate a PDF*. An investigator asking that would receive a
  document instead of an answer.

  **Deliberately NOT pre-empted with a deterministic pattern.** The router's few-shots do not
  cover the ambiguous phrasing either way, and adding a `\breport on\b` exclusion without
  evidence risks the opposite error — suppressing genuine document requests. Every other
  deterministic pattern in `router.py` was added in response to a confirmed live misroute, and
  that discipline is what keeps the pattern sets narrow enough to stay correct.

  **Add a tier-1 pattern only if real misclassification shows up in testing.** The fix would be
  small: a pattern that forces `output_format` back to `chat` for report-shaped prose requests,
  ahead of the classifier's format check.

- **Supervisor gateway dispatch uses runtime introspection, not a declared contract.**
  `supervisor._call_node()` calls `inspect.signature(node)` and forwards `gateway` only to nodes
  that declare the parameter — five of the seven sub-agents take one, Semantic Search and Case
  Summarization do not, and passing it unconditionally would raise `TypeError`.

  **Why it is done this way:** the alternative is forcing all seven to accept a parameter two of
  them would ignore, purely to satisfy a uniform signature. That trades a real per-call
  introspection for a fake uniformity, and `SubAgentCallable` is deliberately a loose protocol
  (`Callable[..., Awaitable[SubAgentResult]]`) rather than a rigid one.

  **The tradeoff worth remembering:** dispatch behaviour now depends on a signature rather than
  on a declaration. A future rename of the `gateway` parameter, or wrapping a node in a
  `*args`-only closure, would SILENTLY change what gets forwarded — no error, just a sub-agent
  quietly falling back to `get_gateway()`. That fallback is correct in production, which is
  exactly what would make the change hard to notice.
  `test_gateway_is_forwarded_to_nodes_that_declare_it` and
  `test_nodes_without_a_gateway_parameter_still_work` pin both directions, but they test the
  mechanism, not every node's conformance to it.

  **If this becomes load-bearing** — e.g. a sub-agent that MUST have a specific gateway rather
  than the singleton — replace the introspection with an explicit declaration (a class attribute
  or a registration-time flag) so a mismatch fails loudly at registration instead of silently at
  dispatch. Not needed today: every consumer falls back correctly.

- **Consolidation candidate (not urgent): two sub-agents implement "the grounding gate is
  prose-only — do not route a non-prose finding through it."** Cross-Case Linkage's
  unconfirmed-links handling (789867b) and Large-Scale Aggregate's Verifier-rejection fallback
  both exist because a correct finding was nearly discarded by a check that only makes sense for
  generated prose. In both cases the gate behaved correctly; the mistake was what was routed
  through it.

  **Examined for a shared helper, and deliberately left duplicated.** They differ on every axis
  that would make one useful:

  | | Cross-Case Linkage | Large-Scale Aggregate |
  |---|---|---|
  | Trigger | `not chunks` — *before* generating | Verifier rejection — *after* the gate |
  | Verifier | never called | called, and rejected |
  | Substitute text | a written constant | `raw_summary_text` from the tool |
  | Status | `EMPTY` (nothing contributed) | `PARTIAL` (the tool did contribute) |
  | Payload field | `cross_case_links` | `answer_text` |
  | `tools_used` | `[]` | `["XAGG"]` |

  What overlaps is 3–4 lines of `SubAgentResult(...)` construction with different values in every
  field. A helper would need parameters for status, text, payload field, tools and caveats — a
  constructor wrapper that obscures more than it saves, and one that would couple two paths that
  happen to rhyme rather than share a mechanism.

  **What IS shared is a principle, not code**, and it is documented as such in both module
  docstrings, with Large-Scale Aggregate's naming the Cross-Case Linkage precedent explicitly.
  Prose is the right carrier for a design lesson that two implementations apply differently.

  **Revisit if a third occurrence appears** — at that point the common shape is likely real
  rather than coincidental, and there would be enough evidence to see which parameters actually
  vary.

- **XAGG has no time-wise grouping, and the original design assumed it did.** `xagg.py` groups
  only by `police_station` (area/station-wise) or `crime_category` — `_station_or_category_counts`
  picks between exactly those two and nothing else. There is no date, month, or year grouping
  anywhere in the tool. The three grouping shapes the design brief described (area/station, time,
  crime-category) therefore only ever had two implementations behind them.

  **What this meant in practice, before it was noticed:** a "how many cases per month" query
  matched none of the station keywords, fell through to the `crime_category` default, and
  returned CATEGORY counts — an answer to a question nobody asked, presented as though it were
  the requested one.

  **How Large-Scale Aggregate handles it:** the sub-agent detects a time-series request and
  reports it as explicitly unsupported, while still serving the real station/category figures
  under a caveat. Those figures are correct; they are simply not the grouping asked for, and
  saying so is better than either silently misinterpreting the request or discarding usable data.

  **Deliberately NOT fixed here.** Building time-wise grouping would mean adding a third
  aggregate family to `xagg.py`, and design §2.4 is explicit that the two canned families are a
  bounded surface on purpose — "do not let a harness 'make it smarter' pass turn this into
  free-form query generation." A sub-agent is the wrong place to grow a tool's capability.

  **This is a tool gap, not a harness-contract issue.** Adding real time-wise grouping is a
  future `xagg.py` enhancement: a third dispatch family plus its own keyword set, with the same
  bounded-by-design discipline as the existing two. The harness contract needs no change to
  accommodate it — `aggregate_kind` already discriminates families, so a new one slots in without
  touching `SubAgentResult`.

- **~~Cross-Case Linkage's per-source trace mechanism is an open call~~ — RESOLVED (789867b),
  prediction confirmed.** The question was whether tool-emitted events suffice for XGRAPH +
  XNETWORK, or whether sub-agent-interpreted events are needed as they are for Investigative
  Analysis. Confirmed empirically against the built tools rather than inherited, and the
  prediction held for FOUR independent reasons rather than the one predicted:

  1. Neither tool can declare a fallback — `Literal[False]`, type-enforced.
  2. Neither names a degradation TARGET at all. GRAPH and SQL collapse specifically because both
     name RAG; these name nothing.
  3. No shared backing store — XGRAPH traverses the AGE graph, XNETWORK searches a precomputed
     community-summary Chroma collection.
  4. Disjoint result shapes (`unconfirmed_links`/`hop_count` vs
     `community_ids`/`raw_summary_text`), so neither can stand in for the other even in principle.

  Reasons 1 and 4 are pinned by regression test, so a future change introducing a shared fallback
  target fails loudly rather than silently making the trace ambiguous. Kept here rather than
  deleted because the *method* generalises: the §2.1.4.1 decision test is cheap to run against a
  built sub-agent, and the Case Summarization surprise earlier in this work is why it gets run
  rather than assumed.

- **`SOURCE_TOOL_DISPLAY_LABELS` is duplicated in the admin frontend — a confirmed drift risk,
  not a hypothetical one.** The canonical map lives in `contracts.py` and is what the disclosure
  templates substitute through. But `admin-frontend`'s `StepTrace` component hardcodes its own
  copy of the same labels in TSX, so the investigator-facing vocabulary now exists in two places
  that nothing keeps in sync.

  This is the same class of problem the contract/doc parity guard exists to catch, one layer
  over: a rename in `contracts.py` silently leaves the admin chips showing the old wording, and
  no test fails. It is exactly the risk that made "do not add a third copy" the rule for the
  investigator-facing chat surface, which takes pre-labelled strings from the backend instead.

  **Fix:** migrate `StepTrace` to consume backend-provided labels the same way, leaving one
  source of truth. The mechanism already exists and needs no new work —
  `build_degradation_trace()` emits a `labels` block of pre-rendered strings, which the
  investigator-facing `QueryChecks` component renders verbatim. `StepTrace` reads the same
  `output_summary.trace` payload, so this is a component change only: read `trace.labels.*`
  instead of mapping `trace.contributed_only` through its local map, then delete the map.

  Deliberately NOT done as part of the chat-surface work — that task added no duplication, and
  fixing this one is a separate change to a separate app with its own test suite. No current
  drift: the two copies agree today.

- **`admin-frontend`'s test suite does not run out of the box — pre-existing, unrelated to the
  harness.** Surfaced while verifying the Run History trace rendering. Two independent
  breakages, neither caused by harness work and neither fixed here:

  1. **Missing native binding.** `@rolldown/binding-win32-x64-msvc` is not installed by
     `npm install` (it is an optional platform dependency of `rolldown`, which Vite 8 uses).
     Without it, `npm run build`, `npm run dev`, and `vitest` all fail at startup with
     `MODULE_NOT_FOUND`. Worked around locally with
     `npm install --no-save @rolldown/binding-win32-x64-msvc@<rolldown version>` — deliberately
     unsaved, so it is NOT a durable fix and vanishes on a clean reinstall.
  2. **jsdom / ESM conflict.** `jsdom@28`'s `html-encoding-sniffer` CommonJS-`require`s
     `@exodus/bytes`, which now ships as pure ESM → `ERR_REQUIRE_ESM`. This breaks the
     PRE-EXISTING suite too, confirmed by running `ReviewQueuePage.test.tsx` on an unmodified
     checkout. Worked around with `--environment happy-dom` (a CLI flag; `vitest.config.ts` was
     not modified). Note the other page tests were written against jsdom and some fail under
     happy-dom, so this substitution is a local verification aid, not a suite-wide fix.

  Same class of problem as the Python baseline addressed in 62a23e1/75cedd2: a test suite that
  cannot run is a suite nobody trusts. Worth a dedicated pass — pinning the platform binding as
  a real dependency and resolving the jsdom version conflict — rather than each contributor
  rediscovering both workarounds.

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
  hedging check's exact failure mode.

  **PART A — DONE.** Scoping this uncovered a worse, unrelated defect underneath it: the hedging
  check was not merely ambiguous for harness chunks, it was **silently disabled** for all of
  them. `_check_hedging()` reads `chunk["graph_confidence"]` off the chunk dict's TOP LEVEL,
  while the harness had normalized that value into `metadata.confidence`. The verifier's lookup
  returned `None`, hit its own `if gc is None: continue` guard, and every low-confidence graph
  chunk passed unhedged. Confirmed by running the identical chunk through both shapes: flagged
  in the legacy shape, zero issues in the harness shape. It had not bitten only because nothing
  routes to the harness yet. Fixed with an explicitly commented compatibility shim
  (`EvidenceChunk.graph_confidence`, duplicating `metadata.confidence`), plus
  `tests/harness/test_hedging_shim.py`, which fails in four places if the shim is removed.

  **PART B — STILL DEFERRED, and deliberately not a ride-along.** The original ambiguity remains:
  a `confidence_state` sentinel (`computed` / `not_applicable` / `unknown`) distinguishing "this
  tool computes no confidence" from "computation failed", with `_check_hedging()` skipping the
  former and REQUIRING a hedge on the latter — the RESOLVED-5 treatment, one layer down.

  It is deferred for a specific reason, not just size: **it changes `verifier.py`'s failure
  semantics, and `verifier.py` is shared with the legacy orchestrator path that is still serving
  every real query.** Making the hedging check fail closed on `unknown` could start rejecting
  legacy answers that pass today. That blast radius has not been measured, and measuring it is
  the actual work — the harness-side change (one field, seven adapters declaring which state
  they emit) is the easy half.

  **Needs its own scoped change:** measure how many legacy-path chunks would newly require
  hedging, decide whether the legacy path adopts the stricter semantics or is explicitly
  grandfathered, then change both together. Do not fold it into harness work. Part A's shim is
  removable as part of it.

- **RESOLVED — Timeline Building retrieved from GRAPH only, and the graph is thin on timeline
  evidence.** Surfaced by the first real-data run after generation was wired (CASE-009, an
  8-document case): the GRAPH tool returned exactly **one** chunk at `max_hops` 1, 2 and 3 alike,
  a Recovery Memo table header with no extractable date — so the timeline rendered as a single
  `undated` "event", while the same case's RAG leg returned 9 chunks, 3 of them dated.

  **Fixed** by adding a RAG leg as a SUPPLEMENT, not a replacement. GRAPH stays primary and is
  merged first, so its structurally-derived events keep the lower `[Document N]` indices and its
  conflict-detection metadata continues to drive `ConflictState`; RAG can only contribute chunk
  ids GRAPH did not already supply. `_contributing_tools()` reports both legs per [RESOLVED-4],
  and a GRAPH failure now abstains only when RAG did not cover for it. Measured after the change
  on the same case: **1 event -> 9, 0 dated -> 3, 1 citation -> 9**, `tools_used=['GRAPH','RAG']`.

  Still open, deliberately: the graph carries 24 `Date` vertices and 56 `OCCURRED_ON` edges that
  no traversal surfaces. A date-aware Cypher traversal would make GRAPH's own contribution far
  richer and is worth doing, but it is graph-query work, not sub-agent work.

- **RESOLVED (and my original diagnosis was wrong) — Cross-Case Linkage reported "no connections
  found".** Recorded here because the correction matters more than the fix: this was logged as a
  **false negative**, on the reasoning that raw Cypher showed 8+ entities spanning multiple cases
  while the sub-agent reported none. That reasoning was wrong.

  What the data actually shows: **168 Person vertices, 168 distinct `entity_id`s, and zero
  spanning more than one case.** The same real person mentioned in two cases becomes two separate
  vertices joined by a `SAME_AS` edge. Of 763 such edges, 113 are `status='confirmed'` and **none
  of those cross a case boundary**; the 523 pending ones do, but they are low-confidence
  name-similarity guesses (0.37-0.61) between Urdu and Latin renderings of a name. `EMPTY` plus
  populated `unconfirmed_links` was therefore the CORRECT answer, and `_expand_confirmed_identity`
  reading `r.status = 'confirmed'` is correct too — my "confirmed = true" check was simply the
  wrong query against a status-string property.

  Two REAL defects surfaced underneath the bad diagnosis, both fixed:

  1. **Every unconfirmed link rendered as "Possible identity link between an entity and another
     entity."** `_links_from_xgraph` read `raw['from']`/`raw['to']`, but
     `graph_retriever._unconfirmed_same_as_links()` emits `entity`/`candidate`, so both always hit
     the placeholder fallback. In an investigative context that is worse than useless: it tells a
     reviewer a possible identity match exists while withholding the two names needed to confirm
     or dismiss it. Now renders real names plus the resolver's `basis` (newly propagated) and
     `tier`.
  2. **`router.py`'s deterministic XGRAPH override hardcodes `target_entity: None`**, and one of
     its patterns is `(other|another)\s+cases?` — which matches the archetypal cross-case
     question, "What other cases is X involved in?". So routing short-circuits before the LLM
     extraction step on exactly the queries where an entity matters most. `target_entity` is now
     threaded on `SubAgentInput` (populated by the supervisor from `route_result`, explicit values
     winning), with `_recover_target_entity()` falling back to `ner.extract_statistical` —
     regex/gazetteer only, no model call — when routing supplies none. Verified: "What other cases
     is Hina Malik involved in?" went from 0 links to the same 3 the explicitly-anchored query
     returns. `router.py` itself is untouched, since it serves every live legacy query.

  XNETWORK still cannot be exercised at all: `community_reports` is empty (`community_runs` has
  one row that produced nothing) and no community-summary Chroma collection exists, so that leg
  needs the offline community pipeline run first. Also worth knowing: graph `Person` vertices key
  on `canonical_name`, **not** `name`, and many are Urdu-script.

- **RESOLVED — `report_draft` file generation failed on every invocation.** `_generate_file()`
  passed `{"session_id": None, ...}` to `gateway.log_generated_file()`, whose backend called
  `uuid.UUID()` on it unguarded. The whole pipeline (RAG + GRAPH, generation, verification, PDF
  build) ran for ~45s and then discarded the report at the logging step.

  Not a missing None-guard, as first assumed: `generated_files.session_id` is **NOT NULL** in the
  schema, matching what legacy passes at orchestrator.py:2099. The harness simply had no session
  to record, so `SubAgentInput.session_id` was added and Report Drafting now validates it UPFRONT,
  beside the existing `output_format` check — turning a 45-second walk into a failure into an
  immediate, explainable refusal. `direct_backend` additionally raises a named error instead of
  bare "one of the hex, bytes, bytes_le..." when it is missing. Verified end-to-end: `status=OK`,
  6 citations, a real 4,818-byte PDF recorded against CASE-009.
