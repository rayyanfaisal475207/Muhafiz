"""Shared authorization and conversation preparation for every chat adapter."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID
from types import SimpleNamespace

from fastapi import HTTPException

from .types import Scope


@dataclass
class PreparedConversation:
    scope: Scope
    history: list[dict]
    profile: dict
    attachments: list[dict]


async def reauthorize(gateway, scope: Scope):
    """Recheck current permissions before publishing durable work."""
    from src.api.evidence import ExistingConversation
    user = await gateway.get_user_by_id(str(scope.user_id))
    if not user:
        raise HTTPException(403, 'Run owner no longer exists')
    prepared = await prepare(gateway, SimpleNamespace(id=scope.user_id,role=user['role']),
                             ExistingConversation(session_id=str(scope.session_id)),create=False)
    if (not set(scope.case_ids) <= set(prepared.scope.case_ids)
            or scope.project_id != prepared.scope.project_id
            or scope.include_global and not prepared.scope.include_global):
        raise HTTPException(403, 'Run authorization has changed')
    return prepared.scope


async def prepare(gateway, user, payload, *, create: bool = True) -> PreparedConversation:
    from src.auth.rls_context import set_cross_case_scope
    # Session lookup must precede case filtering. Ownership is mandatory here.
    set_cross_case_scope()
    try:
        session_id = UUID(str(payload.session_id))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "session_id must be a UUID")
    user_id = str(user.id)
    session = await gateway.get_session(str(session_id))
    if session is None and not create:
        raise HTTPException(404, "Conversation not found")
    if session and str(session.get("user_id")) != user_id:
        raise HTTPException(403, "Not authorized to access this conversation")
    fields = getattr(payload, "model_fields_set", set())
    case_id = payload.case_id if "case_id" in fields else (session or {}).get("case_id")
    project_id = payload.project_id if "project_id" in fields else (session or {}).get("project_id")
    # A session's case/project cannot be silently rebound: earlier attachments
    # and messages retain the original scope. A scope change creates a new chat.
    if session and (case_id != session.get("case_id") or project_id != session.get("project_id")):
        raise HTTPException(409, "Start a new conversation when changing the case or project")
    if project_id:
        try:
            project_id = str(UUID(str(project_id)))
        except ValueError:
            raise HTTPException(422, "project_id must be a UUID")
        project = await gateway.get_project(project_id)
        if not project or str(project.get("user_id")) != user_id:
            raise HTTPException(403, "Not authorized to access this project")
    if case_id:
        if await gateway.get_case(case_id) is None:
            raise HTTPException(404, "Case not found")
        if not await gateway.check_case_access(case_id, user_id, user.role):
            raise HTTPException(403, "Not assigned to this case")
        cases = [case_id]
    elif user.role in {"supervisor", "station-admin", "platform-admin"}:
        cases = [r["case_id"] for r in await gateway.get_cases(user_id, user.role)]
    else:
        cases = []  # Reference-only scope; user prose cannot widen it.
    attachments = await gateway.get_attachments_for_session(str(session_id), include_text=True)
    if any(str(a.get("user_id")) != user_id for a in attachments):
        raise HTTPException(403, "Not authorized to access this conversation's attachments")
    if session is None and create:
        title = " ".join(payload.message.split()[:6])[:80] or "New Conversation"
        await gateway.create_session(str(session_id), user_id, title, project_id, case_id)
    history = await gateway.get_session_history(str(session_id)) if session else []
    mirror_ids = set()
    for item in history:
        key = (item.get('degradation_trace') or {}).get('evidence_run_id')
        if key:
            try:
                mirror_ids.add(UUID(str(key)))
            except (ValueError, TypeError, AttributeError):
                pass  # Malformed mirrors are excluded, never treated as legacy turns.
    if mirror_ids:
        from .database import connection
        current_scope = Scope(user_id=UUID(user_id),session_id=session_id,case_ids=cases,
                              project_id=UUID(project_id) if project_id else None)
        async with connection(current_scope) as conn:
            saved = await conn.fetch('SELECT run_id,scope FROM evidence.runs WHERE session_id=$1 AND run_id=ANY($2::uuid[])',
                                     session_id,list(mirror_ids))
        allowed = {str(row['run_id']) for row in saved if set(row['scope'].get('case_ids',[])) <= set(cases)
                   and row['scope'].get('project_id') == (str(project_id) if project_id else None)
                   and (not row['scope'].get('include_global',True) or current_scope.include_global)}
        history = [item for item in history if not (item.get('degradation_trace') or {}).get('evidence_run_id')
                   or str(item['degradation_trace']['evidence_run_id']) in allowed]
    else:
        history = [item for item in history if not (item.get('degradation_trace') or {}).get('evidence_run_id')]
    profile = await gateway.get_user_context_profile(user_id)
    return PreparedConversation(
        Scope(user_id=UUID(user_id), session_id=session_id, case_ids=cases,
              project_id=UUID(project_id) if project_id else None),
        history[-12:], profile, [a for a in attachments if a.get("status") == "ready"],
    )
