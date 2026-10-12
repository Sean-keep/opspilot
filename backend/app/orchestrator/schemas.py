"""Request and response shapes for the task API.

Response models carry status, timing, and a redacted detail — never command
output that could hold a secret, and never anything from the credential store.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from shared.redaction import redact_text


class TaskTarget(BaseModel):
    """One server the task should run on, as seen at submit time."""

    server_id: int = Field(gt=0)


class TaskCreate(BaseModel):
    """Submit a task.

    ``kind`` names the work; ``params`` is validated by the kind's own schema
    in S3. For S2 the recognised kinds are the read-only checks that already
    exist behind ``ExecPort``.
    """

    kind: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    targets: list[TaskTarget] = Field(min_length=1, max_length=200)

    @field_validator("kind")
    @classmethod
    def _kind_is_plain(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned or any(c in cleaned for c in " \t\n\r;&|<>()$`\\\"'/*?[]{}~#"):
            raise ValueError("kind must be a plain identifier")
        return cleaned

    @field_validator("title")
    @classmethod
    def _title_is_boring(cls, v: str) -> str:
        return redact_text(v) if v else ""


class TaskStepOut(BaseModel):
    id: int
    step_key: str
    attempt: int
    action: str
    status: str
    idempotency_class: str
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    # Redacted on the way out, belt and braces.
    result: dict[str, Any] | None = None


class TaskRunOut(BaseModel):
    id: int
    task_id: int
    server_id: int
    status: str
    attempt: int
    lease_owner: str = ""
    lease_expires_at: dt.datetime | None = None
    heartbeat_at: dt.datetime | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    error_detail: str = ""
    recovery_note: str = ""
    steps: list[TaskStepOut] = Field(default_factory=list)


class TaskOut(BaseModel):
    id: int
    kind: str
    title: str
    status: str
    idempotency_key: str = ""
    requested_by: int | None = None
    requested_by_name: str = ""
    queued_at: dt.datetime | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    cancel_requested_at: dt.datetime | None = None
    retry_of_task_id: int | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class TaskDetailOut(TaskOut):
    runs: list[TaskRunOut] = Field(default_factory=list)


class TaskLogOut(BaseModel):
    id: int
    run_id: int
    step_id: int | None = None
    seq: int
    ts: dt.datetime
    level: str
    stream: str
    message: str


class TaskLogPage(BaseModel):
    """One page of history. ``next_seq`` is the cursor for the following page."""

    items: list[TaskLogOut]
    next_seq: int
    total: int


class TaskCancelRequest(BaseModel):
    """Cancel needs a reason and an explicit confirmation.

    Cancelling is not a rollback: completed steps stay completed. The reason is
    audited, and the confirmation flag is what the UI's second dialog sets —
    hiding the button is not a control, but neither is a single click.
    """

    reason: str = Field(default="", max_length=500)
    confirm: bool = False


class TaskRetryRequest(BaseModel):
    reason: str = Field(default="", max_length=500)
    # Restrict the retry to these runs. Empty = every non-successful run.
    run_ids: list[int] = Field(default_factory=list, max_length=500)


class RunResolveRequest(BaseModel):
    """Human verdict on a run the recovery pass left ``UNKNOWN``.

    ``verdict="success"`` says "the remote work did land; record it".
    ``verdict="retry"`` says "rule it failed and try again" — this opens a new
    attempt, it does not pretend the old one succeeded.
    """

    verdict: Literal["success", "retry", "failure"]
    reason: str = Field(min_length=1, max_length=500)


class RunResolveOut(BaseModel):
    id: int
    task_id: int
    server_id: int
    status: str
    attempt: int
    recovery_note: str = ""


class TaskEvent(BaseModel):
    """One SSE frame. Deliberately small: the browser re-reads detail over REST."""

    type: str
    task_id: int
    run_id: int | None = None
    status: str | None = None
    seq: int | None = None
    message: str = ""
    ts: dt.datetime | None = None
