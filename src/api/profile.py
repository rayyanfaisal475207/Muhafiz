# ============================================================
# API Routes — User Profile
# ============================================================

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
    preferred_language: str
    llm_mode: str

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
