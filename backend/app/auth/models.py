"""Identity, roles, and permissions.

Five tables, exactly as the database design section specifies:

* ``ops_users`` — platform accounts
* ``ops_roles`` — named roles
* ``ops_permissions`` — the permission catalogue (stable ``code`` strings)
* ``ops_user_roles`` — user ↔ role
* ``ops_role_permissions`` — role ↔ permission

A permission is checked in the backend on every dangerous operation. The
frontend may hide controls the user lacks, but that is presentation only.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.base import (
    Base,
    BigIntPk,
    Code,
    Name,
    ShortStatus,
    TimestampMixin,
    UTCDateTime,
)

# User status values. Explicit and short, never free text.
USER_ACTIVE = "active"
USER_DISABLED = "disabled"
USER_LOCKED = "locked"
USER_STATUSES = (USER_ACTIVE, USER_DISABLED, USER_LOCKED)


class User(Base, TimestampMixin):
    """A platform operator account."""

    __tablename__ = "ops_users"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(Name, nullable=False, default="")
    email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(ShortStatus, nullable=False, default=USER_ACTIVE)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    failed_login_count: Mapped[int] = mapped_column(default=0, nullable=False)

    roles: Mapped[list[Role]] = relationship(
        secondary="ops_user_roles", back_populates="users", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User id={self.id} username={self.username!r} status={self.status!r}>"


class Role(Base, TimestampMixin):
    """A named bundle of permissions."""

    __tablename__ = "ops_roles"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Code, unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    users: Mapped[list[User]] = relationship(
        secondary="ops_user_roles", back_populates="roles", lazy="selectin"
    )
    permissions: Mapped[list[Permission]] = relationship(
        secondary="ops_role_permissions", back_populates="roles", lazy="selectin"
    )


class Permission(Base, TimestampMixin):
    """One checkable capability, identified by a stable ``code``.

    Codes are dotted and lower-case, e.g. ``server.read`` or ``task.create``.
    They are the vocabulary used by ``require_permission`` and by the frontend
    permission gate.
    """

    __tablename__ = "ops_permissions"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(Code, unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    roles: Mapped[list[Role]] = relationship(
        secondary="ops_role_permissions", back_populates="permissions", lazy="selectin"
    )


class UserRole(Base):
    """User ↔ role membership. Composite key — no surrogate id needed."""

    __tablename__ = "ops_user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_ops_user_roles_user_role"),)

    user_id: Mapped[int] = mapped_column(
        BigIntPk, ForeignKey("ops_users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[int] = mapped_column(
        BigIntPk, ForeignKey("ops_roles.id", ondelete="CASCADE"), primary_key=True
    )


class RolePermission(Base):
    """Role ↔ permission membership."""

    __tablename__ = "ops_role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_ops_role_permissions_role_perm"),
    )

    role_id: Mapped[int] = mapped_column(
        BigIntPk, ForeignKey("ops_roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[int] = mapped_column(
        BigIntPk, ForeignKey("ops_permissions.id", ondelete="CASCADE"), primary_key=True
    )
