# ============================================================
# RAG System — All Settings, Loaded from .env
#
# Provider strategy used in this project:
#   LLM_PROVIDER   = groq    → fast LLM calls (rewriter, router, evaluator, response)
#   EMBEDDING_PROVIDER = gemini → Gemini text-embedding-004 (Groq has no embedding API)
#   Vision          = gemini → Gemini 2.0 Flash for image/scanned-PDF understanding
#
# Why this split?
#   Groq runs open-source models (LLaMA 3.3) on custom inference chips at
#   extremely low latency (~200ms per call). Perfect for 3–4 calls per query.
#   Gemini provides best-in-class embeddings and multimodal vision — both
#   features Groq doesn't currently offer.
# ============================================================

import os
from pathlib import Path
from dotenv import load_dotenv

# ── Load .env file ────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


# ── LLM Provider (text generation) ───────────────────────────────────────────
# Controls which backend handles: query rewriter, router, evaluator, response
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "gemini")  # "groq" | "gemini" | "openai" | "anthropic"

# Groq — fast open-source inference
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
# "llama-3.3-70b-versatile" (the previous default) was retired from
# Groq's catalog -- confirmed live via GET /v1/models with this
# project's own key: every llama-3.x chat model is gone, the lineup
# moved to openai/gpt-oss-*, qwen/qwen3.6-*, groq/compound, allam-2-7b.
# Every call_llm()/stream_llm() site using LLM_PROVIDER=groq was
# 404ing outright on this model whenever the local model server didn't
# serve the request first (found via a real error_logs row, not a
# hypothetical). Groq model names WILL change again -- check
# https://console.groq.com/docs/models before assuming this default is
# still current.
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Google Gemini — also used as LLM fallback and for vision
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# OpenAI (optional, kept for compatibility)
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")

# Anthropic (optional, kept for compatibility)
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")

# Optional local OpenAI-compatible endpoint (LM Studio / vLLM / ngrok tunnel).
# Leave LOCAL_LLM_URL empty to disable. Only used when a user's llm_mode
# setting is "local".
# Reasoning slot — Qwen3-14B, used for routing/rewriting/evaluation/etc.
LOCAL_LLM_URL: str = os.getenv("LOCAL_LLM_URL", "")
LOCAL_LLM_MODEL: str = os.getenv("LOCAL_LLM_MODEL", "qwen3.5-2b")
LOCAL_LLM_API_KEY: str = os.getenv("LOCAL_LLM_API_KEY", "")
LOCAL_LLM_TIMEOUT: float = float(os.getenv("LOCAL_LLM_TIMEOUT", "20"))

# Generation slot — Qalb, used only for the final user-facing answer.
LOCAL_GEN_LLM_URL: str = os.getenv("LOCAL_GEN_LLM_URL", "")
LOCAL_GEN_LLM_MODEL: str = os.getenv("LOCAL_GEN_LLM_MODEL", "")
LOCAL_GEN_LLM_API_KEY: str = os.getenv("LOCAL_GEN_LLM_API_KEY", "")
LOCAL_GEN_LLM_TIMEOUT: float = float(os.getenv("LOCAL_GEN_LLM_TIMEOUT", "20"))

# The base URL behind LOCAL_LLM_URL/LOCAL_GEN_LLM_URL/EMBEDDINGS_URL/
# RERANKER_URL (a single rotating free-tier ngrok tunnel in dev — see
# .env's ${MODEL_SERVER_BASE_URL} expansion). Phase 4's LLM-dependent
# extraction modules (doc_classifier, ner, domain_entities,
# entity_resolution's adjudicator) hit `{MODEL_SERVER_BASE_URL}/health`
# before a batch run rather than firing hundreds of calls at a possibly-
# dead tunnel — see src/extraction/llm_health.py.
MODEL_SERVER_BASE_URL: str = os.getenv("MODEL_SERVER_BASE_URL", "") or LOCAL_LLM_URL


# ── Embedding Provider ────────────────────────────────────────────────────────
# Controls which backend converts text → vectors for ChromaDB storage/search.
# Default is the locally-served multilingual-e5-large-instruct model.
EMBEDDING_PROVIDER: str = os.getenv("EMBEDDING_PROVIDER", "e5")  # "e5" | "gemini" | "openai" | "local"
GEMINI_EMBEDDING_MODEL: str = os.getenv("GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-001")
OPENAI_EMBEDDING_MODEL: str = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

# Expected vector dimension for whichever EMBEDDING_PROVIDER is active — see
# src/retrieval/embedder.py's per-provider docstrings for where these numbers
# come from (e5: multilingual-e5-large-instruct; gemini: gemini-embedding-001;
# openai: text-embedding-3-small; local: chromadb's DefaultEmbeddingFunction,
# all-MiniLM-L6-v2). Used by ChromaVectorStore.upsert() to reject a
# wrong-dimension write before it reaches Chroma — Chroma itself only raises a
# dimension-mismatch error once a collection is non-empty, so a freshly-created
# or manually-cleared collection would otherwise silently adopt whatever
# dimension the first vector happens to have. Override via env if a
# non-default model changes a provider's output size.
_EMBEDDING_DIMS: dict[str, int] = {"e5": 1024, "gemini": 3072, "openai": 1536, "local": 384}
EXPECTED_EMBEDDING_DIM: int = int(
    os.getenv("EXPECTED_EMBEDDING_DIM", "") or _EMBEDDING_DIMS.get(EMBEDDING_PROVIDER, 1024)
)

# Local e5 embeddings + reranker (served via the same ngrok/FastAPI model server
# as LOCAL_LLM_URL / LOCAL_GEN_LLM_URL).
EMBEDDINGS_URL: str = os.getenv("EMBEDDINGS_URL", "")
RERANKER_URL: str = os.getenv("RERANKER_URL", "")

# [Gold-QA fix — Module 145] Semantic dispatch — the cross-encoder fallback
# under XAGG's phrase lists (src/pipeline/semantic_dispatch.py). On by
# default; tests/conftest.py turns it off so the unit suite stays offline,
# and an operator can turn it off to prove a live route was phrase-driven.
SEMANTIC_DISPATCH_ENABLED: bool = os.getenv("SEMANTIC_DISPATCH_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")

# [Gold-QA fix — Module 178] LLM-generated graph queries as the LAST
# fallback for a cross-case question that no aggregate answers and no plan
# composes (src/pipeline/llm_query_fallback.py). OFF by default: the module's
# own measurement (docs/gold-qa-wave2-results/MODULE178_RESULT.md §6) is what
# decides whether a deployment turns it on — a generated query can run clean
# and return a plausible wrong number that nobody can audit afterwards, and
# this system's figures go into case files. The mechanism ships; the default
# is the finding. When on, it still fires only after keyword dispatch,
# semantic dispatch, sub-agent selection and plan matching have ALL missed.
LLM_QUERY_FALLBACK_ENABLED: bool = os.getenv("LLM_QUERY_FALLBACK_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
# Bounds (Module 178, bound 4): the most rows a generated query may return
# and the longest it may run. Both are ceilings the module clamps to, not
# defaults the model is asked to respect.
LLM_QUERY_FALLBACK_ROW_CAP: int = int(os.getenv("LLM_QUERY_FALLBACK_ROW_CAP", "200"))
LLM_QUERY_FALLBACK_TIMEOUT_S: float = float(os.getenv("LLM_QUERY_FALLBACK_TIMEOUT_S", "15"))

# Graph Scale & Schema Expansion, Milestone A3: the model server's /embed
# route takes one text per request (no batch parameter — a {"texts": [...]}
# payload 422s, per src/retrieval/embedder.py's own comment), so "batched"
# here means bounded WORKER CONCURRENCY rather than one larger request —
# this many requests may be in flight to EMBEDDINGS_URL at once, replacing
# the old sequential-with-0.3s-pacing loop. Conservative default: still
# polite to a free-tier ngrok tunnel, but throughput now scales with this
# number instead of staying wall-clock-linear in corpus size.
EMBEDDING_MAX_CONCURRENCY: int = int(os.getenv("EMBEDDING_MAX_CONCURRENCY", "8"))


# ── ChromaDB ─────────────────────────────────────────────────────────────────
CHROMA_PERSIST_DIR: Path = Path(
    os.getenv("CHROMA_PERSIST_DIR", str(_PROJECT_ROOT / "data" / "chroma_db"))
)
CHROMA_COLLECTION_NAME: str = os.getenv("CHROMA_COLLECTION_NAME", "muhafiz_kb")


# ── Muhafiz Data API (see docs/decisions/0001-muhafiz-api-migration.md) ──────
# A live REST API fronting real-schema PSRMS/CMS/PKM/criminal-record data
# (API_CONSUMER_GUIDE.md), replacing the locally generated synthetic corpus as
# the source of evidence for ingestion. As of this migration, confirmed a
# same-schema STAND-IN, not the real police system — the actual integration
# arrives post-MVP as a separate text-format handoff against this same
# schema, at larger volume. Nothing here is air-gap-aware by design: this
# endpoint is a plain internet REST call, gated entirely by
# MUHAFIZ_API_BASE_URL being set — leave it empty to disable the source.
MUHAFIZ_API_BASE_URL: str = os.getenv("MUHAFIZ_API_BASE_URL", "")
MUHAFIZ_API_KEY: str = os.getenv("MUHAFIZ_API_KEY", "")
# Seconds per HTTP call; the live API is occasionally slow on cold start
# (Render free-tier), so this is generous relative to the model-server calls
# elsewhere in this file.
MUHAFIZ_API_TIMEOUT: float = float(os.getenv("MUHAFIZ_API_TIMEOUT", "60"))
# Max page_size the API accepts (API_CONSUMER_GUIDE.md: "1 to 100").
MUHAFIZ_API_PAGE_SIZE: int = int(os.getenv("MUHAFIZ_API_PAGE_SIZE", "100"))

# [findings.md Module 10 — Meta-Analysis] Per-sub-query timeout for the
# Meta-Analysis sub-agent's dispatch step (src/pipeline/harness/agents/
# meta_analysis.py) — each sub-query re-enters the full Supervisor.handle()
# pipeline (retrieval + generation + verification), a heavier multi-step
# operation than a single LLM call, so this mirrors MUHAFIZ_API_TIMEOUT's
# generous style rather than LOCAL_LLM_TIMEOUT's single-call budget. A
# sub-query that exceeds this is treated as a non-contributing failure
# (disclosed as a caveat) — it never blocks the other sub-queries or the
# synthesis step.
#
# [Gold-QA fix — Module 53] 60 -> 150. This is NOT a per-sub-query budget,
# which is how the original 60 was chosen. `_dispatch_one()` applies it
# inside an `asyncio.gather()` over all N sub-queries at once, so it is one
# WALL-CLOCK deadline shared by the whole fan-out, starting at fan-out —
# and the shared model server does not run the sub-queries in parallel.
# Module 50 measured it serialising them into a ~10 s-per-sub-query
# staircase: G1's five sub-answers at +25.0, +33.4, +44.3, +50.9 and
# +56.7 s. At the old 60 s that leaves the FIFTH slot 3.3 s of headroom on
# an otherwise-quiet machine, and Module 50 measured it failing outright
# under contention from other backends — the sub-query was killed for
# being served last, not for being slow.
#
# 150 s is derived, not guessed: `_MAX_PLAN_SUB_QUERIES` (5) x the measured
# ~12 s worst-case slot cost = 60 s of real work, x2.5 for the contention
# Module 50 measured on this machine. It is an upper bound on a pathology,
# not a latency target — a healthy fan-out still finishes in ~55 s and
# never touches it. Module 53's salvage path (see
# `agents/_salvage.py`) is the real fix; this number only stops a
# deterministic aggregate being thrown away in the first place.
META_ANALYSIS_SUBQUERY_TIMEOUT: float = float(os.getenv("META_ANALYSIS_SUBQUERY_TIMEOUT", "150"))

# [Gold-QA fix — Module 150] How many of a Meta-Analysis fan-out's sub-queries
# may be IN FLIGHT at once. 0 disables the gate (every sub-query dispatches
# immediately — the pre-Module-150 behaviour).
#
# This is about the PER-CALL timeout, not the deadline above. The local
# model server runs requests strictly one at a time (Module 150 measured
# N=8 concurrent paraphrase calls completing at +18, +26, +33, +39, +45,
# +51, +57 and +63 s — a serial staircase, exactly as Module 50 saw), while
# `LOCAL_LLM_TIMEOUT` is an httpx read timeout that starts the moment each
# request is SENT. Fan out eight sub-queries at once and the sixth-to-eighth
# LLM calls spend their whole budget queued behind the first five on the
# server, hit the 60 s timeout with an empty `httpx.ReadTimeout` message,
# and fall back to Groq ("Local LLM failed: . Falling back to groq...").
# Nothing was slow; they were served last. Client-side concurrency buys
# no throughput against a serial server, so gating the fan-out costs no
# wall time — it moves the queue from the server (where the wait counts
# against the per-call timeout) to the client (where it does not).
#
# 2, not 1: keeping one request queued at the server while another is
# being served hides the ngrok round trip and each sub-query's own
# deterministic-aggregate compute (~1 s), and the second-in-line call waits
# at most ONE service time (~10-15 s measured, ~30 s under the heaviest
# shared load seen) before its own — inside a 60 s per-call timeout with
# 2x headroom. 3 would put the third-in-line at 2 x 30 s under load, on
# the timeout. Reasoned and measured in MODULE150_RESULT.md §1-2, pinned
# by `tests/test_harness_agent_meta_analysis.py::test_module150_*`.
META_ANALYSIS_MAX_CONCURRENT_SUBQUERIES: int = int(os.getenv("META_ANALYSIS_MAX_CONCURRENT_SUBQUERIES", "2"))


# ── Pipeline Settings ─────────────────────────────────────────────────────────
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "1"))
# [Gold-QA fix — Module 143] Whether the RAG retry loop consults
# `src/pipeline/retry_gate.py` after a rejected FIRST pass on the plain
# (non-legal-KB) path, and abstains at once when the evaluator's own reason
# says the missing thing is a cross-case aggregate no document holds.
# MAX_RETRIES itself is untouched: a near-miss still gets every retry.
# Off switch only — for bisecting a regression back to this gate.
RETRY_GATE_ENABLED: bool = os.getenv("RETRY_GATE_ENABLED", "true").lower() in ("1", "true", "yes")
# [Gold-QA fix — Module 151] Off switch for the verifier's schema-absence
# grounding (verifier.py's "GROUNDING AN ABSENCE AGAINST THE RECORD
# INVENTORY" block). Off = the pre-Module-151 verifier, byte for byte: a
# correct "our records have no field for X" is rejected as unsupported.
SCHEMA_ABSENCE_GROUNDING_ENABLED: bool = (
    os.getenv("SCHEMA_ABSENCE_GROUNDING_ENABLED", "true").lower() in ("1", "true", "yes")
)
TOP_K_RETRIEVAL: int = int(os.getenv("TOP_K_RETRIEVAL", "10"))
TOP_K_RERANK: int = int(os.getenv("TOP_K_RERANK", "5"))

# findings.md Module 8 (Local Search) — how many semantically-matched
# "access point" entities to fan out from per query. Small on purpose: each
# match costs its own retrieve_graph() traversal call (asyncio.gather'd, not
# free), and the entities that matter for a within-case question are almost
# always a handful, not dozens.
LOCAL_SEARCH_TOP_K_ENTITIES: int = int(os.getenv("LOCAL_SEARCH_TOP_K_ENTITIES", "3"))

# RETRIEVAL_DIVERSITY_FIX_PROMPT.md, Fix 2: when a query is NOT scoped to a
# single case (no case_id in the where_clause — more than one case could
# legitimately match), pure nearest-neighbor vector search lets whichever
# single case's chunks happen to sit closest in embedding space for that
# exact phrasing dominate the entire TOP_K_RETRIEVAL window. Two knobs
# control the mitigation (see orchestrator.py's RAG route and
# vector_store.cap_case_diversity):
#   - CROSS_CASE_RETRIEVAL_MULTIPLIER widens the per-query Chroma fetch for
#     unscoped queries only (e.g. top-30 instead of top-10), so chunks from
#     a second/third relevant case actually make it into the candidate pool
#     in the first place — capping alone can't rescue a case whose chunks
#     were never fetched.
#   - CROSS_CASE_PER_CASE_CAP then limits how many of those candidates any
#     single case can contribute before the pool is trimmed back down to
#     TOP_K_RETRIEVAL and handed to RRF fusion, unchanged from before.
# A case-scoped query (case_id present) uses neither knob — it fetches
# exactly TOP_K_RETRIEVAL as before, with no capping, so that path's
# behavior is byte-for-byte unchanged.
CROSS_CASE_RETRIEVAL_MULTIPLIER: int = int(os.getenv("CROSS_CASE_RETRIEVAL_MULTIPLIER", "3"))
CROSS_CASE_PER_CASE_CAP: int = int(os.getenv("CROSS_CASE_PER_CASE_CAP", "5"))


# ── Entity resolution — structured-record corroboration gate (M6a of the
# Muhafiz Data API migration, docs/decisions/0001-muhafiz-api-migration.md)
# ────────────────────────────────────────────────────────────────────────
# Governs src/graph/structured_projection.py's gate on name-fallback for
# structured-record person mentions with no CNIC (measured: 4/37 witnesses
# on the live dataset). True (default) = today's behavior, gated by a
# corroboration check (shared case / shared structured id / matching
# address) before a no-CNIC mention is even allowed to reach
# entity_resolution.py's FLAGGED/REVIEW tiers. False = skip name-fallback
# entirely for these mentions — mint a new node, never risk a SAME_AS
# candidate. Does NOT affect CNIC-present mentions (the hard-block/
# auto-merge path in entity_resolution.py already fully protects those,
# regardless of this flag) or narrative-text NER mentions (unrelated code
# path, untouched by this flag).
ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED: bool = (
    os.getenv("ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED", "true").strip().lower() == "true"
)

# ── Confidence-hedged retrieval rollout flag (Milestone D2,
# GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md) ───────────────────────────────────
# Default OFF: opens graph_retriever.py's pending-SAME_AS traversal
# exclusion (see that module's docstring) for the cross-case retrieval
# path only — XGRAPH is live production traffic today, so this ships
# behind its own flag rather than changing default behavior on merge.
# When on, cross-case graph traversal follows pending (not just
# confirmed) SAME_AS identity links, always at a confidence capped below
# the verifier's 0.85 hedge threshold, so verifier.py's existing
# _check_hedging() always requires (and the generation prompts already
# instruct — cross_case_response.txt rules 4/5b) a disclosed hedge on
# anything reached this way. Recall goes up (real leads that used to be
# silently excluded now surface); nothing here confirms/rejects a match.
FEATURE_HEDGED_PENDING_TRAVERSAL: bool = (
    os.getenv("FEATURE_HEDGED_PENDING_TRAVERSAL", "false").strip().lower() == "true"
)

# ── Guarded web search (Phase 5.7) ────────────────────────────────────────────
# Air-gap deployments disable ALL outbound web access — this is the one route
# in the whole architecture that needs it (architecture doc, "Guarded web
# search"). No prior AIR_GAP flag exists anywhere in the codebase (confirmed
# by repo-wide search before adding this) — greenfield naming.
AIR_GAP_MODE: bool = os.getenv("AIR_GAP_MODE", "false").strip().lower() == "true"

# ── Agent harness live-traffic cutover (AGENT_HARNESS_IMPLEMENTATION_PLAN.md
# §6, "Rollout strategy") ─────────────────────────────────────────────────
# Empty by default: orchestrator.py serves every route unless its
# router.py::route_query() classification (upper-cased "route" value, e.g.
# "RAG") appears in this set, in which case main.py's chat_endpoint sends
# that request through src.pipeline.harness.supervisor.Supervisor instead.
# Per-route, not per-sub-agent, deliberately: it's checked against the same
# route string classify_to_subagent() itself branches on, so the set here
# reads the same way supervisor.py's own _ROUTE_TO_SUBAGENT table does.
# Cutting a route in here is a live-traffic decision, not a code-deploy
# decision — flip it by restarting with a different env var, never by
# editing this file. Session-scoped first slice: HARNESS_CUTOVER_ROUTES=RAG
# routes plain semantic-search chat queries (not file-output requests, which
# main.py additionally excludes regardless of this set) through Semantic
# Search; every other route stays on orchestrator.py until deliberately
# added here.
HARNESS_CUTOVER_ROUTES: frozenset[str] = frozenset(
    r.strip().upper() for r in os.getenv("HARNESS_CUTOVER_ROUTES", "").split(",") if r.strip()
)

# Which engine answers a cross-case aggregate question.
#
#   "legacy"       xagg.run_aggregate() — the keyword-dispatched engine that
#                  has always served this route. DEFAULT, so an unset
#                  environment behaves exactly as before this flag existed.
#   "aggregate_v1" src.pipeline.aggregate.orchestrator.answer_question() —
#                  NL -> AggregateSpec -> validation -> verification policy ->
#                  structured/AGE/semantic -> reconciliation.
#
# A LIVE-TRAFFIC DECISION, NOT A CODE-DEPLOY ONE, for the same reason
# HARNESS_CUTOVER_ROUTES above is: flip it by restarting with a different
# env var, never by editing this file.
#
# WHY IT IS NOT DEFAULTED ON. The two engines disagree BY DESIGN. The new
# one refuses questions the legacy one answers — compound questions, filters
# it cannot express, traversals past its hop limit — because answering them
# meant silently answering a different question. That is the correct
# behaviour and it is also a visible product change, so it is enabled
# deliberately rather than inherited.
AGGREGATE_ENGINE_LEGACY = "legacy"
AGGREGATE_ENGINE_V1 = "aggregate_v1"
#: "aggregate_v2" — EXPERIMENT, not a product mode. Same pipeline as v1 with
#: the deterministic schema checks in `validator.py` REPLACED by an LLM judge
#: that is shown the schema card and asked, strictly, whether the spec names
#: anything the card does not contain.
#:
#: It exists to answer one question with a number instead of an argument:
#: can a model police its own schema adherence as reliably as a registry
#: lookup? Run the blind corpus in all three modes and compare the count of
#: SILENTLY WRONG ANSWERS — legacy measured 5, v1 measured 0.
#:
#: Note before interpreting a result: v1's prompt ALREADY says "Do not invent
#: a property because the question implies one should exist", and the model
#: invented `criminal_risk_score` in 2 of 3 runs regardless, once answering
#: 208 (every person) for a field that does not exist. The judge is a second
#: model call over the same evidence, so the open question is whether a
#: second look catches what the first one missed, or repeats it.
AGGREGATE_ENGINE_V2 = "aggregate_v2"
AGGREGATE_ENGINE_MODE: str = (
    os.getenv("AGGREGATE_ENGINE_MODE", AGGREGATE_ENGINE_LEGACY).strip().lower()
    or AGGREGATE_ENGINE_LEGACY
)

# Relevance/reliability control, not just safety (architecture doc) — WEB
# results are restricted to government/legal/established-news domains, never
# the open web. Comma-separated env override; sensible starting default.
WEB_ALLOWED_DOMAINS: list[str] = [
    d.strip() for d in os.getenv(
        "WEB_ALLOWED_DOMAINS",
        "gov.pk,islamabadpolice.gov.pk,nadra.gov.pk,pakistantoday.com.pk,"
        "dawn.com,tribune.com.pk,thenews.com.pk,geo.tv,app.com.pk"
    ).split(",") if d.strip()
]


# ── Text Chunking ─────────────────────────────────────────────────────────────
CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "512"))
CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "64"))


# ── Conversation Memory ────────────────────────────────────────────────────────
MAX_HISTORY_TOKENS: int = int(os.getenv("MAX_HISTORY_TOKENS", "2000"))


# ── FastAPI Server ────────────────────────────────────────────────────────────
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))
RELOAD: bool = os.getenv("RELOAD", "true").lower() == "true"
ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
VALID_ENVIRONMENTS: set[str] = {"development", "staging", "production"}


# ── Document Ingestion ────────────────────────────────────────────────────────
DOCUMENTS_DIR: Path = Path(
    os.getenv("DOCUMENTS_DIR", str(_PROJECT_ROOT / "data" / "documents"))
)

# The project's data/ root — src/api/admin.py's entity-resolution eval-metrics
# endpoint reads data/eval/resolution_metrics.json (written by
# scripts/eval_entity_resolution.py, itself using this same
# <project_root>/data path, not an env-configurable location) off this.
# Was referenced as config.DATA_DIR before this constant actually existed —
# confirmed live: every call to GET /api/admin/eval/entity-resolution 500'd
# with AttributeError regardless of caller role, found during a full E2E
# pass while fixing an unrelated RBAC mismatch on the same endpoint.
DATA_DIR: Path = Path(os.getenv("DATA_DIR", str(_PROJECT_ROOT / "data")))
MAX_UPLOAD_SIZE_MB: int = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))


# ── Relational Database (Pipeline Logging) ────────────────────────────────────
# SQLite file that stores all pipeline events in a normalized schema.
# Lives in data/ alongside ChromaDB so everything is in one place.
DB_PATH: Path = Path(
    os.getenv("DB_PATH", str(_PROJECT_ROOT / "data" / "pipeline_logs.db"))
)

# The legacy SQLite schema (src/database/db.py) predates the entire
# case/auth/RBAC/RLS model — it has no users, cases, case_assignments, or
# audit_logs tables. Silently falling back to it when DATABASE_URL isn't
# configured means the app "starts successfully" and then fails per-request
# the moment anyone registers, logs in, or touches a case. Defaulting this
# to true makes that fallback something an operator must consciously opt
# into (e.g. for narrow local/legacy debugging), not something that happens
# by accident.
REQUIRE_POSTGRES: bool = os.getenv("REQUIRE_POSTGRES", "true").strip().lower() == "true"


# ── Directory Setup ───────────────────────────────────────────────────────────
def ensure_directories() -> None:
    """Create all required data directories if they don't already exist."""
    for d in [CHROMA_PERSIST_DIR, DOCUMENTS_DIR, DB_PATH.parent]:
        d.mkdir(parents=True, exist_ok=True)



# ── Validation ────────────────────────────────────────────────────────────────
def validate_config() -> tuple[list[str], list[str]]:
    """
    Check required API keys and settings.

    Returns (warnings, critical_errors):
      * warnings — non-fatal; the server can still start (individual calls
        that depend on the missing setting will fail at request time).
      * critical_errors — a real security or availability defect (a public
        JWT secret, an unrecognized ENVIRONMENT value) that should stop a
        production deployment from serving traffic. The caller decides how
        to enforce that (see src/main.py's lifespan handler); this function
        only classifies, it never raises or exits itself.
    """
    errors: list[str] = []
    critical: list[str] = []

    # LLM provider key check
    if LLM_PROVIDER == "groq" and not GROQ_API_KEY:
        errors.append("GROQ_API_KEY is not set. LLM calls will fail.")
    elif LLM_PROVIDER == "gemini" and not GEMINI_API_KEY:
        errors.append("GEMINI_API_KEY is not set. LLM calls will fail.")
    elif LLM_PROVIDER == "openai" and not OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY is not set. LLM calls will fail.")
    elif LLM_PROVIDER == "anthropic" and not ANTHROPIC_API_KEY:
        errors.append("ANTHROPIC_API_KEY is not set. LLM calls will fail.")
    elif LLM_PROVIDER not in ("groq", "gemini", "openai", "anthropic"):
        errors.append(
            f"Unknown LLM_PROVIDER '{LLM_PROVIDER}'. "
            "Valid values: 'groq', 'gemini', 'openai', 'anthropic'."
        )

    # Embedding provider key check
    if EMBEDDING_PROVIDER == "gemini" and not GEMINI_API_KEY:
        errors.append("GEMINI_API_KEY is not set. Embedding calls will fail.")
    elif EMBEDDING_PROVIDER == "openai" and not OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY is not set. Embedding calls will fail.")

    # Chunking sanity check
    if CHUNK_OVERLAP >= CHUNK_SIZE:
        errors.append(
            f"CHUNK_OVERLAP ({CHUNK_OVERLAP}) must be less than CHUNK_SIZE ({CHUNK_SIZE})."
        )

    # DATABASE_URL presence. src/main.py's lifespan is the actual enforcement
    # point (it can refuse to start); this is a defense-in-depth warning for
    # any other caller of validate_config() that doesn't go through main.py.
    if not DATABASE_URL:
        errors.append(
            "DATABASE_URL is not set. Muhafiz's case/auth/RBAC model requires "
            "PostgreSQL — see REQUIRE_POSTGRES."
        )

    # MCP_DATABASE_URL unset means src/mcp/client.py's execute_query() will
    # raise RuntimeError the moment the MCP SQL route is actually called —
    # there is no superuser fallback (removed once migrations/009's
    # muhafiz_mcp_readonly role was verified end-to-end). This warning exists
    # so an operator finds out at startup, not at first request.
    if DATABASE_URL and not MCP_DATABASE_URL:
        errors.append(
            "MCP_DATABASE_URL is not set — the MCP Postgres route "
            "(src/mcp/client.py) will raise RuntimeError on first use. "
            "Provision the least-privilege muhafiz_mcp_readonly role and set "
            "MCP_DATABASE_URL. See migrations/009_mcp_readonly_role.sql."
        )

    # The evaluator database is TEST-ONLY after Phase 5D — no runtime path
    # needs it, so an unset URL is not worth a warning. A MISDIRECTED one
    # still is: the adversarial tests deliberately execute mutating Cypher
    # against whatever it names, so pointing it at production would aim
    # those tests at real data.
    if AGE_EVAL_DATABASE_URL:
        _eval_db = (
            AGE_EVAL_DATABASE_URL.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        )
        if _eval_db == "muhafiz":
            critical.append(
                "AGE_EVAL_DATABASE_URL points at the production database "
                "('muhafiz'). That database is the target of destructive "
                "adversarial tests — point it at 'muhafiz_age_eval'."
            )
        elif _eval_db != "muhafiz_age_eval":
            errors.append(
                f"AGE_EVAL_DATABASE_URL names database '{_eval_db}', but the "
                "evaluator target is fixed to 'muhafiz_age_eval'."
            )

    # AIR_GAP_MODE consistency: with no local LLM endpoint configured, every
    # LLM call refuses cloud fallback and fails outright (src/llm/client.py).
    if AIR_GAP_MODE and not LOCAL_LLM_URL:
        errors.append(
            "AIR_GAP_MODE is enabled but LOCAL_LLM_URL is not set — every LLM "
            "call will refuse the cloud fallback and fail."
        )

    # MUHAFIZ_API_BASE_URL and MUHAFIZ_API_KEY are meant to be set together —
    # a base URL with no key will 401 on every call except /health; a key
    # with no base URL is simply unused. Neither half is required (the
    # source is opt-in), but a half-configured pair is always a mistake.
    if bool(MUHAFIZ_API_BASE_URL) != bool(MUHAFIZ_API_KEY):
        errors.append(
            "Only one of MUHAFIZ_API_BASE_URL / MUHAFIZ_API_KEY is set — the "
            "Muhafiz data API client needs both, or neither."
        )

    # ENVIRONMENT must be a real, recognized value. The cookie Secure flag
    # (src/auth/routes.py) and this function's own JWT-secret check below
    # both key off it — an unrecognized value (unset, typo) should never be
    # silently treated as equivalent to "development".
    if ENVIRONMENT not in VALID_ENVIRONMENTS:
        critical.append(
            f"ENVIRONMENT='{ENVIRONMENT}' is not one of {sorted(VALID_ENVIRONMENTS)}. "
            "Cookie security and other environment-gated behavior depend on "
            "this being set explicitly and exactly."
        )

    # The single most security-critical secret in the app must not still be
    # the public, hardcoded default outside of local development.
    if JWT_SECRET_KEY == "your-secret-key-for-dev" and ENVIRONMENT != "development":
        critical.append(
            "JWT_SECRET_KEY is still the public default value "
            "('your-secret-key-for-dev'). Anyone can forge a valid JWT for "
            "any user, including platform-admin. Set a real secret."
        )

    return errors, critical


import os
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-for-dev")
JWT_ALGORITHM = "HS256"
CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS", "http://localhost:5173,http://localhost:3000,http://localhost:5174"
).split(",")

# Added for MCP
# The MCP Postgres server used to connect with DATABASE_URL directly — the
# same superuser role as the rest of the app, full read/write to every
# table. MCP_DATABASE_URL should point at a least-privilege role (see
# migrations/009_mcp_readonly_role.sql) that can only SELECT from
# police_reference_data. Falls back to DATABASE_URL (with a startup
# warning — see validate_config()) so an environment that hasn't
# provisioned the role yet doesn't hard-break.
MCP_DATABASE_URL: str = os.getenv("MCP_DATABASE_URL", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── Aggregate AGE query limits (Phase 5D safe gateway) ───────────────────────
# The independent AGE route no longer executes model-written Cypher. A model
# emits a typed `AgeQueryPlan`; trusted code validates it against the live
# registry and compiles it into read-only Cypher (see
# src/pipeline/aggregate/age_plan.py, age_plan_validator.py, age_compiler.py).
# Mutation is not filtered out — it is unrepresentable, because the plan
# schema has no field that can carry a clause and the compiler has no
# mutation emitter.
#
# These limits therefore protect AVAILABILITY, not integrity: a read-only
# aggregate over 13,467 edges can still pin a connection. The pool is
# separate from age_client's so a slow aggregate cannot exhaust the slots
# the rest of the application needs.
AGE_QUERY_STATEMENT_TIMEOUT_MS: int = int(
    os.getenv("AGE_QUERY_STATEMENT_TIMEOUT_MS", "15000")
)
AGE_QUERY_MAX_ROWS: int = int(os.getenv("AGE_QUERY_MAX_ROWS", "1000"))
AGE_QUERY_MAX_POOL_SIZE: int = int(os.getenv("AGE_QUERY_MAX_POOL_SIZE", "4"))

# ── Isolated AGE evaluator (Phase 5C) — TEST INFRASTRUCTURE ONLY ─────────────
# Phase 5C protected production by running model-written Cypher against a
# disposable copy of the graph. Phase 5D removed the need for that at
# runtime: there is no model-written Cypher any more. The evaluator database
# is RETAINED as adversarial/security-regression test infrastructure (see
# scripts/rebuild_age_eval.py and tests/test_age_eval_containment.py), and
# NO production runtime path requires it.
#
# Leaving these unset is now entirely normal and disables nothing.
# Model-generated Cypher does NOT run against DATABASE_URL. Phase 5 measured
# that Apache AGE 1.5.0 offers no read-only boundary — SET, REMOVE and
# DETACH DELETE all succeeded under SELECT-only grants AND
# default_transaction_read_only=on — so a generated query that slips past
# src/pipeline/aggregate/cypher_guard.py executes as a write against whatever
# graph it was pointed at. That is not hypothetical; it destroyed production
# graph data once during Phase 5 testing.
#
# The containment boundary is therefore the CONNECTION, not the query text:
# a dedicated role (muhafiz_age_eval_app) in a separate, disposable database
# (muhafiz_age_eval) that has NO CONNECT privilege on `muhafiz`. PostgreSQL
# refuses at connection time, before any Cypher is parsed. Measured on this
# cluster: pg_hba is `trust` for 127.0.0.1/::1 and scram-sha-256 elsewhere,
# so on loopback the evaluator PASSWORD is not a boundary at all — the
# CONNECT denial is what contains the evaluator, and it holds regardless of
# auth method.
#
# UNSET IS NORMAL AFTER PHASE 5D. This no longer gates any runtime route —
# the aggregate AGE path answers questions with this unset (verified). It is
# read only by `age_eval_client`, which the adversarial/security tests use
# (tests/test_age_eval_containment.py) and which `scripts/rebuild_age_eval.py`
# refreshes. That client still refuses any DSN naming `muhafiz`, or anything
# other than `muhafiz_age_eval`, so a copy-pasted production URL fails closed
# before destructive tests could aim at real data.
AGE_EVAL_DATABASE_URL: str = os.getenv("AGE_EVAL_DATABASE_URL", "")

# Server-side statement_timeout for evaluator queries, in milliseconds. Set
# as a server setting rather than a client-side cancel so PostgreSQL itself
# kills a generated query that plans badly over the copied graph.
AGE_EVAL_STATEMENT_TIMEOUT_MS: int = int(
    os.getenv("AGE_EVAL_STATEMENT_TIMEOUT_MS", "15000")
)

# Hard cap on rows a single evaluator query may return. An aggregate that
# wants more than this is not an aggregate; the client refuses rather than
# materialising an unbounded result.
AGE_EVAL_MAX_ROWS: int = int(os.getenv("AGE_EVAL_MAX_ROWS", "1000"))

# Bounded evaluator concurrency — the evaluation path must not be able to
# exhaust connection slots the application needs.
AGE_EVAL_MAX_POOL_SIZE: int = int(os.getenv("AGE_EVAL_MAX_POOL_SIZE", "4"))

# OPERATOR-ONLY connection to the evaluator database, used by
# scripts/rebuild_age_eval.py and by nothing else. No application runtime
# path reads this or AGE_EVAL_DATABASE_URL after Phase 5D.
#
# Why a second URL rather than widening the evaluator role. Rebuilding the
# graph calls drop_graph()/create_graph(), which mutate ag_catalog objects
# owned by the role that installed the AGE extension (measured: `must be
# owner of sequence _label_id_seq` when muhafiz_age_eval_app attempts it).
# Granting muhafiz_age_eval_app that ownership would widen the exact role
# whose narrowness IS the containment boundary — it is the role that runs
# model-generated Cypher. Rebuilding is an operator action performed out of
# band, so it gets operator credentials and the application role stays
# least-privilege.
AGE_EVAL_ADMIN_DATABASE_URL: str = os.getenv("AGE_EVAL_ADMIN_DATABASE_URL", "")


# ── Rate-limiter proxy awareness (audit finding F-09) ────────────────────────
# slowapi's default key_func (get_remote_address) reads the TCP peer address.
# Behind any reverse proxy/load balancer that address is the proxy's own IP
# for every request, collapsing every client into one shared rate-limit
# bucket -- one user can exhaust the login/case-create limit for everyone.
#
# Trusting X-Forwarded-For unconditionally would be worse than that bug: it's
# a plain request header, so any direct client could forge it to evade rate
# limiting entirely. This is therefore opt-in and paired with an allowlist --
# the forwarded header is only honored when the immediate TCP peer is itself
# one of these trusted proxies; a request arriving directly from anyone else
# still gets rate-limited on its real peer address, header or not.
#
# Default false / empty: exactly today's behavior (peer address, no XFF) for
# any deployment that hasn't explicitly configured its proxy.
TRUST_PROXY_HEADERS: bool = os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() == "true"
TRUSTED_PROXY_IPS: list[str] = [
    ip.strip() for ip in os.getenv("TRUSTED_PROXY_IPS", "").split(",") if ip.strip()
]
