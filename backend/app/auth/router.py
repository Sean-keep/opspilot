"""Auth endpoints: login, whoami, and user administration."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from backend.app.audit.models import RESULT_FAILURE, RESULT_SUCCESS
from backend.app.audit.service import write_audit
from backend.app.auth.deps import (
    AuthServiceDep,
    CurrentUser,
    request_id_of,
    require_permission,
)
from backend.app.auth.models import User
from backend.app.auth.schemas import LoginRequest, MeOut, TokenResponse, UserCreate, UserOut
from backend.app.core.errors import RateLimited, Unauthenticated
from backend.app.core.logging import get_logger
from backend.app.core.security import create_access_token
from backend.app.core.settings import get_settings

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# Permission-scoped callers. The dependency both authenticates and authorises.
CanCreateUser = Annotated[User, Depends(require_permission("user.create"))]
CanReadUsers = Annotated[User, Depends(require_permission("user.read"))]


# --------------------------------------------------------------------------
# Login rate limiting
# --------------------------------------------------------------------------
# Sliding window kept in process memory. The API runs as a single process under
# systemd in the target deployment, so this is sufficient for V1. It is
# documented in docs/security-model.md as a known limitation; a later phase can
# move the counter into MySQL if the API is scaled out.
_LOGIN_FAILURES: dict[str, list[float]] = {}


def _rate_limit_key(username: str, client_ip: str) -> str:
    return f"{username.strip().lower()}|{client_ip}"


def _check_rate_limit(key: str, now: float) -> None:
    cfg = get_settings()
    window = cfg.login_window_seconds
    events = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < window]
    _LOGIN_FAILURES[key] = events
    if len(events) >= cfg.login_max_failures:
        raise RateLimited(
            "too many failed login attempts; try again later",
            details={"retry_after_seconds": window},
        )


def _record_failure(key: str, now: float) -> None:
    _LOGIN_FAILURES.setdefault(key, []).append(now)


def _clear_failures(key: str) -> None:
    _LOGIN_FAILURES.pop(key, None)


def reset_rate_limiter() -> None:
    """Test hook: forget every recorded login failure."""
    _LOGIN_FAILURES.clear()


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    auth: AuthServiceDep,
) -> TokenResponse:
    """Exchange credentials for an access token.

    Failure is recorded for rate limiting and audited. The response never
    distinguishes "no such user" from "wrong password".

    Audit uses :func:`write_audit` (its own transaction) rather than the request
    session: a rejected login must still leave a trail, and FastAPI rolls the
    request session back when the handler raises.
    """
    client_ip = request.client.host if request.client else "unknown"
    key = _rate_limit_key(payload.username, client_ip)
    now = time.monotonic()
    rid = request_id_of(request)

    _check_rate_limit(key, now)

    try:
        user = auth.authenticate(payload.username, payload.password)
    except Unauthenticated:
        _record_failure(key, now)
        write_audit(
            "auth.login",
            actor_name=payload.username,
            target_type="user",
            target_id=payload.username,
            result=RESULT_FAILURE,
            detail="invalid credentials",
            request_id=rid,
        )
        raise

    _clear_failures(key)
    write_audit(
        "auth.login",
        actor_id=user.id,
        actor_name=user.username,
        target_type="user",
        target_id=str(user.id),
        result=RESULT_SUCCESS,
        request_id=rid,
    )

    cfg = get_settings()
    token = create_access_token(user.id, user.username, settings=cfg)
    return TokenResponse(
        access_token=token,
        token_type="bearer",  # noqa: S106 - OAuth scheme name, not a secret
        expires_in=cfg.access_token_ttl,
        user=auth.to_out(user),
    )


@router.get("/me", response_model=MeOut)
def me(user: CurrentUser, auth: AuthServiceDep) -> MeOut:
    """Return the calling user and their effective permissions.

    Only authentication is required here — every signed-in operator may see
    their own identity. Authorisation for anything else is a separate check.
    """
    return MeOut(user=auth.to_out(user), permissions=sorted(auth.permissions_for(user)))


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(
    payload: UserCreate,
    request: Request,
    user: CanCreateUser,
    auth: AuthServiceDep,
) -> UserOut:
    """Create a platform account. Requires ``user.create``."""
    created = auth.create_user(payload)
    write_audit(
        "auth.user.create",
        actor_id=user.id,
        actor_name=user.username,
        target_type="user",
        target_id=str(created.id),
        result=RESULT_SUCCESS,
        request_id=request_id_of(request),
        snapshot={"username": created.username, "roles": payload.role_names},
    )
    return auth.to_out(created)


@router.get("/users", response_model=list[UserOut])
def list_users(_user: CanReadUsers, auth: AuthServiceDep) -> list[UserOut]:
    """List platform accounts. Requires ``user.read``.

    ``_user`` is bound only so the authorisation dependency runs.
    """
    rows = auth.session.scalars(select(User).order_by(User.id))
    return [auth.to_out(u) for u in rows]
