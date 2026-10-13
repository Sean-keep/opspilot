"""``middleware/redis`` must install Redis without ever owning its state.

The two things this suite exists to pin:

* the service account is an explicit step with a nologin shell, not something
  the template trusts the package to get right;
* the data directory is only ever *probed*. A template that can create or
  reset a datadir is how a redis restore turns into a redis wipe.
"""

from __future__ import annotations

from pathlib import Path

from shared.actions.base import spec_of
from shared.actions.render import validate_read_path
from shared.templates import LoadedTemplate, dry_run_params, validate_template
from templates.harness import RecordingPort, probes_from_schema, run_steps

WRITING_ACTIONS = frozenset(
    {"file.render", "app.install_release", "app.fetch_release", "systemd.render_unit"}
)


def test_the_template_directory_is_named_after_the_template(template_dir: Path) -> None:
    assert template_dir.name == "redis"


def test_it_loads_and_validates_without_errors(loaded: LoadedTemplate) -> None:
    assert loaded.name == "redis"
    assert validate_template(loaded.spec, loaded.inputs_schema) == []
    assert dry_run_params(loaded.spec, loaded.inputs_schema) == {}


def test_the_steps_are_in_dependency_order(loaded: LoadedTemplate) -> None:
    assert [s.key for s in loaded.spec.steps] == [
        "check_os",
        "check_datadir_space",
        "ensure_user",
        "install_packages",
        "probe_redis",
    ]


# ==========================================================================
# The data directory is read-only to this template
# ==========================================================================
def test_the_datadir_is_probed_and_never_written(loaded: LoadedTemplate) -> None:
    """``system.check_disk`` takes a read path on purpose.

    Writing the datadir would need ``file.render``, whose write allowlist
    refuses ``/var/lib/redis`` anyway — so this is two independent refusals,
    not one.
    """
    step = next(s for s in loaded.spec.steps if s.key == "check_datadir_space")
    assert step.action == "system.check_disk"
    resolved = step.params["path"].replace("{{ inputs.datadir }}", "/var/lib/redis")
    assert validate_read_path(resolved) == "/var/lib/redis"

    used = {s.action for s in loaded.spec.steps}
    assert not (used & WRITING_ACTIONS), sorted(used & WRITING_ACTIONS)


def test_the_default_datadir_is_the_one_the_package_uses(loaded: LoadedTemplate) -> None:
    assert loaded.inputs_schema["properties"]["datadir"]["default"] == "/var/lib/redis"


# ==========================================================================
# Redis must not run as root
# ==========================================================================
def test_the_service_account_is_an_explicit_step(loaded: LoadedTemplate) -> None:
    ensure = next(s for s in loaded.spec.steps if s.key == "ensure_user")
    assert ensure.action == "system.ensure_user"
    assert ensure.params["username"] == "{{ inputs.redis_user }}"
    assert ensure.params["shell"] == "/usr/sbin/nologin"


def test_the_account_defaults_to_the_one_the_package_uses(loaded: LoadedTemplate) -> None:
    props = loaded.inputs_schema["properties"]
    assert props["redis_user"]["default"] == "redis"
    # A login shell would be a second, unmanaged way onto the host.
    assert loaded.spec.steps[2].params["shell"] == "/usr/sbin/nologin"


def test_the_package_install_is_the_only_privileged_step(loaded: LoadedTemplate) -> None:
    sudo_steps = [
        s.key for s in loaded.spec.steps if spec_of(s.action).requires_sudo
    ]
    # ensure_user needs useradd and package.install needs apt-get. Nothing
    # else in this template may escalate.
    assert sorted(sudo_steps) == ["ensure_user", "install_packages"]


def test_the_packages_are_the_redis_server_and_its_client(loaded: LoadedTemplate) -> None:
    step = next(s for s in loaded.spec.steps if s.key == "install_packages")
    assert sorted(step.params["packages"]) == ["redis-server", "redis-tools"]


# ==========================================================================
# Verification is part of the template, not a separate promise
# ==========================================================================
def test_the_last_step_proves_redis_answers(loaded: LoadedTemplate) -> None:
    probe = loaded.spec.steps[-1]
    assert probe.action == "redis.precheck"
    # No config_path: this is a PING after the install. A config file that
    # happens to exist must not short-circuit the health proof.
    assert "config_path" not in probe.params
    assert probe.params["host"] == "{{ inputs.redis_host }}"
    assert probe.params["port"] == "{{ inputs.redis_port }}"


def test_running_it_asks_for_a_ping_and_writes_nothing(loaded: LoadedTemplate) -> None:
    port = RecordingPort()
    run_steps(loaded, port, inputs=probes_from_schema(loaded.inputs_schema))
    heads = {argv[0] for argv in port.argvs()}
    assert "redis-cli" in heads
    # mkdir/chown/chmod come from ensure_user's useradd path and are about the
    # account, not about the datadir. Nothing may write a file.
    file_writers = {"dd", "tee", "install", "mv", "rm"}
    assert not (heads & file_writers), sorted(heads & file_writers)
