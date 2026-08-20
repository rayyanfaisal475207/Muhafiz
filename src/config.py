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
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

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


# ── Pipeline Settings ─────────────────────────────────────────────────────────
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "1"))
TOP_K_RETRIEVAL: int = int(os.getenv("TOP_K_RETRIEVAL", "10"))
TOP_K_RERANK: int = int(os.getenv("TOP_K_RERANK", "5"))

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
