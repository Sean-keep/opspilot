"""Runtime configuration — re-export shim.

The implementation lives in :mod:`shared.config` because the worker process
reads settings too and must not import ``backend.app``. Existing imports of
``backend.app.core.settings`` keep working unchanged.

Do not add new code here. Add it to ``shared.config`` instead.
"""

from __future__ import annotations

from shared.config import Settings, clear_settings_cache, get_settings

__all__ = ["Settings", "clear_settings_cache", "get_settings"]
