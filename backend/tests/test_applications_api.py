"""Application and environment API contract.

The rules worth locking down here are the ones that would otherwise be
discovered on a host: an application name becomes a directory under
``/opt/opspilot/apps/``, a unit path has to be a real unit, a service name has
to be on the restart whitelist, and an application is never deletable over
HTTP because deployment records point at it.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _app_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "billing-api",
        "runtime_type": "python",
        "run_user": "billing",
        "description": "invoice service",
    }
    body.update(overrides)
    return body


def _env_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "production",
        "service_name": "opspilot-billing",
        "unit_path": "/etc/systemd/system/opspilot-billing.service",
        "health_url": "http://127.0.0.1:8000/healthz",
        "health_timeout_s": 10,
        "keep_releases": 5,
    }
    body.update(overrides)
    return body


def _create_app(client: TestClient, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    resp = client.post("/api/v1/applications", headers=headers, json=_app_body(**overrides))
    assert resp.status_code == 201, resp.text
    created: dict[str, Any] = resp.json()
    return created


def _refused_field(resp: Any) -> str:
    """The body field a 422 names, for asserting *which* rule fired.

    The API's validation envelope carries field paths, not validator prose —
    which is deliberate, since a validator's message can quote the value it
    just rejected. Assert on the field, not on the wording.
    """
    assert resp.status_code == 422, resp.text
    fields = resp.json().get("details", {}).get("fields", [])
    assert fields, resp.text
    return " ".join(fields)


# --------------------------------------------------------------------------
class TestApplicationCreate:
    def test_create_returns_the_application_with_no_environments(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = _create_app(client, admin_headers)
        assert body["name"] == "billing-api"
        assert body["run_user"] == "billing"
        assert body["environments"] == []

    def test_create_is_201_and_a_second_create_conflicts(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        _create_app(client, admin_headers)
        resp = client.post(
            "/api/v1/applications", headers=admin_headers, json=_app_body()
        )
        assert resp.status_code == 409, resp.text

    def test_a_reserved_layout_name_is_refused(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        # An app named 'current' would make current -> releases/<id> unstatable.
        resp = client.post(
            "/api/v1/applications",
            headers=admin_headers,
            json=_app_body(name="current"),
        )
        assert "name" in _refused_field(resp)

    def test_a_name_is_normalised_to_a_lowercase_slug(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        # The name becomes a directory, so the validator folds case rather than
        # refusing: 'Billing-API' and 'billing-api' are the same application.
        body = _create_app(client, admin_headers, name="Billing-API")
        assert body["name"] == "billing-api"

    def test_a_name_that_is_not_a_directory_slug_is_refused(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        for bad in ("1st-app", "../escape", "app/../etc", "a" * 65, ""):
            resp = client.post(
                "/api/v1/applications",
                headers=admin_headers,
                json=_app_body(name=bad),
            )
            assert "name" in _refused_field(resp), bad

    def test_run_user_may_not_be_root(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        resp = client.post(
            "/api/v1/applications",
            headers=admin_headers,
            json=_app_body(run_user="root"),
        )
        assert "run_user" in _refused_field(resp)

    def test_an_unknown_runtime_is_refused(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        resp = client.post(
            "/api/v1/applications",
            headers=admin_headers,
            json=_app_body(runtime_type="ruby"),
        )
        assert resp.status_code == 422, resp.text


class TestApplicationUpdate:
    def test_patch_changes_only_what_was_sent(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        created = _create_app(client, admin_headers)
        resp = client.patch(
            f"/api/v1/applications/{created['id']}",
            headers=admin_headers,
            json={"description": "invoices, revised"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["description"] == "invoices, revised"
        assert body["name"] == "billing-api"
        assert body["run_user"] == "billing"

    def test_patch_cannot_take_the_run_user_to_root(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        created = _create_app(client, admin_headers)
        resp = client.patch(
            f"/api/v1/applications/{created['id']}",
            headers=admin_headers,
            json={"run_user": "root"},
        )
        assert resp.status_code == 422, resp.text

    def test_patch_of_a_missing_application_is_404(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        resp = client.patch(
            "/api/v1/applications/99999", headers=admin_headers, json={"description": "x"}
        )
        assert resp.status_code == 404, resp.text


class TestEnvironments:
    def test_create_environment_and_see_it_on_the_application(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        app = _create_app(client, admin_headers)
        resp = client.post(
            f"/api/v1/applications/{app['id']}/environments",
            headers=admin_headers,
            json=_env_body(),
        )
        assert resp.status_code == 201, resp.text
        env = resp.json()
        assert env["name"] == "production"
        assert env["service_name"] == "opspilot-billing"

        listed = client.get(f"/api/v1/applications/{app['id']}", headers=admin_headers)
        assert listed.status_code == 200
        assert [e["name"] for e in listed.json()["environments"]] == ["production"]

    def test_duplicate_environment_name_conflicts(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        app = _create_app(client, admin_headers)
        first = client.post(
            f"/api/v1/applications/{app['id']}/environments",
            headers=admin_headers,
            json=_env_body(),
        )
        assert first.status_code == 201
        second = client.post(
            f"/api/v1/applications/{app['id']}/environments",
            headers=admin_headers,
            json=_env_body(),
        )
        assert second.status_code == 409, second.text

    def test_an_unknown_environment_name_is_refused(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        app = _create_app(client, admin_headers)
        resp = client.post(
            f"/api/v1/applications/{app['id']}/environments",
            headers=admin_headers,
            json=_env_body(name="qa"),
        )
        assert resp.status_code == 422, resp.text

    def test_a_service_outside_the_restart_whitelist_is_refused(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        app = _create_app(client, admin_headers)
        resp = client.post(
            f"/api/v1/applications/{app['id']}/environments",
            headers=admin_headers,
            json=_env_body(service_name="sshd", unit_path="/etc/systemd/system/sshd.service"),
        )
        assert "service_name" in _refused_field(resp)

    def test_a_unit_path_outside_systemd_is_refused(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        app = _create_app(client, admin_headers)
        for path in (
            "/tmp/evil.service",
            "/etc/systemd/system/../system/evil.service",
            "/etc/systemd/system/units/nested.service",
            "/etc/systemd/system/opspilot-billing.unit",
        ):
            resp = client.post(
                f"/api/v1/applications/{app['id']}/environments",
                headers=admin_headers,
                json=_env_body(unit_path=path),
            )
            assert resp.status_code == 422, f"{path} -> {resp.status_code}"

    def test_service_and_unit_must_come_together(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        app = _create_app(client, admin_headers)
        resp = client.post(
            f"/api/v1/applications/{app['id']}/environments",
            headers=admin_headers,
            json=_env_body(unit_path=""),
        )
        assert resp.status_code == 422, resp.text

    def test_config_ref_must_look_like_a_credential(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        app = _create_app(client, admin_headers)
        for ref in ("cred:", "secret:1", "/etc/passwd", "cred:abc"):
            resp = client.post(
                f"/api/v1/applications/{app['id']}/environments",
                headers=admin_headers,
                json=_env_body(config_ref=ref),
            )
            assert resp.status_code == 422, f"{ref} -> {resp.status_code}"

        resp = client.post(
            f"/api/v1/applications/{app['id']}/environments",
            headers=admin_headers,
            json=_env_body(config_ref="cred:7"),
        )
        assert resp.status_code == 201, resp.text

    def test_health_url_is_ssrf_checked(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        app = _create_app(client, admin_headers)
        for url in (
            "file:///etc/passwd",
            "http://169.254.169.254/latest/meta-data/",
            "http://user:pass@example.com/health",
        ):
            resp = client.post(
                f"/api/v1/applications/{app['id']}/environments",
                headers=admin_headers,
                json=_env_body(health_url=url),
            )
            assert resp.status_code == 422, f"{url} -> {resp.status_code}"

    def test_update_cannot_rename_an_environment(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        app = _create_app(client, admin_headers)
        created = client.post(
            f"/api/v1/applications/{app['id']}/environments",
            headers=admin_headers,
            json=_env_body(),
        )
        env_id = created.json()["id"]

        resp = client.patch(
            f"/api/v1/applications/{app['id']}/environments/{env_id}",
            headers=admin_headers,
            json={"name": "staging"},
        )
        # A rename is not a field on the update model, so it is ignored rather
        # than accepted — history keeps referring to 'production'.
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "production"

        resp = client.patch(
            f"/api/v1/applications/{app['id']}/environments/{env_id}",
            headers=admin_headers,
            json={"keep_releases": 3},
        )
        assert resp.status_code == 200
        assert resp.json()["keep_releases"] == 3

    def test_environment_under_the_wrong_parent_is_404(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        first = _create_app(client, admin_headers)
        second = _create_app(client, admin_headers, name="other-app")
        created = client.post(
            f"/api/v1/applications/{first['id']}/environments",
            headers=admin_headers,
            json=_env_body(),
        )
        env_id = created.json()["id"]

        resp = client.patch(
            f"/api/v1/applications/{second['id']}/environments/{env_id}",
            headers=admin_headers,
            json={"keep_releases": 2},
        )
        assert resp.status_code == 404, resp.text


class TestApplicationLifecycle:
    def test_an_application_is_not_deletable(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        app = _create_app(client, admin_headers)
        resp = client.delete(f"/api/v1/applications/{app['id']}", headers=admin_headers)
        assert resp.status_code in (404, 405), resp.text
        assert client.get(
            f"/api/v1/applications/{app['id']}", headers=admin_headers
        ).status_code == 200

    def test_reading_applications_needs_permission(
        self, client: TestClient, viewer_headers: dict[str, str]
    ) -> None:
        resp = client.get("/api/v1/applications", headers=viewer_headers)
        # The viewer role reads but does not create.
        assert resp.status_code == 200, resp.text
        denied = client.post(
            "/api/v1/applications", headers=viewer_headers, json=_app_body(name="nope")
        )
        assert denied.status_code == 403, denied.text

    def test_unauthenticated_is_401(self, client: TestClient) -> None:
        resp = client.get("/api/v1/applications")
        assert resp.status_code == 401, resp.text
