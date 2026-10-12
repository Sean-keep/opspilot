"""Template catalog tables.

A template has a stable name; a *version* is one immutable body of
``template.yaml`` plus its input schema. The split matters: publishing is the
act of freezing a body, and a task pins the version row, not the name — so
editing a template can never change what an already-submitted task will do.

Only the lifecycle columns move after insert. ``spec_json``,
``inputs_schema_json`` and ``checksum`` are written once and never updated;
:mod:`backend.app.catalog.service` refuses the mutation and a test proves the
body is byte-identical afterwards.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.base import Base, BigIntPk, TimestampMixin, UTCDateTime

# Template categories — one directory under templates/.
CATEGORY_RUNTIME = "runtime"
CATEGORY_WEB = "web"
CATEGORY_APPLICATION = "application"
CATEGORY_DATABASE = "database"
CATEGORY_MIDDLEWARE = "middleware"
TEMPLATE_CATEGORIES = (
    CATEGORY_RUNTIME,
    CATEGORY_WEB,
    CATEGORY_APPLICATION,
    CATEGORY_DATABASE,
    CATEGORY_MIDDLEWARE,
)

# Version lifecycle. Mirrors shared.templates.TEMPLATE_STATUSES — repeated here
# as literals so the model module does not import the validator just for names.
TEMPLATE_DRAFT = "DRAFT"
TEMPLATE_VALIDATED = "VALIDATED"
TEMPLATE_TESTED = "TESTED"
TEMPLATE_PUBLISHED = "PUBLISHED"
TEMPLATE_DEPRECATED = "DEPRECATED"
TEMPLATE_STATUSES = (
    TEMPLATE_DRAFT,
    TEMPLATE_VALIDATED,
    TEMPLATE_TESTED,
    TEMPLATE_PUBLISHED,
    TEMPLATE_DEPRECATED,
)

#: Statuses in which the version body is frozen. Everything but DRAFT counts:
#: VALIDATED and TESTED already refer to a body that passed a check, so
#: rewriting it under those states would quietly invalidate the check.
FROZEN_STATUSES = (
    TEMPLATE_VALIDATED,
    TEMPLATE_TESTED,
    TEMPLATE_PUBLISHED,
    TEMPLATE_DEPRECATED,
)


class Template(Base, TimestampMixin):
    """The stable identity of a template."""

    __tablename__ = "ops_templates"
    __table_args__ = (Index("ix_ops_templates_category", "category"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # Newest version number, for listing. Never an implicit "run this one".
    latest_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    versions: Mapped[list[TemplateVersion]] = relationship(
        "TemplateVersion",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="TemplateVersion.id",
    )


class TemplateVersion(Base, TimestampMixin):
    """One immutable body of a template."""

    __tablename__ = "ops_template_versions"
    __table_args__ = (
        UniqueConstraint(
            "template_id", "version", name="uq_ops_template_versions_template_version"
        ),
        Index("ix_ops_template_versions_status", "status", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        BigIntPk,
        ForeignKey("ops_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=TEMPLATE_DRAFT)

    #: SHA-256 of the template's identity files, recomputed before execution.
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: Written once at insert. See FROZEN_STATUSES.
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    inputs_schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    deprecated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    template: Mapped[Template] = relationship("Template", back_populates="versions")
