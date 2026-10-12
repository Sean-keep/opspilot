"""Task lifecycle: submit, observe, cancel, retry, and rule on UNKNOWN runs.

The API never executes anything here. It writes rows, the worker picks them up
through the outbox, and this module reads the results back.

Three invariants this module exists to hold:

* **create is atomic** — the task, its runs, and the outbox row commit together
* **duplicate submits return the original task**, they do not enqueue twice
* **UNKNOWN is never auto-retried** — only :meth:`TaskService.resolve_run`
  can move it, and only on an explicit human verdict
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from shared.dbtypes import utcnow
from shared.redaction import redact, redact_text
from shared.task_protocol import (
    RUN_CANCELLED,
    RUN_CANCELLING,
    RUN_FAILED,
    RUN_SUCCESS,
    RUN_UNKNOWN,
    RUN_WAITING,
    TASK_CANCELLED,
    TASK_CANCELLING,
    TASK_FAILED,
    TASK_PARTIAL_SUCCESS,
    TASK_PENDING,
    TASK_QUEUED,
    TASK_SUCCESS,
    InvalidStateTransition,
    OutboxEvent,
    Task,
    TaskLog,
    TaskRun,
    TaskStep,
    ensure_run_transition,
    ensure_task_transition,
    recompute_task_status,
)
from shared.task_protocol.models import ApiIdempotency
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.audit.service import RESULT_SUCCESS, write_audit
from backend.app.core.errors import (
    Conflict,
    NotFound,
    ValidationError,
)
from backend.app.core.errors import (
    InvalidStateTransition as ApiInvalidStateTransition,
)
from backend.app.core.logging import get_logger

log = get_logger(__name__)

IDEMPOTENCY_SCOPE = "POST /api/v1/tasks"

#: Task kinds the S2 engine will actually run. S3 widens this to the Action
#: Registry; until then anything else is rejected at submit time rather than
#: silently sitting in the queue forever.
S2_TASK_KINDS: frozenset[str] = frozenset({"server.check"})


def _hash_request(payload: dict[str, Any]) -> str:
    """Stable digest of a request body, for idempotency replay matching."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _guard(fn: Any, current: str, target: str) -> None:
    """Run a transition guard, re-raising as the API's error type."""
    try:
        fn(current, target)
    except InvalidStateTransition as exc:
        raise ApiInvalidStateTransition(str(exc)) from exc


class TaskService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get(self, task_id: int) -> Task:
        task = self.session.get(Task, task_id)
        if task is None:
            raise NotFound(f"task {task_id} not found")
        return task

    def require_run(self, run_id: int) -> TaskRun:
        run = self.session.get(TaskRun, run_id)
        if run is None:
            raise NotFound(f"run {run_id} not found")
        return run

    def list_all(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Task]:
        stmt = select(Task).order_by(Task.id.desc()).limit(limit).offset(offset)
        if status:
            stmt = stmt.where(Task.status == status)
        return list(self.session.execute(stmt).scalars())

    def runs_for(self, task_id: int) -> list[TaskRun]:
        return list(
            self.session.execute(
                select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.id)
            ).scalars()
        )

    def steps_for(self, run_id: int) -> list[TaskStep]:
        return list(
            self.session.execute(
                select(TaskStep)
                .where(TaskStep.run_id == run_id)
                .order_by(TaskStep.id, TaskStep.attempt)
            ).scalars()
        )

    def logs_for(
        self, run_id: int, *, since_seq: int = 0, limit: int = 200
    ) -> tuple[list[TaskLog], int]:
        self.require_run(run_id)
        total = int(
            self.session.execute(
                select(func.count()).select_from(TaskLog).where(TaskLog.run_id == run_id)
            ).scalar_one()
        )
        rows = list(
            self.session.execute(
                select(TaskLog)
                .where(TaskLog.run_id == run_id, TaskLog.seq > since_seq)
                .order_by(TaskLog.seq)
                .limit(limit)
            ).scalars()
        )
        return rows, total

    def to_out(self, task: Task) -> dict[str, Any]:
        return {
            "id": task.id,
            "kind": task.kind,
            "title": task.title,
            "status": task.status,
            "idempotency_key": task.idempotency_key,
            "requested_by": task.requested_by,
            "requested_by_name": task.requested_by_name,
            "queued_at": task.queued_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "cancel_requested_at": task.cancel_requested_at,
            "retry_of_task_id": task.retry_of_task_id,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    def run_to_out(self, run: TaskRun, *, with_steps: bool = True) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        if with_steps:
            for step in self.steps_for(run.id):
                result = step.result_json
                scrubbed = redact(dict(result)) if result else None
                steps.append(
                    {
                        "id": step.id,
                        "step_key": step.step_key,
                        "attempt": step.attempt,
                        "action": step.action,
                        "status": step.status,
                        "idempotency_class": step.idempotency_class,
                        "started_at": step.started_at,
                        "finished_at": step.finished_at,
                        "exit_code": step.exit_code,
                        "duration_ms": step.duration_ms,
                        "result": scrubbed if isinstance(scrubbed, dict) else None,
                    }
                )
        return {
            "id": run.id,
            "task_id": run.task_id,
            "server_id": run.server_id,
            "status": run.status,
            "attempt": run.attempt,
            "lease_owner": run.lease_owner,
            "lease_expires_at": run.lease_expires_at,
            "heartbeat_at": run.heartbeat_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "error_detail": redact_text(run.error_detail) if run.error_detail else "",
            "recovery_note": redact_text(run.recovery_note) if run.recovery_note else "",
            "steps": steps,
        }

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    def create(
        self,
        *,
        kind: str,
        title: str,
        params: dict[str, Any],
        server_ids: list[int],
        actor_id: int | None,
        actor_name: str,
        idempotency_key: str = "",
        request_hash: str = "",
    ) -> tuple[Task, bool]:
        """Create a task and its runs in one transaction.

        Returns ``(task, created)``. ``created`` is False when an existing row
        was returned because the ``Idempotency-Key`` already landed.

        The outbox row commits in the same transaction as the task, so there is
        no window where the database has work that nothing will pick up.
        """
        if kind not in S2_TASK_KINDS:
            raise ValidationError(
                f"unsupported task kind {kind!r}; S2 accepts {sorted(S2_TASK_KINDS)}"
            )
        if not server_ids:
            raise ValidationError("a task needs at least one target server")

        key = (idempotency_key or "").strip()
        if key:
            existing = self.session.execute(
                select(ApiIdempotency).where(
                    ApiIdempotency.scope == IDEMPOTENCY_SCOPE,
                    ApiIdempotency.idempotency_key == key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.request_hash and request_hash and existing.request_hash != request_hash:
                    raise Conflict(
                        "this Idempotency-Key was already used with a different request body",
                        code="idempotency_key_reuse",
                    )
                body = existing.response_body or {}
                task_id = body.get("id")
                if task_id is None:
                    raise Conflict("idempotent replay is missing the original task")
                return self.get(int(task_id)), False

        now = utcnow()
        task = Task(
            kind=kind,
            title=title or kind,
            status=TASK_PENDING,
            idempotency_key=key,
            params_json=params or {},
            target_snapshot_json={"server_ids": list(server_ids)},
            requested_by=actor_id,
            requested_by_name=actor_name or "",
            queued_at=now,
        )
        self.session.add(task)
        self.session.flush()

        # Freeze the target set. Later group membership changes must not
        # retarget a task that is already queued.
        snapshot = {
            str(sid): {"server_id": sid} for sid in dict.fromkeys(server_ids)
        }
        task.target_snapshot_json = {"server_ids": sorted(snapshot), "servers": snapshot}

        for sid in sorted(snapshot):
            self.session.add(
                TaskRun(
                    task_id=task.id,
                    server_id=sid,
                    server_snapshot_json={"server_id": sid},
                    status=RUN_WAITING,
                    attempt=1,
                )
            )

        self.session.add(
            OutboxEvent(
                topic="task.created",
                payload_json={"task_id": task.id, "kind": kind},
            )
        )

        # PENDING -> QUEUED is a real state move; the runs are already WAITING.
        _guard(ensure_task_transition, task.status, TASK_QUEUED)
        task.status = TASK_QUEUED

        if key:
            self.session.add(
                ApiIdempotency(
                    scope=IDEMPOTENCY_SCOPE,
                    idempotency_key=key,
                    request_hash=request_hash or "",
                    response_status=201,
                    response_body={"id": task.id},
                )
            )

        self.session.commit()

        write_audit(
            "task.create",
            actor_id=actor_id,
            actor_name=actor_name,
            target_type="task",
            target_id=str(task.id),
            result=RESULT_SUCCESS,
            task_id=task.id,
            snapshot={"kind": kind, "targets": sorted(snapshot), "idempotent": bool(key)},
        )
        return task, True

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------
    def cancel(
        self,
        task_id: int,
        *,
        actor_id: int | None,
        actor_name: str,
        reason: str,
        confirm: bool,
    ) -> Task:
        """Ask a task to stop.

        This is **not** a rollback. Steps that already ran stay ran; the only
        thing cancel does is stop further work and record why.

        ``confirm`` must be True. The backend enforces this — hiding the button
        in the UI would not be a control.
        """
        task = self.get(task_id)
        if not confirm:
            raise ValidationError(
                "cancel requires explicit confirmation; set confirm=true"
            )

        if task.status in {TASK_SUCCESS, TASK_FAILED, TASK_CANCELLED, TASK_PARTIAL_SUCCESS}:
            raise Conflict(
                f"task {task_id} is already finished ({task.status})",
                code="task_already_finished",
            )

        _guard(ensure_task_transition, task.status, TASK_CANCELLING)
        now = utcnow()
        task.status = TASK_CANCELLING
        task.cancel_requested_at = now
        task.cancel_requested_by = actor_name or ""

        # Mark runs that have not started so the worker skips them entirely.
        # Runs already in flight get the CANCELLING flag and stop at the next
        # step boundary with remote evidence.
        for run in self.runs_for(task_id):
            if run.status == RUN_WAITING:
                _guard(ensure_run_transition, run.status, RUN_CANCELLED)
                run.status = RUN_CANCELLED
                run.finished_at = now
                run.lease_owner = ""
                run.lease_expires_at = None
            elif run.status not in {RUN_SUCCESS, RUN_FAILED, RUN_CANCELLED, RUN_UNKNOWN}:
                _guard(ensure_run_transition, run.status, RUN_CANCELLING)
                run.status = RUN_CANCELLING

        self.session.add(
            OutboxEvent(
                topic="task.cancel_requested",
                payload_json={"task_id": task.id, "reason": redact_text(reason or "")},
            )
        )
        self.session.commit()

        write_audit(
            "task.cancel",
            actor_id=actor_id,
            actor_name=actor_name,
            target_type="task",
            target_id=str(task.id),
            result=RESULT_SUCCESS,
            task_id=task.id,
            snapshot={"reason": redact_text(reason or ""), "status": task.status},
        )
        return task

    # ------------------------------------------------------------------
    # Retry
    # ------------------------------------------------------------------
    def retry(
        self,
        task_id: int,
        *,
        actor_id: int | None,
        actor_name: str,
        reason: str,
        run_ids: list[int] | None = None,
    ) -> Task:
        """Re-open finished runs as a new attempt.

        A retry **bumps ``attempt``** on the existing run row. It never inserts
        a second run for the same (task, server) pair — that is what the unique
        constraint is for.

        ``UNKNOWN`` runs are deliberately **not** included by default. A human
        must rule on each one via :meth:`resolve_run` before work resumes;
        blanket-retrying them is exactly the failure mode that corrupts a
        database mid-initialisation.
        """
        task = self.get(task_id)
        # SUCCESS is a hard terminal: to do the work again, submit a new task.
        # Retrying a success would blur "this landed" into "we meant to do it".
        if task.status not in {
            TASK_FAILED,
            TASK_PARTIAL_SUCCESS,
            TASK_CANCELLED,
        }:
            raise Conflict(
                f"task {task_id} is {task.status}; only failed, partially "
                "successful, or cancelled tasks can be retried",
                code="task_not_finished",
            )

        wanted = set(run_ids or [])
        candidates = []
        for run in self.runs_for(task_id):
            if wanted and run.id not in wanted:
                continue
            if run.status == RUN_UNKNOWN:
                raise Conflict(
                    f"run {run.id} is UNKNOWN; it needs a human verdict via "
                    "POST /task-runs/{id}/resolve before it can be retried",
                    code="run_unknown_needs_resolution",
                )
            if run.status == RUN_SUCCESS and not wanted:
                continue
            candidates.append(run)

        if not candidates:
            raise Conflict("nothing to retry", code="nothing_to_retry")

        now = utcnow()
        for run in candidates:
            _guard(ensure_run_transition, run.status, RUN_WAITING)
            run.status = RUN_WAITING
            run.attempt += 1
            run.lease_owner = ""
            run.lease_expires_at = None
            run.started_at = None
            run.finished_at = None
            run.error_detail = ""
            run.recovery_note = f"retry #{run.attempt} requested by {actor_name}: "
            run.recovery_note += redact_text(reason or "")[:500]

        _guard(ensure_task_transition, task.status, TASK_QUEUED)
        task.status = TASK_QUEUED
        task.finished_at = None
        task.queued_at = now

        self.session.add(
            OutboxEvent(
                topic="task.created",
                payload_json={"task_id": task.id, "kind": task.kind, "retry": True},
            )
        )
        self.session.commit()

        write_audit(
            "task.retry",
            actor_id=actor_id,
            actor_name=actor_name,
            target_type="task",
            target_id=str(task.id),
            result=RESULT_SUCCESS,
            task_id=task.id,
            snapshot={
                "reason": redact_text(reason or ""),
                "runs": [r.id for r in candidates],
                "attempts": {r.id: r.attempt for r in candidates},
            },
        )
        return task

    # ------------------------------------------------------------------
    # Resolve UNKNOWN
    # ------------------------------------------------------------------
    def resolve_run(
        self,
        run_id: int,
        *,
        actor_id: int | None,
        actor_name: str,
        verdict: str,
        reason: str,
    ) -> TaskRun:
        """Human ruling on a run the recovery pass could not classify.

        * ``success`` — the remote work did land; record it and move on
        * ``failure`` — it did not; mark the run failed
        * ``retry``   — mark it failed and open a new attempt

        There is no fourth option, and nothing else in the codebase may move a
        run out of ``UNKNOWN``.
        """
        run = self.require_run(run_id)
        if run.status != RUN_UNKNOWN:
            raise Conflict(
                f"run {run_id} is {run.status}, not UNKNOWN; nothing to resolve",
                code="run_not_unknown",
            )
        if verdict not in {"success", "failure", "retry"}:
            raise ValidationError(f"unknown verdict {verdict!r}")

        note = f"resolved as {verdict} by {actor_name}: {redact_text(reason)}"
        now = utcnow()

        if verdict == "success":
            _guard(ensure_run_transition, run.status, RUN_SUCCESS)
            run.status = RUN_SUCCESS
            run.finished_at = now
        elif verdict == "failure":
            _guard(ensure_run_transition, run.status, RUN_FAILED)
            run.status = RUN_FAILED
            run.finished_at = now
        else:  # retry
            _guard(ensure_run_transition, run.status, RUN_FAILED)
            run.status = RUN_FAILED
            run.finished_at = now
            # Re-open immediately as a new attempt. The FAILED -> WAITING edge
            # is the human retry path.
            _guard(ensure_run_transition, run.status, RUN_WAITING)
            run.status = RUN_WAITING
            run.attempt += 1
            run.finished_at = None
            run.started_at = None
            run.error_detail = ""
            self.session.add(
                OutboxEvent(
                    topic="task.created",
                    payload_json={"task_id": run.task_id, "kind": "retry", "run_id": run.id},
                )
            )

        run.recovery_note = note[:8000]
        run.lease_owner = ""
        run.lease_expires_at = None
        self.session.commit()

        write_audit(
            "task.resolve",
            actor_id=actor_id,
            actor_name=actor_name,
            target_type="task_run",
            target_id=str(run.id),
            result=RESULT_SUCCESS,
            task_id=run.task_id,
            snapshot={"verdict": verdict, "reason": redact_text(reason), "attempt": run.attempt},
        )

        if verdict != "retry":
            recompute_task_status(
                self.session, task_id=run.task_id, allow_terminal_repivot=True
            )
        return run

    # ------------------------------------------------------------------
    def mark_finished_if_ready(self, task_id: int) -> str | None:
        """Recompute the task status after a run reached a terminal state."""
        return recompute_task_status(self.session, task_id=task_id)
