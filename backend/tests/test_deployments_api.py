"""Deployment API contract.

The behaviours that are expensive to get wrong are the ones that change what is
running on a host: a mutable release alias, a release without a checksum, two
concurrent deploys into the same environment, a rollback that is not confirmed,
and — the one that is invisible until it leaks — a secret that reaches the task
row. Each of those is refused here rather than being caught three steps in.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


CHECKSUM = hashlib.sha256(b"billing-1.4.2").hexdigest()


def _make_server(db: Session, name: str = "web-1") -> int:
    from backend.app.credentials.models import Credential
    from backend.app.inventory.models import (
        HOSTKEY_VERIFIED,
        SERVER_ACTIVE,
        Server,
    )

    cred = Credential(
        name=f"c-{name}",
        kind="ssh_key",
        description="",
        status="active",
        secret_ref=f"cred:{name}",
        payload_encrypted="Z0FBQUFBQm4",
        fingerprint="SHA256:x",
    )
    db.add(cred)
    db.flush()
    server = Server(
        name=name,
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


def _setup(
    client: TestClient, headers: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """An application with a production environment, ready to deploy into."""
    app = client.post(
        "/api/v1/applications",
        headers=headers,
        json={"name": "billing-api", "run_user": "billing"},
    )
    assert app.status_code == 201, app.text
    env = client.post(
        f"/api/v1/applications/{app.json()['id']}/environments",
        headers=headers,
        json={
            "name": "production",
            "config_ref": "cred:77",
            "service_name": "opspilot-billing",
            "unit_path": "/etc/systemd/system/opspilot-billing.service",
            "health_url": "http://127.0.0.1:8000/healthz",
            "health_timeout_s": 5,
        },
    )
    assert env.status_code == 201, env.text
    return app.json(), env.json()


def _deploy_body(server_id: int, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "environment": "production",
        "release_version": "1.4.2",
        "artifact_url": "https://artifacts.example.com/billing-1.4.2.tar.gz",
        "artifact_checksum": CHECKSUM,
        "targets": [{"server_id": server_id}],
        "module_name": "billing.main",
        "app_port": 8000,
        "python_version": "3.12",
        "requirements": "requirements.lock",
        "config": {"LOG_LEVEL": "info"},
    }
    body.update(overrides)
    return body


def _submit(
    client: TestClient, headers: dict[str, str], app_id: int, body: dict[str, Any]
) -> Any:
    return client.post(
        f"/api/v1/applications/{app_id}/deploy", headers=headers, json=body
    )


def _mark_succeeded(db: Session, deployment_id: int) -> None:
    db.execute(
        text("UPDATE ops_deployments SET status = 'SUCCEEDED' WHERE id = :id"),
        {"id": deployment_id},
    )
    db.commit()


def _task_params(db: Session, task_id: int) -> dict[str, Any]:
    raw: Any = db.execute(
        text("SELECT params_json FROM ops_tasks WHERE id = :id"), {"id": task_id}
    ).scalar_one()
    # Raw SQL hands back the JSON column as text; the ORM is what decodes it.
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


# --------------------------------------------------------------------------
class TestDeploySubmit:
    def test_submit_is_202_and_records_the_release(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        resp = _submit(client, admin_headers, app["id"], _deploy_body(sid))
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["kind"] == "DEPLOY"
        assert body["status"] == "PENDING"
        assert body["release_id"] == "1.4.2"
        assert body["task_id"] is not None
        assert resp.headers["Location"].endswith(f"/api/v1/deployments/{body['id']}")

    def test_the_queued_task_is_an_app_deploy(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        body = _submit(client, admin_headers, app["id"], _deploy_body(sid)).json()
        kind: str = db.execute(
            text("SELECT kind FROM ops_tasks WHERE id = :id"), {"id": body["task_id"]}
        ).scalar_one()
        assert kind == "app.deploy"

    def test_a_mutable_release_alias_is_refused(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        for alias in ("latest", "current", "shared", "releases", "ops"):
            resp = _submit(
                client, admin_headers, app["id"], _deploy_body(sid, release_version=alias)
            )
            assert resp.status_code == 422, f"{alias} -> {resp.status_code}"

    def test_a_release_without_a_checksum_is_refused(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        for checksum in ("", "abc", "z" * 64):
            resp = _submit(
                client, admin_headers, app["id"], _deploy_body(sid, artifact_checksum=checksum)
            )
            assert resp.status_code == 422, repr(checksum)

    def test_an_artifact_url_is_ssrf_checked(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        for url in (
            "file:///etc/passwd",
            "http://169.254.169.254/latest/meta-data/",
            "http://user:pass@artifacts.example.com/x.tgz",
        ):
            resp = _submit(
                client, admin_headers, app["id"], _deploy_body(sid, artifact_url=url)
            )
            assert resp.status_code == 422, f"{url} -> {resp.status_code}"

    def test_needs_at_least_one_target(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        _make_server(db)
        resp = _submit(client, admin_headers, app["id"], _deploy_body(1, targets=[]))
        assert resp.status_code == 422, resp.text

    def test_deploying_into_an_unknown_environment_is_404(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        resp = _submit(
            client, admin_headers, app["id"], _deploy_body(sid, environment="staging")
        )
        assert resp.status_code == 404, resp.text


class TestDeployMutex:
    def test_a_second_deploy_into_a_busy_environment_is_409(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        """§10: one deployment at a time per application environment."""
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        first = _submit(client, admin_headers, app["id"], _deploy_body(sid))
        assert first.status_code == 202, first.text

        second = _submit(
            client, admin_headers, app["id"], _deploy_body(sid, release_version="1.4.3")
        )
        assert second.status_code == 409, second.text

    def test_the_lock_releases_when_the_deployment_finishes(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        first = _submit(client, admin_headers, app["id"], _deploy_body(sid)).json()
        _mark_succeeded(db, first["id"])

        second = _submit(
            client, admin_headers, app["id"], _deploy_body(sid, release_version="1.4.3")
        )
        assert second.status_code == 202, second.text

    def test_a_failed_deployment_also_releases_the_lock(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        first = _submit(client, admin_headers, app["id"], _deploy_body(sid)).json()
        db.execute(
            text("UPDATE ops_deployments SET status = 'FAILED' WHERE id = :id"),
            {"id": first["id"]},
        )
        db.commit()

        second = _submit(
            client, admin_headers, app["id"], _deploy_body(sid, release_version="1.4.3")
        )
        assert second.status_code == 202, second.text


class TestPreviousRelease:
    def test_a_first_deploy_has_nothing_to_restore(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        body = _submit(client, admin_headers, app["id"], _deploy_body(sid)).json()
        assert body["previous_release_id"] == ""

    def test_the_previous_release_is_what_is_currently_live(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        first = _submit(client, admin_headers, app["id"], _deploy_body(sid)).json()
        _mark_succeeded(db, first["id"])

        second = _submit(
            client, admin_headers, app["id"], _deploy_body(sid, release_version="1.4.3")
        ).json()
        assert second["previous_release_id"] == "1.4.2"

    def test_a_failed_attempt_does_not_become_the_previous_release(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        """A deploy that never flipped the link left the old release live."""
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        first = _submit(client, admin_headers, app["id"], _deploy_body(sid)).json()
        _mark_succeeded(db, first["id"])

        bad = _submit(
            client, admin_headers, app["id"], _deploy_body(sid, release_version="2.0.0")
        ).json()
        db.execute(
            text("UPDATE ops_deployments SET status = 'FAILED' WHERE id = :id"),
            {"id": bad["id"]},
        )
        db.commit()

        third = _submit(
            client, admin_headers, app["id"], _deploy_body(sid, release_version="2.0.1")
        ).json()
        assert third["previous_release_id"] == "1.4.2"


class TestSecretsStayOffTheWire:
    def test_the_task_carries_a_reference_not_the_values(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        """``config_ref`` on the task; values resolved by the worker only."""
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        body = _submit(client, admin_headers, app["id"], _deploy_body(sid)).json()
        params = _task_params(db, body["task_id"])

        assert params["config_ref"] == "cred:77"
        deploy = params["deploy"]
        assert "secrets" not in deploy
        assert "config_ref" not in deploy
        assert deploy["config"] == {"LOG_LEVEL": "info"}

    def test_the_deployment_response_does_not_expose_a_secret(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        body = _submit(client, admin_headers, app["id"], _deploy_body(sid)).json()
        listed = client.get(
            f"/api/v1/applications/{app['id']}/deployments", headers=admin_headers
        )
        assert listed.status_code == 200
        blob = listed.text
        assert "cred:77" not in blob
        assert "BEGIN OPENSSH" not in blob
        assert body["artifact_checksum"] == CHECKSUM


class TestRollback:
    def _deploy_then_succeed(
        self, client: TestClient, headers: dict[str, str], db: Session
    ) -> tuple[int, dict[str, Any]]:
        app, _env = _setup(client, headers)
        sid = _make_server(db)
        first = _submit(client, headers, app["id"], _deploy_body(sid)).json()
        _mark_succeeded(db, first["id"])
        second = _submit(
            client, headers, app["id"], _deploy_body(sid, release_version="1.4.3")
        ).json()
        _mark_succeeded(db, second["id"])
        return app["id"], second

    def test_rollback_without_confirmation_is_refused(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        _app_id, target = self._deploy_then_succeed(client, admin_headers, db)
        resp = client.post(
            f"/api/v1/deployments/{target['id']}/rollback",
            headers=admin_headers,
            json={"targets": [{"server_id": 1}], "confirm": False},
        )
        assert resp.status_code == 422, resp.text

    def test_rollback_creates_a_new_deployment_not_an_edit(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        _app_id, target = self._deploy_then_succeed(client, admin_headers, db)
        resp = client.post(
            f"/api/v1/deployments/{target['id']}/rollback",
            headers=admin_headers,
            json={
                "targets": [{"server_id": 1}],
                "reason": "bad release",
                "confirm": True,
            },
        )
        assert resp.status_code == 202, resp.text
        undo = resp.json()
        assert undo["id"] != target["id"]
        assert undo["kind"] == "ROLLBACK"
        assert undo["rollback_of"] == target["id"]
        assert undo["release_id"] == "1.4.2"
        assert undo["previous_release_id"] == "1.4.3"

        # The original keeps its own status; history is not rewritten.
        original = client.get(
            f"/api/v1/deployments/{target['id']}", headers=admin_headers
        ).json()
        assert original["status"] == "SUCCEEDED"

    def test_the_rollback_task_is_an_app_rollback(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        _app_id, target = self._deploy_then_succeed(client, admin_headers, db)
        undo = client.post(
            f"/api/v1/deployments/{target['id']}/rollback",
            headers=admin_headers,
            json={"targets": [{"server_id": 1}], "confirm": True},
        ).json()
        kind: str = db.execute(
            text("SELECT kind FROM ops_tasks WHERE id = :id"), {"id": undo["task_id"]}
        ).scalar_one()
        assert kind == "app.rollback"

    def test_a_first_deploy_has_nothing_to_roll_back_to(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        first = _submit(client, admin_headers, app["id"], _deploy_body(sid)).json()
        _mark_succeeded(db, first["id"])

        resp = client.post(
            f"/api/v1/deployments/{first['id']}/rollback",
            headers=admin_headers,
            json={"targets": [{"server_id": sid}], "confirm": True},
        )
        assert resp.status_code == 422, resp.text

    def test_a_live_deployment_may_not_be_rolled_back(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        first = _submit(client, admin_headers, app["id"], _deploy_body(sid)).json()
        # Still PENDING: the worker has not finished, so the link is unknown.
        resp = client.post(
            f"/api/v1/deployments/{first['id']}/rollback",
            headers=admin_headers,
            json={"targets": [{"server_id": sid}], "confirm": True},
        )
        assert resp.status_code == 409, resp.text

    def test_a_rollback_inherits_the_non_secret_config(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        """A rollback rewrites the env file, so it must not wipe it."""
        _app_id, target = self._deploy_then_succeed(client, admin_headers, db)
        undo = client.post(
            f"/api/v1/deployments/{target['id']}/rollback",
            headers=admin_headers,
            json={"targets": [{"server_id": 1}], "confirm": True},
        ).json()
        params = _task_params(db, undo["task_id"])
        assert params["deploy"]["config"] == {"LOG_LEVEL": "info"}
        assert params["deploy"]["module_name"] == "billing.main"

    def test_the_rollback_lock_respects_the_environment_mutex(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        _app_id, target = self._deploy_then_succeed(client, admin_headers, db)
        busy = _submit(
            client,
            admin_headers,
            target["application_id"],
            _deploy_body(1, release_version="9.9.9"),
        )
        assert busy.status_code == 202, busy.text

        resp = client.post(
            f"/api/v1/deployments/{target['id']}/rollback",
            headers=admin_headers,
            json={"targets": [{"server_id": 1}], "confirm": True},
        )
        assert resp.status_code == 409, resp.text


class TestIdempotency:
    def test_a_retried_submission_returns_the_original_deployment(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        headers = {**admin_headers, "Idempotency-Key": "deploy-1"}
        body = _deploy_body(sid)

        first = client.post(
            f"/api/v1/applications/{app['id']}/deploy", headers=headers, json=body
        )
        assert first.status_code == 202, first.text

        # Finish it so the mutex would otherwise allow a second.
        _mark_succeeded(db, first.json()["id"])

        again = client.post(
            f"/api/v1/applications/{app['id']}/deploy", headers=headers, json=body
        )
        assert again.status_code == 202, again.text
        assert again.json()["id"] == first.json()["id"]

        count: int = db.execute(text("SELECT COUNT(*) FROM ops_deployments")).scalar_one()
        assert count == 1


class TestPermissions:
    def test_a_viewer_may_read_but_not_deploy(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        viewer_headers: dict[str, str],
        db: Session,
    ) -> None:
        app, _env = _setup(client, admin_headers)
        sid = _make_server(db)
        _submit(client, admin_headers, app["id"], _deploy_body(sid))

        listed = client.get("/api/v1/deployments", headers=viewer_headers)
        assert listed.status_code == 200, listed.text
        assert len(listed.json()) == 1

        denied = _submit(client, viewer_headers, app["id"], _deploy_body(sid))
        assert denied.status_code == 403, denied.text

    def test_deploying_needs_authentication(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployments")
        assert resp.status_code == 401, resp.text
