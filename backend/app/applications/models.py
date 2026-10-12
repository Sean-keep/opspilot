"""Application and environment tables.

An *application* is the thing that gets deployed; an *environment* is one
deployment target for it (test / staging / production). The split matters
because the dangerous settings — health endpoint, service name, how many old
releases to keep — are per-environment, not per-application: the same artifact
is promoted from test to production, but production is where a failed health
check has to stop the batch.

``ops_applications.name`` doubles as the directory name under
``/opt/opspilot/apps/``. It is validated to a conservative slug so it can
never escape that tree or collide with the reserved ``releases`` / ``shared`` /
``current`` siblings.

``config_ref`` is a *reference* to a secret, never the secret itself. The
value is resolved by the worker at execution time and is redacted out of every
log line and every ``result_json``.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.base import Base, BigIntPk, TimestampMixin

# Runtime kinds. V1 is Python-only; the column exists so the next runtime does
# not need a migration to be expressible.
RUNTIME_PYTHON = "python"
RUNTIMES = (RUNTIME_PYTHON,)

# Environment names. Free text would make "is this production?" unanswerable at
# the permission layer, so the names are closed.
ENV_TEST = "test"
ENV_STAGING = "staging"
ENV_PRODUCTION = "production"
ENVIRONMENTS = (ENV_TEST, ENV_STAGING, ENV_PRODUCTION)


class Application(Base, TimestampMixin):
    """A deployable service."""

    __tablename__ = "ops_applications"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    #: Directory name under /opt/opspilot/apps/. Unique because two apps cannot
    #: share a tree on a host.
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    runtime_type: Mapped[str] = mapped_column(String(32), nullable=False, default=RUNTIME_PYTHON)
    #: Default artifact source. Every URL is SSRF-checked again at deploy time.
    repo_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    default_branch: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    #: The non-root account the service runs as. Never root.
    run_user: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    environments: Mapped[list[AppEnvironment]] = relationship(
        "AppEnvironment",
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="AppEnvironment.id",
    )


class AppEnvironment(Base, TimestampMixin):
    """One deployment target for an application."""

    __tablename__ = "ops_app_environments"
    __table_args__ = (
        UniqueConstraint(
            "application_id", "name", name="uq_ops_app_environments_application_name"
        ),
        Index("ix_ops_app_environments_name", "name"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        BigIntPk,
        ForeignKey("ops_applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)

    #: Reference to the secret holding this environment's config. The value is
    #: never stored here and never returned by the API.
    config_ref: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    service_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    unit_path: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    health_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    health_timeout_s: Mapped[int] = mapped_column(Integer, nullable=False, default=10)

    #: How many old release directories to leave on the host. Cleaning is
    #: always an explicit operator action — ``shared/data`` in particular is
    #: never auto-cleaned, whatever this is set to.
    keep_releases: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    application: Mapped[Application] = relationship("Application", back_populates="environments")
