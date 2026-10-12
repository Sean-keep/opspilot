"""python312 must be safe to run twice.

This template is pure provisioning: nothing it does is destructive, so a
re-run has to converge rather than compound. The two properties under test:

* every step is ``SAFE`` or ``CONDITIONAL`` — no ``DESTRUCTIVE`` action appears;
* rendering the same inputs twice yields the same argv, so a retry cannot
  drift into a different command just because time passed.
"""

from __future__ import annotations

from shared.actions.base import spec_of
from shared.templates import LoadedTemplate
from templates.harness import RecordingPort, run_steps


def test_no_step_is_destructive(loaded: LoadedTemplate) -> None:
    classes = {s.key: spec_of(s.action).idempotency_class for s in loaded.spec.steps}
    assert set(classes.values()) <= {"SAFE", "CONDITIONAL"}, classes


def test_read_only_steps_are_declared_safe(loaded: LoadedTemplate) -> None:
    for step in loaded.spec.steps:
        spec = spec_of(step.action)
        if step.key in ("check_os", "check_disk"):
            assert spec.safe is True
            assert spec.idempotency_class == "SAFE"


def test_a_rerun_issues_the_same_commands(loaded: LoadedTemplate) -> None:
    first = RecordingPort()
    second = RecordingPort()
    a = run_steps(loaded, first)
    b = run_steps(loaded, second)
    assert [argv for _, argv in a] == [argv for _, argv in b]
    assert first.argvs() == second.argvs()
    assert first.argvs(), "the template ran no commands at all"


def test_directory_creation_uses_p_not_mkdir_exotic_flags(loaded: LoadedTemplate) -> None:
    port = RecordingPort()
    run_steps(loaded, port)
    joined = port.joined()
    # `mkdir -p` is the only idempotent way to make a tree; anything else would
    # fail on the second run once the directory exists.
    assert "mkdir -p" in joined
    assert "file.render" not in joined  # actions never appear in argv


def test_no_step_carries_a_payload_on_stdin(loaded: LoadedTemplate) -> None:
    # This template only makes directories; there is no file content to write,
    # so nothing may be smuggled into argv pretending to be content.
    port = RecordingPort()
    run_steps(loaded, port)
    assert all(payload is None for payload in port.payloads())
