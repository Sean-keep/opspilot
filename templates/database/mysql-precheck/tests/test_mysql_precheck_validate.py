"""mysql_precheck must be provably read-only.

Every assertion here is about what the template does *not* do. A precheck that
can create a datadir is an outage waiting for a typo.
"""

from __future__ import annotations

from pathlib import Path

from shared.actions.base import spec_of
from shared.templates import (
    LoadedTemplate,
    TemplateSpec,
    compute_checksum,
    dry_run_params,
    validate_template,
)


def test_the_template_directory_is_named_after_the_template(template_dir: Path) -> None:
    assert template_dir.name == "mysql-precheck"


def test_it_loads_and_validates_without_errors(loaded: LoadedTemplate) -> None:
    assert loaded.name == "mysql-precheck"
    assert validate_template(loaded.spec, loaded.inputs_schema) == []
    assert dry_run_params(loaded.spec, loaded.inputs_schema) == {}


def test_every_action_is_declared_read_only(loaded: LoadedTemplate) -> None:
    for step in loaded.spec.steps:
        spec = spec_of(step.action)
        assert spec.safe is True, f"{step.action} is not read-only"
        assert spec.idempotency_class == "SAFE"


def test_no_action_needs_sudo(loaded: LoadedTemplate) -> None:
    for step in loaded.spec.steps:
        assert spec_of(step.action).requires_sudo is False


def test_the_datadir_may_live_outside_the_write_allowlist(loaded: LoadedTemplate) -> None:
    """/var/lib/mysql is a legitimate thing to *probe*.

    The write allowlist exists to stop a template putting a file somewhere it
    must not; applying it to a read path would make this template unexpressible.
    """
    from shared.actions.render import validate_read_path

    datadir = loaded.inputs_schema["properties"]["datadir"]["default"]
    assert datadir == "/var/lib/mysql"
    validate_read_path(datadir)


def test_the_template_pins_expect_existing_datadir(loaded: LoadedTemplate) -> None:
    probe = next(s for s in loaded.spec.steps if s.key == "probe_mysql")
    assert probe.params["expect_existing_datadir"] is True
    schema = loaded.inputs_schema
    # The caller cannot switch initialisation on: the field is not an input.
    assert "expect_existing_datadir" not in schema.get("properties", {})


def test_refusing_to_initialise_a_missing_datadir_is_the_action_behaviour() -> None:
    """The template's job is to ask; the action's job is to say no.

    Asserted here against the real action so a refactor cannot quietly make
    ``mysql.precheck`` start creating directories.
    """
    from shared.actions.base import resolve
    from shared.executor.port import ExecContext, ExecResult
    from templates.harness import RecordingPort

    port = RecordingPort(
        answers={
            "test": ExecResult(exit_code=1, stdout="", stderr="", argv=("test", "-d")),
            "mysql": ExecResult(exit_code=1, stdout="", stderr="", argv=("mysql",)),
        }
    )
    action = resolve("mysql.precheck", port)
    result = action.run(
        ExecContext(host="h", port=22, user="u", host_key_fingerprint="SHA256:x"),
        {
            "host": "127.0.0.1",
            "port": 3306,
            "datadir": "/var/lib/mysql",
            "expect_existing_datadir": True,
        },
    )
    assert result.ok is False
    heads = {argv[0] for argv in port.argvs()}
    assert not (heads & {"mkdir", "mysqld", "chown", "chmod", "rm", "install", "dd", "tee"}), heads


def test_the_checksum_covers_the_identity_files(loaded: LoadedTemplate) -> None:
    assert loaded.checksum == compute_checksum(loaded.files)
    assert set(loaded.files) == {"template.yaml", "schemas/inputs.schema.json"}


def test_a_non_probe_action_cannot_be_smuggled_in() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["datadir"],
        "properties": {"datadir": {"type": "string", "default": "/var/lib/mysql"}},
    }
    spec = TemplateSpec.model_validate(
        {
            "api_version": "v1",
            "name": "mysql-precheck",
            "version": "1.0.0",
            "description": "probe",
            "steps": [
                {
                    "key": "oops",
                    "action": "file.render",
                    "params": {"path": "{{ inputs.datadir }}/oops", "content": "x"},
                }
            ],
        }
    )
    # /var/lib/mysql is not a write destination; the action model refuses it.
    problems = dry_run_params(spec, schema)
    assert "oops" in problems
