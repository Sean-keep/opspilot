"""Action Registry: the only way anything runs on a managed host.

A template is a list of *named* actions. There is no generic "run this shell"
action, and V1 must never grow one — see docs/security-model.md. Everything an
operator can do to a server is a name in this module's table plus a Pydantic
model that says what its arguments may be.

Two properties matter more than anything else here:

1. **Unregistered means refused.** ``resolve()`` raises ``UnknownAction``; the
   template loader calls it at load time so a template with a typo never becomes
   a task.
2. **The params model is the only validator.** Actions never read a raw dict, so
   an action cannot be talked into accepting a value its model would reject.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from shared.executor.port import ExecContext, ExecPort, ExecResult

__all__ = [
    "Action",
    "ActionProtocol",
    "ActionResult",
    "ActionSpec",
    "IdempotencyClass",
    "UnknownAction",
    "ValidationError",
    "ensure_argv",
    "get_registry",
    "register",
    "register_all",
    "reset_registry",
    "resolve",
    "spec_of",
]


# Idempotency classes. Same vocabulary as ``ops_task_steps.idempotency_class``.
SAFE = "SAFE"
CONDITIONAL = "CONDITIONAL"
DESTRUCTIVE = "DESTRUCTIVE"

IdempotencyClass = str  # one of the three constants above

_IDEMPOTENCY_CLASSES = frozenset({SAFE, CONDITIONAL, DESTRUCTIVE})


class UnknownAction(LookupError):
    """A template named an action this build does not have.

    Raised at load time. A template that cannot name its actions must never
    reach a worker.
    """


def ensure_argv(argv: Sequence[str]) -> list[str]:
    """The argv shape check, in one place.

    Every command an action runs goes through this. A shell string, a mixed
    list, or an empty argv is refused here rather than being quietly joined —
    the difference between a parameter and a command line is the whole of the
    injection defence.
    """
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a sequence[str] — never a shell string")
    if not argv:
        raise ValueError("argv must not be empty")
    for item in argv:
        if not isinstance(item, str):
            raise TypeError(
                f"argv elements must be str, got {type(item).__name__}"
            )
    return list(argv)


class ParamsModel(BaseModel):
    """Base for every action's parameter model."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What one action did, in a shape safe to persist and show."""

    ok: bool
    # Human summary. Redacted on the way to logs; must not carry secrets.
    summary: str = ""
    # Structured facts for ``ops_task_steps.result_json``. Redacted recursively.
    data: Mapping[str, Any] = field(default_factory=dict)
    # Skip/changed reporting: "no" means the target was already in the wanted
    # state and nothing was touched.
    changed: bool = True
    # argv actually executed, for the audit trail. Never a shell string.
    argv: Sequence[str] = ()

    @classmethod
    def skipped(cls, summary: str, **data: Any) -> ActionResult:
        return cls(ok=True, summary=summary, data=data, changed=False)

    @classmethod
    def failure(cls, summary: str, **data: Any) -> ActionResult:
        return cls(ok=False, summary=summary, data=data, changed=False)

    @classmethod
    def from_exec(cls, result: ExecResult, summary: str = "", **data: Any) -> ActionResult:
        return cls(
            ok=result.ok,
            summary=summary or (result.stdout.strip() or result.stderr.strip())[:500],
            data={**data, "exit_code": result.exit_code},
            changed=result.ok,
            argv=result.argv,
        )


@runtime_checkable
class ActionProtocol(Protocol):
    """Anything the registry will run."""

    params: type[BaseModel]

    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult: ...


class Action[P: BaseModel]:
    """Base class for actions. Subclasses set ``params`` and use ``@register``.

    The ``ExecPort`` is constructor-injected rather than read off ``ctx``, which
    keeps the documented ``run(ctx, params)`` shape while making the transport
    impossible to forget and easy to replace in tests. Template *validation*
    never constructs an Action at all — it only reads ``ActionSpec.params``.

    ``run`` receives the **raw** mapping, not a pre-built model, so there is
    exactly one place a value is validated: ``build_params``, which the action
    calls on its own arguments. A caller cannot hand an action a model it would
    not have produced itself.
    """

    #: Pydantic model for the arguments. Required.
    params: ClassVar[type[BaseModel]] = ParamsModel
    #: Metadata; set by ``@register``.
    spec: ClassVar[ActionSpec]

    def __init__(self, port: ExecPort) -> None:
        self.port = port

    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:  # pragma: no cover
        raise NotImplementedError(f"{type(self).__name__}.run is not implemented")

    @classmethod
    def build_params(cls, raw: Mapping[str, Any]) -> P:
        """Validate a raw parameter mapping. The only way args reach ``run``.

        A classmethod on purpose: validating a template must not require (or
        allow) anyone to build a transport.
        """
        return cast("P", cls.params.model_validate(dict(raw)))


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """Metadata the platform needs before it will even consider running an action."""

    name: str
    params: type[BaseModel]
    #: True only for pure reads. Anything that touches the remote mutably is False.
    safe: bool = False
    idempotency_class: IdempotencyClass = CONDITIONAL
    #: Whether the implementation needs a restricted sudo rule to do its job.
    requires_sudo: bool = False
    #: One-line description, surfaced in the UI and in audit rows.
    description: str = ""

    def __post_init__(self) -> None:
        if self.idempotency_class not in _IDEMPOTENCY_CLASSES:
            raise ValueError(
                f"{self.name}: unknown idempotency_class {self.idempotency_class!r} "
                f"(expected one of {sorted(_IDEMPOTENCY_CLASSES)})"
            )
        if self.safe and self.idempotency_class == DESTRUCTIVE:
            raise ValueError(
                f"{self.name}: a read-only action cannot be DESTRUCTIVE"
            )


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
@dataclass
class _Entry:
    spec: ActionSpec
    factory: type[Action[Any]]


_REGISTRY: dict[str, _Entry] = {}


def register(
    name: str,
    *,
    params: type[BaseModel],
    safe: bool = False,
    idempotency_class: IdempotencyClass = CONDITIONAL,
    requires_sudo: bool = False,
    description: str = "",
) -> Callable[[type[Action[Any]]], type[Action[Any]]]:
    """Class decorator. Registers ``cls`` under *name* and stamps its spec."""

    def decorate(cls: type[Action[Any]]) -> type[Action[Any]]:
        spec = ActionSpec(
            name=name,
            params=params,
            safe=safe,
            idempotency_class=idempotency_class,
            requires_sudo=requires_sudo,
            description=description,
        )
        if name in _REGISTRY:
            raise ValueError(f"action {name!r} is already registered")
        cls.params = params
        cls.spec = spec
        _REGISTRY[name] = _Entry(spec=spec, factory=cls)
        return cls

    return decorate


def register_all() -> None:
    """Load the built-in actions.

    Explicit rather than an import-side-effect sweep: a registry that fills
    itself in when something happens to import a module is a registry nobody can
    reason about, and it makes test isolation depend on collection order.
    """
    from shared.actions import builtin  # noqa: F401 — importing runs the decorators

    if not _REGISTRY:
        raise RuntimeError("register_all() loaded no actions")


def get_registry() -> dict[str, ActionSpec]:
    """A copy of the name → spec table. Used by the template loader and API."""
    return {name: entry.spec for name, entry in sorted(_REGISTRY.items())}


def spec_of(name: str) -> ActionSpec:
    return _require(name).spec


def resolve(name: str, port: ExecPort) -> Action[Any]:
    """Build the action named *name* against *port*.

    Raises ``UnknownAction`` for anything not registered. Template validation
    never calls this — it stops at ``spec_of`` — so a template cannot reach a
    transport just by being loaded.
    """
    return _require(name).factory(port)


def _require(name: str) -> _Entry:
    entry = _REGISTRY.get(name)
    if entry is None:
        known = ", ".join(sorted(_REGISTRY)) or "<none registered>"
        raise UnknownAction(f"unregistered action {name!r}; known actions: {known}")
    return entry


def reset_registry() -> None:
    """Drop every registration. Test-only."""
    _REGISTRY.clear()
