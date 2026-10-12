"""Unified API errors.

The requirements mandate one error shape for every endpoint::

    {"code": "...", "message": "...", "request_id": "..."}

Stack traces, secrets, and sensitive command parameters must never reach the
client. ``ApiError`` carries a stable machine code and a human message; the
handler in ``backend.app.main`` serialises it and nothing else.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """An error safe to return to the client."""

    status_code: int = 400
    code: str = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        # `details` is rendered to the client. Callers must not put secrets here.
        self.details = details or {}

    def to_payload(self, request_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "request_id": request_id,
        }
        if self.details:
            payload["details"] = self.details
        return payload


class ValidationError(ApiError):
    status_code = 422
    code = "validation_error"


class Unauthenticated(ApiError):
    status_code = 401
    code = "unauthenticated"


class Forbidden(ApiError):
    status_code = 403
    code = "forbidden"


class NotFound(ApiError):
    status_code = 404
    code = "not_found"


class Conflict(ApiError):
    status_code = 409
    code = "conflict"


class RateLimited(ApiError):
    status_code = 429
    code = "rate_limited"


class ServerError(ApiError):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str = "internal error", **kwargs: Any) -> None:
        # Never echo internals to the client; the real message goes to the log.
        super().__init__(message, **kwargs)


class InvalidStateTransition(ApiError):
    """Raised when a task/run state machine is asked to move illegally."""

    status_code = 409
    code = "invalid_state_transition"
