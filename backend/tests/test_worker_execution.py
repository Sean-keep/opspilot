"""Worker execution: claim, run, cancel, recover.

These drive the real scheduler and runner against real MySQL, with a fake
transport so no socket is opened. The live-SSH path is covered separately by
``test_ssh_live.py``; what matters here is the state bookkeeping — that a
claimed run reaches a terminal state, that a cancel leaves evidence, and that
``UNKNOWN`` is never quietly turned back into work.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from shared.config import Settings
from shared.crypto import encrypt_secret
from shared.dbtypes import utcnow
from shared.executor.port import ExecContext, ExecResult, TransportError
from shared.task_protocol import (
    LeaseRecovery,
    Task,
    TaskLog,
    TaskRun,
    TaskStep,
    claim_runs,
    find_expired_runs,
    recompute_task_status,
)
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from worker.runner import Runner
from worker.scheduler import Scheduler

FAKE_SECRET = "-----BEGIN OPENSSH PRIVATE KEY-----\nTEST\n-----END OPENSSH PRIVATE KEY-----\n"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
class FakePort:
    """Returns canned facts; records what it was asked to run."""

    def __init__(self, *, fail: Exception | None = None, on_run: Any | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail = fail
        self.on_run = on_run

    def run(self, ctx: ExecContext, argv: Any) -> ExecResult:
        self.calls.append(tuple(argv))
        if self.on_run is not None:
            self.on_run()
        if self.fail is not None:
            raise self.fail
        joined = " ".join(argv)
        if joined.startswith("cat /etc/os-release"):
            out = 'NAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\n'
        elif joined.startswith("uname"):
            out = "Linux 6.8.0 x86_64\n"
        elif joined.startswith("nproc"):
            out = "4\n"
        elif joined.startswith("cat /proc/meminfo"):
            out = "MemTotal:       16000000 kB\nMemAvailable:    8000000 kB\n"
        elif joined.startswith("grep"):
            out = "model name\t: Test CPU\n"
        elif joined.startswith("df"):
            out = (
                "Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
                "/dev/sda1        100000000  40000000  60000000      40% /\n"
            )
        else:
            out = ""
        return ExecResult(exit_code=0, stdout=out, stderr="", argv=tuple(argv))


def _seed_server(db: Session, *, verified: bool = True) -> tuple[int, int]:
    from backend.app.credentials.models import Credential
    from backend.app.inventory.models import HOSTKEY_UNVERIFIED, HOSTKEY_VERIFIED, Server

    cred = Credential(
        name="w-cred",
        kind="ssh_key",
        description="",
        status="active",
        secret_ref="cred:w",
        payload_encrypted=encrypt_secret(FAKE_SECRET),
        fingerprint="SHA256:aaaa",
    )
    db.add(cred)
    db.flush()
    server = Server(
        name="w-host",
        hostname="127.0.0.1",
        port=22,
        ssh_user="opstest",
        credential_id=cred.id,
        description="",
        status="active",
        host_key_state=HOSTKEY_VERIFIED if verified else HOSTKEY_UNVERIFIED,
        host_key_fingerprint=("SHA256:" + "A" * 43) if verified else "",
        host_key_algorithm="ssh-ed25519",
        last_check_detail="",
    )
    db.add(server)
    db.commit()
    return int(server.id), int(cred.id)


def _seed_task(db: Session, server_id: int) -> tuple[Task, TaskRun]:
    task = Task(
        kind="server.check",
        title="check",
        status="QUEUED",
        idempotency_key="",
        params_json={},
        target_snapshot_json={"server_ids": [server_id]},
        requested_by=1,
        requested_by_name="tester",
        queued_at=utcnow(),
    )
    db.add(task)
    db.flush()
    run = TaskRun(
        task_id=task.id,
        server_id=server_id,
        server_snapshot_json={"server_id": server_id},
        status="WAITING",
        attempt=1,
    )
    db.add(run)
    db.commit()
    return task, run


def _claim(db: Session, owner: str = "worker-a") -> Any:
    claimed = claim_runs(db, owner=owner, limit=5, lease_seconds=30)
    assert len(claimed) == 1
    return claimed[0]


# --------------------------------------------------------------------------
class TestRunExecution:
    def test_claimed_run_executes_and_stores_facts(
        self, db: Session, settings: Settings
    ) -> None:
        server_id, _ = _seed_server(db)
        task, run = _seed_task(db, server_id)
        claimed = _claim(db)

        port = FakePort()
        runner = Runner(db, settings, worker_id="worker-a", port_factory=lambda: port)
        outcome = runner.execute(claimed)
        recompute_task_status(db, task_id=task.id)

        assert outcome.status == "SUCCESS"
        db.refresh(run)
        assert run.status == "SUCCESS"
        assert run.lease_owner == ""
        assert run.finished_at is not None

        steps = list(db.execute(select(TaskStep).where(TaskStep.run_id == run.id)).scalars())
        assert len(steps) == 1
        assert steps[0].step_key == "collect_facts"
        assert steps[0].status == "SUCCESS"
        assert steps[0].idempotency_class == "SAFE"
        assert steps[0].attempt == 1

        # A facts snapshot landed in the inventory table.
        facts = db.execute(
            text("SELECT os_name, arch, cpu_count FROM ops_server_facts WHERE server_id = :s"),
            {"s": server_id},
        ).mappings().first()
        assert facts is not None
        assert facts["arch"] == "x86_64"
        assert facts["cpu_count"] == 4

        logs = list(db.execute(select(TaskLog).where(TaskLog.run_id == run.id)).scalars())
        assert logs, "execution must leave a log trail"

        # And the task folded up.
        db.refresh(task)
        assert task.status == "SUCCESS"

    def test_unverified_host_key_refuses_to_run(
        self, db: Session, settings: Settings
    ) -> None:
        server_id, _ = _seed_server(db, verified=False)
        _task, run = _seed_task(db, server_id)
        claimed = _claim(db)

        port = FakePort()
        runner = Runner(db, settings, worker_id="worker-a", port_factory=lambda: port)
        outcome = runner.execute(claimed)

        assert outcome.status == "FAILED"
        assert port.calls == [], "no command may run on an untrusted host"
        db.refresh(run)
        assert run.status == "FAILED"
        assert "host key is not verified" in run.error_detail

    def test_transport_failure_marks_the_run_failed(
        self, db: Session, settings: Settings
    ) -> None:
        server_id, _ = _seed_server(db)
        _task, run = _seed_task(db, server_id)
        claimed = _claim(db)

        runner = Runner(
            db,
            settings,
            worker_id="worker-a",
            port_factory=lambda: FakePort(fail=TransportError("connection refused")),
        )
        outcome = runner.execute(claimed)

        assert outcome.status == "FAILED"
        db.refresh(run)
        assert run.status == "FAILED"
        assert "connection refused" in run.error_detail

    def test_log_messages_are_redacted(self, db: Session, settings: Settings) -> None:
        server_id, _ = _seed_server(db)
        _task, run = _seed_task(db, server_id)
        claimed = _claim(db)

        runner = Runner(
            db,
            settings,
            worker_id="worker-a",
            port_factory=lambda: FakePort(fail=TransportError("auth failed password=hunter2")),
        )
        runner.execute(claimed)

        logs = list(db.execute(select(TaskLog).where(TaskLog.run_id == run.id)).scalars())
        blob = " ".join(r.message for r in logs)
        assert "hunter2" not in blob


class TestCancelEvidence:
    def test_cancel_mid_step_records_remote_evidence(
        self, db: Session, settings: Settings
    ) -> None:
        server_id, _ = _seed_server(db)
        _task, run = _seed_task(db, server_id)
        claimed = _claim(db)

        # The operator stops the task while the remote work is in flight.
        def cancel_now() -> None:
            row = db.get(TaskRun, run.id)
            assert row is not None
            row.status = "CANCELLING"
            db.commit()

        port = FakePort(on_run=cancel_now)
        runner = Runner(db, settings, worker_id="worker-a", port_factory=lambda: port)
        outcome = runner.execute(claimed)

        assert outcome.cancelled is True
        assert port.calls, "the cancel must be observed after the step started"
        db.refresh(run)
        assert run.status == "CANCELLED"
        assert run.error_detail == "cancelled by operator"

        steps = list(db.execute(select(TaskStep).where(TaskStep.run_id == run.id)).scalars())
        assert steps, "a cancel mid-run must still leave step records"
        result = steps[0].result_json or {}
        cancel = result.get("cancel")
        assert cancel is not None, f"no cancel evidence in {result}"
        assert "cancel_requested_at" in cancel
        assert "remote_signal_sent" in cancel
        assert "remote_exit_after_signal" in cancel

    def test_cancel_before_any_step_leaves_a_log(
        self, db: Session, settings: Settings
    ) -> None:
        server_id, _ = _seed_server(db)
        _task, run = _seed_task(db, server_id)
        claimed = _claim(db)
        run.status = "CANCELLING"
        db.commit()

        runner = Runner(db, settings, worker_id="worker-a", port_factory=FakePort)
        runner.execute(claimed)

        logs = list(db.execute(select(TaskLog).where(TaskLog.run_id == run.id)).scalars())
        assert any("cancel" in r.message.lower() for r in logs)
        db.refresh(run)
        assert run.status == "CANCELLED"


class TestLeaseRecovery:
    def _expired(self, db: Session, *, step_class: str = "SAFE") -> TaskRun:
        server_id, _ = _seed_server(db)
        _task, run = _seed_task(db, server_id)
        run.status = "EXECUTING"
        run.lease_owner = "dead-worker"
        run.lease_expires_at = utcnow() - dt.timedelta(seconds=5)
        run.attempt = 1
        db.add(
            TaskStep(
                run_id=run.id,
                step_key="collect_facts",
                attempt=1,
                action="system.check_os",
                status="RUNNING",
                idempotency_class=step_class,
            )
        )
        db.commit()
        return run

    def test_safe_step_is_requeued_when_the_host_is_reachable(
        self, db: Session, settings: Settings
    ) -> None:
        run = self._expired(db, step_class="SAFE")

        scheduler = Scheduler(worker_id="recovery", settings=settings)
        # Inject a reachable probe by monkeypatching the transport.
        scheduler._probe_remote = lambda session, run: (  # type: ignore[method-assign]
            "finished",
            {"host": "127.0.0.1", "probe_argv": ["true"]},
        )
        assert scheduler._recover_expired(db) is True

        db.refresh(run)
        assert run.status == "WAITING"
        assert run.attempt == 1, "recovery must not bump attempt"
        rows = list(
            db.execute(select(LeaseRecovery).where(LeaseRecovery.run_id == run.id)).scalars()
        )
        assert rows and rows[0].decision == "requeued"

    def test_destructive_step_is_parked_as_unknown(
        self, db: Session, settings: Settings
    ) -> None:
        run = self._expired(db, step_class="DESTRUCTIVE")

        scheduler = Scheduler(worker_id="recovery", settings=settings)
        scheduler._probe_remote = lambda session, run: (  # type: ignore[method-assign]
            "finished", {"host": "127.0.0.1"}
        )
        assert scheduler._recover_expired(db) is True

        db.refresh(run)
        assert run.status == "UNKNOWN"
        assert run.attempt == 1

    def test_unreachable_host_is_unknown_even_for_safe_steps(
        self, db: Session, settings: Settings
    ) -> None:
        run = self._expired(db, step_class="SAFE")

        scheduler = Scheduler(worker_id="recovery", settings=settings)
        scheduler._probe_remote = lambda session, run: (  # type: ignore[method-assign]
            "unreachable", {"error": "refused"}
        )
        assert scheduler._recover_expired(db) is True

        db.refresh(run)
        assert run.status == "UNKNOWN"

    def test_unknown_is_not_reclaimed_as_work(
        self, db: Session, settings: Settings
    ) -> None:
        run = self._expired(db, step_class="DESTRUCTIVE")
        scheduler = Scheduler(worker_id="recovery", settings=settings)
        scheduler._probe_remote = lambda session, run: ("finished", {})  # type: ignore[method-assign]
        scheduler._recover_expired(db)
        db.refresh(run)
        assert run.status == "UNKNOWN"

        # A later tick must not pick it up again.
        assert find_expired_runs(db, limit=10) == []
        claimed = claim_runs(db, owner="worker-b", limit=5, lease_seconds=30)
        assert [c.run_id for c in claimed] != [run.id]


class TestSchedulerTick:
    def test_tick_runs_a_queued_task_end_to_end(
        self, db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server_id, _ = _seed_server(db)
        task, run = _seed_task(db, server_id)

        # Point worker.db at the test engine so Scheduler and the test share
        # one database.
        import worker.db as worker_db
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False, future=True)
        monkeypatch.setattr(worker_db, "_factory", factory)
        monkeypatch.setattr(worker_db, "_engine", db.get_bind())

        scheduler = Scheduler(worker_id="tick-worker", settings=settings)
        # No expired leases to recover, and a fake transport for the run.
        monkeypatch.setattr(
            scheduler,
            "_probe_remote",
            lambda session, r: ("unreachable", {"error": "unused"}),
        )

        original = Runner.__init__

        def patched_init(
            self: Any,
            session: Any,
            cfg: Any,
            *,
            worker_id: str,
            port_factory: Any = None,
        ) -> None:
            original(self, session, cfg, worker_id=worker_id, port_factory=FakePort)

        monkeypatch.setattr(Runner, "__init__", patched_init)

        assert scheduler.tick() is True

        db.refresh(run)
        assert run.status == "SUCCESS"
        db.refresh(task)
        assert task.status == "SUCCESS"

    def test_idle_tick_reports_no_work(
        self, db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import worker.db as worker_db
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False, future=True)
        monkeypatch.setattr(worker_db, "_factory", factory)

        scheduler = Scheduler(worker_id="idle-worker", settings=settings)
        assert scheduler.tick() is False
