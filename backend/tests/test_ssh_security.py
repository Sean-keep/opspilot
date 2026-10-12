"""SSH transport security.

These lock down the three properties the security model depends on:

1. an argument containing shell metacharacters stays one inert argv token;
2. a host key is only accepted when its fingerprint matches the pin;
3. private key material exists on disk for one connection and no longer.
"""

from __future__ import annotations

import stat
from pathlib import Path

import paramiko
import pytest
from shared.executor.keys import staged_private_key
from shared.executor.port import ExecContext, HostKeyError, NotVerifiedError
from shared.executor.ssh import (
    SshExecPort,
    _PinnedHostKeyPolicy,
    quote_argv,
    ssh_fingerprint,
)


# --------------------------------------------------------------------------
# argv separation
# --------------------------------------------------------------------------
def test_quote_argv_keeps_metacharacters_inert() -> None:
    """A command-injection payload must survive as one quoted token."""
    import shlex

    payload = "hello; touch /tmp/opspilot-pwned"
    command = quote_argv(["echo", payload])

    # The ';' is inside a quoted word, so a shell will not treat it as a
    # separator. shlex.split is the ground truth: one token in, one token out.
    assert command == "echo 'hello; touch /tmp/opspilot-pwned'"
    assert shlex.split(command) == ["echo", payload]

    # And the same for the rest of the metacharacter zoo.
    for bad in ("$(id)", "`id`", "a && b", "a | b", "a > /etc/passwd", "a\nb"):
        assert shlex.split(quote_argv(["echo", bad])) == ["echo", bad]


def test_quote_argv_rejects_empty_argv() -> None:
    with pytest.raises(ValueError, match="empty"):
        quote_argv([])


def test_quote_argv_rejects_nul() -> None:
    with pytest.raises(ValueError, match="NUL"):
        quote_argv(["echo", "a\x00b"])


def test_quote_argv_rejects_non_string() -> None:
    with pytest.raises(TypeError, match="str"):
        quote_argv(["echo", 42])  # type: ignore[list-item]


def test_quote_argv_quotes_command_substitution() -> None:
    for payload in ("$(id)", "`id`", "a\nb", "a|b", "a&&b", "a > /etc/passwd"):
        command = quote_argv(["echo", payload])
        import shlex

        assert shlex.split(command) == ["echo", payload]


def test_quote_argv_preserves_empty_string_argument() -> None:
    import shlex

    command = quote_argv(["echo", ""])
    assert shlex.split(command) == ["echo", ""]


# --------------------------------------------------------------------------
# Host key pinning
# --------------------------------------------------------------------------
def test_ssh_fingerprint_matches_openssh_format() -> None:
    """Same shape as ``ssh-keygen -lf``: SHA256 + unpadded base64."""
    key = paramiko.ECDSAKey.generate()
    fingerprint = ssh_fingerprint(key)
    assert fingerprint.startswith("SHA256:")
    body = fingerprint[len("SHA256:") :]
    assert "=" not in body
    # sha256 -> 32 bytes -> base64 without padding is 43 chars.
    assert len(body) == 43


def test_unknown_host_is_refused_not_auto_added() -> None:
    """The policy must never trust a first sighting. No AutoAddPolicy anywhere."""
    policy = _PinnedHostKeyPolicy("")
    client = paramiko.SSHClient()
    key = paramiko.ECDSAKey.generate()

    with pytest.raises(NotVerifiedError, match="must confirm"):
        policy.missing_host_key(client, "example.test", key)

    # Nothing was added to the client's host key store.
    assert not client.get_host_keys().lookup("example.test")


def test_mismatched_host_key_is_refused() -> None:
    policy = _PinnedHostKeyPolicy("SHA256:" + "A" * 43)
    client = paramiko.SSHClient()
    key = paramiko.ECDSAKey.generate()

    with pytest.raises(HostKeyError, match="mismatch"):
        policy.missing_host_key(client, "example.test", key)
    assert not client.get_host_keys().lookup("example.test")


def test_matching_host_key_is_recorded_for_this_connection_only() -> None:
    key = paramiko.ECDSAKey.generate()
    fingerprint = ssh_fingerprint(key)
    policy = _PinnedHostKeyPolicy(fingerprint)
    client = paramiko.SSHClient()

    policy.missing_host_key(client, "example.test", key)
    # Added only after the constant-time comparison succeeded.
    assert client.get_host_keys().lookup("example.test") is not None
    assert policy.offered_fingerprint == fingerprint


def test_unpinned_context_refuses_to_run() -> None:
    """SshExecPort must refuse a command when the context has no pin.

    The refusal happens *before* any socket is opened — an untrusted host must
    not even be probed.
    """
    port = SshExecPort(connect_timeout=1, default_timeout=1)
    ctx = ExecContext(host="127.0.0.1", port=1, user="nobody", host_key_fingerprint="")
    with pytest.raises(NotVerifiedError):
        port.run(ctx, ["true"])


def test_no_autopolicy_is_ever_instantiated() -> None:
    """``AutoAddPolicy`` must never be *used* — mentioning it in prose is fine.

    Checked via the AST so a docstring that explains the rule is allowed while
    any actual reference to the class is not.
    """
    import ast

    root = Path(__file__).resolve().parents[2] / "shared" / "executor"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Name | ast.Attribute):
                continue
            ident = getattr(node, "id", None) or getattr(node, "attr", None)
            if ident == "AutoAddPolicy":
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"AutoAddPolicy referenced in code: {offenders}"


# --------------------------------------------------------------------------
# Private key staging
# --------------------------------------------------------------------------
def test_staged_private_key_is_mode_600_and_removed() -> None:
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----"
    with staged_private_key(secret) as path_str:
        path = Path(path_str)
        assert path.is_file()
        assert path.read_text(encoding="utf-8").startswith("-----BEGIN")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"
        # Not world/group readable while it exists.
        assert not (path.stat().st_mode & 0o077)
    assert not path.exists(), "staged key must be unlinked after the block"


def test_staged_private_key_removed_on_error() -> None:
    seen: list[str] = []
    with pytest.raises(RuntimeError), staged_private_key("secret-material") as path_str:
        seen.append(path_str)
        raise RuntimeError("boom")
    assert seen and not Path(seen[0]).exists()


def test_staged_private_key_rejects_empty_and_nul() -> None:
    with pytest.raises(ValueError, match="empty"), staged_private_key(""):
        pass
    with pytest.raises(ValueError, match="NUL"), staged_private_key("a\x00b"):
        pass


def test_staged_key_name_does_not_leak_the_secret() -> None:
    with staged_private_key("super-secret-key-material") as path_str:
        assert "super-secret" not in Path(path_str).name
        assert "super-secret" not in path_str


# --------------------------------------------------------------------------
# Context never carries plaintext
# --------------------------------------------------------------------------
def test_exec_context_has_no_plaintext_field() -> None:
    """The transport object is not a place to stash secrets."""
    ctx = ExecContext(host="h", port=22, user="u", credential_id=7)
    assert "password" not in ctx.extra
    assert "private_key" not in ctx.extra
    # Repr must not grow a secret slot later.
    assert "secret" not in repr(ctx)
    assert "password" not in repr(ctx)
