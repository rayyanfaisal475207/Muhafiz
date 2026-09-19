# AI_RAG_ARCHITECTURE.md

> This file documents the AI/RAG/agent system, since it is present in this
> repository. Sources: [src/llm/](../src/llm/), [src/retrieval/](../src/retrieval/),
> [src/pipeline/](../src/pipeline/), [src/pipeline/harness/](../src/pipeline/harness/),
> [prompts/](../prompts/), [src/graph/](../src/graph/), [src/config.py](../src/config.py).

---

## 1. LLM

### 1.1 Providers

Configured in [src/config.py](../src/config.py) via `LLM_PROVIDER` ∈
`groq | gemini | openai | anthropic` (default `gemini`), plus two **local
OpenAI-compatible slots**:

| Slot | Env var | Used for |
|---|---|---|
| Reasoning | `LOCAL_LLM_URL` / `LOCAL_LLM_MODEL` (default `qwen3.5-2b`) | Router, rewriter, evaluator, verifier — everything except the final user-facing answer |
| Generation | `LOCAL_GEN_LLM_URL` / `LOCAL_GEN_LLM_MODEL` | Only the final answer text ("Qalb" per code comments) |

Cloud model defaults: `GROQ_MODEL=openai/gpt-oss-120b`, `GEMINI_MODEL=gemini-2.5-flash`,
`OPENAI_MODEL=gpt-4o`, `ANTHROPIC_MODEL=claude-3-5-sonnet-20241022`.
A code comment records that Groq's catalogue changed and the previous default
(`llama-3.3-70b-versatile`) was retired — model availability should be
re-verified, not assumed current.

### 1.2 Call resolution order — [src/llm/client.py](../src/llm/client.py)

`call_llm()` / `stream_llm()` are **local-first with cloud fallback**:

1. If `role == "generation"`, try `LOCAL_GEN_LLM_URL`; else try `LOCAL_LLM_URL`
   (unless `force_cloud=True`).
2. On any local failure, fall back to the configured cloud `provider`
   (`_call_groq`, `_call_gemini`, or an OpenAI/Anthropic branch).
3. **`AIR_GAP_MODE` disables the cloud fallback entirely** — with no local
   endpoint configured, every call fails outright rather than silently reaching
   the internet. This is described as a data-sovereignty boundary that a
   caller-side quality issue must never override.

### 1.3 API key rotation — [src/llm/key_manager.py](../src/llm/key_manager.py)

`KeyManager` loads every `GEMINI_API_KEY_*` / `GROQ_API_KEY_*` env var into a
rotation list (falling back to the single `GEMINI_API_KEY`/`GROQ_API_KEY` if no
numbered keys exist). `_is_rate_limit()` in `client.py` detects 429/rate-limit
errors by string match and rotates to the next key.

### 1.4 Reasoning-effort tuning

`route_query()` calls with `cloud_max_tokens=300, reasoning_effort="low"` — a
documented fix after a cloud-fallback router call blew past an 8000-token
request budget because a low `max_tokens` on a reasoning-capable model produced
a large hidden thinking trace with no room left for the actual JSON answer.

---

## 2. Embeddings

[src/retrieval/embedder.py](../src/retrieval/embedder.py) — `EMBEDDING_PROVIDER`:

| Provider | Model | Dim | Notes |
|---|---|---|---|
| `e5` (default) | multilingual-e5-large-instruct, served locally via `EMBEDDINGS_URL` | 1024 | Asymmetric `is_query` flag: `True` at query time, `False` at ingestion — **getting this backwards silently degrades retrieval quality, without an error.** |
| `gemini` | `gemini-embedding-001` | 3072 | Cloud fallback for environments without local serving |
| `openai` | `text-embedding-3-small` | 1536 | Cloud fallback |
| `local` | ChromaDB's `DefaultEmbeddingFunction` (all-MiniLM-L6-v2) | 384 | CPU-only last resort |

`config.EXPECTED_EMBEDDING_DIM` is derived from the active provider and used by
`ChromaVectorStore.upsert()` to reject a wrong-dimension write *before* it
reaches Chroma. **All chunks in one collection must share one embedding
model** — switching providers requires dropping the collection and re-ingesting
(`scripts/reingest_kb.py`).

`EMBEDDING_MAX_CONCURRENCY` (default 8) bounds concurrent requests to
`EMBEDDINGS_URL` — the endpoint accepts one text per request, so "batching"
here means bounded worker concurrency, not a larger single request.

---

## 3. Vector database

**ChromaDB**, local persistent collection ([src/retrieval/vector_store.py](../src/retrieval/vector_store.py)).
`ChromaVectorStore` is a singleton (`get_instance()`), collection name
`CHROMA_COLLECTION_NAME` (default `muhafiz_kb`), persisted at
`CHROMA_PERSIST_DIR` (default `data/chroma_db`).

### Cross-case diversity guard
For a query **not** scoped to a single case, plain nearest-neighbor search can
let one case's chunks dominate the top-k window purely by embedding-space
proximity. Two knobs mitigate this ([src/config.py](../src/config.py)):
- `CROSS_CASE_RETRIEVAL_MULTIPLIER` (default 3) — widens the Chroma fetch for
  unscoped queries only (e.g. fetch top-30 instead of top-10).
- `CROSS_CASE_PER_CASE_CAP` (default 5) — caps how many candidates any one case
  can contribute before trimming back to `TOP_K_RETRIEVAL`.

A case-scoped query uses **neither** knob — it fetches exactly
`TOP_K_RETRIEVAL` with no capping.

---

## 4. Chunking

[src/ingestion/chunker.py](../src/ingestion/chunker.py):

- `CHUNK_SIZE = 512`, `CHUNK_OVERLAP = 64` (both env-overridable;
  `validate_config()` errors if overlap ≥ size).
- Break points snap to the **last sentence boundary** that fits the window
  ([src/ingestion/sentence_splitter.py](../src/ingestion/sentence_splitter.py)); paragraph
  breaks are a subset of sentence boundaries. If no sentence boundary fits (a
  single very long sentence, or unpunctuated text), it falls back to the
  nearest whitespace and never cuts mid-word.
- Each new chunk starts `CHUNK_OVERLAP` characters before the previous chunk
  ended, so boundary sentences appear in full context in at least one chunk.

---

## 5. Retrieval

Two independent answering engines share the same retrieval primitives.

### 5.1 Hybrid retrieval (RAG route)

```
embed_text(query, task_type="RETRIEVAL_QUERY")
      │
      ├── ChromaDB vector search (semantic)
      └── BM25 over the chunk_fulltext candidate pool (keyword)
                     │
                     ▼
        RRF fusion — src/retrieval/reranker.py
```

**RRF (Reciprocal Rank Fusion):** `score(doc) = Σ 1/(rank + k)` across every
ranked list the document appears in, `RRF_K = 60`.

**Semantic confidence floor (`SEMANTIC_FLOOR_SCORE = 0.85`):** pure RRF looks
only at ordinal rank, so a chunk found *only* by semantic search at very high
cosine similarity can still lose to a chunk ranked merely moderately in both
lists. Documented root cause (2026-08-02): a genuinely relevant document never
appeared in the fused top-10 because BM25 never surfaced it at all — its
distinguishing terms were diluted across hundreds of unrelated documents
sharing one common word. The floor rescues any semantic-only hit scoring
≥ 0.85 that RRF fusion would otherwise drop.

**Cross-encoder reranking** ([src/retrieval/cross_reranker.py](../src/retrieval/cross_reranker.py)):
a second pass over RRF's fused candidate set, scoring the query jointly against
each candidate's full text via a locally served `bge-reranker-v2-m3`
(`RERANKER_URL`), cutting to `TOP_K_RERANK` (default 5). Documented limitation:
the reranker server's response carries only `{document, score}` — no
index/id — so results are matched back to candidates by
whitespace-normalized text; a candidate whose text is genuinely *modified*
(not just re-whitespaced) cannot be matched back.

### 5.2 Graph retrieval

[src/retrieval/graph_retriever.py](../src/retrieval/graph_retriever.py): `MAX_HOPS = 3`.
Within-case traversal is a single filtered hop via `BELONGS_TO_CASE` for most
questions; multi-hop traversal follows `ASSOCIATED_WITH` between different
entities (never hopping through a shared `Incident` node, which would falsely
suggest connection). Every within-case Cypher template is routed through
`scoped_cypher()` ([src/graph/case_scope.py](../src/graph/case_scope.py)), which refuses
(`ValueError`) any template not literally referencing `$case_id`.

**Cross-case gate** (`_enforce_cross_case_role_gate`): raises `PermissionError`
for any caller whose role is not in `CROSS_CASE_ROLES =
("supervisor", "station-admin", "platform-admin")`, and writes an
`authorization_violation` audit record before doing so; a successful traversal
writes a `graph_traversal_cross_case` audit record. The identical role list and
gate pattern is duplicated independently in `xagg.py::run_aggregate()` and
`xnetwork.py::run_network_query()` — three independent enforcement points,
none of which supersede one another (compliance test 3 asserts this).

### 5.3 Local Search (harness sub-agent / tool)

Fans out from semantically-matched "access point" entities
(`LOCAL_SEARCH_TOP_K_ENTITIES`, default 3) into per-entity graph traversal, run
concurrently via `asyncio.gather`. Kept small deliberately — the entities that
matter for a within-case question are almost always a handful, not dozens.

### 5.4 Global Search (harness sub-agent / tool)

Synthesizes over pre-computed **community reports**
(`community_reports.summary_text`) rather than raw chunks — a GraphRAG-style
"whole-dataset theme" answer shape, distinct from XNETWORK's narrower
network/cluster framing.

### 5.5 Cross-case routes summary

| Route | Sub-agent | Shape |
|---|---|---|
| `XGRAPH` | Cross-Case Linkage | Entity-anchored graph traversal across cases |
| `XAGG` | Large-Scale Aggregate | Deterministic counts/aggregates across cases |
| `XNETWORK` | Cross-Case Linkage | Synthesis over multiple community reports |

All three require the `CROSS_CASE_ROLES` gate; all three are structurally
separate from every case-scoped answer path (never silently blended).

---

## 6. Reranking

Two-stage: **RRF** (rank fusion of semantic + BM25, deterministic, cheap) then
**cross-encoder** (`bge-reranker-v2-m3`, one joint query/document pass, applied
only to the already-fused candidate set because it is too expensive to run over
the whole corpus). See §5.1.

---

## 7. Prompts

All prompts are plain text files loaded once at import time from
[prompts/](../prompts/):

| File | Used by |
|---|---|
| `router.txt` | `pipeline/router.py` — few-shot route classification |
| `query_rewriter.txt`, `query_expander.txt` | Query rewriting/expansion |
| `evaluator.txt` | Relevance evaluation |
| `verifier.txt` | Grounding/hallucination judge |
| `validation.txt` | Semantic entailment (FULL tier) |
| `final_response.txt` | GRAPH / GRAPH_HYBRID / RAG answer generation |
| `direct_response.txt` | DIRECT route |
| `web_response.txt` | WEB route |
| `cross_case_response.txt` | XGRAPH — carries the hedging-word rule (rule 5b) |
| `cross_case_aggregate.txt` | XAGG — a single synthetic summary/listing chunk, not per-row citations |
| `cross_case_network.txt` | XNETWORK — multiple already-summarized community reports |
| `sql_param_extractor.txt` | SQL route parameter extraction |
| `doc_classifier.txt`, `domain_entities.txt`, `ner_fallback.txt`, `relationship_extraction.txt` | Ingestion-time extraction |
| `resolution_adjudicator.txt` | Entity-resolution LLM tie-break for ambiguous matches |
| `community_summarizer.txt` | Community report generation |
| `meta_analysis_decomposer.txt` | Meta-Analysis sub-agent's query decomposition |
| `cross_script_query.txt` | Roman-Urdu / Urdu-script query handling |
| `file_structurer.txt` | Structuring an answer into a PDF/XLSX/DOCX payload |

**Dynamic prompt augmentation:** `orchestrator.py` appends
`_CROSS_CASE_HEDGING_RULE` to the system prompt **only** when a secondary
XGRAPH fetch actually contributed cross-case evidence to a GRAPH/GRAPH_HYBRID
answer — `final_response.txt` itself carries no hedging instruction, because it
was designed only for this case's own evidence.

---

## 8. Citations

- Answers cite evidence with a `[Document N]` marker convention, parsed
  identically by three independent modules that must never define the pattern
  differently: `verifier.py::_check_leakage()`/`_check_hedging()`,
  `citation_consistency.py`, and `validation.py`.
- **Citation consistency** ([src/pipeline/citation_consistency.py](../src/pipeline/citation_consistency.py))
  is deterministic index arithmetic, not an LLM check — it verifies that
  Report Drafting's re-composed prose still points `[Document N]` at the same
  evidence Case Summarization was actually given, catching renumbered/invented/
  dropped markers even when the underlying meaning is preserved.
- **Confidence hedging.** Any cited chunk with `confidence < 0.85` must be
  accompanied by one of a fixed set of hedge phrases in the answer language —
  English (*unconfirmed, possible, pending, not yet verified, under review,
  flagged, uncertain, may be*) **and** the equivalent Urdu-script phrases. The
  Urdu list is not optional: the corresponding response prompt forces the model
  to answer entirely in the user's preferred language, so an English-only hedge
  list would fail every correctly-hedged Urdu answer.
- **Cross-case leakage check** (`_check_leakage`): a within-case answer must
  never cite a chunk from a different `case_id` unless the route is one of the
  three structurally-separate cross-case routes.
- Generated documents (Report Drafting, file exports) carry citations through
  to the output file via `file_structurer.py` and the `generation/*` builders.

---

## 9. Agents (harness)

Source: [src/pipeline/harness/](../src/pipeline/harness/). This is **one of two
answering engines** — see [ARCHITECTURE.md](ARCHITECTURE.md) §3.4 for when it serves
traffic (only when `HARNESS_CUTOVER_ROUTES` names a route; empty by default).

### 9.1 Supervisor

`src/pipeline/harness/supervisor.py`. Per its own docstring, it does exactly
three things: (a) classify the question to one of the sub-agent names, (b)
thread `ExecutionContext` unchanged through to that sub-agent, (c) return
exactly the `SubAgentResult` it gets back — never reformatted or unwrapped. It
never talks to more than one sub-agent per question and never touches a tool
directly.

**Classification** reuses `router.py::route_query()` verbatim (not
reimplemented), then maps its 9 tool-level routes to one of the sub-agent names
via `_ROUTE_TO_SUBAGENT`:

| Router route | Sub-agent |
|---|---|
| `RAG` | Semantic Search |
| `GRAPH`, `GRAPH_HYBRID` | Case Summarization |
| `SQL` | Investigative Analysis |
| `XGRAPH`, `XNETWORK` | Cross-Case Linkage |
| `XAGG` | Large-Scale Aggregate |
| `WEB` | Semantic Search (closest available fallback) |
| `DIRECT` | `NO_SUB_AGENT` (`"__direct__"`) — deliberately **not** Semantic Search; the Verifier never gates DIRECT answers, so forcing it through the retrieve-generate-verify pipeline would be a regression |
| any route with `output_format` in `{file_pdf, file_xlsx, file_docx}` | Report Drafting (overrides the table above) |

Additional **provisional, regex-triggered** overrides layered before the table
lookup (each documented as narrow and evidence-anchored, not a general
classifier): Local Search (officer-role phrasing, e.g. "investigating officer"),
Global Search ("top N themes/patterns" phrasing), Meta-Analysis (compound
"summarize...across all...and flag" phrasing — checked **first**, before every
other override). A Meta-Analysis or cross-case-shaped match on a query whose own
`case_scope` is not `"cross_case"` demotes to Semantic Search rather than
attempting cross-case dispatch.

**Timeline Building and Data-Quality are not reachable via classification** —
documented as a known gap: no route/regex was ever built to trigger them
(Timeline Building needs new keyword triggers; Data-Quality is a wholly new
capability with no predecessor in the legacy orchestrator).

### 9.2 Sub-agents (11)

`src/pipeline/harness/agents/`: `semantic_search`, `local_search`,
`global_search`, `case_summarization`, `timeline_building`,
`cross_case_linkage`, `investigative_analysis`, `large_scale_aggregate`,
`report_drafting`, `data_quality`, `meta_analysis`.

Status vocabulary (`SubAgentStatus`): `OK`, `PARTIAL` (degraded but useful),
`EMPTY` (legitimately nothing found — not an error), `ABSTAINED` (could not
answer safely, no answer text served), `DENIED` (a role gate refused the
request). **`DENIED` must never collapse into `ABSTAINED`/`EMPTY`** — a spike in
denials is a security-relevant signal, a spike in empties is a coverage
problem, and flattening them destroys that distinction.

**Meta-Analysis** decomposes a compound question into sub-queries, each of
which **re-enters the Supervisor** and is gated on its own merits by whichever
tool it resolves to — it adds no role check of its own.

**Report Drafting** consumes Case Summarization's already-Verifier-passed
answer and generates a *new* draft from it — the reason Citation Consistency
exists as a separate check specifically for this sub-agent.

### 9.3 Cutover mechanism

[src/pipeline/harness/cutover.py](../src/pipeline/harness/cutover.py) — `run_cutover_query()`
is invoked by `chat_endpoint` only when `route_query()`'s classified route is in
`HARNESS_CUTOVER_ROUTES` **and** `output_format == "chat"` (a file-output
request always stays on the legacy path this session, since Report Drafting is
not part of the cutover slice). A `delegate_to_legacy` event mid-stream falls
back to `process_query()` for the remainder of that turn.

---

## 10. Tools (harness)

`src/pipeline/harness/tools/`: `rag`, `graph`, `local_search`, `global_search`,
`sql`, `web`, `xagg`, `xgraph`, `xnetwork`. Each wraps an existing retrieval
function (`retrieve_graph`, `run_aggregate`, `run_network_query`, etc.) rather
than reimplementing access control — per compliance test 3's own framing, a
tool wrapper's entire job on the cross-case axis is to translate the wrapped
function's `PermissionError` into `status=DENIED`, nothing more. Tool wrappers
are asserted (compliance tests 2, 3, 5) to never arm RLS themselves, never
duplicate a role check, and never call `age_client.execute_cypher()` directly.

`ToolStatus`: `OK`, `EMPTY` (a successful call that legitimately found
nothing — distinct from an error for several tools), `FAILED`, `DENIED`.

---

## 11. Memory

- **Conversation memory** ([src/memory/conversation.py](../src/memory/conversation.py)):
  history is kept up to `MAX_HISTORY_TOKENS` (default 2000), dropping from the
  **oldest** end first so recent context is always retained.
- **Project memory** ([src/pipeline/memory_updater.py](../src/pipeline/memory_updater.py)):
  a rolling summary stored in `project_memory.summary_text`, updated after each turn.
- **User context profile** (`user_context_profiles`): `context_text` (free text,
  ≤1000 chars), `preferred_language`, `llm_mode` — injected into generation
  prompts but not part of the retrieval pipeline itself.
- No long-term semantic memory / vector-store-backed personal memory exists
  beyond `project_memory` and the conversation history — **UNKNOWN** whether a
  future module adds one; not found in this codebase.

---

## 12. Guardrails

| Guardrail | Mechanism | Fail posture |
|---|---|---|
| Grounding / hallucination | `verifier.py::verify_grounding()` — 3 deterministic pre-checks (temporal validity, cross-case leakage, confidence hedging) + an LLM claim-by-claim judge, 2-attempt retry on JSON parse failure | **Fails closed** — default is "not grounded" |
| Second-opinion entailment | `validation.py` — FULL (LLM, three-way SUPPORTED/PARTIALLY_SUPPORTED/NOT_SUPPORTED) for Cross-Case Linkage / Investigative Analysis / Report Drafting; STRUCTURAL-ONLY (deterministic numeric/date/name check, SUPPORTED/NOT_SUPPORTED only) for Semantic Search / Large-Scale Aggregate / Case Summarization | **Fails open** — a flagged claim adds a caveat, never blocks the already-verified answer |
| Citation index integrity | `citation_consistency.py` | Deterministic; used before the Verifier in Report Drafting |
| Cross-case access | Role gate duplicated independently in `graph_retriever.py`, `xagg.py`, `xnetwork.py` | Raises `PermissionError` + writes an `authorization_violation` audit record |
| Web search domain allow-list | `WEB_ALLOWED_DOMAINS` (gov.pk / islamabadpolice.gov.pk / nadra.gov.pk / major PK news outlets by default) | Results outside the list are never returned |
| Air-gap mode | `AIR_GAP_MODE=true` disables web search and the cloud LLM fallback outright | Web search returns `[]`; LLM calls fail rather than reach the internet |
| Explicit web-search opt-in | `enable_web_search` is a per-request flag, never inferred, never a fallback from an exhausted RAG retry | An exhausted retry **abstains** instead of silently searching the web |
| Retry bound | `MAX_RETRIES` (default 1) on the evaluator retry loop | Bounded, not unbounded |
| Cypher template safety | `scoped_cypher()` refuses a within-case template with no `$case_id` reference | `ValueError` |
| SQL parameterization | `police_reference_data` queries built with SQLAlchemy's Core query builder + literal-bind compilation, not string concatenation | N/A |
| MCP least privilege | `muhafiz_mcp_readonly` role — `SELECT` on `police_reference_data` only; no superuser fallback | `RuntimeError` on missing `MCP_DATABASE_URL` |

---

## 13. Enforcement / compliance test suite

`src/pipeline/harness/compliance/` (run separately in CI, always blocking) —
five independent enforcement points, per
[AGENT_HARNESS_DESIGN.md](../AGENT_HARNESS_DESIGN.md) §4, "none of which supersedes
another":

1. **API boundary** — `check_case_access()` runs, and 403s, before
   `process_query()`/the Supervisor is ever invoked.
2. **RLS arming** — `set_case_scope()` is called once, at the API boundary; no
   harness tool wrapper may reference the RLS `ContextVar`s directly.
3. **Cross-case role gate** — independently inside `graph_retriever.py`,
   `xagg.py`, `xnetwork.py`; static (no wrapper duplicates the check) and
   behavioral (a `PermissionError` really surfaces as `DENIED`) checks, per tool.
4. **Role provenance** — `user_role` always derives from
   `current_user.role`/`CallerContext.role`, never from `user_profile`.
5. **Scoped Cypher** — no harness tool calls `age_client.execute_cypher()`
   directly; `scoped_cypher()` still refuses an unscoped template.
