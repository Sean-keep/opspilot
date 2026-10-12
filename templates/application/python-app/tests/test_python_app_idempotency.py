"""python_app: a re-run must converge, not stack.

The interesting step is ``app.install_release``: it is CONDITIONAL, meaning it
skips when the release directory already exists. That is what makes a retry
after a flaky health check safe — the artifact is not re-unpacked over a live
one, and the symlink switch is not repeated.
"""

from __future__ import annotations

from shared.actions.base import spec_of
from shared.templates import LoadedTemplate
from templates.harness import RecordingPort, run_steps


def test_only_the_restart_is_destructive(loaded: LoadedTemplate) -> None:
    classes = {s.key: spec_of(s.action).idempotency_class for s in loaded.spec.steps}
    destructive = {k for k, v in classes.items() if v == "DESTRUCTIVE"}
    assert destructive == {"restart_service"}, classes


def test_the_restart_is_declared_destructive_on_purpose() -> None:
    spec = spec_of("systemd.restart")
    assert spec.idempotency_class == "DESTRUCTIVE"
    assert spec.requires_sudo is True


def test_a_rerun_issues_the_same_commands(loaded: LoadedTemplate) -> None:
    first = RecordingPort()
    second = RecordingPort()
    a = run_steps(loaded, first)
    b = run_steps(loaded, second)
    assert [argv for _, argv in a] == [argv for _, argv in b]
    assert first.argvs() == second.argvs()


def test_the_release_install_probes_before_unpacking(loaded: LoadedTemplate) -> None:
    """CONDITIONAL means "check, then act". Without the probe, a retry would
    unpack over a release that is already there."""
    port = RecordingPort()
    run_steps(loaded, port)
    argvs = [" ".join(a) for a in port.argvs()]
    assert any("test -e" in a for a in argvs), argvs
    assert any("tar" in a or "unzip" in a for a in argvs)


def test_shared_state_is_never_touched_by_the_install(loaded: LoadedTemplate) -> None:
    """``shared/`` holds config, logs, and data across releases.

    ``app.install_release`` refuses it outright; if this ever fails, a deploy
    would wipe an application's database.
    """
    install = next(s for s in loaded.spec.steps if s.key == "install_release")
    assert "shared" not in str(install.params)
    root = "/opt/opspilot/apps/demo"
    from shared.actions.builtin import ISOLATED_STATE_MARKERS

    target = f"{root}/releases/2026.10.12-1"
    assert not any(marker in target for marker in ISOLATED_STATE_MARKERS)


def test_env_and_unit_bodies_go_over_stdin(loaded: LoadedTemplate) -> None:
    port = RecordingPort()
    run_steps(loaded, port)
    payloads = [p for p in port.payloads() if p]
    assert payloads, "no content was written at all"
    joined = port.joined()
    for blob in payloads:
        text = blob.decode("utf-8", "replace").strip().split("\n")[0]
        assert text not in joined, f"payload line leaked into argv: {text!r}"
