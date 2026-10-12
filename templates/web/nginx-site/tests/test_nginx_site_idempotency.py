"""nginx_site must converge on a second run.

Writing the same server block twice is the definition of idempotent: the file
is overwritten with identical bytes, and the only observable difference is a
fresh backup timestamp. What must *not* happen is a second run appending to
the config or spawning a duplicate vhost.
"""

from __future__ import annotations

from shared.actions.base import spec_of
from shared.templates import LoadedTemplate
from templates.harness import RecordingPort, run_steps


def test_no_step_is_destructive(loaded: LoadedTemplate) -> None:
    classes = {s.key: spec_of(s.action).idempotency_class for s in loaded.spec.steps}
    assert set(classes.values()) <= {"SAFE", "CONDITIONAL"}, classes


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
    # `dd of=…` truncates; `tee -a` or `cat >>` would accumulate a vhost per run.
    assert "dd of=" in joined
    assert "tee" not in joined
    assert ">>" not in joined


def test_a_previous_config_is_backed_up_before_the_write(loaded: LoadedTemplate) -> None:
    port = RecordingPort()
    run_steps(loaded, port)
    argvs = port.argvs()
    # `test -e` probes first; a backup copy is only taken when something exists.
    assert any(a and a[0] == "test" for a in argvs)
    assert any("cp" in a for a in argvs) or any("install" in a for a in argvs)


def test_the_reachability_check_only_makes_a_plain_http_request(loaded: LoadedTemplate) -> None:
    port = RecordingPort()
    run_steps(loaded, port)
    http_calls = [a for a in port.argvs() if a and a[0] == "curl"]
    assert http_calls, "http.check issued no request at all"
    for argv in http_calls:
        # Protocol pinning keeps a hostile redirect from walking to file:// or gopher://.
        assert "--proto" in argv
        assert "=http,https" in argv
