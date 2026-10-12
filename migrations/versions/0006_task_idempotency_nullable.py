"""Make ``ops_tasks.idempotency_key`` mean "absent" instead of "empty".

``0003`` declared the column ``NOT NULL DEFAULT ''`` with a UNIQUE constraint.
In InnoDB a UNIQUE index treats NULLs as distinct but equal strings as one
value, so that schema allows exactly **one** task without an
``Idempotency-Key`` for the entire lifetime of the database. The second
keyless submit fails with a duplicate-key error at insert time.

That is a schema bug, not a caller bug: "no key supplied" is a legitimate and
common case — the API's ``Idempotency-Key`` header is optional by design. The
correct encoding is NULL, because a UNIQUE index permits any number of NULLs.

So the column becomes nullable and every existing ``''`` is rewritten to NULL.
The UNIQUE constraint is left exactly as it is: it is already correct once
empty strings are gone, and dropping and re-adding it would only put the same
index back.

``downgrade`` restores ``NOT NULL DEFAULT ''`` but deliberately does **not**
rewrite NULLs back to ``''`` — doing so would immediately re-introduce the
duplicate-key failure this revision fixes, for as many rows as had no key.

Revision ID: 0006_task_idempotency_nullable
Revises: 0005_applications_and_deployments
Create Date: 2026-10-12
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0006_task_idempotency_nullable"
down_revision = "0005_applications_and_deployments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ops_tasks MODIFY idempotency_key VARCHAR(128) NULL DEFAULT NULL"
    )
    op.execute("UPDATE ops_tasks SET idempotency_key = NULL WHERE idempotency_key = ''")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE ops_tasks MODIFY idempotency_key VARCHAR(128) NOT NULL DEFAULT ''"
    )
    # Not rewritten back to '' on purpose — see the module docstring.
