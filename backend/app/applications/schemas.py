"""Wire shapes for applications and environments.

Validation lives here rather than in the router so a bad value is refused the
same way whether it arrives over HTTP or is built in a test. The rules mirror
the ones the actions enforce at execution time — an application name becomes a
directory under ``/opt/opspilot/apps/``, a unit path has to be a real unit, a
health URL has to be a target the SSRF filter will accept — so a submission
that the worker would reject at step 6 is rejected at submit instead.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator
from shared.actions.builtin import check_service_allowed, check_url

from backend.app.applications.models import ENVIRONMENTS, RUNTIMES

__all__ = [
    "RESERVED_APP_NAMES",
    "AppEnvironmentCreate",
    "AppEnvironmentOut",
    "AppEnvironmentUpdate",
    "ApplicationCreate",
    "ApplicationOut",
    "ApplicationUpdate",
]

#: Directory names under ``/opt/opspilot/apps/<app>/`` that the layout owns.
#: An application called ``current`` would make ``current -> releases/<id>``
#: impossible to express.
RESERVED_APP_NAMES = frozenset({"releases", "shared", "current"})

_APP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CRED_REF_RE = re.compile(r"^cred:[1-9][0-9]{0,17}$")


def _app_name(value: str) -> str:
    cleaned = value.strip().lower()
    if not _APP_NAME_RE.match(cleaned):
        raise ValueError(
            "name must be lowercase letters, digits and dashes, starting with a letter"
        )
    if cleaned in RESERVED_APP_NAMES:
        raise ValueError(f"name {cleaned!r} is reserved by the release layout")
    return cleaned


class ApplicationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    runtime_type: str = Field(default="python", max_length=32)
    repo_url: str = Field(default="", max_length=512)
    default_branch: str = Field(default="", max_length=128)
    run_user: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=512)

    @field_validator("name")
    @classmethod
    def _name_is_a_directory(cls, v: str) -> str:
        return _app_name(v)

    @field_validator("runtime_type")
    @classmethod
    def _runtime_is_known(cls, v: str) -> str:
        if v not in RUNTIMES:
            raise ValueError(f"runtime_type must be one of {list(RUNTIMES)}")
        return v

    @field_validator("run_user")
    @classmethod
    def _user_is_a_username(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            return ""
        if cleaned == "root":
            raise ValueError("the service must not run as root")
        if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", cleaned):
            raise ValueError("run_user must be a valid non-root username")
        return cleaned


class ApplicationUpdate(BaseModel):
    runtime_type: str | None = Field(default=None, max_length=32)
    repo_url: str | None = Field(default=None, max_length=512)
    default_branch: str | None = Field(default=None, max_length=128)
    run_user: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=512)

    @field_validator("runtime_type")
    @classmethod
    def _runtime_is_known(cls, v: str | None) -> str | None:
        if v is not None and v not in RUNTIMES:
            raise ValueError(f"runtime_type must be one of {list(RUNTIMES)}")
        return v

    @field_validator("run_user")
    @classmethod
    def _user_is_a_username(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return ApplicationCreate._user_is_a_username(v)


class AppEnvironmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    config_ref: str = Field(default="", max_length=255)
    service_name: str = Field(default="", max_length=64)
    unit_path: str = Field(default="", max_length=255)
    health_url: str = Field(default="", max_length=512)
    health_timeout_s: int = Field(default=10, ge=1, le=60)
    keep_releases: int = Field(default=5, ge=1, le=100)

    @field_validator("name")
    @classmethod
    def _name_is_known(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if cleaned not in ENVIRONMENTS:
            raise ValueError(f"name must be one of {list(ENVIRONMENTS)}")
        return cleaned

    @field_validator("config_ref")
    @classmethod
    def _ref_is_a_reference(cls, v: str) -> str:
        cleaned = v.strip()
        if cleaned and not _CRED_REF_RE.match(cleaned):
            raise ValueError("config_ref must look like 'cred:<id>', or be empty")
        return cleaned

    @field_validator("service_name")
    @classmethod
    def _service_is_whitelisted(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            return ""
        check_service_allowed(cleaned)
        return cleaned

    @field_validator("unit_path")
    @classmethod
    def _unit_is_a_unit(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            return ""
        if not cleaned.startswith("/etc/systemd/system/"):
            raise ValueError("unit_path must live under /etc/systemd/system/")
        if not cleaned.endswith(".service"):
            raise ValueError("unit_path must be a .service file")
        if "/" in cleaned[len("/etc/systemd/system/") :]:
            raise ValueError("unit_path must not create subdirectories")
        return cleaned

    @field_validator("health_url")
    @classmethod
    def _health_is_an_acceptable_target(cls, v: str) -> str:
        cleaned = v.strip()
        if cleaned:
            check_url(cleaned, label="health_url")
        return cleaned

    @model_validator(mode="after")
    def _unit_and_service_agree(self) -> AppEnvironmentCreate:
        if self.service_name and not self.unit_path:
            raise ValueError("unit_path is required when service_name is set")
        if self.unit_path and not self.service_name:
            raise ValueError("service_name is required when unit_path is set")
        return self


class AppEnvironmentUpdate(BaseModel):
    """In-place edits.

    ``name`` is absent on purpose: an environment's name is its identity
    within the application, and every deployment record that ever pointed at
    it refers to that name. Renaming would rewrite history.
    """

    config_ref: str | None = Field(default=None, max_length=255)
    service_name: str | None = Field(default=None, max_length=64)
    unit_path: str | None = Field(default=None, max_length=255)
    health_url: str | None = Field(default=None, max_length=512)
    health_timeout_s: int | None = Field(default=None, ge=1, le=60)
    keep_releases: int | None = Field(default=None, ge=1, le=100)

    @field_validator("config_ref")
    @classmethod
    def _ref_is_a_reference(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return AppEnvironmentCreate._ref_is_a_reference(v)

    @field_validator("service_name")
    @classmethod
    def _service_is_whitelisted(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return AppEnvironmentCreate._service_is_whitelisted(v)

    @field_validator("unit_path")
    @classmethod
    def _unit_is_a_unit(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return AppEnvironmentCreate._unit_is_a_unit(v)

    @field_validator("health_url")
    @classmethod
    def _health_is_an_acceptable_target(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return AppEnvironmentCreate._health_is_an_acceptable_target(v)


class AppEnvironmentOut(BaseModel):
    id: int
    application_id: int
    name: str
    config_ref: str
    service_name: str
    unit_path: str
    health_url: str
    health_timeout_s: int
    keep_releases: int

    model_config = {"from_attributes": True}


class ApplicationOut(BaseModel):
    id: int
    name: str
    runtime_type: str
    repo_url: str
    default_branch: str
    run_user: str
    description: str
    environments: list[AppEnvironmentOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}
