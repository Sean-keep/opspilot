"""Remote execution transport.

Lives in ``shared/`` because the worker is the process that actually opens
sockets to managed hosts, and the worker must not import ``backend.app``.
``backend.app.executor`` re-exports these names so API code keeps working.

Two hard rules, enforced only here:

* host keys are verified; ``AutoAddPolicy`` is never used
* commands are argv lists, never shell strings
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
