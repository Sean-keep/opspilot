"""FastAPI dependencies: current user and permission enforcement.

These are the single place where an HTTP request is turned into an
authorisation decision. Route handlers must depend on ``CurrentUser`` (or a
``require_permission`` dependency) — they must not read the token themselves.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.app.auth.models import User
from backend.app.auth.service import AuthService
from backend.app.core.errors import Forbidden, Unauthenticated
from backend.app.core.security import decode_access_token
from backend.app.db.session import get_session_factory

_bearer = HTTPBearer(auto_error=False)


def get_db() -> Generator[Session, None, None]:
    """Request-scoped database session.

    Commits at the end of a successful request and rolls back on error. Kept
    deliberately simple — the API is short-lived per request.
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Resolve the authenticated user or raise ``401``."""
    if credentials is None or not credentials.credentials:
        raise Unauthenticated("missing bearer token")

    payload = decode_access_token(credentials.credentials)
    service = AuthService(db)
    user = service.get_user_by_id(payload.user_id)
    if user is None:
        raise Unauthenticated("account no longer exists")

    from backend.app.auth.models import USER_ACTIVE

    if user.status != USER_ACTIVE:
        raise Unauthenticated("account is not active")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_auth_service(db: DbSession) -> AuthService:
    return AuthService(db)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def require_permission(code: str) -> Callable[..., User]:
    """Build a dependency that rejects the request unless *code* is granted.

    Usage::

        @router.post("/servers", dependencies=[Depends(require_permission("server.create"))])
        ...

    or, when the handler needs the user::

        user: User = Depends(require_permission("server.create"))

    The check is against the database on every request, so revoking a role
    takes effect immediately — a stale token cannot keep a privilege it lost.
    """

    def _dependency(
        user: CurrentUser,
        db: DbSession,
    ) -> User:
        service = AuthService(db)
        if not service.has_permission(user, code):
            raise Forbidden(f"permission {code!r} is required for this operation")
        return user

    _dependency.__name__ = f"require_permission_{code.replace('.', '_')}"
    return _dependency


def request_id_of(request: Request) -> str:
    """Return the request id stamped by the logging middleware."""
    return getattr(request.state, "request_id", "") or ""
