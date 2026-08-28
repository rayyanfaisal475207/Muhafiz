# ============================================================
# API Routes — User Profile
# ============================================================

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field

from src.data_gateway import get_gateway
from src.database.models import User
from src.auth.routes import get_current_user

router = APIRouter()

class ProfileUpdate(BaseModel):
    context_text: str = Field(..., max_length=1000)
    # Audit hypotheses #6/#7: both were a bare `str` -- any string was
    # accepted here, including XSS-shaped input. The frontend's own
    # <select> (SettingsPage.tsx) only ever sends these exact values, so
    # this just enforces server-side what was already the only reachable
    # UI path.
    preferred_language: Literal["auto", "english", "urdu"]
    llm_mode: Literal["cloud", "local"]

@router.get("")
async def get_profile(
    current_user: User = Depends(get_current_user),
):
    gateway = await get_gateway()
    return await gateway.get_user_context_profile(current_user.id)

@router.put("")
async def update_profile(
    update_data: ProfileUpdate,
    current_user: User = Depends(get_current_user),
):
    gateway = await get_gateway()
    return await gateway.update_user_context_profile(current_user.id, update_data.model_dump())
