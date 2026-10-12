"""Request/response shapes for credentials.

The response models here have **no field for the secret**. That is the whole
point: if an endpoint ever needs to return key material, the model is wrong.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.credentials.models import CREDENTIAL_KINDS


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(default="ssh_key", max_length=32)
    description: str = Field(default="", max_length=4000)
    # The plaintext secret. Accepted on write, never echoed back.
    secret: str = Field(min_length=1, max_length=32768)
    meta: dict[str, Any] | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in CREDENTIAL_KINDS:
            raise ValueError(f"kind must be one of {', '.join(CREDENTIAL_KINDS)}")
        return v


class CredentialRotate(BaseModel):
    secret: str = Field(min_length=1, max_length=32768)


class CredentialOut(BaseModel):
    """Client-safe projection. Never contains the payload."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: str
    description: str
    status: str
    secret_ref: str
    fingerprint: str
    meta: dict[str, Any] | None
    used_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime
