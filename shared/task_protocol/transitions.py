"""State-machine transition tables and their guards.

Every status change in the task engine goes through :func:`ensure_task_transition`
or :func:`ensure_run_transition`. Illegal moves raise
:class:`backend.app.core.errors.InvalidStateTransition` — or rather its
protocol-level equivalent, because this module cannot import ``backend.app``.

The tables are the single source of truth. Tests assert the table shape
directly so that adding a state without adding its edges is a failing test,
not a production surprise.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from shared.task_protocol.states import (
    RUN_CANCELLED,
    RUN_CANCELLING,
    RUN_EXECUTING,
    RUN_FAILED,
    RUN_PRECHECK,
    RUN_STATES,
    RUN_SUCCESS,
    RUN_TERMINAL,
    RUN_UNKNOWN,
    RUN_VERIFYING,
    RUN_WAITING,
    STEP_CANCELLED,
    STEP_FAILED,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_STATES,
    STEP_SUCCESS,
    STEP_TERMINAL,
    STEP_UNKNOWN,
    TASK_CANCELLED,
    TASK_CANCELLING,
    TASK_FAILED,
    TASK_PARTIAL_SUCCESS,
    TASK_PENDING,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_STATES,
    TASK_SUCCESS,
    TASK_TERMINAL,
)


class InvalidStateTransition(ValueError):
    """A state machine was asked to make a move its table does not allow.

    Subclasses ``ValueError`` so the API layer can re-raise it as its own
    ``InvalidStateTransition`` ``ApiError`` without shared importing backend.
    """

    def __init__(self, kind: str, current: str, target: str) -> None:
        super().__init__(f"illegal {kind} transition: {current} -> {target}")
        self.kind = kind
        self.current = current
        self.target = target


# --------------------------------------------------------------------------
# Task
#
#   PENDING ──► QUEUED ──► RUNNING ──┬──► SUCCESS
#                                    ├──► PARTIAL_SUCCESS
#                                    ├──► FAILED
#                                    └──► CANCELLING ──► CANCELLED
#
# Retry re-opens a finished task by going back to QUEUED. That is an
# *explicit* human action (``POST /tasks/{id}/retry``), never automatic.
# --------------------------------------------------------------------------
TASK_TRANSITIONS: Mapping[str, frozenset[str]] = {
    TASK_PENDING: frozenset({TASK_QUEUED, TASK_CANCELLING, TASK_FAILED}),
    TASK_QUEUED: frozenset({TASK_RUNNING, TASK_CANCELLING, TASK_FAILED}),
    TASK_RUNNING: frozenset(
        {TASK_SUCCESS, TASK_PARTIAL_SUCCESS, TASK_FAILED, TASK_CANCELLING}
    ),
    # Cancel is cooperative: a run may finish before the stop lands, so the
    # task can still resolve to a real outcome while cancelling.
    TASK_CANCELLING: frozenset(
        {
            TASK_CANCELLED,
            TASK_SUCCESS,
            TASK_PARTIAL_SUCCESS,
            TASK_FAILED,
        }
    ),
    # Retry edges. Only reachable via the explicit retry endpoint.
    TASK_SUCCESS: frozenset(),
    TASK_PARTIAL_SUCCESS: frozenset({TASK_QUEUED}),
    TASK_FAILED: frozenset({TASK_QUEUED}),
    TASK_CANCELLED: frozenset({TASK_QUEUED}),
}

# --------------------------------------------------------------------------
# Run
#
#   WAITING ──► PRECHECK ──► EXECUTING ──► VERIFYING ──┬──► SUCCESS
#                                                     └──► FAILED
#   any active ──► CANCELLING ──► CANCELLED
#   any active ──► UNKNOWN   (lease expired and the remote outcome is undetermined)
#
# UNKNOWN is a human-only branch. The only way out is
# ``POST /task-runs/{id}/resolve`` with an explicit verdict.
# --------------------------------------------------------------------------
RUN_TRANSITIONS: Mapping[str, frozenset[str]] = {
    # A run that never started goes straight to CANCELLED when the task is
    # stopped — there is no in-flight work to unwind, so CANCELLING would be
    # theatre.
    RUN_WAITING: frozenset({RUN_PRECHECK, RUN_CANCELLING, RUN_CANCELLED, RUN_FAILED}),
    # `-> WAITING` on an active state is the lease-expiry requeue: the run had
    # not reached a verdict yet, so putting it back on the queue is safe.
    # `-> SUCCESS` from PRECHECK covers runs whose precheck *is* the work.
    RUN_PRECHECK: frozenset(
        {
            RUN_EXECUTING,
            RUN_VERIFYING,
            RUN_SUCCESS,
            RUN_FAILED,
            RUN_CANCELLING,
            RUN_UNKNOWN,
            RUN_WAITING,
        }
    ),
    RUN_EXECUTING: frozenset(
        {
            RUN_VERIFYING,
            RUN_SUCCESS,
            RUN_FAILED,
            RUN_CANCELLING,
            RUN_UNKNOWN,
            RUN_WAITING,
        }
    ),
    RUN_VERIFYING: frozenset(
        {RUN_SUCCESS, RUN_FAILED, RUN_CANCELLING, RUN_UNKNOWN, RUN_WAITING}
    ),
    RUN_CANCELLING: frozenset(
        {RUN_CANCELLED, RUN_FAILED, RUN_SUCCESS, RUN_UNKNOWN, RUN_WAITING}
    ),
    RUN_SUCCESS: frozenset(),
    # Explicit human retry re-opens the run at WAITING and bumps `attempt`.
    RUN_FAILED: frozenset({RUN_WAITING}),
    RUN_CANCELLED: frozenset({RUN_WAITING}),
    # Human verdict only: re-open for retry, or rule it a success/failure.
    # Nothing in the worker may take either edge automatically — there is no
    # path from UNKNOWN back into PRECHECK/EXECUTING without going through
    # WAITING, and the only caller that moves UNKNOWN -> WAITING is the
    # human-facing resolve endpoint.
    RUN_UNKNOWN: frozenset({RUN_WAITING, RUN_SUCCESS, RUN_FAILED}),
}

# --------------------------------------------------------------------------
# Step
# --------------------------------------------------------------------------
STEP_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "PENDING": frozenset({STEP_RUNNING, STEP_CANCELLED, STEP_SKIPPED}),
    STEP_RUNNING: frozenset(
        {STEP_SUCCESS, STEP_FAILED, STEP_CANCELLED, STEP_UNKNOWN}
    ),
    STEP_SUCCESS: frozenset(),
    STEP_FAILED: frozenset(),
    STEP_SKIPPED: frozenset(),
    STEP_CANCELLED: frozenset(),
    # Same rule as run-level UNKNOWN: human verdict, or a fresh attempt.
    STEP_UNKNOWN: frozenset({STEP_SUCCESS, STEP_FAILED, STEP_CANCELLED}),
}


# --------------------------------------------------------------------------
def _ensure(
    table: Mapping[str, frozenset[str]],
    known: frozenset[str],
    kind: str,
    current: str,
    target: str,
) -> None:
    if current not in known:
        raise InvalidStateTransition(kind, current, target)
    if target not in known:
        raise InvalidStateTransition(kind, current, target)
    if target not in table[current]:
        raise InvalidStateTransition(kind, current, target)


def ensure_task_transition(current: str, target: str) -> None:
    """Raise unless ``current -> target`` is an allowed task move."""
    _ensure(TASK_TRANSITIONS, TASK_STATES, "task", current, target)


def ensure_run_transition(current: str, target: str) -> None:
    """Raise unless ``current -> target`` is an allowed run move."""
    _ensure(RUN_TRANSITIONS, RUN_STATES, "run", current, target)


def ensure_step_transition(current: str, target: str) -> None:
    """Raise unless ``current -> target`` is an allowed step move."""
    _ensure(STEP_TRANSITIONS, STEP_STATES, "step", current, target)


def can_transition(table: Mapping[str, frozenset[str]], current: str, target: str) -> bool:
    """Non-raising variant, for UI affordances and recovery probes."""
    return target in table.get(current, frozenset())


# Preferred order when several stepping stones would work. RUNNING first: it is
# the state a task is actually in while its runs are in flight.
_TASK_HOP_ORDER = (TASK_RUNNING, TASK_CANCELLING, TASK_QUEUED, TASK_PENDING)


def task_route(current: str, target: str) -> tuple[str, ...]:
    """Every state to visit, in order, to get from ``current`` to ``target``.

    The task status is derived from run outcomes, so a fold can legitimately
    want to skip a state nobody observed: a one-server task can finish between
    two recomputations and look like ``QUEUED -> SUCCESS``. Rather than adding
    a shortcut edge to the table — which would also permit nonsense like
    ``FAILED -> SUCCESS`` — route through the state that was true in between.

    Only non-terminal states may be used as stepping stones, and only when we
    start from a non-terminal state. A finished task still needs
    ``allow_terminal_repivot`` to move to a *different* finished status.
    """
    if can_transition(TASK_TRANSITIONS, current, target):
        return (target,)
    if current in TASK_TERMINAL:
        raise InvalidStateTransition("task", current, target)

    # Breadth-first over non-terminal stepping stones only.
    seen = {current}
    frontier: list[tuple[str, tuple[str, ...]]] = [(current, ())]
    while frontier:
        state, path = frontier.pop(0)
        ordered = [s for s in _TASK_HOP_ORDER if s in TASK_TRANSITIONS[state]]
        ordered += sorted(TASK_TRANSITIONS[state] - set(ordered))
        for nxt in ordered:
            if nxt in seen or nxt in TASK_TERMINAL:
                continue
            new_path = (*path, nxt)
            if can_transition(TASK_TRANSITIONS, nxt, target):
                return (*new_path, target)
            seen.add(nxt)
            frontier.append((nxt, new_path))
    raise InvalidStateTransition("task", current, target)


def is_task_terminal(status: str) -> bool:
    return status in TASK_TERMINAL


def is_run_terminal(status: str) -> bool:
    return status in RUN_TERMINAL


def is_step_terminal(status: str) -> bool:
    return status in STEP_TERMINAL


# --------------------------------------------------------------------------
def aggregate_task_status(
    run_statuses: Iterable[str], *, cancel_requested: bool = False
) -> str:
    """Fold per-run outcomes into one task status.

    The rules, in the order the design document lists them:

    1. every run ``SUCCESS``                     -> ``SUCCESS``
    2. some ``SUCCESS`` plus other terminal runs -> ``PARTIAL_SUCCESS``
    3. no ``SUCCESS`` and at least one ``FAILED`` -> ``FAILED``
    4. cancellation is the story                -> ``CANCELLED``

    Anything left over is a mix of ``CANCELLED`` / ``UNKNOWN`` with no success
    and no failure. That is not a clean outcome, so it reports
    ``PARTIAL_SUCCESS``: the task is finished but a human has to look at it.
    ``UNKNOWN`` never folds into ``SUCCESS``.
    """
    statuses = [str(s) for s in run_statuses]

    if not statuses:
        # A task with no runs cannot succeed. Treat as failed so it is visible.
        return TASK_FAILED

    if any(s not in RUN_TERMINAL for s in statuses):
        # Still in flight. If the user asked to stop, surface that intent.
        return TASK_CANCELLING if cancel_requested else TASK_RUNNING

    if all(s == RUN_SUCCESS for s in statuses):
        return TASK_SUCCESS

    if any(s == RUN_SUCCESS for s in statuses):
        return TASK_PARTIAL_SUCCESS

    if any(s == RUN_FAILED for s in statuses):
        return TASK_FAILED

    if cancel_requested or all(s == RUN_CANCELLED for s in statuses):
        return TASK_CANCELLED

    # Remaining: only CANCELLED and/or UNKNOWN. Needs human eyes.
    return TASK_PARTIAL_SUCCESS
