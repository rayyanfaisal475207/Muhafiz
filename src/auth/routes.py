from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, field_validator
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


def _rate_limit_key(request: Request) -> str:
    """
    Audit finding F-09: slowapi's default get_remote_address reads the TCP
    peer address, which is the reverse proxy's own IP for every request when
    this app sits behind one -- collapsing every distinct client into one
    shared rate-limit bucket. See src/config.py's TRUST_PROXY_HEADERS
    docstring for why this is opt-in and allowlist-gated rather than trusting
    X-Forwarded-For unconditionally (a client-controlled header would
    otherwise let anyone forge their way past the limit).
    """
    peer_ip = get_remote_address(request)
    if config.TRUST_PROXY_HEADERS and peer_ip in config.TRUSTED_PROXY_IPS:
        forwarded = request.headers.get("X-Forwarded-For", "")
        # X-Forwarded-For may be a comma-separated hop chain; the first
        # entry is the original client as reported by the nearest trusted
        # hop -- everything after it is proxy-to-proxy, not the client.
        client_ip = forwarded.split(",")[0].strip()
        if client_ip:
            return client_ip
    return peer_ip


limiter = Limiter(key_func=_rate_limit_key)

router = APIRouter()

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    company_name: str | None = None

    @field_validator("password")
    @classmethod
    def password_minimum_length(cls, value: str) -> str:
        # 12+ chars, no complexity-class requirements (uppercase/digit/symbol
        # rules push predictable substitutions per current NIST guidance) —
        # this platform has no MFA, so length is the one lever available.
        if len(value) < 12:
            raise ValueError("Password must be at least 12 characters long.")
        return value

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
