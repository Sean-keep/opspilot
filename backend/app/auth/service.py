"""Authentication and permission resolution.

Everything that touches ``ops_users`` lives here. The HTTP layer in
``backend.app.auth.router`` stays thin so the same logic can be exercised from
tests and from the worker.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.app.auth.models import (
    USER_ACTIVE,
    USER_LOCKED,
    Permission,
    Role,
    User,
)
from backend.app.auth.schemas import RoleOut, UserCreate, UserOut
from backend.app.core.errors import Conflict, Forbidden, Unauthenticated, ValidationError
from backend.app.core.security import (
    hash_password,
    verify_password_with_upgrade,
)
from backend.app.db.base import utcnow

# A throwaway hash used to equalise timing when an account does not exist.
# Generated once at import so login cannot be used to enumerate usernames.
_DUMMY_HASH = hash_password("opspilot-timing-equaliser-not-a-real-secret")


class AuthService:
    """Login, user management, and permission lookup."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_user_by_username(self, username: str) -> User | None:
        stmt = (
            select(User)
            .where(User.username == username)
            .options(selectinload(User.roles).selectinload(Role.permissions))
        )
        return self.session.scalar(stmt)

    def get_user_by_id(self, user_id: int) -> User | None:
        stmt = (
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.roles).selectinload(Role.permissions))
        )
        return self.session.scalar(stmt)

    def permissions_for(self, user: User) -> frozenset[str]:
        """Union of every permission granted through the user's roles."""
        codes: set[str] = set()
        for role in user.roles:
            for perm in role.permissions:
                codes.add(perm.code)
        return frozenset(codes)

    def to_out(self, user: User) -> UserOut:
        """Project a :class:`User` into the API shape (never leaks the hash)."""
        return UserOut(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            status=user.status,
            roles=[
                RoleOut(id=r.id, name=r.name, description=r.description) for r in user.roles
            ],
            permissions=sorted(self.permissions_for(user)),
            last_login_at=user.last_login_at,
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def create_user(self, payload: UserCreate) -> User:
        if self.get_user_by_username(payload.username) is not None:
            raise Conflict(f"username {payload.username!r} is already taken")

        try:
            hashed = hash_password(payload.password)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        user = User(
            username=payload.username,
            display_name=payload.display_name or payload.username,
            email=payload.email,
            password_hash=hashed,
            status=USER_ACTIVE,
            failed_login_count=0,
        )
        self.session.add(user)

        if payload.role_names:
            roles = self._load_roles(payload.role_names)
            user.roles.extend(roles)
        self.session.flush()
        return user

    def _load_roles(self, names: list[str]) -> list[Role]:
        stmt = select(Role).where(Role.name.in_(names)).options(selectinload(Role.permissions))
        found = list(self.session.scalars(stmt))
        missing = sorted(set(names) - {r.name for r in found})
        if missing:
            raise ValidationError(f"unknown roles: {', '.join(missing)}")
        return found

    def grant_role(self, user: User, role_name: str) -> None:
        roles = self._load_roles([role_name])
        if roles[0] not in user.roles:
            user.roles.append(roles[0])
        self.session.flush()

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------
    def authenticate(self, username: str, password: str) -> User:
        """Verify credentials and return the user, or raise.

        Failure paths are deliberately indistinguishable to the caller: a
        missing account and a wrong password both raise the same
        :class:`Unauthenticated`, so the endpoint cannot be used to enumerate
        usernames.
        """
        user = self.get_user_by_username(username)
        generic = Unauthenticated("invalid username or password")

        if user is None:
            # Burn comparable time against a throwaway hash so a missing user is
            # not measurably faster to reject than a wrong password.
            verify_password_with_upgrade(password, _DUMMY_HASH)
            raise generic

        if user.status == USER_LOCKED:
            raise Unauthenticated("account is locked; contact an administrator")
        if user.status != USER_ACTIVE:
            raise Unauthenticated("account is disabled")

        ok, upgraded = verify_password_with_upgrade(password, user.password_hash)
        if not ok:
            user.failed_login_count = int(user.failed_login_count or 0) + 1
            self.session.flush()
            raise generic

        if upgraded is not None:
            user.password_hash = upgraded

        user.failed_login_count = 0
        user.last_login_at = utcnow()
        self.session.flush()
        return user

    def change_password(self, user: User, new_password: str) -> None:
        try:
            user.password_hash = hash_password(new_password)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        self.session.flush()

    # ------------------------------------------------------------------
    # Authorisation
    # ------------------------------------------------------------------
    def require_permission(self, user: User, code: str) -> None:
        """Raise :class:`Forbidden` unless *user* holds permission *code*.

        This is the security boundary. The frontend hiding a button is not.
        """
        if code in self.permissions_for(user):
            return
        raise Forbidden(f"permission {code!r} is required for this operation")

    def has_permission(self, user: User, code: str) -> bool:
        return code in self.permissions_for(user)


def bootstrap_permissions(session: Session, codes: dict[str, str]) -> list[Permission]:
    """Ensure every permission *code* exists with its description.

    Idempotent: existing rows are left alone so a later description change in
    code does not silently rewrite what an operator agreed to.
    """
    existing = {p.code: p for p in session.scalars(select(Permission))}
    created: list[Permission] = []
    for code, description in codes.items():
        if code in existing:
            continue
        perm = Permission(code=code, description=description)
        session.add(perm)
        created.append(perm)
    if created:
        session.flush()
    return created


def bootstrap_roles(session: Session, names: dict[str, str]) -> list[Role]:
    """Ensure each role exists, creating it when missing. Idempotent."""
    existing = {r.name: r for r in session.scalars(select(Role))}
    created: list[Role] = []
    for name, description in names.items():
        if name in existing:
            continue
        role = Role(name=name, description=description)
        session.add(role)
        created.append(role)
    if created:
        session.flush()
    return created


def attach_role_permissions(
    session: Session, role_name: str, permission_codes: list[str]
) -> Role:
    """Grant *permission_codes* to the named role. Idempotent."""
    role = session.scalar(
        select(Role).where(Role.name == role_name).options(selectinload(Role.permissions))
    )
    if role is None:
        raise ValidationError(f"unknown role: {role_name}")
    wanted = set(permission_codes)
    current = {p.code for p in role.permissions}
    if wanted <= current:
        return role
    perms = list(session.scalars(select(Permission).where(Permission.code.in_(wanted))))
    for perm in perms:
        if perm.code not in current:
            role.permissions.append(perm)
    session.flush()
    return role


def now_utc() -> dt.datetime:
    return utcnow()
