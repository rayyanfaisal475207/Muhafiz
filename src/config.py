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


# ── Guarded web search (Phase 5.7) ────────────────────────────────────────────
# Air-gap deployments disable ALL outbound web access — this is the one route
# in the whole architecture that needs it (architecture doc, "Guarded web
# search"). No prior AIR_GAP flag exists anywhere in the codebase (confirmed
# by repo-wide search before adding this) — greenfield naming.
AIR_GAP_MODE: bool = os.getenv("AIR_GAP_MODE", "false").strip().lower() == "true"

# ── Agent-harness shadow mode (see docs/AGENT_HARNESS_DESIGN.md) ──────────────
# Runs the agent harness on a SAMPLE of real queries AFTER the legacy pipeline
# has already answered the user, and logs what the harness would have said to
# `harness_shadow_runs` (migration 020). Nothing it produces is ever shown to an
# investigator, and it cannot change the answer the user received.
#
# DEFAULTS OFF, and that is the point: enabling it doubles retrieval and
# generation work for every sampled query, against the same model server the
# live path depends on. It is a deliberate operator decision with a real cost,
# not something that should switch on because a new build was deployed.
HARNESS_SHADOW_MODE: bool = (
    os.getenv("HARNESS_SHADOW_MODE", "false").strip().lower() == "true"
)

# Fraction of eligible queries to shadow, 0.0–1.0. 0.05 = 5%.
#
# Sampling is what keeps the added load bounded. A shadow run costs roughly what
# the real query cost — the same retrieval, the same generation — so shadowing
# everything would double the load on the model server and could slow the live
# path it shares. Start low; raise it only once the disagreement rate is known
# and the model server has headroom.
HARNESS_SHADOW_SAMPLE_RATE: float = float(
    os.getenv("HARNESS_SHADOW_SAMPLE_RATE", "0.05") or 0.05
)

# How many shadow runs may be in flight at once, process-wide.
#
# Sampling bounds the RATE; this bounds the CONCURRENCY, and they fail
# differently. A burst of traffic can put many samples in flight simultaneously
# even at a low rate, and each one holds a model-server slot the live path also
# needs. At 1 (the default) a shadow run is single-flight: if one is already
# running, the next eligible query is skipped rather than queued — the harness
# must never become a queue that outlives the request that spawned it.
HARNESS_SHADOW_MAX_CONCURRENCY: int = int(
    os.getenv("HARNESS_SHADOW_MAX_CONCURRENCY", "1") or 1
)

# Which legacy routes are eligible. Deliberately NOT every route:
#   * DIRECT has no retrieval to compare, so there is nothing to learn.
#   * The cross-case routes (XGRAPH/XAGG/XNETWORK) run under a role gate and
#     arm cross-case RLS scope. Shadowing them would arm that scope a second
#     time, outside the request that authorized it, for a result no one reads.
#     Excluded until within-case shadowing has been proven in production.
HARNESS_SHADOW_ROUTES: frozenset = frozenset(
    r.strip().upper()
    for r in os.getenv("HARNESS_SHADOW_ROUTES", "RAG,GRAPH,GRAPH_HYBRID").split(",")
    if r.strip()
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

    # Shadow mode is fire-and-forget by design, so a bad setting here would
    # otherwise present as "shadow mode logs nothing" with no error anywhere —
    # indistinguishable from it being switched off. Caught at startup instead.
    if HARNESS_SHADOW_MODE:
        if not 0.0 < HARNESS_SHADOW_SAMPLE_RATE <= 1.0:
            errors.append(
                f"HARNESS_SHADOW_MODE is enabled but HARNESS_SHADOW_SAMPLE_RATE "
                f"is {HARNESS_SHADOW_SAMPLE_RATE} — must be greater than 0 and "
                f"at most 1.0, or no query is ever sampled."
            )
        if HARNESS_SHADOW_MAX_CONCURRENCY < 1:
            errors.append(
                f"HARNESS_SHADOW_MAX_CONCURRENCY is "
                f"{HARNESS_SHADOW_MAX_CONCURRENCY} — must be at least 1, or "
                f"every shadow run is rejected by its own concurrency guard."
            )
        if not HARNESS_SHADOW_ROUTES:
            errors.append(
                "HARNESS_SHADOW_MODE is enabled but HARNESS_SHADOW_ROUTES is "
                "empty — no route would ever be eligible."
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
