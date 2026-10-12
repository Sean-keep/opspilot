"""Wire shapes for deployment records and the two ways to start one.

A deployment is submitted with a *pinned* release. ``release_version`` has to
be a concrete identifier — a tag, a commit, a build number — and is refused if
it is a mutable alias. That check is here rather than in the router so it is
impossible to reach the worker without passing it, and it is repeated in
:func:`shared.deploy_plan.build_deploy_plan` so the two cannot drift.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, field_validator
from shared.actions.builtin import check_release_id, check_url
from shared.redaction import redact_text

from backend.app.deployments.models import DEPLOYMENT_KINDS, DEPLOYMENT_STATUSES

__all__ = [
    "DeployCreate",
    "DeployTarget",
    "DeploymentOut",
    "RollbackCreate",
]


class DeployTarget(BaseModel):
    server_id: int = Field(gt=0)


class DeployCreate(BaseModel):
    """One pinned release, to one environment, on a fixed set of servers.

    ``config`` holds non-secret environment entries and is recorded like any
    other parameter. Secret entries come from the environment's ``config_ref``
    and are resolved by the worker — they never appear here.
    """

    environment: str = Field(min_length=1, max_length=32)
    release_version: str = Field(min_length=1, max_length=128)
    artifact_url: str = Field(min_length=1, max_length=512)
    artifact_checksum: str = Field(min_length=64, max_length=64)
    targets: list[DeployTarget] = Field(min_length=1, max_length=200)

    module_name: str = Field(default="", max_length=128)
    app_port: int = Field(default=8000, ge=1, le=65535)
    python_version: str = Field(default="3.12", max_length=16)
    requirements: str = Field(default="requirements.lock", max_length=128)
    config: dict[str, str] = Field(default_factory=dict)

    @field_validator("environment")
    @classmethod
    def _environment_is_boring(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if not cleaned or any(c in cleaned for c in " \t\n\r/\\"):
            raise ValueError("environment must be a plain name")
        return cleaned

    @field_validator("release_version")
    @classmethod
    def _release_is_pinned(cls, v: str) -> str:
        # Reuses the action-level rule: 'latest' and its siblings are reserved
        # because they name a moving target, and a deployment record that says
        # "latest" cannot answer what is live tomorrow.
        check_release_id(v)
        return v

    @field_validator("artifact_checksum")
    @classmethod
    def _checksum_is_a_digest(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if len(cleaned) != 64 or any(c not in "0123456789abcdef" for c in cleaned):
            raise ValueError("artifact_checksum must be a 64-character hex sha256")
        return cleaned

    @field_validator("artifact_url")
    @classmethod
    def _source_is_acceptable(cls, v: str) -> str:
        check_url(v, label="artifact_url")
        return v

    @field_validator("config")
    @classmethod
    def _config_keys_are_identifiers(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            if not key or not all(c.isalnum() or c == "_" for c in key):
                raise ValueError(f"config key {key!r} must be alphanumeric or underscore")
            if "\n" in value or "\r" in value:
                raise ValueError(f"config value {key!r} must not contain a newline")
        return v


class RollbackCreate(BaseModel):
    """Put a previous release back.

    Double confirmation on purpose: a rollback restarts a live service. The
    hidden button in the UI is not the control — refusing an unconfirmed
    request is.
    """

    targets: list[DeployTarget] = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=500)
    confirm: bool = False

    @field_validator("reason")
    @classmethod
    def _reason_is_boring(cls, v: str) -> str:
        return redact_text(v) if v else ""


class DeploymentOut(BaseModel):
    id: int
    application_id: int
    environment_id: int
    kind: str
    status: str
    release_version: str
    release_id: str
    previous_release_id: str
    artifact_url: str
    artifact_checksum: str
    task_id: int | None
    rollback_of: int | None
    requested_by: str
    error_detail: str
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime

    model_config = {"from_attributes": True}

    @field_validator("kind")
    @classmethod
    def _kind_is_known(cls, v: str) -> str:
        if v not in DEPLOYMENT_KINDS:
            raise ValueError(f"unknown deployment kind {v!r}")
        return v

    @field_validator("status")
    @classmethod
    def _status_is_known(cls, v: str) -> str:
        if v not in DEPLOYMENT_STATUSES:
            raise ValueError(f"unknown deployment status {v!r}")
        return v
