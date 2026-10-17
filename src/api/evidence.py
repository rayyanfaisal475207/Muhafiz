"""Run recovery, cancellation, source access and artifact downloads."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.auth.routes import get_current_user, limiter
from src.data_gateway import get_gateway
from src.evidence import database
from src.evidence.conversation import prepare
from src.evidence.service import get_run
from src.evidence.types import Scope

router = APIRouter(prefix="/api/evidence",tags=["Evidence"])


class ExistingConversation(BaseModel):
    session_id: str
    message: str = ""
    case_id: str | None = None
    project_id: str | None = None


async def sources_accessible(scope, response, attachments):
    """Saved citations do not grant access after a document or attachment is revoked."""
    from src.evidence.ingestion import stable_id
    attached = {str(stable_id('attachment',item['attachment_id'])):item.get('extracted_text') or ''
                for item in attachments}
    try:
        citations = {span['span_id']:span for result in (response or {}).get('results',[])
                     for span in result.get('citations',[])}
        for span in citations.values():
            UUID(span['span_id']); UUID(span['document_id'])
            if (not isinstance(span['start'],int) or not isinstance(span['end'],int)
                    or span['start']<0 or span['end']<=span['start'] or not isinstance(span['text'],str)):
                return False
    except (KeyError,TypeError,ValueError,AttributeError):return False
    database_ids = []
    for span in citations.values():
        text = attached.get(span['document_id'])
        if text is not None:
            if text[span['start']:span['end']] != span['text']: return False
        else:
            try: database_ids.append(UUID(span['span_id']))
            except (TypeError,ValueError): return False
    if not database_ids: return True
    async with database.connection(scope) as conn:
        rows = await conn.fetch('''SELECT s.span_id,s.document_id,s.char_start AS start_offset,s.char_end AS end_offset,
            substring(d.content FROM s.char_start+1 FOR s.char_end-s.char_start) AS text,
            d.source_metadata
            FROM evidence.spans s JOIN evidence.documents d USING(document_id)
            WHERE s.span_id=ANY($1::uuid[])''',database_ids)
    if len(rows) != len(database_ids): return False
    for row in rows:
        span = citations[str(row['span_id'])]
        metadata=row.get('source_metadata') or {}
        if metadata.get('endpoint')=='session_attachments':
            current=next((a for a in attachments if str(a['attachment_id'])==metadata.get('attachment_id')),None)
            if not current or (current.get('extracted_text') or '')[span['start']:span['end']] != span['text']:
                return False
        if (str(row['document_id']) != span['document_id'] or row['start_offset'] != span['start']
                or row['end_offset'] != span['end'] or row['text'] != span['text']): return False
    return True


@router.get("/sessions/{session_id}/runs")
async def session_runs(session_id: UUID,current_user=Depends(get_current_user)):
    prepared=await prepare(await get_gateway(),current_user,ExistingConversation(session_id=str(session_id)),create=False)
    async with database.connection(prepared.scope) as conn:
        rows=await conn.fetch("SELECT run_id,scope,question,status,response,created_at FROM evidence.runs WHERE session_id=$1 ORDER BY created_at",session_id)
    result=[]
    for row in rows:
        scope=Scope.model_validate(row["scope"])
        if (not set(scope.case_ids)<=set(prepared.scope.case_ids) or scope.project_id!=prepared.scope.project_id
                or scope.include_global and not prepared.scope.include_global):
            continue
        if not await sources_accessible(prepared.scope,row['response'],prepared.attachments): continue
        result.append({k:row[k] for k in ("run_id","question","status","response","created_at")})
    return {"runs":result}


async def authorized_run(run_id: UUID,user):
    # Run rows are owner-scoped; source access is rechecked against CURRENT
    # assignments before a persisted answer or artifact can be returned.
    initial = Scope(user_id=user.id,session_id=run_id)
    async with database.connection(initial) as conn:
        row = await conn.fetchrow("SELECT session_id FROM evidence.runs WHERE run_id=$1",run_id)
    if row is None:
        raise HTTPException(404,"Run not found")
    prepared = await prepare(await get_gateway(),user,ExistingConversation(session_id=str(row["session_id"])),create=False)
    result = await get_run(prepared.scope,run_id)
    if not await sources_accessible(prepared.scope,result.get('response'),prepared.attachments):
        raise HTTPException(403,'Access to a cited source has changed')
    return prepared.scope,result


@router.get("/runs/{run_id}")
async def read_run(run_id: UUID,current_user=Depends(get_current_user)):
    scope,row = await authorized_run(run_id,current_user)
    result = {key:row.get(key) for key in ("run_id","session_id","status","progress","response","updated_at")}
    async with database.connection(scope) as conn:
        discovery = await conn.fetchrow('''SELECT status,progress FROM evidence.discovery_jobs
            WHERE run_id=$1 ORDER BY updated_at DESC LIMIT 1''',run_id)
    if discovery:
        progress=discovery['progress'] or {}
        result['discovery']={'status':discovery['status'],'completed':len(progress.get('completed',[])),
                             'total':progress.get('total',len(progress.get('population',[])))}
    return result


@router.post("/runs/{run_id}/cancel")
@limiter.limit("30/minute")
async def cancel_run(request: Request,run_id: UUID,current_user=Depends(get_current_user)):
    scope,row = await authorized_run(run_id,current_user)
    async with database.connection(scope,write=True) as conn:
        await conn.execute("""UPDATE evidence.runs SET cancel_requested=true,
          status=CASE WHEN status='pending' THEN 'cancelled' ELSE status END,
          response=CASE WHEN status='pending' THEN jsonb_build_object('run_id',run_id,'status','cancelled','text','Request cancelled.','results','[]'::jsonb) ELSE response END,
          updated_at=now() WHERE run_id=$1 AND status IN ('pending','running')""",run_id)
        await conn.execute("UPDATE evidence.discovery_jobs SET status='cancelled',updated_at=now() WHERE run_id=$1 AND status IN ('pending','running')",run_id)
    return {"run_id":str(run_id),"cancel_requested":True}


@router.get("/artifacts/{artifact_id}")
async def artifact(artifact_id: UUID,current_user=Depends(get_current_user)):
    initial = Scope(user_id=current_user.id,session_id=artifact_id)
    async with database.connection(initial) as conn:
        row = await conn.fetchrow("SELECT * FROM evidence.artifacts WHERE artifact_id=$1",artifact_id)
    if not row:
        raise HTTPException(404,"Artifact not found")
    _,run=await authorized_run(row["run_id"],current_user)
    if (run.get('response') or {}).get('artifact_url') != f'/api/evidence/artifacts/{artifact_id}':
        raise HTTPException(404,'Artifact was not published by this run')
    root = (Path(__file__).resolve().parents[2]/"data"/"generated").resolve()
    path = Path(row["path"]).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404,"Artifact is unavailable")
    return FileResponse(path,filename=f"Muhafiz.{row['format']}")


@router.get("/runs/{run_id}/sources/{span_id}")
async def source(run_id: UUID,span_id: UUID,current_user=Depends(get_current_user)):
    scope,row = await authorized_run(run_id,current_user)
    response = row.get("response") or {}
    # Citation must actually have supported this run, including session-only
    # attachments. Arbitrary span ids are never a separate access surface.
    for result in response.get("results",[]):
        for span in result.get("citations",[]):
            if span.get("span_id") == str(span_id):
                return span
    raise HTTPException(404,"Citation not found for this run")


@router.get("/health")
async def health(current_user=Depends(get_current_user)):
    scope = Scope(user_id=current_user.id,session_id=current_user.id)
    try:
        async with database.connection(scope) as conn:
            version = await conn.fetchval("SELECT to_regclass('evidence.assertions') IS NOT NULL")
        from src import config
        return {"storage_ready":bool(version),"enabled":config.EVIDENCE_RUNTIME_ENABLED,
                "release_ready":False,"model_verified":False,
                "reason":"Release readiness requires a reviewed acceptance report; schema availability alone is insufficient."}
    except Exception:
        return {"ready":False,"reason":"Evidence credentials or migration are unavailable"}
