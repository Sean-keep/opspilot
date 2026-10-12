"""supervisor_program: the config converges, the restart is deliberate.

Writing the same ini twice is idempotent. Restarting the program is not — it
drops in-flight work — so it is the single DESTRUCTIVE step and the reason this
template is never auto-retried after a lease expiry.
"""

from __future__ import annotations

from shared.actions.base import spec_of
from shared.templates import LoadedTemplate
from templates.harness import RecordingPort, run_steps


def test_only_the_restart_is_destructive(loaded: LoadedTemplate) -> None:
    classes = {s.key: spec_of(s.action).idempotency_class for s in loaded.spec.steps}
    assert {k for k, v in classes.items() if v == "DESTRUCTIVE"} == {"restart_program"}


def test_a_rerun_issues_the_same_commands(loaded: LoadedTemplate) -> None:
    first = RecordingPort()
    second = RecordingPort()
    a = run_steps(loaded, first)
    b = run_steps(loaded, second)
    assert [argv for _, argv in a] == [argv for _, argv in b]
    assert first.argvs() == second.argvs()


def test_the_config_is_replaced_never_appended(loaded: LoadedTemplate) -> None:
    port = RecordingPort()
    run_steps(loaded, port)
    joined = port.joined()
    assert "dd of=" in joined
    assert ">>" not in joined


def test_a_previous_config_is_backed_up_before_the_write(loaded: LoadedTemplate) -> None:
    port = RecordingPort()
    run_steps(loaded, port)
    argvs = port.argvs()
    assert any(a and a[0] == "test" for a in argvs)
    assert any(a and a[0] in {"cp", "install"} for a in argvs)


def test_the_restart_only_touches_the_declared_program(loaded: LoadedTemplate) -> None:
    port = RecordingPort()
    run_steps(loaded, port)
    # The command is reached through the restricted sudo rule, so it is not
    # argv[0] — look for it anywhere in the token list.
    restarts = [a for a in port.argvs() if a and "/usr/bin/supervisorctl" in a]
    assert restarts
    for argv in restarts:
        assert "opspilot-demo" in argv
        assert "all" not in argv, "restarting every program is a DoS primitive"
