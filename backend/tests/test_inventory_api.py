"""Inventory API: CRUD, RBAC, host-key trust, and the check preconditions.

The live SSH transport is exercised separately (``test_ssh_live.py``) and the
end-to-end check path is ``test_check_via_worker.py``. Here the tests assert
what the *platform* does: refuse untrusted hosts, never echo secrets, and
enforce permissions on the backend.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.inventory.models import (
    HOSTKEY_PENDING_CONFIRM,
    HOSTKEY_UNVERIFIED,
    HOSTKEY_VERIFIED,
    SERVER_DELETED,
    Server,
)

FAKE_FINGERPRINT = "SHA256:" + "B" * 43
FAKE_ALGORITHM = "ssh-ed25519"


@pytest.fixture()
def credential_id(client: TestClient, admin_headers: dict[str, str]) -> int:
    resp = client.post(
        "/api/v1/credentials",
        headers=admin_headers,
        json={
            "name": "test-key",
            "kind": "ssh_key",
            "secret": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END",
        },
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


def _create_server(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str = "web-1",
    hostname: str = "10.0.0.11",
    credential_id: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "hostname": hostname,
        "port": 22,
        "ssh_user": "opstest",
    }
    if credential_id is not None:
        payload["credential_id"] = credential_id
    payload.update(extra)
    resp = client.post("/api/v1/servers", headers=headers, json=payload)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _patch_trust_flow(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the real first-contact handshake with a canned fingerprint."""
    seen: dict[str, Any] = {}

    def fake_discover(host: str, port: int, *, connect_timeout: int = 10) -> dict[str, str]:
        seen["host"] = host
        seen["port"] = port
        return {"algorithm": FAKE_ALGORITHM, "fingerprint": FAKE_FINGERPRINT}

    monkeypatch.setattr(
        "backend.app.inventory.service.discover_host_key", fake_discover
    )
    return seen


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------
def test_create_and_list_server(
    client: TestClient, admin_headers: dict[str, str], credential_id: int
) -> None:
    body = _create_server(client, admin_headers, credential_id=credential_id)
    assert body["name"] == "web-1"
    assert body["hostname"] == "10.0.0.11"
    assert body["status"] == "active"
    assert body["host_key_state"] == HOSTKEY_UNVERIFIED
    assert body["host_key_fingerprint"] == ""
    assert body["latest_facts"] is None
    # Nothing credential-shaped in the response.
    assert "payload" not in body
    assert "secret" not in body

    listed = client.get("/api/v1/servers", headers=admin_headers)
    assert listed.status_code == 200
    assert [s["name"] for s in listed.json()] == ["web-1"]


def test_duplicate_server_name_is_rejected(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    _create_server(client, admin_headers)
    resp = client.post(
        "/api/v1/servers",
        headers=admin_headers,
        json={"name": "web-1", "hostname": "10.0.0.12"},
    )
    assert resp.status_code == 422
    assert "already taken" in resp.json()["message"]


def test_soft_delete_keeps_the_row(
    client: TestClient, admin_headers: dict[str, str], db: Session
) -> None:
    body = _create_server(client, admin_headers)
    deleted = client.delete(f"/api/v1/servers/{body['id']}", headers=admin_headers)
    assert deleted.status_code == 200
    assert deleted.json()["status"] == SERVER_DELETED

    gone = client.get(f"/api/v1/servers/{body['id']}", headers=admin_headers)
    assert gone.status_code == 404

    row = db.get(Server, int(body["id"]))
    assert row is not None, "soft delete must not remove the audit-visible row"
    assert row.status == SERVER_DELETED


# --------------------------------------------------------------------------
# Hostname / user validation — the injection surface
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hostname",
    [
        "10.0.0.1; rm -rf /",
        "10.0.0.1 && curl evil",
        "$(id)",
        "`id`",
        "10.0.0.1|tee /etc/passwd",
        "10.0.0.1 > /etc/shadow",
        "10.0.0.1\nrm -rf /",
        "../../../etc/passwd",
        "10.0.0.1/../../etc",
        "-oProxyCommand=evil",
        "host name",
    ],
)
def test_hostname_rejects_shell_metacharacters_and_traversal(
    client: TestClient, admin_headers: dict[str, str], hostname: str
) -> None:
    resp = client.post(
        "/api/v1/servers",
        headers=admin_headers,
        json={"name": "evil", "hostname": hostname},
    )
    assert resp.status_code == 422, f"{hostname!r} was accepted"
    assert resp.json()["code"] == "validation_error"


@pytest.mark.parametrize("user", ["root; id", "a b", "op$(id)", "op/stest", "op\x00st"])
def test_ssh_user_rejects_shell_metacharacters(
    client: TestClient, admin_headers: dict[str, str], user: str
) -> None:
    resp = client.post(
        "/api/v1/servers",
        headers=admin_headers,
        json={"name": "evil", "hostname": "10.0.0.11", "ssh_user": user},
    )
    assert resp.status_code == 422, f"{user!r} was accepted"


def test_hostname_and_user_are_stripped_not_rewritten(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    body = _create_server(
        client,
        admin_headers,
        name="padded",
        hostname="  10.0.0.20  ",
        ssh_user="  opstest  ",
    )
    assert body["hostname"] == "10.0.0.20"
    assert body["ssh_user"] == "opstest"


# --------------------------------------------------------------------------
# Host key trust
# --------------------------------------------------------------------------
def test_check_refuses_when_host_key_never_discovered(
    client: TestClient,
    admin_headers: dict[str, str],
    credential_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_trust_flow(monkeypatch)
    body = _create_server(client, admin_headers, credential_id=credential_id)
    resp = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)

    assert resp.status_code == 409
    assert resp.json()["code"] == "host_key_not_verified"
    # Refused before any work was queued — an untrusted host gets no task.
    tasks = client.get("/api/v1/tasks", headers=admin_headers)
    assert tasks.status_code == 200
    assert [t for t in tasks.json() if t["kind"] == "server.check"] == []


def test_discover_then_accept_pins_the_fingerprint(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_trust_flow(monkeypatch)
    body = _create_server(client, admin_headers)

    discovered = client.post(
        f"/api/v1/servers/{body['id']}/discover-hostkey", headers=admin_headers
    )
    assert discovered.status_code == 200, discovered.text
    assert discovered.json() == {
        "algorithm": FAKE_ALGORITHM,
        "fingerprint": FAKE_FINGERPRINT,
        "state": HOSTKEY_PENDING_CONFIRM,
    }
    assert seen == {"host": "10.0.0.11", "port": 22}

    accepted = client.post(
        f"/api/v1/servers/{body['id']}/accept-hostkey",
        headers=admin_headers,
        json={"fingerprint": FAKE_FINGERPRINT},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["host_key_state"] == HOSTKEY_VERIFIED
    assert accepted.json()["host_key_fingerprint"] == FAKE_FINGERPRINT
    assert accepted.json()["host_key_accepted_at"] is not None


def test_accept_rejects_a_different_fingerprint(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_trust_flow(monkeypatch)
    body = _create_server(client, admin_headers)
    client.post(f"/api/v1/servers/{body['id']}/discover-hostkey", headers=admin_headers)

    wrong = client.post(
        f"/api/v1/servers/{body['id']}/accept-hostkey",
        headers=admin_headers,
        json={"fingerprint": "SHA256:" + "Z" * 43},
    )
    assert wrong.status_code == 409
    assert wrong.json()["code"] == "host_key_mismatch"

    listed = client.get(f"/api/v1/servers/{body['id']}", headers=admin_headers)
    assert listed.json()["host_key_state"] != HOSTKEY_VERIFIED


def test_accept_without_discover_is_a_conflict(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    body = _create_server(client, admin_headers)
    resp = client.post(
        f"/api/v1/servers/{body['id']}/accept-hostkey",
        headers=admin_headers,
        json={"fingerprint": FAKE_FINGERPRINT},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "host_key_not_discovered"


def test_changing_hostname_drops_the_pin(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_trust_flow(monkeypatch)
    body = _create_server(client, admin_headers)
    client.post(f"/api/v1/servers/{body['id']}/discover-hostkey", headers=admin_headers)
    client.post(
        f"/api/v1/servers/{body['id']}/accept-hostkey",
        headers=admin_headers,
        json={"fingerprint": FAKE_FINGERPRINT},
    )

    moved = client.patch(
        f"/api/v1/servers/{body['id']}",
        headers=admin_headers,
        json={"hostname": "10.0.0.99"},
    )
    assert moved.status_code == 200
    assert moved.json()["host_key_state"] == HOSTKEY_UNVERIFIED
    assert moved.json()["host_key_fingerprint"] == ""


# --------------------------------------------------------------------------
# Check — the API enqueues; the Worker runs (the S1 -> S2 seam)
#
# The end-to-end path (facts land, failure is a result, no secret anywhere)
# lives in ``test_check_via_worker.py``. What is asserted here is that the API
# side of the seam does its part and nothing more.
# --------------------------------------------------------------------------
def _trust(
    client: TestClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
) -> None:
    _patch_trust_flow(monkeypatch)
    client.post(f"/api/v1/servers/{body['id']}/discover-hostkey", headers=headers)
    client.post(
        f"/api/v1/servers/{body['id']}/accept-hostkey",
        headers=headers,
        json={"fingerprint": FAKE_FINGERPRINT},
    )


def test_check_enqueues_a_task_instead_of_running_it(
    client: TestClient,
    admin_headers: dict[str, str],
    credential_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``202 Accepted``: the check has been queued, not performed."""
    body = _create_server(client, admin_headers, credential_id=credential_id)
    _trust(client, admin_headers, monkeypatch, body)

    resp = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)
    assert resp.status_code == 202, resp.text
    task = resp.json()
    assert task["kind"] == "server.check"
    assert task["status"] == "QUEUED"
    assert resp.headers["location"] == f"/api/v1/tasks/{task['id']}"

    # Nothing has run yet: no facts, and the server still reads as unchecked.
    listed = client.get(f"/api/v1/servers/{body['id']}", headers=admin_headers)
    assert listed.json()["last_check_status"] == "unknown"
    assert listed.json()["latest_facts"] is None

    runs = client.get(f"/api/v1/tasks/{task['id']}/runs", headers=admin_headers)
    assert runs.status_code == 200, runs.text
    assert len(runs.json()) == 1
    assert runs.json()[0]["status"] == "WAITING"
    assert runs.json()[0]["server_id"] == body["id"]


def test_check_refuses_when_credential_missing(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _create_server(client, admin_headers)  # no credential
    _trust(client, admin_headers, monkeypatch, body)

    resp = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)
    assert resp.status_code == 422
    assert "credential" in resp.json()["message"]

    # A refused check must not leave work behind for the Worker to trip over.
    tasks = client.get("/api/v1/tasks", headers=admin_headers)
    assert [t for t in tasks.json() if t["kind"] == "server.check"] == []


def test_check_never_leaks_the_private_key(
    client: TestClient,
    admin_headers: dict[str, str],
    credential_id: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_marker = "SUPER-SECRET-KEY-MATERIAL-DO-NOT-LOG"
    # Swap the credential for one whose payload we can grep for.
    resp = client.post(
        "/api/v1/credentials",
        headers=admin_headers,
        json={"name": "marked", "kind": "ssh_key", "secret": secret_marker},
    )
    marked_id = int(resp.json()["id"])

    body = _create_server(
        client, admin_headers, name="marked-host", credential_id=marked_id
    )
    _trust(client, admin_headers, monkeypatch, body)

    check = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)
    assert check.status_code == 202, check.text
    assert secret_marker not in check.text

    task_id = check.json()["id"]
    runs = client.get(f"/api/v1/tasks/{task_id}/runs", headers=admin_headers)
    assert runs.status_code == 200
    run_id = runs.json()[0]["id"]

    for url in (
        f"/api/v1/tasks/{task_id}",
        f"/api/v1/tasks/{task_id}/runs",
        f"/api/v1/task-runs/{run_id}/logs",
        f"/api/v1/servers/{body['id']}",
    ):
        got = client.get(url, headers=admin_headers)
        assert secret_marker not in got.text, url

    # Not in the credential view either.
    view = client.get(f"/api/v1/credentials/{marked_id}", headers=admin_headers)
    assert secret_marker not in view.text
    assert "payload_encrypted" not in view.text

    # And not in anything the request path logged.
    _out, err = capsys.readouterr()
    assert secret_marker not in err
    assert secret_marker not in _out


# --------------------------------------------------------------------------
# RBAC — the backend refuses; hiding a button is not a control
# --------------------------------------------------------------------------
def test_viewer_can_read_but_not_write(
    client: TestClient, viewer_headers: dict[str, str]
) -> None:
    listed = client.get("/api/v1/servers", headers=viewer_headers)
    assert listed.status_code == 200

    created = client.post(
        "/api/v1/servers",
        headers=viewer_headers,
        json={"name": "nope", "hostname": "10.0.0.50"},
    )
    assert created.status_code == 403
    assert created.json()["code"] == "forbidden"

    checked = client.post("/api/v1/servers/1/check", headers=viewer_headers)
    assert checked.status_code == 403


def test_viewer_cannot_accept_a_host_key(
    client: TestClient, viewer_headers: dict[str, str], admin_headers: dict[str, str]
) -> None:
    body = _create_server(client, admin_headers)
    resp = client.post(
        f"/api/v1/servers/{body['id']}/accept-hostkey",
        headers=viewer_headers,
        json={"fingerprint": FAKE_FINGERPRINT},
    )
    assert resp.status_code == 403
    assert "server.update" in resp.json()["message"]


def test_unauthenticated_is_401(client: TestClient) -> None:
    get_resp = client.get("/api/v1/servers")
    assert get_resp.status_code == 401
    assert get_resp.json()["code"] == "unauthenticated"

    post_resp = client.post("/api/v1/servers", json={"name": "x", "hostname": "10.0.0.1"})
    assert post_resp.status_code == 401
    assert post_resp.json()["code"] == "unauthenticated"


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
def test_host_key_acceptance_is_audited(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_trust_flow(monkeypatch)
    body = _create_server(client, admin_headers)
    client.post(f"/api/v1/servers/{body['id']}/discover-hostkey", headers=admin_headers)
    client.post(
        f"/api/v1/servers/{body['id']}/accept-hostkey",
        headers=admin_headers,
        json={"fingerprint": FAKE_FINGERPRINT},
    )

    audit = client.get("/api/v1/audit-logs", headers=admin_headers)
    actions = [row["action"] for row in audit.json()]
    assert "server.create" in actions
    assert "server.hostkey.discover" in actions
    assert "server.hostkey.accept" in actions

    accept = next(r for r in audit.json() if r["action"] == "server.hostkey.accept")
    snap = accept.get("snapshot") or {}
    assert snap.get("fingerprint") == FAKE_FINGERPRINT
    # The audit snapshot must not carry key material.
    assert "payload" not in snap
    assert "secret" not in snap
