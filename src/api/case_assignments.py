from fastapi import APIRouter, HTTPException, Depends, Request
from typing import List
from pydantic import BaseModel
from src.data_gateway import get_gateway
from src.auth.routes import get_current_user, limiter
from src.auth.jwt import require_role
from src.auth.rls_context import case_rls_dependency
from src.database.models import User

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

@router.get("/", response_model=List[CaseAssignmentResponse])
async def list_assignments(case_id: str, current_user: User = Depends(require_role("station-admin"))):
    """List all users assigned to a case."""
    gateway = await get_gateway()
    return await gateway.get_case_assignments(case_id)

@router.post("/")
@limiter.limit("20/minute")
async def assign_user(request: Request, case_id: str, assignment: CaseAssignmentCreate, current_user: User = Depends(require_role("station-admin"))):
    """Assign a user (by email) to a case with a specific role."""
    gateway = await get_gateway()
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
    await gateway.unassign_user_from_case(case_id, user_id)
    await gateway.log_audit_event(
        "admin_action", 
        {"action": "unassign_user", "target_user_id": user_id}, 
        str(current_user.id), 
        case_id
    )
    return {"status": "unassigned"}
