"""python_app must load, validate, and dry-run clean.

This is the most dangerous template in the tree — it fetches code, unpacks it,
writes a service unit, and restarts a process — so the assertions below lean
on the refusal layers rather than the happy path.
"""

from __future__ import annotations

from pathlib import Path

from shared.templates import (
    LoadedTemplate,
    TemplateSpec,
    compute_checksum,
    dry_run_params,
    validate_template,
)


def test_the_template_directory_is_named_after_the_template(template_dir: Path) -> None:
    assert template_dir.name == "python-app"


def test_it_loads_and_validates_without_errors(loaded: LoadedTemplate) -> None:
    assert loaded.name == "python-app"
    assert validate_template(loaded.spec, loaded.inputs_schema) == []
    assert dry_run_params(loaded.spec, loaded.inputs_schema) == {}


def test_the_steps_are_in_dependency_order(loaded: LoadedTemplate) -> None:
    keys = [s.key for s in loaded.spec.steps]
    assert keys == [
        "check_os",
        "check_disk",
        "ensure_user",
        "fetch_release",
        "install_release",
        "render_env",
        "render_unit",
        "restart_service",
        "check_health",
    ]
    # Fetch must come before install; install before the unit is written.
    assert keys.index("fetch_release") < keys.index("install_release")
    assert keys.index("install_release") < keys.index("render_unit")
    # The restart must be last-but-one: a failed health check after it is a
    # rollback signal, not a reason to restart again.
    assert keys.index("restart_service") < keys.index("check_health")


def test_latest_is_refused_as_a_release_id() -> None:
    """Production must pin a release. ``latest`` is the failure mode."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["app_user", "app_root", "release_id", "artifact_url", "artifact_sha256"],
        "properties": {
            "app_user": {"type": "string", "default": "opspilot-app"},
            "app_root": {"type": "string", "default": "/opt/opspilot/apps/demo"},
            "release_id": {"type": "string", "default": "latest"},
            "artifact_url": {
                "type": "string",
                "default": "https://artifacts.example.internal/demo/x.tar.gz",
            },
            "artifact_sha256": {"type": "string", "default": "a" * 64},
        },
    }
    spec = TemplateSpec.model_validate(
        {
            "api_version": "v1",
            "name": "python-app",
            "version": "1.0.0",
            "description": "probe",
            "steps": [
                {
                    "key": "fetch",
                    "action": "app.fetch_release",
                    "params": {
                        "app_root": "{{ inputs.app_root }}",
                        "release_id": "{{ inputs.release_id }}",
                        "source": "{{ inputs.artifact_url }}",
                        "checksum": "{{ inputs.artifact_sha256 }}",
                    },
                }
            ],
        }
    )
    problems = dry_run_params(spec, schema)
    assert "fetch" in problems
    assert any("app.fetch_release" in p for p in problems["fetch"])


def test_the_artifact_is_always_checksummed(loaded: LoadedTemplate) -> None:
    fetch = next(s for s in loaded.spec.steps if s.key == "fetch_release")
    assert "checksum" in fetch.params
    assert fetch.params["checksum"] == "{{ inputs.artifact_sha256 }}"
    assert loaded.inputs_schema["required"].count("artifact_sha256") == 1


def test_the_unit_path_cannot_name_a_subdirectory() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["unit_path"],
        "properties": {
            "unit_path": {
                "type": "string",
                "default": "/etc/systemd/system/evil/nested.service",
            }
        },
    }
    spec = TemplateSpec.model_validate(
        {
            "api_version": "v1",
            "name": "python-app",
            "version": "1.0.0",
            "description": "probe",
            "steps": [
                {
                    "key": "unit",
                    "action": "systemd.render_unit",
                    "params": {"unit_path": "{{ inputs.unit_path }}", "content": "[Unit]\n"},
                }
            ],
        }
    )
    problems = dry_run_params(spec, schema)
    assert "unit" in problems


def test_the_restart_targets_the_allowlist(loaded: LoadedTemplate) -> None:
    restart = next(s for s in loaded.spec.steps if s.key == "restart_service")
    assert restart.params["service"] == "{{ inputs.service_name }}"
    assert loaded.inputs_schema["properties"]["service_name"]["default"].startswith("opspilot-")


def test_env_file_contents_reach_the_host_on_stdin_not_argv() -> None:
    """The env file is written by ``file.render``.

    Its content is payload, so it has to travel on stdin. argv is logged and
    persisted in ``result_json``; a config body does not belong there.
    """
    from shared.actions.base import resolve
    from shared.executor.port import ExecContext
    from templates.harness import RecordingPort

    port = RecordingPort()
    action = resolve("file.render", port)
    action.run(
        ExecContext(host="h", port=22, user="u", host_key_fingerprint="SHA256:x"),
        {
            "path": "/opt/opspilot/apps/demo/shared/config/app.env",
            "kind": "file",
            "content": "APP_PORT=8000\n",
            "mode": "0640",
        },
    )
    assert port.payloads() == [None, b"APP_PORT=8000\n", None] or any(
        p == b"APP_PORT=8000\n" for p in port.payloads()
    )
    assert "APP_PORT=8000" not in port.joined()


def test_the_checksum_covers_the_identity_files(loaded: LoadedTemplate) -> None:
    assert loaded.checksum == compute_checksum(loaded.files)
    assert set(loaded.files) == {"template.yaml", "schemas/inputs.schema.json"}
