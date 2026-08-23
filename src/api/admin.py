# ============================================================
# Admin API — analytics dashboard + knowledge base management
#
# Protected by JWT-cookie auth + the require_admin dependency
# (the user's is_admin flag). Called only by the admin-frontend app.
#
# Every number served here is computed from real rows (pipeline_runs,
# pipeline_steps, documents, error_logs, ingestion_jobs) plus the live
# ChromaDB collection count.
# Nothing is stubbed. Where the instrumentation tables from migration 003
# do not exist yet, the gateways return empty datasets and /instrumentation
# reports which tables are missing, so the UI can say so plainly rather than
# rendering a confident chart of nothing.
# ============================================================

import os
import time
import uuid
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from src import config
from src.auth.jwt import require_role
from src.auth.routes import limiter
from src.auth.rls_context import cross_case_rls_dependency
from src.database.models import User, PoliceReferenceData
from src.data_gateway import get_gateway
from src.ingestion.validation import validate_file, FileValidationError
from src.observability import analytics, errors as error_capture
from src.pipeline.sql_extractor import extract_sql_params
from src.mcp.client import execute_query

logger = logging.getLogger(__name__)

# Phase 2: admin dashboards deliberately aggregate across every case for
# station-admin/platform-admin (pipeline_runs/error_logs/etc. platform-wide
# stats) — a real per-case RLS restriction here wouldn't narrow these
# views, it would silently break them. RLS is armed but the case dimension
# is bypassed; the existing require_role() gates remain the real access
# control. See src/auth/rls_context.py.
router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(cross_case_rls_dependency)])

# Formats the ingestion loaders can actually read.
ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".md", ".csv", ".xlsx", ".xls",
    ".html", ".htm", ".docx", ".png", ".jpg", ".jpeg", ".webp",
}


# ══════════════════════════════════════════════════════════════════════
# Overview
# ══════════════════════════════════════════════════════════════════════

@router.get("/metrics")
async def get_metrics(admin: User = Depends(require_role("platform-admin"))):
    """Aggregated system metrics for the dashboard's summary cards."""
    gateway = await get_gateway()
    return await gateway.get_system_metrics()


@router.get("/instrumentation")
async def get_instrumentation(admin: User = Depends(require_role("platform-admin"))):
    """
    Which observability tables exist. The dashboard uses this to show an
    honest "run migration 003" banner instead of empty charts that look
    like a healthy, silent system.
    """
    gateway = await get_gateway()

    # Probe for real existence. Asking get_errors() whether it worked is not
    # enough: it swallows a missing table and returns [], so "no rows" and "no
    # table" come back identical — and an un-run migration would look like a
    # healthy, silent system.
    status = {}
    for table in ("error_logs", "ingestion_jobs", "session_attachments"):
        try:
            status[table] = await gateway.table_exists(table)
        except Exception:
            status[table] = False

    return {
        "tables": status,
        "ready": all(status.values()),
        "error_queue": error_capture.stats(),
    }


# ══════════════════════════════════════════════════════════════════════
# Usage, routing, latency
# ══════════════════════════════════════════════════════════════════════

@router.get("/usage")
async def get_usage(days: int = 30, granularity: str = "day", admin: User = Depends(require_role("platform-admin"))):
    """Requests over time + routing breakdown, from pipeline_runs."""
    if granularity not in ("day", "hour"):
        raise HTTPException(status_code=400, detail="granularity must be 'day' or 'hour'")

    gateway = await get_gateway()
    runs = await gateway.get_runs_since(analytics.since_iso(days))

    return {
        "days": days,
        "granularity": granularity,
        "total_requests": len(runs),
        "timeseries": analytics.usage_timeseries(runs, days, granularity),
        "routing": analytics.routing_breakdown(runs),
    }


@router.get("/verifier/stats")
async def get_verifier_stats(days: int = 30, admin: User = Depends(require_role("platform-admin"))):
    """Pass/fail stats for the Phase 6 grounding verifier."""
    gateway = await get_gateway()
    runs = await gateway.get_runs_since(analytics.since_iso(days))
    return analytics.verifier_stats(runs)


@router.get("/latency")
async def get_latency(days: int = 30, granularity: str = "day", admin: User = Depends(require_role("platform-admin"))):
    """
    Latency: overall avg/p50/p95, the trend over time, per-route, and — the
    useful one — per pipeline step, so you can see *which* stage is slow.
    """
    if granularity not in ("day", "hour"):
        raise HTTPException(status_code=400, detail="granularity must be 'day' or 'hour'")

    gateway = await get_gateway()
    since = analytics.since_iso(days)
    runs = await gateway.get_runs_since(since)
    steps = await gateway.get_step_latencies_since(since)

    return {
        "days": days,
        "granularity": granularity,
        "summary": analytics.latency_summary(runs),
        "timeseries": analytics.latency_timeseries(runs, days, granularity),
        "by_route": analytics.routing_breakdown(runs),
        "by_step": analytics.latency_by_step(steps),
    }


# ══════════════════════════════════════════════════════════════════════
# Errors
# ══════════════════════════════════════════════════════════════════════

@router.get("/errors")
async def get_errors(
    limit: int = 100,
    offset: int = 0,
    days: int = 30,
    severity: str = None,
    module: str = None,
    error_type: str = None,
    admin: User = Depends(require_role("platform-admin")),
):
    """Filterable error history."""
    gateway = await get_gateway()
    return {
        "errors": await gateway.get_errors(
            limit=limit, offset=offset, severity=severity, module=module,
            error_type=error_type, since=analytics.since_iso(days),
        ),
        "facets": await gateway.get_error_facets(),
    }


@router.get("/errors/trend")
async def get_error_trend(days: int = 30, granularity: str = "day", admin: User = Depends(require_role("platform-admin"))):
    """Errors over time, split by severity."""
    gateway = await get_gateway()
    rows = await gateway.get_errors_since(analytics.since_iso(days))
    return {
        "days": days,
        "granularity": granularity,
        "total": len(rows),
        "timeseries": analytics.error_timeseries(rows, days, granularity),
    }


# ══════════════════════════════════════════════════════════════════════
# Knowledge base
# ══════════════════════════════════════════════════════════════════════

@router.get("/kb/stats")
async def get_kb_stats(admin: User = Depends(require_role("platform-admin"))):
    """Total chunks indexed + chunks per document."""
    gateway = await get_gateway()
    return await gateway.get_kb_stats()


@router.get("/eval/entity-resolution")
async def get_entity_resolution_metrics(admin: User = Depends(require_role("supervisor"))):
    """
    Returns static metrics generated by scripts/eval_entity_resolution.py.
    The script wipes the evidence graph to run its tests, so it is run
    offline and dumps its results to a JSON file which we serve here.
    """
    import json
    metrics_path = config.DATA_DIR / "eval" / "resolution_metrics.json"
    if not metrics_path.exists():
        return {
            "generated_at": None,
            "error": "Metrics file not found. Run scripts/eval_entity_resolution.py to generate it."
        }
    try:
        with open(metrics_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.error("Failed to read resolution metrics: %s", exc)
        return {"generated_at": None, "error": "Failed to read metrics file."}


@router.get("/kb/jobs")
async def get_kb_jobs(limit: int = 50, offset: int = 0, admin: User = Depends(require_role("platform-admin"))):
    """Ingestion status per uploaded document: processing / success / failed."""
    gateway = await get_gateway()
    return await gateway.get_ingestion_jobs(limit=limit, offset=offset)


@router.get("/audit-logs")
async def get_audit_logs(
    limit: int = 100,
    offset: int = 0,
    days: int = 30,
    event_type: str = None,
    case_id: str = None,
    user_id: str = None,
    admin: User = Depends(require_role("platform-admin"))
):
    """View system audit logs."""
    gateway = await get_gateway()
    return await gateway.get_audit_logs(
        limit=limit,
        offset=offset,
        event_type=event_type,
        case_id=case_id,
        user_id=user_id,
        since=analytics.since_iso(days),
    )


@router.post("/kb/upload")
@limiter.limit("10/minute")
async def upload_kb_document(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    admin: User = Depends(require_role("platform-admin")),
):
    """
    Upload a document into the SHARED knowledge base.

    This is the real thing the old user-facing "ingest" page pretended to be:
    the file is written to the documents directory, then chunked, embedded and
    inserted into the same ChromaDB collection the retriever reads — not a
    separate store. Chunking runs in the background, and its progress is
    tracked in `ingestion_jobs` so the UI can show processing/success/failed.
    """
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    contents = await file.read()
    max_bytes = config.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File is {len(contents) // 1024 // 1024}MB; the limit is {config.MAX_UPLOAD_SIZE_MB}MB.",
        )

    # Keep the original name (retrieval cites it), but never let it escape the
    # documents directory.
    safe_name = os.path.basename(file.filename or f"upload{ext}")
    config.DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.DOCUMENTS_DIR / safe_name

    # Module 4.3: never silently overwrite an existing file of the same
    # name — the original bytes are part of the evidentiary record. On a
    # collision, disambiguate instead (filename__2.ext, incrementing) and
    # carry the renamed name through the job/response so the caller knows
    # a collision occurred, rather than losing the original file with no
    # error or log distinguishing "new" from "overwrote".
    if dest.exists():
        stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
        counter = 2
        while dest.exists():
            safe_name = f"{stem}__{counter}{suffix}"
            dest = config.DOCUMENTS_DIR / safe_name
            counter += 1
        logger.warning(
            "KB upload filename collision for %r — saved as %r instead.",
            file.filename, safe_name,
        )

    dest.write_bytes(contents)

    # Module 7.2: magic-byte/claimed-extension match and the zip-bomb
    # guard (for .docx/.xlsx) — route_and_load() also enforces this
    # later during ingestion, but checking here rejects synchronously
    # with a 400 instead of letting the caller believe the upload
    # succeeded ("processing") only for the background job to fail silently.
    try:
        validate_file(dest)
    except FileValidationError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc))

    gateway = await get_gateway()
    job_id = await gateway.create_ingestion_job({
        "filename": safe_name,
        "file_type": ext.lstrip("."),
        "file_size_bytes": len(contents),
        "status": "processing",
        "uploaded_by": str(admin.id),
    })

    await gateway.log_audit_event("admin_action", {"action": "upload_kb_document", "filename": safe_name}, str(admin.id))

    background_tasks.add_task(_ingest_uploaded_file, dest, job_id)

    return {
        "job_id": job_id,
        "filename": safe_name,
        "status": "processing",
        "file_size_bytes": len(contents),
    }


async def _ingest_uploaded_file(path: Path, job_id: str) -> None:
    """Chunk + embed an uploaded file, recording the outcome on its job row."""
    from src.ingestion.service import ingest_file

    gateway = await get_gateway()
    started = time.monotonic()
    try:
        stats = await ingest_file(path, is_global=True)
        duration_ms = int((time.monotonic() - started) * 1000)

        if stats.get("error"):
            await gateway.update_ingestion_job(job_id, {
                "status": "failed",
                "error_message": str(stats["error"])[:1000],
                "duration_ms": duration_ms,
            })
            return

        chunks = stats.get("chunks_added", 0)
        if chunks == 0:
            await gateway.update_ingestion_job(job_id, {
                "status": "failed",
                "error_message": "No text could be extracted from this file.",
                "duration_ms": duration_ms,
            })
            return

        await gateway.update_ingestion_job(job_id, {
            "status": "success",
            "chunks_added": chunks,
            "doc_id": stats.get("doc_id"),
            "duration_ms": duration_ms,
        })
    except Exception as exc:
        logger.error("KB ingestion failed for %s: %s", path.name, exc, exc_info=True)
        await gateway.update_ingestion_job(job_id, {
            "status": "failed",
            "error_message": f"{type(exc).__name__}: {exc}"[:1000],
            "duration_ms": int((time.monotonic() - started) * 1000),
        })


@router.delete("/kb/documents/{source_file}")
async def delete_kb_document(source_file: str, admin: User = Depends(require_role("platform-admin"))):
    """Remove a document and all of its chunks from the knowledge base."""
    import asyncio
    from src.retrieval import fulltext_index
    from src.retrieval.vector_store import ChromaVectorStore

    gateway = await get_gateway()
    deleted = await asyncio.to_thread(ChromaVectorStore.get_instance().delete_by_source, source_file)
    # Milestone A2: keep the persistent full-text index (chunk_fulltext)
    # from drifting out of sync with Chroma on deletion, same as it's
    # kept in sync on ingest (vector_store.py's upsert_documents()).
    await fulltext_index.delete_by_source(source_file)
    await gateway.delete_document_records(source_file)
    await gateway.log_audit_event("admin_action", {"action": "delete_kb_document", "source_file": source_file, "deleted_chunks": deleted}, str(admin.id))
    return {"deleted_chunks": deleted, "source_file": source_file}


# ══════════════════════════════════════════════════════════════════════
# Existing operational views
# ══════════════════════════════════════════════════════════════════════

@router.get("/runs")
async def get_runs(limit: int = 50, offset: int = 0, route_filter: str = None,
                   admin: User = Depends(require_role("platform-admin"))):
    gateway = await get_gateway()
    return await gateway.get_runs(limit=limit, offset=offset, route_filter=route_filter)


@router.get("/runs/{run_id}/steps")
async def get_run_steps(run_id: str, admin: User = Depends(require_role("platform-admin"))):
    gateway = await get_gateway()
    return await gateway.get_run_steps(run_id)


@router.get("/files")
async def get_files(limit: int = 50, offset: int = 0, admin: User = Depends(require_role("platform-admin"))):
    gateway = await get_gateway()
    return await gateway.get_generated_files(limit=limit, offset=offset)


@router.get("/mcp-calls")
async def get_mcp_calls(limit: int = 50, offset: int = 0, admin: User = Depends(require_role("platform-admin"))):
    gateway = await get_gateway()
    return await gateway.get_mcp_calls(limit=limit, offset=offset)


class McpDemoRequest(BaseModel):
    query: str


def _build_police_reference_sql(params: dict | None) -> str:
    """
    Builds the police_reference_data SELECT for the MCP demo route.

    The MCP `query` tool (@modelcontextprotocol/server-postgres) only
    accepts a single literal `sql` string — it has no bind-parameter
    support at the protocol level — so there's no way to hand it a
    genuinely parameterized query. This uses SQLAlchemy's own Core query
    builder (the same .ilike() pattern
    direct_backend.py::query_police_reference_data uses) plus literal-bind
    compilation to render safely-escaped SQL text, instead of hand-rolled
    string concatenation with manual quote-doubling.
    """
    conditions = []
    if params:
        if params.get("category"):
            conditions.append(PoliceReferenceData.category.ilike(f"%{params['category']}%"))
        if params.get("subject"):
            conditions.append(PoliceReferenceData.subject.ilike(f"%{params['subject']}%"))
        if params.get("section_ref"):
            conditions.append(PoliceReferenceData.section_ref.ilike(f"%{params['section_ref']}%"))

    stmt = select(PoliceReferenceData)
    if conditions:
        stmt = stmt.where(*conditions)

    # paramstyle="named" (rather than the dialect default, pyformat) avoids
    # SQLAlchemy doubling literal '%' characters to escape them for a
    # printf-style DBAPI substitution that will never actually happen here
    # (literal_binds means there are no bind params left to substitute) —
    # functionally harmless either way for LIKE/ILIKE, but this keeps the
    # rendered SQL text exactly what it appears to be.
    dialect = postgresql.dialect(paramstyle="named")
    return str(stmt.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))


@router.post("/mcp-demo")
async def mcp_demo(request: McpDemoRequest, admin: User = Depends(require_role("platform-admin"))):
    """
    Explicitly demonstrates the MCP Postgres tool-call path against
    police_reference_data, forcing MCP instead of the direct-SQL fast path
    every real chat query uses (src/pipeline/orchestrator.py's SQL branch).
    MCP stays fully wired in and callable — this is how it's shown
    separately rather than gating every SQL-routed question behind it.
    """
    gateway = await get_gateway()

    # mcp_tool_calls.run_id is a real FK to pipeline_runs, which itself FKs
    # to sessions — a demo call still needs both rows to log correctly.
    session_id = str(uuid.uuid4())
    await gateway.create_session(session_id, str(admin.id), "MCP demo (admin debug)")
    run_id = await gateway.create_run(session_id, request.query)

    params = None
    sql_query = _build_police_reference_sql(None)
    db_results = []
    status = "success"
    output_summary = None
    t0 = time.monotonic()
    try:
        params = await extract_sql_params(request.query)
        sql_query = _build_police_reference_sql(params)

        db_results = await execute_query(sql_query)
        output_summary = {"row_count": len(db_results)}
        return {
            "query": request.query,
            "extracted_params": params,
            "sql": sql_query,
            "results": db_results,
            "run_id": run_id,
        }
    except Exception as exc:
        status = "failed"
        output_summary = {"error": str(exc)}
        raise HTTPException(status_code=502, detail=f"MCP query failed: {exc}")
    finally:
        duration_ms = int((time.monotonic() - t0) * 1000)
        await gateway.log_mcp_tool_call(
            run_id=run_id,
            mcp_server="postgres",
            tool_name="query",
            status=status,
            input_params={"user_query": request.query, "extracted_params": params, "sql": sql_query},
            output_summary=output_summary,
            duration_ms=duration_ms,
        )


@router.delete("/files/{file_id}")
async def delete_file(file_id: str, admin: User = Depends(require_role("platform-admin"))):
    """Delete a generated file record, and its bytes from disk."""
    gateway = await get_gateway()
    file_record = await gateway.delete_generated_file(file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")

    storage_path = file_record.get("storage_path")
    if storage_path and os.path.exists(storage_path):
        try:
            os.remove(storage_path)
        except Exception as e:
            logger.error(f"Failed to delete physical file {storage_path}: {e}")

    await gateway.log_audit_event("admin_action", {"action": "delete_generated_file", "file_id": file_id}, str(admin.id))
    return {"status": "success", "deleted": file_id}


@router.get("/users")
async def get_users(limit: int = 50, offset: int = 0, admin: User = Depends(require_role("platform-admin"))):
    gateway = await get_gateway()
    return await gateway.get_all_users(limit=limit, offset=offset)
