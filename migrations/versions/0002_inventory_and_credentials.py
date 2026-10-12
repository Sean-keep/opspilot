"""Inventory and credential storage.

Tables added:

* ``ops_credentials``  — encrypted secret payloads plus safe-to-show metadata
* ``ops_servers``      — managed hosts, including SSH host-key pinning state
* ``ops_server_groups`` / ``ops_server_group_members`` — grouping
* ``ops_server_facts`` — append-only snapshots of system facts

Conventions match revision ``0001``: InnoDB, utf8mb4, BIGINT UNSIGNED keys,
DATETIME(3) timestamps written in UTC.

No data migration and no destructive DDL. Safe to run twice — Alembic records
the revision, so a second ``upgrade head`` is a no-op.

Revision ID: 0002_inventory_and_credentials
Revises: 0001_initial_auth_and_audit
Create Date: 2026-10-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0002_inventory_and_credentials"
down_revision: str | None = "0001_initial_auth_and_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT_UNSIGNED = mysql.BIGINT(unsigned=True)
DATETIME3 = mysql.DATETIME(fsp=3)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Credentials first: ops_servers has a foreign key into it.
    # ------------------------------------------------------------------
    op.create_table(
        "ops_credentials",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="ssh_key"),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("secret_ref", sa.String(64), nullable=False, server_default=""),
        # Fernet ciphertext. Never selected into an API response.
        sa.Column("payload_encrypted", sa.Text, nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False, server_default=""),
        sa.Column("meta_json", mysql.JSON, nullable=True),
        sa.Column("used_at", DATETIME3, nullable=True),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_credentials"),
        sa.UniqueConstraint("name", name="uq_ops_credentials_name"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ops_credentials_secret_ref", "ops_credentials", ["secret_ref"])
    op.create_index("ix_ops_credentials_status", "ops_credentials", ["status"])

    # ------------------------------------------------------------------
    # Servers
    # ------------------------------------------------------------------
    op.create_table(
        "ops_servers",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer, nullable=False, server_default="22"),
        sa.Column("ssh_user", sa.String(64), nullable=False, server_default=""),
        sa.Column("credential_id", BIGINT_UNSIGNED, nullable=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("tags", mysql.JSON, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        # SSH host-key pinning. Commands run only when state = 'verified' AND
        # the offered key still matches the pinned fingerprint.
        sa.Column("host_key_algorithm", sa.String(32), nullable=False, server_default=""),
        sa.Column("host_key_fingerprint", sa.String(128), nullable=False, server_default=""),
        sa.Column("host_key_state", sa.String(32), nullable=False, server_default="unverified"),
        sa.Column("host_key_accepted_at", DATETIME3, nullable=True),
        sa.Column("last_check_at", DATETIME3, nullable=True),
        sa.Column("last_check_status", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("last_check_detail", sa.Text, nullable=False),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_servers"),
        sa.UniqueConstraint("name", name="uq_ops_servers_name"),
        sa.ForeignKeyConstraint(
            ["credential_id"], ["ops_credentials.id"],
            name="fk_ops_servers_credential", ondelete="SET NULL",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ops_servers_name", "ops_servers", ["name"])
    op.create_index("ix_ops_servers_credential", "ops_servers", ["credential_id"])
    op.create_index("ix_ops_servers_status_created", "ops_servers", ["status", "created_at"])
    op.create_index("ix_ops_servers_host_key_state", "ops_servers", ["host_key_state"])

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------
    op.create_table(
        "ops_server_groups",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_server_groups"),
        sa.UniqueConstraint("name", name="uq_ops_server_groups_name"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )

    op.create_table(
        "ops_server_group_members",
        sa.Column("server_id", BIGINT_UNSIGNED, nullable=False),
        sa.Column("group_id", BIGINT_UNSIGNED, nullable=False),
        sa.PrimaryKeyConstraint("server_id", "group_id", name="pk_ops_server_group_members"),
        sa.ForeignKeyConstraint(
            ["server_id"], ["ops_servers.id"],
            name="fk_ops_server_group_members_server", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["ops_server_groups.id"],
            name="fk_ops_server_group_members_group", ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "server_id", "group_id", name="uq_ops_server_group_members_pair"
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )

    # ------------------------------------------------------------------
    # Facts (append-only snapshots)
    # ------------------------------------------------------------------
    op.create_table(
        "ops_server_facts",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("server_id", BIGINT_UNSIGNED, nullable=False),
        sa.Column("collected_at", DATETIME3, nullable=False),
        sa.Column("os_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("os_version", sa.String(128), nullable=False, server_default=""),
        sa.Column("kernel", sa.String(128), nullable=False, server_default=""),
        sa.Column("arch", sa.String(32), nullable=False, server_default=""),
        sa.Column("cpu_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cpu_model", sa.String(128), nullable=False, server_default=""),
        sa.Column("memory_total_mb", sa.Integer, nullable=False, server_default="0"),
        sa.Column("memory_available_mb", sa.Integer, nullable=False, server_default="0"),
        sa.Column("disk_total_gb", sa.Float, nullable=False, server_default="0"),
        sa.Column("disk_available_gb", sa.Float, nullable=False, server_default="0"),
        # Redacted raw output. Never secrets — see shared/redaction.py.
        sa.Column("raw_json", mysql.JSON, nullable=True),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_server_facts"),
        sa.ForeignKeyConstraint(
            ["server_id"], ["ops_servers.id"],
            name="fk_ops_server_facts_server", ondelete="CASCADE",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_ops_server_facts_server_collected", "ops_server_facts", ["server_id", "collected_at"]
    )


def downgrade() -> None:
    # Reverse dependency order. Destructive — only ever run against a scratch DB.
    op.drop_table("ops_server_facts")
    op.drop_table("ops_server_group_members")
    op.drop_table("ops_server_groups")
    op.drop_table("ops_servers")
    op.drop_table("ops_credentials")
