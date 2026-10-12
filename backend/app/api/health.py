"""Health endpoints.

``/healthz`` is intentionally unauthenticated: systemd, a reverse proxy, and a
load balancer all need to probe it without credentials. It reports only
liveness plus a coarse database reachability flag — never configuration
values, never secrets.
"""

from __future__ import annotations

import datetime as dt

from fastapi import Response

from backend.app.api.routing import AppRouter
from backend.app.core.settings import get_settings
from backend.app.db.session import ping

router = AppRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, object]:
    """Liveness + dependency reachability.

    Returns 200 while the process is up. ``database`` is ``"ok"`` / ``"error"``
    — a database outage does not make the process "unhealthy" for systemd
    purposes, because the API should stay up to report the problem.
    """
    settings = get_settings()
    db_ok = ping(settings)
    return {
        "status": "ok",
        "env": settings.env,
        "time": dt.datetime.now(dt.UTC).isoformat(),
        "database": "ok" if db_ok else "error",
    }


@router.get("/readyz")
def readyz(response: Response) -> dict[str, object]:
    """Readiness: only ready when the metadata database answers.

    Used by a reverse proxy before sending traffic. Returns 503 when the
    database is unreachable so the proxy takes the instance out of rotation.
    """
    settings = get_settings()
    db_ok = ping(settings)
    if not db_ok:
        response.status_code = 503
        return {"status": "not_ready", "database": "error"}
    return {"status": "ready", "database": "ok"}
