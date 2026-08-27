"""
Shared request-boundary UUID validation (audit finding F-06).

Several endpoints accept an id (session_id, attachment_id, JWT `sub`) typed
as a plain `str`, then let something downstream cast it with `uuid.UUID(...)`.
A malformed value used to reach one of those casts unguarded and surface as
an unhandled 500 instead of a clean 4xx — the same shape of bug repeated at
several call sites (src/main.py's chat endpoint, src/api/attachments.py's
upload/list/delete, src/auth/jwt.py's token `sub`). This validates once, at
the boundary, before anything downstream sees the value — mirroring the
correct pattern already used by main.py's `/files/{file_id}` route.
"""
from uuid import UUID

from fastapi import HTTPException


def validate_uuid_field(value: str, field_name: str = "id") -> str:
    """Raise a 422 if value isn't a well-formed UUID; otherwise return it unchanged."""
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=422, detail=f"Invalid {field_name} format")
    return value
