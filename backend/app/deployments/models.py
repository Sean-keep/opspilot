"""Deployment records.

A deployment is the *record* of one attempt to make a release live on one
environment. It is not the work itself — the work is an
:class:`~shared.task_protocol.models.Task`, and ``task_id`` points at it. The
record outlives the task so the operator can answer "what is live on
production, and what was live before it" long after the task logs have aged
out.

Three columns carry the rollback story and nothing else:

* ``release_id`` — what this deployment was trying to make live
* ``previous_release_id`` — what ``current`` pointed at when it started
* ``rollback_of`` — set when this deployment exists only to undo another

A rollback is therefore a **new** deployment whose target is the previous
release, not a mutation of the failed one. History stays honest: the failed
deploy keeps its own row and its own task, and the undo is separately
auditable and separately cancellable.

Status is deliberately small. ``ROLLED_BACK`` means this attempt failed *and*
the old link was put back; a bare ``FAILED`` means the host is in whatever
state it is in and needs a human.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base, BigIntPk, TimestampMixin, UTCDateTime

KIND_DEPLOY = "DEPLOY"
KIND_ROLLBACK = "ROLLBACK"
DEPLOYMENT_KINDS = (KIND_DEPLOY, KIND_ROLLBACK)

DEPLOY_PENDING = "PENDING"
DEPLOY_RUNNING = "RUNNING"
DEPLOY_SUCCEEDED = "SUCCEEDED"
DEPLOY_FAILED = "FAILED"
DEPLOY_ROLLED_BACK = "ROLLED_BACK"
DEPLOY_CANCELLED = "CANCELLED"
DEPLOYMENT_STATUSES = (
    DEPLOY_PENDING,
    DEPLOY_RUNNING,
    DEPLOY_SUCCEEDED,
    DEPLOY_FAILED,
    DEPLOY_ROLLED_BACK,
    DEPLOY_CANCELLED,
)

#: Statuses from which nothing more will happen on its own.
DEPLOYMENT_TERMINAL = (
    DEPLOY_SUCCEEDED,
    DEPLOY_FAILED,
    DEPLOY_ROLLED_BACK,
    DEPLOY_CANCELLED,
)


class Deployment(Base, TimestampMixin):
    """One attempt to make a release live on one environment."""

    __tablename__ = "ops_deployments"
    __table_args__ = (
        Index("ix_ops_deployments_environment_id_id", "environment_id", "id"),
        Index("ix_ops_deployments_application_id_id", "application_id", "id"),
        Index("ix_ops_deployments_status_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        BigIntPk,
        ForeignKey("ops_applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    environment_id: Mapped[int] = mapped_column(
        BigIntPk,
        ForeignKey("ops_app_environments.id", ondelete="CASCADE"),
        nullable=False,
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=KIND_DEPLOY)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=DEPLOY_PENDING)

    #: What the operator asked for — a tag, a commit, a build number. Refused
    #: when it is ``latest`` and the environment is production: a mutable alias
    #: is not a release.
    release_version: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Directory name under ``releases/``. Derived from ``release_version`` and
    #: stable, so a re-run of the same version lands in the same directory and
    #: the install steps become no-ops.
    release_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: What ``current`` pointed at before this deployment. Empty on a first
    #: deploy. This is what ``POST /deployments/{id}/rollback`` restores.
    previous_release_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    artifact_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    artifact_checksum: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    #: The task that does the work. A deployment is the durable record; the
    #: task is the execution attempt, and can be retried or cancelled without
    #: rewriting history.
    task_id: Mapped[int | None] = mapped_column(
        BigIntPk,
        ForeignKey("ops_tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    rollback_of: Mapped[int | None] = mapped_column(
        BigIntPk,
        ForeignKey("ops_deployments.id", ondelete="SET NULL"),
        nullable=True,
    )

    requested_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_detail: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
