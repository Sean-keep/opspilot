#!/usr/bin/env python3
"""Seed the control-plane database with permissions, roles, and an admin.

Safe to run repeatedly: every step is idempotent and never overwrites an
existing row. If the bootstrap admin already exists its password is left
alone.

Usage::

    python3 scripts/bootstrap.py --admin-username admin --admin-password '...'

The password may also be supplied via ``OPSPILOT_BOOTSTRAP_PASSWORD`` so it
does not land in shell history. If neither is given and no admin exists, the
script refuses to create one rather than inventing a default credential.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make the repository importable when invoked as a script from anywhere.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.auth.models import User  # noqa: E402
from backend.app.auth.permissions import DEFAULT_ROLES, PERMISSION_CATALOG  # noqa: E402
from backend.app.auth.schemas import UserCreate  # noqa: E402
from backend.app.auth.service import (  # noqa: E402
    AuthService,
    attach_role_permissions,
    bootstrap_permissions,
    bootstrap_roles,
)
from backend.app.core.logging import configure_logging, get_logger  # noqa: E402
from backend.app.core.settings import get_settings  # noqa: E402
from backend.app.db.session import session_scope  # noqa: E402
from sqlalchemy import select  # noqa: E402

log = get_logger(__name__)


def seed(session) -> dict[str, object]:  # type: ignore[no-untyped-def]
    """Create permissions and roles. Returns a summary of what changed."""
    created_perms = bootstrap_permissions(session, PERMISSION_CATALOG)
    created_roles = bootstrap_roles(
        session,
        {name: str(spec["description"]) for name, spec in DEFAULT_ROLES.items()},
    )
    for name, spec in DEFAULT_ROLES.items():
        codes = list(spec["permissions"])  # type: ignore[arg-type]
        attach_role_permissions(session, name, codes)

    return {
        "permissions_created": len(created_perms),
        "roles_created": len(created_roles),
        "permissions_total": len(PERMISSION_CATALOG),
        "roles_total": len(DEFAULT_ROLES),
    }


def ensure_admin(
    session,
    *,
    username: str,
    password: str,
) -> User | None:
    """Create the bootstrap admin if it does not exist.

    Returns the created user, or ``None`` when one already exists. Never resets
    an existing password — that would be exactly the "overwrite existing data"
    behaviour the requirements forbid.
    """
    auth = AuthService(session)
    existing = session.scalar(select(User).where(User.username == username))
    if existing is not None:
        log.info("bootstrap.admin_exists", username=username, user_id=existing.id)
        return None

    user = auth.create_user(
        UserCreate(username=username, password=password, display_name="Administrator")
    )
    auth.grant_role(user, "admin")
    log.info("bootstrap.admin_created", username=username, user_id=user.id)
    return user


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-username", default="admin")
    parser.add_argument(
        "--admin-password",
        default=os.environ.get("OPSPILOT_BOOTSTRAP_PASSWORD", ""),
        help="prefer OPSPILOT_BOOTSTRAP_PASSWORD so it stays out of shell history",
    )
    parser.add_argument(
        "--skip-admin",
        action="store_true",
        help="seed permissions and roles only",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("bootstrap.starting", **settings.safe_summary())

    with session_scope(settings) as session:
        summary = seed(session)
        log.info("bootstrap.seeded", **summary)

        created: User | None = None
        if not args.skip_admin:
            if not args.admin_password:
                existing = session.scalar(select(User).where(User.username == args.admin_username))
                if existing is None:
                    log.error(
                        "bootstrap.no_admin_password",
                        hint="pass --admin-password or set OPSPILOT_BOOTSTRAP_PASSWORD",
                    )
                    return 2
            else:
                created = ensure_admin(
                    session,
                    username=args.admin_username,
                    password=args.admin_password,
                )

    log.info("bootstrap.done", admin_created=created is not None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
