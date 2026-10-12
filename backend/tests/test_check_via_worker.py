"""The S1 -> S2 seam, end to end.

``POST /api/v1/servers/{id}/check`` used to SSH to the host inside the request
and answer immediately. Since S2 it only enqueues a task; the Worker is the
only process that decrypts a credential and opens a session.

These tests drive both halves against real MySQL: the API enqueues, the worker
executes with a fake transport, and the facts land where the UI reads them.
No socket is opened — the live SSH path is ``test_ssh_live.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from shared.config import Settings
from shared.executor.port import ExecContext, ExecResult, TransportError
from shared.task_protocol import Task, claim_runs, recompute_task_status
from sqlalchemy.orm import Session
from worker.runner import Runner


class FakePort:
    """Canned facts, or a transport failure. Never opens a socket."""

    def __init__(
        self,
        *,
        fail: Exception | None = None,
        outputs: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail = fail
        self.outputs = outputs or {
            "cat /etc/os-release": 'NAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\n',
            "uname -srm": "Linux 6.8.0-45-generic x86_64\n",
            "nproc": "4\n",
            "cat /proc/meminfo": "MemTotal:       16326224 kB\nMemAvailable:    8123456 kB\n",
            "grep -m1": "model name\t: Intel(R) Core(TM) i5-10400 CPU @ 2.90GHz\n",
            "df -P": (
                "Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
                "/dev/sda1      10737418240 536870912 10200547328       5% /\n"
            ),
        }

    def run(self, ctx: ExecContext, argv: Any) -> ExecResult:
        argv_list = [str(a) for a in argv]
        self.calls.append(tuple(argv_list))
        if self.fail is not None:
            raise self.fail
        key = " ".join(argv_list[:2])
        return ExecResult(
            exit_code=0,
            stdout=self.outputs.get(key, self.outputs.get(argv_list[0], "")),
            stderr="",
            argv=tuple(argv_list),
        )


FAKE_FINGERPRINT = "SHA256:" + "B" * 43


def _fake_port(self: Any, *args: Any, **kwargs: Any) -> ExecContext:
    raise AssertionError("the API must not construct a transport")


def _trust(
    client: TestClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
) -> None:
    monkeypatch.setattr(
        "backend.app.inventory.service.discover_host_key",
        lambda host, port, *, connect_timeout=10: {
            "algorithm": "ssh-ed25519",
            "fingerprint": FAKE_FINGERPRINT,
        },
    )
    client.post(f"/api/v1/servers/{body['id']}/discover-hostkey", headers=headers)
    client.post(
        f"/api/v1/servers/{body['id']}/accept-hostkey",
        headers=headers,
        json={"fingerprint": FAKE_FINGERPRINT},
    )


def _make_ready_server(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    secret: str = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END",
    name: str = "seam-host",
) -> dict[str, Any]:
    cred = client.post(
        "/api/v1/credentials",
        headers=admin_headers,
        json={"name": f"{name}-key", "kind": "ssh_key", "secret": secret},
    )
    assert cred.status_code == 201, cred.text
    server = client.post(
        "/api/v1/servers",
        headers=admin_headers,
        json={
            "name": name,
            "hostname": "10.0.0.44",
            "port": 22,
            "ssh_user": "opstest",
            "credential_id": cred.json()["id"],
        },
    )
    assert server.status_code == 201, server.text
    body = dict(server.json())
    _trust(client, admin_headers, monkeypatch, body)
    return body


def _run_one_task(
    db: Session,
    settings: Settings,
    *,
    port: FakePort,
    worker_id: str = "seam-worker",
) -> Task:
    """Claim whatever is waiting and run it, exactly as the scheduler does."""
    claimed = claim_runs(db, owner=worker_id, limit=5, lease_seconds=30)
    assert len(claimed) == 1, f"expected one run to claim, got {len(claimed)}"
    runner = Runner(db, settings, worker_id=worker_id, port_factory=lambda: port)
    runner.execute(claimed[0])
    recompute_task_status(db, task_id=claimed[0].task_id)
    task = db.get(Task, claimed[0].task_id)
    assert task is not None
    return task


# --------------------------------------------------------------------------
class TestCheckViaWorker:
    def test_enqueued_check_reaches_the_worker_and_stores_facts(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        db: Session,
        settings: Settings,
    ) -> None:
        body = _make_ready_server(client, admin_headers, monkeypatch)

        queued = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)
        assert queued.status_code == 202, queued.text
        task_id = queued.json()["id"]

        # The API side has done everything it is allowed to do.
        assert queued.json()["status"] == "QUEUED"

        port = FakePort()
        task = _run_one_task(db, settings, port=port)

        assert task.id == task_id
        assert task.status == "SUCCESS"

        # The facts are on the server record the UI reads.
        listed = client.get(f"/api/v1/servers/{body['id']}", headers=admin_headers)
        assert listed.status_code == 200
        row = listed.json()
        assert row["last_check_status"] == "ok"
        facts = row["latest_facts"]
        assert facts is not None
        assert facts["os_name"] == "ubuntu"
        assert facts["os_version"] == "24.04"
        assert facts["arch"] == "x86_64"
        assert facts["cpu_count"] == 4
        assert "i5-10400" in facts["cpu_model"]
        assert facts["memory_total_mb"] == 15943  # 16326224 kB // 1024
        assert facts["disk_total_gb"] > 0

        history = client.get(f"/api/v1/servers/{body['id']}/facts", headers=admin_headers)
        assert history.status_code == 200
        assert len(history.json()) == 1

        # Every remote command was argv-separated, not a shell line.
        assert port.calls, "the worker must actually talk to the host"
        for argv in port.calls:
            assert all(isinstance(a, str) for a in argv)
            assert not any(x in argv for x in ("sh", "bash", "sudo"))
            if argv and argv[0] == "grep":
                assert argv[1] == "-m1"

    def test_unreachable_host_is_a_result_not_an_api_error(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        db: Session,
        settings: Settings,
    ) -> None:
        """A host that is down answers the operator's question.

        The HTTP call that *queued* the work succeeds; the failure lands on the
        run and on the server's ``last_check_status``.
        """
        body = _make_ready_server(
            client, admin_headers, monkeypatch, name="down-host"
        )
        queued = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)
        assert queued.status_code == 202, queued.text

        task = _run_one_task(
            db,
            settings,
            port=FakePort(fail=TransportError("connection refused")),
        )

        assert task.status == "FAILED"
        listed = client.get(f"/api/v1/servers/{body['id']}", headers=admin_headers)
        assert listed.json()["last_check_status"] == "failed"
        assert "connection refused" in listed.json()["last_check_detail"]

        # The run carries the detail the UI shows.
        runs = client.get(f"/api/v1/tasks/{task.id}/runs", headers=admin_headers)
        assert runs.status_code == 200
        detail = runs.json()[0]
        assert detail["status"] == "FAILED"
        assert "connection refused" in detail["error_detail"]

        # And the facts history is still empty — nothing was collected.
        history = client.get(f"/api/v1/servers/{body['id']}/facts", headers=admin_headers)
        assert history.json() == []

    def test_key_material_never_reaches_the_task_surface(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        db: Session,
        settings: Settings,
    ) -> None:
        marker = "SUPER-SECRET-KEY-MATERIAL-DO-NOT-LOG"
        body = _make_ready_server(
            client, admin_headers, monkeypatch, secret=marker, name="marked-seam"
        )
        queued = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)
        assert queued.status_code == 202, queued.text
        task_id = queued.json()["id"]

        # Run it too, so the logs and the step result exist to inspect.
        _run_one_task(db, settings, port=FakePort())

        for url in (
            f"/api/v1/tasks/{task_id}",
            f"/api/v1/tasks/{task_id}/runs",
            f"/api/v1/servers/{body['id']}",
            f"/api/v1/servers/{body['id']}/facts",
        ):
            got = client.get(url, headers=admin_headers)
            assert marker not in got.text, url

        runs = client.get(f"/api/v1/tasks/{task_id}/runs", headers=admin_headers)
        run_id = runs.json()[0]["id"]
        logs = client.get(f"/api/v1/task-runs/{run_id}/logs", headers=admin_headers)
        assert logs.status_code == 200
        assert marker not in logs.text

    def test_the_api_never_builds_a_transport(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Structural: no request path may construct an SSH port.

        If someone puts a socket back in the API, this fails rather than
        quietly reopening the hole the S2 seam closed.
        """
        monkeypatch.setattr(
            "backend.app.inventory.service.discover_host_key",
            lambda host, port, *, connect_timeout=10: {
                "algorithm": "ssh-ed25519",
                "fingerprint": FAKE_FINGERPRINT,
            },
        )
        monkeypatch.setattr(
            "backend.app.executor.ssh.SshExecPort",
            _fake_port,
            raising=True,
        )
        monkeypatch.setattr(
            "shared.executor.ssh.SshExecPort",
            _fake_port,
            raising=True,
        )

        body = _make_ready_server(client, admin_headers, monkeypatch, name="no-socket")
        # discover-hostkey is the documented exception: an unauthenticated
        # banner fetch with no credentials and no commands. Everything else —
        # create, list, check — must get through without a transport.
        assert client.get("/api/v1/servers", headers=admin_headers).status_code == 200
        queued = client.post(f"/api/v1/servers/{body['id']}/check", headers=admin_headers)
        assert queued.status_code == 202, queued.text
