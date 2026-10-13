"""Template loading and validation.

A template is a YAML document that names registered actions and binds their
parameters. It is not a scripting language, and this module is the reason it
cannot become one: every expression that is not a bare ``{{ inputs.x }}`` or
``{{ server.y }}`` lookup is refused before a template ever reaches a worker.

Three things are checked, in this order, and a failure at any stage rejects the
whole template rather than the offending step:

1. **Structure.** The YAML parses into :class:`TemplateSpec` with unknown keys
   forbidden, step keys unique, and every ``action`` present in the registry.
2. **Binding.** Every parameter key is a declared field of that action's
   Pydantic model, so a typo cannot silently become an unused argument, and
   every placeholder names something the template actually declares.
3. **Rendering.** Each parameter value is legal template text, and — when the
   input schema can supply probe values — a dry render of the step must satisfy
   the action's parameter model.

The loader never builds a transport. It only reads ``ActionSpec.params``. That
is deliberate: a template that is merely *loaded* must not be able to open a
socket, which is why :func:`validate_template` takes no ``ExecPort`` at all.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from shared.actions.base import UnknownAction, spec_of
from shared.actions.render import (
    PLACEHOLDER_RE,
    RenderError,
    find_placeholders,
    render,
    validate_read_path,
    validate_read_path_or_empty,
    validate_template_text,
    validate_value_for,
)
from shared.deploy_plan import PlanStep

__all__ = [
    "DEPRECATED",
    "DRAFT",
    "PUBLISHED",
    "SERVER_VALUE_KEYS",
    "TEMPLATE_STATUSES",
    "TESTED",
    "VALIDATED",
    "LoadedTemplate",
    "TemplateApplyParams",
    "TemplateError",
    "TemplateRef",
    "TemplateSpec",
    "TemplateStep",
    "build_template_plan",
    "compute_checksum",
    "dry_run_params",
    "load_template",
    "plan_template_apply",
    "validate_apply_inputs",
    "validate_template",
]


# --------------------------------------------------------------------------
# Version lifecycle
# --------------------------------------------------------------------------
DRAFT = "DRAFT"
VALIDATED = "VALIDATED"
TESTED = "TESTED"
PUBLISHED = "PUBLISHED"
DEPRECATED = "DEPRECATED"

TEMPLATE_STATUSES = (DRAFT, VALIDATED, TESTED, PUBLISHED, DEPRECATED)

#: The only forward edges. Anything else is a refused transition.
STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    DRAFT: frozenset({VALIDATED}),
    VALIDATED: frozenset({TESTED, DEPRECATED}),
    TESTED: frozenset({PUBLISHED, DEPRECATED}),
    PUBLISHED: frozenset({DEPRECATED}),
    DEPRECATED: frozenset(),
}


class TemplateError(ValueError):
    """A template is malformed, unsafe, or references something it may not.

    Always a refusal of the whole template. Half a template that "mostly
    validated" is how a bad step gets executed.
    """


# --------------------------------------------------------------------------
# What a template may read from the target server
# --------------------------------------------------------------------------
#: Flat names usable as ``{{ server.<name> }}``. Mirrors ``ServerOut`` /
#: ``FactsOut`` — an operator-visible fact, never a credential.
SERVER_VALUE_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "name",
        "hostname",
        "port",
        "ssh_user",
        "description",
        "status",
        "os_name",
        "os_version",
        "kernel",
        "arch",
        "cpu_count",
        "cpu_model",
        "memory_total_mb",
        "memory_available_mb",
        "disk_total_gb",
        "disk_available_gb",
    }
)

_STEP_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------
class TemplateStep(BaseModel):
    """One action invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> TemplateStep:
        if not _STEP_KEY_RE.match(self.key):
            raise ValueError(f"step key {self.key!r} must match {_STEP_KEY_RE.pattern}")
        return self


class TemplateSpec(BaseModel):
    """The whole ``template.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: Literal["v1"]
    name: str
    version: str
    description: str = ""
    inputs_schema: str = "schemas/inputs.schema.json"
    steps: tuple[TemplateStep, ...]

    @model_validator(mode="after")
    def _check(self) -> TemplateSpec:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"template name {self.name!r} must match {_NAME_RE.pattern}")
        if not _VERSION_RE.match(self.version):
            raise ValueError(f"template version {self.version!r} is not a version")
        if not self.steps:
            raise ValueError("a template must have at least one step")
        keys = [step.key for step in self.steps]
        if len(set(keys)) != len(keys):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            raise ValueError(f"duplicate step keys: {dupes}")
        return self


@dataclass(frozen=True)
class LoadedTemplate:
    """A template plus the files it was loaded from."""

    spec: TemplateSpec
    root: Path
    inputs_schema: dict[str, Any]
    checksum: str
    files: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def version(self) -> str:
        return self.spec.version


# --------------------------------------------------------------------------
# Checksum
# --------------------------------------------------------------------------
#: Files that make up a template's identity. Tests do not: they are not part of
#: what runs on a managed host.
_CHECKSUM_GLOBS = ("template.yaml", "schemas/*.json")


def compute_checksum(files: Mapping[str, str]) -> str:
    """SHA-256 over a canonical rendering of the template's identity files.

    Order-independent and newline-normalised, so moving a file between editors
    does not change the checksum while editing its contents does.
    """
    digest = hashlib.sha256()
    for relpath in sorted(files):
        content = files[relpath].replace("\r\n", "\n")
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _path_mode(model_field: Any) -> str:
    """``"read"`` or ``"write"`` for a parameter, from its declared type.

    A field is a read path when its validator is one of the two read rules; the
    write rule (``validate_path``) is the default. Driving this off the field's
    *type* rather than its name is what lets ``system.check_disk.path`` probe
    ``/opt/opspilot/apps`` while ``file.render.path`` still has to stay inside
    the write allowlist.
    """
    for meta in getattr(model_field, "metadata", ()) or ():
        if getattr(meta, "func", None) in (validate_read_path, validate_read_path_or_empty):
            return "read"
    return "write"


# --------------------------------------------------------------------------
# Walking parameters
# --------------------------------------------------------------------------
def _iter_strings(node: Any, path: str = "params") -> Iterator[tuple[str, str]]:
    """Every string leaf inside a parameter mapping, with a dotted path."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for key, value in node.items():
            yield from _iter_strings(value, f"{path}.{key}")
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for index, value in enumerate(node):
            yield from _iter_strings(value, f"{path}[{index}]")


def _map_strings(node: Any, transform: Any, path: str = "params") -> Any:
    """Rebuild a parameter structure with each string leaf transformed."""
    if isinstance(node, str):
        return transform(path, node)
    if isinstance(node, Mapping):
        return {
            key: _map_strings(value, transform, f"{path}.{key}")
            for key, value in node.items()
        }
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        return [
            _map_strings(value, transform, f"{path}[{index}]")
            for index, value in enumerate(node)
        ]
    return node


def _declared_input_names(schema: Mapping[str, Any]) -> frozenset[str]:
    props = schema.get("properties")
    if not isinstance(props, Mapping):
        return frozenset()
    return frozenset(str(k) for k in props)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def validate_template(
    spec: TemplateSpec,
    inputs_schema: Mapping[str, Any],
) -> list[str]:
    """Return every problem with *spec*. An empty list means it is safe to load.

    Deliberately returns a list instead of raising on the first error: an
    operator fixing a template wants to see all of it, and none of these checks
    are load-bearing on each other.
    """
    errors: list[str] = []
    declared = _declared_input_names(inputs_schema)

    for step in spec.steps:
        try:
            action_spec = spec_of(step.action)
        except UnknownAction as exc:
            errors.append(f"step {step.key!r}: {exc}")
            continue

        known_fields = action_spec.params.model_fields
        for key in step.params:
            if key not in known_fields:
                errors.append(
                    f"step {step.key!r}: {step.action} has no parameter {key!r} "
                    f"(known: {', '.join(sorted(known_fields)) or '<none>'})"
                )

        for path, text in _iter_strings(step.params):
            try:
                validate_template_text(text)
            except RenderError as exc:
                errors.append(f"step {step.key!r} {path}: {exc}")
                continue

            for root, name in find_placeholders(text):
                if root == "inputs" and name not in declared:
                    errors.append(
                        f"step {step.key!r} {path}: inputs.{name} is not declared in "
                        f"{spec.inputs_schema}"
                    )
                elif root == "server" and name not in SERVER_VALUE_KEYS:
                    errors.append(
                        f"step {step.key!r} {path}: server.{name} is not a readable "
                        "server field"
                    )

        # Literal values still get the per-field shape check now, so a template
        # cannot ship a hard-coded `/etc/shadow` and only find out at run time.
        # The path rule is taken from the action's declared field type, not from
        # the parameter name alone — see `_path_mode`.
        for path, text in _iter_strings(step.params):
            if PLACEHOLDER_RE.search(text):
                continue
            leaf = path.rsplit(".", 1)[-1].split("[", 1)[0]
            try:
                validate_value_for(leaf, text, path_mode=_path_mode(known_fields.get(leaf)))
            except RenderError as exc:
                errors.append(f"step {step.key!r} {path}: {exc}")

    return errors


def _probe_value(name: str, prop: Mapping[str, Any]) -> Any:
    """A value to dry-render with, when the schema can supply one.

    ``default`` / ``const`` / ``examples`` / ``enum`` are authoritative. A bare
    ``pattern`` is not: synthesising a string to match an arbitrary regex is not
    something this build pretends to do.
    """
    for key in ("default", "const"):
        if key in prop:
            return prop[key]
    if prop.get("examples"):
        return prop["examples"][0]
    if prop.get("enum"):
        return prop["enum"][0]
    if prop.get("type") == "boolean":
        return False
    if prop.get("type") == "integer":
        return 1
    if prop.get("type") == "number":
        return 1.0
    if prop.get("type") == "array":
        return []
    raise LookupError(f"no probe value for input {name!r}")


def dry_run_params(
    spec: TemplateSpec,
    inputs_schema: Mapping[str, Any],
    server: Mapping[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Render each step with probe inputs and validate the result.

    This is the check that catches "the template parses, but the action would
    have refused the value". Returns ``{step_key: [problems]}`` — only the steps
    that failed are present.

    Steps whose inputs have no probe value are reported under the key ``_probe``
    rather than silently skipped.
    """
    props = inputs_schema.get("properties")
    if not isinstance(props, Mapping):
        return {"_probe": ["inputs schema has no properties"]}

    probes: dict[str, Any] = {}
    missing: list[str] = []
    for name in sorted(_declared_input_names(inputs_schema)):
        try:
            probes[name] = _probe_value(name, dict(props[name]))
        except LookupError:
            missing.append(name)

    server_probes: dict[str, Any] = {
        "id": 1,
        "name": "probe",
        "hostname": "probe.example.internal",
        "port": 22,
        "ssh_user": "opstest",
        "description": "",
        "status": "active",
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "kernel": "6.8.0",
        "arch": "x86_64",
        "cpu_count": 4,
        "cpu_model": "probe",
        "memory_total_mb": 16000,
        "memory_available_mb": 8000,
        "disk_total_gb": 100.0,
        "disk_available_gb": 50.0,
        **(server or {}),
    }

    problems: dict[str, list[str]] = {}
    if missing:
        problems["_probe"] = [
            f"no default/enum/example for input {name!r}; dry-run skipped for it"
            for name in missing
        ]

    for step in spec.steps:
        try:
            action_spec = spec_of(step.action)
        except UnknownAction:
            continue  # already reported by validate_template

        def bind(_path: str, text: str) -> Any:
            return render(text, probes, server_probes)

        try:
            rendered = _map_strings(step.params, bind)
            action_spec.params.model_validate(rendered)
        except (RenderError, ValidationError, ValueError) as exc:
            problems.setdefault(step.key, []).append(f"{step.action}: {exc}")

    return problems


def _secret_input_names(inputs_schema: Mapping[str, Any]) -> frozenset[str]:
    """Names the template fills from ``config_ref`` rather than from the body.

    Marked with ``"x-secret": true`` on the property. The ``x-`` prefix is the
    JSON Schema convention for a vendor extension — standard tooling ignores
    it, and this product uses it to keep a password out of ``ops_task_runs``.
    """
    props = inputs_schema.get("properties")
    if not isinstance(props, Mapping):
        return frozenset()
    return frozenset(
        str(k) for k, v in props.items() if isinstance(v, Mapping) and v.get("x-secret") is True
    )


def _check_inputs_against_schema(
    inputs_schema: Mapping[str, Any],
    inputs: Mapping[str, Any],
    secrets: Mapping[str, str] | None = None,
    *,
    pending_secrets: Collection[str] = (),
) -> list[str]:
    """Every required input present, nothing undeclared, nothing missing.

    The schema is JSON Schema, but V1 only relies on ``properties``,
    ``required`` and the ``x-secret`` extension for this gate: the action's own
    Pydantic model is what actually refuses a bad value, and :func:`render`
    re-checks each value's shape as it lands. This check exists so a typo in an
    input name is a refusal at submit time rather than an unresolved
    placeholder at run time.

    ``x-secret`` names may only arrive through *secrets*. Letting them into
    *inputs* would put them in the task body, which is exactly what the
    extension exists to prevent. *pending_secrets* names them when the caller
    is the API and the bundle has not been resolved yet.
    """
    props = inputs_schema.get("properties")
    if not isinstance(props, Mapping):
        return ["inputs schema has no properties"]
    declared = set(props)
    secret_names = _secret_input_names(inputs_schema)
    supplied = set(inputs) | set(secrets or {}) | set(pending_secrets)
    errors: list[str] = []
    required = inputs_schema.get("required") or []
    for name in sorted(set(required) - supplied):
        errors.append(f"missing required input {name!r}")
    for name in sorted(supplied - declared - set(pending_secrets)):
        errors.append(f"input {name!r} is not declared in the inputs schema")
    for name in sorted(set(inputs) & secret_names):
        errors.append(
            f"input {name!r} is declared x-secret and must not be sent in the "
            "task body; name it in config_ref instead"
        )
    for name in sorted(set(secrets or {}) - secret_names):
        errors.append(f"secret {name!r} is not declared x-secret in the inputs schema")
    return errors


def validate_apply_inputs(
    inputs_schema: Mapping[str, Any], inputs: Mapping[str, Any]
) -> list[str]:
    """What the API can check at submit, before any secret exists.

    Same rules as the execution-time gate, except that ``x-secret`` names are
    neither required nor allowed here. The bundle that will fill them is
    resolved on the worker: a submit that demanded the password would be a
    submit that could never succeed, and a submit that accepted it would be
    putting the password in the task body.

    Returns problems rather than raising — the caller turns them into a 422
    with all of them listed.
    """
    return _check_inputs_against_schema(
        inputs_schema,
        inputs,
        secrets=None,
        pending_secrets=_secret_input_names(inputs_schema),
    )


def build_template_plan(
    spec: TemplateSpec,
    inputs_schema: Mapping[str, Any],
    inputs: Mapping[str, Any],
    server: Mapping[str, Any] | None = None,
    *,
    secrets: Mapping[str, str] | None = None,
) -> tuple[PlanStep, ...]:
    """Render a template with concrete inputs into the plan the worker runs.

    This is the seam between "a published template" and "work on one host".
    It is deliberately the same shape as ``build_deploy_plan`` so the worker's
    step runner is shared rather than forked.

    Two things it will not do:

    * **It does not fetch secrets.** Every value in *inputs* must already be
      non-secret. Secret material arrives separately at execution time and is
      passed in as *secrets*, so it never has to travel through
      ``ops_task_steps.params_json`` to reach the host.
    * **It does not authorise.** Whether the caller may run this template is
      the API's decision. This function only refuses input it cannot render.

    *secrets* are merged into the render map but must be **declared** in the
    inputs schema and marked ``"x-secret": true`` — a template cannot smuggle a
    name the author never wrote down, and a password cannot arrive by the same
    door as a hostname.

    Raises :class:`TemplateError` when the inputs do not satisfy the schema or
    when a rendered step's values are refused by the action that will run them.
    """
    errors = _check_inputs_against_schema(inputs_schema, inputs, secrets)
    if errors:
        raise TemplateError("; ".join(errors))
    provided = {**dict(inputs), **dict(secrets or {})}

    plan: list[PlanStep] = []
    for step in spec.steps:
        try:
            action_spec = spec_of(step.action)
        except UnknownAction as exc:  # pragma: no cover - validate_template catches this
            raise TemplateError(f"step {step.key!r}: {exc}") from exc

        def bind(_path: str, text: str) -> Any:
            return render(text, provided, server)

        try:
            rendered = _map_strings(step.params, bind)
            action_spec.params.model_validate(rendered)
        except (RenderError, ValidationError, ValueError) as exc:
            raise TemplateError(f"step {step.key!r} ({step.action}): {exc}") from exc

        plan.append(
            PlanStep(
                key=step.key,
                action=step.action,
                idempotency_class=str(action_spec.idempotency_class),
                params=dict(rendered),
                flips_link=step.action == "app.switch_current",
            )
        )
    return tuple(plan)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def load_template(root: Path | str, *, registry_checked: bool = True) -> LoadedTemplate:
    """Load one template directory and validate it.

    Raises :class:`TemplateError` on anything wrong. Returns a checksum that
    pins exactly the files that were validated — a later edit changes it, which
    is what stops a published version from being swapped out under a running
    task.
    """
    base = Path(root)
    if not base.is_dir():
        raise TemplateError(f"template directory not found: {base}")

    manifest = base / "template.yaml"
    if not manifest.is_file():
        raise TemplateError(f"{base} has no template.yaml")

    files: dict[str, str] = {}
    for pattern in _CHECKSUM_GLOBS:
        for path in sorted(base.glob(pattern)):
            if path.is_file():
                files[path.relative_to(base).as_posix()] = path.read_text(encoding="utf-8")

    if "template.yaml" not in files:
        raise TemplateError(f"{base} has no template.yaml")

    try:
        raw = yaml.safe_load(files["template.yaml"])
    except yaml.YAMLError as exc:
        raise TemplateError(f"{manifest} is not valid YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise TemplateError(f"{manifest} must be a mapping")

    try:
        spec = TemplateSpec.model_validate(dict(raw))
    except ValidationError as exc:
        raise TemplateError(f"{manifest} is not a valid template: {exc}") from exc

    if spec.name != base.name:
        raise TemplateError(
            f"template name {spec.name!r} must match its directory {base.name!r}"
        )

    schema_rel = spec.inputs_schema
    schema_path = base / schema_rel
    if not schema_path.is_file():
        raise TemplateError(f"{manifest} points at a missing inputs schema: {schema_rel}")
    try:
        inputs_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TemplateError(f"{schema_path} is not valid JSON: {exc}") from exc
    if not isinstance(inputs_schema, Mapping):
        raise TemplateError(f"{schema_path} must be a JSON object")
    if inputs_schema.get("type") != "object":
        raise TemplateError(f"{schema_path} must have type: object")
    if inputs_schema.get("additionalProperties") is not False:
        raise TemplateError(
            f"{schema_path} must set additionalProperties: false — a template cannot "
            "accept inputs it never declared"
        )

    if registry_checked:
        errors = validate_template(spec, inputs_schema)
        if errors:
            raise TemplateError(
                f"{manifest} failed validation:\n  - " + "\n  - ".join(errors)
            )

    return LoadedTemplate(
        spec=spec,
        root=base,
        inputs_schema=dict(inputs_schema),
        checksum=compute_checksum(files),
        files=files,
    )


# --------------------------------------------------------------------------
# The template.apply task body
# --------------------------------------------------------------------------
class TemplateRef(BaseModel):
    """Which published body a task is bound to.

    ``checksum`` is the pin: it is what stops a later edit to the catalog from
    changing what a queued task will do. The worker refuses a mismatch rather
    than running whatever is there now.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    checksum: str = Field(min_length=64, max_length=64)


class TemplateApplyParams(BaseModel):
    """The body of a ``template.apply`` task.

    Deliberately self-contained. The approved template body travels with the
    task, so the worker rebuilds exactly the plan the API approved without
    reaching back into the catalog — and the catalog can move on (a new version
    published, an old one deprecated) without changing what a queued run does.

    ``inputs`` are non-secret and are what the API validated. Secret material
    is named only by ``config_ref`` and is resolved on the host side at
    execution time, so it never lands in ``ops_task_runs`` or in a log line.
    """

    model_config = ConfigDict(extra="forbid")

    template: TemplateRef
    spec: TemplateSpec
    inputs_schema: dict[str, Any]
    inputs: dict[str, Any] = Field(default_factory=dict)
    config_ref: str = Field(default="", max_length=255)


def plan_template_apply(
    payload: Mapping[str, Any] | TemplateApplyParams,
    server: Mapping[str, Any] | None = None,
    *,
    secrets: Mapping[str, str] | None = None,
) -> tuple[PlanStep, ...]:
    """Validate one ``template.apply`` body and render the plan it commits to.

    Shared by the API (at submit, with no secrets) and the worker (at
    execution, with the ``config_ref`` bundle resolved). Same function both
    sides, so what one accepts the other will run.
    """
    try:
        params = (
            payload
            if isinstance(payload, TemplateApplyParams)
            else TemplateApplyParams.model_validate(dict(payload))
        )
    except ValidationError as exc:
        raise TemplateError(f"template.apply body is not valid: {exc}") from exc

    try:
        return build_template_plan(
            params.spec,
            params.inputs_schema,
            params.inputs,
            server,
            secrets=secrets,
        )
    except TemplateError as exc:
        raise TemplateError(
            f"template {params.template.name!r} {params.template.version!r}: {exc}"
        ) from exc
