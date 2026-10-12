"""Task engine: tasks, runs, steps, logs, outbox, and request idempotency.

Tables added:

* ``ops_tasks``            — one operator request, possibly across N hosts
* ``ops_task_runs``        — one task on one server; retries bump ``attempt``
* ``ops_task_steps``       — one action inside a run
* ``ops_task_logs``        — append-only execution log lines
* ``ops_outbox``           — work written in the same transaction as its cause
* ``ops_api_idempotency``  — ``Idempotency-Key`` replay store
* ``ops_lease_recoveries`` — what the recovery pass decided, and why

Three unique constraints are load-bearing for concurrency and must not be
softened:

* ``ops_tasks.idempotency_key``
* ``ops_task_runs (task_id, server_id)``
* ``ops_task_steps (run_id, step_key, attempt)``

Conventions match revisions ``0001``/``0002``: InnoDB, utf8mb4, BIGINT
UNSIGNED keys, DATETIME(3) timestamps written in UTC. No data migration and no
destructive DDL. Safe to run twice — Alembic records the revision.

Revision ID: 0003_task_engine
Revises: 0002_inventory_and_credentials
Create Date: 2026-10-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0003_task_engine"
down_revision: str | None = "0002_inventory_and_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT_UNSIGNED = mysql.BIGINT(unsigned=True)
DATETIME3 = mysql.DATETIME(fsp=3)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    op.create_table(
        "ops_tasks",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        # Client `Idempotency-Key`. Empty string = no dedupe requested.
        sa.Column("idempotency_key", sa.String(128), nullable=False, server_default=""),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("title", sa.String(128), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("params_json", mysql.JSON, nullable=True),
        # Frozen at submit time: later group changes must not retarget a task.
        sa.Column("target_snapshot_json", mysql.JSON, nullable=True),
        sa.Column("requested_by", BIGINT_UNSIGNED, nullable=True),
        sa.Column("requested_by_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("queued_at", DATETIME3, nullable=True),
        sa.Column("started_at", DATETIME3, nullable=True),
        sa.Column("finished_at", DATETIME3, nullable=True),
        sa.Column("cancel_requested_at", DATETIME3, nullable=True),
        sa.Column("cancel_requested_by", sa.String(128), nullable=False, server_default=""),
        sa.Column("retry_of_task_id", BIGINT_UNSIGNED, nullable=True),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_tasks"),
        sa.UniqueConstraint("idempotency_key", name="uq_ops_tasks_idempotency_key"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ops_tasks_status_created", "ops_tasks", ["status", "created_at"])
    op.create_index("ix_ops_tasks_requested_by", "ops_tasks", ["requested_by"])

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    op.create_table(
        "ops_task_runs",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("task_id", BIGINT_UNSIGNED, nullable=False),
        sa.Column("server_id", BIGINT_UNSIGNED, nullable=False),
        sa.Column("server_snapshot_json", mysql.JSON, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="WAITING"),
        # Increments only on a real re-run — never on claim or heartbeat.
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("lease_owner", sa.String(128), nullable=False, server_default=""),
        sa.Column("lease_expires_at", DATETIME3, nullable=True),
        sa.Column("heartbeat_at", DATETIME3, nullable=True),
        sa.Column("started_at", DATETIME3, nullable=True),
        sa.Column("finished_at", DATETIME3, nullable=True),
        sa.Column("error_detail", sa.Text, nullable=False),
        sa.Column("recovery_note", sa.Text, nullable=False),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_task_runs"),
        sa.UniqueConstraint("task_id", "server_id", name="uq_ops_task_runs_task_server"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["ops_tasks.id"],
            name="fk_ops_task_runs_task", ondelete="CASCADE",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ops_task_runs_status_lease", "ops_task_runs", ["status", "lease_expires_at"])
    op.create_index("ix_ops_task_runs_task", "ops_task_runs", ["task_id"])
    op.create_index("ix_ops_task_runs_server", "ops_task_runs", ["server_id"])

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    op.create_table(
        "ops_task_steps",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("run_id", BIGINT_UNSIGNED, nullable=False),
        sa.Column("step_key", sa.String(128), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        # Copied from the Action so recovery never has to re-load the registry.
        sa.Column("idempotency_class", sa.String(32), nullable=False, server_default="DESTRUCTIVE"),
        sa.Column("params_json", mysql.JSON, nullable=True),
        # Cancel evidence lives here: cancel_requested_at / remote_signal_sent /
        # remote_exit_after_signal.
        sa.Column("result_json", mysql.JSON, nullable=True),
        sa.Column("started_at", DATETIME3, nullable=True),
        sa.Column("finished_at", DATETIME3, nullable=True),
        sa.Column("exit_code", sa.Integer, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_task_steps"),
        sa.UniqueConstraint(
            "run_id", "step_key", "attempt",
            name="uq_ops_task_steps_run_key_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["ops_task_runs.id"],
            name="fk_ops_task_steps_run", ondelete="CASCADE",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ops_task_steps_run", "ops_task_steps", ["run_id"])
    op.create_index("ix_ops_task_steps_run_status", "ops_task_steps", ["run_id", "status"])

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------
    op.create_table(
        "ops_task_logs",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("run_id", BIGINT_UNSIGNED, nullable=False),
        sa.Column("step_id", BIGINT_UNSIGNED, nullable=True),
        # Monotonic per run. SSE tails on this, not on wall-clock time.
        sa.Column("seq", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ts", DATETIME3, nullable=False),
        sa.Column("level", sa.String(16), nullable=False, server_default="info"),
        sa.Column("stream", sa.String(16), nullable=False, server_default="system"),
        sa.Column("message", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_task_logs"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["ops_task_runs.id"],
            name="fk_ops_task_logs_run", ondelete="CASCADE",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ops_task_logs_run_seq", "ops_task_logs", ["run_id", "seq"])

    # ------------------------------------------------------------------
    # Outbox
    # ------------------------------------------------------------------
    op.create_table(
        "ops_outbox",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("payload_json", mysql.JSON, nullable=True),
        sa.Column("delivered_at", DATETIME3, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_outbox"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ops_outbox_pending", "ops_outbox", ["delivered_at", "id"])

    # ------------------------------------------------------------------
    # Request idempotency
    # ------------------------------------------------------------------
    op.create_table(
        "ops_api_idempotency",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("response_status", sa.Integer, nullable=False, server_default="200"),
        sa.Column("response_body", mysql.JSON, nullable=True),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_api_idempotency"),
        sa.UniqueConstraint("scope", "idempotency_key", name="uq_ops_api_idempotency_scope_key"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )

    # ------------------------------------------------------------------
    # Lease recovery decisions
    # ------------------------------------------------------------------
    op.create_table(
        "ops_lease_recoveries",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("run_id", BIGINT_UNSIGNED, nullable=False),
        # renewed | requeued | marked_unknown | skipped
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("evidence", mysql.JSON, nullable=True),
        sa.Column("decided_by", sa.String(128), nullable=False, server_default=""),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_lease_recoveries"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["ops_task_runs.id"],
            name="fk_ops_lease_recoveries_run", ondelete="CASCADE",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ops_lease_recoveries_run", "ops_lease_recoveries", ["run_id"])


def downgrade() -> None:
    # Children first so the foreign keys drop cleanly.
    op.drop_table("ops_lease_recoveries")
    op.drop_table("ops_api_idempotency")
    op.drop_table("ops_outbox")
    op.drop_table("ops_task_logs")
    op.drop_table("ops_task_steps")
    op.drop_table("ops_task_runs")
    op.drop_table("ops_tasks")
