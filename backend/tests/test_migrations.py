"""Migration behaviour.

Two properties are asserted here and nowhere else:

1. ``alembic upgrade head`` is **repeatable** — a second run is a no-op.
2. The resulting schema has the conventions the requirements mandate:
   InnoDB, ``utf8mb4``, ``BIGINT UNSIGNED`` keys, ``DATETIME(3)`` timestamps,
   and the unique constraints later stages depend on.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from backend.app.core.settings import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.integration


def _run_alembic(args: list[str], url: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["ALEMBIC_DATABASE_URL"] = url
    env.setdefault("OPSPILOT_SECRET_KEY", "test-secret-key-not-for-production")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture()
def empty_test_db(settings: Settings) -> str:
    """A database with no tables and no alembic version history."""
    engine = create_engine(settings.test_database_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        rows = conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
        )
        for row in rows:
            conn.execute(text(f"DROP TABLE IF EXISTS `{row[0]}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    engine.dispose()
    return settings.test_database_url


def test_upgrade_head_is_repeatable(empty_test_db: str) -> None:
    first = _run_alembic(["upgrade", "head"], empty_test_db)
    assert first.returncode == 0, f"stdout={first.stdout}\nstderr={first.stderr}"

    second = _run_alembic(["upgrade", "head"], empty_test_db)
    assert second.returncode == 0, f"stdout={second.stdout}\nstderr={second.stderr}"
    # A repeat run must not emit DDL. "Running upgrade" would mean it re-ran.
    assert "Running upgrade" not in second.stdout + second.stderr

    # Current revision must be whatever alembic reports as the head — the
    # assertion stays valid as S2/S3/S4 add migrations.
    heads = _run_alembic(["heads"], empty_test_db)
    assert heads.returncode == 0, heads.stderr
    head_id = heads.stdout.strip().split()[0]
    current = _run_alembic(["current"], empty_test_db)
    assert current.returncode == 0, current.stderr
    assert head_id in current.stdout, (head_id, current.stdout)
    assert "(head)" in current.stdout


def test_schema_conventions(empty_test_db: str) -> None:
    first = _run_alembic(["upgrade", "head"], empty_test_db)
    assert first.returncode == 0, first.stderr

    engine = create_engine(empty_test_db, future=True)
    try:
        with engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = DATABASE()"
                    )
                )
            }
            expected = {
                "ops_users",
                "ops_roles",
                "ops_permissions",
                "ops_user_roles",
                "ops_role_permissions",
                "ops_audit_logs",
                # S1
                "ops_credentials",
                "ops_servers",
                "ops_server_groups",
                "ops_server_group_members",
                "ops_server_facts",
                "alembic_version",
            }
            assert expected <= tables, f"missing: {expected - tables}"

            # Every ops_* table must be InnoDB with utf8mb4.
            # Positional indexing: information_schema result metadata is
            # case-insensitive here and dict-style access is unreliable.
            for row in conn.execute(
                text(
                    "SELECT table_name, engine, table_collation "
                    "FROM information_schema.tables WHERE table_schema = DATABASE()"
                )
            ):
                name = str(row[0])
                if not name.startswith("ops_"):
                    continue
                engine_name = str(row[1])
                collation = str(row[2])
                assert engine_name == "InnoDB", f"{name} engine={engine_name}"
                assert collation.startswith("utf8mb4"), f"{name} collation={collation}"

            # Surrogate keys must be BIGINT UNSIGNED.
            for row in conn.execute(
                text(
                    "SELECT table_name, column_name, column_type, extra "
                    "FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND column_name = 'id'"
                )
            ):
                table = str(row[0])
                if not table.startswith("ops_"):
                    continue
                col_type = str(row[2])
                assert "bigint" in col_type.lower(), f"{table}.id type={col_type}"
                assert "unsigned" in col_type.lower(), f"{table}.id must be unsigned"

            # Timestamps must be DATETIME(3).
            for row in conn.execute(
                text(
                    "SELECT table_name, column_name, column_type "
                    "FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() "
                    "AND column_name IN ('created_at','updated_at','last_login_at')"
                )
            ):
                table = str(row[0])
                if not table.startswith("ops_"):
                    continue
                column = str(row[1])
                col_type = str(row[2])
                assert "datetime" in col_type.lower(), f"{table}.{column} type={col_type}"
                assert "(3)" in col_type, f"{table}.{column} must be DATETIME(3), got {col_type}"
    finally:
        engine.dispose()


def test_unique_constraints_present(empty_test_db: str) -> None:
    first = _run_alembic(["upgrade", "head"], empty_test_db)
    assert first.returncode == 0, first.stderr

    engine = create_engine(empty_test_db, future=True)
    try:
        with engine.connect() as conn:
            # (table, column) pairs covered by a unique index.
            found: set[tuple[str, str]] = set()
            for row in conn.execute(
                text(
                    "SELECT table_name, column_name "
                    "FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() AND non_unique = 0"
                )
            ):
                found.add((str(row[0]), str(row[1])))

            required = {
                ("ops_users", "username"),
                ("ops_roles", "name"),
                ("ops_permissions", "code"),
                ("ops_user_roles", "user_id"),
                ("ops_user_roles", "role_id"),
                ("ops_role_permissions", "role_id"),
                ("ops_role_permissions", "permission_id"),
            }
            missing = required - found
            assert not missing, f"missing unique constraints: {missing}"
    finally:
        engine.dispose()


def test_downgrade_then_upgrade_round_trips(empty_test_db: str) -> None:
    up = _run_alembic(["upgrade", "head"], empty_test_db)
    assert up.returncode == 0, up.stderr

    down = _run_alembic(["downgrade", "base"], empty_test_db)
    assert down.returncode == 0, down.stderr

    engine = create_engine(empty_test_db, future=True)
    try:
        with engine.connect() as conn:
            remaining = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_name LIKE 'ops\\_%'"
                )
            ).fetchall()
            assert remaining == [], f"downgrade left tables behind: {remaining}"
    finally:
        engine.dispose()

    up_again = _run_alembic(["upgrade", "head"], empty_test_db)
    assert up_again.returncode == 0, up_again.stderr
