from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import uuid
import logging
import os

from src.database.models import User
from src.auth.routes import get_current_user
from src.auth.rls_context import cross_case_rls_dependency
from src.data_gateway import get_gateway

logger = logging.getLogger(__name__)
# Phase 2: sessions are looked up by their own session_id, whose case_id
# (if any) is a property of the row, not knowable before the query runs —
# gated by user-ownership checks below, not case membership. RLS is armed
# here but the case dimension is bypassed; see src/auth/rls_context.py.
router = APIRouter(dependencies=[Depends(cross_case_rls_dependency)])

class SessionRenameRequest(BaseModel):
    title: str

@router.get("", response_model=List[Dict[str, Any]])
async def get_user_sessions(
    project_id: Optional[str] = None,
    case_id: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """
    Get all non-deleted sessions for the current user, optionally filtered by
    project and/or case (ANDed together, same composition as retrieval scoping).
    """
    gateway = await get_gateway()
    return await gateway.get_sessions_for_user(current_user.id, project_id, case_id)

@router.get("/{session_id}")
async def get_session_history(session_id: str, current_user: User = Depends(get_current_user)):
    """
    Get the chat history for a specific session, verifying ownership.
    """
    gateway = await get_gateway()
    session_obj = await gateway.get_session(session_id)
    if not session_obj:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session_obj["user_id"] != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to access this session.")
        
    history = await gateway.get_session_history(session_id)
    # New evidence turns have a durable run identity, structured citations and
    # resumable status. Return them separately to avoid guessing message ids.
    evidence_runs = []
    from src import config
    if config.EVIDENCE_RUNTIME_ENABLED:
        from src.api.evidence import session_runs
        evidence_runs = (await session_runs(uuid.UUID(session_id), current_user))["runs"]
    # Opening a conversation counts as using it: bump `updated_at` so the
    # sidebar (ordered by that column) surfaces it at the top, matching how
    # every other chat UI behaves. Best-effort — a failure here must never
    # cost the user the history they asked for.
    try:
        await gateway.touch_session(session_id)
    except Exception as exc:
        logger.warning("Failed to touch session %s on access: %s", session_id, exc)
    # Mirrored evidence messages have a stable identity. Filtering by time
    # would lose legitimate legacy messages created during a rollback.
    # A rejected run must not leak back through its legacy message mirror.
    # Only the separately reauthorized evidence_runs may expose those turns.
    history = [h for h in history if not (h.get("degradation_trace") or {}).get("evidence_run_id")]
    for item in history:
        if (item.get("degradation_trace") or {}).get("evidence_run_id"):
            item["degradation_trace"] = None
    return {"history": history, "evidence_runs": evidence_runs}

@router.delete("/{session_id}")
async def delete_session(session_id: str, current_user: User = Depends(get_current_user)):
    """
    Soft-delete a session. Verifies ownership.
    """
    gateway = await get_gateway()
    session_obj = await gateway.get_session(session_id)
    if not session_obj:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session_obj["user_id"] != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to access this session.")
        
    await gateway.delete_session(session_id)
    return {"message": "Session deleted successfully"}

@router.patch("/{session_id}")
async def rename_session(session_id: str, request: SessionRenameRequest, current_user: User = Depends(get_current_user)):
    """
    Rename a session. Verifies ownership.
    """
    gateway = await get_gateway()
    session_obj = await gateway.get_session(session_id)
    if not session_obj:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session_obj["user_id"] != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to access this session.")
        
    await gateway.update_session_title(session_id, request.title)
    return {"message": "Session renamed successfully", "title": request.title}

@router.get("/{session_id}/export")
async def export_session(
    session_id: str, 
    format: str = "json", 
    current_user: User = Depends(get_current_user)
):
    """
    Export a chat session in md, pdf, or json format.
    """
    gateway = await get_gateway()
    session_obj = await gateway.get_session(session_id)
    if not session_obj:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session_obj["user_id"] != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to access this session.")
        
    safe_history = await get_session_history(session_id,current_user)
    history = list(safe_history['history'])
    for run in safe_history['evidence_runs']:
        if run.get('response'):
            history.extend([{'role':'user','content':run['question'],'created_at':str(run['created_at'])},
                            {'role':'assistant','content':run['response']['text'],'created_at':str(run['created_at'])}])
    history.sort(key=lambda item:str(item.get('created_at','')))

    if format == "json":
        return JSONResponse(content={"session": session_obj, "history": history})
        
    # Prepare payload for md/pdf
    title = session_obj.get("title", f"Chat Export - {session_id[:8]}")
    sections = []
    
    for msg in history:
        role = "User" if msg["role"] == "user" else "Assistant"
        time_str = msg.get("created_at", "")[:16].replace("T", " ")
        heading = f"{role} ({time_str})" if time_str else role
        
        sections.append({
            "heading": heading,
            "paragraphs": [p for p in msg.get("content", "").split("\n") if p.strip()]
        })
        
    payload = {"title": title, "sections": sections}
    
    if format == "pdf":
        from src.generation.pdf_builder import build_pdf

        # build_pdf() dispatches on each section's "type" key
        # ({"type": "heading"|"paragraph"|"table"}) and silently skips
        # anything it does not recognise. The `sections` built above carry no
        # "type" at all -- they use the {"heading", "paragraphs"} shape the
        # Markdown branch below consumes -- so every section fell through the
        # dispatch and the exported PDF contained nothing but its title.
        # Found live: exporting a chat downloaded a blank page.
        #
        # Translate into build_pdf's own schema here rather than changing
        # `sections`, so the Markdown branch keeps the shape it expects.
        pdf_sections: list[dict] = []
        for sec in sections:
            pdf_sections.append({"type": "heading", "level": 2, "content": sec["heading"]})
            for para in sec["paragraphs"]:
                pdf_sections.append({"type": "paragraph", "content": para})

        filepath, _ = build_pdf({"title": title, "sections": pdf_sections})
        return FileResponse(path=filepath, filename=f"{title}.pdf", media_type="application/pdf")
        
    elif format == "md":
        # Generate markdown string
        lines = [f"# {title}\n"]
        for sec in sections:
            lines.append(f"### {sec['heading']}")
            for p in sec['paragraphs']:
                lines.append(p)
            lines.append("")
        
        md_content = "\n".join(lines)
        import tempfile
        fd, temp_path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(md_content)
            
        return FileResponse(path=temp_path, filename=f"{title}.md", media_type="text/markdown")
        
    else:
        raise HTTPException(status_code=400, detail="Invalid format. Use json, md, or pdf.")
