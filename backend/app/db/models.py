"""Import every ORM model so ``Base.metadata`` is complete.

Alembic and the tests import this module rather than reaching into individual
feature packages. Adding a table means adding it in its feature package and
importing it here — that keeps module boundaries intact while still giving
migrations one place to look.
"""

from __future__ import annotations

from backend.app.audit.models import AuditLog
from backend.app.auth.models import (
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from backend.app.db.base import Base

__all__ = [
    "AuditLog",
    "Base",
    "Permission",
    "Role",
    "RolePermission",
    "User",
    "UserRole",
]
