"""Re-export of :mod:`shared.executor.port`. See ``shared/executor``."""

from __future__ import annotations

from shared.executor.port import (
    ExecContext,
    ExecPort,
    ExecResult,
    HostKeyError,
    NotVerifiedError,
    TransportError,
)

__all__ = [
    "ExecContext",
    "ExecPort",
    "ExecResult",
    "HostKeyError",
    "NotVerifiedError",
    "TransportError",
]
