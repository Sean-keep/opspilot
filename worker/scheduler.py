"""Task scheduling loop: recover, claim, execute, heartbeat.

One :meth:`Scheduler.tick` does a bounded amount of work and returns whether it
did any, so ``worker.main`` can sleep when the queue is empty and drain
promptly on SIGTERM.

Order matters. Recovery runs **before** claiming so that a run whose holder
died is put back in play (or parked in ``UNKNOWN``) before we go looking for
new work. Claiming is the atomic ``FOR UPDATE SKIP LOCKED`` path in
:mod:`shared.task_protocol.claim`; nothing here does a select-then-update.
"""

from __future__ import annotations

from typing import Any

from shared.config import Settings
from shared.crypto import decrypt_secret
from shared.executor.keys import staged_private_key
from shared.executor.port import ExecContext, TransportError
from shared.executor.ssh import SshExecPort
from shared.logsetup import get_logger
from shared.task_protocol import (
    IDEMPOTENCY_DESTRUCTIVE,
    IDEMPOTENCY_SAFE,
    PROBE_FINISHED,
    PROBE_UNREACHABLE,
    RUN_ACTIVE,
    RUN_WAITING,
    TaskRun,
    TaskStep,
    claim_runs,
    decide_recovery,
    find_expired_runs,
    recompute_task_status,
    renew_leases,
)
from shared.task_protocol.claim import apply_recovery
from sqlalchemy import func, select, text

from worker.db import get_factory
from worker.runner import Runner

log = get_logger(__name__)


class Scheduler:
    """Claims and advances remote tasks."""

    def __init__(self, *, worker_id: str, settings: Settings) -> None:
        self.worker_id = worker_id
        self.settings = settings
        self._stopping = False

    def _is_stopping(self) -> bool:
        """Read the stop flag through a method.

        ``shutdown()`` is called from the SIGTERM handler and can land between
        any two statements of ``tick()``. A direct attribute read would let the
        type checker narrow the flag to a constant and treat the mid-loop stop
        branch as dead code, which it is not.
        """
        return self._stopping

    # ------------------------------------------------------------------
    def tick(self) -> bool:
        """One scheduling pass. Returns True when work was done."""
        if self._is_stopping():
            return False

        did_work = False
        factory = get_factory(self.settings)

        # 1. Recovery first: lapsed leases must be settled before new claims.
        session = factory()
        try:
            did_work |= self._recover_expired(session)
        finally:
            session.close()

        # 2. Claim up to the concurrency limit and run each to a terminal state.
        session = factory()
        try:
            claimed = claim_runs(
                session,
                owner=self.worker_id,
                limit=self.settings.worker_concurrency,
                lease_seconds=self.settings.lease_seconds,
            )
            if not claimed:
                return did_work

            runner = Runner(session, self.settings, worker_id=self.worker_id)
            for run in claimed:
                if self._is_stopping():
                    # Put unstarted work back rather than holding the lease.
                    self._release_for_shutdown(session, run.run_id)
                    continue
                did_work = True
                try:
                    outcome = runner.execute(run)
                except Exception:
                    log.exception("worker.run_crashed", run_id=run.run_id)
                    outcome = None
                finally:
                    self._renew(session, [run.run_id])
                if outcome is not None:
                    recompute_task_status(session, task_id=run.task_id)
            return did_work
        finally:
            session.close()

    # ------------------------------------------------------------------
    def _renew(self, session: Any, run_ids: list[int]) -> None:
        renewed = renew_leases(
            session,
            owner=self.worker_id,
            run_ids=run_ids,
            lease_seconds=self.settings.lease_seconds,
        )
        if renewed >= len(run_ids):
            return
        # A shortfall is only a *lost* lease while the work is still ours to
        # hold. Finishing a run releases its lease on purpose, so counting that
        # as a loss would put a warning on every successful run and teach
        # operators to ignore this event.
        still_ours = int(
            session.execute(
                select(func.count())
                .select_from(TaskRun)
                .where(
                    TaskRun.id.in_(list(run_ids)),
                    TaskRun.status.in_(sorted(RUN_ACTIVE)),
                )
            ).scalar_one()
        )
        if still_ours:
            log.warning(
                "worker.lease_lost",
                wanted=len(run_ids),
                renewed=renewed,
                still_active=still_ours,
                worker_id=self.worker_id,
            )

    def _release_for_shutdown(self, session: Any, run_id: int) -> None:
        run = session.get(TaskRun, run_id)
        if run is None or run.lease_owner != self.worker_id:
            return
        run.status = RUN_WAITING
        run.lease_owner = ""
        run.lease_expires_at = None
        session.commit()
        log.info("worker.run_requeued_on_shutdown", run_id=run_id)

    # ------------------------------------------------------------------
    # Lease recovery
    # ------------------------------------------------------------------
    def _recover_expired(self, session: Any) -> bool:
        candidates = find_expired_runs(session, limit=20)
        if not candidates:
            return False

        for run in candidates:
            probe, evidence = self._probe_remote(session, run)
            idempotency_class = self._worst_idempotency_class(session, run)
            precheck_ok = False
            if idempotency_class != IDEMPOTENCY_SAFE and probe == PROBE_FINISHED:
                precheck_ok = self._precheck_target_state(session, run)

            decision = decide_recovery(
                probe,
                idempotency_class,
                precheck_target_state=precheck_ok,
            )
            outcome = apply_recovery(
                session,
                run_id=run.id,
                decision=decision,
                probe=probe,
                evidence=evidence,
                decided_by=self.worker_id,
                lease_seconds=self.settings.lease_seconds,
            )
            log.info(
                "worker.lease_recovery",
                run_id=run.id,
                probe=probe,
                decision=decision,
                idempotency_class=idempotency_class,
            )
            if outcome is not None and decision != "renewed":
                recompute_task_status(session, task_id=run.task_id)
        return True

    def _probe_remote(
        self, session: Any, run: TaskRun
    ) -> tuple[str, dict[str, Any]]:
        """Step 1 of lease recovery: what is the remote actually doing?

        S2's only kind is a read-only check, so the probe is "can we still
        reach this host with its pinned key". A reachable host whose work is
        ``SAFE`` may be re-run; an unreachable one is undetermined and stops.

        S3 replaces this with the real Action Registry probes
        (``systemd.is_active``, PID files, port checks) once actions carry an
        ``idempotency_class``.
        """
        server = self._load_server(session, int(run.server_id))
        if server is None:
            return PROBE_UNREACHABLE, {"error": "server record is gone"}
        if not server.get("host_key_fingerprint"):
            return PROBE_UNREACHABLE, {"error": "host key is not pinned"}
        if not server.get("credential_id"):
            return PROBE_UNREACHABLE, {"error": "no credential bound"}

        try:
            secret = decrypt_secret(
                self._load_credential(session, int(server["credential_id"]))
            )
        except Exception as exc:
            return PROBE_UNREACHABLE, {"error": f"credential unavailable: {exc}"}

        try:
            with staged_private_key(secret) as key_path:
                ctx = ExecContext(
                    host=str(server["hostname"]),
                    port=int(server.get("port") or 22),
                    user=str(server.get("ssh_user") or ""),
                    timeout=min(15, self.settings.ssh_connect_timeout),
                    host_key_fingerprint=str(server["host_key_fingerprint"]),
                    extra={"key_filename": str(key_path)},
                )
                port = SshExecPort(
                    connect_timeout=self.settings.ssh_connect_timeout,
                    default_timeout=15,
                )
                # A trivial read-only probe. Never a state-changing command.
                result = port.run(ctx, ("true",))
                return PROBE_FINISHED, {
                    "host": str(server["hostname"]),
                    "probe_argv": ["true"],
                    "exit_code": result.exit_code,
                }
        except TransportError as exc:
            return PROBE_UNREACHABLE, {
                "host": str(server.get("hostname", "")),
                "error": str(exc)[:500],
            }

    def _worst_idempotency_class(self, session: Any, run: TaskRun) -> str:
        """The most dangerous step class in the run.

        A run may only be re-run automatically when **every** step it would
        repeat is safe to repeat. One DESTRUCTIVE step poisons the lot.
        """
        classes = list(
            session.execute(
                select(TaskStep.idempotency_class).where(TaskStep.run_id == run.id)
            ).scalars()
        )
        if not classes:
            # No step records yet: we do not know what it was going to do, so
            # assume the worst.
            return IDEMPOTENCY_DESTRUCTIVE
        if IDEMPOTENCY_DESTRUCTIVE in classes:
            return IDEMPOTENCY_DESTRUCTIVE
        if "CONDITIONAL" in classes:
            return "CONDITIONAL"
        return IDEMPOTENCY_SAFE

    def _precheck_target_state(self, session: Any, run: TaskRun) -> bool:
        """Step 3 for CONDITIONAL steps: is the host already in the target state?

        S2 has no CONDITIONAL steps. Returning False makes a CONDITIONAL step
        land in ``UNKNOWN`` rather than being silently re-run — which is the
        safe direction to be wrong in.
        """
        del session, run
        return False

    @staticmethod
    def _load_server(session: Any, server_id: int) -> dict[str, Any] | None:
        row = session.execute(
            text(
                """
                SELECT id, hostname, port, ssh_user, credential_id, host_key_fingerprint
                  FROM ops_servers WHERE id = :id
                """
            ),
            {"id": server_id},
        ).mappings().first()
        return dict(row) if row is not None else None

    @staticmethod
    def _load_credential(session: Any, credential_id: int) -> str:
        value: str | None = session.execute(
            text("SELECT payload_encrypted FROM ops_credentials WHERE id = :id"),
            {"id": credential_id},
        ).scalar_one_or_none()
        if not value:
            raise TransportError(f"credential {credential_id} has no stored payload")
        return str(value)

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Stop accepting work and release anything held.

        Called from the process ``finally`` block after SIGTERM. Must be safe
        to call more than once.
        """
        self._stopping = True
