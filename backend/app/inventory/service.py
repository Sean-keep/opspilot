"""Server inventory operations.

Everything that changes a server row or talks to a host lives here, so the
router stays a thin permission/HTTP layer and the Worker can reuse the same
logic in S2 without importing FastAPI.

Two rules are enforced here and not merely documented:

* a command only runs when the host key state is ``verified`` and the offered
  key still matches the pinned fingerprint;
* the credential plaintext is staged to a mode-600 temp file for the length of
  one connection and never appears on the returned objects.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
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
from backend.app.executor.keys import staged_private_key
from backend.app.executor.port import (
    ExecContext,
    ExecPort,
    HostKeyError,
    NotVerifiedError,
    TransportError,
)
from backend.app.executor.ssh import SshExecPort, discover_host_key
from backend.app.inventory.checks import CheckError, SystemFacts, collect_facts
from backend.app.inventory.models import (
    CHECK_FAILED,
    CHECK_OK,
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
    ServerCheckResult,
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


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """Internal result of one connectivity + facts check."""

    ok: bool
    detail: str
    facts: SystemFacts | None = None


class InventoryService:
    """Server CRUD, host-key trust, and the read-only environment check."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        *,
        port: ExecPort | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        # Injected so tests can supply a fake transport without a socket.
        self.port: ExecPort = port or SshExecPort(
            connect_timeout=self.settings.ssh_connect_timeout,
            default_timeout=self.settings.ssh_command_timeout,
        )

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
    # Connectivity + environment check
    # ------------------------------------------------------------------
    def run_check(
        self,
        server_id: int,
        *,
        actor_id: int | None = None,
    ) -> tuple[Server, ServerCheckResult]:
        """Run the read-only facts collection and record the outcome.

        Returns the updated server and a result. A *failed check* is a result
        (``ok=False``), not an HTTP error: "the host is down" is an answer the
        operator asked for. A trust failure (unverified / mismatched host key)
        raises, because running the command at all would be wrong.
        """
        row = self.require(server_id)
        self._require_trusted_host_key(row)

        try:
            facts = self._collect(row)
        except (HostKeyError, NotVerifiedError) as exc:
            self._record_check(row, CHECK_FAILED, str(exc))
            write_audit(
                "server.check",
                actor_id=actor_id,
                target_type="server",
                target_id=str(row.id),
                snapshot={"name": row.name, "outcome": "host_key_error"},
                result=RESULT_FAILURE,
            )
            raise
        except (TransportError, CheckError) as exc:
            detail = _safe_detail(exc)
            self._record_check(row, CHECK_FAILED, detail)
            write_audit(
                "server.check",
                actor_id=actor_id,
                target_type="server",
                target_id=str(row.id),
                snapshot={"name": row.name, "outcome": "failed", "detail": detail},
                result=RESULT_FAILURE,
            )
            return row, ServerCheckResult(ok=False, detail=detail, facts=None)

        snapshot = self._persist_facts(row, facts)
        self._record_check(row, CHECK_OK, "system facts collected")
        write_audit(
            "server.check",
            actor_id=actor_id,
            target_type="server",
            target_id=str(row.id),
            snapshot={
                "name": row.name,
                "outcome": "ok",
                "os": f"{facts.os_name}-{facts.os_version}",
                "arch": facts.arch,
                "cpu_count": facts.cpu_count,
            },
        )
        out = self.to_out(row, facts=snapshot)
        return row, ServerCheckResult(
            ok=True, detail="system facts collected", facts=out.latest_facts
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _collect(self, row: Server) -> SystemFacts:
        """Run the facts check over SSH with the credential staged to disk."""
        user = row.ssh_user or self.settings.ssh_user
        if not user:
            raise ValidationError("server has no ssh_user and OPSPILOT_SSH_USER is unset")

        secret = self._credential_secret(row)
        with staged_private_key(secret) as key_path:
            ctx = ExecContext(
                host=row.hostname,
                port=row.port,
                user=user,
                credential_id=row.credential_id,
                timeout=self.settings.ssh_command_timeout,
                host_key_fingerprint=row.host_key_fingerprint,
                extra={"key_filename": key_path},
            )
            return collect_facts(self.port, ctx)

    def _credential_secret(self, row: Server) -> str:
        if row.credential_id is None:
            raise ValidationError(
                "server has no credential attached; add one before running a check"
            )
        creds = CredentialService(self.session, self.settings)
        cred = creds.require(row.credential_id)
        if cred.kind != CRED_SSH_KEY:
            raise ValidationError(
                f"credential {cred.name!r} is of kind {cred.kind!r}; "
                "only ssh_key credentials can be used for a connectivity check"
            )
        if cred.status != "active":
            raise ValidationError(f"credential {cred.name!r} is retired")
        # Decrypting here is the one place the API touches plaintext, and it
        # lives only inside staged_private_key's temp file. Never logged.
        secret = creds.decrypt(cred.id)
        creds.mark_used(cred.id)
        return secret

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

    def _persist_facts(self, row: Server, facts: SystemFacts) -> ServerFacts:
        snapshot = ServerFacts(
            server_id=row.id,
            collected_at=utcnow(),
            os_name=facts.os_name[:128],
            os_version=facts.os_version[:128],
            kernel=facts.kernel[:128],
            arch=facts.arch[:32],
            cpu_count=facts.cpu_count,
            cpu_model=facts.cpu_model[:128],
            memory_total_mb=facts.memory_total_mb,
            memory_available_mb=facts.memory_available_mb,
            disk_total_gb=facts.disk_total_gb,
            disk_available_gb=facts.disk_available_gb,
            raw_json=None,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def _record_check(self, row: Server, status: str, detail: str) -> None:
        row.last_check_at = utcnow()
        row.last_check_status = status
        row.last_check_detail = detail[:4000]
        self.session.flush()


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


def _safe_detail(exc: Exception) -> str:
    """A one-line, secret-free description of a failed check.

    Transport errors can quote a socket error; they never carry credential
    material, but the text is still passed through redaction before it is
    stored so a stray secret in a future error string cannot land in the DB.
    """
    from shared.redaction import redact_text

    text = str(exc) or exc.__class__.__name__
    return redact_text(text).replace("\n", " ")[:1000]
