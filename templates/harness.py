"""Harness shared by every template's ``tests/`` package.

Each template ships ``tests/test_validate.py`` and ``tests/test_idempotency.py``.
They all need the same three things: the action registry loaded, a way to find
the template directory from wherever pytest was invoked, and an ``ExecPort``
that records argv instead of opening a socket.

Nothing here talks to a managed host. These suites prove that a template is
*legal* and *re-runnable*; proving that it does the right thing on a real
machine is what ``shared/tests/test_actions.py`` and the live SSH target are
for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from shared.actions.base import resolve
from shared.actions.render import render
from shared.executor.port import ExecContext, ExecResult
from shared.templates import LoadedTemplate

__all__ = [
    "SERVER_PROBES",
    "RecordingPort",
    "probes_from_schema",
    "render_params",
    "run_steps",
]

#: A server mapping with every field a template is allowed to read. Mirrors
#: ``SERVER_VALUE_KEYS`` in ``shared/templates.py``.
SERVER_PROBES: dict[str, Any] = {
    "id": 1,
    "name": "probe",
    "hostname": "probe.example.internal",
    "port": 22,
    "ssh_user": "opstest",
    "description": "",
    "status": "active",
    "os_name": "Ubuntu",
    "os_version": "24.04",
    "kernel": "6.8.0",
    "arch": "x86_64",
    "cpu_count": 4,
    "cpu_model": "probe",
    "memory_total_mb": 16000,
    "memory_available_mb": 8000,
    "disk_total_gb": 100.0,
    "disk_available_gb": 50.0,
}


@dataclass
class RecordingPort:
    """An ``ExecPort`` that remembers what it was asked to run.

    ``answers`` maps ``argv[0]`` to the result it should produce. Anything
    unlisted succeeds with empty output, which is the right default for the
    commands these templates issue — the point of these suites is what was
    *asked*, not what a host would have answered.
    """

    answers: dict[str, ExecResult] = field(default_factory=dict)
    calls: list[tuple[tuple[str, ...], bytes | None]] = field(default_factory=list)

    def run(
        self,
        ctx: ExecContext,
        argv: Sequence[str],
        *,
        stdin: bytes | str | None = None,
    ) -> ExecResult:
        del ctx  # these suites never assert on the connection
        tokens = tuple(str(a) for a in argv)
        if stdin is None:
            payload: bytes | None = None
        elif isinstance(stdin, bytes):
            payload = stdin
        else:
            payload = stdin.encode("utf-8")
        self.calls.append((tokens, payload))
        head = tokens[0] if tokens else ""
        return self.answers.get(head, ExecResult(exit_code=0, stdout="", stderr="", argv=tokens))

    # -- assertion helpers -------------------------------------------------
    def argvs(self) -> list[tuple[str, ...]]:
        return [c[0] for c in self.calls]

    def joined(self) -> str:
        return "\n".join(" ".join(a) for a in self.argvs())

    def payloads(self) -> list[bytes | None]:
        return [c[1] for c in self.calls]


def probes_from_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Default values from an inputs schema, for rendering in tests.

    Mirrors the dry-run probe rules in ``shared.templates``: ``default`` wins,
    then ``const``, then the first of ``examples`` / ``enum``.
    """
    props = dict(schema.get("properties") or {})
    out: dict[str, Any] = {}
    for name, prop in props.items():
        prop = dict(prop)
        if "default" in prop:
            out[name] = prop["default"]
        elif "const" in prop:
            out[name] = prop["const"]
        elif prop.get("examples"):
            out[name] = prop["examples"][0]
        elif prop.get("enum"):
            out[name] = prop["enum"][0]
    return out


def render_params(
    params: Mapping[str, Any],
    inputs: Mapping[str, Any],
    server: Mapping[str, Any],
) -> dict[str, Any]:
    """Strict-render one step's params. Only ``{{ inputs.x }}`` / ``{{ server.y }}``."""

    def bind(node: Any) -> Any:
        if isinstance(node, str):
            return render(node, inputs, server)
        if isinstance(node, list):
            return [bind(v) for v in node]
        if isinstance(node, dict):
            return {k: bind(v) for k, v in node.items()}
        return node

    return dict(bind(dict(params)))


def run_steps(
    loaded: LoadedTemplate,
    port: RecordingPort,
    *,
    inputs: Mapping[str, Any] | None = None,
    server: Mapping[str, Any] | None = None,
) -> list[tuple[str, tuple[str, ...]]]:
    """Render and execute every step. Returns ``(step_key, argv)`` in order.

    ``argv`` is whatever that step last asked the port to run — usually a single
    command, but actions that need several will only show the final one here.
    Use ``port.argvs()`` when the whole sequence matters.

    Raises if a step's rendered parameters are not valid — a template that
    cannot run against its own schema defaults has no business shipping.
    """
    values = dict(probes_from_schema(loaded.inputs_schema))
    if inputs:
        values.update(inputs)
    ctx_server = dict(SERVER_PROBES)
    if server:
        ctx_server.update(server)

    ctx = ExecContext(host="probe", port=22, user="opstest", host_key_fingerprint="SHA256:probe")
    executed: list[tuple[str, tuple[str, ...]]] = []
    for step in loaded.spec.steps:
        rendered = render_params(step.params, values, ctx_server)
        action = resolve(step.action, port)
        action.run(ctx, action.build_params(rendered).model_dump())
        executed.append((step.key, port.calls[-1][0] if port.calls else ()))
    return executed
