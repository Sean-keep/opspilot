"""ORM models for the task engine.

These tables are written by the API (create / cancel / retry) and by the worker
(claim / execute / recover), so they live in ``shared/`` — the worker must not
import ``backend.app``. Migrations use hand-written DDL; this module is what
the two processes talk to at runtime.

Three unique constraints are load-bearing for concurrency and must never be
softened:

* ``ops_tasks.idempotency_key``            — one submit, one task
* ``ops_task_runs (task_id, server_id)``   — a retry bumps ``attempt``, never inserts
* ``ops_task_steps (run_id, step_key, attempt)`` — one record per try
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from shared.dbtypes import (
    Base,
    BigIntPk,
    Name,
    ShortStatus,
    TimestampMixin,
    UTCDateTime,
    utcnow,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    pass


class Task(Base, TimestampMixin):
    """One unit of work requested by an operator, possibly across N hosts."""

    __tablename__ = "ops_tasks"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ops_tasks_idempotency_key"),
        Index("ix_ops_tasks_status_created", "status", "created_at"),
        Index("ix_ops_tasks_requested_by", "requested_by"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    # Client-supplied `Idempotency-Key`. NULL = "no dedupe requested" — and
    # NULL is the only value a UNIQUE index allows more than once, which is why
    # the absence of a key is encoded that way rather than as ''.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, default=None
    )
    # What to do, e.g. "server.check", "template.apply", "deployment.publish".
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Name, nullable=False, default="")
    status: Mapped[str] = mapped_column(ShortStatus, nullable=False, default="PENDING")
    # Validated inputs for the action. No secrets: those are `secret_ref` handles.
    params_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Frozen at submit time. Group membership changing later must not retarget
    # a task that is already queued.
    target_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    requested_by: Mapped[int | None] = mapped_column(BigIntPk, nullable=True)
    requested_by_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    queued_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancel_requested_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancel_requested_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # Set when this task is a retry of an earlier one.
    retry_of_task_id: Mapped[int | None] = mapped_column(BigIntPk, nullable=True)


class TaskRun(Base, TimestampMixin):
    """One task on exactly one server. Retries bump ``attempt`` on this row."""

    __tablename__ = "ops_task_runs"
    __table_args__ = (
        UniqueConstraint("task_id", "server_id", name="uq_ops_task_runs_task_server"),
        Index("ix_ops_task_runs_status_lease", "status", "lease_expires_at"),
        Index("ix_ops_task_runs_task", "task_id"),
        Index("ix_ops_task_runs_server", "server_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigIntPk, ForeignKey("ops_tasks.id", name="fk_ops_task_runs_task", ondelete="CASCADE"),
        nullable=False,
    )
    server_id: Mapped[int] = mapped_column(BigIntPk, nullable=False)
    # Hostname / port / credential_ref / host_key_fingerprint at submit time.
    server_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(ShortStatus, nullable=False, default="WAITING")
    # Increments only on a real re-run. Never on lease renewal or recovery.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_owner: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Why the run ended up UNKNOWN / was re-run. Free text, redacted on write.
    recovery_note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class TaskStep(Base, TimestampMixin):
    """One action inside one run. ``(run_id, step_key, attempt)`` is unique."""

    __tablename__ = "ops_task_steps"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "step_key", "attempt",
            name="uq_ops_task_steps_run_key_attempt",
        ),
        Index("ix_ops_task_steps_run", "run_id"),
        Index("ix_ops_task_steps_run_status", "run_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigIntPk, ForeignKey("ops_task_runs.id", name="fk_ops_task_steps_run", ondelete="CASCADE"),
        nullable=False,
    )
    # Stable name of the step within the plan, e.g. "install_packages".
    step_key: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(ShortStatus, nullable=False, default="PENDING")
    # SAFE / CONDITIONAL / DESTRUCTIVE — copied from the Action declaration so
    # recovery can decide without re-loading the registry.
    idempotency_class: Mapped[str] = mapped_column(
        String(32), nullable=False, default="DESTRUCTIVE"
    )
    params_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # For a cancel this carries cancel_requested_at / remote_signal_sent /
    # remote_exit_after_signal. Never secrets.
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TaskLog(Base):
    """Append-only execution log lines. Polled for history, streamed over SSE."""

    __tablename__ = "ops_task_logs"
    __table_args__ = (
        Index("ix_ops_task_logs_run_seq", "run_id", "seq"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigIntPk, ForeignKey("ops_task_runs.id", name="fk_ops_task_logs_run", ondelete="CASCADE"),
        nullable=False,
    )
    step_id: Mapped[int | None] = mapped_column(BigIntPk, nullable=True)
    # Monotonic per run. The SSE stream tails on this, not on wall-clock time.
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ts: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    # stdout | stderr | system
    stream: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")


class OutboxEvent(Base, TimestampMixin):
    """Work that must be picked up, written in the same transaction as its cause.

    The worker polls this table. Nothing is ever "published" to a broker that
    could lose the message — the row and the task commit together or not at all.
    """

    __tablename__ = "ops_outbox"
    __table_args__ = (
        Index("ix_ops_outbox_pending", "delivered_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ApiIdempotency(Base, TimestampMixin):
    """Request-level dedupe for ``Idempotency-Key``.

    Same key + same body  -> replay the stored response.
    Same key + other body -> 409, because the client is confused.
    """

    __tablename__ = "ops_api_idempotency"
    __table_args__ = (
        UniqueConstraint("scope", "idempotency_key", name="uq_ops_api_idempotency_scope_key"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    # e.g. "POST /api/v1/tasks"
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # SHA-256 of the normalised request body.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    response_status: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class LeaseRecovery(Base, TimestampMixin):
    """What the recovery pass decided about a run whose lease expired.

    Kept as its own rows so "who re-ran this and why" survives past the run's
    own ``recovery_note``, which gets overwritten on the next attempt.
    """

    __tablename__ = "ops_lease_recoveries"
    __table_args__ = (
        Index("ix_ops_lease_recoveries_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigIntPk,
        ForeignKey("ops_task_runs.id", name="fk_ops_lease_recoveries_run", ondelete="CASCADE"),
        nullable=False,
    )
    # renewed | requeued | marked_unknown | left_alone
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    # What the remote probe said. Redacted on write.
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
