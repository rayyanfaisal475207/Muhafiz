import logging
from fastapi import APIRouter, HTTPException, Depends, Request
from typing import List
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
    role: str

async def _require_station_match(gateway, case_id: str, current_user: User) -> None:
    """
    Phase 5, Module 5.1: station-scoping. Below platform-admin, a
    station-admin may only manage assignments for cases at their own
    police station — otherwise any station-admin anywhere could
    assign/unassign users on any case regardless of station.

    Bridge until `User.police_station` is backfilled (migration
    012_user_station.sql adds the column with no backfill — this
    environment has no existing case_assignments data to infer a station
    from): a caller with `police_station IS NULL` falls back to the old
    unrestricted behavior, loudly logged, rather than being locked out of
    every case the moment this check ships.
    """
    if current_user.role == "platform-admin":
        return
    if getattr(current_user, "police_station", None) is None:
        logger.warning(
            "case_assignments station-scoping bypassed: user %s (role=%s) has "
            "no police_station set (not yet backfilled) — falling back to "
            "unrestricted access for case %s",
            current_user.id, current_user.role, case_id,
        )
        return
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
