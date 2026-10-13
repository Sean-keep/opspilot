"""``POST /templates/{id}/versions/{version}/apply``.

Three things matter here beyond returning a task id:

* only a **PUBLISHED** body is runnable — a DRAFT is still being edited in
  spirit even when frozen in fact;
* a *middleware* or *database* template needs ``middleware.install`` on top of
  ``template.apply``. That permission exists to gate installing Redis, MySQL,
  Nginx or Supervisor, and ``template.apply`` must not be a way around it;
* the task body carries the approved spec and the inputs, and **no secret
  material**. Anything the template fills from a bundle is named only by
  ``config_ref`` and resolved on the worker.

Nothing here opens a socket: the endpoint only queues work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from shared.actions.base import register_all
from shared.templates import LoadedTemplate, TemplateSpec, compute_checksum
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.audit.models import AuditLog
from backend.app.catalog.service import CatalogService


@pytest.fixture(autouse=True)
def _registry() -> None:
    register_all()


EMPTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}

#: Declares one plain input and one ``x-secret`` input, so the tests can show
#: the split: the plain one belongs in the body, the secret one must not.
SECRET_AWARE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["app_user"],
    "properties": {
        "app_user": {"type": "string", "pattern": "^[a-z_][a-z0-9_-]{0,31}$"},
        "db_password": {"type": "string", "x-secret": True},
    },
}


def _loaded(
    name: str = "demo",
    version: str = "1.0.0",
    *,
    inputs_schema: dict[str, Any] | None = None,
    **overrides: Any,
) -> LoadedTemplate:
    body: dict[str, Any] = {
        "api_version": "v1",
        "name": name,
        "version": version,
        "description": "template.apply api fixture",
        "steps": [
            {"key": "check", "action": "system.check_os", "params": {"expect": ["ubuntu-24.04"]}}
        ],
    }
    body.update(overrides)
    spec = TemplateSpec.model_validate(body)
    schema = dict(EMPTY_SCHEMA if inputs_schema is None else inputs_schema)
    files = {
        "template.yaml": spec.model_dump_json(),
        "schemas/inputs.schema.json": "{}",
    }
    return LoadedTemplate(
        spec=spec,
        root=Path("/nonexistent") / name,
        inputs_schema=schema,
        checksum=compute_checksum(files),
        files=files,
    )


def _seed(
    db: Session,
    *,
    category: str = "runtime",
    inputs_schema: dict[str, Any] | None = None,
    **overrides: Any,
) -> int:
    service = CatalogService(db)
    row = service.upsert_loaded(
        _loaded(inputs_schema=inputs_schema, **overrides), category=category
    )
    db.commit()
    return row.id


def _publish(db: Session, name: str, version: str) -> None:
    """Walk a version up to ``PUBLISHED`` — the only state apply accepts.

    DRAFT → VALIDATED → TESTED → PUBLISHED, the same edges the HTTP surface
    offers. Going straight to ``publish`` from DRAFT is refused by the
    transition table, which is the point: a body that never passed a check is
    not a body to run on a host.
    """
    service = CatalogService(db)
    service.validate_version(name, version)
    service.transition(name, version, "TESTED")
    service.publish(name, version)
    db.commit()


def _apply(
    client: TestClient,
    template_id: int,
    headers: dict[str, str],
    *,
    version: str = "1.0.0",
    inputs: dict[str, Any] | None = None,
    targets: list[dict[str, int]] | None = None,
    config_ref: str = "",
) -> Any:
    return client.post(
        f"/api/v1/templates/{template_id}/versions/{version}/apply",
        headers=headers,
        json={
            "inputs": inputs if inputs is not None else {},
            "targets": targets if targets is not None else [{"server_id": 1}],
            "config_ref": config_ref,
        },
    )


def _task_params(db: Session, task_id: int) -> dict[str, Any]:
    """The task body as the worker will see it.

    Read from the table rather than echoed by the API on purpose: the point of
    ``x-secret`` is what *lands*, and an endpoint that reflected its own input
    would pass a test written against the response.
    """
    row = db.execute(
        text("SELECT params_json FROM ops_tasks WHERE id = :id"), {"id": task_id}
    ).one()
    raw = row[0]
    return json.loads(raw) if isinstance(raw, str) else dict(raw or {})


# --------------------------------------------------------------------------
# Only a published body is runnable
# --------------------------------------------------------------------------
def test_a_published_version_queues_a_task(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    _publish(db, "demo", "1.0.0")

    resp = _apply(client, template_id, admin_headers)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["kind"] == "template.apply"
    assert body["title"] == "apply demo 1.0.0"
    # QUEUED, not PENDING: create() writes the outbox row and promotes the
    # task in the same transaction, so a caller never sees a task that is
    # waiting for a dispatcher that may already have run.
    assert body["status"] == "QUEUED"
    assert resp.headers["Location"] == f"/api/v1/tasks/{body['id']}"


def test_a_draft_version_is_refused(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    resp = _apply(client, template_id, admin_headers)
    assert resp.status_code == 409, resp.text
    assert "DRAFT" in resp.json()["message"]


def test_a_validated_or_tested_version_is_still_refused(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    """Half-tested is not tested. Only the publish click makes a body runnable."""
    template_id = _seed(db)
    service = CatalogService(db)
    service.validate_version("demo", "1.0.0")
    db.commit()
    assert _apply(client, template_id, admin_headers).status_code == 409

    service.transition("demo", "1.0.0", "TESTED")
    db.commit()
    resp = _apply(client, template_id, admin_headers)
    assert resp.status_code == 409, resp.text
    assert "TESTED" in resp.json()["message"]


def test_a_deprecated_version_is_refused(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    _publish(db, "demo", "1.0.0")
    CatalogService(db).transition("demo", "1.0.0", "DEPRECATED")
    db.commit()

    resp = _apply(client, template_id, admin_headers)
    assert resp.status_code == 409, resp.text
    assert "DEPRECATED" in resp.json()["message"]


def test_an_unknown_template_or_version_is_a_404(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    _publish(db, "demo", "1.0.0")
    assert _apply(client, 9999, admin_headers).status_code == 404
    assert _apply(client, template_id, admin_headers, version="9.9.9").status_code == 404


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------
def test_a_viewer_cannot_apply(
    client: TestClient, db: Session, viewer_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    _publish(db, "demo", "1.0.0")
    resp = _apply(client, template_id, viewer_headers)
    assert resp.status_code == 403
    # Reading is still fine.
    assert client.get("/api/v1/templates", headers=viewer_headers).status_code == 200


def test_an_anonymous_request_is_refused(
    client: TestClient, db: Session
) -> None:
    template_id = _seed(db)
    _publish(db, "demo", "1.0.0")
    assert _apply(client, template_id, {}).status_code == 401


def test_an_operator_can_apply_a_runtime_template(
    client: TestClient, db: Session, operator_headers: dict[str, str]
) -> None:
    """``template.apply`` alone is enough for a template that touches only one
    application's own files."""
    template_id = _seed(db, category="runtime")
    _publish(db, "demo", "1.0.0")
    resp = _apply(client, template_id, operator_headers)
    assert resp.status_code == 202, resp.text


@pytest.mark.parametrize("category", ["middleware", "database"])
def test_applying_a_shared_service_template_needs_middleware_install(
    client: TestClient,
    db: Session,
    operator_headers: dict[str, str],
    category: str,
) -> None:
    """The extra gate.

    ``operator`` holds ``template.apply`` but not ``middleware.install``, and
    a middleware/database template is an install in disguise. If this returned
    202 the middleware permission would be decorative.
    """
    template_id = _seed(db, name=f"svc-{category}", category=category)
    _publish(db, f"svc-{category}", "1.0.0")

    resp = _apply(client, template_id, operator_headers)
    assert resp.status_code == 403, resp.text
    assert "middleware.install" in resp.json()["message"]


def test_an_admin_can_apply_a_middleware_template(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db, name="redis-like", category="middleware")
    _publish(db, "redis-like", "1.0.0")
    resp = _apply(client, template_id, admin_headers)
    assert resp.status_code == 202, resp.text


def test_the_gate_is_about_the_category_not_the_name(
    client: TestClient, db: Session, operator_headers: dict[str, str]
) -> None:
    """A template called ``redis`` filed under ``runtime`` is not an install.

    The check reads ``category`` because that is what the import decided, and
    renaming a template must not silently promote or demote it.
    """
    template_id = _seed(db, name="redis", category="runtime")
    _publish(db, "redis", "1.0.0")
    assert _apply(client, template_id, operator_headers).status_code == 202


# --------------------------------------------------------------------------
# The task body
# --------------------------------------------------------------------------
def test_the_body_carries_the_approved_spec_and_inputs(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db, inputs_schema=SECRET_AWARE_SCHEMA)
    _publish(db, "demo", "1.0.0")

    resp = _apply(
        client,
        template_id,
        admin_headers,
        inputs={"app_user": "billing"},
        config_ref="cred:cfg-1",
    )
    assert resp.status_code == 202, resp.text

    task_id = resp.json()["id"]
    body = _task_params(db, task_id)
    assert body["template"]["name"] == "demo"
    assert body["template"]["version"] == "1.0.0"
    assert len(body["template"]["checksum"]) == 64
    assert body["spec"]["steps"][0]["action"] == "system.check_os"
    assert body["inputs"] == {"app_user": "billing"}
    assert body["config_ref"] == "cred:cfg-1"
    assert body["inputs_schema"]["properties"]["db_password"]["x-secret"] is True


def test_no_secret_material_reaches_the_task_body(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    """The whole point of ``x-secret``.

    A secret may not be sent as an input, and even if a caller tried, the body
    that lands in ``ops_tasks`` must not contain the value.
    """
    template_id = _seed(db, inputs_schema=SECRET_AWARE_SCHEMA)
    _publish(db, "demo", "1.0.0")

    resp = _apply(
        client,
        template_id,
        admin_headers,
        inputs={"app_user": "billing", "db_password": "hunter2"},
    )
    assert resp.status_code == 422, resp.text
    assert "x-secret" in resp.text

    rows = db.execute(text("SELECT params_json FROM ops_tasks")).fetchall()
    assert rows == [], "a refused submit must not leave a task behind"


def test_an_undeclared_input_is_refused_at_submit(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    """A typo in an input name is a 422 here, not an unresolved placeholder on
    a live host three steps into an install."""
    template_id = _seed(db, inputs_schema=SECRET_AWARE_SCHEMA)
    _publish(db, "demo", "1.0.0")

    resp = _apply(client, template_id, admin_headers, inputs={"app_usr": "billing"})
    assert resp.status_code == 422, resp.text
    assert "app_usr" in resp.text
    assert "not declared" in resp.text


def test_a_missing_required_input_is_refused_at_submit(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db, inputs_schema=SECRET_AWARE_SCHEMA)
    _publish(db, "demo", "1.0.0")

    resp = _apply(client, template_id, admin_headers, inputs={})
    assert resp.status_code == 422, resp.text
    assert "app_user" in resp.text


def test_an_empty_target_list_is_refused(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    _publish(db, "demo", "1.0.0")
    resp = _apply(client, template_id, admin_headers, targets=[])
    assert resp.status_code == 422, resp.text


def test_one_task_per_server_of_many(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    """A 3-server rollout is one task with three runs, not three tasks."""
    template_id = _seed(db)
    _publish(db, "demo", "1.0.0")

    resp = _apply(
        client,
        template_id,
        admin_headers,
        targets=[{"server_id": 1}, {"server_id": 2}, {"server_id": 3}],
    )
    assert resp.status_code == 202, resp.text

    task_id = resp.json()["id"]
    run_rows = db.execute(
        text("SELECT server_id FROM ops_task_runs WHERE task_id = :id ORDER BY server_id"),
        {"id": task_id},
    ).fetchall()
    assert [r[0] for r in run_rows] == [1, 2, 3]


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
def test_the_apply_is_audited(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    _publish(db, "demo", "1.0.0")

    resp = _apply(client, template_id, admin_headers, config_ref="cred:cfg-1")
    assert resp.status_code == 202, resp.text

    rows = db.query(AuditLog).filter(AuditLog.action == "template.apply").all()
    assert len(rows) == 1
    assert rows[0].target_id == "demo@1.0.0"
    assert rows[0].task_id == resp.json()["id"]
    snapshot = rows[0].snapshot or {}
    assert snapshot.get("config_ref") == "cred:cfg-1"
    assert snapshot.get("targets") == [1]
    assert len(snapshot.get("checksum", "")) == 64


def test_a_refused_apply_is_not_audited_as_a_success(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    """No audit row for work that never queued — otherwise the trail says an
    install happened when nothing did."""
    template_id = _seed(db)  # left in DRAFT
    assert _apply(client, template_id, admin_headers).status_code == 409
    rows = db.query(AuditLog).filter(AuditLog.action == "template.apply").all()
    assert rows == []
