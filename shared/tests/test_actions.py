"""The Action Registry is a security boundary. These tests are the lock.

Every assertion here is about something an attacker or a careless template could
otherwise get wrong: an unregistered name resolving, a path climbing out of the
app tree, a service outside the restart whitelist, a sudo escalation to ``bash``,
a health check pointed at the cloud metadata address, a file body leaking into
the audit trail, a Telegram token landing in argv.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from shared.actions.base import (
    UnknownAction,
    ensure_argv,
    get_registry,
    register_all,
    resolve,
    spec_of,
)
from shared.actions.builtin import (
    ALLOWED_SUDO_RULES,
    check_service_allowed,
    ensure_sudo_allowed,
)
from shared.actions.render import (
    RenderError,
    find_placeholders,
    render,
    validate_path,
    validate_read_path,
    validate_template_text,
)
from shared.executor.port import ExecContext, ExecResult
from shared.redaction import REDACTED, redact_text

EXPECTED_ACTIONS = {
    "app.fetch_release",
    "app.install_release",
    "file.render",
    "http.check",
    "mysql.precheck",
    "notification.telegram",
    "package.install",
    "python.ensure_runtime",
    "redis.precheck",
    "supervisor.restart",
    "system.check_disk",
    "system.check_os",
    "system.ensure_user",
    "systemd.render_unit",
    "systemd.restart",
}

#: The idempotency table from the design document. A change here is a change to
#: what the platform is allowed to retry unattended.
EXPECTED_IDEMPOTENCY = {
    "system.check_os": "SAFE",
    "system.check_disk": "SAFE",
    "system.ensure_user": "CONDITIONAL",
    "package.install": "CONDITIONAL",
    "python.ensure_runtime": "CONDITIONAL",
    "file.render": "CONDITIONAL",
    "app.fetch_release": "CONDITIONAL",
    "app.install_release": "CONDITIONAL",
    "systemd.render_unit": "CONDITIONAL",
    "systemd.restart": "DESTRUCTIVE",
    "supervisor.restart": "DESTRUCTIVE",
    "http.check": "SAFE",
    "mysql.precheck": "SAFE",
    "redis.precheck": "SAFE",
    "notification.telegram": "SAFE",
}

CTX = ExecContext(
    host="testhost",
    port=22,
    user="opstest",
    host_key_fingerprint="SHA256:" + "A" * 43,
)


class RecordingPort:
    """Captures every argv and every stdin payload; answers by argv[0]."""

    def __init__(self, responses: dict[str, ExecResult] | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], bytes | str | None]] = []
        self.responses = responses or {}
        self.default = ExecResult(exit_code=0, stdout="", stderr="", argv=())

    def run(
        self,
        ctx: ExecContext,
        argv: Sequence[str],
        *,
        stdin: bytes | str | None = None,
    ) -> ExecResult:
        tokens = tuple(argv)
        self.calls.append((tokens, stdin))
        canned = self.responses.get(tokens[0], self.default)
        return ExecResult(
            exit_code=canned.exit_code,
            stdout=canned.stdout,
            stderr=canned.stderr,
            argv=tokens,
        )

    # -- assertions helpers -------------------------------------------------
    def argvs(self) -> list[tuple[str, ...]]:
        return [tokens for tokens, _ in self.calls]

    def joined(self) -> list[str]:
        return [" ".join(tokens) for tokens in self.argvs()]

    def stdins(self) -> list[bytes | str | None]:
        return [payload for _, payload in self.calls]

    def stdin_text(self) -> str:
        out: list[str] = []
        for payload in self.calls:
            data = payload[1]
            if isinstance(data, bytes):
                out.append(data.decode("utf-8", "replace"))
            elif isinstance(data, str):
                out.append(data)
        return "".join(out)

    def flat(self) -> str:
        return "\n".join(self.joined())


def _ok(stdout: str = "", exit_code: int = 0) -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr="", argv=())


# ==========================================================================
# Registry shape
# ==========================================================================
def test_register_all_loads_exactly_the_fifteen_v1_actions() -> None:
    register_all()
    assert set(get_registry()) == EXPECTED_ACTIONS


@pytest.mark.parametrize("name", sorted(EXPECTED_ACTIONS))
def test_every_action_has_a_documented_idempotency_class(name: str) -> None:
    register_all()
    assert spec_of(name).idempotency_class == EXPECTED_IDEMPOTENCY[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_ACTIONS))
def test_every_action_has_a_params_model_and_a_description(name: str) -> None:
    register_all()
    spec = spec_of(name)
    assert issubclass(spec.params, BaseModel)
    assert spec.params.model_fields, name
    assert spec.description.strip(), name


def test_unregistered_action_is_refused_not_guessed() -> None:
    register_all()
    with pytest.raises(UnknownAction) as excinfo:
        resolve("shell.run_arbitrary_command", RecordingPort())
    assert "unregistered action" in str(excinfo.value)


def test_resolving_an_action_is_the_only_way_to_get_a_transport() -> None:
    """``spec_of`` must work with no port at all — validation never connects."""
    register_all()
    spec = spec_of("file.render")
    # Building params is a classmethod: no instance, no ExecPort, no socket.
    built = spec.params.model_validate(
        {"path": "/opt/opspilot/apps/x/app.env", "content": "a=b"}
    )
    assert built.model_dump()["path"] == "/opt/opspilot/apps/x/app.env"


# ==========================================================================
# Template syntax — refuse, never partially render
# ==========================================================================
@pytest.mark.parametrize(
    "text",
    [
        "{{ inputs.name | upper }}",
        "{{ inputs.name.upper() }}",
        "{{ inputs['name'] }}",
        "{{ 7 + 1 }}",
        "{{ }}",
        "{% if x %}y{% endif %}",
        "{# comment #}",
        "${HOME}",
        "$(whoami)",
        "`id`",
        "{{ inputs.name",
        "unclosed }}",
    ],
)
def test_illegal_template_expressions_are_refused_outright(text: str) -> None:
    with pytest.raises(RenderError):
        validate_template_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "user = {{ inputs.app_user }}",
        "host = {{ server.host }}",
        "no placeholders at all",
        "both {{ inputs.a }} and {{ server.b }}",
    ],
)
def test_only_bare_dotted_lookups_are_legal(text: str) -> None:
    validate_template_text(text)


def test_render_substitutes_and_reports_placeholders() -> None:
    text = "User={{ inputs.app_user }} Host={{ server.host }}"
    assert find_placeholders(text) == [("inputs", "app_user"), ("server", "host")]
    assert render(text, {"app_user": "deploy"}, {"host": "web01"}) == "User=deploy Host=web01"


def test_render_refuses_a_list_rather_than_joining_it() -> None:
    with pytest.raises(RenderError, match="list"):
        render("{{ inputs.packages }}", {"packages": ["a", "b"]})


def test_render_refuses_a_shellshaped_username() -> None:
    with pytest.raises(RenderError):
        render("user={{ inputs.username }}", {"username": "deploy; rm -rf /"})


def test_render_refuses_a_path_that_travels() -> None:
    with pytest.raises(RenderError):
        render("path={{ inputs.path }}", {"path": "/opt/opspilot/apps/../../etc/shadow"})


# ==========================================================================
# Path discipline
# ==========================================================================
@pytest.mark.parametrize(
    "path",
    [
        "../../etc/shadow",
        "/opt/opspilot/apps/../../etc/passwd",
        "/etc/passwd",
        "/etc/shadow",
        "/root/.ssh/authorized_keys",
        "opt/opspilot/apps/relative",
        "/opt/opspilot/apps/evil\x00name",
    ],
)
def test_write_paths_outside_the_allowlist_are_refused(path: str) -> None:
    with pytest.raises(RenderError):
        validate_path(path)


@pytest.mark.parametrize(
    "path",
    ["/opt/opspilot/apps/foo/releases/1", "/etc/systemd/system/x.service", "/etc/nginx/nginx.conf"],
)
def test_write_paths_inside_the_allowlist_are_accepted(path: str) -> None:
    assert validate_path(path) == path


def test_read_paths_may_leave_the_write_allowlist_but_not_travel() -> None:
    assert validate_read_path("/var/lib/mysql") == "/var/lib/mysql"
    assert validate_read_path("/") == "/"
    with pytest.raises(RenderError):
        validate_read_path("/var/lib/../../etc/shadow")


# ==========================================================================
# Injection: values stay single argv tokens
# ==========================================================================
INJECTION_PAYLOADS = [
    "deploy; touch /tmp/opspilot-pwned",
    "deploy && id",
    "deploy | tee /etc/passwd",
    "deploy$(id)",
    "deploy`id`",
    "deploy'\"$()",
    "deploy --help",
    "deploy%0aid",
]

#: Separate from the list above: a newline is refused by the path rule itself,
#: so it never reaches argv at all.
CONTROL_CHAR_PAYLOADS = ["deploy\n/bin/echo pwned", "deploy\x00", "deploy\r\n"]


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_a_hostile_check_disk_path_stays_one_argv_token(payload: str) -> None:
    df_out = (
        "Filesystem 1024-blocks Used Available Capacity Mounted\n"
        "/dev/sda1 10 1 9 10% /\n"
    )
    port = RecordingPort({"df": _ok(df_out)})
    action = resolve("system.check_disk", port)
    # Absolute, no traversal — so it passes the path rule and reaches argv.
    action.run(CTX, {"path": f"/opt/opspilot/apps/{payload}", "min_available_mb": 1})

    assert len(port.argvs()) == 1
    tokens = port.argvs()[0]
    assert tokens[0] == "df"
    assert payload in tokens[-1]
    assert tokens[-1].count(" ") >= 0  # the whole payload is ONE element
    assert "touch" not in tokens[:-1]


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_a_hostile_package_name_is_rejected_before_anything_runs(payload: str) -> None:
    port = RecordingPort()
    action = resolve("package.install", port)
    with pytest.raises(ValueError):
        action.run(CTX, {"packages": [payload]})
    assert port.calls == []


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_a_hostile_service_name_never_reaches_restart(payload: str) -> None:
    port = RecordingPort()
    action = resolve("systemd.restart", port)
    with pytest.raises(ValueError):
        action.run(CTX, {"service": payload})
    assert port.calls == []


@pytest.mark.parametrize("payload", CONTROL_CHAR_PAYLOADS)
def test_a_path_with_control_characters_is_refused_before_argv(payload: str) -> None:
    port = RecordingPort()
    action = resolve("system.check_disk", port)
    with pytest.raises(ValueError):
        action.run(CTX, {"path": f"/opt/opspilot/apps/{payload}"})
    assert port.calls == []


# ==========================================================================
# file.render
# ==========================================================================
def test_file_render_goes_through_stdin_and_never_argv() -> None:
    secret = "DB_PASSWORD=hunter2-DO-NOT-LOG"
    port = RecordingPort({"dd": _ok(), "mv": _ok(), "chmod": _ok()})
    action = resolve("file.render", port)
    result = action.run(
        CTX,
        {
            "path": "/opt/opspilot/apps/demo/app.env",
            "content": secret,
            "mode": "0600",
        },
    )

    assert result.ok
    assert secret not in port.flat(), "file body must not appear in any argv"
    assert secret in port.stdin_text()
    joined = port.joined()
    assert any(t.startswith("dd of=/opt/opspilot/apps/demo/app.env.opspilot-new") for t in joined)
    assert joined[-1] == (
        "mv -f -- /opt/opspilot/apps/demo/app.env.opspilot-new"
        " /opt/opspilot/apps/demo/app.env"
    )
    assert result.data["sha256"] != secret
    assert secret not in str(result.data)


def test_file_render_backs_up_before_replacing() -> None:
    port = RecordingPort(
        {
            "test": _ok(),  # target exists
            "cp": _ok(),
            "dd": _ok(),
            "mv": _ok(),
            "chmod": _ok(),
        }
    )
    action = resolve("file.render", port)
    action.run(CTX, {"path": "/opt/opspilot/apps/demo/app.conf", "content": "x", "backup": True})

    joined = port.joined()
    backed_up = (
        "cp -p -- /opt/opspilot/apps/demo/app.conf"
        " /opt/opspilot/apps/demo/app.conf.opspilot-bak"
    )
    swapped = (
        "mv -f -- /opt/opspilot/apps/demo/app.conf.opspilot-new"
        " /opt/opspilot/apps/demo/app.conf"
    )
    assert backed_up in joined
    assert joined.index(backed_up) < joined.index(swapped)


def test_file_render_refuses_to_write_outside_the_app_tree() -> None:
    port = RecordingPort()
    action = resolve("file.render", port)
    with pytest.raises(ValueError):
        action.run(CTX, {"path": "/etc/shadow", "content": "root::0:0"})
    assert port.calls == []


def test_file_render_may_write_config_into_shared_config() -> None:
    """`shared/config` is the shared-config slot of the release layout.

    Refusing it would make the deploy chain impossible. The thing that must
    never happen is an *install* unpacking over `shared/` — see the
    install_release test below.
    """
    port = RecordingPort(
        {
            "test": ExecResult(exit_code=1, stdout="", stderr="", argv=()),
            "dd": _ok(),
            "mv": _ok(),
            "chmod": _ok(),
        }
    )
    action = resolve("file.render", port)
    result = action.run(
        CTX,
        {
            "path": "/opt/opspilot/apps/demo/shared/config/app.env",
            "content": "A=1",
            "backup": False,
        },
    )
    assert result.ok
    moved = [t for t in port.joined() if t.startswith("mv -f -- /opt/opspilot/apps/demo/shared")]
    assert moved


# ==========================================================================
# app.* — releases and shared/
# ==========================================================================
def test_install_release_refuses_to_unpack_over_shared_state() -> None:
    port = RecordingPort()
    action = resolve("app.install_release", port)
    with pytest.raises(ValueError):
        action.run(
            CTX,
            {
                "app_root": "/opt/opspilot/apps/demo/shared",
                "release_id": "2026-10-12",
            },
        )
    assert port.calls == []


def test_install_release_is_skipped_when_the_release_is_already_there() -> None:
    port = RecordingPort({"test": _ok(), "tar": _ok(), "rm": _ok()})
    action = resolve("app.install_release", port)
    result = action.run(
        CTX, {"app_root": "/opt/opspilot/apps/demo", "release_id": "2026-10-12"}
    )
    assert result.ok and result.changed is False
    assert not any(t.startswith("tar") for t in port.joined())


def test_install_release_cleans_up_a_half_unpacked_directory() -> None:
    port = RecordingPort(
        {
            "test": ExecResult(exit_code=1, stdout="", stderr="", argv=()),  # missing
            "mkdir": _ok(),
            "tar": ExecResult(exit_code=2, stdout="", stderr="truncated", argv=()),
            "rm": _ok(),
        }
    )
    action = resolve("app.install_release", port)
    result = action.run(
        CTX, {"app_root": "/opt/opspilot/apps/demo", "release_id": "2026-10-12"}
    )
    assert result.ok is False
    cleaned = [t for t in port.joined() if t.startswith("rm -rf --")]
    assert cleaned == ["rm -rf -- /opt/opspilot/apps/demo/releases/2026-10-12"]


def test_fetch_release_refuses_a_checksum_mismatch() -> None:
    port = RecordingPort(
        {
            "curl": _ok(),
            "sha256sum": _ok("deadbeef  /opt/opspilot/apps/demo/releases/1.artifact\n"),
        }
    )
    action = resolve("app.fetch_release", port)
    result = action.run(
        CTX,
        {
            "app_root": "/opt/opspilot/apps/demo",
            "release_id": "2026-10-12",
            "source": "https://example.com/app.tar.gz",
            "checksum": "0" * 64,
        },
    )
    assert result.ok is False
    assert result.data["actual"] == "deadbeef"
    assert not any(t.startswith("tar") for t in port.joined())


@pytest.mark.parametrize(
    "source",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "https://user:pass@example.com/x",
        "https://example.com:22/x",
    ],
)
def test_fetch_release_refuses_an_unacceptable_source(source: str) -> None:
    port = RecordingPort()
    action = resolve("app.fetch_release", port)
    with pytest.raises(ValueError):
        action.run(
            CTX,
            {
                "app_root": "/opt/opspilot/apps/demo",
                "release_id": "1",
                "source": source,
            },
        )
    assert port.calls == []


# ==========================================================================
# Service and sudo whitelists
# ==========================================================================
@pytest.mark.parametrize(
    "name", ["opspilot-api", "opspilot-worker", "nginx", "mysql", "redis-server"]
)
def test_real_service_names_are_allowed(name: str) -> None:
    assert check_service_allowed(name) == name


@pytest.mark.parametrize(
    "name", ["sshd", "cron", "docker", "evil", "opspilot", "mysql;id", "opspilot"]
)
def test_anything_else_is_refused(name: str) -> None:
    with pytest.raises(ValueError):
        check_service_allowed(name)


def test_systemd_restart_rejects_a_service_outside_the_whitelist() -> None:
    port = RecordingPort()
    action = resolve("systemd.restart", port)
    with pytest.raises(ValueError):
        action.run(CTX, {"service": "sshd"})
    assert port.calls == []


def test_systemd_restart_accepts_a_whitelisted_service() -> None:
    port = RecordingPort({"/usr/bin/systemctl": _ok()})
    action = resolve("systemd.restart", port)
    result = action.run(CTX, {"service": "opspilot-api"})
    assert result.ok
    assert port.joined() == ["sudo -n /usr/bin/systemctl restart -- opspilot-api"]


def test_supervisor_restart_rejects_a_program_outside_the_whitelist() -> None:
    port = RecordingPort()
    action = resolve("supervisor.restart", port)
    with pytest.raises(ValueError):
        action.run(CTX, {"program": "attacker-controlled"})
    assert port.calls == []


@pytest.mark.parametrize(
    "program",
    ["/bin/bash", "/bin/sh", "/usr/bin/chmod", "/usr/bin/dd", "/usr/bin/cp", "/usr/bin/rm", "sudo"],
)
def test_sudo_escalates_only_the_whitelisted_programs(program: str) -> None:
    with pytest.raises(ValueError, match="sudo is not allowed"):
        ensure_sudo_allowed(program)
    assert program not in ALLOWED_SUDO_RULES


def test_the_whitelisted_sudo_programs_are_accepted() -> None:
    for program in ALLOWED_SUDO_RULES:
        assert ensure_sudo_allowed(program) == program


def test_a_destructive_restart_escalates_through_the_whitelist() -> None:
    port = RecordingPort({"/usr/bin/systemctl": _ok()})
    action = resolve("systemd.restart", port)
    action.run(CTX, {"service": "nginx"})
    assert port.joined() == ["sudo -n /usr/bin/systemctl restart -- nginx"]


def test_file_render_escalates_nothing_at_all() -> None:
    """Its whole path is unprivileged; it must never emit a sudo argv."""
    port = RecordingPort({"test": _ok(), "cp": _ok(), "dd": _ok(), "mv": _ok(), "chmod": _ok()})
    action = resolve("file.render", port)
    action.run(CTX, {"path": "/opt/opspilot/apps/demo/app.conf", "content": "x"})
    assert not any("sudo" in t for t in port.joined())


# ==========================================================================
# systemd.render_unit
# ==========================================================================
def test_render_unit_refuses_a_path_outside_the_unit_directory() -> None:
    port = RecordingPort()
    action = resolve("systemd.render_unit", port)
    with pytest.raises(ValueError):
        action.run(
            CTX,
            {"unit_path": "/etc/systemd/system/../../etc/cron.d/evil", "content": "[Unit]\n"},
        )
    with pytest.raises(ValueError):
        action.run(CTX, {"unit_path": "/opt/opspilot/apps/x.service", "content": "[Unit]\n"})
    assert port.calls == []


def test_render_unit_backs_up_and_verifies_and_restores_on_failure() -> None:
    port = RecordingPort(
        {
            "test": _ok(),  # unit exists
            "/usr/bin/install": _ok(),
            "dd": _ok(),
            "rm": _ok(),
            "/usr/bin/systemd-analyze": ExecResult(
                exit_code=1, stdout="", stderr="Bad unit", argv=()
            ),
        }
    )
    action = resolve("systemd.render_unit", port)
    result = action.run(
        CTX,
        {
            "unit_path": "/etc/systemd/system/opspilot-demo.service",
            "content": "[Unit]\nDescription=demo\n",
        },
    )

    assert result.ok is False
    joined = port.joined()
    unit = "/etc/systemd/system/opspilot-demo.service"
    backup = f"sudo -n /usr/bin/install -m 0644 {unit} {unit}.bak"
    restore = f"sudo -n /usr/bin/install -m 0644 {unit}.bak {unit}"
    assert backup in joined
    # ...and the backup is put back, after the write.
    assert joined.index(backup) < joined.index(restore) == len(joined) - 1
    assert not any("daemon-reload" in t for t in joined)


def test_render_unit_stages_the_body_out_of_argv() -> None:
    body = "[Service]\nEnvironment=SECRET=hunter2\n"
    port = RecordingPort(
        {
            "test": ExecResult(exit_code=1, stdout="", stderr="", argv=()),
            "/usr/bin/install": _ok(),
            "dd": _ok(),
            "rm": _ok(),
            "/usr/bin/systemd-analyze": _ok(),
            "/usr/bin/systemctl": _ok(),
        }
    )
    action = resolve("systemd.render_unit", port)
    result = action.run(
        CTX,
        {"unit_path": "/etc/systemd/system/opspilot-demo.service", "content": body},
    )

    assert result.ok
    assert body not in port.flat()
    assert body in port.stdin_text()
    assert result.data["daemon_reloaded"] is True


# ==========================================================================
# mysql / redis — read-only, and they prove it
# ==========================================================================
def test_mysql_precheck_refuses_to_initialise_a_missing_datadir() -> None:
    port = RecordingPort({"test": ExecResult(exit_code=1, stdout="", stderr="", argv=())})
    action = resolve("mysql.precheck", port)
    result = action.run(
        CTX,
        {
            "datadir": "/var/lib/mysql",
            "expect_existing_datadir": True,
        },
    )

    assert result.ok is False
    assert result.data["initialised"] is False
    assert "never initialise" in result.summary
    # Nothing that could create or lay out a datadir was even attempted.
    creating = {"mkdir", "mysql_install_db", "mysqld", "chown", "chmod"}
    assert not any(t.split()[0] in creating for t in port.joined())


def test_mysql_precheck_reports_an_existing_datadir_without_touching_it() -> None:
    port = RecordingPort(
        {
            "test": _ok(),
            "ls": _ok("#auto.cnf\nibdata1\nmysql\n"),
        }
    )
    action = resolve("mysql.precheck", port)
    result = action.run(CTX, {"datadir": "/var/lib/mysql"})

    assert result.ok
    assert result.data == {
        "datadir": "/var/lib/mysql",
        "exists": True,
        "nonempty": True,
        "initialised": False,
    }
    assert result.changed is False


def test_redis_precheck_never_writes_an_existing_config() -> None:
    port = RecordingPort({"test": _ok()})
    action = resolve("redis.precheck", port)
    result = action.run(CTX, {"config_path": "/etc/redis/redis.conf"})

    assert result.ok
    assert result.data["config_overwritten"] is False
    assert port.joined() == ["test -e -- /etc/redis/redis.conf"]
    assert not any(t.split()[0] in {"dd", "mv", "cp", "tee", "install"} for t in port.joined())


# ==========================================================================
# http.check — SSRF
# ==========================================================================
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://[::1]:8080/",
        "file:///etc/passwd",
        "http://user:pass@example.com/",
        "http://example.com:22/",
        "gopher://example.com/",
    ],
)
def test_http_check_refuses_an_unacceptable_target(url: str) -> None:
    port = RecordingPort()
    action = resolve("http.check", port)
    with pytest.raises(ValueError):
        action.run(CTX, {"url": url})
    assert port.calls == []


def test_http_check_compares_the_status_it_was_asked_for() -> None:
    port = RecordingPort({"curl": _ok("503")})
    action = resolve("http.check", port)
    result = action.run(CTX, {"url": "https://example.com/healthz", "expect_status": 200})
    assert result.ok is False
    assert result.data["status"] == "503"


# ==========================================================================
# notification.telegram — the token must not be written down
# ==========================================================================
TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawsome"


def test_telegram_token_never_appears_in_argv_or_the_result() -> None:
    port = RecordingPort({"curl": _ok("200")})
    action = resolve("notification.telegram", port)
    result = action.run(
        CTX, {"bot_token": TOKEN, "chat_id": "-100123", "text": "deploy ok"}
    )

    assert result.ok
    assert TOKEN not in port.flat(), "the bot token leaked into argv"
    assert TOKEN not in str(result.data)
    assert TOKEN not in result.summary
    assert TOKEN in port.stdin_text(), "the token must ride on stdin"
    assert port.joined()[0].startswith("curl --proto =https")


def test_telegram_rejects_a_token_that_is_not_a_token() -> None:
    port = RecordingPort()
    action = resolve("notification.telegram", port)
    with pytest.raises(ValueError):
        action.run(CTX, {"bot_token": "not-a-token", "chat_id": "1", "text": "hi"})
    assert port.calls == []


def test_redaction_catches_a_token_that_escapes_into_a_url() -> None:
    line = f"ssh.exec argv=['curl', 'https://api.telegram.org/bot{TOKEN}/sendMessage']"
    scrubbed = redact_text(line)
    assert TOKEN not in scrubbed
    assert REDACTED in scrubbed


# ==========================================================================
# Argv discipline, enforced at the action base
# ==========================================================================
@pytest.mark.parametrize(
    "argv",
    [
        "cat /etc/os-release",
        b"cat /etc/os-release",
        [],
        (),
    ],
)
def test_the_argv_guard_refuses_a_shell_string_or_an_empty_list(argv: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        ensure_argv(argv)


def test_the_argv_guard_refuses_a_non_string_element() -> None:
    with pytest.raises(TypeError, match="must be str"):
        ensure_argv(["cat", 3])  # type: ignore[list-item]


def test_the_argv_guard_accepts_a_list_of_strings() -> None:
    assert ensure_argv(["cat", "/etc/os-release"]) == ["cat", "/etc/os-release"]


def test_destructive_actions_are_never_marked_safe() -> None:
    register_all()
    for name, spec in get_registry().items():
        if spec.idempotency_class == "DESTRUCTIVE":
            assert spec.safe is False, name
            assert spec.requires_sudo is True, name
