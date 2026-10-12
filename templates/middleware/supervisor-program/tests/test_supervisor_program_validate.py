"""supervisor_program must load, validate, and stay inside its config slot."""

from __future__ import annotations

from pathlib import Path

from shared.actions.base import spec_of
from shared.actions.render import validate_path
from shared.templates import (
    LoadedTemplate,
    compute_checksum,
    dry_run_params,
    validate_template,
)


def test_the_template_directory_is_named_after_the_template(template_dir: Path) -> None:
    assert template_dir.name == "supervisor-program"


def test_it_loads_and_validates_without_errors(loaded: LoadedTemplate) -> None:
    assert loaded.name == "supervisor-program"
    assert validate_template(loaded.spec, loaded.inputs_schema) == []
    assert dry_run_params(loaded.spec, loaded.inputs_schema) == {}


def test_every_step_is_a_registered_action(loaded: LoadedTemplate) -> None:
    for step in loaded.spec.steps:
        spec_of(step.action)


def test_the_program_config_is_confined_to_supervisor_conf_d(loaded: LoadedTemplate) -> None:
    props = loaded.inputs_schema["properties"]
    default = props["program_path"]["default"]
    assert default.startswith("/etc/supervisor/conf.d/")
    assert default.endswith(".conf")
    validate_path(default)
    assert props["program_path"]["pattern"].startswith("^/etc/supervisor/conf")


def test_the_log_directory_is_inside_the_app_root(loaded: LoadedTemplate) -> None:
    make_log = next(s for s in loaded.spec.steps if s.key == "make_log_dir")
    assert make_log.params["path"] == "{{ inputs.app_root }}/shared/logs"
    # shared/logs is the durable slot across releases — not releases/<id>, which
    # the next deploy will leave behind.
    assert "releases" not in make_log.params["path"]


def test_the_restart_target_matches_the_program_name(loaded: LoadedTemplate) -> None:
    restart = next(s for s in loaded.spec.steps if s.key == "restart_program")
    render = next(s for s in loaded.spec.steps if s.key == "render_program")
    assert restart.params["program"] == "{{ inputs.program_name }}"
    assert "[program:{{ inputs.program_name }}]" in render.params["content"]


def test_the_program_config_reaches_the_host_on_stdin() -> None:
    from shared.actions.base import resolve
    from shared.executor.port import ExecContext
    from templates.harness import RecordingPort

    port = RecordingPort()
    action = resolve("file.render", port)
    action.run(
        ExecContext(host="h", port=22, user="u", host_key_fingerprint="SHA256:x"),
        {
            "path": "/etc/supervisor/conf.d/opspilot-demo.conf",
            "kind": "file",
            "content": "[program:opspilot-demo]\ncommand=/bin/true\n",
            "mode": "0644",
        },
    )
    assert any(p and b"[program:opspilot-demo]" in p for p in port.payloads())
    assert "[program:opspilot-demo]" not in port.joined()


def test_the_checksum_covers_the_identity_files(loaded: LoadedTemplate) -> None:
    assert loaded.checksum == compute_checksum(loaded.files)
    assert set(loaded.files) == {"template.yaml", "schemas/inputs.schema.json"}
