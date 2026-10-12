"""SSH transport with pinned host keys.

Two hard requirements from the security model, both enforced here and nowhere
else:

1. **Host keys are verified.** ``RejectPolicy`` is the starting point and
   ``AutoAddPolicy`` is never used. A connection only proceeds when the offered
   key matches the fingerprint pinned on the server record. First contact is a
   separate, explicit discovery step whose result must be accepted by a human
   before any command runs.
2. **Commands are argv-separated.** Callers pass a list of strings. Nothing
   here interpolates user input into a shell line.

Paramiko's ``exec_command`` takes a string and the remote side runs it through
the login shell. There is no exec-without-shell on a standard SSH server. So
each argv element is quoted with :func:`shlex.quote` before joining — that is
the correct way to keep a shell channel argv-safe. The quoting is belt and
braces: arguments already come from Pydantic-validated action parameters.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import shlex
import socket
from collections.abc import Sequence

import paramiko

from backend.app.core.logging import get_logger
from backend.app.executor.port import (
    ExecContext,
    ExecPort,
    ExecResult,
    HostKeyError,
    NotVerifiedError,
    TransportError,
)

log = get_logger(__name__)

__all__ = [
    "SshExecPort",
    "discover_host_key",
    "quote_argv",
    "ssh_fingerprint",
]


# --------------------------------------------------------------------------
# Host key fingerprinting
# --------------------------------------------------------------------------
def ssh_fingerprint(key: paramiko.PKey) -> str:
    """SHA256 fingerprint in the same format ``ssh-keygen -lf`` prints.

    ``SHA256:<base64 of sha256 of the raw key blob, padding stripped>``
    """
    digest = hashlib.sha256(key.asbytes()).digest()
    b64 = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{b64}"


class _PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Accept a host key only when its fingerprint matches the pinned one.

    Deliberately not ``AutoAddPolicy``. An unknown host raises ``NotVerifiedError``
    so the caller has to go through the explicit accept flow; a mismatch raises
    ``HostKeyError`` and is never silently upgraded to "trust the new key".
    """

    def __init__(self, expected_fingerprint: str) -> None:
        self.expected = expected_fingerprint.strip()
        self.offered_algorithm = ""
        self.offered_fingerprint = ""

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        self.offered_algorithm = key.get_name()
        self.offered_fingerprint = ssh_fingerprint(key)

        if not self.expected:
            raise NotVerifiedError(
                f"host key for {hostname} is not pinned; "
                f"offered {self.offered_algorithm} {self.offered_fingerprint}. "
                "An operator must confirm this fingerprint before commands can run."
            )

        if not hmac.compare_digest(self.offered_fingerprint, self.expected):
            raise HostKeyError(
                f"host key mismatch for {hostname}: "
                f"offered {self.offered_algorithm} {self.offered_fingerprint}, "
                f"expected {self.expected}. Refusing to connect."
            )

        # Fingerprint matches the pinned value — record it for this connection
        # so Paramiko can proceed. This is the ONLY place a key is added, and
        # it only happens after a constant-time comparison against the pin.
        client.get_host_keys().add(hostname, key.get_name(), key)


# --------------------------------------------------------------------------
# argv handling
# --------------------------------------------------------------------------
def quote_argv(argv: Sequence[str]) -> str:
    """Turn an argv list into a shell-safe command string.

    Every element is quoted independently, so an argument containing ``;``,
    ``$()``, backticks, or spaces stays a single inert token. This never
    substitutes values — it only quotes what the caller already validated.
    """
    if not argv:
        raise ValueError("argv must not be empty")
    parts: list[str] = []
    for item in argv:
        if not isinstance(item, str):
            raise TypeError(f"argv elements must be str, got {type(item).__name__}")
        if "\x00" in item:
            raise ValueError("argv elements must not contain NUL")
        parts.append(shlex.quote(item))
    return " ".join(parts)


# --------------------------------------------------------------------------
# Discovery (first contact)
# --------------------------------------------------------------------------
def discover_host_key(
    host: str,
    port: int,
    *,
    connect_timeout: int = 10,
) -> dict[str, str]:
    """Fetch the host key offered by *host* without running any command.

    This is the **only** unverified connection in the codebase, and it exists
    so an operator can be shown a fingerprint to confirm. It performs no
    authentication and executes nothing. The result must be accepted explicitly
    (``POST /servers/{id}/accept-hostkey``) before any command runs.

    Returns ``{"algorithm": ..., "fingerprint": ...}``.
    """
    transport: paramiko.Transport | None = None
    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout)
    except OSError as exc:
        raise TransportError(f"cannot reach {host}:{port}: {exc}") from exc

    try:
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=connect_timeout)
        key = transport.get_remote_server_key()
        return {"algorithm": key.get_name(), "fingerprint": ssh_fingerprint(key)}
    except paramiko.SSHException as exc:
        raise TransportError(f"SSH handshake with {host}:{port} failed: {exc}") from exc
    finally:
        if transport is not None:
            transport.close()
        sock.close()


# --------------------------------------------------------------------------
# The port
# --------------------------------------------------------------------------
class SshExecPort:
    """Run argv commands over SSH with a pinned host key."""

    def __init__(
        self,
        *,
        connect_timeout: int = 10,
        default_timeout: int = 60,
    ) -> None:
        self.connect_timeout = connect_timeout
        self.default_timeout = default_timeout

    # ------------------------------------------------------------------
    def run(self, ctx: ExecContext, argv: Sequence[str]) -> ExecResult:
        # Refuse before any socket is opened. Checking this only inside the
        # host-key policy would still allow a TCP connection (and a probe of the
        # network) on an untrusted host; the rule is "no pin, no connection".
        if not (ctx.host_key_fingerprint or "").strip():
            raise NotVerifiedError(
                f"host key for {ctx.host} is not pinned; "
                "an operator must confirm the fingerprint before any command runs."
            )

        command = quote_argv(argv)
        # Log the argv, never the resolved secret. ``command`` contains only
        # caller-supplied argument text, which redaction already scrubs.
        log.info(
            "ssh.exec",
            host=ctx.host,
            port=ctx.port,
            user=ctx.user,
            argv=list(argv),
            fingerprint=ctx.host_key_fingerprint or "(unpinned)",
        )

        client = paramiko.SSHClient()
        policy = _PinnedHostKeyPolicy(ctx.host_key_fingerprint)
        client.set_missing_host_key_policy(policy)

        try:
            client.connect(
                hostname=ctx.host,
                port=ctx.port,
                username=ctx.user,
                # The private key is materialised by the caller into a temp file
                # or supplied as a key object; we never pass a password here.
                key_filename=self._key_path(ctx),
                timeout=self.connect_timeout,
                banner_timeout=self.connect_timeout,
                auth_timeout=self.connect_timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.BadHostKeyException as exc:
            raise HostKeyError(
                f"host key mismatch for {ctx.host}: {exc}"
            ) from exc
        except NotVerifiedError:
            raise
        except HostKeyError:
            raise
        except (paramiko.SSHException, OSError) as exc:
            raise TransportError(f"cannot connect to {ctx.host}:{ctx.port}: {exc}") from exc

        timeout = ctx.timeout or self.default_timeout
        try:
            # exec_command runs through the remote login shell; quote_argv has
            # already made each argument a single inert token.
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            stdin.close()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            return ExecResult(exit_code=code, stdout=out, stderr=err, argv=tuple(argv))
        except (paramiko.SSHException, OSError) as exc:
            raise TransportError(f"command failed to run on {ctx.host}: {exc}") from exc
        finally:
            client.close()

    # ------------------------------------------------------------------
    @staticmethod
    def _key_path(ctx: ExecContext) -> str | None:
        """Return the private key path, if the caller staged one.

        The plaintext key is never stored on ``ExecContext``. The caller
        (Worker) writes it to a mode-600 temp file, passes the path here, and
        unlinks it afterwards.
        """
        path = ctx.extra.get("key_filename")
        return str(path) if isinstance(path, str) and path else None


# Structural assertion: SshExecPort satisfies the protocol.
def _assert_protocol() -> None:
    port: ExecPort = SshExecPort()
    del port
