"""Remote execution transport — re-export shim.

The implementations live in :mod:`shared.executor` because the worker is what
opens sockets to managed hosts, and it must not import ``backend.app``. This
module keeps the old import path working for API code and tests.

Do not add new code here. Add it to ``shared/executor`` instead.
"""

from __future__ import annotations

from shared.executor.keys import staged_private_key
from shared.executor.port import (
    ExecContext,
    ExecPort,
    ExecResult,
    HostKeyError,
    NotVerifiedError,
    TransportError,
)
from shared.executor.ssh import SshExecPort, discover_host_key, quote_argv, ssh_fingerprint

__all__ = [
    "ExecContext",
    "ExecPort",
    "ExecResult",
    "HostKeyError",
    "NotVerifiedError",
    "SshExecPort",
    "TransportError",
    "discover_host_key",
    "quote_argv",
    "ssh_fingerprint",
    "staged_private_key",
]
