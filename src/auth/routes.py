from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from src import config
from src.auth.jwt import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    generate_csrf_token,
    get_current_user,
    get_password_hash,
    verify_password,
)
from src.database.models import User
from src.data_gateway import get_gateway

# We will initialize the limiter in main.py, but we can import it or define it there.
# It's better to define it in a separate file or just import from main.
# To avoid circular imports, let's create a small limiter file or just instantiate it here and import in main.
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

router = APIRouter()

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    company_name: str | None = None

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    is_admin: bool  # derived (role == "platform-admin"); kept for existing frontend consumers
    company_name: str | None
    plan: str

    class Config:
        from_attributes = True

@router.post("/register", response_model=UserResponse)
@limiter.limit("5/minute")
async def register_user(request: Request, user_in: UserCreate):
    gateway = await get_gateway()
    existing_user = await gateway.get_user_by_email(user_in.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email already exists."
        )

    user_data = {
        "email": user_in.email,
        "password_hash": get_password_hash(user_in.password),
        "company_name": user_in.company_name
    }
    new_user = await gateway.create_user(user_data)

    return {
        "id": str(new_user["id"]),
        "email": new_user["email"],
        "role": new_user["role"],
        "is_admin": new_user["is_admin"],
        "company_name": new_user["company_name"],
        "plan": new_user["plan"]
    }

@router.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, response: Response, login_data: UserLogin):
    gateway = await get_gateway()
    user = await gateway.get_user_by_email(login_data.email)

    if not user or not verify_password(login_data.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    # Create JWT token
    access_token = create_access_token(data={"sub": str(user["id"])})
    
    # Create CSRF token for Double-Submit CSRF pattern
    csrf_token = generate_csrf_token()

    max_age_seconds = ACCESS_TOKEN_EXPIRE_MINUTES * 60

    is_secure = config.ENVIRONMENT != "development"

    # Set HttpOnly JWT cookie
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=is_secure,
        samesite="lax",       # or "none" if strictly cross-origin
        max_age=max_age_seconds,
    )
    
    # Set CSRF token cookie (readable by frontend)
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        httponly=False,
        secure=is_secure,
        samesite="lax",
        max_age=max_age_seconds,
    )

    return {"message": "Login successful"}

@router.post("/logout")
async def logout(response: Response, current_user: User = Depends(get_current_user)):
    is_secure = config.ENVIRONMENT != "development"
    response.delete_cookie("access_token", secure=is_secure, samesite="lax", httponly=True)
    response.delete_cookie("csrf_token", secure=is_secure, samesite="lax")
    return {"message": "Logged out successfully"}


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "role": current_user.role,
        "is_admin": current_user.is_admin,
        "company_name": current_user.company_name,
        "plan": current_user.plan
    }
