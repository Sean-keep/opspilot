"""SQLAlchemy declarative base and shared column types.

These live in ``shared/`` rather than ``backend/`` because the task tables are
used by **both** the API and the worker process, and the worker must not import
``backend.app`` (see docs/architecture.md). ``backend.app.db.base`` re-exports
everything here so existing model modules keep working unchanged.

Conventions match the database design section of the requirements:

* every table is InnoDB with ``utf8mb4``
* primary keys are ``BIGINT UNSIGNED``
* timestamps are ``DATETIME(3)`` and stored in UTC
* status columns are short constrained strings, not free text
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.dialects.mysql import BIGINT as MYSQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, TypeEngine

# MySQL UNSIGNED BIGINT for every surrogate key. The non-MySQL variant keeps
# the model importable on other dialects (offline SQL generation, tests) even
# though OpsPilot only ever runs against MySQL.
BigIntPk = BigInteger().with_variant(MYSQL_BIGINT(unsigned=True), "mysql")


class UTCDateTime(TypeDecorator[dt.datetime | None]):
    """A ``DATETIME(3)`` column that always round-trips through UTC.

    Naive datetimes written in are assumed to already be UTC. Values read back
    are tz-aware in UTC so application code never has to guess.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "mysql":
            return MYSQL_DATETIME(fsp=3, timezone=False)
        return DateTime()

    def process_bind_param(
        self, value: dt.datetime | None, dialect: Dialect
    ) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            return value.astimezone(dt.UTC).replace(tzinfo=None)
        return value

    def process_result_value(
        self, value: dt.datetime | None, dialect: Dialect
    ) -> dt.datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=dt.UTC)


def utcnow() -> dt.datetime:
    """Current UTC time, tz-aware — the only clock the app should use."""
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    """Declarative base for every OpsPilot table."""

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dt.datetime: UTCDateTime,
    }


class TimestampMixin:
    """``created_at`` / ``updated_at`` on every row, both UTC ``DATETIME(3)``."""

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class NameStr:
    """Length shared by human-readable names, so columns line up across tables."""

    LEN = 128


Name = String(NameStr.LEN)
Code = String(64)
ShortStatus = String(32)
