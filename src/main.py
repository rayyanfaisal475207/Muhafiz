# ============================================================
# Muhafiz — FastAPI Application Entry Point
#
# This file wires together the entire system into a web API.
# At Milestone 1, this file proves the project structure is correct
# by starting without import errors.
#
# WHAT THIS FILE DOES:
# 1. Creates the FastAPI app instance
# 2. Validates configuration at startup
# 3. Ensures required directories exist
# 4. Registers API route handlers (stubs at Milestone 1, real at Milestone 7)
# 5. Provides a health check endpoint to verify the server is running
#
# TO RUN (from the rag_system/ directory):
#   uvicorn src.main:app --reload
# OR (using this file directly):
#   python src/main.py
# ============================================================

import logging
import sys
from contextlib import asynccontextmanager
import json
import uvicorn
from typing import Optional
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src import config
from src.config import ensure_directories, validate_config
from src.database import pipeline_logger
from src.pipeline.orchestrator import process_query
from src.pipeline.router import route_query
from src.pipeline.harness.cutover import run_cutover_query
from src.data_gateway import get_gateway

from src.auth.routes import router as auth_router, limiter, get_current_user
from src.auth.jwt import require_role
from src.database.models import User
from fastapi import Depends
from src.api.sessions import router as sessions_router
from src.api.profile import router as profile_router
from src.api.admin import router as admin_router
from src.api.projects import router as projects_router
from src.api.cases import router as cases_router
from src.api.case_assignments import router as case_assignments_router
from src.api.attachments import router as attachments_router
from src.api.graph_review import router as graph_review_router
from src.api.community_admin import router as community_admin_router
from src.auth.rls_context import set_case_scope
from src.observability import errors as error_capture

from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler


# ── Logging Setup ──────────────────────────────────────────────────────────────
# Configure logging before anything else so all startup messages are captured.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ── Lifespan Handler (startup + shutdown) ──────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager: runs startup code before the server
    accepts requests, and shutdown code when the server is stopping.

    asynccontextmanager: the `yield` separates startup (before) from
    shutdown (after). Think of it as: everything before yield = __init__,
    everything after yield = __del__.
    """
    # ── STARTUP ──────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("Muhafiz starting up...")
    logger.info("=" * 60)

    # Ensure all required directories exist before any component tries to use them
    ensure_directories()
    logger.info("Data directories ensured.")

    # Mirror every ERROR/CRITICAL into the error_logs table so the admin
    # dashboard has an error history instead of a terminal that scrolled away.
    error_capture.install()
    logger.info("Error capture installed.")

    # ── Database Initialization ──────────────────────────────────────────
    from src.database.postgres import is_postgres_configured, init_postgres, MissingSchemaError

    if is_postgres_configured():
        # PostgreSQL (local/self-hosted, direct SQL) is the only database —
        # no REST/hosted fallback exists. A failed init still doesn't kill the
        # server (so /health can report the error rather than the process
        # crash-looping), but every DB-backed route will fail until Postgres
        # is reachable.
        try:
            await init_postgres()
            logger.info("[OK] PostgreSQL initialized (primary database)")
        except MissingSchemaError:
            # init_postgres() already logged a specific, actionable
            # CRITICAL for this — don't also log the generic "unreachable"
            # warning below, which would misdirect troubleshooting toward
            # connectivity when Postgres is reachable and the real cause is
            # a missing plain-SQL migration.
            pass
        except Exception as exc:
            logger.warning(
                "[WARN] PostgreSQL unreachable at startup (%s). "
                "DB-backed routes will fail until DATABASE_URL is reachable "
                "(check docker compose up -d / your self-hosted instance).",
                exc.__class__.__name__,
            )

        # Archive the old SQLite file if it still exists (one-time migration)
        sqlite_path = config.DB_PATH
        if sqlite_path.exists():
            archived = sqlite_path.with_suffix(".db.archived")
            if not archived.exists():
                sqlite_path.rename(archived)
                logger.info(
                    "Archived legacy SQLite: %s → %s",
                    sqlite_path.name, archived.name
                )
            else:
                logger.info("Legacy SQLite already archived: %s", archived.name)

        # pipeline_logger.py's write-only side-log (LLM call previews,
        # per-step timings) runs unconditionally regardless of Postgres vs
        # legacy mode — it is NOT the "legacy SQLite mode" this file's other
        # branch warns about (nothing reads case/user/RBAC data from it).
        # Confirmed live: because init_db() used to only run in the
        # Postgres-NOT-configured branch below, a pipeline_logs.db file
        # created before the verifier feature shipped (Phase 6) never got
        # its ALTER TABLE migration and silently errored on every single
        # query's verifier_passed/verifier_regenerated write from then on —
        # harmless (caught, logged, doesn't affect the real Postgres-backed
        # dashboard) but noisy. Ensuring the schema/migration here too,
        # independent of which DB is primary, is the actual fix rather than
        # patching the one broken file by hand.
        from src.database.db import init_db as init_sqlite_log_db
        init_sqlite_log_db()
    elif config.REQUIRE_POSTGRES:
        # DATABASE_URL isn't configured and nothing explicitly opted out of
        # requiring Postgres. The legacy SQLite schema predates the entire
        # case/auth/RBAC model — silently falling back to it is the actual
        # bug (see issues.md's DATABASE_URL enforcement finding), not a
        # feature to preserve. Refuse to start rather than serve traffic
        # against a schema that has no users/cases/RBAC tables at all.
        logger.critical(
            "[CRITICAL] DATABASE_URL is not configured and REQUIRE_POSTGRES "
            "is not disabled. Refusing to start in the legacy SQLite mode "
            "that predates the case/auth/RBAC model. Set DATABASE_URL, or "
            "set REQUIRE_POSTGRES=false to explicitly opt into the degraded "
            "legacy mode."
        )
        raise RuntimeError(
            "DATABASE_URL is not configured. Refusing to start "
            "(REQUIRE_POSTGRES=true). Set DATABASE_URL, or set "
            "REQUIRE_POSTGRES=false to explicitly accept the legacy "
            "SQLite-only mode."
        )
    else:
        # Explicit opt-out: keep using SQLite (legacy mode)
        from src.database.db import init_db
        init_db()
        logger.critical(
            "[CRITICAL] Running in legacy SQLite mode (REQUIRE_POSTGRES=false). "
            "This schema predates the case/auth/RBAC model — cases, users, "
            "and access control will not work. Intended only for narrow "
            "legacy/local debugging."
        )

    # Validate configuration. Warnings are non-fatal (individual calls will
    # fail until the relevant setting is fixed); critical errors (a public
    # JWT secret, an unrecognized ENVIRONMENT) stop a production deployment
    # from starting at all — see src/config.py::validate_config.
    config_warnings, config_critical = validate_config()
    for warning in config_warnings:
        logger.warning("[WARN] Config warning: %s", warning)

    if config_critical:
        for error in config_critical:
            logger.critical("[CRITICAL] Config error: %s", error)
        if config.ENVIRONMENT == "production":
            raise RuntimeError(
                "Refusing to start in production with critical configuration "
                "errors: " + "; ".join(config_critical)
            )
        logger.critical(
            "Server starting DESPITE the critical configuration errors above "
            "because ENVIRONMENT='%s' (only 'production' is refused at "
            "startup). Do not deploy this configuration as-is.",
            config.ENVIRONMENT,
        )
    elif not config_warnings:
        logger.info("[OK] Configuration valid. LLM provider: %s", config.LLM_PROVIDER)

    logger.info("ChromaDB persist dir: %s", config.CHROMA_PERSIST_DIR)
    logger.info("Server ready at http://%s:%d", config.HOST, config.PORT)

    yield  # Server is running — handle requests

    # ── SHUTDOWN ─────────────────────────────────────────────────────────
    logger.info("Muhafiz shutting down. Goodbye!")


# ── FastAPI App ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Muhafiz API",
    description=(
        "Muhafiz — Islamabad Police reference assistant, answered instantly. "
        "Self-correcting AI assistant with hybrid retrieval (semantic + BM25), "
        "RRF re-ranking, relevance evaluation, and automatic retry loop."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS: allow the React frontend (running on a different port) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Rate Limiting (slowapi) ───────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(sessions_router, prefix="/api/sessions", tags=["sessions"])
app.include_router(profile_router, prefix="/api/profile", tags=["profile"])
app.include_router(admin_router)   # prefix already set inside admin.py
app.include_router(projects_router, prefix="/api/projects", tags=["projects"])
app.include_router(cases_router, prefix="/api/cases", tags=["cases"])
app.include_router(case_assignments_router)
app.include_router(attachments_router, prefix="/api/attachments", tags=["attachments"])
app.include_router(graph_review_router)   # prefix already set inside graph_review.py
app.include_router(community_admin_router)   # prefix already set inside community_admin.py

# ── Models ─────────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    session_id: str
    message: str
    project_id: Optional[str] = None
    case_id: Optional[str] = None
    # Explicit, per-query opt-in for live web search (a UI checkbox/toggle).
    # Never inferred and never triggered automatically as a fallback from a
    # failed RAG attempt — see process_query()'s docstring.
    enable_web_search: bool = False


# ── Health Check ───────────────────────────────────────────────────────────────
@app.get("/health", tags=["System"])
async def health_check():
    """
    Simple health check endpoint.
    Returns system status, degrading `status` to "degraded" when any real
    dependency is unreachable — used by deployment platforms and monitoring
    to verify the service is alive.

    Live-reproduced gap this fixes: this endpoint used to return a
    hardcoded {"status": "ok"} regardless of what it found, and never
    probed Postgres at all. Confirmed live: with Postgres fully down
    (Docker daemon stopped), /health still returned {"status": "ok",
    "vector_store_status": "ok", ...} in the same moment every
    Postgres-backed call (register, login, everything) was failing with a
    500. Monitoring watching only `status` would never have caught it.
    """
    doc_count = 0
    store_status = "ok"
    try:
        import asyncio
        from src.retrieval.vector_store import ChromaVectorStore
        doc_count = await asyncio.to_thread(ChromaVectorStore.get_instance().count)
    except Exception as exc:
        store_status = f"error: {exc}"

    db_status = "ok"
    try:
        import asyncio
        from sqlalchemy import text
        from src.database.postgres import get_session

        async def _probe():
            # get_session() is an @asynccontextmanager (async with), not an
            # async generator/iterator (async for) -- confirmed live
            # (2026-08-06): `async for` here raised "'async for' requires
            # an object with __aiter__ method, got
            # _AsyncGeneratorContextManager" on EVERY call, so this probe
            # always reported db_status: error regardless of whether
            # Postgres was actually reachable, defeating the point of this
            # whole endpoint (a real, healthy Postgres connection verified
            # working throughout that same session while this kept failing).
            async with get_session() as session:
                await session.execute(text("SELECT 1"))

        # A health probe must never itself hang — a stalled/unreachable host
        # (as opposed to a fast connection-refused) could otherwise block
        # this endpoint for the OS-level TCP timeout, which defeats the
        # point of a liveness check.
        await asyncio.wait_for(_probe(), timeout=3.0)
    except Exception as exc:
        db_status = f"error: {exc}"

    overall_status = "ok" if store_status == "ok" and db_status == "ok" else "degraded"

    return {
        "status": overall_status,
        "version": "0.1.0",
        "llm_provider": config.LLM_PROVIDER,
        "vector_store_status": store_status,
        "database_status": db_status,
        "documents_in_store": doc_count,
    }


# ── API Routes ────────────────────────────────────────────────────────────────
@app.post("/api/chat", tags=["Chat"])
@limiter.limit("60/minute")
async def chat_endpoint(request: Request, chat_request: ChatRequest, current_user: User = Depends(get_current_user)):

    """
    Main chat endpoint — accepts a user message and streams pipeline trace events
    + the final response as Server-Sent Events.
    """
    import asyncio
    gateway = await get_gateway()
    user_id = str(current_user.id)

    # Fetch profile and session concurrently — both are independent reads.
    user_profile, session = await asyncio.gather(
        gateway.get_user_context_profile(user_id),
        gateway.get_session(chat_request.session_id),
    )
    project_id = chat_request.project_id or (session.get("project_id") if session else None)
    case_id = chat_request.case_id or (session.get("case_id") if session else None)

    # RBAC/ABAC: a case_id from the client (or a session it never verified either)
    # must never reach the pipeline unchecked — RLS only enforces that retrieved
    # rows belong to this case_id, not that this user is allowed to see it.
    if case_id and not await gateway.check_case_access(case_id, user_id, current_user.role):
        raise HTTPException(status_code=403, detail="Not assigned to this case")

    # Phase 2: arm Postgres RLS for this request HERE, once case_id is
    # resolved and access-checked — process_query() no longer sets this
    # itself (see its docstring). Set before the session-creation gateway
    # call below too, since a general (no-case) session's INSERT needs
    # app.case_id='' already set to satisfy the FOR ALL policy's WITH CHECK.
    set_case_scope(case_id)

    # Create the session up-front WITH its owner. Sessions must never be
    # created as a side effect of logging (that produced ownerless rows
    # that the sidebar can't list and /api/sessions/{id} 403s on).
    if session is None:
        provisional_title = " ".join(chat_request.message.split()[:6])[:80] or "New Conversation"
        await gateway.create_session(chat_request.session_id, user_id, provisional_title, project_id, case_id)

    # Agent-harness live-traffic cutover (AGENT_HARNESS_IMPLEMENTATION_PLAN.md
    # §6). Classify ONCE here to decide cutover-or-not, matching the same
    # route_query() call classify_to_subagent() branches on inside
    # Supervisor.handle() itself.
    #
    # KNOWN, ACCEPTED INEFFICIENCY: a cutover request gets classified TWICE
    # — once here, once again inside Supervisor.handle() (it calls
    # route_query() itself, per its own contract of "classify, dispatch,
    # return exactly what the sub-agent returns" — see supervisor.py).
    # Changing Supervisor.handle()'s signature to accept a pre-classified
    # route was deliberately not done this session — that would touch an
    # already-tested, load-bearing contract for a first cutover slice's
    # sake. router.py's deterministic overrides make the common case cheap;
    # revisit only if this is confirmed to matter under real load.
    #
    # File-output requests are explicitly excluded regardless of route:
    # classify_to_subagent() overrides ANY route to Report Drafting when
    # output_format is a file format, and Report Drafting is not part of
    # this session's cutover slice (Semantic Search only, resolved via
    # AskUserQuestion) — checking output_format here keeps a file request
    # whose text happens to classify as "RAG" from being silently cut over
    # to a sub-agent nobody decided to cut over yet.
    cutover_route: Optional[str] = None
    if config.HARNESS_CUTOVER_ROUTES:
        try:
            route_result = await route_query(chat_request.message)
            candidate = str(route_result.get("route") or "").upper()
            # route_query() itself decides output_format (e.g. "generate a
            # PDF report on..." classifies with output_format="file_pdf") —
            # there is no separate request field for it. A file-output
            # classification always excludes cutover this slice, regardless
            # of `candidate`, per the same reasoning as the comment above.
            classified_output_format = str(route_result.get("output_format") or "chat").lower()
            if candidate in config.HARNESS_CUTOVER_ROUTES and classified_output_format == "chat":
                cutover_route = candidate
        except Exception as exc:
            logger.warning("Cutover classification failed, falling back to orchestrator.py: %s", exc)

    async def event_generator():
        try:
            if cutover_route is not None:
                stream = run_cutover_query(
                    session_id=chat_request.session_id,
                    user_message=chat_request.message,
                    project_id=project_id,
                    case_id=case_id,
                    user_id=user_id,
                    user_role=current_user.role,
                    preferred_language=(user_profile or {}).get("preferred_language"),
                    gateway=gateway,
                )
            else:
                stream = process_query(
                    chat_request.session_id, chat_request.message,
                    project_id=project_id, case_id=case_id, user_profile=user_profile, user_id=user_id,
                    user_role=current_user.role,
                    enable_web_search=chat_request.enable_web_search,
                )
            async for event in stream:
                # [Merge reconciliation — harness-reconciliation Unit 12
                # follow-up] The harness hands DIRECT back rather than
                # answering it: that route performs no retrieval and the
                # Verifier never gates it, so it is outside the harness's
                # scope by design (see supervisor.py's NO_SUB_AGENT
                # handling and cutover.py's own delegate_to_legacy yield).
                # Only `run_cutover_query()` can ever emit this — not
                # currently reachable via `cutover_route` (DIRECT is never
                # in `config.HARNESS_CUTOVER_ROUTES`), but kept so this
                # boundary can't silently misbehave the moment that config
                # is ever widened to include it.
                if event.get("delegate_to_legacy"):
                    yield f"data: {json.dumps(event)}\n\n"
                    async for legacy_event in process_query(
                        chat_request.session_id, chat_request.message,
                        project_id=project_id, case_id=case_id,
                        user_profile=user_profile, user_id=user_id,
                        user_role=current_user.role,
                        enable_web_search=chat_request.enable_web_search,
                    ):
                        yield f"data: {json.dumps(legacy_event)}\n\n"
                    break

                yield f"data: {json.dumps(event)}\n\n"

        except Exception as e:
            logger.error("Chat pipeline error: %s", e, exc_info=True)
            error_event = {"step": "system", "status": "error", "detail": str(e)}
            yield f"data: {json.dumps(error_event)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# NOTE: Document ingestion and knowledge-base management moved to the ADMIN API
# (/api/admin/kb/*). Normal users no longer ingest into the shared knowledge base:
# they attach files to a single conversation via /api/attachments, which never
# touches the vector store. See docs/INGESTION.md.


@app.get("/api/files/{file_id}/download", tags=["Files"])
async def download_file(file_id: str, current_user: User = Depends(get_current_user)):
    """
    Download a generated file. Ensures the user owns the file.
    """
    from src.data_gateway import get_gateway
    import os

    try:
        import uuid
        fid = uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID format")

    gateway = await get_gateway()
    file_record = await gateway.get_generated_file(file_id)

    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")

    # Phase 5, Module 5.4: station-admin used to get blanket cross-case
    # access here, unlike everywhere else in the system (only
    # platform-admin gets that). Now scoped by case_assignments when the
    # file has a case_id; a NULL case_id (no backfill for pre-migration
    # rows, or genuinely not case-derived) keeps the old blanket
    # station-admin/platform-admin access — an explicit, accepted
    # limitation for existing files, not a silent gap. See
    # migrations/013_generated_files_case_id.sql.
    is_owner = str(file_record["user_id"]) == str(current_user.id)
    file_case_id = file_record.get("case_id")
    if is_owner or current_user.role == "platform-admin":
        authorized = True
    elif file_case_id is None:
        authorized = current_user.role in ("station-admin", "platform-admin")
    elif current_user.role == "station-admin":
        authorized = await gateway.check_case_access(file_case_id, str(current_user.id), current_user.role)
    else:
        authorized = False

    if not authorized:
        raise HTTPException(status_code=403, detail="Unauthorized to access this file")

    if not os.path.exists(file_record["storage_path"]):
        raise HTTPException(status_code=404, detail="File content no longer exists on server")

    MIME_TYPES = {
        "pdf": "application/pdf",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    file_type = (file_record.get("file_type") or "").lower()
    file_name = file_record["file_name"]
    # Legacy rows stored the bare title without an extension — repair on the way out.
    if file_type and not file_name.lower().endswith(f".{file_type}"):
        file_name = f"{file_name}.{file_type}"

    return FileResponse(
        path=file_record["storage_path"],
        filename=file_name,
        media_type=MIME_TYPES.get(file_type, "application/octet-stream")
    )



# ── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=config.RELOAD,
    )
