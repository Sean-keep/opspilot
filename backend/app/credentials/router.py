"""Credential endpoints.

Every response goes through :class:`CredentialView`, which has **no field for
the payload**. There is no endpoint that returns key material, and adding one
would be a security regression rather than a feature.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from backend.app.api.routing import AppRouter
from backend.app.audit.service import RESULT_SUCCESS, write_audit
from backend.app.auth.deps import DbSession, require_permission
from backend.app.auth.models import User
from backend.app.credentials.models import Credential
from backend.app.credentials.schemas import CredentialCreate, CredentialOut, CredentialRotate
from backend.app.credentials.service import CredentialService

router = AppRouter(prefix="/credentials", tags=["credentials"])

CanReadCredentials = Annotated[User, Depends(require_permission("credential.read"))]
CanCreateCredential = Annotated[User, Depends(require_permission("credential.create"))]
CanUpdateCredential = Annotated[User, Depends(require_permission("credential.update"))]
CanDeleteCredential = Annotated[User, Depends(require_permission("credential.delete"))]


def _service(db: DbSession) -> CredentialService:
    return CredentialService(db)


ServiceDep = Annotated[CredentialService, Depends(_service)]


def _out(row: Credential) -> CredentialOut:
    """Project to the response model. ``payload_encrypted`` is not a field here."""
    return CredentialOut(
        id=row.id,
        name=row.name,
        kind=row.kind,
        description=row.description,
        status=row.status,
        secret_ref=row.secret_ref,
        fingerprint=row.fingerprint,
        meta=row.meta_json,
        used_at=row.used_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[CredentialOut])
def list_credentials(
    _user: CanReadCredentials,
    service: ServiceDep,
    include_retired: bool = False,
) -> list[CredentialOut]:
    """Credential metadata only. Requires ``credential.read``."""
    return [_out(row) for row in service.list_all(include_retired=include_retired)]


@router.get("/{credential_id}", response_model=CredentialOut)
def get_credential(
    credential_id: int,
    _user: CanReadCredentials,
    service: ServiceDep,
) -> CredentialOut:
    return _out(service.require(credential_id))


@router.post("", response_model=CredentialOut, status_code=201)
def create_credential(
    payload: CredentialCreate,
    user: CanCreateCredential,
    service: ServiceDep,
) -> CredentialOut:
    """Store a new secret.

    The plaintext is accepted here and immediately encrypted. It is never
    echoed back, never logged, and never written to an audit snapshot.
    """
    row = service.create(
        name=payload.name,
        kind=payload.kind,
        secret=payload.secret,
        description=payload.description,
        meta=payload.meta,
    )
    write_audit(
        "credential.create",
        actor_id=user.id,
        target_type="credential",
        target_id=str(row.id),
        result=RESULT_SUCCESS,
        # Metadata only — deliberately no fingerprint, no payload.
        snapshot={"name": row.name, "kind": row.kind, "secret_ref": row.secret_ref},
    )
    return _out(row)


@router.post("/{credential_id}/rotate", response_model=CredentialOut)
def rotate_credential(
    credential_id: int,
    payload: CredentialRotate,
    user: CanUpdateCredential,
    service: ServiceDep,
) -> CredentialOut:
    """Replace the payload, keeping ``secret_ref`` stable."""
    service.require(credential_id)
    row = service.rotate(credential_id, secret=payload.secret, actor_id=user.id)
    return _out(row)


@router.post("/{credential_id}/retire", response_model=CredentialOut)
def retire_credential(
    credential_id: int,
    user: CanDeleteCredential,
    service: ServiceDep,
) -> CredentialOut:
    """Soft-delete. The row stays so audit history and old tasks resolve."""
    service.require(credential_id)
    row = service.retire(credential_id, actor_id=user.id)
    return _out(row)

