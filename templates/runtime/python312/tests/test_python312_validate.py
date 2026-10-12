"""python312 must load, validate, and dry-run clean.

These assertions are the gate between "someone edited the YAML" and "the
catalog will accept this version". A failure here means the template must not
be importable.
"""

from __future__ import annotations

from pathlib import Path

from shared.actions.base import spec_of
from shared.templates import (
    LoadedTemplate,
    compute_checksum,
    dry_run_params,
    validate_template,
)

EXPECTED_ACTIONS = [
    "system.check_os",
    "system.check_disk",
    "system.ensure_user",
    "python.ensure_runtime",
    "file.render",
    "file.render",
    "file.render",
]


def test_the_template_directory_is_named_after_the_template(template_dir: Path) -> None:
    assert template_dir.name == "python312"


def test_it_loads_and_validates_without_errors(loaded: LoadedTemplate) -> None:
    assert loaded.name == "python312"
    assert loaded.version == "1.0.0"
    assert validate_template(loaded.spec, loaded.inputs_schema) == []
    assert dry_run_params(loaded.spec, loaded.inputs_schema) == {}


def test_every_step_is_a_registered_action(loaded: LoadedTemplate) -> None:
    assert [s.action for s in loaded.spec.steps] == EXPECTED_ACTIONS
    for step in loaded.spec.steps:
        spec_of(step.action)  # raises for an unregistered name


def test_step_keys_are_unique_and_readable(loaded: LoadedTemplate) -> None:
    keys = [s.key for s in loaded.spec.steps]
    assert len(keys) == len(set(keys))
    assert "ensure_user" in keys


def test_the_inputs_schema_is_closed_and_bounded(loaded: LoadedTemplate) -> None:
    schema = loaded.inputs_schema
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"app_user", "app_root"}
    props = schema["properties"]
    assert props["python_version"]["enum"] == ["3.11", "3.12"]
    # The app_root pattern is what stops a caller naming /etc or a relative path.
    assert props["app_root"]["pattern"].startswith("^/opt/opspilot/apps/")


def test_the_checksum_covers_the_identity_files(loaded: LoadedTemplate) -> None:
    assert loaded.checksum == compute_checksum(loaded.files)
    assert set(loaded.files) == {"template.yaml", "schemas/inputs.schema.json"}
    assert len(loaded.checksum) == 64


def test_write_paths_stay_inside_the_allowlist(loaded: LoadedTemplate) -> None:
    """Only *write* targets are confined to the allowlist.

    ``system.check_disk.path`` is a read path and legitimately probes
    ``/opt/opspilot/apps`` itself, which is the parent of every allowed write
    prefix. Asserting the write rule on it would be wrong.
    """
    from shared.actions.render import ALLOWED_PATH_PREFIXES, validate_path

    write_fields = {"path", "app_root", "unit_path"}
    for step in loaded.spec.steps:
        if step.action == "system.check_disk":
            continue
        for name, value in step.params.items():
            if name not in write_fields:
                continue
            resolved = str(value).replace("{{ inputs.app_root }}", "/opt/opspilot/apps/demo")
            resolved = resolved.replace("{{ inputs.app_user }}", "opspilot-app")
            validate_path(resolved)
            assert resolved.startswith(ALLOWED_PATH_PREFIXES)
