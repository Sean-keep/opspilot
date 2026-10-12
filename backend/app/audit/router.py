"""Audit query endpoints."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from backend.app.audit.service import AuditService
from backend.app.auth.deps import DbSession, require_permission

router = APIRouter(prefix="/audit-logs", tags=["audit"])

CanReadAudit = Annotated[object, Depends(require_permission("audit.read"))]


class AuditLogOut(BaseModel):
    """One audit row as the client sees it.

    ``snapshot`` is already redacted at write time; this shape simply carries it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_id: int | None
    actor_name: str
    action: str
    target_type: str
    target_id: str
    result: str
    detail: str
    request_id: str
    task_id: int | None
    snapshot: dict[str, Any] | None
    created_at: dt.datetime


@router.get("", response_model=list[AuditLogOut])
def list_audit_logs(
    # Bound only to run the authorisation dependency; the actor is not needed
    # to answer the query itself.
    _user: CanReadAudit,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None, max_length=64),
    actor_id: int | None = Query(default=None),
    target_type: str | None = Query(default=None, max_length=64),
) -> list[AuditLogOut]:
    """Query the audit trail, newest first. Requires ``audit.read``."""
    rows = AuditService(db).list_recent(
        limit=limit,
        offset=offset,
        action=action,
        actor_id=actor_id,
        target_type=target_type,
    )
    return [AuditLogOut.model_validate(r) for r in rows]
