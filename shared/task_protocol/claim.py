"""Atomic claim, lease renewal, and lease-expiry recovery.

The claim path is the part of the task engine that breaks first under
concurrency, so it is written as raw, explicit SQL semantics rather than left
to ORM convenience:

* one ``SELECT … FOR UPDATE SKIP LOCKED`` followed by one ``UPDATE``,
  in the **same transaction**
* ``attempt`` increments only on a real re-run — never on claim, heartbeat,
  or lease renewal
* a lease expiring is **not** a failure; see :func:`decide_recovery`

This module owns no SSH and no process lifecycle. It takes a
:class:`sqlalchemy.orm.Session` and returns plain values.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.orm import Session

from shared.dbtypes import utcnow
from shared.redaction import redact
from shared.task_protocol.models import LeaseRecovery, Task, TaskRun, TaskStep
from shared.task_protocol.states import (
    IDEMPOTENCY_CONDITIONAL,
    IDEMPOTENCY_DESTRUCTIVE,
    IDEMPOTENCY_SAFE,
    RUN_PRECHECK,
    RUN_SUCCESS,
    RUN_UNKNOWN,
    RUN_WAITING,
    STEP_SUCCESS,
    STEP_TERMINAL,
    STEP_UNKNOWN,
)
from shared.task_protocol.transitions import (
    aggregate_task_status,
    ensure_run_transition,
    ensure_step_transition,
    is_task_terminal,
    task_route,
)

# --------------------------------------------------------------------------
# Recovery vocabulary
# --------------------------------------------------------------------------
#: The remote process is still making progress.
PROBE_STILL_RUNNING = "still_running"
#: The remote work is gone and we have evidence it succeeded.
PROBE_SUCCEEDED = "succeeded"
#: The remote work is gone and we have **no** evidence of success.
PROBE_FINISHED = "finished"
#: We cannot even talk to the host, so we cannot tell.
PROBE_UNREACHABLE = "unreachable"

PROBE_OUTCOMES: frozenset[str] = frozenset(
    {PROBE_STILL_RUNNING, PROBE_SUCCEEDED, PROBE_FINISHED, PROBE_UNREACHABLE}
)

#: Extend the lease and keep going with the same attempt.
DECISION_RENEW = "renewed"
#: Throw the run back on the queue as a fresh attempt.
DECISION_REQUEUE = "requeued"
#: Stop and wait for a human. Never auto-retried.
DECISION_UNKNOWN = "marked_unknown"
#: The work is already done; record success without re-running anything.
DECISION_SKIP = "skipped"

DECISIONS: frozenset[str] = frozenset(
    {DECISION_RENEW, DECISION_REQUEUE, DECISION_UNKNOWN, DECISION_SKIP}
)


def decide_recovery(
    probe: str,
    idempotency_class: str,
    *,
    precheck_target_state: bool = False,
) -> str:
    """Three-step lease-expiry judgement. Pure — no I/O.

    1. probe the remote
    2. still running -> renew
    3. finished without success evidence -> re-run **only** if the step is
       idempotent; otherwise ``UNKNOWN``
    4. unreachable -> ``UNKNOWN``

    ``UNKNOWN`` is never a "retry later". It is a stop sign: the only way out
    is a human ruling via ``POST /task-runs/{id}/resolve``.
    """
    if probe not in PROBE_OUTCOMES:
        raise ValueError(f"unknown probe outcome: {probe}")
    known = {IDEMPOTENCY_SAFE, IDEMPOTENCY_CONDITIONAL, IDEMPOTENCY_DESTRUCTIVE}
    if idempotency_class not in known:
        raise ValueError(f"unknown idempotency class: {idempotency_class}")

    if probe == PROBE_SUCCEEDED:
        # Evidence of success beats everything: record it and stop.
        return DECISION_SKIP

    if probe == PROBE_STILL_RUNNING:
        return DECISION_RENEW

    if probe == PROBE_UNREACHABLE:
        return DECISION_UNKNOWN

    # PROBE_FINISHED: gone, and no proof it worked.
    if idempotency_class == IDEMPOTENCY_SAFE:
        return DECISION_REQUEUE

    if idempotency_class == IDEMPOTENCY_CONDITIONAL:
        # Re-run only when a precheck says the host is already in the target
        # state (so the step becomes a no-op) — otherwise it is undetermined.
        return DECISION_SKIP if precheck_target_state else DECISION_UNKNOWN

    return DECISION_UNKNOWN


# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ClaimedRun:
    """A run this worker now holds the lease on."""

    run_id: int
    task_id: int
    server_id: int
    attempt: int
    kind: str
    params: dict[str, Any] | None
    server_snapshot: dict[str, Any] | None
    lease_expires_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """What the recovery pass decided for one expired run."""

    run_id: int
    decision: str
    probe: str
    evidence: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
def claim_runs(
    session: Session,
    *,
    owner: str,
    limit: int,
    lease_seconds: int,
) -> list[ClaimedRun]:
    """Atomically claim up to *limit* waiting runs for *owner*.

    Two workers calling this concurrently never get the same run: the
    ``FOR UPDATE SKIP LOCKED`` row locks mean the second worker's select simply
    skips rows the first one holds. The status change and the lease are written
    in the same transaction as the lock, so there is no window where a run is
    "selected but not yet owned".
    """
    if limit < 1:
        return []
    now = utcnow()
    lease_until = now + dt.timedelta(seconds=lease_seconds)

    candidate_ids = list(
        session.execute(
            select(TaskRun.id)
            .where(
                TaskRun.status == RUN_WAITING,
                or_(
                    TaskRun.lease_expires_at.is_(None),
                    TaskRun.lease_expires_at < now,
                ),
            )
            .order_by(TaskRun.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    if not candidate_ids:
        session.rollback()
        return []

    session.execute(
        update(TaskRun)
        .where(TaskRun.id.in_(candidate_ids))
        .values(
            status=RUN_PRECHECK,
            lease_owner=owner,
            lease_expires_at=lease_until,
            heartbeat_at=now,
            started_at=now,  # first claim stamps it; later attempts overwrite
        )
    )
    session.commit()

    rows = session.execute(
        select(TaskRun, Task)
        .join(Task, Task.id == TaskRun.task_id)
        .where(TaskRun.id.in_(candidate_ids))
        .order_by(TaskRun.id)
    ).all()
    return [
        ClaimedRun(
            run_id=run.id,
            task_id=run.task_id,
            server_id=run.server_id,
            attempt=run.attempt,
            kind=task.kind,
            params=dict(task.params_json or {}),
            server_snapshot=dict(run.server_snapshot_json or {}),
            lease_expires_at=run.lease_expires_at,
        )
        for run, task in rows
    ]


def renew_leases(
    session: Session,
    *,
    owner: str,
    run_ids: Sequence[int],
    lease_seconds: int,
) -> int:
    """Heartbeat: push the lease out and confirm we still hold these runs.

    Returns how many rows we actually renewed. A drop means another worker's
    recovery pass took the run away — the caller must stop working on it.
    """
    if not run_ids:
        return 0
    now = utcnow()
    result = session.execute(
        update(TaskRun)
        .where(
            TaskRun.id.in_(list(run_ids)),
            TaskRun.lease_owner == owner,
        )
        .values(
            heartbeat_at=now,
            lease_expires_at=now + dt.timedelta(seconds=lease_seconds),
        )
    )
    session.commit()
    # DML returns a CursorResult at runtime; the plain `Result` stub has no
    # rowcount, so the cast is for the type checker only.
    return int(cast("CursorResult[Any]", result).rowcount or 0)


def release_run(
    session: Session,
    *,
    run_id: int,
    owner: str,
    status: str,
    detail: str = "",
) -> bool:
    """Finish a run we hold: write the terminal status and drop the lease.

    The state machine is consulted first so a bug cannot park a run in a status
    the table does not allow.
    """
    run = session.get(TaskRun, run_id)
    if run is None or run.lease_owner != owner:
        return False
    ensure_run_transition(run.status, status)
    now = utcnow()
    run.status = status
    run.lease_owner = ""
    run.lease_expires_at = None
    run.finished_at = now
    if detail:
        run.error_detail = detail[:8000]
    session.commit()
    return True


def find_expired_runs(
    session: Session,
    *,
    limit: int = 50,
    now: dt.datetime | None = None,
) -> list[TaskRun]:
    """Runs whose lease has lapsed while they were still active.

    These are *candidates* for recovery, not failures. Calling code must run
    :func:`decide_recovery` before changing anything.
    """
    moment = now or utcnow()
    return list(
        session.execute(
            select(TaskRun)
            .where(
                TaskRun.status.in_(["PRECHECK", "EXECUTING", "VERIFYING", "CANCELLING"]),
                TaskRun.lease_expires_at.isnot(None),
                TaskRun.lease_expires_at < moment,
            )
            .order_by(TaskRun.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars()
    )


def apply_recovery(
    session: Session,
    *,
    run_id: int,
    decision: str,
    probe: str,
    evidence: dict[str, Any] | None = None,
    decided_by: str = "",
    detail: str = "",
    lease_seconds: int = 30,
) -> RecoveryOutcome | None:
    """Persist a recovery decision and the run's resulting state.

    ``DECISION_REQUEUE`` puts the run back to ``WAITING`` **without** bumping
    ``attempt`` — the bump belongs to the worker that actually re-executes, so
    that a crash between requeue and re-claim cannot skip a number. The new
    attempt is opened when the run is claimed again and a step is written with
    ``attempt = run.attempt + 1``.

    ``DECISION_UNKNOWN`` moves the run to ``UNKNOWN`` and stops. Nothing in
    this function, or anywhere else in the worker, may move ``UNKNOWN`` to a
    working state.
    """
    if decision not in DECISIONS:
        raise ValueError(f"unknown recovery decision: {decision}")

    run = session.get(TaskRun, run_id)
    if run is None:
        return None

    scrubbed = redact(dict(evidence or {}))
    note = detail or f"lease recovery: {decision} after probe={probe}"

    if decision == DECISION_RENEW:
        # Same state: just push the lease back out. No transition to check.
        run.heartbeat_at = utcnow()
        run.lease_expires_at = utcnow() + dt.timedelta(seconds=lease_seconds)
    elif decision == DECISION_SKIP:
        ensure_run_transition(run.status, RUN_SUCCESS)
        run.status = RUN_SUCCESS
        run.finished_at = utcnow()
        run.lease_owner = ""
        run.lease_expires_at = None
    elif decision == DECISION_REQUEUE:
        ensure_run_transition(run.status, RUN_WAITING)
        run.status = RUN_WAITING
        run.lease_owner = ""
        run.lease_expires_at = None
    elif decision == DECISION_UNKNOWN:
        ensure_run_transition(run.status, RUN_UNKNOWN)
        run.status = RUN_UNKNOWN
        run.finished_at = utcnow()
        run.lease_owner = ""
        run.lease_expires_at = None

    # A step may only say ``RUNNING`` while something is actually executing it.
    # ``renew`` is the one decision where that is still true — the probe found
    # the remote work alive, so leave its step alone. Every other decision ends
    # the work here, and a step left claiming to run is a lie: a query counting
    # in-flight steps would believe something is happening that is not. (Seen
    # for real: a deploy killed mid-``ensure_user`` recovered the run to
    # ``UNKNOWN`` while the step still read ``RUNNING``.)
    #
    # The status each open step takes is the best verdict available, never a
    # new one — ``STEP_RUNNING -> STEP_UNKNOWN`` and ``-> STEP_SUCCESS`` are
    # both legal moves in ``STEP_TRANSITIONS``:
    #   requeue / unknown -> UNKNOWN   "we never saw the end of this"
    #   skip              -> SUCCESS   the run-level evidence said it worked
    #
    # On ``requeue`` this also keeps the abandoned attempt honest: its step
    # rows stay at their own ``attempt`` number, just no longer mid-flight.
    if decision != DECISION_RENEW:
        closing = STEP_SUCCESS if decision == DECISION_SKIP else STEP_UNKNOWN
        for step in session.execute(
            select(TaskStep).where(
                TaskStep.run_id == run.id,
                TaskStep.status.notin_(sorted(STEP_TERMINAL)),
            )
        ).scalars():
            ensure_step_transition(step.status, closing)
            step.status = closing
            step.finished_at = utcnow()

    run.recovery_note = note[:8000]
    session.add(
        LeaseRecovery(
            run_id=run.id,
            decision=decision,
            evidence=scrubbed if isinstance(scrubbed, dict) else {"value": scrubbed},
            decided_by=decided_by,
        )
    )
    session.commit()
    return RecoveryOutcome(
        run_id=run.id,
        decision=decision,
        probe=probe,
        evidence=scrubbed if isinstance(scrubbed, dict) else {},
    )


# --------------------------------------------------------------------------
def recompute_task_status(
    session: Session,
    *,
    task_id: int,
    allow_terminal_repivot: bool = False,
) -> str | None:
    """Fold the run outcomes back into the task status.

    Called after any run reaches a terminal state. Uses the shared aggregation
    rules so the API and the worker can never disagree about what ``FAILED``
    means.

    ``allow_terminal_repivot`` exists for exactly one caller: the human
    ``resolve_run`` endpoint. Ruling an ``UNKNOWN`` run changes the facts a
    finished task was classified on, so a finished task may move to a
    *different* finished status. Nothing automatic may set this flag — the
    transition table would otherwise forbid ``FAILED -> SUCCESS``, and rightly
    so.
    """
    task = session.get(Task, task_id)
    if task is None:
        return None

    runs = list(
        session.execute(select(TaskRun).where(TaskRun.task_id == task_id)).scalars()
    )
    cancel_requested = task.cancel_requested_at is not None
    target = aggregate_task_status(
        [r.status for r in runs], cancel_requested=cancel_requested
    )
    if target == task.status:
        return target

    route: tuple[str, ...]
    if allow_terminal_repivot and is_task_terminal(task.status) and is_task_terminal(target):
        # Human re-verdict: the old classification was based on incomplete
        # information. Skip the automatic-transition guard on purpose.
        route = (target,)
    else:
        # A fold may want to skip a state nobody observed (a one-server task can
        # finish between two recomputations). Walk the legal path instead of
        # adding a shortcut edge to the table.
        route = task_route(task.status, target)

    now = utcnow()
    for step in route:
        task.status = step
        if task.started_at is None and step in {
            "RUNNING",
            "SUCCESS",
            "PARTIAL_SUCCESS",
            "FAILED",
            "CANCELLING",
        }:
            task.started_at = now
    if target in {"SUCCESS", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"}:
        task.finished_at = now
    else:
        task.finished_at = None
    session.commit()
    return target
