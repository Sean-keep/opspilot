"""RBAC enforcement.

The requirement is explicit: hiding a button in the frontend is not a security
control. These tests prove the backend refuses the operation itself.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.auth.schemas import UserCreate
from backend.app.auth.service import AuthService
from backend.app.core.errors import Forbidden


def test_viewer_cannot_create_user(
    client: TestClient, viewer_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/api/v1/auth/users",
        headers=viewer_headers,
        json={
            "username": "intruder",
            "password": "intruder-password-1234",
            "role_names": ["admin"],
        },
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "forbidden"
    assert "user.create" in body["message"]
    assert body["request_id"]


def test_admin_can_create_user(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/auth/users",
        headers=admin_headers,
        json={
            "username": "second-admin",
            "password": "second-admin-password-1234",
            "role_names": ["operator"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["username"] == "second-admin"
    assert "password_hash" not in body
    assert [r["name"] for r in body["roles"]] == ["operator"]


def test_viewer_cannot_list_users(client: TestClient, viewer_headers: dict[str, str]) -> None:
    """Listing accounts is privileged — a viewer is read-only on inventory only."""
    resp = client.get("/api/v1/auth/users", headers=viewer_headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"


def test_viewer_cannot_create_user_and_can_still_login(
    client: TestClient, viewer_headers: dict[str, str]
) -> None:
    denied = client.post(
        "/api/v1/auth/users",
        headers=viewer_headers,
        json={"username": "nope", "password": "nope-password-12345"},
    )
    assert denied.status_code == 403

    # Their own identity is still reachable — that endpoint only needs auth.
    me = client.get("/api/v1/auth/me", headers=viewer_headers)
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "viewer"


def test_unauthenticated_request_is_401_not_403(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/users")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthenticated"


def test_role_permission_union_is_effective(db: Session, seeded: Session) -> None:
    auth = AuthService(db)
    admin = auth.get_user_by_username("admin")
    assert admin is not None
    assert auth.has_permission(admin, "user.create")
    assert auth.has_permission(admin, "audit.read")
    assert auth.has_permission(admin, "deployment.create")


def test_require_permission_raises_forbidden(db: Session, seeded: Session) -> None:
    auth = AuthService(db)
    admin = auth.get_user_by_username("admin")
    assert admin is not None
    auth.require_permission(admin, "user.create")  # must not raise

    plain = auth.create_user(
        UserCreate(username="plain", password="plain-password-1234")
    )
    with pytest.raises(Forbidden) as excinfo:
        auth.require_permission(plain, "user.create")
    assert "user.create" in str(excinfo.value)


def test_viewer_cannot_query_audit(client: TestClient, viewer_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/audit-logs", headers=viewer_headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"


def test_admin_can_query_audit(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/audit-logs", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_permission_codes_are_stable(db: Session, seeded: Session) -> None:
    """Permission codes are a stored vocabulary — do not let one drift."""
    from sqlalchemy import select

    from backend.app.auth.models import Permission
    from backend.app.auth.permissions import PERMISSION_CATALOG

    rows = {p.code for p in db.scalars(select(Permission))}
    assert set(PERMISSION_CATALOG) <= rows
    # The codes the routes actually check must exist.
    for required in ("user.create", "user.read", "audit.read"):
        assert required in rows
