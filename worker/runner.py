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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from shared.actions.base import resolve as resolve_action
from shared.checks import CheckError, collect_facts
from shared.config import Settings
from shared.crypto import CryptoError, decrypt_secret
from shared.dbtypes import utcnow
from shared.deploy_plan import (
    PlanStep,
    build_deploy_plan,
    build_restore_plan,
    inputs_from_mapping,
)
from shared.executor.keys import staged_private_key
from shared.executor.port import (
    ExecContext,
    TransportError,
)
from shared.executor.ssh import SshExecPort
from shared.logsetup import get_logger
from shared.redaction import redact, redact_params, redact_text
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
from shared.task_protocol.claim import ClaimedRun, renew_leases
from shared.templates import (
    SERVER_VALUE_KEYS,
    TemplateApplyParams,
    TemplateError,
    plan_template_apply,
)
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

log = get_logger(__name__)

#: Kinds this worker executes. Widened together with
#: ``backend.app.orchestrator.service.TASK_KINDS`` — a kind one side accepts and
#: the other ignores is a task that sits in the queue forever.
SUPPORTED_KINDS = frozenset(
    {"server.check", "app.deploy", "app.rollback", "template.apply"}
)


class RunBlocked(Exception):
    """A precondition failed before any remote command could run.

    Distinct from a step failure: nothing was attempted on the host, so there
    is no partial state to explain or undo.
    """

# Read-only argv used by server.check. Fixed list, no substitution.
# (The parsing lives in shared.checks.collect_facts.)


class RunOutcome:
    """What happened to one run, for the caller to log and move on."""

    __slots__ = ("cancelled", "detail", "failed_step_key", "status")

    def __init__(
        self,
        status: str,
        detail: str = "",
        *,
        cancelled: bool = False,
        failed_step_key: str = "",
    ) -> None:
        self.status = status
        self.detail = detail
        self.cancelled = cancelled
        #: Which plan step stopped the run. Empty when the run failed before
        #: or between steps — the deployment restore policy keys off this, so
        #: "I don't know" must stay distinguishable from "the last one".
        self.failed_step_key = failed_step_key


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
        self._open_attempt(run)

        if self._cancel_requested(run.id):
            return self._cancel_before_work(run)

        handler = {
            "server.check": self._run_server_check,
            "app.deploy": self._run_deploy,
            "app.rollback": self._run_deploy,
            "template.apply": self._run_template_apply,
        }[claimed.kind]

        try:
            return handler(claimed, run)
        except RunBlocked as exc:
            detail = redact_text(str(exc))
            self.log(run.id, f"run failed: {detail}", level="error")
            self._note_check(run, "failed", detail)
            return self._fail(run, detail)
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
    # Connection
    # ------------------------------------------------------------------
    @contextmanager
    def _session_to(self, server: dict[str, Any]) -> Iterator[tuple[ExecContext, Any]]:
        """Stage the key and yield a live ``(ctx, port)`` pair.

        The private key only exists on disk inside this ``with``; the context
        carries a path, never the material. Anything that needs a command on
        this host goes through here, which is also where the host-key gate and
        the credential lookup live — one place to refuse, not three.

        Raises :class:`RunBlocked` rather than returning a half-open session.
        """
        credential_id = server.get("credential_id")
        if not credential_id:
            raise RunBlocked("server has no credential bound")

        try:
            secret = decrypt_secret(self._load_credential_payload(int(credential_id)))
        except (CryptoError, TypeError, ValueError) as exc:
            raise RunBlocked(redact_text(str(exc))) from exc

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
            yield ctx, self._make_port()

    def _require_verified_host(self, run: TaskRun, server: dict[str, Any]) -> None:
        if server.get("host_key_state") != "verified" or not server.get("host_key_fingerprint"):
            raise RunBlocked(
                "host key is not verified; an operator must confirm the fingerprint "
                "before any command runs"
            )

    # ------------------------------------------------------------------
    def _run_server_check(self, claimed: ClaimedRun, run: TaskRun) -> RunOutcome:
        """Collect system facts and store a snapshot.

        One step, ``SAFE`` to re-run: it only reads, and a second facts row is
        append-only history rather than corruption.
        """
        server = self._load_server(run.server_id)
        if server is None:
            return self._fail(run, f"server {run.server_id} no longer exists")

        try:
            self._require_verified_host(run, server)
        except RunBlocked as exc:
            detail = redact_text(str(exc))
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

        started = time.monotonic()
        try:
            with self._session_to(server) as (ctx, port):
                facts = collect_facts(port, ctx)
        except RunBlocked as exc:
            duration = int((time.monotonic() - started) * 1000)
            step.duration_ms = duration
            return self._fail_step(run, step, redact_text(str(exc)))
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
    # Deployment
    # ------------------------------------------------------------------
    def _run_deploy(self, claimed: ClaimedRun, run: TaskRun) -> RunOutcome:
        """Run the §8.2 plan, and undo the link if the live half fails.

        The rule that matters: every step *before* ``switch_current`` can fail
        without touching what is live, so a failure there just stops. Every
        step from the switch on can leave the host on a release that does not
        work, so those failures put the previous release back first and only
        then report — and the deployment record says ``ROLLED_BACK`` rather
        than ``FAILED``, because those are different facts.

        A first deploy has no previous release and therefore no undo. That is
        surfaced as ``FAILED`` with an explicit note, not as a quiet success.
        """
        params = dict(claimed.params or {})
        raw = dict(params.get("deploy") or {})
        deployment_id = int(params.get("deployment_id") or 0)
        try:
            inputs = inputs_from_mapping(
                raw, secrets=self._load_config_secrets(self._config_ref(params, raw))
            )
        except (ValueError, TypeError) as exc:
            raise RunBlocked(redact_text(str(exc))) from exc

        try:
            plan = build_deploy_plan(inputs)
        except ValueError as exc:
            raise RunBlocked(redact_text(str(exc))) from exc

        server = self._load_server(run.server_id)
        if server is None:
            raise RunBlocked(f"server {run.server_id} no longer exists")
        self._require_verified_host(run, server)

        self._deployment_started(deployment_id, run.id)
        self.log(
            run.id,
            f"{plan.kind} of release {inputs.release_id} "
            f"({len(plan.steps)} steps) onto {inputs.app_root}",
        )

        with self._session_to(server) as (ctx, port):
            outcome = self._run_plan_steps(run, plan.steps, ctx, port)

            if outcome is None:
                self._deployment_finished(deployment_id, "SUCCEEDED")
                self.log(run.id, f"release {inputs.release_id} is live")
                return self._succeed(run, self._last_step(run))

            if outcome.cancelled:
                # ``_finish_cancel`` already parked the run; only the record left.
                self._deployment_finished(deployment_id, "CANCELLED", outcome.detail)
                return outcome

            # A step failed. Was it past the point of no return?
            failed_key = outcome.failed_step_key
            failed_index = next(
                (i for i, s in enumerate(plan.steps) if s.key == failed_key), -1
            )
            if failed_index < plan.switch_index:
                # The live release was never touched.
                self._deployment_finished(deployment_id, "FAILED", outcome.detail)
                return self._fail(
                    run, outcome.detail, failed_step_key=outcome.failed_step_key
                )

            restore = build_restore_plan(inputs, plan)
            if not restore:
                self.log(
                    run.id,
                    "the link was flipped and there is no previous release to "
                    "put back; the host is on the new release and needs a human",
                    level="error",
                )
                self._deployment_finished(deployment_id, "FAILED", outcome.detail)
                return self._fail(
                    run, outcome.detail, failed_step_key=outcome.failed_step_key
                )

            self.log(
                run.id,
                f"restoring release {plan.restore_release_id} after {failed_key} failed",
                level="warning",
            )
            restore_outcome = self._run_plan_steps(run, restore, ctx, port)
            if restore_outcome is not None and not restore_outcome.cancelled:
                self.log(
                    run.id,
                    f"restore failed too: {restore_outcome.detail}",
                    level="error",
                )
                detail = (
                    f"{outcome.detail}; restore also failed: {restore_outcome.detail}"
                )
                self._deployment_finished(deployment_id, "FAILED", detail)
                return self._fail(
                    run, detail, failed_step_key=outcome.failed_step_key
                )

            self._deployment_finished(deployment_id, "ROLLED_BACK", outcome.detail)
            return self._fail(
                run,
                f"{outcome.detail}; rolled back to {plan.restore_release_id}",
                failed_step_key=outcome.failed_step_key,
            )

    def _run_template_apply(self, claimed: ClaimedRun, run: TaskRun) -> RunOutcome:
        """Run one published template's plan on one host.

        Deliberately **no restore policy**. ``app.deploy`` can undo itself
        because the release layout leaves it a previous ``current`` to put back;
        a template has no such half-live state. "Un-create the user" or
        "unwrite the config" are separate work with separate permissions, so a
        failure stops at the failing step, says which one, and leaves the
        already-completed steps standing.
        """
        try:
            payload = TemplateApplyParams.model_validate(dict(claimed.params or {}))
        except ValueError as exc:
            raise RunBlocked(redact_text(f"invalid template.apply body: {exc}")) from exc

        server = self._load_server(run.server_id)
        if server is None:
            raise RunBlocked(f"server {run.server_id} no longer exists")
        self._require_verified_host(run, server)

        ref = payload.template
        try:
            # The bundle is resolved here, never on the API side: a ref in the
            # task, plaintext only in this process.
            secrets = self._load_config_secrets(payload.config_ref)
            plan = plan_template_apply(
                payload, self._server_field_map(server), secrets=secrets
            )
        except TemplateError as exc:
            raise RunBlocked(redact_text(str(exc))) from exc

        self.log(
            run.id,
            f"applying template {ref.name} {ref.version} "
            f"({len(plan)} steps, checksum {ref.checksum[:12]}) "
            f"onto server {run.server_id}",
        )

        with self._session_to(server) as (ctx, port):
            outcome = self._run_plan_steps(run, plan, ctx, port)

        if outcome is None:
            self.log(run.id, f"template {ref.name} {ref.version} applied")
            return self._succeed(run, self._last_step(run))

        if outcome.cancelled:
            # ``_cancel_before_work`` / ``_cancel_during_step`` already parked
            # the run; only the caller's logging is left to do.
            return outcome

        self.log(
            run.id,
            f"template {ref.name} {ref.version} stopped at {outcome.failed_step_key}",
            level="error",
        )
        return self._fail(
            run, outcome.detail, failed_step_key=outcome.failed_step_key
        )

    def _run_plan_steps(
        self,
        run: TaskRun,
        steps: Sequence[PlanStep],
        ctx: ExecContext,
        port: Any,
    ) -> RunOutcome | None:
        """Run *steps* in order. ``None`` means every one of them succeeded.

        A failure returns a :class:`RunOutcome` **without** touching the run's
        status: the caller may still have an undo to perform, and a run marked
        ``FAILED`` is not one you can keep working in. **The caller must
        therefore transition the run before returning** — a run left in
        ``EXECUTING`` looks live to lease recovery, gets requeued, and the next
        execution collides with the step rows this one already wrote.
        """
        for item in steps:
            if self._cancel_requested(run.id):
                return self._cancel_before_work(run)

            if not self._heartbeat(run):
                # Someone else's recovery pass took the run. Do not write a
                # terminal status over theirs — just stop working on it.
                return RunOutcome(RUN_FAILED, "lease lost mid-run", failed_step_key=item.key)

            step = self._start_step(
                run,
                step_key=item.key,
                action=item.action,
                idempotency_class=item.idempotency_class,
                params=item.params,
            )

            if self._cancel_requested(run.id):
                return self._cancel_during_step(
                    run, step, signal_sent=False, remote_exit=None
                )

            started = time.monotonic()
            try:
                action = resolve_action(item.action, port)
                result = action.run(ctx, action.build_params(item.params).model_dump())
            except (TransportError, ValueError) as exc:
                step.duration_ms = int((time.monotonic() - started) * 1000)
                detail = redact_text(str(exc))
                self._finish_step_failed(step, detail)
                self.log(run.id, f"{item.key} failed: {detail}", level="error", step_id=step.id)
                return RunOutcome(
                    RUN_FAILED, f"{item.key}: {detail}", failed_step_key=item.key
                )

            step.duration_ms = int((time.monotonic() - started) * 1000)
            if not result.ok:
                detail = redact_text(result.summary or f"{item.action} failed")
                self._finish_step_failed(step, detail, result=result)
                self.log(run.id, f"{item.key} failed: {detail}", level="error", step_id=step.id)
                return RunOutcome(
                    RUN_FAILED, f"{item.key}: {detail}", failed_step_key=item.key
                )

            self._finish_step_ok(step, result)
            self.log(run.id, f"{item.key}: {result.summary}", step_id=step.id)
        return None

    def _finish_step_ok(self, step: TaskStep, result: Any) -> None:
        step.status = "SUCCESS"
        step.finished_at = utcnow()
        step.exit_code = 0
        step.result_json = self._result_snapshot(result)
        self.session.commit()

    def _finish_step_failed(
        self, step: TaskStep, detail: str, *, result: Any = None
    ) -> None:
        step.status = "FAILED"
        step.finished_at = utcnow()
        step.result_json = {
            **(self._result_snapshot(result) if result is not None else {}),
            "error": detail,
        }
        self.session.commit()

    def _result_snapshot(self, result: Any) -> dict[str, Any]:
        """``ActionResult`` → ``result_json``, redacted on the way in.

        The summary and every argv element go through :func:`redact_text`, and
        ``data`` through :func:`redact`. This is the last place the result is
        still structured, so it is the last place that can strip something
        before it becomes a database column.
        """
        data = redact(dict(getattr(result, "data", None) or {}))
        payload: dict[str, Any] = {
            **(data if isinstance(data, dict) else {"value": data}),
            "ok": bool(getattr(result, "ok", False)),
            "changed": bool(getattr(result, "changed", False)),
            "summary": redact_text(str(getattr(result, "summary", "") or "")),
            "argv": [redact_text(str(a)) for a in (getattr(result, "argv", ()) or ())],
        }
        return payload

    def _last_step(self, run: TaskRun) -> TaskStep:
        return self.session.scalars(
            select(TaskStep)
            .where(TaskStep.run_id == run.id)
            .order_by(TaskStep.id.desc())
            .limit(1)
        ).one()

    # -- deployment record (raw SQL: the ORM models live in the API) ------
    @staticmethod
    def _config_ref(params: dict[str, Any], deploy: dict[str, Any]) -> str:
        """Where the environment's secret reference lives.

        On the task params, next to ``deploy`` — the API writes it there, and
        the deploy mapping itself is the public half. A ref inside ``deploy``
        is accepted too so a hand-built task does not silently deploy with an
        empty env.
        """
        return str(params.get("config_ref") or deploy.get("config_ref") or "")

    def _load_config_secrets(self, ref: str) -> dict[str, str]:
        """Resolve ``config_ref`` into the env entries the host needs.

        The reference is on the task; the values never are. Same split as the
        SSH private key, for the same reason.
        """
        if not ref:
            return {}
        payload: str | None = self.session.execute(
            text("SELECT payload_encrypted FROM ops_credentials WHERE secret_ref = :ref"),
            {"ref": ref},
        ).scalar_one_or_none()
        if not payload:
            raise RunBlocked(f"config_ref {ref!r} does not resolve to a stored secret")
        plaintext = decrypt_secret(str(payload))
        try:
            decoded = json.loads(plaintext)
        except ValueError as exc:
            raise RunBlocked(f"config_ref {ref!r} is not a JSON object") from exc
        if not isinstance(decoded, dict):
            raise RunBlocked(f"config_ref {ref!r} is not a JSON object")
        out: dict[str, str] = {}
        for key, value in decoded.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise RunBlocked(f"config_ref {ref!r} must map strings to strings")
            out[key] = value
        return out

    def _deployment_started(self, deployment_id: int, task_id: int) -> None:
        if not deployment_id:
            return
        self.session.execute(
            text(
                """
                UPDATE ops_deployments
                   SET status = 'RUNNING',
                       task_id = :tid,
                       started_at = UTC_TIMESTAMP(3),
                       updated_at = UTC_TIMESTAMP(3)
                 WHERE id = :id AND status = 'PENDING'
                """
            ),
            {"id": deployment_id, "tid": task_id},
        )
        self.session.commit()

    def _deployment_finished(
        self, deployment_id: int, status: str, detail: str = ""
    ) -> None:
        if not deployment_id:
            return
        self.session.execute(
            text(
                """
                UPDATE ops_deployments
                   SET status = :status,
                       error_detail = :detail,
                       finished_at = UTC_TIMESTAMP(3),
                       updated_at = UTC_TIMESTAMP(3)
                 WHERE id = :id
                """
            ),
            {"id": deployment_id, "status": status, "detail": detail[:512]},
        )
        self.session.commit()

    # ------------------------------------------------------------------
    # Step helpers
    # ------------------------------------------------------------------
    def _open_attempt(self, run: TaskRun) -> None:
        """Advance ``run.attempt`` past anything already recorded for this run.

        ``claim_runs`` and ``apply_recovery`` deliberately leave ``attempt``
        alone: the bump belongs to the worker that actually re-executes, so a
        crash between requeue and re-claim cannot skip a number. Steps are
        unique on ``(run_id, step_key, attempt)``, so re-running at the number a
        dead worker used would collide with rows it already wrote — and the
        resulting ``IntegrityError`` poisons the session and the run for good.
        """
        used = self.session.scalar(
            select(func.max(TaskStep.attempt)).where(TaskStep.run_id == run.id)
        )
        if used is not None and int(used) >= int(run.attempt):
            run.attempt = int(used) + 1
            self.session.commit()

    def _heartbeat(self, run: TaskRun) -> bool:
        """Push the lease out mid-run. False means we no longer own the run.

        A deploy runs for minutes and ``lease_seconds`` is tens of seconds.
        Without this the lease lapses while this worker is still busy, recovery
        treats the run as orphaned, and a second execution writes the same step
        rows as the first. ``renew_leases`` only updates rows we still own, so a
        drop is a real loss rather than a clock quirk — stop, and let whoever
        holds it now finish the job.
        """
        renewed = renew_leases(
            self.session,
            owner=self.worker_id,
            run_ids=[run.id],
            lease_seconds=self.settings.lease_seconds,
        )
        return renewed == 1

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
            # What we persist, not what we run: a rendered env file or unit is
            # exactly where a deployment puts a secret, and params_json is a
            # column the task API hands back to anyone with task.read.
            params_json=redact_params(params),
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

    def _fail(
        self,
        run: TaskRun,
        detail: str,
        *,
        failed_step_key: str = "",
    ) -> RunOutcome:
        ensure_run_transition(run.status, RUN_FAILED)
        run.status = RUN_FAILED
        run.finished_at = utcnow()
        run.lease_owner = ""
        run.lease_expires_at = None
        run.error_detail = detail[:8000]
        self.session.commit()
        self.log(run.id, f"run failed: {detail}", level="error")
        return RunOutcome(RUN_FAILED, detail, failed_step_key=failed_step_key)

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
                SELECT id, name, hostname, port, ssh_user, credential_id,
                       host_key_state, host_key_fingerprint
                  FROM ops_servers
                 WHERE id = :id
                """
            ),
            {"id": server_id},
        ).mappings().first()
        return dict(row) if row is not None else None

    @staticmethod
    def _server_field_map(server: dict[str, Any]) -> dict[str, Any]:
        """The ``{{ server.x }}`` names a template may read from this host.

        Whitelist, not passthrough: ``credential_id`` and
        ``host_key_fingerprint`` are in the same row and a template must not be
        able to name them. ``shared.templates.SERVER_VALUE_KEYS`` is the
        declared set, and a name outside it is already refused at template load.
        """
        return {
            key: server[key]
            for key in SERVER_VALUE_KEYS
            if key in server and server[key] is not None
        }

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
