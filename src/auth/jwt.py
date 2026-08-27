import os
import secrets
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from jose import JWTError, jwt
import bcrypt


from src import config
from src.database.models import User
from src.data_gateway import get_gateway

# 7-day expiry as per the plan
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7 

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

def get_password_hash(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, config.JWT_SECRET_KEY, algorithm=config.JWT_ALGORITHM)
    return encoded_jwt

def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)

async def get_current_user(request: Request) -> User:
    """
    Extract the JWT from the HttpOnly cookie, verify it, and return the User.
    Also enforce Double-Submit CSRF check on mutating requests.
    """
    # 1. CSRF Verification for mutating requests
    if request.method in ["POST", "PUT", "DELETE", "PATCH"]:
        cookie_csrf = request.cookies.get("csrf_token")
        header_csrf = request.headers.get("x-csrf-token")
        if not cookie_csrf or not header_csrf or cookie_csrf != header_csrf:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF token validation failed."
            )

    # 2. Extract JWT from Cookie
    token = request.cookies.get("access_token")
    if not token:
        # Check Authorization header as a fallback for API/Curl testing if cookie is missing
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    # 3. Decode JWT and find user
    try:
        payload = jwt.decode(token, config.JWT_SECRET_KEY, algorithms=[config.JWT_ALGORITHM])
        user_id_str: str = payload.get("sub")
        if user_id_str is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        user_id = UUID(user_id_str)
    except (JWTError, ValueError):
        # F-06: UUID(user_id_str) raises ValueError, not JWTError, for a
        # validly-signed token whose `sub` claim isn't a UUID -- that used
        # to escape this except clause entirely and surface as a 500.
        # Still requires a valid signature, so this was never an auth
        # bypass, just a wrong status code / unhandled crash.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    gateway = await get_gateway()
    user = await gateway.get_user_by_id(user_id)

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        
    class AuthUser:
        def __init__(self, **entries):
            self.__dict__.update(entries)
            
    return AuthUser(**user)

def require_role(minimum_role: str):
    """Dependency that enforces a minimum RBAC role."""
    async def dependency(current_user: User = Depends(get_current_user)) -> User:
        roles = ['investigator', 'supervisor', 'station-admin', 'platform-admin']
        try:
            user_level = roles.index(current_user.role)
            min_level = roles.index(minimum_role)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Invalid role configuration"
            )
        if user_level < min_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{minimum_role}' or higher required"
            )
        return current_user
    return dependency
