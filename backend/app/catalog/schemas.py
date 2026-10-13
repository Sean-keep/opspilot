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


class TemplateApplyTarget(BaseModel):
    """One server the template should be applied to."""

    server_id: int = Field(gt=0)


class TemplateApplyIn(BaseModel):
    """Run a published template version against one or more servers.

    ``inputs`` are the non-secret values the template declares. Anything the
    template fills from a stored secret is named only by ``config_ref`` and is
    resolved on the worker at execution time — the values never appear here,
    in the task row, or in a log line.
    """

    inputs: dict[str, Any] = Field(default_factory=dict)
    targets: list[TemplateApplyTarget] = Field(min_length=1, max_length=200)
    config_ref: str = Field(default="", max_length=255)
