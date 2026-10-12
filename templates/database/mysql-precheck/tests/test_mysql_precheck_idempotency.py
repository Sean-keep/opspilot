"""mysql_precheck is SAFE: running it twice must be indistinguishable from once."""

from __future__ import annotations

from shared.actions.base import spec_of
from shared.templates import LoadedTemplate
from templates.harness import RecordingPort, run_steps


def test_every_step_is_safe(loaded: LoadedTemplate) -> None:
    for step in loaded.spec.steps:
        assert spec_of(step.action).idempotency_class == "SAFE"


def test_a_rerun_issues_the_same_commands(loaded: LoadedTemplate) -> None:
    first = RecordingPort()
    second = RecordingPort()
    assert first.argvs() == []
    a = run_steps(loaded, first)
    b = run_steps(loaded, second)
    assert [argv for _, argv in a] == [argv for _, argv in b]
    assert first.argvs() == second.argvs()
    assert first.calls == second.calls  # stdin payloads too


def test_no_step_writes_a_file(loaded: LoadedTemplate) -> None:
    port = RecordingPort()
    run_steps(loaded, port)
    heads = {argv[0] for argv in port.argvs()}
    writers = {"dd", "tee", "install", "mv", "cp", "rm", "mkdir", "chown", "chmod"}
    assert not (heads & writers), sorted(heads & writers)


def test_no_step_carries_a_payload(loaded: LoadedTemplate) -> None:
    port = RecordingPort()
    run_steps(loaded, port)
    assert all(p is None for p in port.payloads())
