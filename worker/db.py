"""Database access for the worker process.

Deliberately not ``backend.app.db.session``: the worker must be deployable and
upgradable on its own, so it builds its own engine from the same settings and
never imports the API package. The URL, the UTC pin, and the pool settings are
identical because both come from :mod:`shared.config`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from shared.config import Settings, get_settings
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def _build(url: str) -> Engine:
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=5,
        max_overflow=5,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _utc(dbapi_conn: Any, _record: Any) -> None:
        with suppress(Exception):  # pragma: no cover - driver-specific
            cursor = dbapi_conn.cursor()
            cursor.execute("SET time_zone = '+00:00'")
            cursor.close()

    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine, _factory
    if _engine is None:
        cfg = settings or get_settings()
        _engine = _build(cfg.database_url)
        _factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _factory
    if _factory is None:
        get_engine(settings)
    assert _factory is not None
    return _factory


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    session = get_factory(settings)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose() -> None:
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None
