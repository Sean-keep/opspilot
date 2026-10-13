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
    TemplateApplyParams,
    TemplateError,
    TemplateSpec,
    build_template_plan,
    compute_checksum,
    dry_run_params,
    load_template,
    plan_template_apply,
    validate_apply_inputs,
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


# ==========================================================================
# build_template_plan — a published template becomes runnable work
# ==========================================================================
def test_a_template_renders_into_plan_steps_in_order() -> None:
    spec = _spec(*GOOD_STEPS)
    plan = build_template_plan(
        spec, SCHEMA, {"app_user": "svc20k", "app_root": "/opt/opspilot/apps/20kpay-api"}
    )
    assert [s.key for s in plan] == ["check_os", "ensure_user", "make_root"]
    assert [s.action for s in plan] == [
        "system.check_os",
        "system.ensure_user",
        "file.render",
    ]
    assert plan[1].params == {"username": "svc20k", "shell": "/usr/sbin/nologin"}
    assert plan[2].params["path"] == "/opt/opspilot/apps/20kpay-api/venv"


def test_each_step_carries_the_action_declared_idempotency_class() -> None:
    spec = _spec(*GOOD_STEPS)
    plan = build_template_plan(
        spec, SCHEMA, {"app_user": "svc20k", "app_root": "/opt/opspilot/apps/20kpay-api"}
    )
    classes = {s.key: s.idempotency_class for s in plan}
    assert classes["check_os"] == "SAFE"
    assert classes["ensure_user"] == "CONDITIONAL"
    assert classes["make_root"] == "CONDITIONAL"


def test_a_missing_required_input_is_refused_before_anything_renders() -> None:
    spec = _spec(*GOOD_STEPS)
    with pytest.raises(TemplateError, match="missing required input 'app_root'"):
        build_template_plan(spec, SCHEMA, {"app_user": "svc20k"})


def test_an_undeclared_input_is_refused() -> None:
    spec = _spec(*GOOD_STEPS)
    with pytest.raises(TemplateError, match="not declared in the inputs schema"):
        build_template_plan(
            spec,
            SCHEMA,
            {"app_user": "svc20k", "app_root": "/opt/x", "nope": "1"},
        )


def test_a_value_the_action_would_refuse_fails_the_whole_plan() -> None:
    """One bad step must not yield a partially built plan to run.

    ``make_root`` is legal, so the refusal has to come from ``switch`` — a
    ``release_id`` of ``latest`` is exactly what production deploys forbid. If
    the earlier step were the one to fail, this test would be asserting the
    wrong thing.
    """
    spec = _spec(
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
        {
            "key": "switch",
            "action": "app.switch_current",
            "params": {
                "app_root": "{{ inputs.app_root }}",
                "release_id": "{{ inputs.release }}",
            },
        },
    )
    with pytest.raises(TemplateError, match=r"step 'switch'"):
        build_template_plan(
            spec,
            {
                "type": "object",
                "required": ["app_user", "app_root", "release"],
                "properties": {
                    "app_user": {"type": "string", "default": "deploy"},
                    "app_root": {"type": "string", "default": "/opt/opspilot/apps/x"},
                    "release": {"type": "string", "default": "1.4.2"},
                },
            },
            {
                "app_user": "svc",
                "app_root": "/opt/opspilot/apps/x",
                "release": "latest",
            },
        )


def test_server_fields_are_substituted_from_the_host_being_worked_on() -> None:
    spec = _spec(
        {
            "key": "note",
            "action": "file.render",
            "params": {
                "path": "/opt/opspilot/apps/x/note",
                "kind": "file",
                "content": "on {{ server.hostname }} as {{ inputs.app_user }}",
            },
        }
    )
    plan = build_template_plan(
        spec,
        {
            "type": "object",
            "required": ["app_user"],
            "properties": {"app_user": {"type": "string"}},
        },
        {"app_user": "svc20k"},
        {"hostname": "pay-07.internal"},
    )
    assert plan[0].params["content"] == "on pay-07.internal as svc20k"


def test_a_switch_step_is_marked_as_flipping_the_link() -> None:
    spec = _spec(
        {
            "key": "switch",
            "action": "app.switch_current",
            "params": {
                "app_root": "/opt/opspilot/apps/20kpay-api",
                "release_id": "1.4.2",
            },
        }
    )
    plan = build_template_plan(spec, {"type": "object", "properties": {}}, {})
    assert plan[0].flips_link is True
    # ...and a step that is not the switch must never claim to be.
    spec2 = _spec(
        {
            "key": "render",
            "action": "file.render",
            "params": {"path": "/opt/opspilot/apps/x/f", "kind": "file", "content": "x"},
        }
    )
    assert (
        build_template_plan(spec2, {"type": "object", "properties": {}}, {})[0].flips_link
        is False
    )


def test_the_plan_for_the_same_inputs_is_reproducible() -> None:
    """What the API approves is what the worker runs — twice must be equal."""
    spec = _spec(*GOOD_STEPS)
    inputs = {
        "app_user": "svc20k",
        "app_root": "/opt/opspilot/apps/20kpay-api",
    }
    assert build_template_plan(spec, SCHEMA, inputs) == build_template_plan(
        spec, SCHEMA, inputs
    )


# ==========================================================================
# template.apply — the wire body and the plan it commits to
# ==========================================================================
def _apply_payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "template": {
            "name": "demo",
            "version": "1.0.0",
            "checksum": "a" * 64,
        },
        "spec": _spec(*GOOD_STEPS).model_dump(),
        "inputs_schema": SCHEMA,
        "inputs": {"app_user": "svc20k", "app_root": "/opt/opspilot/apps/20kpay-api"},
    }
    body.update(overrides)
    return body


def test_the_apply_body_carries_the_thing_it_pins() -> None:
    params = TemplateApplyParams.model_validate(_apply_payload())
    assert params.template.name == "demo"
    assert params.template.checksum == "a" * 64
    assert params.config_ref == ""
    # The body is self-contained: the worker never has to ask the catalog.
    assert params.spec.steps[0].action == "system.check_os"


def test_the_apply_body_refuses_a_checksum_that_is_not_a_sha256() -> None:
    with pytest.raises(ValidationError):
        TemplateApplyParams.model_validate(_apply_payload(template={"name": "demo",
                                                                   "version": "1.0.0",
                                                                   "checksum": "short"}))


def test_the_apply_body_refuses_unexpected_keys() -> None:
    """A task body is not a place to smuggle extra instructions."""
    with pytest.raises(ValidationError):
        TemplateApplyParams.model_validate(_apply_payload(on_error="continue"))


def test_an_apply_body_renders_the_same_plan_a_direct_build_would() -> None:
    payload = _apply_payload()
    assert plan_template_apply(payload) == build_template_plan(
        _spec(*GOOD_STEPS), SCHEMA, payload["inputs"]
    )


def test_an_apply_body_that_cannot_render_is_refused_with_the_template_name() -> None:
    payload = _apply_payload(inputs={"app_user": "svc20k"})
    with pytest.raises(TemplateError, match=r"template 'demo' '1\.0\.0'"):
        plan_template_apply(payload)


def test_secrets_fill_xsecret_fields_but_stay_out_of_the_task_body() -> None:
    """A field the template fills from config_ref is declared ``x-secret``.

    The API checks the body at submit without the bundle, so a *required*
    secret field must not make every submit fail — and it must not be accepted
    in the body either, because the body is stored in ``ops_task_runs``.
    """
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "app_user": {"type": "string"},
            "db_password": {"type": "string", "x-secret": True},
        },
        "required": ["app_user", "db_password"],
    }
    spec = _spec(
        {
            "key": "write",
            "action": "file.render",
            "params": {
                "path": "/opt/opspilot/apps/x/app.conf",
                "kind": "file",
                "content": "user={{ inputs.app_user }} pass={{ inputs.db_password }}",
            },
        }
    )
    inputs = {"app_user": "svc20k"}

    # Submit-time: the required secret is excused, and it may not be posted.
    assert validate_apply_inputs(schema, inputs) == []
    assert any("x-secret" in e for e in validate_apply_inputs(schema, {**inputs,
                                                                      "db_password": "x"}))

    # Execution-time: the bundle arrives and fills the field.
    plan = build_template_plan(
        spec, schema, inputs, secrets={"db_password": "s3cret"}
    )
    assert plan[0].params["content"] == "user=svc20k pass=s3cret"
    # ...and the non-secret half is unchanged by the merge.
    assert "s3cret" not in json.dumps(inputs)


def test_a_secret_must_still_be_a_declared_xsecret_input() -> None:
    """config_ref is not a licence to introduce names the author never wrote."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"app_user": {"type": "string"}},
        "required": ["app_user"],
    }
    spec = _spec(
        {
            "key": "write",
            "action": "file.render",
            "params": {
                "path": "/opt/opspilot/apps/x/app.conf",
                "kind": "file",
                "content": "x={{ inputs.app_user }}",
            },
        }
    )
    with pytest.raises(TemplateError, match="not declared"):
        build_template_plan(
            spec, schema, {"app_user": "svc"}, secrets={"not_declared": "x"}
        )


def test_a_secret_cannot_fill_a_field_the_author_did_not_mark() -> None:
    """Otherwise config_ref would be a second, unaudited inputs channel."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"app_user": {"type": "string"}},
        "required": ["app_user"],
    }
    spec = _spec(
        {
            "key": "write",
            "action": "file.render",
            "params": {
                "path": "/opt/opspilot/apps/x/app.conf",
                "kind": "file",
                "content": "u={{ inputs.app_user }}",
            },
        }
    )
    with pytest.raises(TemplateError, match="not declared x-secret"):
        build_template_plan(
            spec, schema, {"app_user": "svc"}, secrets={"app_user": "evil"}
        )
