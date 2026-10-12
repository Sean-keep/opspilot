"""nginx_site must load, validate, and dry-run clean."""

from __future__ import annotations

from pathlib import Path

from shared.actions.base import spec_of
from shared.actions.render import ALLOWED_PATH_PREFIXES, validate_path
from shared.templates import (
    LoadedTemplate,
    compute_checksum,
    dry_run_params,
    validate_template,
)


def test_the_template_directory_is_named_after_the_template(template_dir: Path) -> None:
    assert template_dir.name == "nginx-site"


def test_it_loads_and_validates_without_errors(loaded: LoadedTemplate) -> None:
    assert loaded.name == "nginx-site"
    assert validate_template(loaded.spec, loaded.inputs_schema) == []
    assert dry_run_params(loaded.spec, loaded.inputs_schema) == {}


def test_every_step_is_a_registered_action(loaded: LoadedTemplate) -> None:
    for step in loaded.spec.steps:
        spec_of(step.action)


def test_the_site_path_is_confined_to_nginx_conf_d(loaded: LoadedTemplate) -> None:
    props = loaded.inputs_schema["properties"]
    pattern = props["site_path"]["pattern"]
    assert pattern.startswith("^/etc/nginx/conf")
    # The default must itself satisfy the pattern, or every caller starts broken.
    assert props["site_path"]["default"].startswith("/etc/nginx/conf.d/")
    validate_path(props["site_path"]["default"])
    assert props["site_path"]["default"].startswith(ALLOWED_PATH_PREFIXES)


def test_the_rendered_config_is_the_only_payload(loaded: LoadedTemplate) -> None:
    """The server block is the whole reason this template exists.

    It must reach the host on stdin, never as an argv token — argv is logged
    and persisted, and a vhost is not sensitive but the discipline is.
    """
    steps = {s.key: s for s in loaded.spec.steps}
    assert steps["render_site"].action == "file.render"
    assert "content" in steps["render_site"].params
    assert "{{ inputs.server_name }}" in steps["render_site"].params["content"]


def test_the_config_keeps_nginx_variables_unsubstituted(loaded: LoadedTemplate) -> None:
    """``$host`` is an nginx variable, not a template placeholder.

    The strict renderer rejects ``${...}`` and ``$(...)`` but has no opinion on
    a bare ``$name``, which is exactly right here — a template that could not
    emit nginx config would be useless.
    """
    content = loaded.spec.steps[3].params["content"]
    assert "$host" in content
    assert "$remote_addr" in content
    assert "{{ inputs.server_name }}" in content


def test_the_checksum_covers_the_identity_files(loaded: LoadedTemplate) -> None:
    assert loaded.checksum == compute_checksum(loaded.files)
    assert set(loaded.files) == {"template.yaml", "schemas/inputs.schema.json"}
