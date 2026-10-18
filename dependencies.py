import asyncio
from dataclasses import dataclass
from typing import Any, Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from config import supabase_client

security = HTTPBearer()

ELEVATED_ROLES = {"admin"}

@dataclass
class AuthenticatedUser:
    id: str
    email: Optional[str] = None
    role: str = "customer"
    full_name: Optional[str] = None
    raw_user: Any = None

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> AuthenticatedUser:
    token = credentials.credentials
    try:
        res = await asyncio.to_thread(supabase_client.auth.get_user, token)
        if not res or not res.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = res.user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    def _fetch_profile():
        return (
            supabase_client.table("users")
            .select("is_active, role, full_name")
            .eq("user_id", user.id)
            .execute()
        )

    role = "customer"
    full_name = None

    try:
        profile_res = await asyncio.to_thread(_fetch_profile)
        if profile_res.data:
            profile = profile_res.data[0]
            if profile.get("is_active") is False:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User account is inactive. Please contact support.",
                )
            role = profile.get("role") or role
            full_name = profile.get("full_name")

        meta = user.user_metadata if isinstance(user.user_metadata, dict) else {}
        if not full_name:
            full_name = meta.get("full_name")
        if role == "customer" and meta.get("role"):
            role = meta.get("role")
        if not full_name and user.email and "@" in user.email:
            full_name = user.email.split("@")[0]
    except HTTPException:
        raise
    except Exception:
        pass

    return AuthenticatedUser(
        id=user.id,
        email=user.email,
        role=role,
        full_name=full_name,
        raw_user=user,
    )

async def require_elevated_role(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    if current_user.role not in ELEVATED_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied. Role '{current_user.role}' is not authorized to ingest documents. Elevated roles required: {', '.join(sorted(ELEVATED_ROLES))}.",
        )
    return current_user


security_optional = HTTPBearer(auto_error=False)

async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_optional),
) -> Optional[AuthenticatedUser]:
    if not credentials:
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None

