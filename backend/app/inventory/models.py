"""Server inventory: hosts, groups, and collected system facts.

Servers are soft-deleted (``status``) rather than removed — audit history must
survive the removal of a host, and a dangling foreign key on a task run is
worse than a row that says "this machine is no longer managed".
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

# Imported at runtime, not under TYPE_CHECKING. ``Server.credential_id`` points
# at ``ops_credentials.id`` by name, and SQLAlchemy can only resolve that when
# the table is already in this MetaData — which is not guaranteed by whichever
# module happens to be imported first.
from backend.app.credentials.models import Credential
from backend.app.db.base import (
    Base,
    BigIntPk,
    Code,
    Name,
    ShortStatus,
    TimestampMixin,
    UTCDateTime,
)

# Server lifecycle. "deleted" is a soft delete: the row stays for audit.
SERVER_ACTIVE = "active"
SERVER_DISABLED = "disabled"
SERVER_DELETED = "deleted"
SERVER_STATUSES = (SERVER_ACTIVE, SERVER_DISABLED, SERVER_DELETED)

# Host key trust state. Distinct from lifecycle on purpose: a host can be
# active but not yet trusted, and we must refuse to run commands on it.
HOSTKEY_UNVERIFIED = "unverified"
HOSTKEY_PENDING_CONFIRM = "pending_confirm"
HOSTKEY_VERIFIED = "verified"
HOSTKEY_MISMATCH = "mismatch"
HOSTKEY_STATES = (HOSTKEY_UNVERIFIED, HOSTKEY_PENDING_CONFIRM, HOSTKEY_VERIFIED, HOSTKEY_MISMATCH)

# Last connectivity / environment check outcome.
CHECK_OK = "ok"
CHECK_FAILED = "failed"
CHECK_UNKNOWN = "unknown"
CHECK_STATES = (CHECK_OK, CHECK_FAILED, CHECK_UNKNOWN)


class Server(Base, TimestampMixin):
    """One managed Linux host."""

    __tablename__ = "ops_servers"
    __table_args__ = (
        Index("ix_ops_servers_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Name, unique=True, nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(default=22, nullable=False)
    ssh_user: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    credential_id: Mapped[int | None] = mapped_column(
        BigIntPk, ForeignKey("ops_credentials.id", ondelete="SET NULL"), nullable=True, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(ShortStatus, nullable=False, default=SERVER_ACTIVE)

    # Host key pinning. ``host_key_fingerprint`` is the SHA256 fingerprint of
    # the offered key; commands run only when state == VERIFIED and the offered
    # key still matches. Never AutoAddPolicy — see docs/security-model.md.
    host_key_algorithm: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    host_key_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    host_key_state: Mapped[str] = mapped_column(
        ShortStatus, nullable=False, default=HOSTKEY_UNVERIFIED
    )
    host_key_accepted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    last_check_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_check_status: Mapped[str] = mapped_column(
        ShortStatus, nullable=False, default=CHECK_UNKNOWN
    )
    last_check_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")

    credential: Mapped[Credential | None] = relationship(
        Credential, lazy="joined", back_populates="servers"
    )
    memberships: Mapped[list[ServerGroupMember]] = relationship(
        back_populates="server", cascade="all, delete-orphan", lazy="selectin"
    )
    facts: Mapped[list[ServerFacts]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Server id={self.id} name={self.name!r} {self.hostname}:{self.port}>"


class ServerGroup(Base, TimestampMixin):
    """A named set of servers. Membership is snapshotted at task submit time."""

    __tablename__ = "ops_server_groups"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Code, unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    members: Mapped[list[ServerGroupMember]] = relationship(
        back_populates="group", cascade="all, delete-orphan", lazy="selectin"
    )


class ServerGroupMember(Base):
    """Server ↔ group membership. Composite key — no surrogate id."""

    __tablename__ = "ops_server_group_members"
    __table_args__ = (
        UniqueConstraint("server_id", "group_id", name="uq_ops_server_group_members_pair"),
    )

    server_id: Mapped[int] = mapped_column(
        BigIntPk, ForeignKey("ops_servers.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(
        BigIntPk, ForeignKey("ops_server_groups.id", ondelete="CASCADE"), primary_key=True
    )

    server: Mapped[Server] = relationship(back_populates="memberships")
    group: Mapped[ServerGroup] = relationship(back_populates="members")


class ServerFacts(Base, TimestampMixin):
    """One snapshot of system facts collected from a server.

    Snapshots are append-only: a later check adds a row rather than mutating
    the previous one, so "what did this machine look like last Tuesday" stays
    answerable.
    """

    __tablename__ = "ops_server_facts"
    __table_args__ = (Index("ix_ops_server_facts_server_collected", "server_id", "collected_at"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(
        BigIntPk, ForeignKey("ops_servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    collected_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=None)

    os_name: Mapped[str] = mapped_column(Name, nullable=False, default="")
    os_version: Mapped[str] = mapped_column(Name, nullable=False, default="")
    kernel: Mapped[str] = mapped_column(Name, nullable=False, default="")
    arch: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    cpu_count: Mapped[int] = mapped_column(default=0, nullable=False)
    cpu_model: Mapped[str] = mapped_column(Name, nullable=False, default="")
    memory_total_mb: Mapped[int] = mapped_column(default=0, nullable=False)
    memory_available_mb: Mapped[int] = mapped_column(default=0, nullable=False)
    disk_total_gb: Mapped[float] = mapped_column(default=0.0, nullable=False)
    disk_available_gb: Mapped[float] = mapped_column(default=0.0, nullable=False)

    # Redacted raw output from the check, for debugging. Never secrets.
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    server: Mapped[Server] = relationship(back_populates="facts")
