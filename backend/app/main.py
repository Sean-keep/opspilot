"""FastAPI application factory.

Deliberate choices worth knowing:

* **No DDL at startup.** Migrations are run explicitly with Alembic. The
  application never issues destructive DDL from a lifespan hook.
* **Every request gets a ``request_id``**, echoed in the ``X-Request-Id``
  response header, embedded in every log line, and included in every error
  payload. That is what makes a failure in the field diagnosable.
* **Errors never leak internals.** ``ApiError`` carries a stable code and a
  safe message; anything else becomes a generic 500 with only the request id.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from backend.app.api.router import root
from backend.app.core.errors import ApiError
from backend.app.core.logging import configure_logging, get_logger, set_request_id
from backend.app.core.settings import get_settings

log = get_logger(__name__)

APP_TITLE = "OpsPilot"
APP_VERSION = "0.1.0"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Stamp a request id on every request and surface it to the client."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        incoming = request.headers.get("x-request-id", "")
        # Only accept a client-supplied id if it looks safe to echo into logs.
        safe = bool(incoming) and len(incoming) <= 64 and incoming.isascii()
        request_id = incoming if safe else uuid.uuid4().hex
        set_request_id(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            set_request_id(None)
        response.headers["X-Request-Id"] = request_id
        return response


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start-up / shutdown.

    Intentionally does **not** run migrations or seed data. Both are explicit
    operator actions, so an API restart can never mutate schema behind someone's
    back.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("api.starting", **settings.safe_summary())
    yield
    log.info("api.stopping")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=APP_TITLE,
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.env != "production" else None,
    )

    app.add_middleware(RequestContextMiddleware)

    # CORS: same-origin in production (a reverse proxy serves the SPA from the
    # API origin). In dev the Vite dev server needs a narrow, explicit origin.
    if settings.env == "dev":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[f"http://127.0.0.1:{settings.api_port + 1000}", "http://localhost:5173"],
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-Id"],
        )

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        rid = getattr(request.state, "request_id", "") or ""
        log.warning(
            "api.error",
            code=exc.code,
            status=exc.status_code,
            message=exc.message,
            path=request.url.path,
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload(rid))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = getattr(request.state, "request_id", "") or ""
        # Pydantic error messages can echo submitted values; keep only locations.
        fields = sorted({str(err.get("loc", [])) for err in exc.errors()})
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "request body failed validation",
                "request_id": rid,
                "details": {"fields": fields},
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "") or ""
        # The traceback goes to the log only — never to the client.
        log.exception("api.unhandled_error", path=request.url.path, error=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": "internal error",
                "request_id": rid,
            },
        )

    app.include_router(root)
    return app


# `uvicorn backend.app.main:app` and the tests both use this instance.
app = create_app()


def run() -> None:
    """Console-script entry point for the ``opspilot-api`` systemd unit."""
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "backend.app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover
    run()
