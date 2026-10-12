"""Database engine and session management.

The control-plane metadata database is a regular MySQL instance. Migrations are
run explicitly with Alembic (``alembic upgrade head``) — the application never
issues DDL at startup, per the requirements.

Every connection is pinned to UTC so ``DATETIME(3)`` columns mean one thing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.settings import Settings, get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _build_engine(url: str) -> Engine:
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=5,
        max_overflow=10,
        future=True,
        # The URL already carries init_command=SET time_zone='+00:00' for PyMySQL.
    )

    @event.listens_for(engine, "connect")
    def _set_session_timezone(dbapi_conn: Any, _record: Any) -> None:
        """Belt and braces: force UTC on every fresh connection."""
        # The URL already carries the same init_command; if this second attempt
        # fails the connection is still usable and the first one covers us.
        with suppress(Exception):  # pragma: no cover - driver-specific edge
            cursor = dbapi_conn.cursor()
            cursor.execute("SET time_zone = '+00:00'")
            cursor.close()

    return engine


def get_engine(settings: Settings | None = None, *, force: bool = False) -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine, _session_factory
    if _engine is not None and not force:
        return _engine
    cfg = settings or get_settings()
    if _engine is not None and force:
        _engine.dispose()
    _engine = _build_engine(cfg.database_url)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    """Return the process-wide session factory."""
    global _session_factory
    if _session_factory is None:
        get_engine(settings)
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    factory = get_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    """Tear down the engine (used by tests and on worker shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def ping(settings: Settings | None = None) -> bool:
    """Cheap connectivity probe used by the health endpoint."""
    from sqlalchemy import text

    try:
        engine = get_engine(settings)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
