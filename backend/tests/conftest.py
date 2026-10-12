"""Shared fixtures.

Integration tests run against a **real MySQL** database (``opspilot_test``).
SQLite is not a substitute here: the schema relies on ``FOR UPDATE SKIP
LOCKED``, ``utf8mb4`` collation, ``DATETIME(3)``, and ``BIGINT UNSIGNED``, none
of which behave the same way on SQLite. When MySQL is unreachable the
integration tests are skipped rather than silently mocked green.
"""

from __future__ import annotations

import base64
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Keys for the optional live SSH target. Test-scoped on purpose: they are not
# part of the production Settings model, and they are never read by the app.
SSH_TEST_ENV_KEYS = (
    "OPSPILOT_TEST_SSH_HOST",
    "OPSPILOT_TEST_SSH_PORT",
    "OPSPILOT_TEST_SSH_USER",
    "OPSPILOT_TEST_SSH_KEY_FILE",
)


def _load_ssh_test_env() -> None:
    """Expose ``OPSPILOT_TEST_SSH_*`` from ``.env`` to ``os.environ``.

    ``pydantic-settings`` only feeds ``Settings``; these keys are read directly
    by the live-transport fixtures. Loading them here keeps a plain ``pytest``
    working once the operator has filled in ``.env`` — no wrapper script
    required, and no magic that the production process would inherit.
    """
    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        return
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - dotenv ships with pydantic-settings
        return
    values = dotenv_values(env_file)
    for key in SSH_TEST_ENV_KEYS:
        if not os.environ.get(key) and values.get(key):
            os.environ[key] = str(values[key])


_load_ssh_test_env()

from backend.app.auth.permissions import DEFAULT_ROLES, PERMISSION_CATALOG  # noqa: E402
from backend.app.auth.schemas import UserCreate  # noqa: E402
from backend.app.auth.service import (  # noqa: E402
    AuthService,
    attach_role_permissions,
    bootstrap_permissions,
    bootstrap_roles,
)
from backend.app.core.settings import Settings, clear_settings_cache, get_settings  # noqa: E402
from backend.app.db import models as _models  # noqa: E402,F401  — register every table
from backend.app.db import session as db_session  # noqa: E402
from backend.app.db.base import Base  # noqa: E402

TEST_DB_NAME = os.environ.get("OPSPILOT_TEST_DB_NAME", "opspilot_test")
BOOTSTRAP_ADMIN = "admin"
BOOTSTRAP_PASSWORD = "test-admin-password-1234"

# A deterministic Fernet key so credential round-trips are reproducible.
# Fernet requires urlsafe-base64 of exactly 32 raw bytes — a prose placeholder
# would fail at encrypt time and make every credential test error for the wrong
# reason. This value is public and only ever guards the throwaway test DB.
TEST_MASTER_KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()


# --------------------------------------------------------------------------
# Settings / engine
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def settings() -> Iterator[Settings]:
    """Settings pointing at the test database, with test-safe secrets."""
    os.environ.setdefault("OPSPILOT_SECRET_KEY", "test-secret-key-not-for-production")
    os.environ.setdefault("OPSPILOT_MASTER_KEY", TEST_MASTER_KEY)
    os.environ.setdefault("OPSPILOT_ENV", "dev")
    os.environ.setdefault("OPSPILOT_TEST_DB_NAME", TEST_DB_NAME)
    clear_settings_cache()
    cfg = get_settings()
    yield cfg
    clear_settings_cache()
    db_session.dispose_engine()


@pytest.fixture(scope="session")
def mysql_available(settings: Settings) -> bool:
    """Probe MySQL once per session; integration tests skip without it."""
    try:
        engine = create_engine(settings.test_database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def engine(settings: Settings, mysql_available: bool) -> Iterator[Engine]:
    """Engine bound to the test database, with schema created."""
    if not mysql_available:
        pytest.skip("MySQL is not reachable; integration tests need a live server")

    db_session.dispose_engine()
    # READ COMMITTED so a test session sees rows committed by the HTTP layer.
    # MySQL's default REPEATABLE READ would hide them behind the test's own
    # open transaction and make audit assertions flake.
    eng = create_engine(
        settings.test_database_url,
        pool_pre_ping=True,
        future=True,
        isolation_level="READ COMMITTED",
    )

    # Create schema from the ORM metadata. Migrations themselves are asserted
    # separately (test_migrations.py) so a metadata/migration drift is caught
    # rather than papered over.
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)

    yield eng
    db_session.dispose_engine()
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    """A clean session per test: rows are truncated, schema is kept."""
    _truncate_all(engine)

    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    # Point the application's session factory at the test engine so FastAPI
    # dependencies and the test body share one database.
    db_session._engine = engine
    db_session._session_factory = factory

    session = factory()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


def _truncate_all(engine: Engine) -> None:
    """Empty every table so tests cannot see each other's rows."""
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f"TRUNCATE TABLE `{table.name}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


# --------------------------------------------------------------------------
# Seed data
# --------------------------------------------------------------------------
@pytest.fixture()
def seeded(db: Session) -> Session:
    """Permissions, default roles, and a bootstrap admin with every permission."""
    bootstrap_permissions(db, PERMISSION_CATALOG)
    bootstrap_roles(db, {n: str(s["description"]) for n, s in DEFAULT_ROLES.items()})
    for name, spec in DEFAULT_ROLES.items():
        attach_role_permissions(db, name, spec["permissions"])

    auth = AuthService(db)
    admin = auth.create_user(
        UserCreate(username=BOOTSTRAP_ADMIN, password=BOOTSTRAP_PASSWORD, display_name="Admin")
    )
    auth.grant_role(admin, "admin")
    db.commit()
    return db


@pytest.fixture()
def viewer(db: Session, seeded: Session) -> Any:
    """A second account with only the ``viewer`` role."""
    auth = AuthService(db)
    user = auth.create_user(
        UserCreate(username="viewer", password="viewer-password-1234", display_name="Viewer")
    )
    auth.grant_role(user, "viewer")
    db.commit()
    return user


# --------------------------------------------------------------------------
# Optional live SSH target
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SshTarget:
    host: str
    port: int
    user: str
    private_key_path: Path

    def read_private_key(self) -> str:
        return self.private_key_path.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def ssh_target() -> SshTarget:
    """A real, disposable SSH target for transport tests.

    Configured with ``OPSPILOT_TEST_SSH_*``. When those are absent the live
    transport tests skip rather than pretending to pass — a green suite that
    never opened a socket would be worse than a skipped one.

    The only sanctioned target is the local ``opstest`` account created by
    ``scripts/setup-ssh-test-target.sh``. Tests never point this at a
    production host.
    """
    host = os.environ.get("OPSPILOT_TEST_SSH_HOST", "").strip()
    key_path = os.environ.get("OPSPILOT_TEST_SSH_KEY_FILE", "").strip()
    if not host or not key_path:
        pytest.skip(
            "OPSPILOT_TEST_SSH_HOST / OPSPILOT_TEST_SSH_KEY_FILE not set; "
            "live SSH tests need the local opstest target"
        )
    path = Path(key_path)
    if not path.is_file():
        pytest.skip(f"SSH test key {path} is missing")
    return SshTarget(
        host=host,
        port=int(os.environ.get("OPSPILOT_TEST_SSH_PORT", "22")),
        user=os.environ.get("OPSPILOT_TEST_SSH_USER", "opstest").strip() or "opstest",
        private_key_path=path,
    )


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
@pytest.fixture()
def client(seeded: Session, settings: Settings) -> Iterator[TestClient]:
    """A TestClient whose database session is the same one under test."""
    from backend.app.main import create_app

    app = create_app()
    with TestClient(app, base_url="http://testserver") as c:
        yield c


@pytest.fixture()
def admin_token(client: TestClient) -> str:
    """Log in as the bootstrap admin and return a bearer token."""
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": BOOTSTRAP_ADMIN, "password": BOOTSTRAP_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


@pytest.fixture()
def admin_headers(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture()
def viewer_headers(client: TestClient, viewer: Any) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "viewer", "password": "viewer-password-1234"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}
