"""Initial schema: identity (users/roles/permissions) and audit log.

Conventions used here and in every later revision:

* InnoDB + ``utf8mb4`` / ``utf8mb4_0900_ai_ci``
* primary keys ``BIGINT UNSIGNED AUTO_INCREMENT``
* timestamps ``DATETIME(3)`` written in UTC (the session is pinned to
  ``time_zone='+00:00'`` by ``migrations/env.py``)
* status columns are short constrained strings

This revision is safe to run twice: a second ``alembic upgrade head`` is a
no-op because Alembic records the revision. There is no data migration and no
destructive DDL.

Revision ID: 0001_initial_auth_and_audit
Revises:
Create Date: 2026-10-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0001_initial_auth_and_audit"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# BIGINT UNSIGNED for every surrogate key.
BIGINT_UNSIGNED = mysql.BIGINT(unsigned=True)
DATETIME3 = mysql.DATETIME(fsp=3)


def upgrade() -> None:
    op.create_table(
        "ops_users",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("email", sa.String(255), nullable=False, server_default=""),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("last_login_at", DATETIME3, nullable=True),
        sa.Column("failed_login_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_users"),
        sa.UniqueConstraint("username", name="uq_ops_users_username"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ops_users_username", "ops_users", ["username"], unique=False)
    op.create_index("ix_ops_users_status", "ops_users", ["status"], unique=False)

    op.create_table(
        "ops_roles",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_roles"),
        sa.UniqueConstraint("name", name="uq_ops_roles_name"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )

    op.create_table(
        "ops_permissions",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_permissions"),
        sa.UniqueConstraint("code", name="uq_ops_permissions_code"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ops_permissions_code", "ops_permissions", ["code"], unique=False)

    op.create_table(
        "ops_user_roles",
        sa.Column("user_id", BIGINT_UNSIGNED, nullable=False),
        sa.Column("role_id", BIGINT_UNSIGNED, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "role_id", name="pk_ops_user_roles"),
        sa.ForeignKeyConstraint(["user_id"], ["ops_users.id"], name="fk_ops_user_roles_user",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["ops_roles.id"], name="fk_ops_user_roles_role",
                                ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "role_id", name="uq_ops_user_roles_user_role"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )

    op.create_table(
        "ops_role_permissions",
        sa.Column("role_id", BIGINT_UNSIGNED, nullable=False),
        sa.Column("permission_id", BIGINT_UNSIGNED, nullable=False),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name="pk_ops_role_permissions"),
        sa.ForeignKeyConstraint(["role_id"], ["ops_roles.id"], name="fk_ops_role_permissions_role",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["ops_permissions.id"],
            name="fk_ops_role_permissions_permission", ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "role_id", "permission_id", name="uq_ops_role_permissions_role_perm"
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )

    op.create_table(
        "ops_audit_logs",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("actor_id", BIGINT_UNSIGNED, nullable=True),
        sa.Column("actor_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False, server_default=""),
        sa.Column("target_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("result", sa.String(32), nullable=False, server_default="success"),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("task_id", BIGINT_UNSIGNED, nullable=True),
        sa.Column("snapshot", mysql.JSON, nullable=True),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_audit_logs"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_ops_audit_logs_actor_created", "ops_audit_logs", ["actor_id", "created_at"]
    )
    op.create_index(
        "ix_ops_audit_logs_target", "ops_audit_logs", ["target_type", "target_id"]
    )
    op.create_index("ix_ops_audit_logs_request", "ops_audit_logs", ["request_id"])
    op.create_index("ix_ops_audit_logs_action", "ops_audit_logs", ["action"])
    op.create_index("ix_ops_audit_logs_task", "ops_audit_logs", ["task_id"])


def downgrade() -> None:
    # Reverse dependency order. Destructive — only ever run against a scratch DB.
    op.drop_table("ops_audit_logs")
    op.drop_table("ops_role_permissions")
    op.drop_table("ops_user_roles")
    op.drop_table("ops_permissions")
    op.drop_table("ops_roles")
    op.drop_table("ops_users")
