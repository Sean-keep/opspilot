"""Structured logging.

Lives in ``shared/`` so both processes log the same way without importing
each other. ``backend.app.core.logging`` re-exports these names.

Every log line is JSON, carries a ``request_id`` when one is in scope, and runs
through :func:`shared.redaction.redact` before it is emitted. That last part is
what keeps the "never log secrets" requirement true even when a caller passes a
naive dict.
"""

from __future__ import annotations

import contextvars
import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

from shared.redaction import redact, redact_text

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

_CONFIGURED = False


def set_request_id(request_id: str | None) -> None:
    """Bind *request_id* to the current context (and clear it with ``None``)."""
    _request_id.set(request_id)


def get_request_id() -> str | None:
    return _request_id.get()


def _scrub_event(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    """structlog processor: redact values and stamp the request id."""
    scrubbed = redact(dict(event_dict))
    if not isinstance(scrubbed, dict):  # pragma: no cover - defensive
        scrubbed = {"event": scrubbed}
    message = scrubbed.get("event")
    if isinstance(message, str):
        scrubbed["event"] = redact_text(message)
    rid = get_request_id()
    if rid:
        scrubbed.setdefault("request_id", rid)
    return scrubbed


def _drop_color_message(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    """structlog's ConsoleRenderer noise; irrelevant for JSON output."""
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Install JSON logging for both stdlib and structlog loggers."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _drop_color_message,
            _scrub_event,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
