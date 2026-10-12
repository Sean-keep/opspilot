"""Wire shapes for the template catalog.

Nothing here carries executable content to the client beyond what an operator
needs to see: the step list is names and parameters, and a template has no
credentials of its own.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TemplateVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: str
    status: str
    checksum: str
    notes: str
    published_at: dt.datetime | None
    deprecated_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: str
    description: str
    latest_version: str
    versions: list[TemplateVersionOut] = Field(default_factory=list)
    created_at: dt.datetime
    updated_at: dt.datetime


class TemplateVersionDetailOut(TemplateVersionOut):
    """A version with its body. Used when an operator is inspecting a template."""

    spec: dict[str, Any]
    inputs_schema: dict[str, Any]


class TemplateValidateResult(BaseModel):
    """What the validator found. ``ok`` means the version may move to VALIDATED."""

    ok: bool
    template: str
    version: str
    checksum: str
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    status: str
