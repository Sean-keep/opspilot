"""Worker execution of ``template.apply``: one published body, one host.

What matters here is the *policy*, not the plumbing:

* a run that fails **stops at the failing step and says which one** — there is
  deliberately no restore, because a template has no previous ``current`` to
  put back and "un-create the user" is separate work with separate permissions
* the steps that already completed **stand**. Nothing is unwound behind the
  failure, and nothing is run ahead of it
* secret material resolved from ``config_ref`` reaches the host and nothing
  else — never a persisted step parameter, never a log line, never a result
  snapshot

The transport here answers the plan's argv instead of opening a socket, so the
assertions are about what the runner asked a host to do.
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
CONFIG_REF = "cred:cfg-77"
CHECKSUM = "b" * 64


# --------------------------------------------------------------------------
# A host that answers the template's argv
# --------------------------------------------------------------------------
class TemplatePort:
    """Answers the fixture template's commands; records what it was asked.

    ``fail_at`` is an argv *prefix*. When the host is asked for that command it
    returns a non-zero exit, which is how a real host reports a failure, so the
    action layer takes its ordinary ``ActionResult.failure`` path rather than a
    test-only one.
    """

    def __init__(self, *, fail_at: tuple[str, ...] | None = None, user_exists: bool = False):
        self.calls: list[tuple[str, ...]] = []
        self.fail_at = fail_at
        self.user_exists = user_exists
        self.created_users: list[str] = []
        self.bodies: list[str] = []

    # -- inspection ------------------------------------------------------
    def heads(self) -> list[str]:
        return [c[0] for c in self.calls]

    def calls_with(self, *prefix: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[: len(prefix)] == prefix]

    def saw_body(self, needle: str) -> bool:
        return any(needle in b for b in self.bodies)

    # -- execution -------------------------------------------------------
    def run(self, ctx: ExecContext, argv: Any, *, stdin: Any = None) -> ExecResult:
        argv = tuple(argv)
        self.calls.append(argv)
        if self.fail_at is not None and argv[: len(self.fail_at)] == self.fail_at:
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr=f"injected failure at {self.fail_at!r}",
                argv=argv,
            )
        return self._answer(argv, stdin)

    def _ok(self, argv: tuple[str, ...], out: str = "") -> ExecResult:
        return ExecResult(exit_code=0, stdout=out, stderr="", argv=argv)

    def _missing(self, argv: tuple[str, ...]) -> ExecResult:
        return ExecResult(exit_code=1, stdout="", stderr="not found", argv=argv)

    def _answer(self, argv: tuple[str, ...], stdin: Any) -> ExecResult:
        head = argv[0]
        args = list(argv[1:])

        if head == "cat" and args[-1] == "/etc/os-release":
            return self._ok(argv, 'NAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\n')

        if head == "df":
            return self._ok(
                argv,
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                "/dev/sda1 100000000 40000000 60000000 40% /\n",
            )

        if head == "id":
            # ``id -u -- <name>``: present means the account already exists and
            # ``ensure_user`` will skip. A missing account is what makes the
            # create branch run at all.
            return self._ok(argv) if self.user_exists else self._missing(argv)

        if head == "sudo":
            if "/usr/sbin/useradd" in args:
                self.created_users.append(args[-1])
            return self._ok(argv)

        if head == "dd" and stdin is not None:
            body = stdin.decode() if isinstance(stdin, bytes) else str(stdin)
            self.bodies.append(body)
            return self._ok(argv)

        if head == "redis-cli":
            return self._ok(argv, "PONG\n")

        return self._ok(argv)


# --------------------------------------------------------------------------
# The fixture template
# --------------------------------------------------------------------------
#: Three steps with a real middle effect (``ensure_user`` creates an account),
#: so "the completed steps stand" is something observable rather than a claim.
STEPS: list[dict[str, Any]] = [
    {"key": "check_os", "action": "system.check_os", "params": {"expect": ["ubuntu-24.04"]}},
    {
        "key": "ensure_user",
        "action": "system.ensure_user",
        "params": {"username": "{{ inputs.app_user }}", "shell": "/usr/sbin/nologin"},
    },
    {
        "key": "probe",
        "action": "redis.precheck",
        "params": {"host": "{{ inputs.redis_host }}", "port": "{{ inputs.redis_port }}"},
    },
]

INPUTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["app_user"],
    "properties": {
        "app_user": {"type": "string"},
        "redis_host": {"type": "string"},
        "redis_port": {"type": "integer"},
    },
}

#: Same shape, plus one ``x-secret`` input and a step that writes it into a
#: file body — the ordinary way a template hands a password to a host.
SECRET_STEPS: list[dict[str, Any]] = [
    *STEPS[:1],
    {
        "key": "render_config",
        "action": "file.render",
        "params": {
            "path": "/etc/redis/redis.conf",
            "content": "requirepass {{ inputs.db_password }}\n",
            "mode": "0640",
        },
    },
]

SECRET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["app_user", "db_password"],
    "properties": {
        "app_user": {"type": "string"},
        "db_password": {"type": "string", "x-secret": True},
    },
}


def _body(
    *,
    steps: list[dict[str, Any]] | None = None,
    schema: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    config_ref: str = "",
    name: str = "demo",
) -> dict[str, Any]:
    spec = {
        "api_version": "v1",
        "name": name,
        "version": "1.0.0",
        "description": "worker fixture",
        "inputs_schema": "schemas/inputs.schema.json",
        "steps": steps if steps is not None else STEPS,
    }
    return {
        "template": {"name": name, "version": "1.0.0", "checksum": CHECKSUM},
        "spec": spec,
        "inputs_schema": schema if schema is not None else INPUTS_SCHEMA,
        "inputs": inputs
        if inputs is not None
        else {"app_user": "billing", "redis_host": "127.0.0.1", "redis_port": 6379},
        "config_ref": config_ref,
    }


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def _seed_server(db: Session, *, with_config: bool = False) -> int:
    from backend.app.credentials.models import Credential
    from backend.app.inventory.models import HOSTKEY_VERIFIED, Server

    cred = Credential(
        name="tpl-cred",
        kind="ssh_key",
        description="",
        status="active",
        secret_ref="cred:ssh-7",
        payload_encrypted=encrypt_secret("FAKE KEY"),
        fingerprint="SHA256:aaaa",
    )
    db.add(cred)
    db.flush()
    server = Server(
        name="tpl-host",
        hostname="127.0.0.1",
        port=22,
        ssh_user="opstest",
        credential_id=cred.id,
        description="",
        status="active",
        host_key_state=HOSTKEY_VERIFIED,
        host_key_fingerprint="SHA256:" + "B" * 43,
        host_key_algorithm="ssh-ed25519",
        last_check_detail="",
    )
    db.add(server)
    if with_config:
        db.add(
            Credential(
                name="tpl-config",
                kind="env_file",
                description="",
                status="active",
                secret_ref=CONFIG_REF,
                payload_encrypted=encrypt_secret(json.dumps({"db_password": SECRET_VALUE})),
                fingerprint="",
            )
        )
    db.commit()
    return int(server.id)


def _seed_task(
    db: Session,
    server_id: int,
    body: dict[str, Any],
    *,
    kind: str = "template.apply",
) -> tuple[Task, TaskRun]:
    raw_ref: Any = body.get("template")
    ref: dict[str, Any] = raw_ref if isinstance(raw_ref, dict) else {}
    title = f"apply {ref.get('name', '?')} {ref.get('version', '?')}"
    task = Task(
        kind=kind,
        title=title,
        status="QUEUED",
        idempotency_key=None,
        params_json=body,
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


def _execute(db: Session, settings: Settings, port: TemplatePort) -> Any:
    claimed = claim_runs(db, owner="worker-a", limit=5, lease_seconds=60)
    assert len(claimed) == 1, claimed
    runner = Runner(db, settings, worker_id="worker-a", port_factory=lambda: port)
    return runner.execute(claimed[0])


def _step_rows(db: Session, run_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            "SELECT step_key, status, params_json, result_json "
            "FROM ops_task_steps WHERE run_id = :id ORDER BY id"
        ),
        {"id": run_id},
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        params = json.loads(r[2]) if isinstance(r[2], str) else (r[2] or {})
        result = json.loads(r[3]) if isinstance(r[3], str) else (r[3] or {})
        out.append({"step_key": r[0], "status": r[1], "params": params, "result": result})
    return out


def _run_state(db: Session, run_id: int) -> dict[str, Any]:
    row = db.execute(
        text(
            "SELECT status, error_detail FROM ops_task_runs WHERE id = :id"
        ),
        {"id": run_id},
    ).one()
    return {"status": row[0], "error_detail": row[1] or ""}


def _logs(db: Session, run_id: int) -> str:
    rows = db.execute(
        text("SELECT message FROM ops_task_logs WHERE run_id = :id ORDER BY seq"),
        {"id": run_id},
    ).fetchall()
    return "\n".join(r[0] for r in rows)


def _step_params_blob(db: Session, run_id: int) -> str:
    rows = db.execute(
        text("SELECT params_json FROM ops_task_runs r "
             "JOIN ops_task_steps s ON s.run_id = r.id WHERE r.id = :id"),
        {"id": run_id},
    ).fetchall()
    return "".join(
        (r[0] if isinstance(r[0], str) else json.dumps(r[0] or {})) for r in rows
    )


# --------------------------------------------------------------------------
class TestTemplateApplyHappyPath:
    def test_the_whole_plan_runs_and_the_run_ends_succeeded(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run = _seed_task(db, sid, _body())
        port = TemplatePort()

        outcome = _execute(db, settings, port)
        assert outcome.status == "SUCCESS", outcome.detail

        rows = _step_rows(db, run.id)
        assert [s["step_key"] for s in rows] == ["check_os", "ensure_user", "probe"]
        assert all(s["status"] == "SUCCESS" for s in rows)
        assert _run_state(db, run.id)["status"] == "SUCCESS"
        assert "applied" in _logs(db, run.id)

    def test_the_last_step_really_pings(
        self, db: Session, settings: Settings
    ) -> None:
        """The run is only successful because the host answered, not because
        the runner got to the end of its list."""
        sid = _seed_server(db)
        _task, _run = _seed_task(db, sid, _body())
        port = TemplatePort()

        _execute(db, settings, port)
        pings = port.calls_with("redis-cli")
        assert len(pings) == 1
        assert pings[0][-1] == "PING"
        assert pings[0][pings[0].index("-p") + 1] == "6379"

    def test_the_account_is_created_with_a_nologin_shell(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, _run = _seed_task(db, sid, _body())
        port = TemplatePort()  # user missing -> create branch

        _execute(db, settings, port)
        assert port.created_users == ["billing"]
        add = port.calls_with("sudo", "-n", "/usr/sbin/useradd")[0]
        assert "--system" in add
        assert "--shell" in add and "/usr/sbin/nologin" in add

    def test_an_existing_account_is_left_alone(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, _run = _seed_task(db, sid, _body())
        port = TemplatePort(user_exists=True)

        outcome = _execute(db, settings, port)
        assert outcome.status == "SUCCESS", outcome.detail
        assert port.created_users == [], "must not recreate an existing user"
        rows = _step_rows(db, _run.id)
        assert rows[1]["step_key"] == "ensure_user"
        assert rows[1]["result"]["changed"] is False


# ==========================================================================
# A failure stops at the failing step and restores nothing
# ==========================================================================
class TestTemplateApplyHasNoRestore:
    def test_a_mid_plan_failure_stops_at_the_failing_step(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run = _seed_task(db, sid, _body())
        port = TemplatePort(fail_at=("redis-cli",))

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert outcome.failed_step_key == "probe"

        rows = _step_rows(db, run.id)
        assert [s["step_key"] for s in rows] == ["check_os", "ensure_user", "probe"]
        assert [s["status"] for s in rows] == ["SUCCESS", "SUCCESS", "FAILED"]

        state = _run_state(db, run.id)
        assert state["status"] == "FAILED"
        assert "probe" in state["error_detail"]
        assert "stopped at probe" in _logs(db, run.id)

    def test_nothing_after_the_failure_is_run(
        self, db: Session, settings: Settings
    ) -> None:
        """Four steps, fail the third: the fourth must never be asked for."""
        steps = [
            *STEPS,
            {
                "key": "never",
                "action": "system.check_disk",
                "params": {"path": "/opt", "min_available_mb": 1},
            },
        ]
        sid = _seed_server(db)
        _task, run = _seed_task(db, sid, _body(steps=steps))
        port = TemplatePort(fail_at=("redis-cli",))

        _execute(db, settings, port)
        assert [s["step_key"] for s in _step_rows(db, run.id)] == [
            "check_os",
            "ensure_user",
            "probe",
        ]
        assert not port.calls_with("df", "-Pm", "--", "/opt")

    def test_the_completed_steps_are_left_standing(
        self, db: Session, settings: Settings
    ) -> None:
        """This is the whole no-restore rule, stated as an observation.

        ``ensure_user`` created an account before the probe failed. A deploy
        would undo its work; a template must not, because "un-create the user"
        is a different operation with a different permission. The account is
        still there, and no unwinding command was ever issued.
        """
        sid = _seed_server(db)
        _task, run = _seed_task(db, sid, _body())
        port = TemplatePort(fail_at=("redis-cli",))

        _execute(db, settings, port)
        assert port.created_users == ["billing"]

        undo = [c for c in port.calls if c[0] in {"userdel", "groupdel", "rm", "mv", "ln"}]
        assert undo == [], undo
        assert not port.calls_with("sudo", "-n", "/usr/sbin/userdel")

        rows = _step_rows(db, run.id)
        assert rows[1]["step_key"] == "ensure_user"
        assert rows[1]["status"] == "SUCCESS"
        assert rows[1]["result"]["ok"] is True

    def test_a_first_step_failure_never_starts_the_rest(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run = _seed_task(db, sid, _body())
        port = TemplatePort(fail_at=("cat", "/etc/os-release"))

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert outcome.failed_step_key == "check_os"
        assert [s["step_key"] for s in _step_rows(db, run.id)] == ["check_os"]
        assert port.created_users == []
        assert not port.calls_with("redis-cli")

    def test_a_template_apply_never_builds_a_restore_plan(
        self, db: Session, settings: Settings
    ) -> None:
        """The difference from ``app.deploy`` in one shape.

        A deploy that fails after the switch flips ``current`` back. There is
        no equivalent here, so the run must not grow extra steps on failure —
        no hidden "undo" phase that a reader of the step list would have to
        know to ignore.
        """
        sid = _seed_server(db)
        _task, run = _seed_task(db, sid, _body())
        port = TemplatePort(fail_at=("redis-cli",))

        _execute(db, settings, port)
        keys = [s["step_key"] for s in _step_rows(db, run.id)]
        assert keys == ["check_os", "ensure_user", "probe"]
        assert not any("restore" in k or "rollback" in k or "undo" in k for k in keys)


# ==========================================================================
# Secrets
# ==========================================================================
class TestTemplateApplySecrets:
    def test_a_secret_reaches_the_host_and_nothing_else(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db, with_config=True)
        _task, run = _seed_task(
            db,
            sid,
            _body(
                steps=SECRET_STEPS,
                schema=SECRET_SCHEMA,
                inputs={"app_user": "billing"},
                config_ref=CONFIG_REF,
            ),
        )
        port = TemplatePort()

        outcome = _execute(db, settings, port)
        assert outcome.status == "SUCCESS", outcome.detail

        # It reached the host.
        assert port.saw_body(SECRET_VALUE)

        # It did not reach the record.
        blob = _step_params_blob(db, run.id)
        assert SECRET_VALUE not in blob
        assert "[BODY" in blob, blob
        assert SECRET_VALUE not in _logs(db, run.id)

        result_blob = "".join(
            json.dumps(s["result"]) for s in _step_rows(db, run.id)
        )
        assert SECRET_VALUE not in result_blob

    def test_the_secret_is_not_stored_on_the_task_either(
        self, db: Session, settings: Settings
    ) -> None:
        """``config_ref`` is a pointer. If the value were in ``params_json`` of
        the task row the whole split would be decorative."""
        sid = _seed_server(db, with_config=True)
        _task, _run = _seed_task(
            db,
            sid,
            _body(
                steps=SECRET_STEPS,
                schema=SECRET_SCHEMA,
                inputs={"app_user": "billing"},
                config_ref=CONFIG_REF,
            ),
        )
        raw: Any = db.execute(
            text("SELECT params_json FROM ops_tasks WHERE id = :id"), {"id": _task.id}
        ).scalar_one()
        blob = raw if isinstance(raw, str) else json.dumps(raw)
        assert SECRET_VALUE not in blob
        assert CONFIG_REF in blob

    def test_an_unresolvable_config_ref_blocks_the_run(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)  # no config credential seeded
        _task, _run = _seed_task(
            db,
            sid,
            _body(
                steps=SECRET_STEPS,
                schema=SECRET_SCHEMA,
                inputs={"app_user": "billing"},
                config_ref="cred:nope",
            ),
        )
        port = TemplatePort()

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert "cred:nope" in outcome.detail
        # Nothing was attempted on the host.
        assert port.calls == []

    def test_a_template_with_no_secret_needs_no_bundle(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, _run = _seed_task(db, sid, _body(config_ref=""))
        port = TemplatePort()

        outcome = _execute(db, settings, port)
        assert outcome.status == "SUCCESS", outcome.detail


# ==========================================================================
# Refusals before any host is touched
# ==========================================================================
class TestTemplateApplyRefusals:
    def test_a_malformed_body_is_blocked_not_crashed(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, _run = _seed_task(db, sid, {"template": {}})  # nothing valid
        port = TemplatePort()

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert "template.apply body" in outcome.detail
        assert port.calls == [], "a body the API would never have approved runs nothing"

    def test_an_input_the_schema_does_not_declare_is_refused(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        body = _body(
            inputs={"app_user": "billing", "nope": "x", "redis_host": "h", "redis_port": 1}
        )
        _task, _run = _seed_task(db, sid, body)
        port = TemplatePort()

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert "nope" in outcome.detail
        assert port.calls == []

    def test_an_unverified_host_is_refused_before_any_command(
        self, db: Session, settings: Settings
    ) -> None:
        from backend.app.inventory.models import Server

        sid = _seed_server(db)
        server = db.get(Server, sid)
        assert server is not None
        server.host_key_state = "pending"
        db.commit()

        _task, _run = _seed_task(db, sid, _body())
        port = TemplatePort()

        outcome = _execute(db, settings, port)
        assert outcome.status == "FAILED"
        assert "host key" in outcome.detail.lower()
        assert port.calls == []


# ==========================================================================
# The record
# ==========================================================================
class TestTemplateApplyRecord:
    def test_the_record_moves_pending_running_succeeded(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run = _seed_task(db, sid, _body())
        seen: list[str] = []

        class Watching(TemplatePort):
            def run(self, ctx: Any, argv: Any, *, stdin: Any = None) -> ExecResult:
                seen.append(_run_state(db, run.id)["status"])
                return TemplatePort.run(self, ctx, argv, stdin=stdin)

        _execute(db, settings, Watching())
        assert _run_state(db, run.id)["status"] == "SUCCESS"
        assert seen[0] == "EXECUTING", seen

    def test_the_log_names_the_template_and_its_checksum(
        self, db: Session, settings: Settings
    ) -> None:
        sid = _seed_server(db)
        _task, run = _seed_task(db, sid, _body())
        port = TemplatePort()

        _execute(db, settings, port)
        logs = _logs(db, run.id)
        assert "demo 1.0.0" in logs
        assert CHECKSUM[:12] in logs
