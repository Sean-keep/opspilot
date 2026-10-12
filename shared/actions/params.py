"""Constrained field types shared by action parameter models.

These are the second layer of the injection defence. The renderer refuses
illegal expressions and shape-checks each substituted value; these types then
re-check the assembled parameters so an action can never be handed a value its
own model would not have produced.

``RemotePath`` in particular is not a convenience wrapper — it is what stops
``file.render`` from writing to ``/etc/shadow`` or from a relative path that
climbs out of the application root.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, Field

from shared.actions.render import (
    PACKAGE_RE,
    SERVICE_NAME_RE,
    USERNAME_RE,
    VERSION_RE,
    validate_path,
    validate_read_path,
)

__all__ = [
    "NonEmpty",
    "OptionalUserName",
    "OptionalVersion",
    "Packages",
    "ReadPath",
    "RemotePath",
    "ServiceName",
    "UserName",
    "Version",
    "positive_int",
    "tcp_port",
]


def _username(value: str) -> str:
    if not USERNAME_RE.match(value):
        raise ValueError(f"invalid username {value!r}")
    return value


def _username_or_empty(value: str) -> str:
    """An optional identity field: a real username, or ``""`` for "unset"."""
    if value == "":
        return value
    return _username(value)


def _version(value: str) -> str:
    if not VERSION_RE.match(value):
        raise ValueError(f"invalid version {value!r}")
    return value


def _version_or_empty(value: str) -> str:
    """An optional version field: a real one, or ``""`` for "unset"."""
    if value == "":
        return value
    return _version(value)


def _service_name(value: str) -> str:
    if not SERVICE_NAME_RE.match(value):
        raise ValueError(f"invalid service name {value!r}")
    return value


def _package(value: str) -> str:
    if not PACKAGE_RE.match(value):
        raise ValueError(f"invalid package name {value!r}")
    return value


def _packages(value: list[str]) -> list[str]:
    for item in value:
        if not PACKAGE_RE.match(item):
            raise ValueError(f"invalid package name {item!r}")
    return value


def _port(value: int) -> int:
    if not 1 <= value <= 65535:
        raise ValueError(f"port out of range: {value}")
    return value


UserName = Annotated[str, AfterValidator(_username)]
#: For optional fields such as ``owner``: a real username, or ``""`` meaning unset.
#: A strict ``UserName`` rejects ``""``, which would make the field undeclarable
#: as optional even though every action treats an empty owner as "leave it alone".
OptionalUserName = Annotated[str, AfterValidator(_username_or_empty)]
Version = Annotated[str, AfterValidator(_version)]
#: For optional fields such as ``python_version``: a real version, or ``""``
#: meaning "this release does not need one".
OptionalVersion = Annotated[str, AfterValidator(_version_or_empty)]
ServiceName = Annotated[str, AfterValidator(_service_name)]
Packages = Annotated[list[str], AfterValidator(_packages), Field(min_length=1)]
#: A path that is absolute and inside an allowed **write** prefix.
#: See ``validate_path``.
RemotePath = Annotated[str, AfterValidator(validate_path)]
#: A path that will only be read or probed. Absolute and traversal-free, but
#: not confined to the write prefixes — see ``validate_read_path``.
ReadPath = Annotated[str, AfterValidator(validate_read_path)]
NonEmpty = Annotated[str, Field(min_length=1, max_length=512)]
positive_int = Annotated[int, Field(ge=1)]
tcp_port = Annotated[int, AfterValidator(_port)]
