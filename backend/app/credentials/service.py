"""Credential encryption.

Plaintext exists in exactly two places: the moment a credential is written (the
caller hands us the secret) and the moment the Worker decrypts it to use it.
In between it is Fernet ciphertext in MySQL.

The master key comes from the environment — preferably
``OPSPILOT_MASTER_KEY_FILE``, so the key is not stored next to the ciphertext
it protects. ``Settings.resolve_master_key`` refuses group/world-readable key
files.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from backend.app.audit.service import write_audit
from backend.app.core.errors import NotFound, ValidationError
from backend.app.core.logging import get_logger
from backend.app.core.settings import Settings, get_settings
from backend.app.credentials.models import (
    CREDENTIAL_ACTIVE,
    CREDENTIAL_KINDS,
    Credential,
)
from backend.app.db.base import utcnow

log = get_logger(__name__)

__all__ = ["CredentialService", "CredentialWriteError"]


class CredentialWriteError(ValidationError):
    """Raised when a credential payload cannot be stored or read back."""


@dataclass(frozen=True, slots=True)
class CredentialView:
    """Everything about a credential that is safe to return to a client.

    Deliberately has no field for the payload. If a future endpoint needs to
    show a secret, that is a bug — not a reason to widen this type.
    """

    id: int
    name: str
    kind: str
    description: str
    status: str
    secret_ref: str
    fingerprint: str
    meta: dict[str, object] | None
    used_at: object
    created_at: object
    updated_at: object


def _fernet(settings: Settings | None = None) -> Fernet:
    cfg = settings or get_settings()
    raw = cfg.resolve_master_key()
    try:
        return Fernet(raw.encode("utf-8"))
    except (ValueError, TypeError) as exc:  # pragma: no cover - config error path
        raise CredentialWriteError(
            "master key is not a valid Fernet key; generate one with "
            "cryptography.fernet.Fernet.generate_key()"
        ) from exc


def public_fingerprint(value: str) -> str:
    """A stable, non-secret fingerprint of a credential payload.

    Used so an operator can tell two private keys apart without seeing either.
    This is a digest of the material, not the material itself — but it is still
    derived from a secret, so it never goes into an audit snapshot either.
    """
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"SHA256:{digest[:32]}"


class CredentialService:
    """Read/write credentials against a session."""

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get(self, credential_id: int) -> Credential | None:
        return self.session.get(Credential, credential_id)

    def get_by_name(self, name: str) -> Credential | None:
        from sqlalchemy import select

        return self.session.scalar(select(Credential).where(Credential.name == name))

    def require(self, credential_id: int) -> Credential:
        row = self.get(credential_id)
        if row is None:
            raise NotFound(f"credential {credential_id} not found")
        return row

    def list_all(self, *, include_retired: bool = False) -> list[Credential]:
        from sqlalchemy import select

        stmt = select(Credential).order_by(Credential.id)
        if not include_retired:
            stmt = stmt.where(Credential.status == CREDENTIAL_ACTIVE)
        return list(self.session.scalars(stmt))

    @staticmethod
    def to_view(row: Credential) -> CredentialView:
        """Project to the client-safe shape. Never includes the payload."""
        return CredentialView(
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

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def create(
        self,
        *,
        name: str,
        kind: str,
        secret: str,
        description: str = "",
        meta: dict[str, object] | None = None,
    ) -> Credential:
        if kind not in CREDENTIAL_KINDS:
            raise ValidationError(f"unknown credential kind: {kind!r}")
        if not secret:
            raise ValidationError("credential secret must not be empty")
        if self.get_by_name(name) is not None:
            raise ValidationError(f"credential name {name!r} is already taken")

        payload = self._encrypt(secret)
        row = Credential(
            name=name,
            kind=kind,
            description=description,
            payload_encrypted=payload,
            secret_ref="",  # filled after we know the id
            fingerprint=public_fingerprint(secret),
            meta_json=meta,
        )
        self.session.add(row)
        self.session.flush()
        row.secret_ref = f"cred:{row.id}"
        self.session.flush()
        return row

    def rotate(self, credential_id: int, *, secret: str, actor_id: int | None = None) -> Credential:
        """Replace the payload of an existing credential.

        Rotation keeps the ``secret_ref`` stable so templates and tasks that
        reference it keep working. It is deliberately NOT a create+delete:
        that would break the reference and the audit trail.
        """
        if not secret:
            raise ValidationError("credential secret must not be empty")
        row = self.require(credential_id)
        row.payload_encrypted = self._encrypt(secret)
        row.fingerprint = public_fingerprint(secret)
        row.used_at = None
        self.session.flush()
        write_audit(
            "credential.rotate",
            actor_id=actor_id,
            target_type="credential",
            target_id=str(row.id),
            snapshot={"name": row.name, "kind": row.kind, "fingerprint": row.fingerprint},
        )
        return row

    def retire(self, credential_id: int, *, actor_id: int | None = None) -> Credential:
        """Soft-delete. The row stays so audit history and old tasks resolve."""
        row = self.require(credential_id)
        row.status = "retired"
        self.session.flush()
        write_audit(
            "credential.retire",
            actor_id=actor_id,
            target_type="credential",
            target_id=str(row.id),
            snapshot={"name": row.name},
        )
        return row

    def mark_used(self, credential_id: int) -> None:
        row = self.get(credential_id)
        if row is not None:
            row.used_at = utcnow()
            self.session.flush()

    # ------------------------------------------------------------------
    # Secret material
    # ------------------------------------------------------------------
    def decrypt(self, credential_id: int) -> str:
        """Return the plaintext secret.

        **Only the Worker may call this**, at the moment the secret is needed.
        The result must never be logged, audited, or returned over HTTP.
        """
        row = self.require(credential_id)
        return self._decrypt(row.payload_encrypted)

    def _encrypt(self, secret: str) -> str:
        return _fernet(self.settings).encrypt(secret.encode("utf-8")).decode("ascii")

    def _decrypt(self, payload: str) -> str:
        try:
            return _fernet(self.settings).decrypt(payload.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise CredentialWriteError(
                "credential payload cannot be decrypted with the configured master key"
            ) from exc
