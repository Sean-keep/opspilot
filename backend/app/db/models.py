"""Import every ORM model so ``Base.metadata`` is complete.

Alembic and the tests import this module rather than reaching into individual
feature packages. Adding a table means adding it in its feature package and
importing it here — that keeps module boundaries intact while still giving
migrations one place to look.

The task-engine models live in :mod:`shared.task_protocol.models` because the
worker writes them too and must not import ``backend.app``. They are still
listed here so ``Base.metadata`` sees them.
"""

from __future__ import annotations

from shared.task_protocol.models import (
    ApiIdempotency,
    LeaseRecovery,
    OutboxEvent,
    Task,
    TaskLog,
    TaskRun,
    TaskStep,
)

from backend.app.audit.models import AuditLog
from backend.app.auth.models import (
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from backend.app.credentials.models import Credential
from backend.app.db.base import Base
from backend.app.inventory.models import (
    Server,
    ServerFacts,
    ServerGroup,
    ServerGroupMember,
)

__all__ = [
    "ApiIdempotency",
    "AuditLog",
    "Base",
    "Credential",
    "LeaseRecovery",
    "OutboxEvent",
    "Permission",
    "Role",
    "RolePermission",
    "Server",
    "ServerFacts",
    "ServerGroup",
    "ServerGroupMember",
    "Task",
    "TaskLog",
    "TaskRun",
    "TaskStep",
    "User",
    "UserRole",
]
