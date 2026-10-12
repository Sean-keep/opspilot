"""Application and environment CRUD.

Thin on purpose. The interesting rules — what a name is allowed to look like,
which service names may be restarted, which hosts a health check may point at
— live in :mod:`backend.app.applications.schemas` so they fire identically
however a row is created. This module owns persistence and the two lookups
the deployment service needs.

An application is never deleted through this API. A record that deployments
point at must outlive the UI that created it; retiring one is a deactivation
an operator performs by hand.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.app.applications.models import AppEnvironment, Application
from backend.app.applications.schemas import (
    AppEnvironmentCreate,
    AppEnvironmentUpdate,
    ApplicationCreate,
    ApplicationUpdate,
)
from backend.app.core.errors import Conflict, NotFound

__all__ = ["ApplicationService"]


class ApplicationService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def list_all(self) -> list[Application]:
        stmt = (
            select(Application)
            .options(selectinload(Application.environments))
            .order_by(Application.id)
            .execution_options(populate_existing=True)
        )
        return list(self.db.scalars(stmt))

    def require(self, application_id: int) -> Application:
        stmt = (
            select(Application)
            .options(selectinload(Application.environments))
            .where(Application.id == application_id)
            .execution_options(populate_existing=True)
        )
        row = self.db.scalars(stmt).one_or_none()
        if row is None:
            raise NotFound(f"no application with id {application_id}")
        return row

    def require_by_name(self, name: str) -> Application:
        stmt = (
            select(Application)
            .options(selectinload(Application.environments))
            .where(Application.name == name)
            .execution_options(populate_existing=True)
        )
        row = self.db.scalars(stmt).one_or_none()
        if row is None:
            raise NotFound(f"no application named {name!r}")
        return row

    def require_environment(self, application_id: int, name: str) -> AppEnvironment:
        stmt = (
            select(AppEnvironment)
            .where(
                AppEnvironment.application_id == application_id,
                AppEnvironment.name == name,
            )
            .execution_options(populate_existing=True)
        )
        row = self.db.scalars(stmt).one_or_none()
        if row is None:
            raise NotFound(f"application {application_id} has no environment {name!r}")
        return row

    def require_environment_by_id(self, environment_id: int) -> AppEnvironment:
        row = self.db.get(AppEnvironment, environment_id)
        if row is None:
            raise NotFound(f"no environment with id {environment_id}")
        return row

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def create(self, payload: ApplicationCreate) -> Application:
        existing = self.db.scalars(
            select(Application).where(Application.name == payload.name)
        ).one_or_none()
        if existing is not None:
            raise Conflict(f"an application named {payload.name!r} already exists")

        row = Application(**payload.model_dump())
        self.db.add(row)
        self.db.flush()
        return row

    def update(self, application_id: int, payload: ApplicationUpdate) -> Application:
        row = self.require(application_id)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(row, field, value)
        self.db.flush()
        return row

    def create_environment(
        self, application_id: int, payload: AppEnvironmentCreate
    ) -> AppEnvironment:
        self.require(application_id)
        try:
            self.require_environment(application_id, payload.name)
        except NotFound:
            pass
        else:
            raise Conflict(
                f"application {application_id} already has an environment {payload.name!r}"
            )

        row = AppEnvironment(application_id=application_id, **payload.model_dump())
        self.db.add(row)
        self.db.flush()
        return row

    def update_environment(
        self, environment_id: int, payload: AppEnvironmentUpdate
    ) -> AppEnvironment:
        row = self.require_environment_by_id(environment_id)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(row, field, value)
        self.db.flush()
        return row
