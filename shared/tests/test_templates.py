"""Templates must not be able to name, reach, or shape anything they may not.

These tests pin the four refusal layers in :mod:`shared.templates`: an
unregistered action, an unknown parameter, an illegal expression, and a value
the action's own model would have rejected. A template that trips any of them
must fail as a whole — never "the good steps still run".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from shared.actions.base import register_all
from shared.templates import (
    PUBLISHED,
    STATUS_TRANSITIONS,
    TEMPLATE_STATUSES,
    TemplateError,
    TemplateSpec,
    compute_checksum,
    dry_run_params,
    load_template,
    validate_template,
)

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["app_user", "app_root"],
    "additionalProperties": False,
    "properties": {
        "app_user": {"type": "string", "default": "deploy"},
        "app_root": {"type": "string", "default": "/opt/opspilot/apps/demo"},
        "python_version": {"type": "string", "enum": ["3.11", "3.12"], "default": "3.12"},
    },
}


@pytest.fixture(autouse=True)
def _registry() -> None:
    register_all()


def _spec(*steps: dict[str, Any]) -> TemplateSpec:
    return TemplateSpec.model_validate(
        {
            "api_version": "v1",
            "name": "demo",
            "version": "1.0.0",
            "description": "test template",
            "steps": list(steps),
        }
    )


def _write_template(
    root: Path,
    manifest: dict[str, Any],
    schema: dict[str, Any] | None = None,
    *,
    dirname: str = "demo",
) -> Path:
    base = root / dirname
    (base / "schemas").mkdir(parents=True)
    (base / "template.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    (base / "schemas" / "inputs.schema.json").write_text(
        json.dumps(schema if schema is not None else SCHEMA, indent=2), encoding="utf-8"
    )
    return base


GOOD_STEPS = [
    {
        "key": "check_os",
        "action": "system.check_os",
        "params": {"expect": ["ubuntu-24.04", "debian-12"]},
    },
    {
        "key": "ensure_user",
        "action": "system.ensure_user",
        "params": {"username": "{{ inputs.app_user }}", "shell": "/usr/sbin/nologin"},
    },
    {
        "key": "make_root",
        "action": "file.render",
        "params": {
            "path": "{{ inputs.app_root }}/venv",
            "kind": "directory",
            "mode": "0750",
            "owner": "{{ inputs.app_user }}",
        },
    },
]


# ==========================================================================
# Happy path
# ==========================================================================
def test_a_wellformed_template_validates_with_no_errors() -> None:
    spec = _spec(*GOOD_STEPS)
    assert validate_template(spec, SCHEMA) == []
    assert dry_run_params(spec, SCHEMA) == {}


def test_load_template_reads_yaml_schema_and_checksum(tmp_path: Path) -> None:
    base = _write_template(
        tmp_path,
        {
            "api_version": "v1",
            "name": "demo",
            "version": "1.0.0",
            "description": "d",
            "steps": GOOD_STEPS,
        },
    )
    loaded = load_template(base)
    assert loaded.name == "demo"
    assert loaded.version == "1.0.0"
    assert loaded.spec.steps[0].action == "system.check_os"
    assert loaded.inputs_schema["additionalProperties"] is False
    assert len(loaded.checksum) == 64


# ==========================================================================
# Refusal layer 1 — the action registry
# ==========================================================================
def test_an_unregistered_action_is_refused() -> None:
    spec = _spec({"key": "evil", "action": "shell.run_arbitrary", "params": {}})
    errors = validate_template(spec, SCHEMA)
    assert any("unregistered action" in e for e in errors)


def test_a_typoed_action_name_is_refused_not_ignored() -> None:
    spec = _spec({"key": "typo", "action": "system.check_oss", "params": {}})
    assert any("unregistered action" in e for e in validate_template(spec, SCHEMA))


def test_load_template_raises_rather_than_returning_a_half_valid_template(tmp_path: Path) -> None:
    base = _write_template(
        tmp_path,
        {
            "api_version": "v1",
            "name": "demo",
            "version": "1.0.0",
            "steps": [
                {"key": "ok", "action": "system.check_os", "params": {}},
                {"key": "bad", "action": "shell.run", "params": {}},
            ],
        },
    )
    with pytest.raises(TemplateError, match="failed validation"):
        load_template(base)


# ==========================================================================
# Refusal layer 2 — parameter binding
# ==========================================================================
def test_an_unknown_parameter_name_is_refused() -> None:
    spec = _spec(
        {
            "key": "x",
            "action": "file.render",
            "params": {"path": "/opt/opspilot/apps/a", "pth": "y"},
        }
    )
    errors = validate_template(spec, SCHEMA)
    assert any("has no parameter 'pth'" in e for e in errors)


def test_a_placeholder_for_an_undeclared_input_is_refused() -> None:
    spec = _spec(
        {
            "key": "x",
            "action": "system.ensure_user",
            "params": {"username": "{{ inputs.nope }}"},
        }
    )
    errors = validate_template(spec, SCHEMA)
    assert any("inputs.nope is not declared" in e for e in errors)


@pytest.mark.parametrize(
    "field",
    ["ssh_password", "credential", "master_key", "host_key_fingerprint", "password", "nope"],
)
def test_a_placeholder_for_an_unreadable_server_field_is_refused(field: str) -> None:
    spec = _spec(
        {
            "key": "x",
            "action": "file.render",
            "params": {
                "path": "/opt/opspilot/apps/a/x.conf",
                "content": f"h={{{{ server.{field} }}}}",
            },
        }
    )
    errors = validate_template(spec, SCHEMA)
    assert any(f"server.{field} is not a readable server field" in e for e in errors)


def test_readable_server_fields_are_accepted() -> None:
    spec = _spec(
        {
            "key": "x",
            "action": "file.render",
            "params": {
                "path": "/opt/opspilot/apps/a/x.conf",
                "content": "host={{ server.hostname }} os={{ server.os_name }}",
            },
        }
    )
    assert validate_template(spec, SCHEMA) == []


# ==========================================================================
# Refusal layer 3 — template expressions
# ==========================================================================
@pytest.mark.parametrize(
    "content",
    [
        "{{ inputs.app_user | upper }}",
        "{{ inputs.app_user() }}",
        "{{ inputs['app_user'] }}",
        "{% if x %}y{% endif %}",
        "${HOME}",
        "$(id)",
        "`id`",
        "{{ 1 + 1 }}",
    ],
)
def test_an_illegal_expression_is_refused(content: str) -> None:
    spec = _spec(
        {
            "key": "x",
            "action": "file.render",
            "params": {"path": "/opt/opspilot/apps/a/x.conf", "content": content},
        }
    )
    errors = validate_template(spec, SCHEMA)
    assert any("params.content" in e for e in errors)


def test_an_illegal_expression_inside_a_nested_parameter_is_still_refused() -> None:
    spec = _spec(
        {
            "key": "x",
            "action": "system.check_os",
            "params": {"expect": ["ok", "{{ inputs.app_user | upper }}"]},
        }
    )
    errors = validate_template(spec, SCHEMA)
    assert any("params.expect[1]" in e for e in errors)


# ==========================================================================
# Refusal layer 4 — literal values the action model would refuse
# ==========================================================================
@pytest.mark.parametrize(
    "path",
    ["/etc/shadow", "/etc/passwd", "/opt/opspilot/apps/../../etc/shadow", "relative/path"],
)
def test_a_hardcoded_illegal_path_is_refused_at_load_time(path: str) -> None:
    spec = _spec(
        {"key": "x", "action": "file.render", "params": {"path": path, "content": "x"}}
    )
    errors = validate_template(spec, SCHEMA)
    assert any("params.path" in e for e in errors)


def test_a_service_name_outside_the_restart_whitelist_is_refused() -> None:
    # `sshd` matches the *character class* of a service name, so it gets past the
    # load-time shape check. The action model's whitelist is what stops it — and
    # the dry run is what surfaces that before anything is executed.
    spec = _spec({"key": "x", "action": "systemd.restart", "params": {"service": "sshd"}})
    assert validate_template(spec, SCHEMA) == []
    problems = dry_run_params(spec, SCHEMA)
    assert "x" in problems
    assert any("systemd.restart" in p for p in problems["x"])


def test_dry_run_catches_a_rendered_value_the_action_would_refuse() -> None:
    # `app_root` is declared and has a probe, so the dry render actually runs —
    # and the rendered path leaves the allowlist.
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "app_root": {"type": "string", "default": "/var/lib/evil"},
        },
    }
    spec = _spec(
        {
            "key": "x",
            "action": "file.render",
            "params": {"path": "{{ inputs.app_root }}/x.conf", "content": "a"},
        }
    )
    problems = dry_run_params(spec, schema)
    assert "x" in problems
    assert any("file.render" in p for p in problems["x"])


# ==========================================================================
# Schema discipline
# ==========================================================================
def test_a_schema_that_accepts_undeclared_inputs_is_refused(tmp_path: Path) -> None:
    loose = dict(SCHEMA)
    loose.pop("additionalProperties")
    base = _write_template(
        tmp_path, {"api_version": "v1", "name": "demo", "version": "1.0.0", "steps": GOOD_STEPS},
        schema=loose,
    )
    with pytest.raises(TemplateError, match="additionalProperties"):
        load_template(base)


def test_a_template_name_must_match_its_directory(tmp_path: Path) -> None:
    base = _write_template(
        tmp_path,
        {"api_version": "v1", "name": "something-else", "version": "1.0.0", "steps": GOOD_STEPS},
    )
    with pytest.raises(TemplateError, match="must match its directory"):
        load_template(base)


def test_duplicate_step_keys_are_refused() -> None:
    with pytest.raises(ValidationError, match="duplicate step keys"):
        _spec(
            {"key": "a", "action": "system.check_os", "params": {}},
            {"key": "a", "action": "system.check_disk", "params": {}},
        )


def test_an_empty_step_list_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least one step"):
        TemplateSpec.model_validate(
            {"api_version": "v1", "name": "demo", "version": "1.0.0", "steps": []}
        )


def test_unknown_top_level_keys_are_refused() -> None:
    with pytest.raises(ValidationError):
        TemplateSpec.model_validate(
            {
                "api_version": "v1",
                "name": "demo",
                "version": "1.0.0",
                "steps": [{"key": "a", "action": "system.check_os", "params": {}}],
                "on_error": "continue",
            }
        )


def test_unknown_step_keys_are_refused() -> None:
    with pytest.raises(ValidationError):
        TemplateSpec.model_validate(
            {
                "api_version": "v1",
                "name": "demo",
                "version": "1.0.0",
                "steps": [
                    {
                        "key": "a",
                        "action": "system.check_os",
                        "params": {},
                        "ignore_errors": True,
                    }
                ],
            }
        )


# ==========================================================================
# Checksum pinning
# ==========================================================================
def test_the_checksum_is_deterministic_and_order_independent() -> None:
    files_a = {"template.yaml": "a: 1\n", "schemas/inputs.schema.json": "{}"}
    files_b = {"schemas/inputs.schema.json": "{}", "template.yaml": "a: 1\n"}
    assert compute_checksum(files_a) == compute_checksum(files_b)
    assert compute_checksum(files_a) == compute_checksum(files_a)


def test_the_checksum_changes_when_the_content_changes() -> None:
    base = {"template.yaml": "a: 1\n"}
    assert compute_checksum(base) != compute_checksum({"template.yaml": "a: 2\n"})
    assert compute_checksum(base) != compute_checksum({"template.yaml": "a: 1\n", "extra": "x"})


def test_the_checksum_ignores_line_endings() -> None:
    assert compute_checksum({"a": "x\ny\n"}) == compute_checksum({"a": "x\r\ny\r\n"})


def test_reloading_an_unchanged_template_gives_the_same_checksum(tmp_path: Path) -> None:
    base = _write_template(
        tmp_path, {"api_version": "v1", "name": "demo", "version": "1.0.0", "steps": GOOD_STEPS}
    )
    first = load_template(base).checksum
    second = load_template(base).checksum
    assert first == second

    manifest = base / "template.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    assert load_template(base).checksum != first


def test_the_checksum_covers_the_inputs_schema_not_the_tests(tmp_path: Path) -> None:
    base = _write_template(
        tmp_path, {"api_version": "v1", "name": "demo", "version": "1.0.0", "steps": GOOD_STEPS}
    )
    before = load_template(base).checksum
    (base / "tests").mkdir()
    (base / "tests" / "test_validate.py").write_text("x = 1\n", encoding="utf-8")
    assert load_template(base).checksum == before

    schema = base / "schemas" / "inputs.schema.json"
    schema.write_text(schema.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert load_template(base).checksum != before


# ==========================================================================
# Lifecycle
# ==========================================================================
def test_the_status_vocabulary_matches_the_design_document() -> None:
    assert set(TEMPLATE_STATUSES) == {"DRAFT", "VALIDATED", "TESTED", "PUBLISHED", "DEPRECATED"}


def test_a_published_version_cannot_move_back_to_draft() -> None:
    assert "DRAFT" not in STATUS_TRANSITIONS[PUBLISHED]
    assert STATUS_TRANSITIONS[PUBLISHED] == frozenset({"DEPRECATED"})
    assert STATUS_TRANSITIONS["DEPRECATED"] == frozenset()


def test_every_status_has_a_transition_table_entry() -> None:
    assert set(STATUS_TRANSITIONS) == set(TEMPLATE_STATUSES)
