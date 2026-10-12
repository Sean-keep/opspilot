"""OpsPilot task worker.

An independent process from the API. It talks to nothing but the control-plane
MySQL database and the target servers over SSH. It can run in multiple
instances: task claiming uses ``SELECT … FOR UPDATE SKIP LOCKED`` with a lease,
so two workers never execute the same run.

This module owns the process lifecycle — signal handling, graceful shutdown,
and the main loop. Scheduling and execution live in ``worker.scheduler`` and
``worker.runner`` and are filled in by later stages.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import time
import uuid

from backend.app.core.logging import configure_logging, get_logger
from backend.app.core.settings import Settings, get_settings

from worker.scheduler import Scheduler

log = get_logger(__name__)


class _Shutdown:
    """Cooperative shutdown flag flipped by SIGTERM/SIGINT."""

    def __init__(self) -> None:
        self.requested = False

    def request(self, signum: int, _frame: object) -> None:
        log.info("worker.shutdown_requested", signal=int(signum))
        self.requested = True


def resolve_worker_id(settings: Settings) -> str:
    """Stable-enough identity used as ``ops_task_runs.lease_owner``.

    Prefer the configured id. Otherwise compose hostname + pid + a short random
    suffix so two workers on one host never collide, and so a restarted worker
    is recognisably a new lease holder.
    """
    if settings.worker_id:
        return settings.worker_id
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def run() -> None:
    """Console-script entry point for the ``opspilot-worker`` systemd unit."""
    settings = get_settings()
    configure_logging(settings.log_level)
    worker_id = resolve_worker_id(settings)

    shutdown = _Shutdown()
    signal.signal(signal.SIGTERM, shutdown.request)
    signal.signal(signal.SIGINT, shutdown.request)

    log.info(
        "worker.starting",
        worker_id=worker_id,
        lease_seconds=settings.lease_seconds,
        heartbeat_seconds=settings.heartbeat_seconds,
        concurrency=settings.worker_concurrency,
    )

    scheduler = Scheduler(worker_id=worker_id, settings=settings)
    last_idle_log = 0.0

    try:
        while not shutdown.requested:
            try:
                did_work = scheduler.tick()
            except Exception:
                # A scheduler failure must not kill the worker; log and continue.
                log.exception("worker.scheduler_error")
                did_work = False

            if not did_work:
                now = time.monotonic()
                if now - last_idle_log > 30.0:
                    log.info("worker.idle")
                    last_idle_log = now
                # Nothing claimed: sleep briefly so we do not spin the database.
                time.sleep(1.0)
    finally:
        log.info("worker.draining")
        scheduler.shutdown()
        log.info("worker.stopped", worker_id=worker_id)


if __name__ == "__main__":  # pragma: no cover
    run()
    sys.exit(0)
