"""Task scheduling loop.

Stage S0 ships the process skeleton only: the scheduler runs, reports whether
it did any work, and shuts down cleanly. Task claiming, leases, and recovery
arrive with the task/run/step schema in stage S2 and land in this module — the
call sites in ``worker.main`` do not change.

The contract for :meth:`Scheduler.tick` is already fixed so S2 can be dropped
in without touching process lifecycle code:

* return ``True`` when it claimed or advanced at least one unit of work
* return ``False`` when the queue was empty (the caller then sleeps)
* never raise — a transient database error is logged and treated as idle
* honour ``shutdown()`` promptly so SIGTERM is not blocked by a long poll
"""

from __future__ import annotations

from backend.app.core.logging import get_logger
from backend.app.core.settings import Settings

log = get_logger(__name__)


class Scheduler:
    """Claims and advances remote tasks. Empty in S0 by design."""

    def __init__(self, *, worker_id: str, settings: Settings) -> None:
        self.worker_id = worker_id
        self.settings = settings
        self._stopping = False

    def tick(self) -> bool:
        """One scheduling pass. Returns True when work was done."""
        if self._stopping:
            return False
        # S2 replaces this with an atomic claim over ops_task_runs.
        return False

    def shutdown(self) -> None:
        """Stop accepting work and release anything held.

        Called from the process ``finally`` block after SIGTERM. Must be safe
        to call more than once.
        """
        self._stopping = True
