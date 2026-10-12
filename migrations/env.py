"""Alembic environment.

URL comes from ``Settings`` (OPSPILOT_* env vars / .env) — never from a
committed constant. Application models are imported so ``Base.metadata`` is
complete before autogenerate or ``compare_metadata`` runs.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the repo root importable when alembic is invoked from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.app.core.settings import get_settings  # noqa: E402
from backend.app.db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the URL, honouring ``ALEMBIC_DATABASE_URL`` for the test suite."""
    import os

    override = os.environ.get("ALEMBIC_DATABASE_URL")
    if override:
        return override
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit DDL to stdout without a live connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Pin the migration session to UTC so every DATETIME(3) means one thing.
        connection.exec_driver_sql("SET time_zone = '+00:00'")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()

        # MySQL runs DDL non-transactionally ("Will assume non-transactional
        # DDL"). SQLAlchemy 2.x connections are commit-as-you-go, so without an
        # explicit commit the alembic_version row is written into a transaction
        # that is never committed — DDL lands, the version stamp does not, and
        # `alembic downgrade base` then believes there is nothing to reverse.
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
