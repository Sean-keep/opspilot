"""Strict parameter substitution. Deliberately not a template engine.

``{{ inputs.app_user }}`` is not an expression language. It is one lookup in one
already-validated mapping, and the whole point is that there is nothing else it
can be. A real Jinja environment — even a "sandboxed" one — is an interpreter,
and an interpreter with untrusted input is RCE with extra steps.

So this module does exactly two things:

1. ``find_placeholders`` / ``validate_template_text`` decide whether a template
   is even legal. Anything that is not a bare ``{{ inputs.x }}`` or
   ``{{ server.y }}`` is **rejected outright**, not passed through and not
   escaped. A template that contains ``{{ a | b }}`` never becomes a task.
2. ``render`` replaces the legal placeholders with values from mappings that
   have already been validated, and refuses to substitute a value that is not
   the right *shape* for the field it lands in (username, path, version, ...).

Shell metacharacters in a substituted value are not escaped here. They do not
need to be: values land in an argv list, and ``shared.executor`` quotes each
element for Paramiko's login-shell transport. Two independent layers, because
neither one is enough alone.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "PLACEHOLDER_RE",
    "USERNAME_RE",
    "VALID_VALUE_KEYS",
    "RenderError",
    "find_placeholders",
    "render",
    "validate_path",
    "validate_read_path",
    "validate_template_text",
    "validate_value_for",
]


class RenderError(ValueError):
    """A template, a placeholder, or a substituted value is not acceptable.

    Always a refusal of the whole template — never a partial render. Half a
    rendered template that "mostly worked" is how injection gets in.
    """


# Only these two shapes are legal: a dotted name rooted at `inputs` or `server`.
# Note what is absent: whitespace-flexible forms, filters, indexing, calls.
PLACEHOLDER_RE: Final = re.compile(
    r"\{\{\s*(?P<root>inputs|server)\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\}\}"
)

# Any `{{ ... }}` at all — used to find the ones that are NOT legal.
_ANY_BRACE_RE: Final = re.compile(r"\{\{.*?\}\}", re.DOTALL)

# Statement and comment syntax from template engines. Rejected, not rendered.
_STATEMENT_RE: Final = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.DOTALL)

# Shell expansion. Rejected everywhere in template text — see the module note
# in docs/template-protocol.md about why literal `$` is not available in V1.
_SHELL_EXPAND_RE: Final = re.compile(r"\$\{[^}]*\}|\$\(|`[^`]*`")

USERNAME_RE: Final = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
VERSION_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
SERVICE_NAME_RE: Final = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9@_.:-]{0,127}$")
PACKAGE_RE: Final = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,63}$")
PORT_RE: Final = re.compile(r"^[0-9]{1,5}$")

#: Remote path prefixes a template may write to. Anything else is a traversal
#: attempt or an accident; both must be refused.
ALLOWED_PATH_PREFIXES: Final = (
    "/opt/opspilot/apps/",
    "/opt/opspilot/test/",
    "/etc/systemd/system/",
    "/etc/supervisor/conf.d/",
    "/etc/nginx/",
)

#: Field names that take a value with a restricted shape.
VALID_VALUE_KEYS: Final = frozenset(
    {
        "username",
        "user",
        "path",
        "file_path",
        "owner",
        "group",
        "version",
        "python_version",
        "service",
        "service_name",
        "unit",
        "unit_name",
        "program",
        "package",
        "packages",
        "port",
    }
)


def find_placeholders(text: str) -> list[tuple[str, str]]:
    """Every legal placeholder, as ``(root, name)`` pairs, in source order.

    Does not validate the text; call ``validate_template_text`` first or you
    will silently miss the illegal expressions.
    """
    return [(m.group("root"), m.group("name")) for m in PLACEHOLDER_RE.finditer(text)]


def validate_template_text(text: str) -> None:
    """Reject any template expression that is not a bare dotted lookup.

    Raises ``RenderError`` naming the offending snippet. The message is safe to
    show an operator; it quotes template source, never substituted values.
    """
    if _STATEMENT_RE.search(text):
        raise RenderError(
            "template uses statement or comment syntax ({% ... %} / {# ... #}); "
            "only {{ inputs.x }} and {{ server.y }} are allowed"
        )

    shell_hit = _SHELL_EXPAND_RE.search(text)
    if shell_hit:
        raise RenderError(
            f"template contains shell expansion ({shell_hit.group(0)!r}); "
            "templates are data, not scripts"
        )

    for match in _ANY_BRACE_RE.finditer(text):
        snippet = match.group(0)
        if not PLACEHOLDER_RE.fullmatch(snippet):
            raise RenderError(
                f"unsupported template expression {snippet!r}; "
                "only {{ inputs.x }} and {{ server.y }} are allowed"
            )

    # Peel off every complete `{{ ... }}` and look at what is left. A `{{` that
    # never closed, or a `}}` that never opened, is almost always a broken
    # template expression rather than prose — refuse rather than guess.
    #
    # (An earlier version of this check ran its regex over the whole text, which
    # matched the closing braces of every *legal* placeholder and rejected every
    # valid template. The tests are the reason that is no longer the case.)
    leftover = _ANY_BRACE_RE.sub("", text)
    if "{{" in leftover or "}}" in leftover:
        raise RenderError("template has an unbalanced {{ or }}")


def validate_read_path(value: str) -> str:
    """A path that will only be read or probed.

    Absolute, no traversal, no control characters — but deliberately *not*
    limited to the writable prefixes. Asking how much space is free under
    ``/var`` is legitimate; the write whitelist below is about writes, not
    about what an operator may look at.
    """
    if not value:
        raise RenderError("path must not be empty")
    if not value.startswith("/"):
        raise RenderError(f"path must be absolute: {value!r}")
    if ".." in value.split("/"):
        raise RenderError(f"path must not contain '..': {value!r}")
    if "\x00" in value or "\n" in value:
        raise RenderError("path must not contain control characters")
    return value


def validate_path(value: str) -> str:
    """A remote path that will be **written** must also be inside an allowlist."""
    validate_read_path(value)
    if not any(value.startswith(p) for p in ALLOWED_PATH_PREFIXES):
        allowed = ", ".join(ALLOWED_PATH_PREFIXES)
        raise RenderError(
            f"path {value!r} is outside the allowed prefixes ({allowed})"
        )
    return value


def validate_value_for(field: str, value: Any, *, path_mode: str = "write") -> Any:
    """Constrain a substituted value by the field it is destined for.

    Called per field name found in the template's parameter mapping, so a
    parameter named ``username`` can never receive a path even if the caller's
    schema was loose enough to allow it.

    ``path_mode`` selects which path rule applies to a ``path`` / ``file_path``
    value: ``"write"`` (the default) enforces the write allowlist, while
    ``"read"`` only requires a well-formed absolute path. The distinction
    matters because ``system.check_disk.path`` legitimately probes
    ``/var/lib/mysql`` while ``file.render.path`` must not.
    """
    if field not in VALID_VALUE_KEYS:
        return value

    if field in {"packages"}:
        if not isinstance(value, (list, tuple)):
            raise RenderError(f"{field} must be a list of package names")
        for item in value:
            validate_value_for("package", item)
        return list(value)

    if not isinstance(value, str):
        if field == "port":
            value = str(value)
        else:
            raise RenderError(f"{field} must be a string, got {type(value).__name__}")

    if field in {"username", "user", "owner", "group"}:
        if not USERNAME_RE.match(value):
            raise RenderError(f"invalid {field} {value!r}")
    elif field in {"path", "file_path"}:
        if path_mode == "read":
            validate_read_path(value)
        elif path_mode == "write":
            validate_path(value)
        else:
            raise ValueError(f"unknown path_mode {path_mode!r}")
    elif field in {"version", "python_version"}:
        if not VERSION_RE.match(value):
            raise RenderError(f"invalid {field} {value!r}")
    elif field in {"service", "service_name", "unit", "unit_name", "program"}:
        if not SERVICE_NAME_RE.match(value):
            raise RenderError(f"invalid {field} {value!r}")
    elif field == "package":
        if not PACKAGE_RE.match(value):
            raise RenderError(f"invalid package name {value!r}")
    elif field == "port":
        if not PORT_RE.match(value):
            raise RenderError(f"invalid port {value!r}")
        number = int(value)
        if not 1 <= number <= 65535:
            raise RenderError(f"port out of range: {number}")
    return value


def _lookup(root: str, name: str, inputs: Mapping[str, Any], server: Mapping[str, Any]) -> Any:
    source = inputs if root == "inputs" else server
    if name not in source:
        raise RenderError(f"unknown {root} value {name!r}")
    return source[name]


def render(
    text: str,
    inputs: Mapping[str, Any],
    server: Mapping[str, Any] | None = None,
) -> str:
    """Substitute every legal placeholder. Refuses anything else.

    ``inputs`` and ``server`` must already have been validated; this function
    re-checks the *shape* of each value as it lands, but it is not the place
    where an input schema is enforced.
    """
    validate_template_text(text)
    server_map: Mapping[str, Any] = server or {}

    def replace(match: re.Match[str]) -> str:
        root = match.group("root")
        name = match.group("name")
        value = _lookup(root, name, inputs, server_map)
        checked = validate_value_for(name, value)
        if isinstance(checked, (list, tuple)):
            # A list cannot be spliced into a single argv token safely. Callers
            # that need repetition must declare a list parameter, not join one.
            raise RenderError(
                f"{root}.{name} is a list and cannot be substituted inline"
            )
        return str(checked)

    return PLACEHOLDER_RE.sub(replace, text)
