"""Worker execution of a deployment: §8.2 end to end, with a scripted host.

These drive the real runner against real MySQL with a transport that answers
the plan's argv instead of opening a socket. What matters is the *policy*:

* a step that fails before the link flips stops the batch and leaves the live
  release alone
* a step that fails at or after the flip puts the previous release back
* a first deploy has no previous release, and says so rather than pretending
* a secret resolved from ``config_ref`` reaches the host and nothing else —
  never a step parameter, never a log line, never a result snapshot

The switch's argv shape (``ln -s`` then ``mv -Tf``, never ``rm`` of the live
link) is asserted in ``shared/tests/test_actions.py``; here it is asserted in
the context of a whole run, where it would be easy to lose.
"""

from __future__ import annotations

import json
from typing import Any

from shared.config import Settings
from shared.crypto import encrypt_secret
from shared.dbtypes import utcnow
from shared.executor.port import ExecContext, ExecResult
from shared.task_protocol import Task, TaskRun, claim_runs
from sqlalchemy import text
from sqlalchemy.orm import Session
from worker.runner import Runner

SECRET_VALUE = "hunter2-super-secret"
CONFIG_REF = "cred:cfg-99"


# --------------------------------------------------------------------------
# A host that answers the plan's argv
# --------------------------------------------------------------------------
class DeployPort:
    """Answers every command the deploy plan issues; records what it was asked.

    ``fail_at`` is an argv *prefix*. When the host is asked for that command it
    returns a non-zero exit, which is exactly how a real host reports a
    failure, so the action layer takes its normal ``ActionResult.failure``
    path rather than a special test-only one.

    ``fail_times`` limits that to the first *n* matches. A host whose restart
    is broken for the new release but works again once the previous one is
    back is the ordinary shape of a half-bad deploy, and it is the only way to
    observe a successful restore rather than a double failure.
    """

    def __init__(
        self,
        *,
        checksum: str,
        health_status: str = "200",
        fail_at: tuple[str, ...] | None = None,
        fail_times: int | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.checksum = checksum
        self.health_status = health_status
        self.fail_at = fail_at
        self.fail_times = fail_times
        self._failures = 0
        self.exists: set[str] = set()
        self.links: dict[str, str] = {}
        self.bodies: list[str] = []

    def admit_release(self, app_root: str, release_id: str) -> None:
        """Say a release tree is already on the host.

        The previous release has to be there for a restore to have something to
        point ``current`` back at — which is exactly the situation the restore
        plan is built for.
        """
        self.exists.add(f"{app_root}/releases/{release_id}")
        self.links[f"{app_root}/current"] = f"releases/{release_id}"

    def run(self, ctx: ExecContext, argv: Any, *, stdin: Any = None) -> ExecResult:
        argv = tuple(argv)
        self.calls.append(argv)
        matches = self.fail_at is not None and argv[: len(self.fail_at)] == self.fail_at
        under_budget = self.fail_times is None or self._failures < self.fail_times
        if matches and under_budget:
            self._failures += 1
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr=f"injected failure at {self.fail_at!r}",
                argv=argv,
            )
        return self._answer(argv, stdin)

    # -- helpers ---------------------------------------------------------
    def _ok(self, argv: tuple[str, ...], out: str = "") -> ExecResult:
        return ExecResult(exit_code=0, stdout=out, stderr="", argv=argv)

    def _missing(self, argv: tuple[str, ...]) -> ExecResult:
        return ExecResult(exit_code=1, stdout="", stderr="not found", argv=argv)

    def calls_with(self, *prefix: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[: len(prefix)] == prefix]

    def _answer(self, argv: tuple[str, ...], stdin: Any) -> ExecResult:
        head = argv[0]
        args = list(argv[1:])

        if head == "test" and "-e" in args:
            path = args[-1]
            return self._ok(argv) if path in self.exists else self._missing(argv)

        if head == "readlink":
            return self._ok(argv, self.links.get(args[-1], "") + "\n")

        if head == "ln" and "-s" in args:
            wanted, scratch = args[-2], args[-1]
            self.links[scratch] = wanted
            self.exists.add(scratch)
            return self._ok(argv)

        if head == "mv" and "-Tf" in args:
            scratch, link = args[-2], args[-1]
            if scratch not in self.links:
                return self._missing(argv)
            self.links[link] = self.links.pop(scratch)
            self.exists.discard(scratch)
            self.exists.add(link)
            return self._ok(argv)

        if head == "mv" and "-f" in args:
            self.exists.add(args[-1])
            return self._ok(argv)

        if head == "dd" and stdin is not None:
            body = stdin.decode() if isinstance(stdin, bytes) else str(stdin)
            self.bodies.append(body)
            return self._ok(argv)

        if head == "sha256sum":
            return self._ok(argv, f"{self.checksum}  {args[-1]}\n")

        if head == "tar":
            # tar -xzf <artifact> -C <target> --strip-components=1
            target = args[args.index("-C") + 1]
            self.exists.add(target)
            return self._ok(argv)

        if head == "mkdir":
            self.exists.add(args[-1])
            return self._ok(argv)

        if head == "curl":
            if "-w" in args:
                # The health probe reports the status code on stdout.
                return self._ok(argv, self.health_status)
            # The fetch: create whatever -o named.
            if "-o" in args:
                self.exists.add(args[args.index("-o") + 1])
            return self._ok(argv)

        if head == "cat" and args and args[-1] == "/etc/os-release":
            return self._ok(argv, 'NAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\n')

        if head == "df":
            return self._ok(
                argv,
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                "/dev/sda1 100000000 40000000 60000000 40% /\n",
            )

        if head == "id":
            return self._ok(argv, "1000\n")

        if head.startswith("python"):
            return self._ok(argv, "Python 3.12.3\n")

        if head == "sudo":
            # sudo -n <program> ... — the whitelist has already been checked.
            return self._ok(argv)

        if head == "/usr/bin/systemd-analyze":
            return self._ok(argv)

        if head in {"chmod", "chown", "cp", "rm"}:
            return self._ok(argv)

        return self._ok(argv)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def _seed_server(db: Session) -> int:
    from backend.app.credentials.models import Credential
    from backend.app.inventory.models import HOSTKEY_VERIFIED, Server

    cred = Credential(
        name="deploy-cred",
        kind="ssh_key",
        description="",
        status="active",
        secret_ref="cred:ssh-1",
        payload_encrypted=encrypt_secret("FAKE KEY"),
        fingerprint="SHA256:aaaa",
    )
    db.add(cred)
    db.flush()
    server = Server(
        name="deploy-host",
        hostname="127.0.0.1",
        port=22,
        ssh_user="opstest",
        credential_id=cred.id,
        description="",
        status="active",
        host_key_state=HOSTKEY_VERIFIED,
        host_key_fingerprint="SHA256:" + "A" * 43,
        host_key_algorithm="ssh-ed25519",
        last_check_detail="",
    )
    db.add(server)

    db.add(
        Credential(
            name="deploy-config",
            kind="env_file",
            description="",
            status="active",
            secret_ref=CONFIG_REF,
            payload_encrypted=encrypt_secret(json.dumps({"DB_PASSWORD": SECRET_VALUE})),
            fingerprint="",
        )
    )
    db.commit()
    return int(server.id)


def _seed_deployment(
    db: Session,
    server_id: int,
    *,
    kind: str = "deploy",
    release_id: str = "1.4.2",
    previous: str = "",
    config_ref: str = CONFIG_REF,
    with_secret: bool = True,
) -> tuple[Task, TaskRun, int]:
    from backend.app.applications.models import AppEnvironment, Application
    from backend.app.deployments.models import Deployment

    app = Application(name="billing-api", run_user="billing")
    db.add(app)
    db.flush()
    env = AppEnvironment(
        application_id=app.id,
        name="production",
        config_ref=CONFIG_REF,
        service_name="opspilot-billing",
        unit_path="/etc/systemd/system/opspilot-billing.service",
        health_url="http://127.0.0.1:8000/healthz",
        health_timeout_s=5,
    )
    db.add(env)
    db.flush()
    record = Deployment(
        application_id=app.id,
        environment_id=env.id,
        kind="DEPLOY" if kind == "deploy" else "ROLLBACK",
        release_version=release_id,
        release_id=release_id,
        previous_release_id=previous,
        artifact_url="https://artifacts.example.com/x.tar.gz",
        artifact_checksum="a" * 64,
        requested_by="tester",
    )
    db.add(record)
    db.flush()

    deploy: dict[str, Any] = {
        "app_root": "/opt/opspilot/apps/billing-api",
        "release_id": release_id,
        "kind": kind,
        "release_version": release_id,
        "artifact_url": "https://artifacts.example.com/x.tar.gz",
        "artifact_checksum": "a" * 64,
        "previous_release_id": previous,
        "run_user": "billing",
        "service_name": "opspilot-billing",
        "unit_path": "/etc/systemd/system/opspilot-billing.service",
        "python_version": "3.12",
        "requirements": "requirements.lock",
        "health_url": "http://127.0.0.1:8000/healthz",
        "health_timeout_s": 5,
        "module_name": "billing.main",
        "app_port": 8000,
        "config": {"LOG_LEVEL": "info"},
    }
    task = Task(
        kind="app.rollback" if kind != "deploy" else "app.deploy",
        title=f"{kind} {release_id}",
        status="QUEUED",
        idempotency_key=None,
        params_json={
            "deployment_id": record.id,
            "config_ref": config_ref if with_secret else "",
            "deploy": deploy,
        },
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
    return task, run, int(record.id)


def _execute(db: Session, settings: Settings, port: DeployPort) -> Any:
    claimed = claim_runs(db, owner="worker-a", limit=5, lease_seconds=60)
    assert len(claimed) == 1, claimed
    runner = Runner(
        db,
        settings,
        worker_id="worker-a",
        port_factory=lambda: port,
    )
    return runner.execute(claimed[0])


def _step_rows(db: Session, run_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            "SELECT step_key, status, params_json, result_json "
            "FROM ops_task_steps WHERE run_id = :id ORDER BY id"
        ),
        {"id": run_id},
    ).fetchall()
    return [
        {
            "step_key": r[0],
            "status": r[1],
            "params": json.loads(r[2]) if isinstance(r[2], str) else (r[2] or {}),
            "result": json.loads(r[3]) if isinstance(r[3], str) else (r[3] or {}),
        }
        for r in rows
    ]


def _deployment(db: Session, deployment_id: int) -> dict[str, Any]:
    row = db.execute(
        text(
            "SELECT status, error_detail, release_id, previous_release_id "
            "FROM ops_deployments WHERE id = :id"
        ),
        {"id": deployment_id},
    ).one()
    return {
        "status": row[0],
        "error_detail": row[1] or "",
        "release_id": row[2],
        "previous_release_id": row[3] or "",
    }


def _logs(db: Session, run_id: int) -> str:
    rows = db.execute(
        text("SELECT message FROM ops_task_logs WHERE run_id = :id ORDER BY seq"),
        {"id": run_id},
    ).fetchall()
    return "\n".join(r[0] for r in rows)


CHECKSUM = "a" * 64


# --------------------------------------------------------------------------
class TestDeployHappyPath:
    def test_the_whole_plan_runs_and_the_record_ends_succeeded(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run, dep = _seed_deployment(db, sid)
        port = DeployPort(checksum=CHECKSUM)

        outcome = _execute(db, settings, port)
        assert outcome.status == "SUCCESS", outcome.detail

        keys = [s["step_key"] for s in _step_rows(db, run.id)]
        assert keys == [
            "check_os",
            "check_disk",
            "ensure_user",
            "ensure_runtime",
            "fetch_release",
            "install_release",
            "render_config",
            "render_unit",
            "switch_current",
            "restart_service",
            "check_health",
        ]
        assert all(s["status"] == "SUCCESS" for s in _step_rows(db, run.id))
        assert _deployment(db, dep)["status"] == "SUCCEEDED"

    def test_the_link_flip_is_one_rename_over_the_old_link(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, _run_row, _dep = _seed_deployment(db, sid)
        port = DeployPort(checksum=CHECKSUM)

        _execute(db, settings, port)

        scratch = "/opt/opspilot/apps/billing-api/.current.opspilot-new"
        link = "/opt/opspilot/apps/billing-api/current"
        wanted = "releases/1.4.2"
        assert port.calls_with("ln", "-s", "--", wanted, scratch)
        assert port.calls_with("mv", "-Tf", "--", scratch, link)
        # The live link is never removed first — that would leave the host with
        # no `current` between the two commands.
        assert not port.calls_with("rm", "-f", "--", link)
        assert port.links[link] == wanted

    def test_the_unit_names_current_and_never_a_pinned_release(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, _run, _dep = _seed_deployment(db, sid, release_id="9.9.9")
        port = DeployPort(checksum=CHECKSUM)

        _execute(db, settings, port)

        unit = next(b for b in port.bodies if "ExecStart=" in b)
        assert "WorkingDirectory=/opt/opspilot/apps/billing-api/current" in unit
        assert "ExecStart=/opt/opspilot/apps/billing-api/current/venv/bin/python" in unit
        assert "releases/9.9.9" not in unit


class TestFailureBeforeTheSwitch:
    def test_a_fetch_failure_stops_the_batch_without_touching_the_link(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run, dep = _seed_deployment(db, sid, previous="1.4.1")
        port = DeployPort(checksum=CHECKSUM, fail_at=("curl", "-fsSL"))

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert outcome.failed_step_key == "fetch_release"

        keys = [s["step_key"] for s in _step_rows(db, run.id)]
        assert "fetch_release" in keys
        assert "switch_current" not in keys
        assert "restart_service" not in keys
        # Nothing to restore: the live release was never touched.
        assert not port.calls_with("ln", "-s", "--")
        assert _deployment(db, dep)["status"] == "FAILED"

    def test_a_checksum_mismatch_refuses_to_install(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run, dep = _seed_deployment(db, sid)
        port = DeployPort(checksum="b" * 64)  # what the host reports ≠ the pinned

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert outcome.failed_step_key == "fetch_release"
        assert "checksum" in outcome.detail.lower()
        keys = [s["step_key"] for s in _step_rows(db, run.id)]
        assert "install_release" not in keys
        assert _deployment(db, dep)["status"] == "FAILED"


class TestFailureAtOrAfterTheSwitch:
    def test_a_health_check_failure_puts_the_previous_release_back(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, _run_row, dep = _seed_deployment(db, sid, previous="1.4.1")
        port = DeployPort(checksum=CHECKSUM, health_status="503")
        # Only the old release is pre-installed: the deploy has to make the new
        # one live itself, so the flip under test is a real one.
        port.admit_release("/opt/opspilot/apps/billing-api", "1.4.1")

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert outcome.failed_step_key == "check_health"
        assert "rolled back to 1.4.1" in outcome.detail

        # The restore plan ran: current went back to the previous release.
        link = "/opt/opspilot/apps/billing-api/current"
        assert port.links[link] == "releases/1.4.1"
        restores = port.calls_with("ln", "-s", "--", "releases/1.4.1")
        assert restores, [c for c in port.calls if c[0] == "ln"]

        record = _deployment(db, dep)
        assert record["status"] == "ROLLED_BACK"
        assert record["release_id"] == "1.4.2"
        assert record["previous_release_id"] == "1.4.1"

    def test_a_restart_failure_also_restores(
        self, db: Session, settings: Settings
    ) -> None:
        """The service restart is the first step that can break the *new*
        release while the link is already on it. One broken restart, then a
        working one once the old release is back — which is what puts the
        deployment in ``ROLLED_BACK`` rather than ``FAILED``.
        """
        sid = _seed_server(db)
        _task, _run_row, dep = _seed_deployment(db, sid, previous="1.4.1")
        port = DeployPort(
            checksum=CHECKSUM,
            fail_at=("sudo", "-n", "/usr/bin/systemctl", "restart"),
            fail_times=1,
        )
        port.admit_release("/opt/opspilot/apps/billing-api", "1.4.1")

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert outcome.failed_step_key == "restart_service"
        assert "rolled back to 1.4.1" in outcome.detail

        # The switch really happened before the failure, and was undone after.
        link = "/opt/opspilot/apps/billing-api/current"
        assert port.links[link] == "releases/1.4.1"
        assert port.calls_with("ln", "-s", "--", "releases/1.4.1")
        assert _deployment(db, dep)["status"] == "ROLLED_BACK"

        # Both restarts were attempted: the new release's, then the old one's.
        restarts = port.calls_with("sudo", "-n", "/usr/bin/systemctl", "restart")
        assert len(restarts) == 2, restarts

    def test_a_first_deploy_that_fails_after_the_switch_has_nothing_to_put_back(
        self, db: Session, settings: Settings
    ) -> None:
        """Reported as FAILED with a note — not as a quiet success."""
        sid = _seed_server(db)
        _task, run, dep = _seed_deployment(db, sid, previous="")
        port = DeployPort(checksum=CHECKSUM, health_status="500")

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert "no previous release" in _logs(db, run.id)

        # The restore plan is empty: no second switch is attempted.
        assert not port.calls_with("ln", "-s", "--", "releases/")
        assert _deployment(db, dep)["status"] == "FAILED"


class TestSecretsNeverLeaveTheHostBoundary:
    def test_a_secret_reaches_the_env_file_and_nothing_else(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run, _dep = _seed_deployment(db, sid)
        port = DeployPort(checksum=CHECKSUM)

        _execute(db, settings, port)

        # It is on the host: the env file body carries it.
        env_body = next(b for b in port.bodies if "APP_RELEASE=" in b)
        assert f"DB_PASSWORD={SECRET_VALUE}" in env_body

        # It is not in any step parameter.
        for step in _step_rows(db, run.id):
            blob = json.dumps(step["params"], ensure_ascii=False)
            assert SECRET_VALUE not in blob, step["step_key"]
        render_config = next(s for s in _step_rows(db, run.id) if s["step_key"] == "render_config")
        assert render_config["params"]["content"] == (
            f"[BODY {len(env_body.encode('utf-8'))} bytes]"
        )

        # It is not in any log line, and not in any result snapshot.
        assert SECRET_VALUE not in _logs(db, run.id)
        for step in _step_rows(db, run.id):
            assert SECRET_VALUE not in json.dumps(step["result"], ensure_ascii=False)

    def test_an_unresolvable_config_ref_blocks_the_run(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, _run_row, dep = _seed_deployment(db, sid, config_ref="cred:nope")
        port = DeployPort(checksum=CHECKSUM)

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        # Nothing was attempted: the secret could not be resolved.
        assert port.calls == []
        assert _deployment(db, dep)["status"] == "PENDING"


class TestSwitchIdempotence:
    def test_rerunning_when_current_already_points_at_the_release_skips_the_flip(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run, dep = _seed_deployment(db, sid)
        port = DeployPort(checksum=CHECKSUM)
        # Simulate a prior run that already made the release live.
        port.exists.add("/opt/opspilot/apps/billing-api/releases/1.4.2")
        port.links["/opt/opspilot/apps/billing-api/current"] = "releases/1.4.2"

        outcome = _execute(db, settings, port)
        assert outcome.status == "SUCCESS", outcome.detail

        switch = next(s for s in _step_rows(db, run.id) if s["step_key"] == "switch_current")
        assert switch["status"] == "SUCCESS"
        assert switch["result"].get("changed") is False
        # No second rename was issued.
        assert not port.calls_with("mv", "-Tf", "--")
        assert _deployment(db, dep)["status"] == "SUCCEEDED"

    def test_a_switch_refuses_when_the_release_is_not_installed(
        self, db: Session, settings: Settings
    ) -> None:
        """The switch is the boundary: failing *in* it is treated as past it.

        The action refuses without touching the link, but the runner cannot
        know that, so it does the conservative thing and re-points ``current``
        at the previous release anyway. That is a no-op on a host the switch
        never touched, and the only safe answer on one where it half did.
        """
        sid = _seed_server(db)
        _task, _run_row, dep = _seed_deployment(db, sid, previous="1.4.1")
        port = DeployPort(checksum=CHECKSUM)
        port.admit_release("/opt/opspilot/apps/billing-api", "1.4.1")
        # Make install_release believe the tree exists, then take it away again
        # so the switch's own `test -e` sees a missing release.
        port.fail_at = None
        original_answer = port._answer

        def answer(argv: tuple[str, ...], stdin: Any) -> ExecResult:
            if (
                argv[:2] == ("test", "-e")
                and argv[-1].endswith("/releases/1.4.2")
                and "switching" in port.exists
            ):
                return port._missing(argv)
            return original_answer(argv, stdin)

        port._answer = answer  # type: ignore[method-assign]
        port.exists.add("switching")

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert outcome.failed_step_key == "switch_current"
        assert "rolled back to 1.4.1" in outcome.detail

        # The new release was never linked...
        assert not port.calls_with("ln", "-s", "--", "releases/1.4.2")
        # ...and the previous one is live either way.
        assert port.links["/opt/opspilot/apps/billing-api/current"] == "releases/1.4.1"
        assert _deployment(db, dep)["status"] == "ROLLED_BACK"


class TestDeploymentRecordLifecycle:
    def test_the_record_moves_pending_running_succeeded(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, _run_row, dep = _seed_deployment(db, sid)
        port = DeployPort(checksum=CHECKSUM)

        assert _deployment(db, dep)["status"] == "PENDING"
        _execute(db, settings, port)

        finished = _deployment(db, dep)
        assert finished["status"] == "SUCCEEDED"

        started = db.execute(
            text("SELECT started_at, finished_at FROM ops_deployments WHERE id = :id"),
            {"id": dep},
        ).one()
        assert started[0] is not None
        assert started[1] is not None

    def test_a_cancelled_deployment_says_cancelled(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        task, _run_row, dep = _seed_deployment(db, sid)
        port = DeployPort(checksum=CHECKSUM)
        # Ask for cancellation as soon as a step has been recorded.
        task.cancel_requested_at = utcnow()
        db.commit()

        outcome = _execute(db, settings, port)
        assert outcome.cancelled or outcome.status == "CANCELLED"
        assert _deployment(db, dep)["status"] in {"CANCELLED", "PENDING"}


# --------------------------------------------------------------------------
class TestTheRunAlwaysEndsTerminal:
    """A run left in ``EXECUTING`` is a live grenade.

    Lease recovery treats an ``EXECUTING`` run with a lapsed lease as orphaned
    and requeues it. The next execution then tries to write the same
    ``(run_id, step_key, attempt)`` step rows this one already wrote, the
    unique key fires, the ``IntegrityError`` poisons the session, and the run
    wedges for good — which is exactly what a real first deploy did. Every
    path out of ``Runner.execute`` must leave the run terminal and give up the
    lease.
    """

    def _run_state(self, db: Session, run_id: int) -> tuple[str, str]:
        row = db.execute(
            text(
                "SELECT status, COALESCE(lease_owner, '') AS owner "
                "FROM ops_task_runs WHERE id = :id"
            ),
            {"id": run_id},
        ).one()
        return str(row[0]), str(row[1])

    def test_a_pre_switch_failure_leaves_the_run_failed(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run, dep = _seed_deployment(db, sid)
        port = DeployPort(checksum=CHECKSUM, fail_at=("df",))

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert outcome.failed_step_key == "check_disk"

        status, owner = self._run_state(db, run.id)
        assert status == "FAILED", "the run must not be left EXECUTING"
        assert owner == ""
        assert _deployment(db, dep)["status"] == "FAILED"

    def test_a_rolled_back_deploy_still_fails_the_run(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run, dep = _seed_deployment(db, sid, previous="1.4.1")
        port = DeployPort(checksum=CHECKSUM, health_status="503")
        port.admit_release("/opt/opspilot/apps/billing-api", "1.4.1")

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert "rolled back to 1.4.1" in outcome.detail

        status, owner = self._run_state(db, run.id)
        assert status == "FAILED"
        assert owner == ""
        assert _deployment(db, dep)["status"] == "ROLLED_BACK"

    def test_a_first_deploy_with_nothing_to_put_back_fails_the_run(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run, _dep = _seed_deployment(db, sid)
        port = DeployPort(checksum=CHECKSUM, health_status="503")

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"

        status, owner = self._run_state(db, run.id)
        assert status == "FAILED"
        assert owner == ""

    def test_reexecuting_a_requeued_run_opens_a_new_attempt(
        self, db: Session, settings: Settings
    ) -> None:
        """Recovery requeues without bumping ``attempt``; the worker must.

        Steps are unique on ``(run_id, step_key, attempt)``. Re-running at the
        number the dead worker used collides with the rows it left behind.
        """
        sid = _seed_server(db)
        _task, run, _dep = _seed_deployment(db, sid)

        first = _execute(db, settings, DeployPort(checksum=CHECKSUM, fail_at=("df",)))
        assert first.status == "FAILED"
        attempts = {
            (r[0], r[1])
            for r in db.execute(
                text("SELECT step_key, attempt FROM ops_task_steps WHERE run_id = :id"),
                {"id": run.id},
            ).fetchall()
        }
        assert ("check_disk", 1) in attempts

        # What ``apply_recovery(DECISION_REQUEUE)`` leaves behind: WAITING, the
        # attempt number untouched, the step rows still there.
        db.execute(
            text(
                "UPDATE ops_task_runs SET status = 'WAITING', lease_owner = '', "
                "lease_expires_at = NULL, finished_at = NULL WHERE id = :id"
            ),
            {"id": run.id},
        )
        db.commit()

        second = _execute(db, settings, DeployPort(checksum=CHECKSUM))
        assert second.status == "SUCCESS", second.detail

        raw: Any = db.execute(
            text(
                "SELECT step_key, attempt, status FROM ops_task_steps "
                "WHERE run_id = :id ORDER BY attempt, id"
            ),
            {"id": run.id},
        ).fetchall()
        by_attempt: dict[int, list[tuple[str, str]]] = {}
        for key, attempt, status in raw:
            by_attempt.setdefault(int(attempt), []).append((str(key), str(status)))
        assert set(by_attempt) == {1, 2}, by_attempt
        assert ("check_os", "SUCCESS") in by_attempt[2]
        assert all(s == "SUCCESS" for _k, s in by_attempt[2])
        # The first attempt's rows are history, not overwritten.
        assert ("check_disk", "FAILED") in by_attempt[1]

        db.refresh(run)
        assert run.attempt == 2
