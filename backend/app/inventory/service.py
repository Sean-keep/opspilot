"""Server inventory operations.

Everything that changes a server row lives here, so the router stays a thin
permission/HTTP layer.

The API does **not** execute commands against managed hosts. It may *discover*
a host key — an unauthenticated banner fetch that carries no credentials and
runs nothing — and it may *enqueue* a check. The Worker is the only process
that decrypts a credential and opens a session to run a command. This module
enforces the preconditions for that hand-off and nothing more.

Two rules are enforced here and not merely documented:

* work is only enqueued when the host key state is ``verified`` and a usable
  ``ssh_key`` credential is bound;
* the host key pin is dropped when the address changes, so a new endpoint is
  treated as a new host.
"""

from __future__ import annotations

import hmac
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.app.audit.service import RESULT_FAILURE, write_audit
from backend.app.core.errors import ApiError, Conflict, NotFound, ValidationError
from backend.app.core.logging import get_logger
from backend.app.core.settings import Settings, get_settings
from backend.app.credentials.models import CRED_SSH_KEY, Credential
from backend.app.credentials.service import CredentialService
from backend.app.db.base import utcnow
from backend.app.executor.port import TransportError
from backend.app.executor.ssh import discover_host_key
from backend.app.inventory.models import (
    HOSTKEY_MISMATCH,
    HOSTKEY_PENDING_CONFIRM,
    HOSTKEY_UNVERIFIED,
    HOSTKEY_VERIFIED,
    SERVER_ACTIVE,
    SERVER_DELETED,
    SERVER_DISABLED,
    Server,
    ServerFacts,
    ServerGroupMember,
)
from backend.app.inventory.schemas import (
    FactsOut,
    HostKeyDiscoverOut,
    ServerCreate,
    ServerGroupOut,
    ServerOut,
    ServerUpdate,
)

log = get_logger(__name__)

__all__ = ["HostKeyMismatch", "HostKeyNotVerified", "InventoryService"]


class HostKeyNotVerified(Conflict):
    """The host key has never been accepted by an operator — refuse to run."""

    code = "host_key_not_verified"


class HostKeyMismatch(Conflict):
    """The offered host key does not match the pinned fingerprint."""

    code = "host_key_mismatch"


class InventoryService:
    """Server CRUD, host-key trust, and check preconditions."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get(self, server_id: int, *, include_deleted: bool = False) -> Server | None:
        stmt = (
            select(Server)
            .where(Server.id == server_id)
            .options(selectinload(Server.memberships).selectinload(ServerGroupMember.group))
        )
        row = self.session.scalar(stmt)
        if row is not None and not include_deleted and row.status == SERVER_DELETED:
            return None
        return row

    def require(self, server_id: int, *, include_deleted: bool = False) -> Server:
        row = self.get(server_id, include_deleted=include_deleted)
        if row is None:
            raise NotFound(f"server {server_id} not found")
        return row

    def list_all(self, *, include_deleted: bool = False) -> list[Server]:
        stmt = (
            select(Server)
            .order_by(Server.id)
            .options(selectinload(Server.memberships).selectinload(ServerGroupMember.group))
        )
        if not include_deleted:
            stmt = stmt.where(Server.status != SERVER_DELETED)
        return list(self.session.scalars(stmt))

    def latest_facts(self, server_id: int) -> ServerFacts | None:
        stmt = (
            select(ServerFacts)
            .where(ServerFacts.server_id == server_id)
            .order_by(ServerFacts.collected_at.desc(), ServerFacts.id.desc())
            .limit(1)
        )
        return self.session.scalar(stmt)

    def list_facts(self, server_id: int, *, limit: int = 50) -> list[ServerFacts]:
        stmt = (
            select(ServerFacts)
            .where(ServerFacts.server_id == server_id)
            .order_by(ServerFacts.collected_at.desc(), ServerFacts.id.desc())
            .limit(max(1, min(limit, 500)))
        )
        return list(self.session.scalars(stmt))

    def to_out(self, row: Server, *, facts: ServerFacts | None = None) -> ServerOut:
        """Project to the client shape. Never any credential material."""
        groups = [
            ServerGroupOut(id=m.group.id, name=m.group.name, description=m.group.description)
            for m in row.memberships
        ]
        latest = facts if facts is not None else self.latest_facts(row.id)
        facts_out = None
        if latest is not None:
            facts_out = FactsOut(
                id=latest.id,
                server_id=latest.server_id,
                collected_at=latest.collected_at,
                os_name=latest.os_name,
                os_version=latest.os_version,
                kernel=latest.kernel,
                arch=latest.arch,
                cpu_count=latest.cpu_count,
                cpu_model=latest.cpu_model,
                memory_total_mb=latest.memory_total_mb,
                memory_available_mb=latest.memory_available_mb,
                disk_total_gb=latest.disk_total_gb,
                disk_available_gb=latest.disk_available_gb,
            )
        return ServerOut(
            id=row.id,
            name=row.name,
            hostname=row.hostname,
            port=row.port,
            ssh_user=row.ssh_user,
            credential_id=row.credential_id,
            description=row.description,
            tags=row.tags,
            status=row.status,
            host_key_algorithm=row.host_key_algorithm,
            host_key_fingerprint=row.host_key_fingerprint,
            host_key_state=row.host_key_state,
            host_key_accepted_at=row.host_key_accepted_at,
            last_check_at=row.last_check_at,
            last_check_status=row.last_check_status,
            last_check_detail=row.last_check_detail,
            groups=groups,
            latest_facts=facts_out,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def create(self, payload: ServerCreate, *, actor_id: int | None = None) -> Server:
        existing = self.session.scalar(select(Server).where(Server.name == payload.name))
        if existing is not None:
            raise ValidationError(f"server name {payload.name!r} is already taken")

        if payload.credential_id is not None:
            self._require_usable_credential(payload.credential_id)

        row = Server(
            name=payload.name,
            hostname=payload.hostname,
            port=payload.port,
            ssh_user=payload.ssh_user or self.settings.ssh_user,
            credential_id=payload.credential_id,
            description=payload.description,
            tags=payload.tags,
            status=SERVER_ACTIVE,
            # Trust starts empty. Accepting a host key is a separate, explicit step.
            host_key_algorithm="",
            host_key_fingerprint="",
            host_key_state=HOSTKEY_UNVERIFIED,
        )
        self.session.add(row)
        self.session.flush()
        write_audit(
            "server.create",
            actor_id=actor_id,
            target_type="server",
            target_id=str(row.id),
            snapshot={
                "name": row.name,
                "hostname": row.hostname,
                "port": row.port,
                "ssh_user": row.ssh_user,
                "credential_id": row.credential_id,
            },
        )
        return row

    def update(
        self,
        server_id: int,
        payload: ServerUpdate,
        *,
        actor_id: int | None = None,
    ) -> Server:
        row = self.require(server_id)
        changed: dict[str, Any] = {}

        if payload.hostname is not None:
            _validate_hostname(payload.hostname)
            changed["hostname"] = {"from": row.hostname, "to": payload.hostname}
            row.hostname = payload.hostname
            # A different address is a different host: the old pin no longer applies.
            self._reset_host_key(row)
        if payload.port is not None and payload.port != row.port:
            changed["port"] = {"from": row.port, "to": payload.port}
            row.port = payload.port
            self._reset_host_key(row)
        if payload.ssh_user is not None:
            _validate_user(payload.ssh_user)
            changed["ssh_user"] = {"from": row.ssh_user, "to": payload.ssh_user}
            row.ssh_user = payload.ssh_user
        if payload.credential_id is not None:
            self._require_usable_credential(payload.credential_id)
            changed["credential_id"] = {"from": row.credential_id, "to": payload.credential_id}
            row.credential_id = payload.credential_id
        if payload.description is not None:
            row.description = payload.description
        if payload.tags is not None:
            row.tags = payload.tags
        if payload.status is not None:
            new_status = SERVER_ACTIVE if payload.status == "active" else SERVER_DISABLED
            changed["status"] = {"from": row.status, "to": new_status}
            row.status = new_status

        self.session.flush()
        if changed:
            write_audit(
                "server.update",
                actor_id=actor_id,
                target_type="server",
                target_id=str(row.id),
                snapshot={"name": row.name, "changed": changed},
            )
        return row

    def soft_delete(self, server_id: int, *, actor_id: int | None = None) -> Server:
        """Mark a server ``deleted``. The row stays so audit history resolves."""
        row = self.require(server_id)
        row.status = SERVER_DELETED
        self.session.flush()
        write_audit(
            "server.delete",
            actor_id=actor_id,
            target_type="server",
            target_id=str(row.id),
            snapshot={"name": row.name, "hostname": row.hostname},
        )
        return row

    # ------------------------------------------------------------------
    # Host key trust
    # ------------------------------------------------------------------
    def discover(self, server_id: int, *, actor_id: int | None = None) -> HostKeyDiscoverOut:
        """First contact: fetch the offered host key and record it as *pending*.

        Nothing is trusted yet. The operator must echo the fingerprint back
        through :meth:`accept_host_key` before any command runs.
        """
        row = self.require(server_id)
        try:
            offered = discover_host_key(
                row.hostname,
                row.port,
                connect_timeout=self.settings.ssh_connect_timeout,
            )
        except TransportError as exc:
            raise ApiError(
                f"cannot reach {row.hostname}:{row.port}",
                code="host_unreachable",
                status_code=502,
                details={"reason": str(exc)},
            ) from exc

        algorithm = offered["algorithm"]
        fingerprint = offered["fingerprint"]

        # A change of the offered key is a trust event, not a quiet overwrite.
        previous = row.host_key_fingerprint
        if previous and previous != fingerprint and row.host_key_state == HOSTKEY_VERIFIED:
            row.host_key_state = HOSTKEY_MISMATCH
            self.session.flush()
            write_audit(
                "server.hostkey.mismatch",
                actor_id=actor_id,
                target_type="server",
                target_id=str(row.id),
                snapshot={
                    "name": row.name,
                    "offered_algorithm": algorithm,
                    "offered_fingerprint": fingerprint,
                    "pinned_fingerprint": previous,
                },
                result=RESULT_FAILURE,
            )
            raise HostKeyMismatch(
                f"host key for {row.name} changed: offered {algorithm} {fingerprint}, "
                f"pinned {previous}. A human must investigate before this host is trusted again.",
                details={"offered_fingerprint": fingerprint, "pinned_fingerprint": previous},
            )

        row.host_key_algorithm = algorithm
        row.host_key_fingerprint = fingerprint
        if row.host_key_state != HOSTKEY_VERIFIED:
            row.host_key_state = HOSTKEY_PENDING_CONFIRM
        self.session.flush()

        write_audit(
            "server.hostkey.discover",
            actor_id=actor_id,
            target_type="server",
            target_id=str(row.id),
            snapshot={
                "name": row.name,
                "algorithm": algorithm,
                "fingerprint": fingerprint,
                "state": row.host_key_state,
            },
        )
        return HostKeyDiscoverOut(
            algorithm=algorithm,
            fingerprint=fingerprint,
            state=row.host_key_state,
        )

    def accept_host_key(
        self,
        server_id: int,
        fingerprint: str,
        *,
        actor_id: int | None = None,
    ) -> Server:
        """Pin the discovered fingerprint after an operator confirms it.

        The comparison is constant-time. A mismatch is refused — accepting a
        host key is not "write whatever the client sends".
        """
        row = self.require(server_id)
        if not row.host_key_fingerprint:
            raise Conflict(
                "no host key has been discovered for this server yet",
                code="host_key_not_discovered",
            )
        if not hmac.compare_digest(row.host_key_fingerprint.strip(), fingerprint.strip()):
            write_audit(
                "server.hostkey.accept",
                actor_id=actor_id,
                target_type="server",
                target_id=str(row.id),
                snapshot={"name": row.name, "outcome": "fingerprint_mismatch"},
                result=RESULT_FAILURE,
            )
            raise HostKeyMismatch(
                "the fingerprint you confirmed does not match the one offered by the host",
                details={"offered_fingerprint": row.host_key_fingerprint},
            )

        row.host_key_state = HOSTKEY_VERIFIED
        row.host_key_accepted_at = utcnow()
        self.session.flush()
        write_audit(
            "server.hostkey.accept",
            actor_id=actor_id,
            target_type="server",
            target_id=str(row.id),
            snapshot={
                "name": row.name,
                "algorithm": row.host_key_algorithm,
                "fingerprint": row.host_key_fingerprint,
            },
        )
        return row

    # ------------------------------------------------------------------
    # Environment check — the S1 -> S2 seam
    # ------------------------------------------------------------------
    def require_checkable(self, server_id: int) -> Server:
        """Return a server that is safe to enqueue work against.

        Since S2 the API no longer runs the check itself: it validates the
        preconditions and hands a task to the Worker, which is the only process
        that decrypts credentials and opens a session to a managed host.

        The preconditions are still enforced here, synchronously, because
        enqueueing work that is known to be wrong helps nobody:

        * the host key must already be verified by a human;
        * the server must have an active ``ssh_key`` credential bound.

        Anything that needs the network is the Worker's problem.
        """
        row = self.require(server_id)
        self._require_trusted_host_key(row)

        if row.credential_id is None:
            raise ValidationError(
                "server has no credential attached; add one before running a check"
            )
        cred = self._require_usable_credential(int(row.credential_id))
        if cred.kind != CRED_SSH_KEY:
            raise ValidationError(
                f"credential {cred.name!r} is of kind {cred.kind!r}; "
                "only ssh_key credentials can be used for a connectivity check"
            )
        if not (row.ssh_user or self.settings.ssh_user):
            raise ValidationError("server has no ssh_user and OPSPILOT_SSH_USER is unset")
        return row

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _require_trusted_host_key(self, row: Server) -> None:
        if not row.host_key_fingerprint:
            raise HostKeyNotVerified(
                f"host key for {row.name} has never been discovered; "
                "call discover-hostkey and have an operator confirm the fingerprint first"
            )
        if row.host_key_state != HOSTKEY_VERIFIED:
            raise HostKeyNotVerified(
                f"host key for {row.name} is in state {row.host_key_state!r}; "
                "an operator must confirm the fingerprint before commands run",
                details={"host_key_state": row.host_key_state},
            )

    def _require_usable_credential(self, credential_id: int) -> Credential:
        creds = CredentialService(self.session, self.settings)
        cred = creds.require(credential_id)
        if cred.status != "active":
            raise ValidationError(f"credential {cred.name!r} is retired")
        return cred

    def _reset_host_key(self, row: Server) -> None:
        """Drop the pin when the address changes — a new endpoint is a new host."""
        row.host_key_algorithm = ""
        row.host_key_fingerprint = ""
        row.host_key_state = HOSTKEY_UNVERIFIED
        row.host_key_accepted_at = None


# --------------------------------------------------------------------------
# Small validators shared by create/update (schemas do the HTTP-side ones).
# --------------------------------------------------------------------------
_FORBIDDEN_HOST_CHARS = set(" \t\n\r;&|<>()$`\\\"'*?[]{}~#/")


def _validate_hostname(value: str) -> str:
    if not value or value.startswith("-") or _FORBIDDEN_HOST_CHARS & set(value):
        raise ValidationError("hostname must be a plain host or address")
    return value


def _validate_user(value: str) -> str:
    if value and not all(c.isalnum() or c in "_-" for c in value):
        raise ValidationError("ssh_user must be a plain username")
    return value
