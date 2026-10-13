"""Template catalog endpoints.

Reading a template is ``template.read``. Changing a version's *status* is the
dangerous half and is split by intent: ``template.publish`` freezes a body
forever, ``template.deprecate`` retires it. Validation itself is
``template.validate`` and is deliberately non-mutating — except that a version
still sitting in ``DRAFT`` is promoted to ``VALIDATED`` when the checks pass,
which is the whole point of running them.

No handler here executes anything on a remote host. A template is data; the
Worker is what eventually turns one into commands, and only against a version
that is already published.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Response

from backend.app.api.routing import AppRouter
from backend.app.audit.service import write_audit
from backend.app.auth.deps import DbSession, require_permission
from backend.app.auth.models import User
from backend.app.auth.service import AuthService
from backend.app.catalog.models import TEMPLATE_DEPRECATED, Template, TemplateVersion
from backend.app.catalog.schemas import (
    TemplateApplyIn,
    TemplateOut,
    TemplateValidateResult,
    TemplateVersionDetailOut,
    TemplateVersionOut,
)
from backend.app.catalog.service import CatalogService, InvalidTransition, TemplateConflict
from backend.app.core.errors import Conflict, Forbidden, NotFound
from backend.app.orchestrator.schemas import TaskOut
from backend.app.orchestrator.service import TaskService

router = AppRouter(prefix="/templates", tags=["catalog"])

CanRead = Annotated[User, Depends(require_permission("template.read"))]
CanValidate = Annotated[User, Depends(require_permission("template.validate"))]
CanPublish = Annotated[User, Depends(require_permission("template.publish"))]
CanDeprecate = Annotated[User, Depends(require_permission("template.deprecate"))]
CanApply = Annotated[User, Depends(require_permission("template.apply"))]

#: Categories that reconfigure a *shared* service rather than one application's
#: own files. Applying one of these is a ``middleware.install`` in disguise, so
#: holding only ``template.apply`` must not be enough to reach it — otherwise
#: the permission that exists to gate installing Redis would be decorative.
MIDDLEWARE_CATEGORIES = frozenset({"middleware", "database"})

def _service(db: DbSession) -> CatalogService:
    return CatalogService(db)


ServiceDep = Annotated[CatalogService, Depends(_service)]


def _tasks(db: DbSession) -> TaskService:
    return TaskService(db)


TasksDep = Annotated[TaskService, Depends(_tasks)]


def _map_conflict[T](fn: Callable[[], T]) -> T:
    """Run a service call, turning lifecycle refusals into HTTP conflicts."""
    try:
        return fn()
    except TemplateConflict as exc:
        raise Conflict(str(exc)) from exc
    except InvalidTransition as exc:
        raise Conflict(str(exc)) from exc


# --------------------------------------------------------------------------
# Wire shaping
# --------------------------------------------------------------------------
def _version_out(row: TemplateVersion) -> TemplateVersionOut:
    return TemplateVersionOut.model_validate(row)


def _template_out(row: Template) -> TemplateOut:
    return TemplateOut(
        id=row.id,
        name=row.name,
        category=row.category,
        description=row.description,
        latest_version=row.latest_version,
        versions=[_version_out(v) for v in row.versions],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _version_detail(row: TemplateVersion) -> TemplateVersionDetailOut:
    return TemplateVersionDetailOut(
        **_version_out(row).model_dump(),
        spec=dict(row.spec_json or {}),
        inputs_schema=dict(row.inputs_schema_json or {}),
    )


def _require_version(service: CatalogService, template_id: int, version: str) -> Template:
    template = service.require_by_id(template_id)
    if service.get_version(template.name, version) is None:
        raise NotFound(f"no template {template.name!r} version {version!r}")
    return template


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
@router.get("", response_model=list[TemplateOut])
def list_templates(_user: CanRead, service: ServiceDep) -> list[TemplateOut]:
    """Every known template with its version history, oldest version first."""
    return [_template_out(row) for row in service.list_all()]


@router.get("/{template_id}", response_model=TemplateOut)
def get_template(template_id: int, _user: CanRead, service: ServiceDep) -> TemplateOut:
    return _template_out(service.require_by_id(template_id))


@router.get("/{template_id}/versions/{version}", response_model=TemplateVersionDetailOut)
def get_version(
    template_id: int,
    version: str,
    _user: CanRead,
    service: ServiceDep,
) -> TemplateVersionDetailOut:
    """One version with its body — what an operator reads before pinning."""
    template = _require_version(service, template_id, version)
    row = service.get_version(template.name, version)
    assert row is not None  # _require_version just checked
    return _version_detail(row)


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
@router.post("/{template_id}/validate", response_model=TemplateValidateResult)
def validate_latest(
    template_id: int,
    _user: CanValidate,
    service: ServiceDep,
) -> TemplateValidateResult:
    """Re-run every template check against the version ``latest_version`` names.

    Returns the full problem list rather than 4xx-ing on a bad template: an
    operator needs to see *why* a version will not move, and a version that
    fails validation simply stays where it is.
    """
    template = service.require_by_id(template_id)
    if not template.latest_version:
        raise NotFound(f"template {template.name!r} has no versions to validate")
    return TemplateValidateResult(
        **service.validate_version(template.name, template.latest_version)
    )


@router.post(
    "/{template_id}/versions/{version}/validate",
    response_model=TemplateValidateResult,
)
def validate_version(
    template_id: int,
    version: str,
    _user: CanValidate,
    service: ServiceDep,
) -> TemplateValidateResult:
    template = _require_version(service, template_id, version)
    return TemplateValidateResult(**service.validate_version(template.name, version))


@router.post("/{template_id}/versions/{version}/publish", response_model=TemplateVersionOut)
def publish_version(
    template_id: int,
    version: str,
    user: CanPublish,
    service: ServiceDep,
    notes: str = "",
) -> TemplateVersionOut:
    """Freeze a version's body. Only deprecation follows from here."""
    template = _require_version(service, template_id, version)
    row = _map_conflict(lambda: service.publish(template.name, version, notes=notes))
    service.db.commit()
    write_audit(
        "template.publish",
        actor_id=user.id,
        actor_name=user.username,
        target_type="template_version",
        target_id=f"{template.name}@{version}",
        detail=notes,
    )
    return _version_out(row)


@router.post(
    "/{template_id}/versions/{version}/deprecate",
    response_model=TemplateVersionOut,
)
def deprecate_version(
    template_id: int,
    version: str,
    user: CanDeprecate,
    service: ServiceDep,
    notes: str = "",
) -> TemplateVersionOut:
    """Retire a version. Tasks that already pinned it keep their reference."""
    template = _require_version(service, template_id, version)
    row = _map_conflict(
        lambda: service.transition(template.name, version, TEMPLATE_DEPRECATED, notes=notes)
    )
    service.db.commit()
    write_audit(
        "template.deprecate",
        actor_id=user.id,
        actor_name=user.username,
        target_type="template_version",
        target_id=f"{template.name}@{version}",
        detail=notes,
    )
    return _version_out(row)


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------
@router.post(
    "/{template_id}/versions/{version}/apply",
    response_model=TaskOut,
    status_code=202,
)
def apply_version(
    template_id: int,
    version: str,
    payload: TemplateApplyIn,
    response: Response,
    user: CanApply,
    service: ServiceDep,
    tasks: TasksDep,
) -> TaskOut:
    """Queue a published template to run on each target server.

    ``202 Accepted`` because nothing has happened yet. Follow ``Location`` for
    status or watch ``GET /api/v1/tasks/{id}/events`` for live logs; one task
    carries one run per target, so a 20-server rollout is one task with twenty
    runs, each with its own steps and its own outcome.

    Unlike ``app.deploy`` there is no automatic undo: a template has no
    previous ``current`` to put back. A run that fails stops at the failing
    step and says which one.

    Applying a *middleware* or *database* template also needs
    ``middleware.install``. That permission exists to gate installing Redis,
    MySQL, Nginx or Supervisor, and it would be decorative if ``template.apply``
    were a way around it.
    """
    template = _require_version(service, template_id, version)
    if template.category in MIDDLEWARE_CATEGORIES and not AuthService(
        service.db
    ).has_permission(user, "middleware.install"):
        raise Forbidden(
            f"template {template.name!r} is in category {template.category!r}; "
            "applying it needs 'middleware.install' as well as 'template.apply'"
        )

    body = _map_conflict(
        lambda: service.build_apply_params(
            template.name,
            version,
            inputs=payload.inputs,
            config_ref=payload.config_ref,
        )
    )
    task, _created = tasks.create(
        kind="template.apply",
        title=f"apply {template.name} {version}",
        params=body,
        server_ids=[t.server_id for t in payload.targets],
        actor_id=user.id,
        actor_name=user.username,
    )
    write_audit(
        "template.apply",
        actor_id=user.id,
        actor_name=user.username,
        target_type="template_version",
        target_id=f"{template.name}@{version}",
        task_id=task.id,
        snapshot={
            "targets": sorted(t.server_id for t in payload.targets),
            "checksum": body["template"]["checksum"],
            "config_ref": payload.config_ref or "",
        },
    )
    response.headers["Location"] = f"/api/v1/tasks/{task.id}"
    return TaskOut(**tasks.to_out(task))
