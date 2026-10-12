"""Template catalog: templates and their immutable versions.

Tables added:

* ``ops_templates``         — the stable identity of a template (its name)
* ``ops_template_versions`` — one row per ``template.yaml`` that ever existed

Two properties are load-bearing and are enforced here plus in the service layer:

* ``(template_id, version)`` is unique. A version number means one thing.
* A version's body (``spec_json`` / ``inputs_schema_json`` / ``checksum``) is
  written **once**, at insert. Only lifecycle fields move afterwards. MySQL
  cannot express "do not UPDATE these columns" without a trigger, so the
  service refuses the mutation and a test proves the body is byte-identical
  after a rejected edit.

Conventions match revisions ``0001``-``0003``: InnoDB, utf8mb4, BIGINT
UNSIGNED keys, DATETIME(3) timestamps written in UTC. No data migration and no
destructive DDL. Safe to run twice — Alembic records the revision.

Revision ID: 0004_templates
Revises: 0003_task_engine
Create Date: 2026-10-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0004_templates"
down_revision: str | None = "0003_task_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT_UNSIGNED = mysql.BIGINT(unsigned=True)
DATETIME3 = mysql.DATETIME(fsp=3)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------
    op.create_table(
        "ops_templates",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        # runtime / web / application / database / middleware
        sa.Column("category", sa.String(32), nullable=False, server_default=""),
        sa.Column("description", sa.String(512), nullable=False, server_default=""),
        # Version number of the newest row in ops_template_versions. A
        # convenience pointer, never an implicit "run this one" — a task must
        # pin a concrete version.
        sa.Column("latest_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_templates"),
        sa.UniqueConstraint("name", name="uq_ops_templates_name"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_ops_templates_category", "ops_templates", ["category"])

    # ------------------------------------------------------------------
    # Template versions
    # ------------------------------------------------------------------
    op.create_table(
        "ops_template_versions",
        sa.Column("id", BIGINT_UNSIGNED, autoincrement=True, nullable=False),
        sa.Column("template_id", BIGINT_UNSIGNED, nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        # DRAFT -> VALIDATED -> TESTED -> PUBLISHED -> DEPRECATED
        sa.Column("status", sa.String(32), nullable=False, server_default="DRAFT"),
        # SHA-256 over the template's identity files. Recomputed at execution
        # time and compared against this; a mismatch refuses the run.
        sa.Column("checksum", sa.String(64), nullable=False, server_default=""),
        # Written once, at insert. Never updated.
        sa.Column("spec_json", mysql.JSON, nullable=False),
        sa.Column("inputs_schema_json", mysql.JSON, nullable=False),
        sa.Column("notes", sa.Text, nullable=False),
        sa.Column("published_at", DATETIME3, nullable=True),
        sa.Column("deprecated_at", DATETIME3, nullable=True),
        sa.Column("created_at", DATETIME3, nullable=False),
        sa.Column("updated_at", DATETIME3, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ops_template_versions"),
        sa.UniqueConstraint(
            "template_id", "version", name="uq_ops_template_versions_template_version"
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["ops_templates.id"],
            name="fk_ops_template_versions_template", ondelete="CASCADE",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_ops_template_versions_status", "ops_template_versions", ["status", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_ops_template_versions_status", table_name="ops_template_versions")
    op.drop_table("ops_template_versions")
    op.drop_index("ix_ops_templates_category", table_name="ops_templates")
    op.drop_table("ops_templates")
