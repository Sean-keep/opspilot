"""Application and environment endpoints.

Reading is ``app.read``; changing is ``app.create`` / ``app.update``. Nothing
here runs anything on a host — an application is a definition, and the first
command is issued only when someone submits a deployment.

An application is deliberately not deletable over HTTP. Deployment records
refer to it by id for as long as they exist, and a record whose application
has vanished cannot answer "what is live on production right now".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, status

from backend.app.api.routing import AppRouter
from backend.app.applications.schemas import (
    AppEnvironmentCreate,
    AppEnvironmentOut,
    AppEnvironmentUpdate,
    ApplicationCreate,
    ApplicationOut,
    ApplicationUpdate,
)
from backend.app.applications.service import ApplicationService
from backend.app.auth.deps import DbSession, require_permission
from backend.app.auth.models import User
from backend.app.core.errors import NotFound

router = AppRouter(prefix="/applications", tags=["applications"])

CanRead = Annotated[User, Depends(require_permission("app.read"))]
CanCreate = Annotated[User, Depends(require_permission("app.create"))]
CanUpdate = Annotated[User, Depends(require_permission("app.update"))]


def _service(db: DbSession) -> ApplicationService:
    return ApplicationService(db)


ServiceDep = Annotated[ApplicationService, Depends(_service)]


def _out(row: object) -> ApplicationOut:
    return ApplicationOut.model_validate(row)


@router.get("", response_model=list[ApplicationOut])
def list_applications(_user: CanRead, service: ServiceDep) -> list[ApplicationOut]:
    return [_out(row) for row in service.list_all()]


@router.post("", response_model=ApplicationOut, status_code=status.HTTP_201_CREATED)
def create_application(
    payload: ApplicationCreate, _user: CanCreate, service: ServiceDep
) -> ApplicationOut:
    row = service.create(payload)
    service.db.commit()
    return _out(service.require(row.id))


@router.get("/{application_id}", response_model=ApplicationOut)
def get_application(
    application_id: int, _user: CanRead, service: ServiceDep
) -> ApplicationOut:
    return _out(service.require(application_id))


@router.patch("/{application_id}", response_model=ApplicationOut)
def update_application(
    application_id: int,
    payload: ApplicationUpdate,
    _user: CanUpdate,
    service: ServiceDep,
) -> ApplicationOut:
    service.update(application_id, payload)
    service.db.commit()
    return _out(service.require(application_id))


@router.post(
    "/{application_id}/environments",
    response_model=AppEnvironmentOut,
    status_code=status.HTTP_201_CREATED,
)
def create_environment(
    application_id: int,
    payload: AppEnvironmentCreate,
    _user: CanUpdate,
    service: ServiceDep,
) -> AppEnvironmentOut:
    row = service.create_environment(application_id, payload)
    service.db.commit()
    return AppEnvironmentOut.model_validate(row)


@router.patch(
    "/{application_id}/environments/{environment_id}",
    response_model=AppEnvironmentOut,
)
def update_environment(
    application_id: int,
    environment_id: int,
    payload: AppEnvironmentUpdate,
    _user: CanUpdate,
    service: ServiceDep,
) -> AppEnvironmentOut:
    row = service.require_environment_by_id(environment_id)
    if row.application_id != application_id:
        # Refuse rather than silently editing a row under the wrong parent.
        service.require(application_id)
        raise NotFound(
            f"environment {environment_id} is not on application {application_id}"
        )
    service.update_environment(environment_id, payload)
    service.db.commit()
    return AppEnvironmentOut.model_validate(row)
