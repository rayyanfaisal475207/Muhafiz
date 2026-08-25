import logging
import uuid
from typing import Optional, Any
from datetime import datetime

from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.postgres import get_session, engine
from src.database.models import (
    User, UserContextProfile, Session, Message, PipelineRun, PipelineStep,
    GeneratedFile, Document, McpToolCall, Project, ProjectMemory,
    PoliceReferenceData, Case,
)

logger = logging.getLogger(__name__)

# Phase 7, Module 7.4: get_runs_since()/get_step_latencies_since()/
# get_errors_since() feed admin analytics timeseries/aggregates (usage,
# latency, error trends) — they intentionally return every row in the
# caller's `since` window, not a page of results (there is no "load more"
# consumer for these; get_errors() is the separate, already-paginated
# listing endpoint). A hard row cap here is a defensive backstop against
# genuinely pathological unbounded growth (e.g. an old install with years
# of history, or a caller requesting a huge `days` window) — at realistic
# data volumes it never triggers and the aggregates are exact; past it,
# the most recent max_rows are used instead of silently loading everything
# into memory. Not real pagination — see issues.md/solution.md's own
# "unbounded" framing, which describes the missing LIMIT, not a listing UI.
ANALYTICS_MAX_ROWS = 50_000


def _kb_stats_documents(counts_by_source, doc_by_filename) -> tuple[int, list[dict]]:
    """
    Pure helper behind DirectGateway.get_kb_stats()'s `documents`/
    `total_documents` fields — pulled out so it's directly unit-testable
    without a real Chroma instance or DB session.

    Bug fix (found during a full E2E pass): the original implementation
    used `counts_by_source.most_common(500)`, which ranks by CHUNK COUNT,
    not recency, and silently truncates there — a freshly-uploaded
    document with only 1-2 chunks could never appear no matter how
    recent, since any multi-chunk FIR record already in the corpus
    outranked it. `total_documents` was then `len(docs)` AFTER that same
    cap, which reads as "the total document count" but was actually
    "however many survived the cap" — misleading once the real corpus
    exceeds 500 documents (it already does).

    Returns (total_documents, documents) where `total_documents` is the
    TRUE, uncapped count and `documents` is sorted by `ingested_at`
    descending (most recent first — so a fresh upload is immediately
    visible) and capped at 500 for response-size sanity. A document with
    no matching `documents` row (`ingested_at` unknown) sorts as OLDEST,
    not first.
    """
    docs_all = [{
        "doc_id": source,
        "filename": source,
        "doc_type": doc_by_filename[source].doc_type if source in doc_by_filename else None,
        "chunk_count": count,
        "is_global": True,
        "ingested_at": (
            doc_by_filename[source].ingested_at.isoformat()
            if source in doc_by_filename and doc_by_filename[source].ingested_at else None
        ),
    } for source, count in counts_by_source.items()]

    # "" would sort before every real ISO timestamp on a descending sort —
    # backwards for a doc with no known ingested_at, which must sort last.
    docs_all.sort(key=lambda d: d["ingested_at"] or "0000-00-00", reverse=True)

    return len(docs_all), docs_all[:500]


class DirectGateway:
    # ── User Operations ──
    async def get_user_by_id(self, user_id: str) -> Optional[dict]:
        async with get_session() as db:
            res = await db.execute(select(User).where(User.id == uuid.UUID(str(user_id))))
            u = res.scalars().first()
            return {
                "id": str(u.id), "email": u.email, "password_hash": u.password_hash,
                "role": u.role, "is_admin": u.role == "platform-admin",
                "company_name": u.company_name, "plan": u.plan,
                "police_station": u.police_station,
            } if u else None

    async def get_user_by_email(self, email: str) -> Optional[dict]:
        async with get_session() as db:
            res = await db.execute(select(User).where(User.email == email))
            u = res.scalars().first()
            return {
                "id": str(u.id), "email": u.email, "password_hash": u.password_hash,
                "role": u.role, "is_admin": u.role == "platform-admin",
                "company_name": u.company_name, "plan": u.plan,
                "police_station": u.police_station,
            } if u else None

    async def create_user(self, user_data: dict) -> dict:
        async with get_session() as db:
            u = User(**user_data)
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return {
                "id": str(u.id), "email": u.email, "password_hash": u.password_hash,
                "role": u.role, "is_admin": u.role == "platform-admin",
                "company_name": u.company_name, "plan": u.plan,
            }

    async def get_all_users(self, limit: int = 50, offset: int = 0) -> list[dict]:
        async with get_session() as db:
            res = await db.execute(select(User).order_by(desc(User.created_at)).limit(limit).offset(offset))
            return [{
                "id": str(u.id), "email": u.email, "role": u.role,
                "is_admin": u.role == "platform-admin",
                "company_name": u.company_name, "plan": u.plan,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            } for u in res.scalars().all()]

    async def get_user_context_profile(self, user_id: str) -> dict:
        async with get_session() as db:
            res = await db.execute(select(UserContextProfile).where(UserContextProfile.user_id == uuid.UUID(str(user_id))))
            p = res.scalars().first()
            if p:
                return {"id": str(user_id), "context_text": p.context_text, "preferred_language": p.preferred_language, "llm_mode": p.llm_mode}
            return {"id": str(user_id), "context_text": "", "preferred_language": "auto", "llm_mode": "cloud"}

    async def update_user_context_profile(self, user_id: str, data: dict) -> dict:
        async with get_session() as db:
            res = await db.execute(select(UserContextProfile).where(UserContextProfile.user_id == uuid.UUID(str(user_id))))
            p = res.scalars().first()
            if not p:
                p = UserContextProfile(user_id=uuid.UUID(str(user_id)), **data)
                db.add(p)
            else:
                for k, v in data.items():
                    setattr(p, k, v)
            await db.commit()
            return {"context_text": p.context_text, "preferred_language": p.preferred_language, "llm_mode": p.llm_mode}

    # ── Session & Message Operations ──
    async def create_session(self, session_id: str, user_id: str, title: str, project_id: Optional[str] = None, case_id: Optional[str] = None) -> None:
        async with get_session() as db:
            session_kwargs = {
                "session_id": uuid.UUID(str(session_id)),
                "user_id": uuid.UUID(str(user_id)) if user_id else None,
                "title": title
            }
            if project_id:
                session_kwargs["project_id"] = uuid.UUID(str(project_id))
            if case_id:
                session_kwargs["case_id"] = case_id
            db.add(Session(**session_kwargs))
            await db.commit()

    async def get_sessions_for_user(self, user_id: str, project_id: str | None = None, case_id: str | None = None) -> list[dict]:
        async with get_session() as db:
            q = select(Session).where(Session.user_id == uuid.UUID(str(user_id))).where(Session.deleted_at == None)
            if project_id:
                q = q.where(Session.project_id == uuid.UUID(str(project_id)))
            if case_id:
                q = q.where(Session.case_id == case_id)
            res = await db.execute(q.order_by(desc(Session.updated_at)))
            return [{
                "session_id": str(s.session_id), "title": s.title,
                "project_id": str(s.project_id) if s.project_id else None,
                "case_id": s.case_id,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            } for s in res.scalars().all()]

    async def get_session(self, session_id: str) -> Optional[dict]:
        async with get_session() as db:
            res = await db.execute(select(Session).where(Session.session_id == uuid.UUID(str(session_id))).where(Session.deleted_at == None))
            s = res.scalars().first()
            return {"session_id": str(s.session_id), "user_id": str(s.user_id) if s.user_id else None, "project_id": str(s.project_id) if s.project_id else None, "case_id": s.case_id, "title": s.title} if s else None

    async def update_session_title(self, session_id: str, title: str) -> None:
        async with get_session() as db:
            res = await db.execute(select(Session).where(Session.session_id == uuid.UUID(str(session_id))))
            s = res.scalars().first()
            if s:
                s.title = title
                s.updated_at = datetime.utcnow()
                await db.commit()

    async def delete_session(self, session_id: str) -> None:
        async with get_session() as db:
            res = await db.execute(select(Session).where(Session.session_id == uuid.UUID(str(session_id))))
            s = res.scalars().first()
            if s:
                s.deleted_at = datetime.utcnow()
                await db.commit()

    async def save_message(self, session_id: str, role: str, content: str,
                           degradation_trace: dict = None) -> None:
        """
        `degradation_trace` (migration 019) is written in the SAME INSERT that
        creates the message, deliberately. The alternative — write the message,
        then find it again to attach the trace — is what
        `update_message_citations` below has to do, and it matches on
        session_id + role + exact content text, which mis-keys the moment two
        answers in one session are byte-identical. Passing the payload in
        avoids ever needing that lookup.
        """
        async with get_session() as db:
            db.add(Message(
                session_id=uuid.UUID(str(session_id)), role=role, content=content,
                degradation_trace=degradation_trace,
            ))
            await db.commit()

    async def get_session_history(self, session_id: str) -> list[dict]:
        async with get_session() as db:
            res = await db.execute(select(Message).where(Message.session_id == uuid.UUID(str(session_id))).order_by(Message.created_at))
            return [{"role": m.role, "content": m.content,
                     "degradation_trace": m.degradation_trace,
                     "created_at": m.created_at.isoformat() if m.created_at else None} for m in res.scalars().all()]

    async def update_message_citations(self, session_id: str, response_text: str, unverified: list[str]) -> None:
        async with get_session() as db:
            stmt = select(Message).where(Message.session_id == uuid.UUID(str(session_id)), Message.role == "assistant", Message.content == response_text).order_by(desc(Message.created_at)).limit(1)
            res = await db.execute(stmt)
            msg = res.scalars().first()
            if msg:
                msg.citation_validated = True
                msg.unverified_citations = unverified
                await db.commit()

    # ── Pipeline & Admin Operations ──
    async def create_run(self, session_id: str, user_message: str) -> str:
        async with get_session() as db:
            r = PipelineRun(session_id=uuid.UUID(str(session_id)), original_query=user_message)
            db.add(r)
            await db.commit()
            return str(r.run_id)

    async def update_run(self, run_id: str, **kwargs) -> None:
        if not run_id: return
        allowed = {"rewritten_query", "routed_to", "retry_count", "final_outcome", "total_duration_ms"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates: return
        async with get_session() as db:
            res = await db.execute(select(PipelineRun).where(PipelineRun.run_id == uuid.UUID(str(run_id))))
            r = res.scalars().first()
            if r:
                for k, v in updates.items(): setattr(r, k, v)
                await db.commit()

    # SSE event statuses → Postgres CHECK constraint vocabulary
    _STEP_STATUS_MAP = {"done": "success", "active": "success", "error": "failed", "retry": "retry", "skipped": "skipped"}

    async def log_step(self, run_id: str, step_name: str, step_order: int, status: str,
                       duration_ms: int = None, input_summary: dict = None, output_summary: dict = None) -> None:
        if not run_id:
            return
        async with get_session() as db:
            db.add(PipelineStep(
                run_id=uuid.UUID(str(run_id)), step_name=step_name, step_order=step_order,
                status=self._STEP_STATUS_MAP.get(status, status), duration_ms=duration_ms,
                input_summary=input_summary, output_summary=output_summary,
            ))
            await db.commit()

    async def create_step(self, run_id: str, step_name: str, step_order: int) -> int:
        async with get_session() as db:
            s = PipelineStep(run_id=uuid.UUID(str(run_id)), step_name=step_name, step_order=step_order, status="running")
            db.add(s)
            await db.commit()
            await db.refresh(s)
            return s.step_id

    async def update_step(self, step_id: int, **kwargs) -> None:
        allowed = {"status", "duration_ms", "input_summary", "output_summary"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates: return
        async with get_session() as db:
            res = await db.execute(select(PipelineStep).where(PipelineStep.step_id == step_id))
            s = res.scalars().first()
            if s:
                for k, v in updates.items(): setattr(s, k, v)
                await db.commit()

    async def log_mcp_tool_call(self, run_id: str, mcp_server: str, tool_name: str, status: str, input_params: dict = None, output_summary: dict = None, duration_ms: int = None, rejected_by_role: bool = False) -> None:
        async with get_session() as db:
            tc = McpToolCall(
                run_id=uuid.UUID(str(run_id)), mcp_server=mcp_server, tool_name=tool_name, 
                status=status, input_params=input_params, output_summary=output_summary, 
                duration_ms=duration_ms, rejected_by_role=rejected_by_role
            )
            db.add(tc)
            await db.commit()

    async def log_document(self, doc_id: str, filename: str, doc_type: str = None, chunk_count: int = None, is_global: bool = False, case_id: str = None) -> None:
        async with get_session() as db:
            res = await db.execute(select(Document).where(Document.doc_id == doc_id))
            d = res.scalars().first()
            if d:
                d.filename = filename
                if doc_type is not None: d.doc_type = doc_type
                if chunk_count is not None: d.chunk_count = chunk_count
                d.is_global = is_global
                if case_id is not None: d.case_id = case_id
            else:
                db.add(Document(doc_id=doc_id, filename=filename, doc_type=doc_type, chunk_count=chunk_count, is_global=is_global, case_id=case_id))
            await db.commit()

    async def log_generated_file(self, file_data: dict) -> str:
        # File data will have session_id, user_id, file_type, file_name, file_size_bytes, storage_path
        async with get_session() as db:
            user_id_val = uuid.UUID(file_data["user_id"]) if file_data.get("user_id") else None
            # `generated_files.session_id` is NOT NULL, so a missing session is
            # a caller error — but bare `uuid.UUID(None)` reports it as "one of
            # the hex, bytes, bytes_le, fields, or int arguments must be given",
            # which says nothing about which field was missing or why it
            # mattered. Name it instead. (`user_id` above is guarded for a
            # different reason: it accepts None and stores NULL.)
            raw_session_id = file_data.get("session_id")
            if not raw_session_id:
                raise ValueError(
                    "log_generated_file requires a session_id: generated_files "
                    "records every file against a chat session (NOT NULL)."
                )
            gf = GeneratedFile(
                file_id=uuid.uuid4(),
                session_id=uuid.UUID(raw_session_id),
                user_id=user_id_val,
                case_id=file_data.get("case_id"),
                file_type=file_data["file_type"],
                file_name=file_data["file_name"],
                file_size_bytes=file_data["file_size_bytes"],
                storage_path=file_data["storage_path"]
            )
            db.add(gf)
            await db.commit()
            return str(gf.file_id)

    async def get_generated_file(self, file_id: str) -> Optional[dict]:
        async with get_session() as db:
            res = await db.execute(select(GeneratedFile).where(GeneratedFile.file_id == uuid.UUID(str(file_id))))
            gf = res.scalars().first()
            if gf:
                return {
                    "file_id": str(gf.file_id),
                    "session_id": str(gf.session_id),
                    "user_id": str(gf.user_id),
                    "case_id": gf.case_id,
                    "file_type": gf.file_type,
                    "file_name": gf.file_name,
                    "file_size_bytes": gf.file_size_bytes,
                    "storage_path": gf.storage_path,
                    "created_at": gf.created_at.isoformat() if gf.created_at else None
                }
            return None

    async def get_system_metrics(self) -> dict:
        import asyncio
        from src.retrieval.vector_store import ChromaVectorStore
        total_chunks = await asyncio.to_thread(ChromaVectorStore.get_instance().count)

        async with get_session() as db:
            total_runs = (await db.execute(select(func.count(PipelineRun.run_id)))).scalar() or 0
            routes_res = await db.execute(
                select(PipelineRun.routed_to, func.count(PipelineRun.run_id),
                       func.count(PipelineRun.run_id).filter(PipelineRun.final_outcome.isnot(None), PipelineRun.final_outcome != "safe"))
                .group_by(PipelineRun.routed_to)
            )
            routes = {}
            route_metrics = {}
            for route, count, success in routes_res.all():
                key = route or "unknown"
                routes[key] = count
                rate = (success / count) * 100 if count else 0
                route_metrics[key] = {"count": count, "success_rate": round(rate, 1)}
            avg_duration = int((await db.execute(select(func.avg(PipelineRun.total_duration_ms)))).scalar() or 0)
            files_type_res = await db.execute(select(GeneratedFile.file_type, func.count(GeneratedFile.file_id)).group_by(GeneratedFile.file_type))
            file_types = {row[0]: row[1] for row in files_type_res.all()}
            total_files = (await db.execute(select(func.count(GeneratedFile.file_id)))).scalar() or 0
            total_storage = int((await db.execute(select(func.sum(GeneratedFile.file_size_bytes)))).scalar() or 0)
            total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0
            total_sessions = (await db.execute(select(func.count(Session.session_id)))).scalar() or 0
            total_docs = (await db.execute(select(func.count(Document.doc_id)))).scalar() or 0
            total_mcp = (await db.execute(select(func.count(McpToolCall.call_id)))).scalar() or 0
            table_stats = [
                {"table": "chroma_chunks", "count": total_chunks},
                {"table": "documents", "count": total_docs},
                {"table": "pipeline_runs", "count": total_runs},
                {"table": "sessions", "count": total_sessions},
                {"table": "users", "count": total_users},
                {"table": "mcp_tool_calls", "count": total_mcp},
                {"table": "generated_files", "count": total_files},
            ]
            return {
                "total_runs": total_runs, "total_users": total_users, "total_sessions": total_sessions,
                "total_files": total_files, "total_storage_bytes": total_storage, "routes": routes,
                "route_metrics": route_metrics, "file_types": file_types,
                "avg_duration_ms": avg_duration, "table_stats": table_stats
            }

    async def get_runs(self, limit: int = 50, offset: int = 0, route_filter: str = None) -> list[dict]:
        async with get_session() as db:
            q = select(PipelineRun).order_by(desc(PipelineRun.created_at)).limit(limit).offset(offset)
            if route_filter: q = q.where(PipelineRun.routed_to == route_filter.upper())
            res = await db.execute(q)
            return [{"run_id": str(r.run_id), "original_query": r.original_query, "rewritten_query": r.rewritten_query, "routed_to": r.routed_to, "final_outcome": r.final_outcome, "retry_count": r.retry_count, "total_duration_ms": r.total_duration_ms, "created_at": r.created_at.isoformat() if r.created_at else None} for r in res.scalars().all()]

    async def get_run_steps(self, run_id: str) -> list[dict]:
        async with get_session() as db:
            res = await db.execute(select(PipelineStep).where(PipelineStep.run_id == uuid.UUID(str(run_id))).order_by(PipelineStep.step_order))
            return [{"step_id": s.step_id, "step_name": s.step_name, "step_order": s.step_order, "status": s.status, "duration_ms": s.duration_ms, "input_summary": s.input_summary, "output_summary": s.output_summary, "created_at": s.created_at.isoformat() if s.created_at else None} for s in res.scalars().all()]

    async def get_generated_files(self, limit: int = 50, offset: int = 0) -> list[dict]:
        async with get_session() as db:
            res = await db.execute(select(GeneratedFile).order_by(desc(GeneratedFile.created_at)).limit(limit).offset(offset))
            return [{"file_id": str(f.file_id), "file_name": f.file_name, "file_type": f.file_type, "storage_path": f.storage_path, "file_size_bytes": f.file_size_bytes, "created_at": f.created_at.isoformat() if f.created_at else None} for f in res.scalars().all()]

    async def delete_generated_file(self, file_id: str) -> Optional[dict]:
        async with get_session() as db:
            f = await db.get(GeneratedFile, uuid.UUID(str(file_id)))
            if f:
                record = {
                    "file_id": str(f.file_id),
                    "file_name": f.file_name,
                    "file_type": f.file_type,
                    "storage_path": f.storage_path,
                }
                await db.delete(f)
                await db.commit()
                return record
        return None

    async def get_mcp_calls(self, limit: int = 50, offset: int = 0) -> list[dict]:
        from src.database.models import McpToolCall
        from sqlalchemy.orm import joinedload
        async with get_session() as db:
            # The model has created_at / duration_ms / output_summary — not
            # started_at / completed_at / error_message. Referencing the latter
            # raised AttributeError on every call.
            res = await db.execute(
                select(McpToolCall)
                .options(joinedload(McpToolCall.run))
                .order_by(desc(McpToolCall.created_at))
                .limit(limit)
                .offset(offset)
            )
            return [{
                "call_id": str(c.call_id),
                "run_id": str(c.run_id),
                "mcp_server": c.mcp_server,
                "tool_name": c.tool_name,
                "input_params": c.input_params,
                "output_summary": c.output_summary,
                "status": c.status,
                "duration_ms": c.duration_ms,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "run": {"original_query": c.run.original_query} if c.run else None
            } for c in res.scalars().unique().all()]

    async def get_ingested_files_summary(self, project_id: str = None) -> list[dict]:
        async with get_session() as db:
            if project_id:
                from sqlalchemy import or_
                res = await db.execute(
                    select(Document)
                    .where(or_(Document.project_id == project_id, Document.is_global == True))
                    .order_by(desc(Document.ingested_at))
                )
            else:
                res = await db.execute(select(Document).order_by(desc(Document.ingested_at)))
            return [{"doc_id": str(d.doc_id), "filename": d.filename, "doc_type": d.doc_type, "chunk_count": d.chunk_count, "is_global": d.is_global, "ingested_at": d.ingested_at.isoformat() if d.ingested_at else None} for d in res.scalars().all()]

    async def delete_ingested_file(self, doc_id: str) -> None:
        async with get_session() as db:
            res = await db.execute(select(Document).where(Document.doc_id == doc_id))
            d = res.scalars().first()
            if d:
                await db.delete(d)
                await db.commit()

    # ── Project Operations ──
    @staticmethod
    def _project_to_dict(p: Project) -> dict:
        return {
            "id": str(p.id), "user_id": str(p.user_id), "name": p.name,
            "description": p.description, "domain_context": p.domain_context,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }

    async def get_project(self, project_id: str) -> Optional[dict]:
        async with get_session() as db:
            res = await db.execute(select(Project).where(Project.id == uuid.UUID(str(project_id))))
            p = res.scalars().first()
            return self._project_to_dict(p) if p else None

    async def get_projects_for_user(self, user_id: str) -> list[dict]:
        async with get_session() as db:
            res = await db.execute(select(Project).where(Project.user_id == uuid.UUID(str(user_id))).order_by(desc(Project.created_at)))
            return [self._project_to_dict(p) for p in res.scalars().all()]

    async def create_project(self, data: dict) -> Optional[dict]:
        async with get_session() as db:
            p = Project(
                user_id=uuid.UUID(str(data["user_id"])),
                name=data["name"],
                description=data.get("description"),
                domain_context=data.get("domain_context"),
            )
            db.add(p)
            await db.commit()
            await db.refresh(p)
            return self._project_to_dict(p)

    async def update_project(self, project_id: str, data: dict) -> Optional[dict]:
        allowed = {"name", "description", "domain_context"}
        async with get_session() as db:
            res = await db.execute(select(Project).where(Project.id == uuid.UUID(str(project_id))))
            p = res.scalars().first()
            if not p:
                return None
            for k, v in data.items():
                if k in allowed:
                    setattr(p, k, v)
            await db.commit()
            await db.refresh(p)
            return self._project_to_dict(p)

    async def delete_project(self, project_id: str) -> None:
        async with get_session() as db:
            res = await db.execute(select(Project).where(Project.id == uuid.UUID(str(project_id))))
            p = res.scalars().first()
            if p:
                await db.delete(p)
                await db.commit()

    async def get_project_context(self, project_id: str) -> Optional[str]:
        async with get_session() as db:
            res = await db.execute(select(Project.domain_context).where(Project.id == uuid.UUID(str(project_id))))
            row = res.first()
            return row[0] if row else None

    async def get_project_memory(self, project_id: str) -> Optional[dict]:
        async with get_session() as db:
            res = await db.execute(select(ProjectMemory).where(ProjectMemory.project_id == uuid.UUID(str(project_id))).limit(1))
            m = res.scalars().first()
            return {"id": str(m.id), "project_id": str(m.project_id), "summary_text": m.summary_text} if m else None

    async def upsert_project_memory(self, project_id: str, summary_text: str) -> None:
        async with get_session() as db:
            res = await db.execute(select(ProjectMemory).where(ProjectMemory.project_id == uuid.UUID(str(project_id))).limit(1))
            m = res.scalars().first()
            if m:
                m.summary_text = summary_text
            else:
                db.add(ProjectMemory(project_id=uuid.UUID(str(project_id)), summary_text=summary_text))
            await db.commit()

    async def delete_document_records(self, source_file: str) -> None:
        async with get_session() as db:
            res = await db.execute(select(Document).where(Document.filename == source_file))
            for d in res.scalars().all():
                await db.delete(d)
            await db.commit()

    # ── Case Operations ──
    @staticmethod
    def _case_to_dict(c: Case) -> dict:
        return {
            "case_id": c.case_id,
            "fir_number": c.fir_number,
            "crime_category": c.crime_category,
            "investigation_officer": c.investigation_officer,
            "police_station": c.police_station,
            "incident_date": c.incident_date.isoformat() if c.incident_date else None,
            "investigation_status": c.investigation_status,
            "location": c.location,
            "description": c.description,
            "victim_info": c.victim_info,
            "suspect_info": c.suspect_info,
            # Migration 019. NULL/None means no completed conflict detection is
            # on record — NOT "no conflicts found".
            "conflicts_checked_at": (
                c.conflicts_checked_at.isoformat() if c.conflicts_checked_at else None
            ),
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            # [Reconciliation fix — Unit 6] Read by
            # timeline_building.py's conflict-detection-completion check.
            "conflicts_checked_at": (
                c.conflicts_checked_at.isoformat() if c.conflicts_checked_at else None
            ),
        }

    async def get_case(self, case_id: str) -> Optional[dict]:
        async with get_session() as db:
            res = await db.execute(select(Case).where(Case.case_id == case_id))
            c = res.scalars().first()
            return self._case_to_dict(c) if c else None

    async def get_cases(self, user_id: str = None, user_role: str = None) -> list[dict]:
        # RBAC: Platform admins see all cases. Others see only assigned cases.
        async with get_session() as db:
            if user_role == "platform-admin":
                res = await db.execute(select(Case).order_by(desc(Case.created_at)))
            else:
                from src.database.models import CaseAssignment
                res = await db.execute(
                    select(Case).join(CaseAssignment).where(
                        CaseAssignment.user_id == uuid.UUID(str(user_id))
                    ).order_by(desc(Case.created_at))
                )
            return [self._case_to_dict(c) for c in res.scalars().all()]

    async def check_case_access(self, case_id: str, user_id: str, user_role: str, min_role: str = None) -> bool:
        if user_role == "platform-admin":
            return True
        from src.database.models import CaseAssignment
        async with get_session() as db:
            res = await db.execute(select(CaseAssignment).where(
                CaseAssignment.case_id == case_id,
                CaseAssignment.user_id == uuid.UUID(str(user_id))
            ))
            assignment = res.scalars().first()
            if assignment is None:
                return False
            if min_role is None:
                return True
            # Per-case role, NOT the caller's global role — a global
            # supervisor assigned to this specific case as "investigator"
            # must not get supervisor-level destructive access here.
            roles = ["investigator", "supervisor", "station-admin", "platform-admin"]
            try:
                return roles.index(assignment.role) >= roles.index(min_role)
            except ValueError:
                return False

    async def get_case_assignments(self, case_id: str) -> list[dict]:
        from src.database.models import CaseAssignment
        async with get_session() as db:
            res = await db.execute(
                select(CaseAssignment, User.email)
                .join(User, User.id == CaseAssignment.user_id)
                .where(CaseAssignment.case_id == case_id)
            )
            return [
                {"user_id": str(a.user_id), "email": email, "role": a.role}
                for a, email in res.all()
            ]

    async def assign_user_to_case(self, case_id: str, user_id: str, role: str) -> None:
        from src.database.models import CaseAssignment
        async with get_session() as db:
            res = await db.execute(select(CaseAssignment).where(
                CaseAssignment.case_id == case_id, CaseAssignment.user_id == uuid.UUID(str(user_id))
            ))
            a = res.scalars().first()
            if a:
                a.role = role
            else:
                db.add(CaseAssignment(case_id=case_id, user_id=uuid.UUID(str(user_id)), role=role))
            await db.commit()

    async def unassign_user_from_case(self, case_id: str, user_id: str) -> None:
        from src.database.models import CaseAssignment
        async with get_session() as db:
            res = await db.execute(select(CaseAssignment).where(
                CaseAssignment.case_id == case_id, CaseAssignment.user_id == uuid.UUID(str(user_id))
            ))
            a = res.scalars().first()
            if a:
                await db.delete(a)
                await db.commit()

    async def create_case(self, data: dict) -> Optional[dict]:
        async with get_session() as db:
            c = Case(
                case_id=data["case_id"],
                fir_number=data.get("fir_number"),
                crime_category=data.get("crime_category"),
                investigation_officer=data.get("investigation_officer"),
                police_station=data.get("police_station"),
                incident_date=data.get("incident_date"),
                investigation_status=data.get("investigation_status"),
                location=data.get("location"),
                description=data.get("description"),
                victim_info=data.get("victim_info"),
                suspect_info=data.get("suspect_info"),
            )
            db.add(c)
            await db.commit()
            await db.refresh(c)
            return self._case_to_dict(c)

    async def mark_conflicts_checked(self, case_id: str) -> None:
        """
        Record that conflict detection COMPLETED for this case (migration 019).

        Deliberately its own method rather than a new key in `update_case`'s
        allowlist: that allowlist is user-editable case data, and this is a
        system-written fact about a background job. Widening it would let an
        API caller assert that a check happened.

        Called by the background task ON RETURN, never at schedule time — a
        query racing an in-flight detection must still find no marker and read
        UNKNOWN.
        """
        from datetime import datetime as _dt

        async with get_session() as db:
            res = await db.execute(select(Case).where(Case.case_id == case_id))
            c = res.scalars().first()
            if c:
                c.conflicts_checked_at = _dt.utcnow()
                await db.commit()

    async def update_case(self, case_id: str, data: dict) -> Optional[dict]:
        allowed = {
            "fir_number", "crime_category", "investigation_officer", "police_station",
            "incident_date", "investigation_status", "location", "description",
            "victim_info", "suspect_info",
        }
        async with get_session() as db:
            res = await db.execute(select(Case).where(Case.case_id == case_id))
            c = res.scalars().first()
            if not c:
                return None
            for k, v in data.items():
                if k in allowed:
                    setattr(c, k, v)
            c.updated_at = datetime.utcnow()
            await db.commit()
            await db.refresh(c)
            return self._case_to_dict(c)

    async def delete_case(self, case_id: str) -> None:
        async with get_session() as db:
            res = await db.execute(select(Case).where(Case.case_id == case_id))
            c = res.scalars().first()
            if c:
                await db.delete(c)
                await db.commit()

    async def mark_conflicts_checked(self, case_id: str) -> None:
        """[Reconciliation fix — harness-reconciliation Unit 6, migration
        018] Called only by src/ingestion/conflict_bg.py after
        detect_conflicts() returns without raising — see that column's own
        comment in models.py for the full rationale. Silently no-ops on an
        unknown case_id (the case may have been deleted between scheduling
        and completion) rather than raising — this is best-effort
        observability, not a step whose failure should surface anywhere."""
        async with get_session() as db:
            res = await db.execute(select(Case).where(Case.case_id == case_id))
            c = res.scalars().first()
            if not c:
                return
            c.conflicts_checked_at = datetime.utcnow()
            await db.commit()

    # ── Vector Store Operations ──
    async def insert_documents(self, documents: list[dict]) -> None:
        from sqlalchemy import text
        if not documents: return
        async with engine.begin() as conn:
            for doc in documents:
                if "project_id" not in doc:
                    doc["project_id"] = None
                if "case_id" not in doc:
                    doc["case_id"] = None
                await conn.execute(text("""
                    INSERT INTO documents (doc_id, filename, doc_type, is_global, project_id, case_id)
                    VALUES (:doc_id, :filename, :doc_type, :is_global, :project_id, :case_id)
                    ON CONFLICT (doc_id) DO UPDATE SET
                        filename = EXCLUDED.filename,
                        case_id = EXCLUDED.case_id,
                        project_id = EXCLUDED.project_id,
                        is_global = EXCLUDED.is_global,
                        doc_type = EXCLUDED.doc_type
                """), doc)

    async def _query_police_reference_data_exact(
        self,
        category: Optional[str] = None,
        subject: Optional[str] = None,
        section_ref: Optional[str] = None,
    ) -> list[dict]:
        """
        Single exact-match query: every non-null filter is ANDed together
        with ILIKE. A query with no filters returns nothing (mirrors the
        old tax_rates branch, which always supplied at least one ILIKE
        clause before querying). No relaxation here — see
        query_police_reference_data() for the retry loop that calls this.
        """
        conditions = []
        if category:
            conditions.append(PoliceReferenceData.category.ilike(f"%{category}%"))
        if subject:
            conditions.append(PoliceReferenceData.subject.ilike(f"%{subject}%"))
        if section_ref:
            conditions.append(PoliceReferenceData.section_ref.ilike(f"%{section_ref}%"))

        if not conditions:
            return []

        async with get_session() as db:
            stmt = select(PoliceReferenceData).where(*conditions)
            rows = (await db.execute(stmt)).scalars().all()
            return [
                {
                    "ref_id": str(r.ref_id),
                    "category": r.category,
                    "subject": r.subject,
                    "description": r.description,
                    "fine_amount": float(r.fine_amount) if r.fine_amount is not None else None,
                    "section_ref": r.section_ref,
                    "source_document": r.source_document,
                    "source_type": r.source_type,
                    "effective_from": r.effective_from.isoformat() if r.effective_from else None,
                }
                for r in rows
            ]

    # Weakest, most free-text/variable signal dropped first on a 0-row
    # exact match; section_ref (never listed here) is the strongest signal
    # and is kept as long as possible.
    _RELAX_DROP_ORDER = ("subject", "category")

    async def query_police_reference_data(
        self,
        category: Optional[str] = None,
        subject: Optional[str] = None,
        section_ref: Optional[str] = None,
    ) -> list[dict]:
        """
        Direct parameterized fast path for the SQL route (Phase 3).

        Every filter is optional and combined with AND (see
        _query_police_reference_data_exact). Because an extracted filter
        can only narrow the result set, a more precisely-worded question
        can zero-match data that a vaguer question about the same fact
        would have found (findings.md Module 5). If the full-AND query
        returns no rows, progressively relax by dropping one filter at a
        time (subject first, then category — section_ref is never
        dropped), down to a single remaining filter, before giving up.
        Bounded at 2 extra queries. A query whose full filter set already
        matches returns from the first call below, unchanged from before
        this retry loop existed.
        """
        filters = {}
        if category:
            filters["category"] = category
        if subject:
            filters["subject"] = subject
        if section_ref:
            filters["section_ref"] = section_ref

        if not filters:
            return []

        rows = await self._query_police_reference_data_exact(**filters)
        if rows:
            return rows

        relaxed = dict(filters)
        for field in self._RELAX_DROP_ORDER:
            if len(relaxed) <= 1:
                break  # never relax below a single filter
            if field not in relaxed:
                continue  # nothing to drop for this field this round
            del relaxed[field]
            rows = await self._query_police_reference_data_exact(**relaxed)
            if rows:
                return rows

        return []

    # ══════════════════════════════════════════════════════════════════
    # Admin dashboard: errors, ingestion jobs, KB stats, usage, latency
    # (mirrors RestGateway — both backends are live, so both must exist)
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _missing_table(exc: Exception) -> bool:
        return "does not exist" in str(exc).lower()

    @staticmethod
    def _naive_utc(since: str) -> datetime:
        """
        Parse an ISO cutoff to a naive UTC datetime.

        The pipeline_runs / pipeline_steps / error_logs timestamp columns are
        `timestamp without time zone`, so binding a tz-aware datetime raises
        "can't subtract offset-naive and offset-aware datetimes" under asyncpg.
        """
        dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    async def table_exists(self, table: str) -> bool:
        """Does this table/view exist? (See RestGateway.table_exists for why.)"""
        from sqlalchemy import text
        async with get_session() as db:
            res = await db.execute(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table}"}
            )
            return bool(res.scalar())

    async def log_error(self, record: dict) -> None:
        from src.database.models import ErrorLog
        try:
            async with get_session() as db:
                db.add(ErrorLog(
                    severity=record.get("severity", "error"),
                    error_type=record.get("error_type"),
                    module=record.get("module"),
                    message=record.get("message", ""),
                    stack_trace=record.get("stack_trace"),
                    run_id=uuid.UUID(record["run_id"]) if record.get("run_id") else None,
                    session_id=uuid.UUID(record["session_id"]) if record.get("session_id") else None,
                    user_id=uuid.UUID(record["user_id"]) if record.get("user_id") else None,
                    context=record.get("context"),
                ))
                await db.commit()
        except Exception:
            pass  # error logging must never raise

    async def log_audit_event(self, event_type: str, details: dict, user_id: str = None, case_id: str = None) -> None:
        from src.database.models import AuditLog
        try:
            async with get_session() as db:
                db.add(AuditLog(
                    event_type=event_type,
                    user_id=uuid.UUID(str(user_id)) if user_id else None,
                    case_id=case_id,
                    details=details
                ))
                await db.commit()
        except Exception as exc:
            if not self._missing_table(exc):
                logger.error(f"Audit log failed: {exc}")

    async def get_audit_logs(self, limit: int = 100, offset: int = 0, event_type: str = None, case_id: str = None, user_id: str = None, since: str = None) -> list[dict]:
        from src.database.models import AuditLog
        try:
            async with get_session() as db:
                q = select(AuditLog)
                if event_type:
                    q = q.where(AuditLog.event_type == event_type)
                if case_id:
                    q = q.where(AuditLog.case_id == case_id)
                if user_id:
                    q = q.where(AuditLog.user_id == uuid.UUID(str(user_id)))
                if since:
                    q = q.where(AuditLog.timestamp >= self._naive_utc(since))
                res = await db.execute(q.order_by(desc(AuditLog.timestamp)).limit(limit).offset(offset))
                return [{
                    "log_id": str(log.log_id),
                    "timestamp": log.timestamp.isoformat(),
                    "event_type": log.event_type,
                    "user_id": str(log.user_id) if log.user_id else None,
                    "case_id": log.case_id,
                    "details": log.details,
                } for log in res.scalars().all()]
        except Exception as exc:
            if not self._missing_table(exc):
                logger.error(f"Failed to get audit logs: {exc}")
            return []

    async def get_errors(self, limit: int = 100, offset: int = 0, severity: str = None,
                         module: str = None, error_type: str = None,
                         since: str = None) -> list[dict]:
        from src.database.models import ErrorLog
        from datetime import datetime as _dt
        try:
            async with get_session() as db:
                q = select(ErrorLog)
                if severity:
                    q = q.where(ErrorLog.severity == severity)
                if module:
                    q = q.where(ErrorLog.module == module)
                if error_type:
                    q = q.where(ErrorLog.error_type == error_type)
                if since:
                    q = q.where(ErrorLog.occurred_at >= self._naive_utc(since))
                res = await db.execute(q.order_by(desc(ErrorLog.occurred_at)).limit(limit).offset(offset))
                return [{
                    "error_id": str(e.error_id),
                    "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
                    "severity": e.severity, "error_type": e.error_type, "module": e.module,
                    "message": e.message, "stack_trace": e.stack_trace,
                    "run_id": str(e.run_id) if e.run_id else None,
                    "session_id": str(e.session_id) if e.session_id else None,
                } for e in res.scalars().all()]
        except Exception as exc:
            if self._missing_table(exc):
                return []
            raise

    async def get_error_facets(self) -> dict:
        from src.database.models import ErrorLog
        try:
            async with get_session() as db:
                mods = (await db.execute(select(ErrorLog.module).distinct())).scalars().all()
                types = (await db.execute(select(ErrorLog.error_type).distinct())).scalars().all()
                sev = (await db.execute(select(ErrorLog.severity).distinct())).scalars().all()
                return {
                    "modules": sorted([m for m in mods if m]),
                    "error_types": sorted([t for t in types if t]),
                    "severities": sorted([s for s in sev if s]),
                }
        except Exception as exc:
            if self._missing_table(exc):
                return {"modules": [], "error_types": [], "severities": []}
            raise

    async def get_errors_since(self, since: str, max_rows: int = ANALYTICS_MAX_ROWS) -> list[dict]:
        from src.database.models import ErrorLog
        from datetime import datetime as _dt
        try:
            async with get_session() as db:
                res = await db.execute(
                    select(ErrorLog.occurred_at, ErrorLog.severity)
                    .where(ErrorLog.occurred_at >= self._naive_utc(since))
                    .order_by(desc(ErrorLog.occurred_at))
                    .limit(max_rows)
                )
                return [{"occurred_at": r[0].isoformat() if r[0] else None, "severity": r[1]} for r in res.all()]
        except Exception as exc:
            if self._missing_table(exc):
                return []
            raise

    # ── Ingestion jobs ──
    async def create_ingestion_job(self, data: dict) -> str:
        from src.database.models import IngestionJob
        job_id = uuid.uuid4()
        try:
            async with get_session() as db:
                db.add(IngestionJob(
                    job_id=job_id,
                    filename=data["filename"],
                    file_type=data.get("file_type"),
                    file_size_bytes=data.get("file_size_bytes"),
                    status=data.get("status", "processing"),
                    uploaded_by=uuid.UUID(data["uploaded_by"]) if data.get("uploaded_by") else None,
                ))
                await db.commit()
        except Exception as exc:
            if not self._missing_table(exc):
                raise
        return str(job_id)

    async def update_ingestion_job(self, job_id: str, data: dict) -> None:
        from src.database.models import IngestionJob
        from datetime import datetime as _dt
        allowed = {"doc_id", "status", "chunks_added", "error_message", "duration_ms"}
        try:
            async with get_session() as db:
                res = await db.execute(select(IngestionJob).where(IngestionJob.job_id == uuid.UUID(str(job_id))))
                job = res.scalars().first()
                if not job:
                    return
                for k, v in data.items():
                    if k in allowed:
                        setattr(job, k, v)
                if data.get("status") in ("success", "failed"):
                    job.finished_at = _dt.utcnow()
                await db.commit()
        except Exception as exc:
            if not self._missing_table(exc):
                raise

    async def update_ingestion_job_by_doc(self, doc_id: str, data: dict) -> None:
        from src.database.models import IngestionJob
        from datetime import datetime as _dt
        allowed = {"status", "error_message"}
        try:
            async with get_session() as db:
                res = await db.execute(select(IngestionJob).where(IngestionJob.doc_id == str(doc_id)))
                job = res.scalars().first()
                if not job:
                    return
                # Only update if job isn't already failed by something else,
                # or if we are marking it as failed.
                if data.get("status") == "success" and job.status == "failed":
                    data.pop("status", None)
                    
                for k, v in data.items():
                    if k in allowed:
                        setattr(job, k, v)
                if data.get("status") in ("success", "failed"):
                    job.finished_at = _dt.utcnow()
                await db.commit()
        except Exception as exc:
            if not self._missing_table(exc):
                raise

    async def get_ingestion_jobs(self, limit: int = 50, offset: int = 0) -> list[dict]:
        from src.database.models import IngestionJob
        try:
            async with get_session() as db:
                res = await db.execute(
                    select(IngestionJob).order_by(desc(IngestionJob.started_at)).limit(limit).offset(offset)
                )
                return [{
                    "job_id": str(j.job_id), "doc_id": j.doc_id, "filename": j.filename,
                    "file_type": j.file_type, "file_size_bytes": j.file_size_bytes,
                    "status": j.status, "chunks_added": j.chunks_added,
                    "error_message": j.error_message, "duration_ms": j.duration_ms,
                    "started_at": j.started_at.isoformat() if j.started_at else None,
                    "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                } for j in res.scalars().all()]
        except Exception as exc:
            if self._missing_table(exc):
                return []
            raise

    # ── Knowledge base stats ──
    async def get_kb_stats(self) -> dict:
        """
        Chunks indexed, and how they are distributed across documents.

        Chunk counts come from Chroma (Phase 1) grouped by metadata.source —
        the identity a human recognises and the one retrieval cites — rather
        than the removed document_chunks table (see Phase 4 Step 0).
        doc_type/ingested_at still come from the relational `documents`
        table (Postgres, unchanged).
        """
        import asyncio
        from collections import Counter
        from src.retrieval.vector_store import ChromaVectorStore

        store = ChromaVectorStore.get_instance()
        all_metadata = await asyncio.to_thread(store.get_all_metadata)
        counts_by_source = Counter(m.get("source", "unknown") for m in all_metadata)
        total_chunks = len(all_metadata)

        async with get_session() as db:
            doc_res = await db.execute(select(Document))
            doc_by_filename = {d.filename: d for d in doc_res.scalars().all()}

        total_documents, docs = _kb_stats_documents(counts_by_source, doc_by_filename)

        return {
            "total_chunks": total_chunks,
            "total_documents": total_documents,
            "documents": docs,
            "grouped_by": "source_file",
        }

    # ── Usage / routing / latency ──
    async def get_runs_since(self, since: str, max_rows: int = ANALYTICS_MAX_ROWS) -> list[dict]:
        from datetime import datetime as _dt
        async with get_session() as db:
            res = await db.execute(
                select(PipelineRun)
                .where(PipelineRun.created_at >= self._naive_utc(since))
                .order_by(desc(PipelineRun.created_at))
                .limit(max_rows)
            )
            return [{
                "run_id": str(r.run_id), "routed_to": r.routed_to, "final_outcome": r.final_outcome,
                "total_duration_ms": r.total_duration_ms,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            } for r in res.scalars().all()]

    async def get_step_latencies_since(self, since: str, max_rows: int = ANALYTICS_MAX_ROWS) -> list[dict]:
        from datetime import datetime as _dt
        async with get_session() as db:
            res = await db.execute(
                select(PipelineStep.step_name, PipelineStep.duration_ms, PipelineStep.status,
                       PipelineStep.created_at)
                .where(PipelineStep.created_at >= self._naive_utc(since))
                .order_by(desc(PipelineStep.created_at))
                .limit(max_rows)
            )
            return [{
                "step_name": r[0], "duration_ms": r[1], "status": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
            } for r in res.all()]

    # ── Session attachments (per-conversation, NOT the knowledge base) ──
    @staticmethod
    def _attachment_to_dict(a, include_text: bool = False) -> dict:
        d = {
            "attachment_id": str(a.attachment_id), "session_id": str(a.session_id),
            "user_id": str(a.user_id) if a.user_id else None,
            "filename": a.filename, "file_type": a.file_type,
            "file_size_bytes": a.file_size_bytes, "char_count": a.char_count,
            "status": a.status, "error_message": a.error_message,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        if include_text:
            d["extracted_text"] = a.extracted_text
        return d

    async def create_attachment(self, data: dict) -> dict:
        from src.database.models import SessionAttachment
        async with get_session() as db:
            a = SessionAttachment(
                session_id=uuid.UUID(str(data["session_id"])),
                user_id=uuid.UUID(str(data["user_id"])) if data.get("user_id") else None,
                filename=data["filename"], file_type=data.get("file_type"),
                file_size_bytes=data.get("file_size_bytes"),
                extracted_text=data.get("extracted_text"),
                char_count=data.get("char_count"),
                status=data.get("status", "ready"),
                error_message=data.get("error_message"),
            )
            db.add(a)
            await db.commit()
            await db.refresh(a)
            return self._attachment_to_dict(a)

    async def get_attachments_for_session(self, session_id: str, include_text: bool = False) -> list[dict]:
        from src.database.models import SessionAttachment
        try:
            async with get_session() as db:
                res = await db.execute(
                    select(SessionAttachment)
                    .where(SessionAttachment.session_id == uuid.UUID(str(session_id)))
                    .order_by(SessionAttachment.created_at)
                )
                return [self._attachment_to_dict(a, include_text) for a in res.scalars().all()]
        except Exception as exc:
            if self._missing_table(exc):
                return []
            raise

    async def get_attachment(self, attachment_id: str) -> Optional[dict]:
        from src.database.models import SessionAttachment
        async with get_session() as db:
            res = await db.execute(
                select(SessionAttachment)
                .where(SessionAttachment.attachment_id == uuid.UUID(str(attachment_id)))
            )
            a = res.scalars().first()
            return self._attachment_to_dict(a, include_text=True) if a else None

    async def delete_attachment(self, attachment_id: str) -> None:
        from src.database.models import SessionAttachment
        async with get_session() as db:
            res = await db.execute(
                select(SessionAttachment)
                .where(SessionAttachment.attachment_id == uuid.UUID(str(attachment_id)))
            )
            a = res.scalars().first()
            if a:
                await db.delete(a)
                await db.commit()
