import re
import uuid
from datetime import date, datetime
from fastapi import APIRouter, HTTPException, Depends, Request
from typing import List, Optional, Any
from pydantic import BaseModel
from src.data_gateway import get_gateway
from src.auth.routes import get_current_user, limiter
from src.auth.rls_context import set_case_scope, cross_case_rls_dependency
from src.database.models import User

router = APIRouter(tags=["cases"])

# Phase 7: RBAC/ABAC case-assignment scoping via case_assignments table.

def require_case_access(min_role: str = None):
    """
    Dependency factory: require the caller to be assigned to :case_id,
    optionally at or above a specific per-case assignment role.

    Phase 5, Module 5.1: `update_case`/`delete_case` pass min_role="supervisor"
    — any assignee could otherwise edit or permanently delete a case record.
    Read/list access is unaffected (min_role=None keeps the original
    "any assignment" threshold), since the finding was about destructive
    operations specifically.
    """
    async def dependency(case_id: str, current_user: User = Depends(get_current_user)) -> str:
        # Phase 2: arm Postgres RLS scoped to this case_id BEFORE the gateway
        # call below (and every gateway call the route handler itself makes)
        # runs — real per-case enforcement, not just the app-layer check that
        # was previously the only backstop. See src/auth/rls_context.py.
        set_case_scope(case_id)
        gateway = await get_gateway()
        if not await gateway.check_case_access(case_id, str(current_user.id), current_user.role, min_role=min_role):
            detail = "Not assigned to this case" if min_role is None else f"Case role '{min_role}' or higher required"
            raise HTTPException(status_code=403, detail=detail)
        return case_id
    return dependency


class CaseCreate(BaseModel):
    case_id: Optional[str] = None
    fir_number: Optional[str] = None
    crime_category: Optional[str] = None
    investigation_officer: Optional[str] = None
    police_station: Optional[str] = None
    incident_date: Optional[date] = None
    investigation_status: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    victim_info: Optional[dict[str, Any]] = None
    suspect_info: Optional[dict[str, Any]] = None


class CaseUpdate(BaseModel):
    fir_number: Optional[str] = None
    crime_category: Optional[str] = None
    investigation_officer: Optional[str] = None
    police_station: Optional[str] = None
    incident_date: Optional[date] = None
    investigation_status: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    victim_info: Optional[dict[str, Any]] = None
    suspect_info: Optional[dict[str, Any]] = None


class CaseResponse(BaseModel):
    case_id: str
    fir_number: Optional[str] = None
    crime_category: Optional[str] = None
    investigation_officer: Optional[str] = None
    police_station: Optional[str] = None
    incident_date: Optional[date] = None
    investigation_status: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    victim_info: Optional[dict[str, Any]] = None
    suspect_info: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime


def _generate_case_id() -> str:
    return f"CASE-{uuid.uuid4().hex[:8].upper()}"


_CASE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@router.get("/", response_model=List[CaseResponse])
async def list_cases(current_user: User = Depends(get_current_user), _rls=Depends(cross_case_rls_dependency)):
    """
    List assigned cases. No single case_id to scope RLS to (this spans
    every case the caller is assigned to, or all of them for
    platform-admin) — RLS is armed but the case dimension is bypassed
    here; gateway.get_cases() already does the real RBAC filtering by
    user_id/role. See src/auth/rls_context.py.
    """
    gateway = await get_gateway()
    return await gateway.get_cases(str(current_user.id), current_user.role)


@router.get("/{case_id}", response_model=CaseResponse)
async def get_case(case_id: str = Depends(require_case_access()), current_user: User = Depends(get_current_user)):
    gateway = await get_gateway()
    case = await gateway.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@router.post("/", response_model=CaseResponse)
@limiter.limit("20/minute")
async def create_case(request: Request, case: CaseCreate, current_user: User = Depends(get_current_user)):
    gateway = await get_gateway()
    payload = case.dict(exclude_unset=True)

    case_id = payload.pop("case_id", None) or _generate_case_id()
    if not _CASE_ID_RE.match(case_id):
        raise HTTPException(status_code=400, detail="case_id may only contain letters, numbers, '.', '_' and '-'")
    # Arm RLS scoped to the case_id being created NOW that it's known —
    # this row's INSERT is checked against the same policy predicate as
    # any read (FOR ALL policies reuse USING as WITH CHECK), so app.case_id
    # must equal this case_id before create_case()'s INSERT runs below.
    set_case_scope(case_id)
    if await gateway.get_case(case_id):
        raise HTTPException(status_code=409, detail=f"Case '{case_id}' already exists")

    payload["case_id"] = case_id
    created = await gateway.create_case(payload)
    if not created:
        raise HTTPException(status_code=500, detail="Failed to create case")
    
    # Auto-assign the creator
    await gateway.assign_user_to_case(case_id, str(current_user.id), "investigator")
    await gateway.log_audit_event("admin_action", {"action": "create_case", "payload": payload}, str(current_user.id), case_id)
    return created


@router.put("/{case_id}", response_model=CaseResponse)
async def update_case(case: CaseUpdate, case_id: str = Depends(require_case_access(min_role="supervisor")), current_user: User = Depends(get_current_user)):
    # Verify it exists

    payload = case.dict(exclude_unset=True)
    if not payload:
        return await get_case(case_id, current_user)

    gateway = await get_gateway()
    updated = await gateway.update_case(case_id, payload)
    if not updated:
        raise HTTPException(status_code=404, detail="Case not found")
    await gateway.log_audit_event("admin_action", {"action": "update_case", "payload": payload}, str(current_user.id), case_id)
    return updated


@router.delete("/{case_id}")
async def delete_case(case_id: str = Depends(require_case_access(min_role="supervisor")), current_user: User = Depends(get_current_user)):
    gateway = await get_gateway()

    # F-10: this comment said "Verify it exists" but never did — a
    # nonexistent/already-deleted case_id returned {"status": "deleted"}
    # and wrote a misleading "delete_case" audit event, because
    # gateway.delete_case() silently no-ops on an unknown case_id (same
    # short-circuit-for-platform-admin gap check_case_access() has
    # elsewhere — see main.py's chat_endpoint for the identical fix
    # already applied there). Check existence before acting or logging.
    if await gateway.get_case(case_id) is None:
        raise HTTPException(status_code=404, detail="Case not found")

    await gateway.log_audit_event("admin_action", {"action": "delete_case"}, str(current_user.id), case_id)
    await gateway.delete_case(case_id)
    return {"status": "deleted"}
