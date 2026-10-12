"""The deployment plan: §8.2 order, and the point of no return.

These tests do not touch a host. They pin the two properties the whole
rollback story rests on — that the unit is version-agnostic so it can be
written before the link flips, and that the link flip is the boundary past
which a failure has to undo something — plus the refusals that must happen at
submit time rather than three steps into execution.
"""

from __future__ import annotations

import pytest

from shared.deploy_plan import (
    KIND_DEPLOY,
    KIND_ROLLBACK,
    DeployInputs,
    build_deploy_plan,
    build_restore_plan,
    render_env_file,
    render_unit_file,
)
from shared.redaction import redact_params

ROOT = "/opt/opspilot/apps/billing"


def _inputs(**overrides: object) -> DeployInputs:
    base: dict[str, object] = {
        "app_root": ROOT,
        "release_id": "1.4.2",
        "kind": KIND_DEPLOY,
        "release_version": "1.4.2",
        "artifact_url": "https://artifacts.example.internal/billing-1.4.2.tar.gz",
        "artifact_checksum": "0" * 64,
        "previous_release_id": "1.4.1",
        "run_user": "billing",
        "service_name": "opspilot-billing",
        "unit_path": "/etc/systemd/system/opspilot-billing.service",
        "python_version": "3.12",
        "health_url": "http://127.0.0.1:8080/healthz",
        "health_timeout_s": 5,
        "module_name": "billing.main",
        "app_port": 8080,
    }
    base.update(overrides)
    return DeployInputs(**base)  # type: ignore[arg-type]


# ==========================================================================
# Order
# ==========================================================================
def test_the_steps_follow_the_documented_publish_order() -> None:
    plan = build_deploy_plan(_inputs())
    keys = [s.key for s in plan.steps]
    assert keys == [
        "check_os",
        "check_disk",
        "ensure_user",
        "ensure_runtime",
        "fetch_release",
        "install_release",
        "render_config",
        "render_unit",
        "switch_current",
        "restart_service",
        "check_health",
    ]


def test_a_rollback_only_moves_the_link_it_is_already_fetched() -> None:
    """Rollback is a link flip, not a reinstall.

    The release it wants is on the host — that is the entire reason a rollback
    is fast. Re-fetching it would also be wrong: the artifact may no longer be
    where it was, and the checksum of an old build is not something an
    operator should have to reproduce at 3am.
    """
    plan = build_deploy_plan(_inputs(kind=KIND_ROLLBACK, release_id="1.4.1"))
    keys = [s.key for s in plan.steps]
    assert keys == [
        "check_os",
        "check_disk",
        "ensure_user",
        "render_config",
        "render_unit",
        "switch_current",
        "restart_service",
        "check_health",
    ]
    assert not any(s.action in {"app.fetch_release", "app.install_release"} for s in plan.steps)


# ==========================================================================
# The point of no return
# ==========================================================================
def test_exactly_one_step_flips_the_link() -> None:
    plan = build_deploy_plan(_inputs())
    flippers = [s.key for s in plan.steps if s.flips_link]
    assert flippers == ["switch_current"]
    assert plan.switch_index == [s.key for s in plan.steps].index("switch_current")


def test_the_unit_names_current_never_a_pinned_release() -> None:
    """Writing the unit before the switch is only safe if it follows the link.

    A unit pinned to ``releases/1.4.2`` would come up on 1.4.2 even after a
    rollback moved ``current`` back to 1.4.1 — which is the exact failure mode
    a "symlink switch plus restore" design is supposed to prevent.
    """
    body = render_unit_file(_inputs())
    assert f"WorkingDirectory={ROOT}/current" in body
    assert f"ExecStart={ROOT}/current/venv/bin/python -m billing.main" in body
    assert "releases/1.4.2" not in body
    assert f"EnvironmentFile={ROOT}/shared/config/app.env" in body


def test_a_first_deploy_has_nothing_to_restore() -> None:
    plan = build_deploy_plan(_inputs(previous_release_id=""))
    assert plan.can_restore is False
    assert build_restore_plan(_inputs(previous_release_id=""), plan) == ()


def test_a_failure_past_the_switch_restores_the_previous_release() -> None:
    inputs = _inputs()
    plan = build_deploy_plan(inputs)
    assert plan.can_restore is True
    assert plan.restore_release_id == "1.4.1"

    restore = build_restore_plan(inputs, plan)
    assert [s.key for s in restore] == ["restore_current", "restore_restart"]
    assert restore[0].params == {"app_root": ROOT, "release_id": "1.4.1"}
    assert restore[1].params == {"service": "opspilot-billing"}


# ==========================================================================
# Refusals at submit time
# ==========================================================================
def test_a_deploy_without_a_checksum_is_refused() -> None:
    with pytest.raises(ValueError, match="artifact_checksum"):
        build_deploy_plan(_inputs(artifact_checksum=""))


@pytest.mark.parametrize("release_id", ["latest", "current", "shared", "releases", "ops"])
def test_a_reserved_release_id_is_refused(release_id: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        build_deploy_plan(_inputs(release_id=release_id))


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown deployment kind"):
        build_deploy_plan(_inputs(kind="upgrade"))


# ==========================================================================
# Secrets
# ==========================================================================
def test_secrets_reach_the_env_file_but_not_the_recorded_params() -> None:
    """The whole point of ``config_ref``: the value is written to the host and
    goes nowhere else. ``params_json`` is a database column the task API hands
    back to anyone with ``task.read``.
    """
    inputs = _inputs(secrets={"OPS_DB_PASSWORD": "hunter2-s3cret"})
    plan = build_deploy_plan(inputs)
    step = plan.step("render_config")
    assert step is not None

    # The body really does contain the secret — that is what the host needs.
    assert "OPS_DB_PASSWORD=hunter2-s3cret" in step.params["content"]

    # ...and what we would persist does not.
    recorded = redact_params(step.params)
    assert "hunter2-s3cret" not in recorded["content"]
    assert recorded["content"].startswith("[BODY ")
    assert recorded["path"] == step.params["path"]


def test_the_unit_body_is_also_withheld_from_the_record() -> None:
    plan = build_deploy_plan(_inputs())
    step = plan.step("render_unit")
    assert step is not None
    recorded = redact_params(step.params)
    assert "ExecStart" not in str(recorded["content"])
    assert recorded["content"].startswith("[BODY ")


def test_the_env_file_is_written_for_the_host_not_for_the_log() -> None:
    body = render_env_file(
        _inputs(
            config={"APP_MODE": "production"},
            secrets={"API_TOKEN": "tok-abc"},
        )
    )
    assert "APP_MODE=production" in body
    assert "API_TOKEN=tok-abc" in body
    assert "APP_RELEASE=1.4.2" in body
    assert f"APP_ROOT={ROOT}" in body


def test_an_env_value_with_a_newline_is_refused() -> None:
    with pytest.raises(ValueError, match="newline"):
        render_env_file(_inputs(config={"X": "a\nb"}))


def test_an_env_key_that_is_not_an_identifier_is_refused() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        render_env_file(_inputs(config={"DROP TABLE x": "1"}))


def test_an_env_value_that_needs_quoting_gets_quoted() -> None:
    body = render_env_file(_inputs(config={"GREETING": 'say "hi"'}))
    assert 'GREETING="say \\"hi\\""' in body
    # A bare-safe value is left bare, so diffs stay readable.
    assert "APP_RELEASE=1.4.2" in body


def test_every_env_key_is_written_exactly_once() -> None:
    """``EnvironmentFile`` is last-wins, so a duplicate is a silent override.

    Found in the S4 drill: ``APP_PORT`` was emitted once from the typed
    ``app_port`` and again from ``config``, so ``config: {"APP_PORT": "9999"}``
    with ``app_port: 8888`` deployed 9999 while the plan and the health check
    still believed 8888.
    """
    body = render_env_file(
        _inputs(config={"APP_MODE": "production"}, secrets={"API_TOKEN": "tok-abc"})
    )
    keys = [
        line.split("=", 1)[0]
        for line in body.splitlines()
        if line and not line.startswith("#")
    ]
    assert len(keys) == len(set(keys)), f"duplicate env keys in {body!r}"
    assert keys.count("APP_PORT") == 1
    assert keys.count("APP_ROOT") == 1
    assert keys.count("APP_RELEASE") == 1
    assert keys.count("APP_USER") == 1


def test_an_env_key_that_clashes_with_a_deploy_input_is_refused() -> None:
    # Refused rather than silently resolved either way: whichever side won,
    # the health check and the process would disagree about the port.
    with pytest.raises(ValueError, match="APP_PORT"):
        render_env_file(_inputs(config={"APP_PORT": "9999"}))
    with pytest.raises(ValueError, match="APP_ROOT"):
        render_env_file(_inputs(secrets={"APP_ROOT": "/somewhere/else"}))
