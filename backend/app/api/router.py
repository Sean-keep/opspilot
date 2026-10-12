"""API router aggregation.

Feature routers are registered here and mounted under ``/api/v1``. Health
routes live at the site root (``/healthz``) so a probe never needs the version
prefix or an auth token.
"""

from __future__ import annotations

from backend.app.api import health
from backend.app.api.routing import AppRouter
from backend.app.applications.router import router as applications_router
from backend.app.audit.router import router as audit_router
from backend.app.auth.router import router as auth_router
from backend.app.catalog.router import router as catalog_router
from backend.app.credentials.router import router as credentials_router
from backend.app.deployments.router import app_router as deploy_router
from backend.app.deployments.router import router as deployments_router
from backend.app.inventory.router import router as inventory_router
from backend.app.orchestrator.router import router as tasks_router
from backend.app.orchestrator.router import run_router as task_runs_router

api_v1 = AppRouter(prefix="/api/v1")
api_v1.include_router(auth_router)
api_v1.include_router(audit_router)
api_v1.include_router(inventory_router)
api_v1.include_router(credentials_router)
api_v1.include_router(catalog_router)
api_v1.include_router(applications_router)
api_v1.include_router(deployments_router)
api_v1.include_router(deploy_router)
api_v1.include_router(tasks_router)
api_v1.include_router(task_runs_router)

root = AppRouter()
root.include_router(health.router)
root.include_router(api_v1)
