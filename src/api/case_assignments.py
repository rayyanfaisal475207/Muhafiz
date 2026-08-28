import logging
from fastapi import APIRouter, HTTPException, Depends, Request
from typing import List, Literal
from pydantic import BaseModel
from src.data_gateway import get_gateway
from src.auth.routes import get_current_user, limiter
from src.auth.jwt import require_role
from src.auth.rls_context import case_rls_dependency
from src.database.models import User

logger = logging.getLogger(__name__)

# Phase 2: every route here has case_id as a genuine path parameter, so a
# single router-level dependency can arm RLS scoped to it before any
# handler body runs. See src/auth/rls_context.py.
router = APIRouter(
    prefix="/api/cases/{case_id}/assignments", tags=["case-assignments"],
    dependencies=[Depends(case_rls_dependency)],
)

class CaseAssignmentResponse(BaseModel):
    user_id: str
    email: str | None = None
    role: str

class CaseAssignmentCreate(BaseModel):
    # Resolved server-side to a user_id via lookup, rather than requiring the
    # caller to already know the raw UUID — the admin user list is
    # platform-admin-only, so a station-admin assigning someone to a case
    # they're already on has no other way to find a user_id.
    email: str
    # Audit hypothesis #5: was a bare `str` -- any string was accepted here
    # with no allow-list, unlike users.role which is a real DB enum. These
    # are the same 4 values check_case_access()'s role hierarchy
    # (src/data_gateway/direct_backend.py) and require_role() already use
    # everywhere else in this codebase.
    role: Literal["investigator", "supervisor", "station-admin", "platform-admin"]

async def _require_station_match(gateway, case_id: str, current_user: User) -> None:
    """
    Phase 5, Module 5.1: station-scoping. Below platform-admin, a
    station-admin may only manage assignments for cases at their own
    police station — otherwise any station-admin anywhere could
    assign/unassign users on any case regardless of station.

    Audit finding F-08: the original bridge fell back to unrestricted
    access (loudly logged) when `police_station IS NULL`, on the reasoning
    that this environment had no case_assignments data to backfill a
    station from at the time migration 012 shipped. That bridge has now
    outlived its purpose — the one station-admin account still missing a
    station is a synthetic smoke-test fixture, not a real officer with a
    real jurisdiction to infer, so there is nothing left to backfill and
    no reason left to keep the unrestricted fallback. A station-admin with
    no police_station set is now denied by default, matching the
    fail-closed posture used everywhere else in this codebase's ABAC
    checks (see e.g. harness role gating). An operator must set
    police_station on any station-admin account before it can manage case
    assignments.
    """
    if current_user.role == "platform-admin":
        return
    if getattr(current_user, "police_station", None) is None:
        logger.warning(
            "case_assignments station-scoping denied: user %s (role=%s) has "
            "no police_station set — an operator must set it before this "
            "account can manage assignments on case %s",
            current_user.id, current_user.role, case_id,
        )
        raise HTTPException(
            status_code=403,
            detail="Your account has no police station configured. Contact an administrator.",
        )
    case = await gateway.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    if case.get("police_station") != current_user.police_station:
        raise HTTPException(status_code=403, detail="Case belongs to a different police station")


@router.get("/", response_model=List[CaseAssignmentResponse])
async def list_assignments(case_id: str, current_user: User = Depends(require_role("station-admin"))):
    """List all users assigned to a case."""
    gateway = await get_gateway()
    await _require_station_match(gateway, case_id, current_user)
    return await gateway.get_case_assignments(case_id)

@router.post("/")
@limiter.limit("20/minute")
async def assign_user(request: Request, case_id: str, assignment: CaseAssignmentCreate, current_user: User = Depends(require_role("station-admin"))):
    """Assign a user (by email) to a case with a specific role."""
    gateway = await get_gateway()
    await _require_station_match(gateway, case_id, current_user)
    target_user = await gateway.get_user_by_email(assignment.email)
    if not target_user:
        raise HTTPException(status_code=404, detail=f"No user found with email '{assignment.email}'")
    target_user_id = target_user["id"] if isinstance(target_user, dict) else target_user.id
    await gateway.assign_user_to_case(case_id, str(target_user_id), assignment.role)
    await gateway.log_audit_event(
        "admin_action",
        {"action": "assign_user", "target_user_id": str(target_user_id), "target_email": assignment.email, "role": assignment.role},
        str(current_user.id),
        case_id
    )
    return {"status": "assigned"}

@router.delete("/{user_id}")
@limiter.limit("20/minute")
async def unassign_user(request: Request, case_id: str, user_id: str, current_user: User = Depends(require_role("station-admin"))):
    """Remove a user from a case."""
    gateway = await get_gateway()
    await _require_station_match(gateway, case_id, current_user)
    await gateway.unassign_user_from_case(case_id, user_id)
    await gateway.log_audit_event(
        "admin_action", 
        {"action": "unassign_user", "target_user_id": user_id}, 
        str(current_user.id), 
        case_id
    )
    return {"status": "unassigned"}
