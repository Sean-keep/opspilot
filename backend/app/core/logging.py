"""Structured logging — re-export shim.

The implementation lives in :mod:`shared.logsetup` because both processes log
the same way and neither may import the other. Existing imports of
``backend.app.core.logging`` keep working unchanged.

Do not add new code here. Add it to ``shared.logsetup`` instead.
"""

from __future__ import annotations

from shared.logsetup import (
    configure_logging,
    get_logger,
    get_request_id,
    set_request_id,
)

__all__ = [
    "configure_logging",
    "get_logger",
    "get_request_id",
    "set_request_id",
]
