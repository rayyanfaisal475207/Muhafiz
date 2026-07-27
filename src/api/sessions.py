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
    return {"history": history}

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
        
    history = await gateway.get_session_history(session_id)
    
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
        filepath, _ = build_pdf(payload)
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
