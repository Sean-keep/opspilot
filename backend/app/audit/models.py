"""Audit log.

Every privileged or dangerous operation writes a row here: who did what, to
which target, with which request id, and what the result was. Audit history is
never destroyed by deleting a server, template, or application — those use soft
delete / status deactivation instead.

Snapshots are passed through :func:`shared.redaction.redact` before they are
stored, so an audit row can never become a secret-leak vector.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Index, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import (
    Base,
    BigIntPk,
    Code,
    Name,
    ShortStatus,
    TimestampMixin,
)

# Result vocabulary. Short, explicit, searchable.
RESULT_SUCCESS = "success"
RESULT_FAILURE = "failure"
RESULT_DENIED = "denied"
RESULTS = (RESULT_SUCCESS, RESULT_FAILURE, RESULT_DENIED)


class AuditLog(Base, TimestampMixin):
    """One audited action."""

    __tablename__ = "ops_audit_logs"
    __table_args__ = (
        Index("ix_ops_audit_logs_actor_created", "actor_id", "created_at"),
        Index("ix_ops_audit_logs_target", "target_type", "target_id"),
        Index("ix_ops_audit_logs_request", "request_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)

    # Who. actor_id is nullable only for system-initiated actions (worker, migrations).
    actor_id: Mapped[int | None] = mapped_column(BigIntPk, nullable=True, index=True)
    actor_name: Mapped[str] = mapped_column(Name, nullable=False, default="")

    # What.
    action: Mapped[str] = mapped_column(Code, nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(Code, nullable=False, default="")
    target_id: Mapped[str] = mapped_column(Code, nullable=False, default="")

    # Outcome.
    result: Mapped[str] = mapped_column(ShortStatus, nullable=False, default=RESULT_SUCCESS)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Correlation.
    request_id: Mapped[str] = mapped_column(Code, nullable=False, default="")
    # Related task, when the action is a task-scoped operation.
    task_id: Mapped[int | None] = mapped_column(BigIntPk, nullable=True, index=True)

    # Redacted structured context. Never raw parameters.
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
