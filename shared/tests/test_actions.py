"""The Action Registry is a security boundary. These tests are the lock.

Every assertion here is about something an attacker or a careless template could
otherwise get wrong: an unregistered name resolving, a path climbing out of the
app tree, a service outside the restart whitelist, a sudo escalation to ``bash``,
a health check pointed at the cloud metadata address, a file body leaking into
the audit trail, a Telegram token landing in argv.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
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
    AppSwitchCurrent,
    check_service_allowed,
    classify_verify_output,
    ensure_sudo_allowed,
)
from shared.actions.render import (
    DEFAULT_ALLOWED_PATH_PREFIXES,
    RenderError,
    allowed_path_prefixes,
    find_placeholders,
    render,
    validate_path,
    validate_read_path,
    validate_template_text,
)
from shared.config import clear_settings_cache
from shared.executor.port import ExecContext, ExecResult
from shared.redaction import REDACTED, redact_text

EXPECTED_ACTIONS = {
    "app.fetch_release",
    "app.install_release",
    "app.switch_current",
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
    "app.switch_current": "CONDITIONAL",
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
def test_register_all_loads_the_whitelisted_actions() -> None:
    """The registry is exactly the set we intend — no more, no fewer.

    Sixteen, not the fifteen in the design document's §7.4 table:
    ``app.switch_current`` is the §8.2 "原子切换 current 软链接" step, which is
    a remote operation and therefore has to go through the same whitelist as
    everything else. Leaving it out would have meant the worker reaching for
    ``ln``/``mv`` outside the registry, which is the one thing the registry
    exists to prevent.
    """
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
# The write allowlist is operator-extensible — and only that
# ==========================================================================
@pytest.fixture()
def _settings_cache() -> Any:
    """``get_settings`` is cached; a test that touches env must clear it."""
    clear_settings_cache()
    yield
    clear_settings_cache()


def test_the_default_allowlist_is_the_documented_one(_settings_cache: Any) -> None:
    assert allowed_path_prefixes() == DEFAULT_ALLOWED_PATH_PREFIXES
    # middleware/redis74 renders /etc/redis/redis.conf — the same class of
    # managed config as /etc/nginx/ and /etc/supervisor/conf.d/.
    assert "/etc/redis/" in DEFAULT_ALLOWED_PATH_PREFIXES


def test_a_setting_replaces_the_default_allowlist_wholesale(
    _settings_cache: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace, not union.

    Union would mean the setting can only ever widen the boundary, never
    tighten it — an operator who wants to confine the product to one app tree
    could not say so. Replace is the only semantic that supports both.
    """
    monkeypatch.setenv(
        "OPSPILOT_ALLOWED_WRITE_PREFIXES", "/opt/20kpay/, /www/python/"
    )
    clear_settings_cache()
    assert allowed_path_prefixes() == ("/opt/20kpay/", "/www/python/")
    assert validate_path("/opt/20kpay/api/venv") == "/opt/20kpay/api/venv"
    assert validate_path("/www/python/venv/bin/python") == "/www/python/venv/bin/python"
    with pytest.raises(RenderError):
        validate_path("/etc/nginx/nginx.conf")


def test_a_suffixed_sibling_is_not_matched_by_a_prefix(
    _settings_cache: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/opt/20kpay/ must not admit /opt/20kpay-evil/."""
    monkeypatch.setenv("OPSPILOT_ALLOWED_WRITE_PREFIXES", "/opt/20kpay")
    clear_settings_cache()
    assert allowed_path_prefixes() == ("/opt/20kpay/",)
    assert validate_path("/opt/20kpay/api") == "/opt/20kpay/api"
    with pytest.raises(RenderError):
        validate_path("/opt/20kpay-evil/api")


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


class _PathAwareDfPort(RecordingPort):
    """Answers ``df`` only for paths that "exist"; fails the others."""

    def __init__(self, existing: set[str]) -> None:
        super().__init__()
        self.existing = existing

    def run(self, ctx, argv, *, stdin=None):
        tokens = tuple(argv)
        self.calls.append((tokens, stdin))
        if tokens[0] != "df":
            return self.default
        if tokens[-1] not in self.existing:
            return ExecResult(
                exit_code=1, stdout="", stderr=f"df: {tokens[-1]}: No such file", argv=tokens
            )
        return ExecResult(
            exit_code=0,
            stdout=(
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                "/dev/sda1 10 1 9 10% /\n"
            ),
            stderr="",
            argv=tokens,
        )


def test_check_disk_walks_up_when_the_path_does_not_exist_yet() -> None:
    """A free-space precheck runs *before* the plan creates the directory.

    ``df`` cannot stat a path that is not there, so the first deploy of an app
    would fail its own precheck. The measurement must fall back to the nearest
    existing ancestor — the filesystem the directory will be created on.
    """
    port = _PathAwareDfPort({"/opt/opspilot/apps"})
    action = resolve("system.check_disk", port)
    result = action.run(
        CTX,
        {"path": "/opt/opspilot/apps/billing-api/releases", "min_available_mb": 1},
    )

    assert result.ok, result.summary
    assert [c[-1] for c in port.argvs()] == [
        "/opt/opspilot/apps/billing-api/releases",
        "/opt/opspilot/apps/billing-api",
        "/opt/opspilot/apps",
    ]
    assert result.data["measured_path"] == "/opt/opspilot/apps"
    assert "billing-api/releases" in result.summary
    assert "via /opt/opspilot/apps" in result.summary


def test_check_disk_does_not_invent_a_path_that_never_appears() -> None:
    """If no ancestor answers, the failure names the path that was asked for."""
    port = _PathAwareDfPort(set())
    action = resolve("system.check_disk", port)
    result = action.run(
        CTX, {"path": "/opt/opspilot/apps/nope/releases", "min_available_mb": 1}
    )
    assert not result.ok
    assert "/opt/opspilot/apps/nope/releases" in result.summary


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


def test_file_render_creates_the_parent_directory_before_writing() -> None:
    """``dd of=`` cannot create the directory it writes into.

    The deploy's env file is ``shared/config/app.env`` — a tree a first deploy
    has not built — so writing without a mkdir first fails on exactly the run
    that matters.
    """
    port = RecordingPort(
        {
            "test": ExecResult(exit_code=1, stdout="", stderr="", argv=()),
            "mkdir": _ok(),
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
    joined = port.joined()
    made = joined.index("mkdir -p -- /opt/opspilot/apps/demo/shared/config")
    wrote = next(i for i, t in enumerate(joined) if t.startswith("dd "))
    assert made < wrote, "the parent directory must exist before dd opens the file"


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


def test_install_release_builds_a_per_release_venv_and_installs_the_lock() -> None:
    """§8.2 step 4: a dedicated virtualenv, deps from the lock file.

    The venv lives inside ``releases/<id>`` so a rollback to the previous
    release does not have to rebuild anything — both trees stay ready.
    """
    venv_pip = "/opt/opspilot/apps/demo/releases/1.0.0/venv/bin/pip"
    port = ScriptedPort(
        {
            "test": [
                ExecResult(exit_code=1, stdout="", stderr="", argv=()),  # no release dir
                ExecResult(exit_code=1, stdout="", stderr="", argv=()),  # no venv yet
                _ok(),  # lock file is there
            ],
            "mkdir": [_ok()],
            "tar": [_ok()],
            "python3.12": [_ok()],
            venv_pip: [_ok()],
        }
    )
    action = resolve("app.install_release", port)
    result = action.run(
        CTX,
        {
            "app_root": "/opt/opspilot/apps/demo",
            "release_id": "1.0.0",
            "python_version": "3.12",
        },
    )
    assert result.ok and result.data["venv"] is True
    joined = port.joined()
    assert "python3.12 -m venv -- /opt/opspilot/apps/demo/releases/1.0.0/venv" in joined
    install = [t for t in joined if t.startswith(venv_pip)]
    assert install == [
        f"{venv_pip} install --no-deps --disable-pip-version-check "
        "-r /opt/opspilot/apps/demo/releases/1.0.0/requirements.lock"
    ]
    # --no-deps is the whole point of a lock file: pip must not resolve.
    assert "--no-deps" in install[0]


def test_install_release_reuses_a_venv_it_already_built() -> None:
    port = ScriptedPort(
        {
            "test": [
                _ok(),  # release dir exists
                _ok(),  # venv exists
            ],
        }
    )
    action = resolve("app.install_release", port)
    result = action.run(
        CTX,
        {
            "app_root": "/opt/opspilot/apps/demo",
            "release_id": "1.0.0",
            "python_version": "3.12",
        },
    )
    assert result.ok and result.changed is False
    assert not any("/bin/pip" in t or t.startswith("python") for t in port.joined())


def test_install_release_treats_a_missing_lock_file_as_nothing_to_install() -> None:
    port = ScriptedPort(
        {
            "test": [
                ExecResult(exit_code=1, stdout="", stderr="", argv=()),  # no release dir
                ExecResult(exit_code=1, stdout="", stderr="", argv=()),  # no venv
                ExecResult(exit_code=1, stdout="", stderr="", argv=()),  # no lock
            ],
            "mkdir": [_ok()],
            "tar": [_ok()],
            "python3.12": [_ok()],
        }
    )
    action = resolve("app.install_release", port)
    result = action.run(
        CTX,
        {
            "app_root": "/opt/opspilot/apps/demo",
            "release_id": "1.0.0",
            "python_version": "3.12",
        },
    )
    assert result.ok
    assert not any("/bin/pip" in t for t in port.joined())


def test_install_release_refuses_a_lock_file_outside_the_release() -> None:
    port = RecordingPort()
    action = resolve("app.install_release", port)
    with pytest.raises(ValueError):
        action.run(
            CTX,
            {
                "app_root": "/opt/opspilot/apps/demo",
                "release_id": "1.0.0",
                "python_version": "3.12",
                "requirements": "/etc/passwd",
            },
        )
    with pytest.raises(ValueError):
        action.run(
            CTX,
            {
                "app_root": "/opt/opspilot/apps/demo",
                "release_id": "1.0.0",
                "python_version": "3.12",
                "requirements": "../../../etc/passwd",
            },
        )
    assert port.calls == []


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


def test_fetch_release_creates_the_releases_directory_before_curl() -> None:
    """``curl -o`` cannot create the directory it writes into.

    On a first deploy nothing has created ``releases/`` — ``system.check_disk``
    only measures its parent — and curl exits 23 ("Failure writing output to
    destination") for want of a mkdir nobody ran.
    """
    port = RecordingPort(
        {
            "mkdir": _ok(),
            "curl": _ok(),
            "sha256sum": _ok("0" * 64 + "  x.artifact\n"),
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
    assert result.ok
    joined = port.joined()
    made = joined.index("mkdir -p -- /opt/opspilot/apps/demo/releases")
    fetched = next(i for i, t in enumerate(joined) if t.startswith("curl "))
    assert made < fetched, "releases/ must exist before curl -o writes into it"


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
# _exists — the probe every "is it already there?" decision rests on
# ==========================================================================
class RealTestPort(RecordingPort):
    """Answers ``test`` by running the real ``/usr/bin/test``.

    A canned ``ExecResult`` cannot tell "the path is missing" from "``test``
    was called with a syntax error" — both are a non-zero exit. Only the real
    binary can, which is exactly how ``test -e -- <path>`` survived: it always
    returned False and every caller believed the path was absent.
    """

    def run(
        self,
        ctx: ExecContext,
        argv: Sequence[str],
        *,
        stdin: bytes | str | None = None,
    ) -> ExecResult:
        tokens = tuple(argv)
        self.calls.append((tokens, stdin))
        if tokens[0] != "test":
            return super().run(ctx, argv, stdin=stdin)
        proc = subprocess.run(["/usr/bin/test", *tokens[1:]], capture_output=True)
        return ExecResult(
            exit_code=proc.returncode,
            stdout=proc.stdout.decode("utf-8", "replace"),
            stderr=proc.stderr.decode("utf-8", "replace"),
            argv=tokens,
        )


def test_exists_tells_a_missing_path_from_a_bad_invocation(tmp_path: Path) -> None:
    """``test`` must actually answer, not error out on its own argv.

    POSIX ``test`` does not honour ``--`` as end-of-options: ``test -e -- /x``
    has three operands and ``test`` reads ``--`` as a binary operator, exiting
    2 with "binary operator expected". That looks exactly like "absent", so
    releases appeared uninstalled and ``app.switch_current`` refused them.
    """
    present = tmp_path / "here"
    present.write_text("x", encoding="utf-8")
    port = RealTestPort()
    action = resolve("app.switch_current", port)
    assert isinstance(action, AppSwitchCurrent)

    assert action._exists(CTX, str(present)) is True
    assert action._exists(CTX, str(tmp_path / "gone")) is False
    # Both probes must have produced a *valid* test(1) invocation.
    for tokens, _ in port.calls:
        assert tokens[0] == "test"
        assert "--" not in tokens, f"test(1) must not be given '--': {tokens}"


# ==========================================================================
# app.switch_current — the atomic release switch
# ==========================================================================
class ScriptedPort(RecordingPort):
    """Answers by ``argv[0]``, popping from a queue per command.

    The switch reads ``current`` twice and the two answers differ, which a
    single canned response per command name cannot express.
    """

    def __init__(self, script: dict[str, list[ExecResult]]) -> None:
        super().__init__()
        self._script = {k: list(v) for k, v in script.items()}

    def run(
        self,
        ctx: ExecContext,
        argv: Sequence[str],
        *,
        stdin: bytes | str | None = None,
    ) -> ExecResult:
        tokens = tuple(argv)
        self.calls.append((tokens, stdin))
        queue = self._script.get(tokens[0] if tokens else "")
        if queue:
            canned = queue.pop(0)
            return ExecResult(
                exit_code=canned.exit_code,
                stdout=canned.stdout,
                stderr=canned.stderr,
                argv=tokens,
            )
        return ExecResult(exit_code=0, stdout="", stderr="", argv=tokens)


def test_switch_current_is_one_rename_over_the_old_link() -> None:
    """Build the new link aside, then rename it into place.

    Unlink-then-symlink leaves a window with no ``current`` at all, and a
    process restarting inside that window comes up against nothing. ``mv -T``
    is the single syscall that closes it, and ``-T`` is required or the move
    lands *inside* the directory the old link points at.
    """
    port = ScriptedPort(
        {
            "test": [_ok(), _ok()],  # release dir exists, scratch already gone
            "readlink": [_ok(""), _ok("releases/1.0.0\n")],
            "rm": [_ok()],
            "ln": [_ok()],
            "mv": [_ok()],
        }
    )
    action = resolve("app.switch_current", port)
    result = action.run(
        CTX, {"app_root": "/opt/opspilot/apps/demo", "release_id": "1.0.0"}
    )
    assert result.ok and result.changed
    joined = port.joined()
    assert "ln -s -- releases/1.0.0 /opt/opspilot/apps/demo/.current.opspilot-new" in joined
    assert (
        "mv -Tf -- /opt/opspilot/apps/demo/.current.opspilot-new /opt/opspilot/apps/demo/current"
        in joined
    )
    # Nothing may remove the live link before the replacement is in place.
    assert not any(t.startswith("rm -f -- /opt/opspilot/apps/demo/current") for t in joined)
    assert not any("ln -sfn" in t for t in joined)


def test_switch_current_is_skipped_when_current_already_points_there() -> None:
    port = ScriptedPort(
        {"test": [_ok()], "readlink": [_ok("releases/1.0.0\n")]}
    )
    action = resolve("app.switch_current", port)
    result = action.run(
        CTX, {"app_root": "/opt/opspilot/apps/demo", "release_id": "1.0.0"}
    )
    assert result.ok and result.changed is False
    assert not any(t.startswith("mv") for t in port.joined())


def test_switch_current_reports_the_link_it_replaced() -> None:
    port = ScriptedPort(
        {
            "test": [_ok(), _ok()],
            "readlink": [_ok("releases/0.9.0\n"), _ok("releases/1.0.0\n")],
            "rm": [_ok()],
            "ln": [_ok()],
            "mv": [_ok()],
        }
    )
    action = resolve("app.switch_current", port)
    result = action.run(
        CTX, {"app_root": "/opt/opspilot/apps/demo", "release_id": "1.0.0"}
    )
    assert result.ok
    assert result.data["previous"] == "releases/0.9.0"
    assert result.data["current"] == "releases/1.0.0"


def test_switch_current_refuses_when_the_release_is_not_installed() -> None:
    port = ScriptedPort({"test": [ExecResult(exit_code=1, stdout="", stderr="", argv=())]})
    action = resolve("app.switch_current", port)
    result = action.run(
        CTX, {"app_root": "/opt/opspilot/apps/demo", "release_id": "1.0.0"}
    )
    assert result.ok is False
    assert "not installed" in result.summary
    assert not any(t.startswith(("ln", "mv")) for t in port.joined())


def test_switch_current_refuses_to_move_onto_shared_state() -> None:
    port = RecordingPort()
    action = resolve("app.switch_current", port)
    with pytest.raises(ValueError):
        action.run(
            CTX,
            {"app_root": "/opt/opspilot/apps/demo/shared", "release_id": "1.0.0"},
        )
    assert port.calls == []


@pytest.mark.parametrize("release_id", ["latest", "current", "shared", "releases", "ops"])
def test_switch_current_refuses_a_reserved_release_id(release_id: str) -> None:
    port = RecordingPort()
    action = resolve("app.switch_current", port)
    with pytest.raises(ValueError):
        action.run(
            CTX, {"app_root": "/opt/opspilot/apps/demo", "release_id": release_id}
        )
    assert port.calls == []


def test_switch_current_puts_a_previous_release_back() -> None:
    """A rollback is the same action pointed at the old release.

    Nothing about undo is special: it is a switch to a release that is already
    on the host, so it must not fetch or unpack anything.
    """
    port = ScriptedPort(
        {
            "test": [_ok(), _ok()],
            "readlink": [_ok("releases/1.0.0\n"), _ok("releases/0.9.0\n")],
            "rm": [_ok()],
            "ln": [_ok()],
            "mv": [_ok()],
        }
    )
    action = resolve("app.switch_current", port)
    result = action.run(
        CTX, {"app_root": "/opt/opspilot/apps/demo", "release_id": "0.9.0"}
    )
    assert result.ok and result.data["previous"] == "releases/1.0.0"
    assert not any(t.startswith(("curl", "tar", "mkdir")) for t in port.joined())


def test_switch_current_gives_up_and_cleans_up_if_the_rename_fails() -> None:
    port = ScriptedPort(
        {
            "test": [_ok(), _ok()],
            "readlink": [_ok(""), _ok("releases/0.9.0\n")],
            "rm": [_ok(), _ok()],
            "ln": [_ok()],
            "mv": [ExecResult(exit_code=1, stdout="", stderr="cross-device", argv=())],
        }
    )
    action = resolve("app.switch_current", port)
    result = action.run(
        CTX, {"app_root": "/opt/opspilot/apps/demo", "release_id": "1.0.0"}
    )
    assert result.ok is False
    # The staged link is removed, and the live one is never touched.
    assert (
        "rm -f -- /opt/opspilot/apps/demo/.current.opspilot-new" in port.joined()
    )
    assert not any("apps/demo/current" in t and t.startswith("rm") for t in port.joined())


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


def test_render_unit_reports_a_stage_dir_it_cannot_create() -> None:
    """A stage dir the SSH user cannot make must say so.

    Blaming the `dd` instead sends an operator looking at the destination, not
    at the scratch directory that was never created.
    """
    port = RecordingPort(
        {
            "test": ExecResult(exit_code=1, stdout="", stderr="", argv=()),
            "/usr/bin/install": _ok(),
            "mkdir": ExecResult(
                exit_code=1, stdout="", stderr="Permission denied", argv=()
            ),
            "dd": ExecResult(
                exit_code=1,
                stdout="",
                stderr="dd: failed to open '…': No such file or directory",
                argv=(),
            ),
        }
    )
    action = resolve("systemd.render_unit", port)
    result = action.run(
        CTX,
        {"unit_path": "/etc/systemd/system/opspilot-demo.service", "content": "[Service]\n"},
    )

    assert result.ok is False
    assert "/opt/opspilot/test" in result.summary
    assert not any(t.startswith("dd ") for t in port.joined())


def test_render_unit_accepts_a_current_path_that_is_not_linked_yet() -> None:
    """§8.2 writes the unit *before* flipping ``current``.

    A version-agnostic unit names ``<app_root>/current/...``, so at verify time
    that binary is not supposed to exist. Failing here would force the link to
    flip before the unit is installed — the exact ordering the design avoids.
    """
    stderr = (
        "opspilot-demo.service: Command "
        "/opt/opspilot/apps/demo/current/venv/bin/python is not executable:"
        " No such file or directory\n"
    )
    port = RecordingPort(
        {
            "test": ExecResult(exit_code=1, stdout="", stderr="", argv=()),
            "/usr/bin/install": _ok(),
            "dd": _ok(),
            "rm": _ok(),
            "/usr/bin/systemd-analyze": ExecResult(
                exit_code=1, stdout="", stderr=stderr, argv=()
            ),
            "/usr/bin/systemctl": _ok(),
        }
    )
    action = resolve("systemd.render_unit", port)
    result = action.run(
        CTX,
        {"unit_path": "/etc/systemd/system/opspilot-demo.service", "content": "[Service]\n"},
    )

    assert result.ok
    assert result.data["exec_start_pending"] is True
    assert any("daemon-reload" in t for t in port.joined())


def test_render_unit_still_refuses_a_missing_binary_elsewhere() -> None:
    """A missing binary that is not behind ``current/`` is just broken."""
    stderr = (
        "opspilot-demo.service: Command /usr/local/bin/typo is not executable:"
        " No such file or directory\n"
    )
    port = RecordingPort(
        {
            "test": ExecResult(exit_code=1, stdout="", stderr="", argv=()),
            "/usr/bin/install": _ok(),
            "dd": _ok(),
            "rm": _ok(),
            "/usr/bin/systemd-analyze": ExecResult(
                exit_code=1, stdout="", stderr=stderr, argv=()
            ),
        }
    )
    action = resolve("systemd.render_unit", port)
    result = action.run(
        CTX,
        {"unit_path": "/etc/systemd/system/opspilot-demo.service", "content": "[Service]\n"},
    )

    assert result.ok is False
    assert "/usr/local/bin/typo" in str(result.data)
    assert not any("daemon-reload" in t for t in port.joined())


def test_render_unit_refuses_a_verify_that_exits_badly_with_no_explanation() -> None:
    """Empty stderr is not an explained exit — do not guess that it is fine."""
    port = RecordingPort(
        {
            "test": ExecResult(exit_code=1, stdout="", stderr="", argv=()),
            "/usr/bin/install": _ok(),
            "dd": _ok(),
            "rm": _ok(),
            "/usr/bin/systemd-analyze": ExecResult(
                exit_code=3, stdout="", stderr="", argv=()
            ),
        }
    )
    action = resolve("systemd.render_unit", port)
    result = action.run(
        CTX,
        {"unit_path": "/etc/systemd/system/opspilot-demo.service", "content": "[Service]\n"},
    )

    assert result.ok is False
    assert not any("daemon-reload" in t for t in port.joined())


def test_classify_verify_output_drops_systemd_warnings_only() -> None:
    problems, deferred = classify_verify_output(
        "u.service:7: Missing '=', ignoring line.\n"
        "u.service: Unknown section 'Garbage'. Ignoring.\n"
    )
    assert (problems, deferred) == ([], [])

    problems, deferred = classify_verify_output(
        "u.service: Service has no ExecStart=, ExecStop=, or SuccessAction=. Refusing.\n"
        "Unit u.service has a bad unit file setting.\n"
    )
    assert deferred == []
    assert len(problems) == 2

    problems, deferred = classify_verify_output(
        "u.service: Command /opt/opspilot/apps/demo/current/venv/bin/python"
        " is not executable: No such file or directory\n"
    )
    assert problems == []
    assert len(deferred) == 1


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
    # No `--`: POSIX test(1) reads it as an operand and errors out.
    assert port.joined() == ["test -e /etc/redis/redis.conf"]
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
