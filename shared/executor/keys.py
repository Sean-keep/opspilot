"""Staging private key material for a single connection.

The plaintext key exists on disk only for the lifetime of one SSH connection,
in a file the process owns with mode 600, and is unlinked in a ``finally``.
Nothing here logs, returns, or persists the secret.

Paramiko needs a path (or a PKey object). A path is used so the same call site
works for an encrypted key with a passphrase later, and so the material never
sits in a Python string that a traceback or a debug dump could capture.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from shared.logsetup import get_logger

log = get_logger(__name__)

__all__ = ["staged_private_key"]


@contextmanager
def staged_private_key(secret: str) -> Iterator[str]:
    """Yield a path to *secret* written to a mode-600 temp file.

    The file is always removed, including on exception. The caller must not
    keep the path after the block exits.
    """
    if not secret:
        raise ValueError("private key material is empty")
    if "\x00" in secret:
        raise ValueError("private key material must not contain NUL")

    fd, raw_path = tempfile.mkstemp(prefix="opspilot-key-", suffix=".pem")
    path = Path(raw_path)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secret)
            if not secret.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        yield str(path)
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best-effort cleanup
            log.warning("executor.key_cleanup_failed", path=str(path))
