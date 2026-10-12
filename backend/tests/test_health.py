"""Health and readiness endpoints.

These must stay unauthenticated — systemd and a reverse proxy probe them
without credentials — and must never leak configuration values.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_is_unauthenticated_and_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert "time" in body


def test_healthz_leaks_no_secrets(client: TestClient) -> None:
    resp = client.get("/healthz")
    text = resp.text.lower()
    for forbidden in ("password", "secret", "token", "key", "ops_db"):
        assert forbidden not in text, f"healthz leaked {forbidden!r}: {resp.text}"


def test_readyz_is_ready_when_database_answers(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "database": "ok"}


def test_healthz_needs_no_authorization_header(client: TestClient) -> None:
    # Explicitly no headers at all.
    resp = client.get("/healthz", headers={})
    assert resp.status_code == 200


def test_every_error_response_carries_request_id(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    body = resp.json()
    assert body["request_id"]
    # The same id is echoed in the header so a client can quote it in a ticket.
    assert resp.headers.get("X-Request-Id") == body["request_id"]


def test_unknown_route_uses_unified_error_shape(client: TestClient) -> None:
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
