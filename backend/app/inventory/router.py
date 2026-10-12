"""Inventory endpoints: servers, host-key trust, and the environment check.

Every handler is a permission check plus a call into
:class:`~backend.app.inventory.service.InventoryService`. No handler opens a
socket, decrypts a credential, or builds a command string itself.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.app.auth.deps import DbSession, require_permission
from backend.app.auth.models import User
from backend.app.inventory.schemas import (
    FactsOut,
    HostKeyAcceptRequest,
    HostKeyDiscoverOut,
    ServerCheckResult,
    ServerCreate,
    ServerOut,
    ServerUpdate,
)
from backend.app.inventory.service import InventoryService

router = APIRouter(prefix="/servers", tags=["inventory"])

CanReadServers = Annotated[User, Depends(require_permission("server.read"))]
CanCreateServer = Annotated[User, Depends(require_permission("server.create"))]
CanUpdateServer = Annotated[User, Depends(require_permission("server.update"))]
CanDeleteServer = Annotated[User, Depends(require_permission("server.delete"))]
CanCheckServer = Annotated[User, Depends(require_permission("server.check"))]


def _service(db: DbSession) -> InventoryService:
    return InventoryService(db)


ServiceDep = Annotated[InventoryService, Depends(_service)]


@router.get("", response_model=list[ServerOut])
def list_servers(_user: CanReadServers, service: ServiceDep) -> list[ServerOut]:
    """List managed hosts. Requires ``server.read``.

    ``_user`` is bound only so the authorisation dependency runs.
    """
    return [service.to_out(row) for row in service.list_all()]


@router.post("", response_model=ServerOut, status_code=201)
def create_server(
    payload: ServerCreate,
    user: CanCreateServer,
    service: ServiceDep,
) -> ServerOut:
    """Register a host. The host key is left unverified on purpose."""
    return service.to_out(service.create(payload, actor_id=user.id))


@router.get("/{server_id}", response_model=ServerOut)
def get_server(server_id: int, _user: CanReadServers, service: ServiceDep) -> ServerOut:
    return service.to_out(service.require(server_id))


@router.patch("/{server_id}", response_model=ServerOut)
def update_server(
    server_id: int,
    payload: ServerUpdate,
    user: CanUpdateServer,
    service: ServiceDep,
) -> ServerOut:
    """Edit a host. Changing the address drops the host-key pin."""
    return service.to_out(service.update(server_id, payload, actor_id=user.id))


@router.delete("/{server_id}", response_model=ServerOut)
def delete_server(server_id: int, user: CanDeleteServer, service: ServiceDep) -> ServerOut:
    """Soft-delete a host. The row stays so audit history and old tasks resolve."""
    return service.to_out(service.soft_delete(server_id, actor_id=user.id))


# --------------------------------------------------------------------------
# Host key trust — the two-step first-contact flow
# --------------------------------------------------------------------------
@router.post("/{server_id}/discover-hostkey", response_model=HostKeyDiscoverOut)
def discover_host_key(
    server_id: int,
    user: CanCheckServer,
    service: ServiceDep,
) -> HostKeyDiscoverOut:
    """Fetch the host's offered key and record it as pending confirmation.

    This performs no authentication and runs no command. Nothing is trusted
    until an operator echoes the fingerprint back through accept-hostkey.
    """
    return service.discover(server_id, actor_id=user.id)


@router.post("/{server_id}/accept-hostkey", response_model=ServerOut)
def accept_host_key(
    server_id: int,
    payload: HostKeyAcceptRequest,
    user: CanUpdateServer,
    service: ServiceDep,
) -> ServerOut:
    """Pin the discovered fingerprint after a human confirms it.

    Requires ``server.update``: trusting a host key is a change to the security
    posture of the platform, not a routine check.
    """
    return service.to_out(
        service.accept_host_key(server_id, payload.fingerprint, actor_id=user.id)
    )


# --------------------------------------------------------------------------
# Connectivity + environment check
# --------------------------------------------------------------------------
@router.post("/{server_id}/check", response_model=ServerCheckResult)
def check_server(
    server_id: int,
    user: CanCheckServer,
    service: ServiceDep,
) -> ServerCheckResult:
    """Run the read-only system check and store a facts snapshot.

    A reachable-but-unhealthy host returns ``ok: false`` with a detail message —
    that is a result, not an error. An untrusted host key refuses to run.
    """
    _row, result = service.run_check(server_id, actor_id=user.id)
    return result


@router.get("/{server_id}/facts", response_model=list[FactsOut])
def list_facts(
    server_id: int,
    _user: CanReadServers,
    service: ServiceDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[FactsOut]:
    """Historical facts snapshots, newest first. Append-only."""
    service.require(server_id)
    rows = service.list_facts(server_id, limit=limit)
    return [
        FactsOut(
            id=r.id,
            server_id=r.server_id,
            collected_at=r.collected_at,
            os_name=r.os_name,
            os_version=r.os_version,
            kernel=r.kernel,
            arch=r.arch,
            cpu_count=r.cpu_count,
            cpu_model=r.cpu_model,
            memory_total_mb=r.memory_total_mb,
            memory_available_mb=r.memory_available_mb,
            disk_total_gb=r.disk_total_gb,
            disk_available_gb=r.disk_available_gb,
        )
        for r in rows
    ]
