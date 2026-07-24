import uuid
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from typing import List, Optional
from pydantic import BaseModel
from src.data_gateway import get_gateway
from src.auth.routes import get_current_user
from src.database.models import User

router = APIRouter(tags=["projects"])

class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None
    domain_context: Optional[str] = None

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    domain_context: Optional[str] = None

class ProjectResponse(BaseModel):
    id: str
    user_id: str
    name: str
    description: Optional[str] = None
    domain_context: Optional[str] = None
    created_at: datetime
    updated_at: datetime

@router.get("/", response_model=List[ProjectResponse])
async def list_projects(current_user: User = Depends(get_current_user)):
    """List all projects for a user"""
    gateway = await get_gateway()
    return await gateway.get_projects_for_user(str(current_user.id))

@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, current_user: User = Depends(get_current_user)):
    gateway = await get_gateway()
    project = await gateway.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if str(project["user_id"]) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to access this project")

    return project

@router.post("/", response_model=ProjectResponse)
async def create_project(project: ProjectCreate, current_user: User = Depends(get_current_user)):
    gateway = await get_gateway()
    payload = project.dict(exclude_unset=True)
    payload["user_id"] = str(current_user.id)

    created = await gateway.create_project(payload)
    if not created:
        raise HTTPException(status_code=500, detail="Failed to create project")
    return created

@router.put("/{project_id}", response_model=ProjectResponse)
async def update_project(project_id: str, project: ProjectUpdate, current_user: User = Depends(get_current_user)):
    # Verify ownership
    await get_project(project_id, current_user)

    payload = project.dict(exclude_unset=True)
    if not payload:
        return await get_project(project_id, current_user)

    gateway = await get_gateway()
    updated = await gateway.update_project(project_id, payload)
    if not updated:
        raise HTTPException(status_code=404, detail="Project not found")
    return updated

@router.delete("/{project_id}")
async def delete_project(project_id: str, current_user: User = Depends(get_current_user)):
    # Verify ownership
    await get_project(project_id, current_user)

    gateway = await get_gateway()
    await gateway.delete_project(project_id)
    return {"status": "deleted"}
