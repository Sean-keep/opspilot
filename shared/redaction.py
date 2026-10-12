"""Recursive redaction of sensitive values in structured data.

The requirements forbid writing passwords, tokens, private keys, or complete
sensitive environment variables into logs, error payloads, and audit snapshots.
This module is the single choke point for that rule and is shared by the API and
the worker so neither can drift.

Redaction is applied to every dict / list / tuple / dataclass-ish structure
before it is persisted or logged. Scalars are returned unchanged unless their
key looks sensitive.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

#: Replacement emitted in place of every redacted value.
REDACTED = "[REDACTED]"

#: Exact key names that always mean "this value must not leave the process".
#: Matching is case-insensitive and ignores `-` / `_` separators, so
#: ``private_key``, ``privateKey`` and ``private-key`` are all caught.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "accesstoken",
        "refreshtoken",
        "bottoken",
        "password",
        "passwd",
        "pass",
        "secret",
        "secretkey",
        "apikey",
        "privatekey",
        "authorization",
        "auth",
        "cookie",
        "setcookie",
        "credential",
        "credentials",
        "passphrase",
        "signature",
        "sessionid",
        "masterkey",
        "dbpassword",
        "sshpassword",
        "sudo_password",
    }
)

#: Substring markers: a key containing any of these is treated as sensitive.
#: This catches names the exact set cannot enumerate, e.g. ``ops_db_password``.
SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "private_key",
    "privatekey",
    "apikey",
    "api_key",
    "passphrase",
    "credential",
)

#: Value-shaped markers. A string whose *value* starts with one of these is
#: redacted even when its key looks harmless (e.g. inline PEM blobs).
#: Telegram bot tokens: a numeric bot id, a colon, then a long opaque secret.
#: They appear inside URLs rather than as a named field, so key-based
#: redaction cannot see them — hence the value-shaped pattern below.
_TELEGRAM_TOKEN_RE: re.Pattern[str] = re.compile(r"/bot\d{6,12}:[A-Za-z0-9_-]{20,}/")

_SENSITIVE_VALUE_PREFIXES: tuple[str, ...] = (
    "-----BEGIN",
    "ssh-rsa ",
    "ssh-ed25519 ",
    "ecdsa-sha2-",
    "AKIA",  # AWS access key id
)


def _normalize_key(key: str) -> str:
    """Collapse a mapping key to a comparable form.

    ``private_key``, ``privateKey`` and ``PRIVATE-KEY`` all become
    ``privatekey``.
    """
    return key.replace("-", "").replace("_", "").lower()


def is_sensitive_key(key: str) -> bool:
    """Return True when *key* names a value that must never be logged."""
    normalized = _normalize_key(key)
    if normalized in {_normalize_key(k) for k in SENSITIVE_KEYS}:
        return True
    lowered = key.lower().replace("-", "_")
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def is_sensitive_value(value: Any) -> bool:
    """Return True when *value* looks like raw key material on its own."""
    if not isinstance(value, str):
        return False
    stripped = value.lstrip()
    return any(stripped.startswith(prefix) for prefix in _SENSITIVE_VALUE_PREFIXES)


def redact(obj: Any, *, depth: int = 0, max_depth: int = 24) -> Any:
    """Return a copy of *obj* with every sensitive value replaced.

    Handles dicts, lists, tuples, sets, and any object exposing ``__dict__``.
    Cycles are broken by replacing the repeated node with ``[REDACTED]``.

    The result is safe to persist in ``result_json`` / ``params_snapshot`` and
    safe to hand to a logger.
    """
    if depth > max_depth:
        return REDACTED

    if isinstance(obj, Mapping):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and is_sensitive_key(key):
                out[key] = REDACTED
            else:
                out[key] = redact(value, depth=depth + 1, max_depth=max_depth)
        return out

    if isinstance(obj, (list, tuple, set, frozenset)):
        items: Iterable[Any] = list(obj)
        redacted_items = [redact(item, depth=depth + 1, max_depth=max_depth) for item in items]
        if isinstance(obj, tuple):
            return tuple(redacted_items)
        if isinstance(obj, (set, frozenset)):
            # Sets of unhashable redacted values would blow up; emit a list.
            return redacted_items
        return redacted_items

    if is_sensitive_value(obj):
        return REDACTED

    # Plain scalars pass through untouched.
    if obj is None or isinstance(obj, (str, int, float, bool, bytes)):
        return obj

    # Dataclasses / pydantic models / simple objects: redact their fields.
    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict) and data:
        return redact(data, depth=depth + 1, max_depth=max_depth)

    return obj


def redact_text(text: str) -> str:
    """Best-effort redaction of inline secrets inside a free-form log line.

    Structural redaction is preferred; this is the safety net for strings that
    already escaped structured handling (e.g. a captured SSH stderr).
    """
    if not text:
        return text
    out = text
    for marker in (
        "-----BEGIN",
        "password=",
        "PASSWORD=",
        "token=",
        "TOKEN=",
        "secret=",
        "SECRET=",
        "Authorization:",
        "Authorization: ",
        "Bearer ",
    ):
        if marker in out:
            out = _mask_after(out, marker)
    # A Telegram bot token is not a `key=value` — it rides in a URL path
    # (``/bot<token>/sendMessage``), which the marker scan above cannot see.
    return _TELEGRAM_TOKEN_RE.sub(f"/bot{REDACTED}/", out)


def _mask_after(text: str, marker: str) -> str:
    """Replace everything after *marker* on that line with ``[REDACTED]``."""
    lines = text.split("\n")
    masked: list[str] = []
    for line in lines:
        if marker in line:
            head, _, _ = line.partition(marker)
            masked.append(f"{head}{marker}{REDACTED}")
        else:
            masked.append(line)
    return "\n".join(masked)
