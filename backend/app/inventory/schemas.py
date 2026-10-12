"""Request/response shapes for the inventory API.

``ServerOut`` and friends deliberately omit anything secret: the credential
payload, the private key, and any resolved password are not fields here at all,
so they cannot leak by accident.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class ServerCreate(BaseModel):
    """Register a host. The host key is NOT pinned yet — that is a separate step."""

    name: str = Field(min_length=1, max_length=128)
    hostname: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="", max_length=64)
    credential_id: int | None = None
    description: str = Field(default="", max_length=4000)
    tags: dict[str, Any] | None = None

    @field_validator("name", "hostname", "ssh_user", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @field_validator("hostname")
    @classmethod
    def _hostname_is_not_a_shell_word(cls, v: str) -> str:
        # Defence in depth: a hostname is a host, not a command, and not a
        # path. Anything with whitespace, shell metacharacters, a path
        # separator, or a leading '-' is rejected before it can reach an argv.
        forbidden = set(" \t\n\r;&|<>()$`\\\"'*?[]{}~#/")
        if not v or v.startswith("-") or forbidden & set(v):
            raise ValueError("hostname must be a plain host or address")
        return v

    @field_validator("ssh_user")
    @classmethod
    def _user_is_a_username(cls, v: str) -> str:
        if v and not all(c.isalnum() or c in "_-" for c in v):
            raise ValueError("ssh_user must be a plain username")
        return v


class ServerUpdate(BaseModel):
    hostname: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=64)
    credential_id: int | None = None
    description: str | None = Field(default=None, max_length=4000)
    tags: dict[str, Any] | None = None
    status: str | None = None

    @field_validator("status")
    @classmethod
    def _known_status(cls, v: str | None) -> str | None:
        if v is not None and v not in ("active", "disabled"):
            raise ValueError("status must be active or disabled")
        return v


class ServerGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str


class FactsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    server_id: int
    collected_at: dt.datetime
    os_name: str
    os_version: str
    kernel: str
    arch: str
    cpu_count: int
    cpu_model: str
    memory_total_mb: int
    memory_available_mb: int
    disk_total_gb: float
    disk_available_gb: float


class ServerOut(BaseModel):
    """A host as the client sees it. No credential material, ever."""

    id: int
    name: str
    hostname: str
    port: int
    ssh_user: str
    credential_id: int | None
    description: str
    tags: dict[str, Any] | None
    status: str

    host_key_algorithm: str
    host_key_fingerprint: str
    host_key_state: str
    host_key_accepted_at: dt.datetime | None

    last_check_at: dt.datetime | None
    last_check_status: str
    last_check_detail: str

    groups: list[ServerGroupOut] = Field(default_factory=list)
    latest_facts: FactsOut | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class HostKeyDiscoverOut(BaseModel):
    """Result of first contact: show this to the operator for confirmation."""

    algorithm: str
    fingerprint: str
    state: str


class HostKeyAcceptRequest(BaseModel):
    """The operator echoes the fingerprint back to confirm it."""

    fingerprint: str = Field(min_length=1, max_length=128)

    @field_validator("fingerprint", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v
