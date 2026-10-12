"""Task API contract.

Focuses on the behaviours that are easy to get wrong and expensive to get
wrong: idempotent submit, cancel needing real confirmation, ``UNKNOWN`` never
being auto-retried, and the SSE stream actually emitting frames.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from shared.dbtypes import utcnow
from shared.task_protocol import (
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_SUCCESS,
    RUN_UNKNOWN,
    RUN_WAITING,
    TASK_FAILED,
    TASK_QUEUED,
    Task,
    TaskLog,
    TaskRun,
)
from sqlalchemy.orm import Session

from backend.app.core.security import create_access_token


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _login(client: TestClient) -> str:
    from backend.tests.conftest import BOOTSTRAP_ADMIN, BOOTSTRAP_PASSWORD

    resp = client.post(
        "/api/v1/auth/login",
        json={"username": BOOTSTRAP_ADMIN, "password": BOOTSTRAP_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return str(body["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_server(db: Session) -> int:
    from backend.app.credentials.models import Credential
    from backend.app.inventory.models import (
        HOSTKEY_VERIFIED,
        SERVER_ACTIVE,
        Server,
    )

    cred = Credential(
        name="c1",
        kind="ssh_key",
        description="",
        status="active",
        secret_ref="cred:1",
        payload_encrypted="Z0FBQUFBQm4",
        fingerprint="SHA256:x",
    )
    db.add(cred)
    db.flush()
    server = Server(
        name="web-1",
        hostname="127.0.0.1",
        port=22,
        ssh_user="opstest",
        credential_id=cred.id,
        description="",
        status=SERVER_ACTIVE,
        host_key_state=HOSTKEY_VERIFIED,
        host_key_fingerprint="SHA256:" + "A" * 43,
        host_key_algorithm="ssh-ed25519",
        last_check_detail="",
    )
    db.add(server)
    db.commit()
    return int(server.id)


def _seed_run(
    db: Session,
    *,
    status: str = RUN_UNKNOWN,
    attempt: int = 1,
) -> tuple[Task, TaskRun]:
    task = Task(
        kind="server.check",
        title="check",
        status=TASK_QUEUED,
        idempotency_key="",
        params_json={},
        target_snapshot_json={"server_ids": [1]},
        requested_by=1,
        requested_by_name="tester",
        queued_at=utcnow(),
    )
    db.add(task)
    db.flush()
    run = TaskRun(
        task_id=task.id,
        server_id=1,
        server_snapshot_json={},
        status=status,
        attempt=attempt,
    )
    db.add(run)
    db.commit()
    return task, run


# --------------------------------------------------------------------------
class TestCreate:
    def test_create_returns_201_and_writes_runs(self, client: TestClient, db: Session) -> None:
        sid = _make_server(db)
        token = _login(client)
        resp = client.post(
            "/api/v1/tasks",
            headers=_auth(token),
            json={
                "kind": "server.check",
                "title": "nightly check",
                "params": {},
                "targets": [{"server_id": sid}],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["kind"] == "server.check"
        assert body["status"] == TASK_QUEUED

        runs = client.get(
            f"/api/v1/tasks/{body['id']}/runs", headers=_auth(token)
        ).json()
        assert len(runs) == 1
        assert runs[0]["server_id"] == sid
        assert runs[0]["status"] == RUN_WAITING

    def test_duplicate_submit_returns_the_original_task(
        self, client: TestClient, db: Session
    ) -> None:
        sid = _make_server(db)
        token = _login(client)
        payload = {
            "kind": "server.check",
            "title": "once",
            "params": {},
            "targets": [{"server_id": sid}],
        }
        headers = {**_auth(token), "Idempotency-Key": "submit-1"}

        first = client.post("/api/v1/tasks", headers=headers, json=payload)
        second = client.post("/api/v1/tasks", headers=headers, json=payload)

        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]

        rows = client.get("/api/v1/tasks", headers=_auth(token)).json()
        assert len(rows) == 1

    def test_same_key_different_body_is_409(self, client: TestClient, db: Session) -> None:
        sid = _make_server(db)
        token = _login(client)
        headers = {**_auth(token), "Idempotency-Key": "submit-2"}
        base = {
            "kind": "server.check",
            "title": "a",
            "params": {},
            "targets": [{"server_id": sid}],
        }
        assert client.post("/api/v1/tasks", headers=headers, json=base).status_code == 201
        changed = {**base, "title": "b"}
        resp = client.post("/api/v1/tasks", headers=headers, json=changed)
        assert resp.status_code == 409
        assert resp.json()["code"] == "idempotency_key_reuse"

    def test_unknown_kind_is_rejected(self, client: TestClient, db: Session) -> None:
        sid = _make_server(db)
        token = _login(client)
        resp = client.post(
            "/api/v1/tasks",
            headers=_auth(token),
            json={
                "kind": "drop_database",
                "targets": [{"server_id": sid}],
            },
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    def test_hostile_kind_is_rejected(self, client: TestClient, db: Session) -> None:
        sid = _make_server(db)
        token = _login(client)
        for kind in ("rm -rf /", "$(id)", "a;b", "../../../etc/passwd", "-oProxyCommand=x"):
            resp = client.post(
                "/api/v1/tasks",
                headers=_auth(token),
                json={"kind": kind, "targets": [{"server_id": sid}]},
            )
            assert resp.status_code == 422, kind

    def test_viewer_cannot_create(self, client: TestClient, db: Session, viewer: Any) -> None:
        sid = _make_server(db)
        _login(client)
        # Mint a token for the viewer account directly.
        from backend.app.auth.service import AuthService

        user = AuthService(db).get_user_by_username("viewer")
        assert user is not None
        vtoken = create_access_token(user.id, user.username)
        resp = client.post(
            "/api/v1/tasks",
            headers=_auth(vtoken),
            json={"kind": "server.check", "targets": [{"server_id": sid}]},
        )
        assert resp.status_code == 403
        assert "task.create" in resp.json()["message"]

    def test_unauthenticated_is_401(self, client: TestClient) -> None:
        assert client.get("/api/v1/tasks").status_code == 401
        assert client.post("/api/v1/tasks", json={"kind": "x", "targets": []}).status_code == 401


class TestCancel:
    def test_cancel_without_confirmation_is_rejected(
        self, client: TestClient, db: Session
    ) -> None:
        task, _run = _seed_run(db)
        token = _login(client)
        resp = client.post(
            f"/api/v1/tasks/{task.id}/cancel",
            headers=_auth(token),
            json={"reason": "oops", "confirm": False},
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

        db.refresh(task)
        assert task.status == TASK_QUEUED

    def test_cancel_stops_unstarted_runs_and_flags_active_ones(
        self, client: TestClient, db: Session
    ) -> None:
        from shared.task_protocol import RUN_EXECUTING

        task, run = _seed_run(db, status=RUN_EXECUTING)
        other = TaskRun(task_id=task.id, server_id=2, status=RUN_WAITING, attempt=1)
        db.add(other)
        db.commit()

        token = _login(client)
        resp = client.post(
            f"/api/v1/tasks/{task.id}/cancel",
            headers=_auth(token),
            json={"reason": "wrong host", "confirm": True},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "CANCELLING"

        db.refresh(run)
        db.refresh(other)
        assert run.status == "CANCELLING"
        assert other.status == RUN_CANCELLED

    def test_cancelled_wait_run_stays_cancelled(self, client: TestClient, db: Session) -> None:
        task, _ = _seed_run(db, status=RUN_WAITING)
        token = _login(client)
        client.post(
            f"/api/v1/tasks/{task.id}/cancel",
            headers=_auth(token),
            json={"reason": "", "confirm": True},
        )
        runs = client.get(f"/api/v1/tasks/{task.id}/runs", headers=_auth(token)).json()
        assert runs[0]["status"] == RUN_CANCELLED


class TestRetryAndResolve:
    def test_unknown_run_is_not_auto_retried(self, client: TestClient, db: Session) -> None:
        task, run = _seed_run(db, status=RUN_UNKNOWN, attempt=2)
        task.status = TASK_FAILED
        db.commit()

        token = _login(client)
        resp = client.post(
            f"/api/v1/tasks/{task.id}/retry",
            headers=_auth(token),
            json={"reason": "please just run it"},
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "run_unknown_needs_resolution"

        db.refresh(run)
        assert run.status == RUN_UNKNOWN
        assert run.attempt == 2

    def test_resolve_success_records_the_verdict(
        self, client: TestClient, db: Session
    ) -> None:
        task, run = _seed_run(db, status=RUN_UNKNOWN)
        task.status = TASK_FAILED
        db.commit()

        token = _login(client)
        resp = client.post(
            f"/api/v1/task-runs/{run.id}/resolve",
            headers=_auth(token),
            json={"verdict": "success", "reason": "release dir is there"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == RUN_SUCCESS

        db.refresh(run)
        assert run.status == RUN_SUCCESS
        assert "release dir" in run.recovery_note

    def test_resolve_retry_opens_a_new_attempt(
        self, client: TestClient, db: Session
    ) -> None:
        task, run = _seed_run(db, status=RUN_UNKNOWN, attempt=1)
        task.status = TASK_FAILED
        db.commit()

        token = _login(client)
        resp = client.post(
            f"/api/v1/task-runs/{run.id}/resolve",
            headers=_auth(token),
            json={"verdict": "retry", "reason": "nothing landed"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == RUN_WAITING
        assert resp.json()["attempt"] == 2

        db.refresh(run)
        assert run.status == RUN_WAITING
        assert run.attempt == 2

    def test_resolve_needs_a_reason(self, client: TestClient, db: Session) -> None:
        _task, run = _seed_run(db, status=RUN_UNKNOWN)
        token = _login(client)
        resp = client.post(
            f"/api/v1/task-runs/{run.id}/resolve",
            headers=_auth(token),
            json={"verdict": "success", "reason": ""},
        )
        assert resp.status_code == 422

    def test_resolve_refuses_a_non_unknown_run(
        self, client: TestClient, db: Session
    ) -> None:
        _task, run = _seed_run(db, status=RUN_SUCCESS)
        token = _login(client)
        resp = client.post(
            f"/api/v1/task-runs/{run.id}/resolve",
            headers=_auth(token),
            json={"verdict": "retry", "reason": "nope"},
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "run_not_unknown"

    def test_retry_bumps_attempt_on_the_same_row(
        self, client: TestClient, db: Session
    ) -> None:
        task, _run = _seed_run(db, status=RUN_FAILED, attempt=1)
        task.status = TASK_FAILED
        db.commit()

        token = _login(client)
        resp = client.post(
            f"/api/v1/tasks/{task.id}/retry",
            headers=_auth(token),
            json={"reason": "transient network"},
        )
        assert resp.status_code == 200, resp.text

        runs = client.get(f"/api/v1/tasks/{task.id}/runs", headers=_auth(token)).json()
        assert len(runs) == 1
        assert runs[0]["attempt"] == 2
        assert runs[0]["status"] == RUN_WAITING


class TestLogs:
    def test_logs_are_paginated_by_seq(self, client: TestClient, db: Session) -> None:
        _task, run = _seed_run(db, status=RUN_SUCCESS)
        for i in range(1, 6):
            db.add(TaskLog(run_id=run.id, seq=i, message=f"line {i}", level="info"))
        db.commit()

        token = _login(client)
        page = client.get(
            f"/api/v1/task-runs/{run.id}/logs",
            headers=_auth(token),
            params={"limit": 2},
        ).json()
        assert [i["seq"] for i in page["items"]] == [1, 2]
        assert page["next_seq"] == 2
        assert page["total"] == 5

        page2 = client.get(
            f"/api/v1/task-runs/{run.id}/logs",
            headers=_auth(token),
            params={"since_seq": page["next_seq"], "limit": 10},
        ).json()
        assert [i["seq"] for i in page2["items"]] == [3, 4, 5]

    def test_log_messages_never_carry_key_material(
        self, client: TestClient, db: Session
    ) -> None:
        _task, run = _seed_run(db, status=RUN_SUCCESS)
        db.add(
            TaskLog(
                run_id=run.id,
                seq=1,
                message="connecting with password=hunter2",
                level="info",
            )
        )
        db.commit()

        token = _login(client)
        body = client.get(
            f"/api/v1/task-runs/{run.id}/logs", headers=_auth(token)
        ).json()
        assert "hunter2" not in json.dumps(body)


class TestSse:
    def test_events_require_a_token(self, client: TestClient, db: Session) -> None:
        task, _ = _seed_run(db)
        resp = client.get(f"/api/v1/tasks/{task.id}/events")
        assert resp.status_code == 401

    def test_events_reject_a_garbage_token(self, client: TestClient, db: Session) -> None:
        task, _ = _seed_run(db)
        resp = client.get(f"/api/v1/tasks/{task.id}/events", params={"token": "nope"})
        assert resp.status_code == 401

    def test_events_stream_frames_for_a_finished_task(
        self, client: TestClient, db: Session
    ) -> None:
        _task, run = _seed_run(db, status=RUN_SUCCESS)
        db.add(TaskLog(run_id=run.id, seq=1, message="hello from the worker", level="info"))
        db.commit()

        from backend.app.auth.service import AuthService

        user = AuthService(db).get_user_by_username("admin")
        assert user is not None
        token = create_access_token(user.id, user.username)

        with client.stream(
            "GET",
            f"/api/v1/tasks/{_task.id}/events",
            params={"token": token},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            collected: list[str] = []
            for line in resp.iter_lines():
                collected.append(line)
                if any(c.startswith("event: done") for c in collected):
                    break
            text = "\n".join(collected)

        assert "event: hello" in text
        assert "hello from the worker" in text
        assert "event: done" in text
        # The stream must never leak a bearer token back out.
        assert token not in text.split("data:")[-1]
