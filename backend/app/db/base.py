"""SQLAlchemy declarative base and shared column types.

The implementations live in :mod:`shared.dbtypes` because the task tables are
shared between the API and the worker, and the worker must not import
``backend.app``. This module re-exports them so existing model modules can keep
importing from ``backend.app.db.base`` unchanged.

Conventions enforced here match the database design section of the
requirements:

* every table is InnoDB with ``utf8mb4``
* primary keys are ``BIGINT UNSIGNED``
* timestamps are ``DATETIME(3)`` and stored in UTC
* status columns are short constrained strings, not free text
"""

from __future__ import annotations

from shared.dbtypes import (
    Base,
    BigIntPk,
    Code,
    Name,
    NameStr,
    ShortStatus,
    TimestampMixin,
    UTCDateTime,
    utcnow,
)

__all__ = [
    "Base",
    "BigIntPk",
    "Code",
    "Name",
    "NameStr",
    "ShortStatus",
    "TimestampMixin",
    "UTCDateTime",
    "utcnow",
]
