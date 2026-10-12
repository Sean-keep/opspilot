"""Read-only system checks — re-export shim.

The implementation lives in :mod:`shared.checks` because the worker runs these
over real SSH and the API must not import into that path. This module keeps
the old import path working for API code and tests.

``CheckError`` here is the *shared* exception, not an ``ApiError``: it never
reaches a client on its own. ``InventoryService.run_check`` catches it and
turns it into a ``ServerCheckResult(ok=False)``.
"""

from __future__ import annotations

from shared.checks import CheckError, SystemFacts, collect_facts

__all__ = ["CheckError", "SystemFacts", "collect_facts"]
