import re
import uuid
from datetime import date, datetime
from fastapi import APIRouter, HTTPException, Depends
from typing import List, Optional, Any
from pydantic import BaseModel
from src.data_gateway import get_gateway
from src.auth.routes import get_current_user
from src.database.models import User

router = APIRouter(tags=["cases"])

# Phase 7: RBAC/ABAC case-assignment scoping via case_assignments table.

async def require_case_access(case_id: str, current_user: User = Depends(get_current_user)) -> str:
    gateway = await get_gateway()
    if not await gateway.check_case_access(case_id, str(current_user.id), current_user.role):
        raise HTTPException(status_code=403, detail="Not assigned to this case")
    return case_id


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
async def list_cases(current_user: User = Depends(get_current_user)):
    """List assigned cases."""
    gateway = await get_gateway()
    return await gateway.get_cases(str(current_user.id), current_user.role)


@router.get("/{case_id}", response_model=CaseResponse)
async def get_case(case_id: str = Depends(require_case_access), current_user: User = Depends(get_current_user)):
    gateway = await get_gateway()
    case = await gateway.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@router.post("/", response_model=CaseResponse)
async def create_case(case: CaseCreate, current_user: User = Depends(get_current_user)):
    gateway = await get_gateway()
    payload = case.dict(exclude_unset=True)

    case_id = payload.pop("case_id", None) or _generate_case_id()
    if not _CASE_ID_RE.match(case_id):
        raise HTTPException(status_code=400, detail="case_id may only contain letters, numbers, '.', '_' and '-'")
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
async def update_case(case: CaseUpdate, case_id: str = Depends(require_case_access), current_user: User = Depends(get_current_user)):
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
async def delete_case(case_id: str = Depends(require_case_access), current_user: User = Depends(get_current_user)):
    # Verify it exists
    gateway = await get_gateway()
    await gateway.log_audit_event("admin_action", {"action": "delete_case"}, str(current_user.id), case_id)
    await gateway.delete_case(case_id)
    return {"status": "deleted"}
