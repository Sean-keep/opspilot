"""Task API: submit, observe, stop, retry, and rule on UNKNOWN runs.

Plus the SSE stream at ``/tasks/{id}/events``. The stream authenticates through
a query parameter because ``EventSource`` cannot set an ``Authorization``
header — a documented trade-off in docs/security-model.md, and the endpoint is
read-only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from shared.redaction import redact, redact_text
from shared.task_protocol import TaskLog, is_run_terminal, is_task_terminal
from sqlalchemy import select

from backend.app.api.routing import AppRouter
from backend.app.auth.deps import DbSession, require_permission
from backend.app.auth.models import User
from backend.app.auth.service import AuthService
from backend.app.core.errors import Forbidden, Unauthenticated
from backend.app.core.security import decode_access_token
from backend.app.orchestrator.schemas import (
    RunResolveOut,
    RunResolveRequest,
    TaskCancelRequest,
    TaskCreate,
    TaskDetailOut,
    TaskLogOut,
    TaskLogPage,
    TaskOut,
    TaskRetryRequest,
    TaskRunOut,
)
from backend.app.orchestrator.service import TaskService

router = AppRouter(prefix="/tasks", tags=["tasks"])

CanRead = Annotated[User, Depends(require_permission("task.read"))]
CanCreate = Annotated[User, Depends(require_permission("task.create"))]
CanCancel = Annotated[User, Depends(require_permission("task.cancel"))]
CanRetry = Annotated[User, Depends(require_permission("task.retry"))]
CanResolve = Annotated[User, Depends(require_permission("task.resolve"))]


def _service(db: DbSession) -> TaskService:
    return TaskService(db)


ServiceDep = Annotated[TaskService, Depends(_service)]


def _task_detail(service: TaskService, task_id: int) -> TaskDetailOut:
    task = service.get(task_id)
    out = service.to_out(task)
    return TaskDetailOut(
        **out,
        runs=[TaskRunOut(**service.run_to_out(r)) for r in service.runs_for(task_id)],
    )


# --------------------------------------------------------------------------
@router.post("", response_model=TaskOut, status_code=201)
def create_task(
    payload: TaskCreate,
    user: CanCreate,
    service: ServiceDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TaskOut:
    """Submit work to one or more servers.

    Send an ``Idempotency-Key`` header to make retries of the same request
    safe: the original task comes back with status **200** instead of a second
    task being queued. Reusing a key with a *different* body is a 409.
    """
    body = {
        "kind": payload.kind,
        "title": payload.title,
        "params": payload.params,
        "targets": [t.server_id for t in payload.targets],
    }
    request_hash = ""
    if idempotency_key:
        scrubbed = redact(body)
        canonical = json.dumps(
            scrubbed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    task, created = service.create(
        kind=payload.kind,
        title=payload.title,
        params=payload.params,
        server_ids=[t.server_id for t in payload.targets],
        actor_id=user.id,
        actor_name=user.username,
        idempotency_key=idempotency_key or "",
        request_hash=request_hash,
    )
    if not created:
        # Replay: the client already has this task. 200, not 201.
        response.status_code = 200
    return TaskOut(**service.to_out(task))


@router.get("", response_model=list[TaskOut])
def list_tasks(
    _user: CanRead,
    service: ServiceDep,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[TaskOut]:
    return [
        TaskOut(**service.to_out(t))
        for t in service.list_all(status=status, limit=limit, offset=offset)
    ]


@router.get("/{task_id}", response_model=TaskDetailOut)
def get_task(task_id: int, _user: CanRead, service: ServiceDep) -> TaskDetailOut:
    return _task_detail(service, task_id)


@router.get("/{task_id}/runs", response_model=list[TaskRunOut])
def list_runs(task_id: int, _user: CanRead, service: ServiceDep) -> list[TaskRunOut]:
    service.get(task_id)
    return [TaskRunOut(**service.run_to_out(r)) for r in service.runs_for(task_id)]


@router.post("/{task_id}/cancel", response_model=TaskOut)
def cancel_task(
    task_id: int,
    payload: TaskCancelRequest,
    user: CanCancel,
    service: ServiceDep,
) -> TaskOut:
    """Request cancellation.

    Cancelling stops further work; it does **not** undo completed steps.
    Rolling back is a separate, explicitly-requested task. ``confirm`` must be
    true — this is enforced here, not in the browser.
    """
    task = service.cancel(
        task_id,
        actor_id=user.id,
        actor_name=user.username,
        reason=payload.reason,
        confirm=payload.confirm,
    )
    return TaskOut(**service.to_out(task))


@router.post("/{task_id}/retry", response_model=TaskOut)
def retry_task(
    task_id: int,
    payload: TaskRetryRequest,
    user: CanRetry,
    service: ServiceDep,
) -> TaskOut:
    """Re-open finished runs as a new attempt.

    Runs in ``UNKNOWN`` are refused: they need a per-run human verdict first.
    """
    task = service.retry(
        task_id,
        actor_id=user.id,
        actor_name=user.username,
        reason=payload.reason,
        run_ids=payload.run_ids,
    )
    return TaskOut(**service.to_out(task))


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------
run_router = AppRouter(prefix="/task-runs", tags=["tasks"])


@run_router.get("/{run_id}/logs", response_model=TaskLogPage)
def list_logs(
    run_id: int,
    _user: CanRead,
    service: ServiceDep,
    since_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> TaskLogPage:
    """Paginated execution history.

    ``since_seq`` is a cursor, not a page number: pass the ``next_seq`` from
    the previous page. Logs are append-only and ordered by ``seq``, so this
    is stable even while the run is still writing.
    """
    rows, total = service.logs_for(run_id, since_seq=since_seq, limit=limit)
    items = [
        TaskLogOut(
            id=r.id,
            run_id=r.run_id,
            step_id=r.step_id,
            seq=r.seq,
            ts=r.ts,
            level=r.level,
            stream=r.stream,
            # Scrub again on the way out. The worker redacts before insert;
            # doing it here too means a row written before that rule existed
            # still cannot leak through the API.
            message=redact_text(r.message),
        )
        for r in rows
    ]
    next_seq = items[-1].seq if items else since_seq
    return TaskLogPage(items=items, next_seq=next_seq, total=total)


@run_router.post("/{run_id}/resolve", response_model=RunResolveOut)
def resolve_run(
    run_id: int,
    payload: RunResolveRequest,
    user: CanResolve,
    service: ServiceDep,
) -> RunResolveOut:
    """Rule on a run the lease recovery pass left ``UNKNOWN``.

    This is the **only** endpoint that can move a run out of ``UNKNOWN``, and
    it always takes a human verdict plus a reason. There is no bulk form.
    """
    run = service.resolve_run(
        run_id,
        actor_id=user.id,
        actor_name=user.username,
        verdict=payload.verdict,
        reason=payload.reason,
    )
    return RunResolveOut(
        id=run.id,
        task_id=run.task_id,
        server_id=run.server_id,
        status=run.status,
        attempt=run.attempt,
        recovery_note=run.recovery_note,
    )


# --------------------------------------------------------------------------
# SSE
# --------------------------------------------------------------------------
def _sse_format(payload: dict[str, Any], *, event: str = "message", event_id: str = "") -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    body = json.dumps(payload, ensure_ascii=False, default=str)
    # SSE frames are `data: <line>` per line of the payload.
    for line in body.splitlines() or [""]:
        lines.append(f"data: {line}")
    lines.append("")
    return "\n".join(lines) + "\n"


@router.get("/{task_id}/events")
async def task_events(
    task_id: int,
    request: Request,
    service: ServiceDep,
    token: str | None = Query(default=None),
) -> StreamingResponse:
    """Server-sent progress stream.

    **Auth is the ``?token=`` query parameter**, not a header: ``EventSource``
    cannot set headers. The token is the same access token used everywhere
    else, and this endpoint only ever reads. If your reverse proxy can forward
    an ``Authorization`` header to an ``EventSource`` polyfill, switch to that.

    Emits ``snapshot`` once, then ``log`` frames as rows appear, then ``state``
    when a run or the task changes status. Ends with ``done`` when the task is
    in a terminal state.
    """
    if not token:
        raise Unauthenticated("missing access token")
    try:
        token_payload = decode_access_token(token)
    except Exception as exc:
        raise Unauthenticated("invalid access token") from exc

    auth = AuthService(service.session)
    user = auth.get_user_by_id(token_payload.user_id)
    if user is None:
        raise Unauthenticated("account no longer exists")
    if not auth.has_permission(user, "task.read"):
        raise Forbidden("token lacks task.read")

    async def stream() -> AsyncIterator[str]:
        last_log_id = 0
        last_status = ""
        yield _sse_format({"type": "hello", "task_id": task_id}, event="hello")

        while True:
            if await request.is_disconnected():
                break

            task = service.get(task_id)
            runs = service.runs_for(task_id)

            # New log rows since the last poll.
            rows = list(
                service.session.execute(
                    select(TaskLog)
                    .where(TaskLog.run_id.in_([r.id for r in runs] or [0]))
                    .where(TaskLog.id > last_log_id)
                    .order_by(TaskLog.id)
                    .limit(200)
                ).scalars()
            )
            for row in rows:
                last_log_id = max(last_log_id, row.id)
                yield _sse_format(
                    {
                        "type": "log",
                        "task_id": task_id,
                        "run_id": row.run_id,
                        "seq": row.seq,
                        "level": row.level,
                        "stream": row.stream,
                        "message": redact_text(row.message),
                        "ts": row.ts.isoformat(),
                    },
                    event="log",
                    event_id=str(row.id),
                )

            status_blob = {
                "task": task.status,
                "runs": {str(r.id): r.status for r in runs},
            }
            encoded = json.dumps(status_blob, sort_keys=True)
            if encoded != last_status:
                last_status = encoded
                yield _sse_format(
                    {"type": "state", "task_id": task_id, **status_blob},
                    event="state",
                )

            if is_task_terminal(task.status) or (
                bool(runs) and all(is_run_terminal(r.status) for r in runs)
            ):
                # The task status can lag a beat behind its runs; report what
                # is actually known rather than waiting on a recompute that
                # may already have happened in another process.
                yield _sse_format(
                    {"type": "done", "task_id": task_id, "status": task.status},
                    event="done",
                )
                break

            await asyncio.sleep(1.0)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
