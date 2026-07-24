# ============================================================
# Chat attachments — files attached to ONE conversation.
#
# THIS IS NOT INGESTION. The distinction matters, so it is enforced here
# rather than left to a filter someone can forget:
#
#   Knowledge base (admin)      | Chat attachment (user)
#   ----------------------------|------------------------------------------
#   chunked + embedded          | text extracted once, never embedded
#   lives in ChromaDB           | lives in session_attachments
#   retrievable by every user   | visible only to this conversation
#   permanent, shared           | deleted with the conversation
#
# An attachment's text is injected into the prompt for its own session. It can
# never be returned by retrieval for anyone else, because it is never in the
# table retrieval reads.
# ============================================================

import logging
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form

from src import config
from src.auth.routes import get_current_user
from src.database.models import User
from src.data_gateway import get_gateway

logger = logging.getLogger(__name__)

router = APIRouter()

ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".md", ".csv", ".xlsx", ".xls",
    ".html", ".htm", ".docx", ".png", ".jpg", ".jpeg", ".webp",
}

# An attachment is prompt context, not a corpus. Cap what one file can
# contribute so a 300-page PDF cannot evict the conversation itself from the
# context window.
MAX_CHARS_PER_ATTACHMENT = 12_000
MAX_ATTACHMENTS_PER_SESSION = 5
MAX_UPLOAD_MB = 10


async def _extract_text(path: Path) -> str:
    """Reuse the ingestion loaders — same parsers, different destination."""
    from src.ingestion.loader_router import route_and_load
    import asyncio

    documents = await asyncio.to_thread(route_and_load, path)
    if not documents:
        return ""
    return "\n\n".join(d.text for d in documents if getattr(d, "text", None))


@router.post("")
async def upload_attachment(
    session_id: str = Form(...),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Attach a file to one conversation. Returns immediately with its status."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"Attachments are limited to {MAX_UPLOAD_MB}MB.",
        )

    gateway = await get_gateway()

    existing = await gateway.get_attachments_for_session(session_id)
    if len(existing) >= MAX_ATTACHMENTS_PER_SESSION:
        raise HTTPException(
            status_code=400,
            detail=f"A conversation can hold at most {MAX_ATTACHMENTS_PER_SESSION} attachments.",
        )

    # Extract to a temp file — attachments are never kept on disk, only their text.
    tmp_path = None
    extracted, error = "", None
    try:
        fd, tmp_name = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        tmp_path = Path(tmp_name)
        tmp_path.write_bytes(contents)
        extracted = await _extract_text(tmp_path)
        if not extracted.strip():
            error = "No readable text could be extracted from this file."
    except Exception as exc:
        logger.error("Attachment extraction failed for %s: %s", file.filename, exc)
        error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    truncated = extracted[:MAX_CHARS_PER_ATTACHMENT]

    try:
        record = await gateway.create_attachment({
            "session_id": session_id,
            "user_id": str(current_user.id),
            "filename": os.path.basename(file.filename or f"attachment{ext}"),
            "file_type": ext.lstrip("."),
            "file_size_bytes": len(contents),
            "extracted_text": truncated or None,
            "char_count": len(extracted),
            "status": "failed" if error else "ready",
            "error_message": error,
        })
    except Exception as exc:
        # Before migration 003 the table does not exist. Say so plainly instead
        # of returning an opaque 500 that the composer would render as
        # "Upload failed" with no way to act on it.
        if "does not exist" in str(exc).lower() or "could not find the table" in str(exc).lower():
            raise HTTPException(
                status_code=503,
                detail="Attachments are not enabled yet — run migration 003 "
                       "(migrations/003_admin_dashboard_and_attachments.sql).",
            )
        raise

    if error:
        # Surfaced, not swallowed: the composer shows the chip in a failed state.
        record["error_message"] = error

    record.pop("extracted_text", None)
    return record


@router.get("")
async def list_attachments(session_id: str, current_user: User = Depends(get_current_user)):
    """Attachments on a conversation (metadata only — never the extracted text)."""
    gateway = await get_gateway()
    session = await gateway.get_session(session_id)
    if session and session.get("user_id") and session["user_id"] != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to access this session.")
    return await gateway.get_attachments_for_session(session_id)


@router.delete("/{attachment_id}")
async def delete_attachment(attachment_id: str, current_user: User = Depends(get_current_user)):
    gateway = await get_gateway()
    record = await gateway.get_attachment(attachment_id)
    if not record:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if record.get("user_id") and record["user_id"] != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to delete this attachment")

    await gateway.delete_attachment(attachment_id)
    return {"status": "deleted", "attachment_id": attachment_id}


# ── Used by the orchestrator ──────────────────────────────────────────────────

async def build_attachment_context(session_id: str, max_chars: int = 24_000) -> str:
    """
    The attached files for this conversation, formatted for the prompt.

    Returns "" when there are none, so the prompt is unchanged for the common
    case. Total size is capped so attachments can never crowd out the documents
    retrieved from the knowledge base.
    """
    try:
        gateway = await get_gateway()
        attachments = await gateway.get_attachments_for_session(session_id, include_text=True)
    except Exception as exc:
        logger.warning("Could not load attachments for session %s: %s", session_id, exc)
        return ""

    usable = [a for a in attachments if a.get("status") == "ready" and a.get("extracted_text")]
    if not usable:
        return ""

    parts, budget = [], max_chars
    for att in usable:
        text = (att.get("extracted_text") or "")[:budget]
        if not text:
            break
        parts.append(f"--- ATTACHED FILE: {att['filename']} ---\n{text}")
        budget -= len(text)
        if budget <= 0:
            break

    return (
        "The user attached the following file(s) to THIS conversation. They are not part "
        "of the knowledge base — treat them as context the user provided about their "
        "own situation, and cite them by filename when you use them.\n\n"
        + "\n\n".join(parts)
    )
