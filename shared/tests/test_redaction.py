"""Redaction must never leak secrets — these tests are the lock on that rule."""

from __future__ import annotations

import pytest

from shared.redaction import REDACTED, is_sensitive_key, redact, redact_text


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Password",
        "PASSWORD",
        "passwd",
        "token",
        "access_token",
        "accessToken",
        "private_key",
        "privateKey",
        "PRIVATE-KEY",
        "api_key",
        "apikey",
        "authorization",
        "secret",
        "secret_ref",
        "ops_db_password",
        "telegram_bot_token",
        "master_key",
        "ssh_password",
    ],
)
def test_sensitive_keys_are_detected(key: str) -> None:
    assert is_sensitive_key(key), key


@pytest.mark.parametrize("key", ["username", "host", "port", "app_name", "status", "id", "name"])
def test_normal_keys_are_not_flagged(key: str) -> None:
    assert not is_sensitive_key(key), key


def test_nested_dict_is_redacted() -> None:
    payload = {
        "username": "alice",
        "password": "hunter2",
        "nested": {"api_key": "sk-abc", "port": 22},
        "items": [{"token": "t1"}, {"ok": 1}],
    }
    out = redact(payload)

    assert out["username"] == "alice"
    assert out["password"] == REDACTED
    assert out["nested"]["api_key"] == REDACTED
    assert out["nested"]["port"] == 22
    assert out["items"][0]["token"] == REDACTED
    assert out["items"][1]["ok"] == 1


def test_key_normalisation_catches_camel_and_kebab() -> None:
    out = redact({"privateKey": "x", "private-key": "y", "private_key": "z"})
    assert set(out.values()) == {REDACTED}


def test_pem_blob_in_an_innocent_key_is_redacted() -> None:
    out = redact({"comment": "-----BEGIN PRIVATE KEY-----\nabc\n-----END"})
    assert out["comment"] == REDACTED


def test_lists_and_tuples_are_walked() -> None:
    out = redact([{"password": "p"}, {"password": "q"}])
    assert out == [{"password": REDACTED}, {"password": REDACTED}]

    out_tuple = redact(({"secret": "s"}, 1))
    assert out_tuple == ({"secret": REDACTED}, 1)


def test_scalars_pass_through() -> None:
    assert redact(None) is None
    assert redact(3) == 3
    assert redact(3.5) == 3.5
    assert redact(True) is True
    assert redact("hello") == "hello"


def test_dataclass_like_object_is_walked() -> None:
    class Cred:
        def __init__(self) -> None:
            self.name = "deploy"
            self.private_key = "KEYMATERIAL"

    out = redact(Cred())
    assert out["name"] == "deploy"
    assert out["private_key"] == REDACTED


def test_deep_nesting_does_not_recurse_forever() -> None:
    node: dict[str, object] = {}
    cursor = node
    for _ in range(100):
        nxt: dict[str, object] = {}
        cursor["child"] = nxt
        cursor = nxt
    cursor["password"] = "deep"

    out = redact(node)  # must not raise RecursionError
    assert out is not None


def test_redact_is_not_a_mutation_of_the_input() -> None:
    original = {"password": "hunter2", "username": "bob"}
    redact(original)
    assert original == {"password": "hunter2", "username": "bob"}


def test_redact_text_masks_inline_assignments() -> None:
    line = "connecting as bob password=hunter2 token=abc123 done"
    out = redact_text(line)
    assert "hunter2" not in out
    assert "abc123" not in out
    assert "connecting as bob" in out


def test_redact_text_masks_authorization_header() -> None:
    out = redact_text("Authorization: Bearer eyJhbGciOi")
    assert "eyJhbGciOi" not in out
    assert REDACTED in out


def test_redact_text_is_a_no_op_without_secrets() -> None:
    msg = "installed python3.12 in 4s"
    assert redact_text(msg) == msg
