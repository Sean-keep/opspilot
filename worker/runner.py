"""Execute one claimed run.

This is the S1 -> S2 seam: the API no longer decrypts credentials or opens
sockets to managed hosts. It writes a task; this module picks the run up and
does the work.

Cancel semantics, which are not rollback semantics:

* a run that has not started a step is cancelled outright
* a run mid-step records ``cancel_requested_at`` / ``remote_signal_sent`` /
  ``remote_exit_after_signal`` in the step's ``result_json`` **before** it is
  marked cancelled, so the stop has evidence
* steps that already completed stay completed; undoing them is a separate
  rollback task

Every remote command goes through :mod:`shared.executor` as an argv list.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any

from shared.checks import CheckError, collect_facts
from shared.config import Settings
from shared.crypto import CryptoError, decrypt_secret
from shared.dbtypes import utcnow
from shared.executor.keys import staged_private_key
from shared.executor.port import (
    ExecContext,
    TransportError,
)
from shared.executor.ssh import SshExecPort
from shared.logsetup import get_logger
from shared.redaction import redact, redact_text
from shared.task_protocol import (
    IDEMPOTENCY_SAFE,
    RUN_CANCELLED,
    RUN_CANCELLING,
    RUN_EXECUTING,
    RUN_FAILED,
    RUN_SUCCESS,
    RUN_VERIFYING,
    RUN_WAITING,
    Task,
    TaskLog,
    TaskRun,
    TaskStep,
    ensure_run_transition,
)
from shared.task_protocol.claim import ClaimedRun
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

log = get_logger(__name__)

# The only kind S2 executes. S3 replaces this with the Action Registry.
SUPPORTED_KINDS = frozenset({"server.check"})

# Read-only argv used by server.check. Fixed list, no substitution.
# (The parsing lives in shared.checks.collect_facts.)


class RunOutcome:
    """What happened to one run, for the caller to log and move on."""

    __slots__ = ("cancelled", "detail", "status")

    def __init__(self, status: str, detail: str = "", *, cancelled: bool = False) -> None:
        self.status = status
        self.detail = detail
        self.cancelled = cancelled


class Runner:
    """Executes claimed runs against real hosts.

    ``port_factory`` is injectable so tests can drive the executor without
    opening a socket. Production passes nothing and gets ``SshExecPort``.
    """

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        worker_id: str,
        port_factory: Any | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.worker_id = worker_id
        self._port_factory = port_factory
        self._seq = 0

    def _make_port(self) -> Any:
        if self._port_factory is not None:
            return self._port_factory()
        return SshExecPort(
            connect_timeout=self.settings.ssh_connect_timeout,
            default_timeout=self.settings.ssh_command_timeout,
        )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _next_seq(self, run_id: int) -> int:
        current = self.session.execute(
            select(func.coalesce(func.max(TaskLog.seq), 0)).where(TaskLog.run_id == run_id)
        ).scalar_one()
        self._seq = int(current) + 1
        return self._seq

    def log(
        self,
        run_id: int,
        message: str,
        *,
        level: str = "info",
        stream: str = "system",
        step_id: int | None = None,
    ) -> None:
        """Append one log line. The message is redacted before it is stored."""
        self.session.add(
            TaskLog(
                run_id=run_id,
                step_id=step_id,
                seq=self._next_seq(run_id),
                ts=utcnow(),
                level=level,
                stream=stream,
                message=redact_text(message)[:8000],
            )
        )
        self.session.commit()

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------
    def _cancel_requested(self, run_id: int) -> bool:
        """Has the operator asked this run to stop?

        Both signals matter. ``TaskService.cancel`` flips active runs to
        ``CANCELLING``, but a run claimed *after* that sweep is only marked at
        the task level — a worker that looked solely at the run row would
        happily execute work the operator already stopped.

        ``refresh`` is not optional: this session is long-lived and
        ``expire_on_commit=False``, so a plain ``get`` hands back the row as
        this worker last saw it and never notices the API's commit.
        """
        run = self.session.get(TaskRun, run_id)
        if run is None:
            return False
        self.session.refresh(run)
        if run.status == RUN_CANCELLING:
            return True
        task = self.session.get(Task, run.task_id)
        if task is None:
            return False
        self.session.refresh(task)
        return task.cancel_requested_at is not None

    def _record_cancel_evidence(
        self,
        step: TaskStep,
        *,
        requested_at: dt.datetime | None,
        signal_sent: bool,
        remote_exit: int | None,
    ) -> None:
        """Store what we actually did to stop the remote work.

        Only changing the database row and calling it cancelled is not
        acceptable: the evidence has to say a signal was delivered and what the
        remote did afterwards.
        """
        payload = dict(step.result_json or {})
        payload["cancel"] = {
            "cancel_requested_at": (requested_at or utcnow()).isoformat(),
            "remote_signal_sent": signal_sent,
            "remote_exit_after_signal": remote_exit,
            "handled_by": self.worker_id,
        }
        scrubbed = redact(payload)
        step.result_json = scrubbed if isinstance(scrubbed, dict) else {"value": scrubbed}
        self.session.commit()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def execute(self, claimed: ClaimedRun) -> RunOutcome:
        run = self.session.get(TaskRun, claimed.run_id)
        if run is None:
            return RunOutcome(RUN_FAILED, "run vanished before execution")
        if run.lease_owner != self.worker_id:
            return RunOutcome(RUN_FAILED, "lease lost before execution")

        if claimed.kind not in SUPPORTED_KINDS:
            return self._fail(run, f"unsupported task kind {claimed.kind!r}")

        self.log(run.id, f"run #{run.id} attempt {run.attempt} starting on server {run.server_id}")

        # A run already flagged for cancellation must never be walked forward
        # into EXECUTING — that would be a state-machine lie and would race the
        # operator's stop.
        if self._cancel_requested(run.id):
            return self._cancel_before_work(run)

        self._advance(run, RUN_EXECUTING)

        if self._cancel_requested(run.id):
            return self._cancel_before_work(run)

        try:
            return self._run_server_check(claimed, run)
        except (TransportError, CheckError, CryptoError) as exc:
            detail = redact_text(str(exc))
            self.log(run.id, f"run failed: {detail}", level="error")
            self._note_check(run, "failed", detail)
            return self._fail(run, detail)
        except Exception as exc:
            log.exception("worker.run_error", run_id=run.id)
            detail = redact_text(f"internal error: {exc}")
            self._note_check(run, "failed", detail)
            return self._fail(run, detail)

    # ------------------------------------------------------------------
    def _run_server_check(self, claimed: ClaimedRun, run: TaskRun) -> RunOutcome:
        """Collect system facts and store a snapshot.

        One step, ``SAFE`` to re-run: it only reads, and a second facts row is
        append-only history rather than corruption.
        """
        server = self._load_server(run.server_id)
        if server is None:
            return self._fail(run, f"server {run.server_id} no longer exists")

        if server.get("host_key_state") != "verified" or not server.get("host_key_fingerprint"):
            detail = (
                "host key is not verified; an operator must confirm the fingerprint "
                "before any command runs"
            )
            self._note_check(run, "failed", detail)
            return self._fail(run, detail)

        step = self._start_step(
            run,
            step_key="collect_facts",
            action="system.check_os",
            idempotency_class=IDEMPOTENCY_SAFE,
            params={"server_id": run.server_id},
        )

        if self._cancel_requested(run.id):
            return self._cancel_during_step(run, step, signal_sent=False, remote_exit=None)

        credential_id = server.get("credential_id")
        if not credential_id:
            return self._fail_step(run, step, "server has no credential bound")

        try:
            secret = decrypt_secret(self._load_credential_payload(int(credential_id)))
        except (CryptoError, TypeError, ValueError) as exc:
            return self._fail_step(run, step, redact_text(str(exc)))

        started = time.monotonic()
        try:
            with staged_private_key(secret) as key_path:
                ctx = ExecContext(
                    host=str(server["hostname"]),
                    port=int(server.get("port") or 22),
                    user=str(server.get("ssh_user") or ""),
                    credential_id=int(credential_id),
                    timeout=self.settings.ssh_command_timeout,
                    host_key_fingerprint=str(server["host_key_fingerprint"]),
                    extra={"key_filename": str(key_path)},
                )
                facts = collect_facts(self._make_port(), ctx)
        except (TransportError, CheckError) as exc:
            duration = int((time.monotonic() - started) * 1000)
            step.duration_ms = duration
            return self._fail_step(run, step, redact_text(str(exc)))

        duration = int((time.monotonic() - started) * 1000)

        if self._cancel_requested(run.id):
            # The work landed, but the operator asked us to stop. Record the
            # evidence and let the cancel win — without pretending the facts
            # were not collected, and without walking a CANCELLING run forward
            # into VERIFYING.
            step.duration_ms = duration
            self._record_cancel_evidence(
                step, requested_at=run.finished_at, signal_sent=False, remote_exit=0
            )
            step.status = "SUCCESS"
            step.finished_at = utcnow()
            step.result_json = {
                **(step.result_json or {}),
                "collected": True,
                "os": facts.os_name,
                "arch": facts.arch,
            }
            self.session.commit()
            self.log(run.id, "cancel arrived after collection finished; facts stored")
            return self._cancel_during_step(run, step, signal_sent=False, remote_exit=0)

        self._advance(run, RUN_VERIFYING)

        self._store_facts(run, step, facts, duration=duration)
        self.log(
            run.id,
            f"collected {facts.os_name} {facts.os_version} / {facts.arch} / "
            f"{facts.cpu_count} cpu / {facts.memory_total_mb} MB",
            step_id=step.id,
        )
        return self._succeed(run, step)

    # ------------------------------------------------------------------
    # Step helpers
    # ------------------------------------------------------------------
    def _start_step(
        self,
        run: TaskRun,
        *,
        step_key: str,
        action: str,
        idempotency_class: str,
        params: dict[str, Any],
    ) -> TaskStep:
        step = TaskStep(
            run_id=run.id,
            step_key=step_key,
            attempt=run.attempt,
            action=action,
            status="RUNNING",
            idempotency_class=idempotency_class,
            params_json=dict(params),
            started_at=utcnow(),
        )
        self.session.add(step)
        self.session.commit()
        return step

    def _store_facts(
        self,
        run: TaskRun,
        step: TaskStep,
        facts: Any,
        *,
        duration: int,
    ) -> None:
        raw = dict(facts.raw or {})
        scrubbed = redact(raw)
        step.status = "SUCCESS"
        step.finished_at = utcnow()
        step.duration_ms = duration
        step.exit_code = 0
        step.result_json = {
            "os_name": facts.os_name,
            "os_version": facts.os_version,
            "arch": facts.arch,
            "cpu_count": facts.cpu_count,
            "memory_total_mb": facts.memory_total_mb,
            "disk_total_gb": facts.disk_total_gb,
        }
        self.session.commit()
        clean = scrubbed if isinstance(scrubbed, dict) else {}
        self._persist_facts_snapshot(run.server_id, facts, raw=clean)

    def _note_check(self, run: TaskRun, status: str, detail: str) -> None:
        """Mirror a failed check onto the server row.

        The API no longer runs the check, so nothing else updates
        ``last_check_*``. Keep the column honest for the server list instead of
        leaving the last success frozen on screen while the host is down.
        """
        self.session.execute(
            text(
                """
                UPDATE ops_servers
                   SET last_check_at = :now,
                       last_check_status = :status,
                       last_check_detail = :detail,
                       updated_at = :now
                 WHERE id = :server_id
                """
            ),
            {
                "server_id": run.server_id,
                "status": status,
                "detail": detail[:4000],
                "now": utcnow().replace(tzinfo=None),
            },
        )
        self.session.commit()

    def _persist_facts_snapshot(self, server_id: int, facts: Any, *, raw: dict[str, Any]) -> None:
        # Facts live in the inventory table the API owns. Writing it here is a
        # plain insert — no API imports, same schema.
        self.session.execute(
            text(
                """
                INSERT INTO ops_server_facts
                    (server_id, os_name, os_version, kernel, arch, cpu_count, cpu_model,
                     memory_total_mb, memory_available_mb, disk_total_gb, disk_available_gb,
                     collected_at, raw_json, created_at, updated_at)
                VALUES
                    (:server_id, :os_name, :os_version, :kernel, :arch, :cpu_count, :cpu_model,
                     :mem_t, :mem_a, :disk_t, :disk_a, :collected_at, :raw, :now, :now)
                """
            ),
            {
                "server_id": server_id,
                "os_name": facts.os_name,
                "os_version": facts.os_version,
                "kernel": facts.kernel,
                "arch": facts.arch,
                "cpu_count": facts.cpu_count,
                "cpu_model": facts.cpu_model,
                "mem_t": facts.memory_total_mb,
                "mem_a": facts.memory_available_mb,
                "disk_t": float(facts.disk_total_gb),
                "disk_a": float(facts.disk_available_gb),
                "collected_at": facts.collected_at or utcnow().replace(tzinfo=None),
                "raw": json.dumps(raw, default=str),
                "now": utcnow().replace(tzinfo=None),
            },
        )
        self.session.execute(
            text(
                """
                UPDATE ops_servers
                   SET last_check_at = :now,
                       last_check_status = 'ok',
                       last_check_detail = :detail,
                       updated_at = :now
                 WHERE id = :server_id
                """
            ),
            {
                "server_id": server_id,
                "now": utcnow().replace(tzinfo=None),
                "detail": f"{facts.os_name} {facts.os_version} / {facts.arch}",
            },
        )
        self.session.commit()

    # ------------------------------------------------------------------
    # Terminal transitions
    # ------------------------------------------------------------------
    def _advance(self, run: TaskRun, status: str) -> None:
        ensure_run_transition(run.status, status)
        run.status = status
        self.session.commit()

    def _succeed(self, run: TaskRun, step: TaskStep) -> RunOutcome:
        ensure_run_transition(run.status, RUN_SUCCESS)
        now = utcnow()
        run.status = RUN_SUCCESS
        run.finished_at = now
        run.lease_owner = ""
        run.lease_expires_at = None
        step.status = "SUCCESS"
        step.finished_at = now
        self.session.commit()
        self.log(run.id, "run succeeded")
        return RunOutcome(RUN_SUCCESS)

    def _fail(self, run: TaskRun, detail: str) -> RunOutcome:
        ensure_run_transition(run.status, RUN_FAILED)
        run.status = RUN_FAILED
        run.finished_at = utcnow()
        run.lease_owner = ""
        run.lease_expires_at = None
        run.error_detail = detail[:8000]
        self.session.commit()
        self.log(run.id, f"run failed: {detail}", level="error")
        return RunOutcome(RUN_FAILED, detail)

    def _fail_step(self, run: TaskRun, step: TaskStep, detail: str) -> RunOutcome:
        step.status = "FAILED"
        step.finished_at = utcnow()
        step.result_json = {**(step.result_json or {}), "error": detail}
        self.session.commit()
        self._note_check(run, "failed", detail)
        return self._fail(run, detail)

    def _cancel_before_work(self, run: TaskRun) -> RunOutcome:
        self.log(run.id, "cancel requested before any step ran")
        return self._finish_cancel(run, step=None, signal_sent=False, remote_exit=None)

    def _cancel_during_step(
        self,
        run: TaskRun,
        step: TaskStep,
        *,
        signal_sent: bool,
        remote_exit: int | None,
    ) -> RunOutcome:
        return self._finish_cancel(
            run, step=step, signal_sent=signal_sent, remote_exit=remote_exit
        )

    def _finish_cancel(
        self,
        run: TaskRun,
        *,
        step: TaskStep | None,
        signal_sent: bool,
        remote_exit: int | None,
    ) -> RunOutcome:
        """Stop the run **with evidence**, then mark it cancelled.

        Changing only the status column would leave no way to tell a clean
        stop from a worker that just gave up.
        """
        if step is not None and step.status == "RUNNING":
            self._record_cancel_evidence(
                step,
                requested_at=run.finished_at,
                signal_sent=signal_sent,
                remote_exit=remote_exit,
            )
            step.status = "CANCELLED"
            step.finished_at = utcnow()

        if run.status not in (RUN_CANCELLING, RUN_WAITING):
            # An in-flight run stops by way of CANCELLING; one that never
            # started goes straight out.
            ensure_run_transition(run.status, RUN_CANCELLING)
            run.status = RUN_CANCELLING
        ensure_run_transition(run.status, RUN_CANCELLED)
        run.status = RUN_CANCELLED
        run.finished_at = utcnow()
        run.lease_owner = ""
        run.lease_expires_at = None
        run.error_detail = "cancelled by operator"
        self.session.commit()
        self.log(
            run.id,
            f"cancelled: remote_signal_sent={signal_sent} "
            f"remote_exit_after_signal={remote_exit}",
        )
        return RunOutcome(RUN_CANCELLED, "cancelled", cancelled=True)

    # ------------------------------------------------------------------
    # Lookups (raw SQL — the ORM models live in the API package)
    # ------------------------------------------------------------------
    def _load_server(self, server_id: int) -> dict[str, Any] | None:
        row = self.session.execute(
            text(
                """
                SELECT id, hostname, port, ssh_user, credential_id,
                       host_key_state, host_key_fingerprint
                  FROM ops_servers
                 WHERE id = :id
                """
            ),
            {"id": server_id},
        ).mappings().first()
        return dict(row) if row is not None else None

    def _load_credential_payload(self, credential_id: int) -> str:
        # `scalar_one_or_none` on a bare `text()` clause infers `None`; the
        # annotation keeps the empty-payload branch honest to the type checker.
        value: str | None = self.session.execute(
            text("SELECT payload_encrypted FROM ops_credentials WHERE id = :id"),
            {"id": credential_id},
        ).scalar_one_or_none()
        if not value:
            raise CryptoError(f"credential {credential_id} has no stored payload")
        return str(value)
