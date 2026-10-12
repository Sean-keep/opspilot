"""Inventory API: CRUD, RBAC, host-key trust, and the check refusal path.

The live SSH transport is exercised separately (``test_ssh_live.py``). Here the
transport is a fake so the tests can assert what the *platform* does: refuse
untrusted hosts, never echo secrets, and enforce permissions on the backend.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.executor.port import ExecContext, ExecResult
from backend.app.inventory.models import (
    CHECK_OK,
    HOSTKEY_PENDING_CONFIRM,
    HOSTKEY_UNVERIFIED,
    HOSTKEY_VERIFIED,
    SERVER_DELETED,
    Server,
)

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeExecPort:
    """Records argv lists and returns canned output. Never opens a socket."""

    def __init__(
        self,
        outputs: dict[str, str] | None = None,
        *,
        exit_code: int = 0,
    ) -> None:
        self.calls: list[tuple[ExecContext, list[str]]] = []
        self.outputs = outputs or {}
        self.exit_code = exit_code

    def run(self, ctx: ExecContext, argv: Sequence[str]) -> ExecResult:
        argv_list = list(argv)
        self.calls.append((ctx, argv_list))
        key = " ".join(argv_list[:2])
        return ExecResult(
            exit_code=self.exit_code,
            stdout=self.outputs.get(key, self.outputs.get(argv_list[0], "")),
            stderr="",
            argv=tuple(argv_list),
        )


OS_RELEASE = 'NAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\n'
UNAME = "Linux 6.8.0-45-generic x86_64\n"
NPROC = "4\n"
MEMINFO = "MemTotal:       16326224 kB\nMemAvailable:    8123456 kB\n"
CPUINFO = "model name\t: Intel(R) Core(TM) i5-10400 CPU @ 2.90GHz\n"
DF = (
    "Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
    "/dev/sda1      10737418240 536870912 10200547328       5% /\n"
)


def outputs_for_facts() -> dict[str, str]:
    """Canned remote output, keyed by the first two argv tokens.

    The check action always issues the same fixed argv lists (see
    ``backend/app/inventory/checks.py``), so those keys are unambiguous.
    """
    return {
        "cat /etc/os-release": OS_RELEASE,
        "uname -srm": UNAME,
        "nproc": NPROC,
        "cat /proc/meminfo": MEMINFO,
        "grep -m1": CPUINFO,
        "df -P": DF,
    }


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
    fake = FakeExecPort(outputs_for_facts())
    monkeypatch.setattr("backend.app.inventory.service.SshExecPort", lambda **_kw: fake)

    body = _create_server(client, admin_headers, credential_id=credential_id)
    resp = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)

    assert resp.status_code == 409
    assert resp.json()["code"] == "host_key_not_verified"
    assert fake.calls == [], "no command may run on an untrusted host"


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
# Check
# --------------------------------------------------------------------------
def test_check_collects_facts_and_stores_a_snapshot(
    client: TestClient,
    admin_headers: dict[str, str],
    credential_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_trust_flow(monkeypatch)
    fake = FakeExecPort(outputs_for_facts())
    monkeypatch.setattr("backend.app.inventory.service.SshExecPort", lambda **_kw: fake)

    body = _create_server(client, admin_headers, credential_id=credential_id)
    client.post(f"/api/v1/servers/{body['id']}/discover-hostkey", headers=admin_headers)
    client.post(
        f"/api/v1/servers/{body['id']}/accept-hostkey",
        headers=admin_headers,
        json={"fingerprint": FAKE_FINGERPRINT},
    )

    resp = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["ok"] is True
    facts = result["facts"]
    assert facts["os_name"] == "ubuntu"
    assert facts["os_version"] == "24.04"
    assert facts["arch"] == "x86_64"
    assert facts["cpu_count"] == 4
    assert "i5-10400" in facts["cpu_model"]
    assert facts["memory_total_mb"] == 15943  # 16326224 kB // 1024
    assert facts["disk_total_gb"] > 0

    # Every remote command was argv-separated, not a shell line.
    for _ctx, argv in fake.calls:
        assert isinstance(argv, list)
        assert all(isinstance(a, str) for a in argv)
        assert not any(x in argv for x in ("sh", "bash", "sudo"))
        # The cpu model lookup must not go through a shell.
        if argv and argv[0] == "grep":
            assert argv[1] == "-m1"

    # The snapshot is persisted and visible on the server.
    listed = client.get(f"/api/v1/servers/{body['id']}", headers=admin_headers)
    assert listed.json()["last_check_status"] == CHECK_OK
    assert listed.json()["latest_facts"]["os_name"] == "ubuntu"

    history = client.get(f"/api/v1/servers/{body['id']}/facts", headers=admin_headers)
    assert history.status_code == 200
    assert len(history.json()) == 1


def test_check_refuses_when_credential_missing(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_trust_flow(monkeypatch)
    fake = FakeExecPort(outputs_for_facts())
    monkeypatch.setattr("backend.app.inventory.service.SshExecPort", lambda **_kw: fake)

    body = _create_server(client, admin_headers)  # no credential
    client.post(f"/api/v1/servers/{body['id']}/discover-hostkey", headers=admin_headers)
    client.post(
        f"/api/v1/servers/{body['id']}/accept-hostkey",
        headers=admin_headers,
        json={"fingerprint": FAKE_FINGERPRINT},
    )

    resp = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)
    assert resp.status_code == 422
    assert "credential" in resp.json()["message"]
    assert fake.calls == []


def test_check_failure_is_a_result_not_an_http_error(
    client: TestClient,
    admin_headers: dict[str, str],
    credential_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that is down is an answer the operator asked for."""
    from backend.app.executor.port import TransportError

    _patch_trust_flow(monkeypatch)

    class FailingPort(FakeExecPort):
        def run(self, ctx: ExecContext, argv: Sequence[str]) -> ExecResult:
            raise TransportError("connection refused")

    fake = FailingPort()
    monkeypatch.setattr("backend.app.inventory.service.SshExecPort", lambda **_kw: fake)

    body = _create_server(client, admin_headers, credential_id=credential_id)
    client.post(f"/api/v1/servers/{body['id']}/discover-hostkey", headers=admin_headers)
    client.post(
        f"/api/v1/servers/{body['id']}/accept-hostkey",
        headers=admin_headers,
        json={"fingerprint": FAKE_FINGERPRINT},
    )

    resp = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "connection refused" in resp.json()["detail"]
    assert resp.json()["facts"] is None

    listed = client.get(f"/api/v1/servers/{body['id']}", headers=admin_headers)
    assert listed.json()["last_check_status"] == "failed"


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

    _patch_trust_flow(monkeypatch)
    fake = FakeExecPort(outputs_for_facts())
    monkeypatch.setattr("backend.app.inventory.service.SshExecPort", lambda **_kw: fake)

    body = _create_server(
        client, admin_headers, name="marked-host", credential_id=marked_id
    )
    client.post(f"/api/v1/servers/{body['id']}/discover-hostkey", headers=admin_headers)
    client.post(
        f"/api/v1/servers/{body['id']}/accept-hostkey",
        headers=admin_headers,
        json={"fingerprint": FAKE_FINGERPRINT},
    )
    check = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)
    assert check.status_code == 200, check.text

    assert secret_marker not in check.text

    # Not in the credential view either.
    view = client.get(f"/api/v1/credentials/{marked_id}", headers=admin_headers)
    assert secret_marker not in view.text
    assert "payload_encrypted" not in view.text

    # And not in anything the transport was asked to log.
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
