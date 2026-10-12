"""Audit trail writes.

The requirement is that every privileged or dangerous operation is auditable
with actor, target, result, and time — and that audit rows can never leak a
secret even if a caller is careless with the snapshot.
"""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from shared.redaction import REDACTED
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.audit.models import RESULT_FAILURE, RESULT_SUCCESS, AuditLog
from backend.app.audit.service import AuditService


def test_successful_login_is_audited(
    client: TestClient, admin_headers: dict[str, str], db: Session
) -> None:
    rows = list(db.scalars(select(AuditLog).where(AuditLog.action == "auth.login")))
    assert rows, "a successful login must write an audit row"

    row = rows[-1]
    assert row.result == RESULT_SUCCESS
    assert row.actor_name == "admin"
    assert row.target_type == "user"
    assert row.request_id
    assert row.created_at is not None
    assert isinstance(row.created_at, dt.datetime)


def test_failed_login_is_audited(client: TestClient, db: Session) -> None:
    client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong-password-entirely"},
    )
    rows = list(db.scalars(select(AuditLog).where(AuditLog.action == "auth.login")))
    assert rows, "a failed login must write an audit row"
    row = rows[-1]
    assert row.result == RESULT_FAILURE
    assert row.detail == "invalid credentials"
    # The password itself must never appear in the audit row.
    assert "wrong-password-entirely" not in (row.detail or "")
    assert "wrong-password-entirely" not in str(row.snapshot or {})


def test_user_creation_is_audited(
    client: TestClient, admin_headers: dict[str, str], db: Session
) -> None:
    resp = client.post(
        "/api/v1/auth/users",
        headers=admin_headers,
        json={
            "username": "audited-user",
            "password": "audited-password-1234",
            "role_names": ["viewer"],
        },
    )
    assert resp.status_code == 201, resp.text

    rows = list(db.scalars(select(AuditLog).where(AuditLog.action == "auth.user.create")))
    assert rows
    row = rows[-1]
    assert row.result == RESULT_SUCCESS
    assert row.actor_name == "admin"
    assert row.target_type == "user"
    assert row.snapshot is not None
    assert row.snapshot.get("username") == "audited-user"
    assert "audited-password-1234" not in str(row.snapshot)


def test_audit_snapshot_is_redacted_on_the_way_in(db: Session) -> None:
    svc = AuditService(db)
    row = svc.record(
        "test.dangerous",
        actor_name="tester",
        target_type="server",
        target_id="7",
        snapshot={
            "host": "10.0.0.5",
            "password": "super-secret-value",
            "nested": {"api_token": "tok-123", "port": 22},
        },
    )
    db.commit()

    assert row.snapshot is not None
    assert row.snapshot["host"] == "10.0.0.5"
    assert row.snapshot["password"] == REDACTED
    assert row.snapshot["nested"]["api_token"] == REDACTED
    assert row.snapshot["nested"]["port"] == 22

    # And it is still redacted when read back out of the database.
    fetched = db.scalar(select(AuditLog).where(AuditLog.id == row.id))
    assert fetched is not None
    assert fetched.snapshot is not None
    assert "super-secret-value" not in str(fetched.snapshot)
    assert "tok-123" not in str(fetched.snapshot)


def test_audit_query_filters(db: Session) -> None:
    svc = AuditService(db)
    svc.record("a.one", actor_id=1, target_type="x")
    svc.record("a.two", actor_id=2, target_type="y")
    svc.record("a.one", actor_id=2, target_type="x")
    db.commit()

    assert len(svc.list_recent(action="a.one")) == 2
    assert len(svc.list_recent(actor_id=2)) == 2
    assert len(svc.list_recent(target_type="x")) == 2
    assert len(svc.list_recent(action="a.one", actor_id=2)) == 1
    # Newest first.
    assert svc.list_recent(limit=1)[0].action == "a.one"


def test_audit_endpoint_returns_rows(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    client.get("/api/v1/audit-logs", headers=admin_headers)  # ensure at least the login rows exist
    resp = client.get("/api/v1/audit-logs", headers=admin_headers, params={"limit": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    for row in body:
        assert set(row) >= {"id", "action", "result", "actor_name", "created_at", "request_id"}
        # Never expose a password hash through the audit API.
        assert "password_hash" not in row
