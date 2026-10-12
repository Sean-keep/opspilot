"""Audit trail writes.

Every privileged operation lands here. Snapshots are redacted on the way in, so
an audit row can never become a secret-leak vector even if a caller is careless.

**Audit writes are their own transaction.** They must survive the request that
produced them — including a request that ends in ``401`` or ``403``. If audit
shared the request session, FastAPI's dependency teardown would roll the row
back together with the failed operation, and the whole point of the trail (we
can see who tried what and was refused) would be lost.
"""

from __future__ import annotations

from typing import Any

from shared.redaction import redact
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.audit.models import (
    RESULT_DENIED,
    RESULT_FAILURE,
    RESULT_SUCCESS,
    AuditLog,
)
from backend.app.core.logging import get_logger
from backend.app.db.session import get_session_factory

log = get_logger(__name__)

__all__ = [
    "RESULT_DENIED",
    "RESULT_FAILURE",
    "RESULT_SUCCESS",
    "AuditService",
    "write_audit",
]


def _scrub(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    scrubbed = redact(snapshot)
    return scrubbed if isinstance(scrubbed, dict) else {"value": scrubbed}


def write_audit(
    action: str,
    *,
    actor_id: int | None = None,
    actor_name: str = "",
    target_type: str = "",
    target_id: str = "",
    result: str = RESULT_SUCCESS,
    detail: str = "",
    request_id: str = "",
    task_id: int | None = None,
    snapshot: dict[str, Any] | None = None,
) -> None:
    """Append one audit row in its own committed transaction.

    Use this from request handlers and from the worker. It never raises: a
    failure to audit must not turn a legitimate ``403`` into a ``500``.
    """
    try:
        factory = get_session_factory()
        session = factory()
        try:
            session.add(
                AuditLog(
                    actor_id=actor_id,
                    actor_name=actor_name,
                    action=action,
                    target_type=target_type,
                    target_id=str(target_id) if target_id != "" else "",
                    result=result,
                    detail=detail,
                    request_id=request_id,
                    task_id=task_id,
                    snapshot=_scrub(snapshot),
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            log.exception("audit.write_failed", action=action)
        finally:
            session.close()
    except Exception:
        log.exception("audit.write_failed", action=action)

    log.info(
        "audit",
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        result=result,
        request_id=request_id,
    )


class AuditService:
    """Append-only audit writes bound to a session, plus queries.

    ``record`` writes inside the caller's transaction — use it when the audit
    row is part of a larger unit of work that should commit or roll back
    together. Prefer :func:`write_audit` when the row must survive regardless.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        action: str,
        *,
        actor_id: int | None = None,
        actor_name: str = "",
        target_type: str = "",
        target_id: str = "",
        result: str = RESULT_SUCCESS,
        detail: str = "",
        request_id: str = "",
        task_id: int | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Write one audit row inside the current transaction."""
        row = AuditLog(
            actor_id=actor_id,
            actor_name=actor_name,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id != "" else "",
            result=result,
            detail=detail,
            request_id=request_id,
            task_id=task_id,
            snapshot=_scrub(snapshot),
        )
        self.session.add(row)
        self.session.flush()
        log.info(
            "audit",
            action=action,
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            result=result,
            request_id=request_id,
        )
        return row

    def list_recent(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        action: str | None = None,
        actor_id: int | None = None,
        target_type: str | None = None,
    ) -> list[AuditLog]:
        """Most recent audit rows first, optionally filtered."""
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).offset(offset).limit(min(limit, 500))
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if target_type:
            stmt = stmt.where(AuditLog.target_type == target_type)
        return list(self.session.scalars(stmt))
