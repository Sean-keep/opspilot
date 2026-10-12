"""Pydantic request/response models for authentication and identity."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """Credentials submitted by the login form."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    """Returned on successful login."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth scheme name, not a secret
    expires_in: int
    user: UserOut


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str


class PermissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    description: str


class UserOut(BaseModel):
    """A user as the frontend sees it.

    ``password_hash`` is deliberately absent. This is the only user shape that
    ever crosses the API boundary.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    email: str
    status: str
    roles: list[RoleOut] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    last_login_at: dt.datetime | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(default="", max_length=128)
    email: str = Field(default="", max_length=255)
    role_names: list[str] = Field(default_factory=list)


class MeOut(BaseModel):
    user: UserOut
    permissions: list[str]
