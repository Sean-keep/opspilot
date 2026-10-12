"""Deployment endpoints: submit, observe, and roll back.

Two permissions do the work. ``deployment.create`` queues a release;
``deployment.rollback`` creates the undo. Both are enforced here — the
confirmation dialog in the UI is a courtesy, and the check that actually
refuses an unconfirmed rollback lives in
:meth:`~backend.app.deployments.service.DeploymentService.rollback`.

A submission returns **202 Accepted** with a ``Location`` pointing at the
deployment record. Nothing has been installed yet: the record and its task
exist, the work is queued for the Worker, and the task is where the steps and
the logs will appear.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Query, Response, status

from backend.app.api.routing import AppRouter
from backend.app.auth.deps import DbSession, require_permission
from backend.app.auth.models import User
from backend.app.deployments.schemas import (
    DeployCreate,
    DeploymentOut,
    RollbackCreate,
)
from backend.app.deployments.service import DeploymentService

router = AppRouter(prefix="/deployments", tags=["deployments"])
app_router = AppRouter(prefix="/applications", tags=["deployments"])

CanRead = Annotated[User, Depends(require_permission("deployment.read"))]
CanDeploy = Annotated[User, Depends(require_permission("deployment.create"))]
CanRollback = Annotated[User, Depends(require_permission("deployment.rollback"))]


def _service(db: DbSession) -> DeploymentService:
    return DeploymentService(db)


ServiceDep = Annotated[DeploymentService, Depends(_service)]

Idempotency = Annotated[str | None, Header(alias="Idempotency-Key")]


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------
@router.get("", response_model=list[DeploymentOut])
def list_deployments(
    _user: CanRead,
    service: ServiceDep,
    application_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[DeploymentOut]:
    """Newest first. Filter by application, or read the whole history."""
    if application_id is not None:
        rows = service.list_for_application(application_id, limit=limit)
    else:
        rows = service.list_all(limit=limit, offset=offset)
    return [DeploymentOut.model_validate(row) for row in rows]


@router.get("/{deployment_id}", response_model=DeploymentOut)
def get_deployment(
    deployment_id: int, _user: CanRead, service: ServiceDep
) -> DeploymentOut:
    return DeploymentOut.model_validate(service.require(deployment_id))


@router.post("/{deployment_id}/rollback", response_model=DeploymentOut, status_code=202)
def rollback_deployment(
    deployment_id: int,
    payload: RollbackCreate,
    user: CanRollback,
    service: ServiceDep,
    response: Response,
    idempotency_key: Idempotency = None,
) -> DeploymentOut:
    """Put a previous release back, as a **new** deployment.

    ``confirm`` must be true. That is the second confirmation: the UI asks, and
    this handler refuses anything that did not answer.

    The response is the new record, not the one being undone — its ``kind`` is
    ``ROLLBACK`` and its ``rollback_of`` names the deployment it reverses. The
    original keeps its own status so history can say what happened, not only
    what the operator did next.
    """
    row, _created = service.rollback(
        deployment_id,
        payload,
        actor_id=user.id,
        actor_name=user.username,
        idempotency_key=idempotency_key or "",
    )
    response.headers["Location"] = f"/api/v1/deployments/{row.id}"
    return DeploymentOut.model_validate(row)


# --------------------------------------------------------------------------
# Submitting a release
# --------------------------------------------------------------------------
@app_router.post(
    "/{application_id}/deploy",
    response_model=DeploymentOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def deploy_release(
    application_id: int,
    payload: DeployCreate,
    user: CanDeploy,
    service: ServiceDep,
    response: Response,
    idempotency_key: Idempotency = None,
) -> DeploymentOut:
    """Queue one pinned release for one environment.

    The release must be pinned: ``release_version`` names a tag, a commit or a
    build number, never ``latest``. The checksum is required and is verified on
    the host before anything is unpacked.

    ``config`` holds non-secret environment entries and is recorded like any
    other parameter. Secrets come from the environment's ``config_ref`` and are
    resolved by the Worker — they never appear in a request body, a task row or
    a log line.

    Send ``Idempotency-Key`` to make a retry of the same submission safe: the
    original deployment comes back instead of a second one being queued.
    """
    row, _created = service.create(
        application_id,
        payload,
        actor_id=user.id,
        actor_name=user.username,
        idempotency_key=idempotency_key or "",
    )
    response.headers["Location"] = f"/api/v1/deployments/{row.id}"
    return DeploymentOut.model_validate(row)


@app_router.get(
    "/{application_id}/deployments",
    response_model=list[DeploymentOut],
)
def list_application_deployments(
    application_id: int,
    _user: CanRead,
    service: ServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[DeploymentOut]:
    """One application's release history, newest first."""
    rows = service.list_for_application(application_id, limit=limit)
    return [DeploymentOut.model_validate(row) for row in rows]
