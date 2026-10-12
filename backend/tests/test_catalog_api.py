"""Template catalog HTTP surface.

Two things matter here beyond the happy path:

* a viewer can read templates but cannot publish one — the permission codes are
  separate on purpose;
* the body of a version never changes through this API, only its status.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from shared.actions.base import register_all
from shared.templates import LoadedTemplate, TemplateSpec, compute_checksum
from sqlalchemy.orm import Session

from backend.app.audit.models import AuditLog
from backend.app.catalog.service import CatalogService


@pytest.fixture(autouse=True)
def _registry() -> None:
    register_all()

EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": {}}


def _loaded(name: str = "demo", version: str = "1.0.0", **overrides: Any) -> LoadedTemplate:
    body: dict[str, Any] = {
        "api_version": "v1",
        "name": name,
        "version": version,
        "description": "catalog api fixture",
        "steps": [
            {"key": "check", "action": "system.check_os", "params": {"expect": ["ubuntu-24.04"]}}
        ],
    }
    body.update(overrides)
    spec = TemplateSpec.model_validate(body)
    files = {
        "template.yaml": spec.model_dump_json(),
        "schemas/inputs.schema.json": "{}",
    }
    return LoadedTemplate(
        spec=spec,
        root=Path("/nonexistent") / name,
        inputs_schema=dict(EMPTY_SCHEMA),
        checksum=compute_checksum(files),
        files=files,
    )


def _seed(db: Session, **overrides: Any) -> int:
    """Import one template and return its id."""
    service = CatalogService(db)
    row = service.upsert_loaded(_loaded(**overrides), category="runtime")
    db.commit()
    return row.id


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
def test_listing_templates_needs_template_read(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    listed = client.get("/api/v1/templates", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [t["id"] for t in body] == [template_id]
    assert body[0]["name"] == "demo"
    assert body[0]["latest_version"] == "1.0.0"
    assert [v["version"] for v in body[0]["versions"]] == ["1.0.0"]
    assert body[0]["versions"][0]["status"] == "DRAFT"


def test_an_anonymous_request_is_refused(client: TestClient, db: Session) -> None:
    _seed(db)
    resp = client.get("/api/v1/templates")
    assert resp.status_code == 401


def test_a_viewer_cannot_publish(
    client: TestClient, db: Session, viewer_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    resp = client.post(
        f"/api/v1/templates/{template_id}/versions/1.0.0/publish",
        headers=viewer_headers,
    )
    assert resp.status_code == 403
    # Reading is still fine.
    assert client.get("/api/v1/templates", headers=viewer_headers).status_code == 200


def test_version_detail_carries_the_body(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    resp = client.get(
        f"/api/v1/templates/{template_id}/versions/1.0.0", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == "1.0.0"
    assert body["spec"]["steps"][0]["action"] == "system.check_os"
    assert body["inputs_schema"]["additionalProperties"] is False
    assert len(body["checksum"]) == 64


def test_an_unknown_template_or_version_is_a_404(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    assert client.get("/api/v1/templates/9999", headers=admin_headers).status_code == 404
    missing = client.get(
        f"/api/v1/templates/{template_id}/versions/9.9.9", headers=admin_headers
    )
    assert missing.status_code == 404


# --------------------------------------------------------------------------
# Lifecycle over HTTP
# --------------------------------------------------------------------------
def test_validate_reports_ok_and_promotes_a_draft(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    resp = client.post(f"/api/v1/templates/{template_id}/validate", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["errors"] == []
    assert body["status"] == "VALIDATED"

    # The promotion is persisted, not just reported.
    listed = client.get("/api/v1/templates", headers=admin_headers).json()
    assert listed[0]["versions"][0]["status"] == "VALIDATED"


def test_validate_of_a_specific_version_works(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    resp = client.post(
        f"/api/v1/templates/{template_id}/versions/1.0.0/validate", headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_validate_of_a_missing_version_is_a_404(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    resp = client.post(
        f"/api/v1/templates/{template_id}/versions/0.0.1/validate", headers=admin_headers
    )
    assert resp.status_code == 404


def test_publishing_from_draft_is_a_conflict(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    resp = client.post(
        f"/api/v1/templates/{template_id}/versions/1.0.0/publish",
        headers=admin_headers,
        params={"notes": "go live"},
    )
    assert resp.status_code == 409, resp.text
    assert "DRAFT" in resp.json()["message"]


def test_the_full_lifecycle_reaches_published_then_deprecated(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)

    assert (
        client.post(f"/api/v1/templates/{template_id}/validate", headers=admin_headers).json()[
            "status"
        ]
        == "VALIDATED"
    )

    # VALIDATED -> TESTED is the operator's "I ran the template tests" click.
    # It is a direct service move; the HTTP surface only exposes publish and
    # deprecate because those are the two that change what a task may execute.
    service = CatalogService(db)
    service.transition("demo", "1.0.0", "TESTED")
    db.commit()

    published = client.post(
        f"/api/v1/templates/{template_id}/versions/1.0.0/publish",
        headers=admin_headers,
        params={"notes": "ship it"},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "PUBLISHED"
    assert published.json()["published_at"] is not None

    # Republishing is refused.
    assert (
        client.post(
            f"/api/v1/templates/{template_id}/versions/1.0.0/publish", headers=admin_headers
        ).status_code
        == 409
    )

    deprecated = client.post(
        f"/api/v1/templates/{template_id}/versions/1.0.0/deprecate",
        headers=admin_headers,
    )
    assert deprecated.status_code == 200
    assert deprecated.json()["status"] == "DEPRECATED"
    assert deprecated.json()["deprecated_at"] is not None

    # Deprecation is terminal.
    assert (
        client.post(
            f"/api/v1/templates/{template_id}/versions/1.0.0/publish", headers=admin_headers
        ).status_code
        == 409
    )


def test_publish_is_audited(
    client: TestClient, db: Session, admin_headers: dict[str, str]
) -> None:
    template_id = _seed(db)
    service = CatalogService(db)
    service.validate_version("demo", "1.0.0")
    service.transition("demo", "1.0.0", "TESTED")
    db.commit()

    client.post(
        f"/api/v1/templates/{template_id}/versions/1.0.0/publish",
        headers=admin_headers,
        params={"notes": "go live"},
    )

    rows = db.query(AuditLog).filter(AuditLog.action == "template.publish").all()
    assert len(rows) == 1
    assert rows[0].target_id == "demo@1.0.0"
    assert rows[0].actor_id is not None
