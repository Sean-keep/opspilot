"""The Python + systemd deployment plan.

This is the executable form of the requirements' §8.2 发布步骤: an ordered list
of :doc:`Action Registry <shared.actions>` calls that take a pinned release
from "fetched" to "live and healthy", plus the rule for what to do when one of
them stops.

It lives in ``shared/`` because both sides need to reason about it: the API
builds one to decide whether a deployment is even *submittable*, and the
worker builds the same one to know what to run. Neither may invent steps.

Two properties are load-bearing and are enforced by construction rather than
by convention:

**The unit is version-agnostic.** It names ``<app_root>/current``, never a
pinned release directory, so writing it *before* the link flips cannot strand
a process on the wrong tree. That is why §8.2 can put "生成 systemd unit" ahead
of "原子切换 current 软链接" without the ordering being a trap.

**The link flip is the point of no return.** Every step before
``switch_current`` can fail harmlessly — the live release is untouched. Every
step from there on can leave the host on a release that does not work, so
``DeployPlan.restore_after`` says exactly what to put back and
``build_restore_plan`` builds the undo. A first deploy has no previous release
and therefore no undo; that is reported as a policy fact, not discovered at
3am.

A rollback is not a special mode. It is the same plan with the fetch/install
steps dropped, because the release it wants is already on the host — which is
precisely why a rollback is fast and a deploy is not.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from shared.actions.builtin import check_release_id

__all__ = [
    "KIND_DEPLOY",
    "KIND_ROLLBACK",
    "DeployInputs",
    "DeployPlan",
    "PlanStep",
    "build_deploy_plan",
    "build_restore_plan",
    "inputs_from_mapping",
    "render_env_file",
    "render_unit_file",
]

KIND_DEPLOY = "deploy"
KIND_ROLLBACK = "rollback"

#: The env file the unit reads. Under ``shared/config`` so it survives a
#: release switch and is never inside a directory an install may overwrite.
ENV_RELATIVE_PATH = "shared/config/app.env"

_ENV_BARE_RE = r"^[A-Za-z0-9_./:@+-]*$"


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One action call in the plan."""

    key: str
    action: str
    idempotency_class: str
    params: dict[str, Any]
    #: True once this step is the one that makes a release live. Failure at or
    #: after it means the host may be on the wrong tree.
    flips_link: bool = False

    def __post_init__(self) -> None:
        # Frozen dataclass: validation belongs to the builder, but a step that
        # claims to flip the link without calling the switch action would make
        # the restore policy lie. Cheap to catch here.
        if self.flips_link and self.action != "app.switch_current":
            raise ValueError(
                f"step {self.key!r} is marked as flipping the link but runs {self.action!r}"
            )


@dataclass(frozen=True, slots=True)
class DeployPlan:
    """What to run, and what to undo."""

    steps: tuple[PlanStep, ...]
    #: Index of the link-flipping step, or ``-1`` when the plan has none.
    switch_index: int
    #: Release to put back when a step at or after ``switch_index`` fails.
    #: Empty means the host has no previous release and cannot be restored.
    restore_release_id: str
    kind: str = KIND_DEPLOY

    @property
    def can_restore(self) -> bool:
        return self.switch_index >= 0 and bool(self.restore_release_id)

    def step(self, key: str) -> PlanStep | None:
        for item in self.steps:
            if item.key == key:
                return item
        return None


@dataclass(frozen=True, slots=True)
class DeployInputs:
    """Everything the plan needs. Values, not references — the caller has
    already resolved ``config_ref`` and decided what is secret."""

    app_root: str
    release_id: str
    kind: str = KIND_DEPLOY

    release_version: str = ""
    artifact_url: str = ""
    artifact_checksum: str = ""
    previous_release_id: str = ""

    run_user: str = ""
    service_name: str = ""
    unit_path: str = ""
    python_version: str = ""
    requirements: str = "requirements.lock"

    health_url: str = ""
    health_timeout_s: int = 10

    module_name: str = ""
    app_port: int = 8000

    #: Non-secret environment entries. Recorded like any other parameter.
    config: Mapping[str, str] = field(default_factory=dict)
    #: Secret environment entries. Written to the host, never to a log or a
    #: database column — see :func:`shared.redaction.redact_params`.
    secrets: Mapping[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# File bodies
# --------------------------------------------------------------------------
def render_env_file(inputs: DeployInputs) -> str:
    """Build the ``EnvironmentFile`` body.

    Format is systemd's: one ``KEY=value`` per line, quoting only when the
    value needs it. A value containing a newline is refused outright — there
    is no quoting that makes a multi-line value mean one thing to
    ``EnvironmentFile``, and silently truncating at the newline would deploy a
    config nobody asked for.

    Every key appears **exactly once**. ``APP_PORT`` and friends come from the
    typed deploy inputs — the same values the plan and the health check are
    built on — and an operator-supplied ``config`` / ``secrets`` entry that
    collides with one of them is refused rather than written a second time.
    ``EnvironmentFile`` is last-wins, so a duplicate would quietly deploy
    ``APP_PORT=9999`` while the health check still probed 8888.
    """
    reserved: dict[str, str] = {
        "APP_ROOT": inputs.app_root,
        "APP_RELEASE": inputs.release_id,
        "APP_PORT": str(inputs.app_port),
    }
    if inputs.run_user:
        reserved["APP_USER"] = inputs.run_user

    merged: dict[str, str] = {}
    for key, value in {**dict(inputs.config), **dict(inputs.secrets)}.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"config value {key!r} must not contain a newline")
        if not key or not all(c.isalnum() or c == "_" for c in key):
            raise ValueError(f"config key {key!r} must be alphanumeric or underscore")
        if key in reserved:
            raise ValueError(
                f"config key {key!r} conflicts with the deploy input of the same "
                f"name (the plan and the health check use the deploy input)"
            )
        merged[key] = value

    lines: list[str] = ["# rendered by OpsPilot; do not edit — the next deploy rewrites it"]
    lines.extend(f"{key}={reserved[key]}" for key in sorted(reserved))
    lines.extend(f"{key}={_env_quote(merged[key])}" for key in sorted(merged))
    return "\n".join(lines) + "\n"


def _env_quote(value: str) -> str:
    if re.match(_ENV_BARE_RE, value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_unit_file(inputs: DeployInputs) -> str:
    """Build the systemd unit body.

    ``WorkingDirectory`` and ``ExecStart`` both go through ``current``, never
    through ``releases/<id>``. That is what makes the unit safe to install
    before the link flips, and what makes a rollback a link flip rather than a
    reinstall.
    """
    if not inputs.service_name:
        raise ValueError("service_name is required to render a unit")
    if not inputs.run_user:
        raise ValueError("run_user is required to render a unit")

    exec_start = f"{inputs.app_root}/current/venv/bin/python"
    if inputs.module_name:
        exec_start += f" -m {inputs.module_name}"

    return (
        "[Unit]\n"
        "Description=OpsPilot managed application\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={inputs.run_user}\n"
        f"WorkingDirectory={inputs.app_root}/current\n"
        f"EnvironmentFile={inputs.app_root}/{ENV_RELATIVE_PATH}\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------
def build_deploy_plan(inputs: DeployInputs) -> DeployPlan:
    """The §8.2 sequence for *inputs*.

    Refuses rather than degrades: a production deploy without a checksum, or a
    link flip with nothing to fall back to, is a bad plan to discover while
    executing it.
    """
    if inputs.kind not in {KIND_DEPLOY, KIND_ROLLBACK}:
        raise ValueError(f"unknown deployment kind {inputs.kind!r}")
    if not inputs.release_id:
        raise ValueError("release_id is required")
    # Refuse at plan-build time, not three steps into execution. This is the
    # same function the actions validate with, so the API and the worker cannot
    # disagree about what "latest" means.
    check_release_id(inputs.release_id)
    if inputs.previous_release_id:
        check_release_id(inputs.previous_release_id)

    steps: list[PlanStep] = [
        PlanStep(
            key="check_os",
            action="system.check_os",
            idempotency_class="SAFE",
            params={"expect": ["ubuntu-22.04", "ubuntu-24.04", "debian-12"]},
        ),
        PlanStep(
            key="check_disk",
            action="system.check_disk",
            idempotency_class="SAFE",
            params={
                "path": f"{inputs.app_root}/releases",
                "min_available_mb": 2048,
            },
        ),
    ]

    if inputs.run_user:
        steps.append(
            PlanStep(
                key="ensure_user",
                action="system.ensure_user",
                idempotency_class="CONDITIONAL",
                params={"username": inputs.run_user, "shell": "/usr/sbin/nologin"},
            )
        )

    # A rollback wants a release that is already on the host. Fetching and
    # unpacking it again would be both wrong and slow; if it is somehow gone,
    # the switch refuses and the failure is honest.
    if inputs.kind == KIND_DEPLOY:
        if inputs.python_version:
            steps.append(
                PlanStep(
                    key="ensure_runtime",
                    action="python.ensure_runtime",
                    idempotency_class="CONDITIONAL",
                    params={"version": inputs.python_version},
                )
            )
        if not inputs.artifact_checksum:
            raise ValueError(
                "artifact_checksum is required for a deploy; a release that cannot "
                "be verified is not a release"
            )
        steps.append(
            PlanStep(
                key="fetch_release",
                action="app.fetch_release",
                idempotency_class="CONDITIONAL",
                params={
                    "app_root": inputs.app_root,
                    "release_id": inputs.release_id,
                    "source": inputs.artifact_url,
                    "checksum": inputs.artifact_checksum,
                },
            )
        )
        steps.append(
            PlanStep(
                key="install_release",
                action="app.install_release",
                idempotency_class="CONDITIONAL",
                params={
                    "app_root": inputs.app_root,
                    "release_id": inputs.release_id,
                    "owner": inputs.run_user,
                    "python_version": inputs.python_version,
                    "requirements": inputs.requirements,
                },
            )
        )

    steps.append(
        PlanStep(
            key="render_config",
            action="file.render",
            idempotency_class="CONDITIONAL",
            params={
                "path": f"{inputs.app_root}/{ENV_RELATIVE_PATH}",
                "kind": "file",
                "mode": "0640",
                "owner": inputs.run_user,
                "backup": True,
                "content": render_env_file(inputs),
            },
        )
    )
    steps.append(
        PlanStep(
            key="render_unit",
            action="systemd.render_unit",
            idempotency_class="CONDITIONAL",
            params={
                "unit_path": inputs.unit_path,
                "mode": "0644",
                "content": render_unit_file(inputs),
            },
        )
    )
    steps.append(
        PlanStep(
            key="switch_current",
            action="app.switch_current",
            idempotency_class="CONDITIONAL",
            params={
                "app_root": inputs.app_root,
                "release_id": inputs.release_id,
            },
            flips_link=True,
        )
    )
    steps.append(
        PlanStep(
            key="restart_service",
            action="systemd.restart",
            idempotency_class="DESTRUCTIVE",
            params={"service": inputs.service_name},
        )
    )
    if inputs.health_url:
        steps.append(
            PlanStep(
                key="check_health",
                action="http.check",
                idempotency_class="SAFE",
                params={
                    "url": inputs.health_url,
                    "expect_status": 200,
                    "timeout_seconds": inputs.health_timeout_s,
                },
            )
        )

    switch_index = next(i for i, s in enumerate(steps) if s.flips_link)
    return DeployPlan(
        steps=tuple(steps),
        switch_index=switch_index,
        restore_release_id=inputs.previous_release_id,
        kind=inputs.kind,
    )


def build_restore_plan(inputs: DeployInputs, plan: DeployPlan) -> tuple[PlanStep, ...]:
    """The undo, for a failure at or after the link flip.

    Empty when the plan says the host cannot be restored — a first deploy, or
    a plan with no switch. The caller decides what that means for the
    deployment record (``FAILED``, not ``ROLLED_BACK``) rather than quietly
    pretending the host is fine.
    """
    if not plan.can_restore:
        return ()
    return (
        PlanStep(
            key="restore_current",
            action="app.switch_current",
            idempotency_class="CONDITIONAL",
            params={
                "app_root": inputs.app_root,
                "release_id": plan.restore_release_id,
            },
            flips_link=True,
        ),
        PlanStep(
            key="restore_restart",
            action="systemd.restart",
            idempotency_class="DESTRUCTIVE",
            params={"service": inputs.service_name},
        ),
    )


#: Field names a task's ``params.deploy`` mapping may carry. Anything else is
#: refused rather than ignored: a parameter nobody declared is a parameter
#: nobody is checking.
_DEPLOY_FIELDS = frozenset(DeployInputs.__dataclass_fields__)


def inputs_from_mapping(
    data: Mapping[str, Any],
    *,
    secrets: Mapping[str, str] | None = None,
) -> DeployInputs:
    """Rebuild inputs from what the API stored on the task.

    ``secrets`` is resolved here and never round-trips through the database:
    the task carries ``config_ref``, the worker decrypts it at execution time,
    and the values go straight into the env file body. The API never sees them,
    which is the same split the SSH private key follows.

    Unknown keys are refused. Silently dropping one would deploy something
    other than what was submitted and leave no trace of the difference.
    """
    unknown = set(data) - _DEPLOY_FIELDS - {"config_ref"}
    if unknown:
        raise ValueError(f"unknown deployment parameters: {sorted(unknown)}")

    kwargs: dict[str, Any] = {k: v for k, v in data.items() if k in _DEPLOY_FIELDS}
    kwargs["secrets"] = dict(secrets or {})
    try:
        return DeployInputs(**kwargs)
    except TypeError as exc:
        raise ValueError(f"bad deployment parameters: {exc}") from exc
