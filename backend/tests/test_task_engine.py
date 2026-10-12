"""Task-engine storage contract against real MySQL.

Covers the parts that SQLite cannot be trusted with and that break hardest in
production: ``FOR UPDATE SKIP LOCKED`` claiming under concurrency, the three
unique constraints, lease expiry recovery, and the rule that ``UNKNOWN`` is
never re-queued by anything other than a human.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

import pytest
from shared.dbtypes import utcnow
from shared.task_protocol import (
    DECISION_RENEW,
    DECISION_REQUEUE,
    DECISION_UNKNOWN,
    IDEMPOTENCY_DESTRUCTIVE,
    IDEMPOTENCY_SAFE,
    PROBE_FINISHED,
    PROBE_STILL_RUNNING,
    PROBE_SUCCEEDED,
    PROBE_UNREACHABLE,
    RUN_CANCELLED,
    RUN_EXECUTING,
    RUN_FAILED,
    RUN_PRECHECK,
    RUN_SUCCESS,
    RUN_UNKNOWN,
    RUN_WAITING,
    STEP_RUNNING,
    STEP_SUCCESS,
    STEP_TERMINAL,
    STEP_UNKNOWN,
    TASK_CANCELLED,
    TASK_CANCELLING,
    TASK_PARTIAL_SUCCESS,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SUCCESS,
    ApiIdempotency,
    Task,
    TaskLog,
    TaskRun,
    TaskStep,
    apply_recovery,
    claim_runs,
    decide_recovery,
    find_expired_runs,
    recompute_task_status,
    release_run,
    renew_leases,
)
from shared.task_protocol.models import LeaseRecovery
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _make_task(
    session: Session,
    *,
    kind: str = "server.check",
    status: str = TASK_QUEUED,
    idempotency_key: str = "",
    params: dict[str, Any] | None = None,
    cancel_requested: bool = False,
) -> Task:
    task = Task(
        kind=kind,
        title=f"{kind} task",
        status=status,
        idempotency_key=idempotency_key,
        params_json=params or {"n": 1},
        target_snapshot_json={"servers": [1]},
        requested_by=1,
        requested_by_name="tester",
        queued_at=utcnow(),
    )
    if cancel_requested:
        task.cancel_requested_at = utcnow()
    session.add(task)
    session.flush()
    return task


def _make_run(
    session: Session,
    task: Task,
    server_id: int,
    *,
    status: str = RUN_WAITING,
    lease_owner: str = "",
    lease_expires_at: dt.datetime | None = None,
    attempt: int = 1,
) -> TaskRun:
    run = TaskRun(
        task_id=task.id,
        server_id=server_id,
        server_snapshot_json={"hostname": "127.0.0.1", "port": 22},
        status=status,
        attempt=attempt,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        heartbeat_at=utcnow() if lease_owner else None,
    )
    session.add(run)
    session.flush()
    return run


def _make_step(
    session: Session,
    run: TaskRun,
    *,
    step_key: str = "ensure_user",
    action: str = "system.ensure_user",
    status: str = STEP_RUNNING,
    idempotency_class: str = IDEMPOTENCY_DESTRUCTIVE,
    attempt: int | None = None,
) -> TaskStep:
    step = TaskStep(
        run_id=run.id,
        step_key=step_key,
        attempt=run.attempt if attempt is None else attempt,
        action=action,
        status=status,
        idempotency_class=idempotency_class,
        started_at=utcnow(),
    )
    session.add(step)
    session.flush()
    return step


def _factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


# --------------------------------------------------------------------------
class TestUniqueConstraints:
    """The three constraints are concurrency's foundation. Softening any of
    them means duplicate work, duplicate side effects, and lost history."""

    def test_duplicate_idempotency_key_is_rejected(self, db: Session) -> None:
        _make_task(db, idempotency_key="client-key-1")
        db.commit()
        with pytest.raises(Exception) as exc:
            _make_task(db, idempotency_key="client-key-1")
            db.commit()
        assert "duplicate" in str(exc.value).lower()
        db.rollback()

    def test_one_run_per_task_and_server(self, db: Session) -> None:
        task = _make_task(db)
        _make_run(db, task, 7)
        db.commit()
        with pytest.raises(Exception) as exc:
            _make_run(db, task, 7)
            db.commit()
        assert "duplicate" in str(exc.value).lower()
        db.rollback()

    def test_one_step_per_run_key_and_attempt(self, db: Session) -> None:
        task = _make_task(db)
        run = _make_run(db, task, 7)
        db.commit()
        with pytest.raises(Exception) as exc:
            for _ in range(2):
                db.add(
                    TaskStep(
                        run_id=run.id,
                        step_key="install",
                        attempt=1,
                        action="package.install",
                        status="PENDING",
                    )
                )
            db.commit()
        assert "duplicate" in str(exc.value).lower()
        db.rollback()

    def test_retry_is_a_new_attempt_not_a_new_row(self, db: Session) -> None:
        task = _make_task(db)
        run = _make_run(db, task, 7, status=RUN_FAILED)
        db.commit()
        run.status = RUN_WAITING
        run.attempt = 2
        db.commit()
        rows = list(db.execute(select(TaskRun).where(TaskRun.task_id == task.id)).scalars())
        assert len(rows) == 1
        assert rows[0].attempt == 2


# --------------------------------------------------------------------------
class TestAtomicClaim:
    def test_claim_marks_run_precheck_and_holds_a_lease(self, db: Session) -> None:
        task = _make_task(db)
        run = _make_run(db, task, 7)
        db.commit()

        got = claim_runs(db, owner="worker-a", limit=5, lease_seconds=30)
        assert [c.run_id for c in got] == [run.id]
        assert got[0].kind == "server.check"
        assert got[0].server_id == 7

        db.refresh(run)
        assert run.status == RUN_PRECHECK
        assert run.lease_owner == "worker-a"
        assert run.lease_expires_at is not None
        assert run.lease_expires_at > utcnow()
        assert run.heartbeat_at is not None

    def test_claim_skips_runs_someone_else_holds(self, db: Session) -> None:
        task = _make_task(db)
        held = _make_run(
            db, task, 7,
            status=RUN_EXECUTING,
            lease_owner="worker-a",
            lease_expires_at=utcnow() + dt.timedelta(seconds=60),
        )
        free = _make_run(db, task, 8)
        db.commit()

        got = claim_runs(db, owner="worker-b", limit=5, lease_seconds=30)
        assert [c.run_id for c in got] == [free.id]
        db.refresh(held)
        assert held.lease_owner == "worker-a"

    def test_concurrent_claim_gives_each_run_to_exactly_one_worker(
        self, db: Session, engine: Engine
    ) -> None:
        """The race this whole design exists to survive.

        N runs, two workers, a start barrier. Every run must be claimed by
        exactly one of them, and no run may be claimed twice.
        """
        n_runs = 20
        for i in range(n_runs):
            task = _make_task(db, idempotency_key=f"race-{i}")
            _make_run(db, task, server_id=100 + i)
        db.commit()
        db.close()

        factory = _factory(engine)
        barrier = threading.Barrier(2)
        results: dict[str, list[int]] = {"worker-a": [], "worker-b": []}
        errors: list[BaseException] = []

        def worker(name: str) -> None:
            session = factory()
            try:
                barrier.wait(timeout=10)
                claimed = claim_runs(
                    session, owner=name, limit=n_runs, lease_seconds=30
                )
                results[name] = [c.run_id for c in claimed]
            except BaseException as exc:
                errors.append(exc)
            finally:
                session.close()

        threads = [
            threading.Thread(target=worker, args=("worker-a",)),
            threading.Thread(target=worker, args=("worker-b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        a = set(results["worker-a"])
        b = set(results["worker-b"])
        assert a.isdisjoint(b), f"both workers claimed: {sorted(a & b)}"
        assert len(a) + len(b) == n_runs, f"lost runs: {sorted(set(range(1, n_runs + 1)) - a - b)}"

        # And the leases on disk agree with who thinks they own what.
        session = factory()
        try:
            rows = list(session.execute(select(TaskRun)).scalars())
            owners = {row.lease_owner for row in rows}
            assert owners <= {"worker-a", "worker-b"}
            for row in rows:
                expected = "worker-a" if row.id in a else "worker-b"
                assert row.lease_owner == expected
                assert row.status == RUN_PRECHECK
        finally:
            session.close()

    def test_claim_respects_limit(self, db: Session) -> None:
        task = _make_task(db)
        for sid in (1, 2, 3, 4, 5):
            _make_run(db, task, sid)
        db.commit()
        got = claim_runs(db, owner="w", limit=2, lease_seconds=30)
        assert len(got) == 2

    def test_empty_queue_returns_nothing(self, db: Session) -> None:
        assert claim_runs(db, owner="w", limit=5, lease_seconds=30) == []

    def test_attempt_does_not_change_on_claim(self, db: Session) -> None:
        task = _make_task(db)
        run = _make_run(db, task, 7, attempt=3)
        db.commit()
        claim_runs(db, owner="w", limit=5, lease_seconds=30)
        db.refresh(run)
        assert run.attempt == 3


class TestLease:
    def test_renew_pushes_the_lease_out(self, db: Session) -> None:
        task = _make_task(db)
        run = _make_run(db, task, 7)
        db.commit()
        claim_runs(db, owner="w", limit=5, lease_seconds=30)
        before = run.lease_expires_at

        renewed = renew_leases(db, owner="w", run_ids=[run.id], lease_seconds=300)
        assert renewed == 1
        db.refresh(run)
        assert run.lease_expires_at is not None
        assert before is not None
        assert run.lease_expires_at >= before

    def test_renew_does_not_work_for_the_wrong_owner(self, db: Session) -> None:
        task = _make_task(db)
        run = _make_run(
            db, task, 7,
            status=RUN_EXECUTING,
            lease_owner="worker-a",
            lease_expires_at=utcnow() + dt.timedelta(seconds=60),
        )
        db.commit()
        assert renew_leases(db, owner="worker-b", run_ids=[run.id], lease_seconds=60) == 0

    def test_heartbeat_never_touches_attempt(self, db: Session) -> None:
        task = _make_task(db)
        run = _make_run(db, task, 7, attempt=4)
        db.commit()
        claim_runs(db, owner="w", limit=5, lease_seconds=30)
        renew_leases(db, owner="w", run_ids=[run.id], lease_seconds=30)
        renew_leases(db, owner="w", run_ids=[run.id], lease_seconds=30)
        db.refresh(run)
        assert run.attempt == 4


# --------------------------------------------------------------------------
class TestLeaseRecovery:
    def _expired_run(self, db: Session) -> TaskRun:
        task = _make_task(db)
        run = _make_run(
            db, task, 7,
            status=RUN_EXECUTING,
            lease_owner="dead-worker",
            lease_expires_at=utcnow() - dt.timedelta(seconds=5),
            attempt=2,
        )
        db.commit()
        return run

    def test_expired_runs_are_candidates_not_failures(self, db: Session) -> None:
        run = self._expired_run(db)
        found = find_expired_runs(db, limit=10)
        assert [r.id for r in found] == [run.id]
        # Finding them changes nothing on its own.
        db.refresh(run)
        assert run.status == RUN_EXECUTING

    def test_live_leases_are_not_recovered(self, db: Session) -> None:
        task = _make_task(db)
        _make_run(
            db, task, 7,
            status=RUN_EXECUTING,
            lease_owner="alive",
            lease_expires_at=utcnow() + dt.timedelta(seconds=60),
        )
        db.commit()
        assert find_expired_runs(db, limit=10) == []

    def test_still_running_renews_and_keeps_the_attempt(self, db: Session) -> None:
        run = self._expired_run(db)
        outcome = apply_recovery(
            db, run_id=run.id, decision=DECISION_RENEW, probe=PROBE_STILL_RUNNING,
            evidence={"probe": "systemctl is-active"}, decided_by="recovery",
        )
        assert outcome is not None and outcome.decision == DECISION_RENEW
        db.refresh(run)
        assert run.status == RUN_EXECUTING
        assert run.attempt == 2
        assert run.lease_expires_at is not None

    def test_safe_step_is_requeued_without_bumping_attempt(self, db: Session) -> None:
        run = self._expired_run(db)
        apply_recovery(
            db, run_id=run.id, decision=DECISION_REQUEUE, probe=PROBE_FINISHED,
            decided_by="recovery",
        )
        db.refresh(run)
        assert run.status == RUN_WAITING
        assert run.lease_owner == ""
        # The bump belongs to whoever actually re-executes.
        assert run.attempt == 2

    def test_destructive_step_goes_unknown_and_stays_there(self, db: Session) -> None:
        run = self._expired_run(db)
        apply_recovery(
            db, run_id=run.id, decision=DECISION_UNKNOWN, probe=PROBE_FINISHED,
            evidence={"why": "no success evidence"}, decided_by="recovery",
        )
        db.refresh(run)
        assert run.status == RUN_UNKNOWN

        # Nothing in the worker may pull it back out: it is no longer a
        # recovery candidate, and no lease holder can finish it off.
        assert find_expired_runs(db, limit=10) == []
        assert (
            release_run(db, run_id=run.id, owner="anyone", status=RUN_SUCCESS) is False
        )
        db.refresh(run)
        assert run.status == RUN_UNKNOWN

    def test_unreachable_is_unknown(self, db: Session) -> None:
        run = self._expired_run(db)
        apply_recovery(
            db, run_id=run.id,
            decision=decide_recovery(PROBE_UNREACHABLE, IDEMPOTENCY_SAFE),
            probe=PROBE_UNREACHABLE,
            decided_by="recovery",
        )
        db.refresh(run)
        assert run.status == RUN_UNKNOWN

    def test_remote_success_is_recorded_without_rerunning(self, db: Session) -> None:
        run = self._expired_run(db)
        apply_recovery(
            db, run_id=run.id,
            decision=decide_recovery(PROBE_SUCCEEDED, IDEMPOTENCY_DESTRUCTIVE),
            probe=PROBE_SUCCEEDED,
            evidence={"readlink": "/opt/opspilot/apps/demo/current"},
            decided_by="recovery",
        )
        db.refresh(run)
        assert run.status == RUN_SUCCESS

    def test_every_decision_leaves_an_audit_row(self, db: Session) -> None:
        run = self._expired_run(db)
        apply_recovery(
            db, run_id=run.id, decision=DECISION_UNKNOWN, probe=PROBE_UNREACHABLE,
            evidence={"error": "connection refused"}, decided_by="recovery",
        )
        rows = list(
            db.execute(
                select(LeaseRecovery).where(LeaseRecovery.run_id == run.id)
            ).scalars()
        )
        assert len(rows) == 1
        assert rows[0].decision == DECISION_UNKNOWN
        assert rows[0].decided_by == "recovery"
        assert rows[0].evidence is not None

    def test_recovery_evidence_is_scrubbed(self, db: Session) -> None:
        run = self._expired_run(db)
        apply_recovery(
            db, run_id=run.id, decision=DECISION_UNKNOWN, probe=PROBE_UNREACHABLE,
            evidence={"password": "hunter2", "host": "10.0.0.1"},
            decided_by="recovery",
        )
        row = next(iter(db.execute(
                select(LeaseRecovery).where(LeaseRecovery.run_id == run.id)
            ).scalars()))
        assert row.evidence is not None
        assert "hunter2" not in str(row.evidence)
        assert "10.0.0.1" in str(row.evidence)

    # -- steps left behind by the dead worker ------------------------------
    #
    # Caught in the live S4 recovery drill: a deploy SIGKILLed mid
    # ``ensure_user`` came back with the run parked ``UNKNOWN`` while the step
    # still read ``RUNNING``. Nothing was running. The run was judged; the step
    # record had to be judged too.
    def test_recovery_closes_the_step_it_died_on(self, db: Session) -> None:
        run = self._expired_run(db)
        step = _make_step(db, run, step_key="ensure_user", status=STEP_RUNNING)
        _make_step(
            db, run, step_key="check_os", status=STEP_SUCCESS,
            idempotency_class=IDEMPOTENCY_SAFE,
        )
        db.commit()

        apply_recovery(
            db, run_id=run.id, decision=DECISION_UNKNOWN, probe=PROBE_FINISHED,
            decided_by="recovery",
        )
        db.refresh(step)
        assert step.status == STEP_UNKNOWN
        assert step.finished_at is not None

    def test_recovery_leaves_a_finished_step_alone(self, db: Session) -> None:
        run = self._expired_run(db)
        done = _make_step(
            db, run, step_key="check_os", status=STEP_SUCCESS,
            idempotency_class=IDEMPOTENCY_SAFE,
        )
        db.commit()

        apply_recovery(
            db, run_id=run.id, decision=DECISION_UNKNOWN, probe=PROBE_FINISHED,
            decided_by="recovery",
        )
        db.refresh(done)
        assert done.status == STEP_SUCCESS
        assert done.finished_at is None

    def test_requeue_leaves_no_step_claiming_to_run(self, db: Session) -> None:
        """A requeued attempt is abandoned; its steps must not look live."""
        run = self._expired_run(db)
        _make_step(db, run, step_key="check_os", status=STEP_SUCCESS,
                   idempotency_class=IDEMPOTENCY_SAFE)
        _make_step(db, run, step_key="collect_facts", status=STEP_RUNNING,
                   idempotency_class=IDEMPOTENCY_SAFE)
        db.commit()

        apply_recovery(
            db, run_id=run.id, decision=DECISION_REQUEUE, probe=PROBE_FINISHED,
            decided_by="recovery",
        )
        db.refresh(run)
        assert run.status == RUN_WAITING
        still_running = list(
            db.execute(
                select(TaskStep).where(
                    TaskStep.run_id == run.id,
                    TaskStep.status.notin_(sorted(STEP_TERMINAL)),
                )
            ).scalars()
        )
        assert still_running == []

    def test_renew_keeps_the_step_running_because_the_remote_is(self, db: Session) -> None:
        run = self._expired_run(db)
        step = _make_step(db, run, step_key="install_release", status=STEP_RUNNING)
        db.commit()

        apply_recovery(
            db, run_id=run.id, decision=DECISION_RENEW, probe=PROBE_STILL_RUNNING,
            decided_by="recovery",
        )
        db.refresh(step)
        assert step.status == STEP_RUNNING
        assert step.finished_at is None


# --------------------------------------------------------------------------
class TestReleaseAndAggregate:
    def test_release_writes_a_terminal_status(self, db: Session) -> None:
        task = _make_task(db)
        run = _make_run(db, task, 7)
        db.commit()
        claim_runs(db, owner="w", limit=5, lease_seconds=30)
        assert release_run(db, run_id=run.id, owner="w", status=RUN_SUCCESS) is True
        db.refresh(run)
        assert run.status == RUN_SUCCESS
        assert run.lease_owner == ""
        assert run.finished_at is not None

    def test_release_refuses_a_run_we_do_not_hold(self, db: Session) -> None:
        task = _make_task(db)
        run = _make_run(
            db, task, 7,
            status=RUN_EXECUTING,
            lease_owner="someone-else",
            lease_expires_at=utcnow() + dt.timedelta(seconds=60),
        )
        db.commit()
        assert release_run(db, run_id=run.id, owner="me", status=RUN_SUCCESS) is False

    def test_recompute_folds_runs_into_the_task(self, db: Session) -> None:
        task = _make_task(db, status=TASK_RUNNING)
        _make_run(db, task, 1, status=RUN_SUCCESS)
        _make_run(db, task, 2, status=RUN_FAILED)
        db.commit()
        got = recompute_task_status(db, task_id=task.id)
        assert got == TASK_PARTIAL_SUCCESS
        db.refresh(task)
        assert task.status == TASK_PARTIAL_SUCCESS
        assert task.finished_at is not None

    def test_recompute_is_idempotent(self, db: Session) -> None:
        task = _make_task(db, status=TASK_RUNNING)
        _make_run(db, task, 1, status=RUN_SUCCESS)
        db.commit()
        assert recompute_task_status(db, task_id=task.id) == TASK_SUCCESS
        # Second call must not raise on the now-terminal task.
        assert recompute_task_status(db, task_id=task.id) == TASK_SUCCESS

    def test_cancel_requested_dominates(self, db: Session) -> None:
        task = _make_task(db, status=TASK_CANCELLING, cancel_requested=True)
        _make_run(db, task, 1, status=RUN_CANCELLED)
        _make_run(db, task, 2, status=RUN_UNKNOWN)
        db.commit()
        assert recompute_task_status(db, task_id=task.id) == TASK_CANCELLED


# --------------------------------------------------------------------------
class TestIdempotencyStore:
    def test_same_key_same_hash_replays(self, db: Session) -> None:
        db.add(
            ApiIdempotency(
                scope="POST /api/v1/tasks",
                idempotency_key="k1",
                request_hash="abc",
                response_status=201,
                response_body={"id": 42},
            )
        )
        db.commit()
        row = db.execute(
            select(ApiIdempotency).where(ApiIdempotency.idempotency_key == "k1")
        ).scalar_one()
        assert row.response_status == 201
        assert row.request_hash == "abc"

    def test_same_key_is_unique_within_a_scope(self, db: Session) -> None:
        with pytest.raises(Exception) as exc:
            for _ in range(2):
                db.add(
                    ApiIdempotency(
                        scope="POST /api/v1/tasks",
                        idempotency_key="k1",
                        request_hash="abc",
                        response_status=201,
                        response_body={},
                    )
                )
            db.commit()
        assert "duplicate" in str(exc.value).lower()
        db.rollback()

    def test_same_key_in_another_scope_is_fine(self, db: Session) -> None:
        for scope in ("POST /api/v1/tasks", "POST /api/v1/deployments"):
            db.add(
                ApiIdempotency(
                    scope=scope, idempotency_key="k1", request_hash="h",
                    response_status=201, response_body={},
                )
            )
        db.commit()


# --------------------------------------------------------------------------
class TestLogOrdering:
    def test_logs_are_ordered_by_seq_within_a_run(self, db: Session) -> None:
        task = _make_task(db)
        run = _make_run(db, task, 7)
        db.commit()
        for i in (3, 1, 2):
            db.add(TaskLog(run_id=run.id, seq=i, message=f"line {i}", level="info"))
        db.commit()
        rows = list(
            db.execute(
                select(TaskLog).where(TaskLog.run_id == run.id).order_by(TaskLog.seq)
            ).scalars()
        )
        assert [r.seq for r in rows] == [1, 2, 3]
