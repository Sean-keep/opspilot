"""Applications, environments, and deployment records.

Tables added:

* ``ops_applications``     — the deployable thing; ``name`` is also its
  directory under ``/opt/opspilot/apps/``
* ``ops_app_environments`` — one target per app (test / staging / production)
  with its own health endpoint, service name and retention
* ``ops_deployments``      — one row per attempt to make a release live

``ops_deployments`` is a record, not a work queue. The work is an
``ops_tasks`` row and ``task_id`` points at it with ``ON DELETE SET NULL``: a
deployment must survive the task it ran on, because "what is live on
production right now, and what was live before it" is exactly the question a
rollback answers after the task logs are gone.

Two columns are the rollback story and are written at creation time so they
cannot be lost later:

* ``previous_release_id`` — what ``current`` pointed at when this started
* ``rollback_of``         — set on a deployment that exists only to undo another

``keep_releases`` on an environment is a retention *hint* for an explicit
cleanup action. Nothing here deletes anything automatically, and
``shared/data`` is never cleaned up by the platform at all.

Conventions match revisions ``0001``-``0004``: InnoDB, utf8mb4, BIGINT
UNSIGNED keys, DATETIME(3) timestamps written in UTC. No data migration and no
destructive DDL. Safe to run twice — Alembic records the revision.

This revision also widens ``alembic_version.version_num`` from 32 to 64
characters. Alembic's default is too narrow for a descriptive revision id, and
the failure mode is that every table in the revision is created and then the
stamp itself is rejected. The widening is deliberately not undone by
``downgrade`` — shrinking it back would only re-arm the trap.

Revision ID: 0005_applications_and_deployments
Revises: 0004_templates
Create Date: 2026-10-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0005_applications_and_deployments"
down_revision: str | None = "0004_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT_UNSIGNED = mysql.BIGINT(unsigned=True)
DATETIME3 = mysql.DATETIME(fsp=3)


def upgrade() -> None:
    # Alembic creates alembic_version with version_num VARCHAR(32). That is
    # narrower than a descriptive revision id, and the failure mode is nasty:
    # every table in the revision is created and then the stamp itself is
    # rejected, leaving a half-applied migration. Widen it once, here.
    op.execute("ALTER TABLE alembic_version MODIFY version_num VARCHAR(64) NOT NULL")

    # ------------------------------------------------------------------
    # Applications
    # ------------------------------------------------------------------
    op.create_table(
        "ops_applications",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        # Directory name under /opt/opspilot/apps/. Unique: two applications
        # cannot share a tree on a host.
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("runtime_type", sa.String(32), nullable=False, server_default="python"),
        sa.Column("repo_url", sa.String(512), nullable=False, server_default=""),
        sa.Column("default_branch", sa.String(128), nullable=False, server_default=""),
        # The non-root account the service runs as. Never root.
        sa.Column("run_user", sa.String(64), nullable=False, server_default=""),
        sa.Column("description", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_applications"),
        sa.UniqueConstraint("name", name="uq_ops_applications_name"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )

    # ------------------------------------------------------------------
    # Environments
    # ------------------------------------------------------------------
    op.create_table(
        "ops_app_environments",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("application_id", BIGINT_UNSIGNED, nullable=False),
        # Closed set: test / staging / production. Free text would make
        # "is this production?" unanswerable at the permission layer.
        sa.Column("name", sa.String(32), nullable=False),
        # A reference to a secret, never the secret. The worker resolves it at
        # execution time and redacts every derived value.
        sa.Column("config_ref", sa.String(255), nullable=False, server_default=""),
        sa.Column("service_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("unit_path", sa.String(255), nullable=False, server_default=""),
        sa.Column("health_url", sa.String(512), nullable=False, server_default=""),
        sa.Column("health_timeout_s", sa.Integer, nullable=False, server_default="10"),
        # Retention hint for an explicit cleanup action. Never automatic.
        sa.Column("keep_releases", sa.Integer, nullable=False, server_default="5"),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_app_environments"),
        sa.UniqueConstraint(
            "application_id", "name", name="uq_ops_app_environments_application_name"
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["ops_applications.id"],
            name="fk_ops_app_environments_application", ondelete="CASCADE",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ops_app_environments_name", "ops_app_environments", ["name"])

    # ------------------------------------------------------------------
    # Deployments
    # ------------------------------------------------------------------
    op.create_table(
        "ops_deployments",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("application_id", BIGINT_UNSIGNED, nullable=False),
        sa.Column("environment_id", BIGINT_UNSIGNED, nullable=False),
        # DEPLOY | ROLLBACK
        sa.Column("kind", sa.String(16), nullable=False, server_default="DEPLOY"),
        # PENDING | RUNNING | SUCCEEDED | FAILED | ROLLED_BACK | CANCELLED
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        # What the operator asked for (a tag, a commit, a build number).
        sa.Column("release_version", sa.String(128), nullable=False),
        # Directory name under releases/. Stable per version, so a re-run is a
        # no-op rather than a second copy of the same release.
        sa.Column("release_id", sa.String(128), nullable=False),
        # What `current` pointed at before this deployment. Empty on a first
        # deploy; this is what a rollback restores.
        sa.Column("previous_release_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("artifact_url", sa.String(512), nullable=False, server_default=""),
        sa.Column("artifact_checksum", sa.String(64), nullable=False, server_default=""),
        # The execution attempt. A deployment outlives its task on purpose.
        sa.Column("task_id", BIGINT_UNSIGNED, nullable=True),
        # Set on a deployment whose only job is to undo another one.
        sa.Column("rollback_of", BIGINT_UNSIGNED, nullable=True),
        sa.Column("requested_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("error_detail", sa.String(512), nullable=False, server_default=""),
        sa.Column("started_at", DATETIME3, nullable=True),
        sa.Column("finished_at", DATETIME3, nullable=True),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_deployments"),
        sa.ForeignKeyConstraint(
            ["application_id"], ["ops_applications.id"],
            name="fk_ops_deployments_application", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["ops_app_environments.id"],
            name="fk_ops_deployments_environment", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["ops_tasks.id"],
            name="fk_ops_deployments_task", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rollback_of"], ["ops_deployments.id"],
            name="fk_ops_deployments_rollback_of", ondelete="SET NULL",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_ops_deployments_environment_id_id", "ops_deployments", ["environment_id", "id"]
    )
    op.create_index(
        "ix_ops_deployments_application_id_id", "ops_deployments", ["application_id", "id"]
    )
    op.create_index("ix_ops_deployments_status_id", "ops_deployments", ["status", "id"])


def downgrade() -> None:
    # Dropping a table drops its indexes, and that is the only way out here:
    # InnoDB binds ``fk_ops_deployments_application`` to the leftmost prefix of
    # ``ix_ops_deployments_application_id_id`` rather than making an index of
    # its own, so a standalone DROP INDEX is rejected with "needed in a foreign
    # key constraint" before DROP TABLE is ever reached.
    op.drop_table("ops_deployments")
    op.drop_table("ops_app_environments")
    op.drop_table("ops_applications")
