"""API router aggregation.

Feature routers are registered here and mounted under ``/api/v1``. Health
routes live at the site root (``/healthz``) so a probe never needs the version
prefix or an auth token.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.app.api import health
from backend.app.audit.router import router as audit_router
from backend.app.auth.router import router as auth_router
from backend.app.credentials.router import router as credentials_router
from backend.app.inventory.router import router as inventory_router

api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(auth_router)
api_v1.include_router(audit_router)
api_v1.include_router(inventory_router)
api_v1.include_router(credentials_router)

root = APIRouter()
root.include_router(health.router)
root.include_router(api_v1)
