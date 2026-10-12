"""The permission catalogue and the default role model.

Permission codes are the vocabulary of every backend authorisation check.
They are stable identifiers — renaming one is a breaking change for stored
audit rows and for any role configuration an operator has done.

Codes are declared here even for stages not yet implemented so the catalogue
grows in one place and a role can be defined once. Routes begin enforcing a
code only once the endpoint exists, so a declared-but-unused code is harmless.
"""

from __future__ import annotations

from typing import TypedDict


class RoleSpec(TypedDict):
    description: str
    permissions: list[str]


#: code -> human description. Insertion order is the display order.
PERMISSION_CATALOG: dict[str, str] = {
    # -- identity --------------------------------------------------------
    "user.read": "List platform accounts and their roles",
    "user.create": "Create platform accounts",
    "user.update": "Change account status, roles, or password",
    "audit.read": "Query the audit trail",
    # -- inventory (S1) --------------------------------------------------
    "server.read": "View servers and their system facts",
    "server.create": "Register a server",
    "server.update": "Edit server attributes or group membership",
    "server.delete": "Deactivate a server (soft delete)",
    "server.check": "Run connectivity and environment checks against a server",
    "credential.read": "View credential metadata (never key material)",
    "credential.create": "Store a new credential",
    "credential.update": "Rotate or edit a credential",
    "credential.delete": "Retire a credential",
    # -- catalog (S3) ----------------------------------------------------
    "template.read": "View templates and published versions",
    "template.validate": "Validate template parameters before submitting a task",
    "template.publish": "Publish a template version (immutable once published)",
    "template.deprecate": "Deprecate a published template version",
    # -- tasks (S2) ------------------------------------------------------
    "task.read": "View tasks, runs, steps, and logs",
    "task.create": "Submit an install, deploy, or check task",
    "task.cancel": "Request cancellation of a running task",
    "task.retry": "Retry a task or selected failed targets",
    "task.resolve": "Resolve a run left in UNKNOWN after remote-execution recovery",
    # -- applications / deployments (S4) ---------------------------------
    "app.read": "View applications and environments",
    "app.create": "Define an application",
    "app.update": "Change an application or environment",
    "deployment.read": "View deployment records and releases",
    "deployment.create": "Deploy a pinned release to target servers",
    "deployment.rollback": "Create a rollback task to restore a previous release",
    # -- danger zone (S5+) ----------------------------------------------
    "service.manage": "Restart or stop a managed service on a target server",
    "middleware.install": "Install or reconfigure MySQL, Redis, Nginx, or Supervisor",
    "approval.decide": "Approve or reject a gated production operation",
}


#: Default roles. Values are the permission codes granted to each.
DEFAULT_ROLES: dict[str, RoleSpec] = {
    "admin": {
        "description": "Full control over the platform",
        "permissions": sorted(PERMISSION_CATALOG),
    },
    "operator": {
        "description": "Runs installs and deployments, but cannot manage accounts",
        "permissions": [
            "server.read",
            "server.create",
            "server.update",
            "server.check",
            "credential.read",
            "credential.create",
            "credential.update",
            "template.read",
            "template.validate",
            "task.read",
            "task.create",
            "task.cancel",
            "task.retry",
            "task.resolve",
            "app.read",
            "app.create",
            "app.update",
            "deployment.read",
            "deployment.create",
            "deployment.rollback",
            "service.manage",
        ],
    },
    "viewer": {
        "description": "Read-only access to inventory, templates, and task history",
        "permissions": [
            "server.read",
            "credential.read",
            "template.read",
            "task.read",
            "app.read",
            "deployment.read",
        ],
    },
}


def all_permission_codes() -> tuple[str, ...]:
    return tuple(PERMISSION_CATALOG)
