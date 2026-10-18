import asyncio
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from supabase_auth.errors import AuthApiError

from config import logger, supabase_auth_client
from dependencies import AuthenticatedUser, get_current_user

router = APIRouter(prefix="/auth", tags=["Auth"])

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


class SignUpRequest(BaseModel):
    email: str
    password: str
    full_name: Optional[str] = None
    redirect_to: Optional[str] = None


class SignInRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ResendRequest(BaseModel):
    email: str
    redirect_to: Optional[str] = None


class VerifyRequest(BaseModel):
    token_hash: str
    type: str = "signup"


def _auth_error(exc: AuthApiError, fallback_status: int = 400) -> HTTPException:
    status_code = exc.status if 400 <= (exc.status or 0) < 500 else fallback_status
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code or "auth_error", "message": exc.message or "Authentication failed."},
    )


def _serialize_session(response: Any) -> Optional[Dict[str, Any]]:
    session = getattr(response, "session", None)
    user = getattr(response, "user", None)
    if not session:
        return None
    return {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expires_in": session.expires_in,
        "expires_at": session.expires_at,
        "token_type": session.token_type,
        "user": {"id": user.id, "email": user.email} if user else None,
    }


@router.post("/signup")
async def sign_up(req: SignUpRequest):
    """Create an account via Supabase Auth (service-key proxy).

    Sends the Supabase verification email. No session is returned until the
    emailed link has been confirmed, unless email confirmation is disabled
    on the Supabase project.
    """
    options: Dict[str, Any] = {
        "data": {"full_name": req.full_name or req.email.split("@")[0].strip()},
    }
    if req.redirect_to:
        options["email_redirect_to"] = req.redirect_to

    def _do():
        return supabase_auth_client.auth.sign_up({
            "email": req.email,
            "password": req.password,
            "options": options,
        })

    try:
        res = await asyncio.to_thread(_do)
    except AuthApiError as e:
        logger.warning(f"Signup failed for '{req.email}': {e.message}")
        raise _auth_error(e)

    session = _serialize_session(res)
    return {
        "session": session,
        "verification_sent": session is None,
        "message": (
            "Account created. Check your email for the verification link."
            if session is None
            else "Account created and signed in."
        ),
    }


@router.post("/signin")
async def sign_in(req: SignInRequest):
    """Password sign-in via Supabase Auth (service-key proxy)."""
    def _do():
        return supabase_auth_client.auth.sign_in_with_password({
            "email": req.email,
            "password": req.password,
        })

    try:
        res = await asyncio.to_thread(_do)
    except AuthApiError as e:
        logger.warning(f"Signin failed for '{req.email}': {e.message}")
        if e.code == "email_not_confirmed":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "email_not_confirmed", "message": "Your email is not verified yet."},
            )
        raise _auth_error(e, fallback_status=401)

    session = _serialize_session(res)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "no_session", "message": "Invalid email or password."})
    return {"session": session}


@router.post("/refresh")
async def refresh(req: RefreshRequest):
    """Exchange a refresh token for a fresh session."""
    try:
        res = await asyncio.to_thread(supabase_auth_client.auth.refresh_session, req.refresh_token)
    except AuthApiError as e:
        raise _auth_error(e, fallback_status=401)

    session = _serialize_session(res)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "no_session", "message": "Could not refresh session."})
    return {"session": session}


@router.post("/resend")
async def resend_verification(req: ResendRequest):
    """Re-send the signup verification email through Supabase."""
    options: Dict[str, Any] = {}
    if req.redirect_to:
        options["email_redirect_to"] = req.redirect_to

    def _do():
        return supabase_auth_client.auth.resend({
            "type": "signup",
            "email": req.email,
            "options": options,
        })

    try:
        await asyncio.to_thread(_do)
    except AuthApiError as e:
        logger.warning(f"Resend verification failed for '{req.email}': {e.message}")
        raise _auth_error(e)

    return {"message": "Verification email sent."}


@router.post("/verify")
async def verify_token_hash(req: VerifyRequest):
    """Exchange an email-link token_hash (custom templates) for a session."""
    try:
        res = await asyncio.to_thread(
            supabase_auth_client.auth.verify_otp,
            {"token_hash": req.token_hash, "type": req.type},
        )
    except AuthApiError as e:
        raise _auth_error(e)

    session = _serialize_session(res)
    if not session:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "no_session", "message": "The verification link is invalid or has expired."})
    return {"session": session}


@router.get("/me")
async def get_me(current_user: AuthenticatedUser = Depends(get_current_user)):
    """Return the profile of the authenticated user (validates the bearer token)."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "role": current_user.role,
    }


@router.get("/confirm")
async def email_confirm_page():
    """Serve the SPA for custom email templates that link to /auth/confirm?token_hash=... .

    The frontend detects the token_hash query parameter and exchanges it for a session.
    """
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Orderly API is running. UI not found in static/index.html"}
