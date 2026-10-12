"""Execution transport, behind a protocol.

The Action Registry needs to run against a remote host (the Worker) and to be
dry-runnable for template validation (the API). Routing both through this
protocol keeps the actions free of SSH imports and keeps the API free of any
ability to open a socket to a managed host.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Outcome of one remote command."""

    exit_code: int
    stdout: str
    stderr: str
    # argv as actually executed, for the audit trail. Never a shell string.
    argv: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True, slots=True)
class ExecContext:
    """Where and as whom a command runs."""

    host: str
    port: int
    user: str
    # Opaque handle to the decrypted secret. The plaintext must never be
    # copied into this object — see docs/security-model.md.
    credential_id: int | None = None
    timeout: int = 60
    # Set when the host key is pinned and verified. Commands must refuse to
    # run without it.
    host_key_fingerprint: str = ""
    extra: dict[str, object] = field(default_factory=dict)


class TransportError(Exception):
    """The command could not be run at all (connection, auth, timeout)."""


class HostKeyError(TransportError):
    """The remote host key did not match the pinned fingerprint."""


class NotVerifiedError(TransportError):
    """The host key has not been pinned and accepted yet — refuse to run."""


@runtime_checkable
class ExecPort(Protocol):
    """Something that can run an argv-separated command on a host."""

    def run(
        self,
        ctx: ExecContext,
        argv: Sequence[str],
        *,
        stdin: bytes | str | None = None,
    ) -> ExecResult:
        """Run *argv* on the host described by *ctx*.

        ``argv`` is a list — never a shell string. Implementations must not
        concatenate arguments, must not use ``shell=True``, and must refuse to
        run when the host key is not verified.

        ``stdin`` is the only channel for payload bytes: file contents, a curl
        config holding a bearer token, anything that must not appear in the
        audit trail. argv is logged and persisted; stdin is not. Anything
        sensitive therefore *has* to come in through here rather than as an
        argument.
        """
        ...
