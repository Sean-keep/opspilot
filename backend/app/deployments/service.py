"""Deployment records and the two ways to start one.

A deployment is created by assembling the §8.2 plan *here*, at submit time,
and storing the plan's inputs on the task. Two reasons:

* a plan that cannot be built — a mutable release alias, a missing checksum,
  an SSRF-shaped artifact URL — is refused before anything is queued, rather
  than surfacing as a failed run three steps in
* the worker rebuilds the *same* plan from the same inputs, so what was
  approved is what runs

Secrets are the one thing that does not round-trip. The task carries the
environment's ``config_ref``; the worker resolves it at execution time, exactly
as it resolves the SSH private key. The API never holds a plaintext config
value, so there is no window in which one could leak into a log.

Two invariants the database does not enforce and this module does:

**One live deployment per application environment.** §10 is explicit about the
mutex. A second submit while one is pending or running is refused with a
conflict, not queued behind it. The check is advisory under concurrency — two
requests that pass it at the same instant can both insert — so the worker's
per-environment behaviour is still the last line of defence.

**A rollback is a new deployment, never an edit.** It targets the release the
record says was live before, and it carries ``rollback_of`` so the undo is
separately auditable and separately cancellable.

Release aliases (``latest``, ``current``, ``shared``, ``releases``, ``ops``) are
refused everywhere by :func:`shared.actions.builtin.check_release_id`, not only
in production. §8.2's "生产禁用 latest" is therefore a subset of the rule the
code actually enforces.
"""

from __future__ import annotations

from typing import Any

from shared import deploy_plan as plan
from shared.deploy_plan import DeployInputs
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.applications.schemas import RESERVED_APP_NAMES
from backend.app.applications.service import ApplicationService
from backend.app.audit.service import RESULT_SUCCESS, write_audit
from backend.app.core.errors import Conflict, NotFound, ValidationError
from backend.app.deployments.models import (
    DEPLOY_SUCCEEDED,
    DEPLOYMENT_TERMINAL,
    KIND_DEPLOY,
    KIND_ROLLBACK,
    Deployment,
)
from backend.app.deployments.schemas import DeployCreate, RollbackCreate
from backend.app.orchestrator.service import TaskService

__all__ = ["DeploymentService"]

#: Fields of :class:`~shared.deploy_plan.DeployInputs` that are safe to put on
#: a task row. ``secrets`` is deliberately absent — see the module docstring.
_WIRE_FIELDS = (
    "app_root",
    "release_id",
    "kind",
    "release_version",
    "artifact_url",
    "artifact_checksum",
    "previous_release_id",
    "run_user",
    "service_name",
    "unit_path",
    "python_version",
    "requirements",
    "health_url",
    "health_timeout_s",
    "module_name",
    "app_port",
    "config",
)

#: A deployment record's kind is the row's vocabulary; the plan's is the
#: worker's. Mapping once here keeps the two from being mixed by accident.
_PLAN_KIND = {
    KIND_DEPLOY: plan.KIND_DEPLOY,
    KIND_ROLLBACK: plan.KIND_ROLLBACK,
}


class DeploymentService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.apps = ApplicationService(db)
        self.tasks = TaskService(db)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def list_for_application(
        self, application_id: int, *, limit: int = 50
    ) -> list[Deployment]:
        self.apps.require(application_id)
        stmt = (
            select(Deployment)
            .where(Deployment.application_id == application_id)
            .order_by(Deployment.id.desc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def list_all(self, *, limit: int = 50, offset: int = 0) -> list[Deployment]:
        stmt = (
            select(Deployment).order_by(Deployment.id.desc()).limit(limit).offset(offset)
        )
        return list(self.db.scalars(stmt))

    def require(self, deployment_id: int) -> Deployment:
        row = self.db.get(Deployment, deployment_id)
        if row is None:
            raise NotFound(f"no deployment with id {deployment_id}")
        return row

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------
    def create(
        self,
        application_id: int,
        payload: DeployCreate,
        *,
        actor_id: int | None,
        actor_name: str,
        idempotency_key: str = "",
    ) -> tuple[Deployment, bool]:
        app = self.apps.require(application_id)
        env = self.apps.require_environment(application_id, payload.environment)

        self._refuse_if_busy(env.id)

        inputs = self._inputs_for(
            app,
            env,
            release_version=payload.release_version,
            artifact_url=payload.artifact_url,
            artifact_checksum=payload.artifact_checksum,
            previous_release_id=self._current_release(env.id),
            module_name=payload.module_name,
            app_port=payload.app_port,
            python_version=payload.python_version,
            requirements=payload.requirements,
            config=payload.config,
            kind=KIND_DEPLOY,
        )
        # Build the plan once here: a submission the plan rejects is not a
        # submission we record as a deployment.
        self._validate_plan(inputs)

        return self._submit(
            app,
            env,
            inputs,
            targets=[t.server_id for t in payload.targets],
            actor_id=actor_id,
            actor_name=actor_name,
            idempotency_key=idempotency_key,
            kind=KIND_DEPLOY,
            rollback_of=None,
        )

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------
    def rollback(
        self,
        deployment_id: int,
        payload: RollbackCreate,
        *,
        actor_id: int | None,
        actor_name: str,
        idempotency_key: str = "",
    ) -> tuple[Deployment, bool]:
        if not payload.confirm:
            raise ValidationError(
                "a rollback restarts a live service; set confirm=true to proceed"
            )

        target = self.require(deployment_id)
        # Order matters: a deployment still running has not yet decided what it
        # did to the host, so "wait for it" is the honest answer — not "there is
        # nothing to restore", which is a statement about a state it has not
        # reached yet.
        if target.status not in DEPLOYMENT_TERMINAL:
            raise Conflict(
                f"deployment {target.id} is still {target.status}; "
                "wait for it to finish before rolling back"
            )
        if not target.previous_release_id:
            raise ValidationError(
                f"deployment {target.id} has no previous release to restore"
            )
        if target.previous_release_id == target.release_id:
            raise ValidationError(
                f"deployment {target.id} already points at {target.release_id}"
            )

        app = self.apps.require(target.application_id)
        env = self.apps.require_environment_by_id(target.environment_id)
        self._refuse_if_busy(env.id)

        # A rollback rewrites the env file too (APP_RELEASE moves), so it has to
        # carry the same non-secret config the deploy wrote. Inheriting it from
        # the target's own task is the only source that is guaranteed to be what
        # that deployment actually used.
        inputs = self._inputs_for(
            app,
            env,
            release_version=target.previous_release_id,
            artifact_url=target.artifact_url,
            artifact_checksum=target.artifact_checksum,
            previous_release_id=target.release_id,
            module_name=self._inherited_field(target, "module_name", ""),
            app_port=self._inherited_field(target, "app_port", 8000),
            python_version=self._inherited_field(target, "python_version", ""),
            requirements=self._inherited_field(target, "requirements", ""),
            config=self._inherited_config(target),
            kind=KIND_ROLLBACK,
        )
        self._validate_plan(inputs)

        return self._submit(
            app,
            env,
            inputs,
            targets=[t.server_id for t in payload.targets],
            actor_id=actor_id,
            actor_name=actor_name,
            idempotency_key=idempotency_key,
            kind=KIND_ROLLBACK,
            rollback_of=target.id,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _validate_plan(self, inputs: DeployInputs) -> None:
        try:
            plan.build_deploy_plan(inputs)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def _refuse_if_busy(self, environment_id: int) -> None:
        """§10: one deployment at a time per application environment."""
        stmt = select(Deployment).where(
            Deployment.environment_id == environment_id,
            Deployment.status.notin_(DEPLOYMENT_TERMINAL),
        )
        busy = list(self.db.scalars(stmt))
        if busy:
            raise Conflict(
                f"environment {environment_id} already has deployment "
                f"{busy[0].id} in state {busy[0].status}"
            )

    def _current_release(self, environment_id: int) -> str:
        """What the platform believes is live on this environment.

        The last *successful* deployment. Not the last attempt — an attempt that
        failed before the switch left the previous release in place, and one
        that failed after it was put back, so in both cases the record's own
        ``release_id`` is the right answer.
        """
        stmt = (
            select(Deployment)
            .where(
                Deployment.environment_id == environment_id,
                Deployment.status == DEPLOY_SUCCEEDED,
            )
            .order_by(Deployment.id.desc())
            .limit(1)
        )
        row = self.db.scalars(stmt).first()
        return row.release_id if row is not None else ""

    def _task_params(self, record: Deployment) -> dict[str, Any]:
        """The deploy inputs a prior deployment actually ran with."""
        if record.task_id is None:
            return {}
        task = self.tasks.get(record.task_id)
        deploy = (task.params_json or {}).get("deploy")
        return dict(deploy) if isinstance(deploy, dict) else {}

    def _inherited_config(self, record: Deployment) -> dict[str, str]:
        raw = self._task_params(record).get("config")
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items()}

    def _inherited_field(self, record: Deployment, name: str, default: Any) -> Any:
        value = self._task_params(record).get(name)
        return default if value is None else value

    def _inputs_for(
        self,
        app: Any,
        env: Any,
        *,
        release_version: str,
        artifact_url: str,
        artifact_checksum: str,
        previous_release_id: str,
        module_name: str,
        app_port: int,
        python_version: str,
        requirements: str,
        config: dict[str, str],
        kind: str,
    ) -> DeployInputs:
        if app.name in RESERVED_APP_NAMES:
            raise ValidationError(f"application name {app.name!r} is reserved")

        raw = {
            "app_root": f"/opt/opspilot/apps/{app.name}",
            "release_id": release_version,
            "kind": _PLAN_KIND[kind],
            "release_version": release_version,
            "artifact_url": artifact_url,
            "artifact_checksum": artifact_checksum,
            "previous_release_id": previous_release_id,
            "run_user": app.run_user,
            "service_name": env.service_name,
            "unit_path": env.unit_path,
            "python_version": python_version,
            "requirements": requirements,
            "health_url": env.health_url,
            "health_timeout_s": env.health_timeout_s,
            "module_name": module_name,
            "app_port": app_port,
            "config": dict(config),
        }
        try:
            return plan.inputs_from_mapping(raw, secrets={})
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def _submit(
        self,
        app: Any,
        env: Any,
        inputs: DeployInputs,
        *,
        targets: list[int],
        actor_id: int | None,
        actor_name: str,
        idempotency_key: str,
        kind: str,
        rollback_of: int | None,
    ) -> tuple[Deployment, bool]:
        if not targets:
            raise ValidationError("a deployment needs at least one target server")

        record = Deployment(
            application_id=app.id,
            environment_id=env.id,
            kind=kind,
            release_version=inputs.release_version,
            release_id=inputs.release_id,
            previous_release_id=inputs.previous_release_id,
            artifact_url=inputs.artifact_url,
            artifact_checksum=inputs.artifact_checksum,
            rollback_of=rollback_of,
            requested_by=actor_name or "",
        )
        self.db.add(record)
        self.db.flush()

        task_params: dict[str, Any] = {
            "deployment_id": record.id,
            "config_ref": env.config_ref or "",
            "deploy": {field: getattr(inputs, field) for field in _WIRE_FIELDS},
        }
        # Rebuild through the same loader the worker uses: if the wire shape is
        # wrong it is wrong here, not on the host.
        try:
            plan.inputs_from_mapping(task_params["deploy"], secrets={})
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        task, created = self.tasks.create(
            kind="app.rollback" if kind == KIND_ROLLBACK else "app.deploy",
            title=f"{kind.lower()} {app.name} {inputs.release_id} -> {env.name}",
            params=task_params,
            server_ids=list(targets),
            actor_id=actor_id,
            actor_name=actor_name,
            idempotency_key=idempotency_key,
        )
        if not created:
            # The key already landed. Discard the record we just staged and hand
            # back the one the original request produced.
            self.db.rollback()
            existing = self._find_by_task(task.id)
            if existing is None:
                raise Conflict(
                    "this Idempotency-Key belongs to a task that is not a deployment"
                )
            return existing, False

        record.task_id = task.id
        self.db.commit()

        write_audit(
            "deployment.rollback" if kind == KIND_ROLLBACK else "deployment.create",
            actor_id=actor_id,
            actor_name=actor_name,
            target_type="deployment",
            target_id=str(record.id),
            result=RESULT_SUCCESS,
            task_id=task.id,
            snapshot={
                "application": app.name,
                "environment": env.name,
                "release_id": inputs.release_id,
                "previous_release_id": inputs.previous_release_id,
                "targets": sorted(dict.fromkeys(targets)),
                "rollback_of": rollback_of,
            },
        )
        return record, True

    def _find_by_task(self, task_id: int) -> Deployment | None:
        stmt = select(Deployment).where(Deployment.task_id == task_id).limit(1)
        return self.db.scalars(stmt).first()
