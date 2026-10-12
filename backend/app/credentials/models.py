"""Credential storage.

A credential row holds **metadata plus an encrypted payload**. The plaintext
(private key, password) is never returned by the API and never written to a log
or an audit snapshot. Only the Worker decrypts it, at the moment it is needed.

``secret_ref`` is the stable handle a template or task refers to (``cred:<id>``)
and is the only thing ever stored in ``params_snapshot_json``.

Encryption is Fernet (AES-128-CBC + HMAC) with a master key supplied from the
environment — preferably ``OPSPILOT_MASTER_KEY_FILE``, so the key is not stored
next to the ciphertext it protects.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.base import (
    Base,
    BigIntPk,
    Name,
    ShortStatus,
    TimestampMixin,
    UTCDateTime,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from backend.app.inventory.models import Server

# Credential kinds. Explicit, short, never free text.
# These are *kind labels*, not secrets — S105 is a false positive here.
CRED_SSH_KEY = "ssh_key"
CRED_SSH_PASSWORD = "ssh_password"  # noqa: S105
CRED_SUDO_PASSWORD = "sudo_password"  # noqa: S105
CREDENTIAL_KINDS = (CRED_SSH_KEY, CRED_SSH_PASSWORD, CRED_SUDO_PASSWORD)

CREDENTIAL_ACTIVE = "active"
CREDENTIAL_RETIRED = "retired"
CREDENTIAL_STATUSES = (CREDENTIAL_ACTIVE, CREDENTIAL_RETIRED)


class Credential(Base, TimestampMixin):
    """An encrypted secret plus the metadata that is safe to show."""

    __tablename__ = "ops_credentials"
    __table_args__ = (UniqueConstraint("name", name="uq_ops_credentials_name"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Name, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default=CRED_SSH_KEY)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(ShortStatus, nullable=False, default=CREDENTIAL_ACTIVE)

    # The handle used by templates and task parameters. Derived from id, but
    # stored so a reference survives a re-import. Never a secret.
    secret_ref: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)

    # Fernet ciphertext. NEVER selected into an API response.
    payload_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Public, non-secret fingerprint (e.g. an SSH public key fingerprint) so an
    # operator can tell two keys apart without seeing either of them.
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # Free-form non-secret metadata (key type, comment, rotation date …).
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    used_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    servers: Mapped[list[Server]] = relationship(back_populates="credential")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Credential id={self.id} name={self.name!r} kind={self.kind!r}>"
