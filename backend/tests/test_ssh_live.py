"""Live SSH transport tests against the local ``opstest`` target.

These are the only tests that open a socket. They skip when
``OPSPILOT_TEST_SSH_*`` is not configured, so the suite stays honest rather
than mocking a connection green.

The target is the disposable account created by
``scripts/setup-ssh-test-target.sh``: non-root, own key, no sudo needed for the
read-only checks. Nothing here is ever pointed at a production host.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.app.executor.keys import staged_private_key
from backend.app.executor.port import (
    ExecContext,
    HostKeyError,
    NotVerifiedError,
    TransportError,
)
from backend.app.executor.ssh import SshExecPort, discover_host_key
from backend.app.inventory.checks import collect_facts
from backend.tests.conftest import SshTarget


@pytest.fixture()
def pinned(ssh_target: SshTarget) -> tuple[str, int, str, str]:
    """(host, port, user, fingerprint) with the fingerprint already trusted."""
    discovered = discover_host_key(ssh_target.host, ssh_target.port)
    return ssh_target.host, ssh_target.port, ssh_target.user, discovered["fingerprint"]


def _ctx(
    target: SshTarget,
    fingerprint: str,
    key_path: str,
) -> ExecContext:
    return ExecContext(
        host=target.host,
        port=target.port,
        user=target.user,
        timeout=30,
        host_key_fingerprint=fingerprint,
        extra={"key_filename": key_path},
    )


def test_discover_matches_openssh_fingerprint(ssh_target: SshTarget) -> None:
    found = discover_host_key(ssh_target.host, ssh_target.port)
    assert found["algorithm"] in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256")
    assert found["fingerprint"].startswith("SHA256:")

    # Must agree with OpenSSH's own fingerprint of the host key.
    out = subprocess.run(
        ["ssh-keygen", "-lf", "/etc/ssh/ssh_host_ed25519_key.pub"],
        capture_output=True,
        text=True,
        check=True,
    )
    openssh_fp = out.stdout.split()[1]
    assert found["fingerprint"] == openssh_fp, (found["fingerprint"], openssh_fp)


def test_real_command_runs_and_stays_argv_separated(
    ssh_target: SshTarget, pinned: tuple[str, int, str, str]
) -> None:
    _host, _port, _user, fingerprint = pinned
    port_impl = SshExecPort(connect_timeout=10, default_timeout=30)

    marker = "/tmp/opspilot-pwned-should-not-exist"
    payload = f"hello; touch {marker}"

    with staged_private_key(ssh_target.read_private_key()) as key_path:
        result = port_impl.run(
            _ctx(ssh_target, fingerprint, key_path), ["echo", payload]
        )

    assert result.exit_code == 0
    # The payload came back as one argument — the shell did not split it.
    assert result.stdout.strip() == payload
    assert not Path(marker).exists(), "command injection succeeded — this is a failure"


def test_unverified_host_key_refuses_a_real_connection(ssh_target: SshTarget) -> None:
    port_impl = SshExecPort(connect_timeout=10, default_timeout=30)
    with (
        staged_private_key(ssh_target.read_private_key()) as key_path,
        pytest.raises(NotVerifiedError),
    ):
        port_impl.run(_ctx(ssh_target, "", key_path), ["true"])


def test_wrong_pinned_fingerprint_refuses_a_real_connection(
    ssh_target: SshTarget,
) -> None:
    port_impl = SshExecPort(connect_timeout=10, default_timeout=30)
    with (
        staged_private_key(ssh_target.read_private_key()) as key_path,
        pytest.raises((HostKeyError, TransportError)),
    ):
        port_impl.run(_ctx(ssh_target, "SHA256:" + "X" * 43, key_path), ["true"])


def test_collect_facts_returns_real_system_information(
    ssh_target: SshTarget, pinned: tuple[str, int, str, str]
) -> None:
    _host, _port, _user, fingerprint = pinned
    port_impl = SshExecPort(connect_timeout=10, default_timeout=60)

    with staged_private_key(ssh_target.read_private_key()) as key_path:
        facts = collect_facts(port_impl, _ctx(ssh_target, fingerprint, key_path))

    assert facts.os_name == "ubuntu"
    assert facts.arch in ("x86_64", "aarch64")
    assert facts.cpu_count >= 1
    assert facts.memory_total_mb > 0
    assert facts.disk_total_gb > 0
    # No secret material in the raw snapshot.
    assert "PRIVATE" not in str(facts.raw)


def test_unreachable_host_reports_a_transport_error() -> None:
    with pytest.raises(TransportError):
        discover_host_key("127.0.0.1", 1, connect_timeout=1)


def test_non_root_ssh_user_is_the_sane_default(ssh_target: SshTarget) -> None:
    """The platform must never be configured to log in as root by default."""
    assert ssh_target.user != "root"
    assert ssh_target.user != ""
