"""Credential API.

The central assertion of this file: a credential's plaintext is accepted on
write and is never returned, logged, or audited afterwards. If a response body
ever contains the secret, these tests fail.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.credentials.models import Credential
from backend.app.credentials.service import CredentialService, public_fingerprint

SECRET = "-----BEGIN OPENSSH PRIVATE KEY-----\nPRIVATE-KEY-MATERIAL-12345\n-----END-----"
SECRET_TWO = "-----BEGIN OPENSSH PRIVATE KEY-----\nROTATED-MATERIAL-99999\n-----END-----"


def _create(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str = "deploy-key",
    secret: str = SECRET,
    kind: str = "ssh_key",
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/credentials",
        headers=headers,
        json={"name": name, "kind": kind, "secret": secret, "description": "d"},
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


# --------------------------------------------------------------------------
# The secret must not come back
# --------------------------------------------------------------------------
def test_create_never_echoes_the_secret(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    body = _create(client, admin_headers)
    # The whole response, as text, must be free of the payload.
    listed = client.get("/api/v1/credentials", headers=admin_headers)
    single = client.get(f"/api/v1/credentials/{body['id']}", headers=admin_headers)

    for raw in (str(body), listed.text, single.text):
        assert "PRIVATE-KEY-MATERIAL-12345" not in raw
        assert "payload_encrypted" not in raw
        assert "BEGIN OPENSSH" not in raw

    # What it does return:
    assert body["name"] == "deploy-key"
    assert body["kind"] == "ssh_key"
    assert body["secret_ref"] == f"cred:{body['id']}"
    assert body["status"] == "active"
    assert body["fingerprint"].startswith("SHA256:")


def test_payload_is_stored_encrypted_not_plaintext(
    client: TestClient, admin_headers: dict[str, str], db: Session
) -> None:
    body = _create(client, admin_headers)
    row = db.get(Credential, int(body["id"]))
    assert row is not None
    assert row.payload_encrypted != SECRET
    assert "PRIVATE-KEY-MATERIAL-12345" not in row.payload_encrypted
    # Fernet ciphertext is urlsafe base64-ish and much longer than the input
    # would be if it were stored raw next to a marker.
    assert row.payload_encrypted.startswith("gAAAA")


def test_fingerprint_is_a_digest_not_the_material() -> None:
    fp = public_fingerprint(SECRET)
    assert fp.startswith("SHA256:")
    assert "PRIVATE-KEY-MATERIAL" not in fp
    # Stable, and different inputs give different fingerprints.
    assert public_fingerprint(SECRET) == fp
    assert public_fingerprint(SECRET_TWO) != fp


def test_rotate_changes_the_payload_but_not_the_ref(
    client: TestClient, admin_headers: dict[str, str], db: Session
) -> None:
    body = _create(client, admin_headers)
    cred_id = int(body["id"])

    rotated = client.post(
        f"/api/v1/credentials/{cred_id}/rotate",
        headers=admin_headers,
        json={"secret": SECRET_TWO},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["secret_ref"] == body["secret_ref"]
    assert rotated.json()["fingerprint"] != body["fingerprint"]
    assert "ROTATED-MATERIAL-99999" not in rotated.text

    row = db.get(Credential, cred_id)
    assert row is not None
    assert "ROTATED-MATERIAL-99999" not in row.payload_encrypted

    # The service can still decrypt what was just written.
    svc = CredentialService(db)
    assert svc.decrypt(cred_id) == SECRET_TWO


def test_retire_is_soft(
    client: TestClient, admin_headers: dict[str, str], db: Session
) -> None:
    body = _create(client, admin_headers)
    cred_id = int(body["id"])

    retired = client.post(f"/api/v1/credentials/{cred_id}/retire", headers=admin_headers)
    assert retired.status_code == 200
    assert retired.json()["status"] == "retired"

    listed = client.get("/api/v1/credentials", headers=admin_headers)
    assert [c["id"] for c in listed.json()] == []

    with_retired = client.get(
        "/api/v1/credentials", headers=admin_headers, params={"include_retired": True}
    )
    assert [c["id"] for c in with_retired.json()] == [cred_id]

    row = db.get(Credential, cred_id)
    assert row is not None, "retire must not delete the row"


# --------------------------------------------------------------------------
# Audit must not carry the payload either
# --------------------------------------------------------------------------
def test_audit_snapshot_has_no_secret(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    _create(client, admin_headers, name="audited-key")
    audit = client.get("/api/v1/audit-logs", headers=admin_headers)
    assert "PRIVATE-KEY-MATERIAL-12345" not in audit.text
    assert "payload_encrypted" not in audit.text

    created = [r for r in audit.json() if r["action"] == "credential.create"]
    assert created, "credential creation must be audited"
    snap = created[0].get("snapshot") or {}
    assert set(snap) <= {"name", "kind", "secret_ref"}
    assert "fingerprint" not in snap, "a secret-derived digest is not audit material"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def test_unknown_kind_is_rejected(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/credentials",
        headers=admin_headers,
        json={"name": "x", "kind": "root_password", "secret": "s"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    fields = body.get("details", {}).get("fields", [])
    assert any("kind" in str(f) for f in fields), body


def test_empty_secret_is_rejected(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/credentials",
        headers=admin_headers,
        json={"name": "x", "kind": "ssh_key", "secret": ""},
    )
    assert resp.status_code == 422


def test_duplicate_name_is_rejected(client: TestClient, admin_headers: dict[str, str]) -> None:
    _create(client, admin_headers, name="dup")
    resp = client.post(
        "/api/v1/credentials",
        headers=admin_headers,
        json={"name": "dup", "kind": "ssh_key", "secret": "s"},
    )
    assert resp.status_code == 422
    assert "already taken" in resp.json()["message"]


def test_missing_credential_is_404(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    resp = client.get("/api/v1/credentials/999999", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


# --------------------------------------------------------------------------
# RBAC
# --------------------------------------------------------------------------
def test_viewer_can_read_but_not_write(
    client: TestClient, viewer_headers: dict[str, str], admin_headers: dict[str, str]
) -> None:
    _create(client, admin_headers)

    listed = client.get("/api/v1/credentials", headers=viewer_headers)
    assert listed.status_code == 200
    assert "PRIVATE-KEY-MATERIAL-12345" not in listed.text

    created = client.post(
        "/api/v1/credentials",
        headers=viewer_headers,
        json={"name": "nope", "kind": "ssh_key", "secret": "s"},
    )
    assert created.status_code == 403
    assert created.json()["code"] == "forbidden"

    rotated = client.post(
        "/api/v1/credentials/1/rotate", headers=viewer_headers, json={"secret": "s"}
    )
    assert rotated.status_code == 403

    retired = client.post("/api/v1/credentials/1/retire", headers=viewer_headers)
    assert retired.status_code == 403


def test_unauthenticated_is_401(client: TestClient) -> None:
    resp = client.get("/api/v1/credentials")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthenticated"


def test_viewer_cannot_see_a_secret_even_with_the_id(
    client: TestClient, viewer_headers: dict[str, str], admin_headers: dict[str, str]
) -> None:
    body = _create(client, admin_headers)
    resp = client.get(f"/api/v1/credentials/{body['id']}", headers=viewer_headers)
    assert resp.status_code == 200
    assert "PRIVATE-KEY-MATERIAL-12345" not in resp.text


# --------------------------------------------------------------------------
# Decryption stays out of the HTTP surface
# --------------------------------------------------------------------------
def test_no_route_returns_decrypted_material(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """The OpenAPI surface must not even describe a secret-returning endpoint."""
    schema = client.get("/openapi.json").json()
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            for status, resp in (op.get("responses") or {}).items():
                if not str(status).startswith("2"):
                    continue
                content = resp.get("content") or {}
                for media in content.values():
                    ref = (media.get("schema") or {}).get("$ref", "")
                    if "Credential" in ref:
                        assert "Out" in ref, f"{method.upper()} {path} returns {ref}"
                        assert "Secret" not in ref and "Private" not in ref

    # And the concrete schema has no payload field at all.
    cred_out = schema["components"]["schemas"]["CredentialOut"]["properties"]
    assert "payload_encrypted" not in cred_out
    assert "secret" not in cred_out
    assert "private_key" not in cred_out


@pytest.mark.parametrize(
    "field", ["payload_encrypted", "secret", "private_key", "password"]
)
def test_response_models_have_no_secret_field(field: str) -> None:
    from backend.app.credentials.schemas import CredentialOut

    assert field not in CredentialOut.model_fields
