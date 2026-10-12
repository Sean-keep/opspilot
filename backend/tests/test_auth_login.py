"""Login and token behaviour.

The security-relevant assertions here are:

* a successful response never contains a password hash
* "no such user" and "wrong password" are indistinguishable
* a failed login is rate limited after enough attempts
* an invalid or expired token is rejected
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from backend.app.auth.router import reset_rate_limiter
from backend.tests.conftest import BOOTSTRAP_ADMIN, BOOTSTRAP_PASSWORD


@pytest.fixture(autouse=True)
def _clean_rate_limiter() -> Iterator[None]:
    reset_rate_limiter()
    yield
    reset_rate_limiter()


def test_login_success_returns_token_and_user_without_hash(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": BOOTSTRAP_ADMIN, "password": BOOTSTRAP_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] > 0

    user = body["user"]
    assert user["username"] == BOOTSTRAP_ADMIN
    assert "password_hash" not in user
    assert "password" not in user
    # Admin must actually hold the admin role and its permissions.
    assert any(r["name"] == "admin" for r in user["roles"])
    assert "user.create" in user["permissions"]


def test_wrong_password_is_rejected_with_unified_error_shape(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": BOOTSTRAP_ADMIN, "password": "wrong-password-entirely"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert set(body) >= {"code", "message", "request_id"}
    assert body["code"] == "unauthenticated"
    assert body["request_id"]
    # No stack trace, no hash, no hint about which half was wrong.
    assert "hash" not in resp.text.lower()
    assert "Traceback" not in resp.text


def test_unknown_user_and_wrong_password_are_indistinguishable(client: TestClient) -> None:
    missing = client.post(
        "/api/v1/auth/login",
        json={"username": "definitely-not-a-user", "password": "wrong-password-entirely"},
    )
    wrong_pw = client.post(
        "/api/v1/auth/login",
        json={"username": BOOTSTRAP_ADMIN, "password": "wrong-password-entirely"},
    )
    assert missing.status_code == wrong_pw.status_code == 401
    assert missing.json()["message"] == wrong_pw.json()["message"]


def test_login_is_rate_limited(client: TestClient) -> None:
    from backend.app.core.settings import get_settings

    cfg = get_settings()
    for _ in range(cfg.login_max_failures):
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": BOOTSTRAP_ADMIN, "password": "wrong-password-entirely"},
        )
        assert resp.status_code == 401

    resp = client.post(
        "/api/v1/auth/login",
        json={"username": BOOTSTRAP_ADMIN, "password": "wrong-password-entirely"},
    )
    assert resp.status_code == 429
    assert resp.json()["code"] == "rate_limited"


def test_me_returns_effective_permissions(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    resp = client.get("/api/v1/auth/me", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["username"] == BOOTSTRAP_ADMIN
    assert isinstance(body["permissions"], list)
    assert "audit.read" in body["permissions"]


def test_me_without_token_is_unauthenticated(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthenticated"


def test_me_with_garbage_token_is_unauthenticated(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthenticated"


def test_expired_token_is_unauthenticated(client: TestClient) -> None:
    from backend.app.core.security import create_access_token
    from backend.app.core.settings import get_settings

    cfg = get_settings()
    past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    token = create_access_token(1, BOOTSTRAP_ADMIN, settings=cfg, now=past)
    # Bump the TTL so the signed expiry is genuinely in the past relative to now.
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthenticated"


def test_token_signed_with_wrong_key_is_rejected(client: TestClient) -> None:
    from backend.app.core.security import create_access_token
    from backend.app.core.settings import Settings

    # Build settings with a different signing key than the app is using.
    rogue = Settings(secret_key="a-completely-different-signing-key")
    token = create_access_token(1, BOOTSTRAP_ADMIN, settings=rogue)
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
