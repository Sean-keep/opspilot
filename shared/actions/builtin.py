"""The V1 built-in actions.

Everything an operator can do to a managed host is one of these fifteen names.
There is no generic shell action and there must never be one — the registry is
as much a security boundary as any auth check.

Each action follows the same shape:

- a frozen Pydantic model declares what its arguments may be;
- ``run`` builds an **argv list** and hands it to ``self.port``;
- nothing concatenates a command string, and nothing calls a shell.

Per-action constraints from the design document are enforced here rather than
in the caller: package names are a narrow character class, remote paths must be
inside an allowed prefix, service names go through a whitelist, ``mysql.precheck``
will not initialise an existing datadir, and ``app.install_release`` refuses to
write under ``shared/``.

Two payload rules are worth stating plainly, because both are easy to get wrong:

- **File contents never travel in argv.** argv is logged and persisted to
  ``ops_task_steps.result_json``. Contents — and anything else that may hold a
  secret — go through ``stdin``, which the transport records only as a length.
- **Nothing that needs root is escalated by guessing.** ``_sudo`` refuses any
  program outside :data:`ALLOWED_SUDO_RULES`. ``chmod`` / ``chown`` / ``cp`` /
  ``mv`` / ``rm`` / ``dd`` / ``sh`` are deliberately not on that list.
"""

from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import posixpath
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator

from shared.actions.base import (
    Action,
    ActionResult,
    ParamsModel,
    ensure_argv,
    register,
)
from shared.actions.params import (
    NonEmpty,
    OptionalUserName,
    OptionalVersion,
    Packages,
    ReadPath,
    RemotePath,
    ServiceName,
    UserName,
    Version,
    positive_int,
    tcp_port,
)
from shared.actions.render import validate_path
from shared.checks import parse_os_release
from shared.executor.port import ExecContext, ExecResult

__all__ = [
    "ALLOWED_SERVICE_PATTERNS",
    "ALLOWED_SUDO_RULES",
    "ISOLATED_STATE_MARKERS",
    "check_service_allowed",
    "classify_verify_output",
    "ensure_sudo_allowed",
]


# --------------------------------------------------------------------------
# Whitelists. Tight by default; widening one of these is a product decision,
# not a refactor.
# --------------------------------------------------------------------------
#: Unit / service names this build may restart or inspect. Anything else is
#: refused even when the path is valid — "restart" is a DoS primitive.
ALLOWED_SERVICE_PATTERNS: tuple[str, ...] = (
    "opspilot-*",
    "nginx",
    "supervisor",
    "redis-server",
    "mysql",
)

#: Path components that mark live application state. An install must never
#: write beneath them.
ISOLATED_STATE_MARKERS: tuple[str, ...] = ("/shared/", "/data/")

#: The restricted sudo rules a template may rely on. An action that needs
#: privilege outside this list is refused rather than escalated. Notably
#: absent: ``chmod``, ``chown``, ``cp``, ``mv``, ``rm``, ``dd``, ``sh``,
#: ``bash`` — every one of those is a general-purpose file primitive, and
#: handing them root is how a whitelist becomes a shell.
ALLOWED_SUDO_RULES: tuple[str, ...] = (
    "/usr/bin/apt-get",
    "/usr/sbin/useradd",
    "/usr/sbin/usermod",
    "/usr/bin/systemctl",
    "/usr/bin/supervisorctl",
    "/usr/sbin/nginx",
    # The one write primitive that may run as root: it copies a file the SSH
    # user already staged, into a location the user cannot write. sudoers can
    # scope it to a destination directory.
    "/usr/bin/install",
)

#: Scratch area the SSH user owns, used to stage privileged writes and to park
#: backups of root-owned files. Inside the write allowlist on purpose.
STAGE_DIR = "/opt/opspilot/test"

#: ``systemd-analyze verify`` line for an ``ExecStart`` that does not resolve.
_NOT_EXECUTABLE_RE = re.compile(r"Command (\S+) is not executable")


def classify_verify_output(stderr: str) -> tuple[list[str], list[str]]:
    """Split ``systemd-analyze verify`` output into (problems, deferred).

    The check the design asks for is a *syntax* check (§8.2 语法检查), and
    ``verify`` gives more than that: it also refuses a unit whose ``ExecStart``
    does not resolve. A unit that names ``<app_root>/current/...`` is installed
    *before* the link flips — that indirection is what makes an early install
    safe — so at verify time that path is not supposed to exist yet. A missing
    binary under a ``current/`` segment is the plan working as designed and
    comes back as *deferred*; a missing binary anywhere else is a unit nobody
    can start.

    systemd's own warnings ("Failed to parse … ignoring", "Unknown section …
    Ignoring") are dropped entirely: it has already said it is not acting on
    them. Everything else is a *problem*.

    An exit with nothing to classify is not a pass — see the caller.
    """
    problems: list[str] = []
    deferred: list[str] = []
    for line in stderr.splitlines():
        text = line.strip()
        if not text:
            continue
        match = _NOT_EXECUTABLE_RE.search(text)
        if match and "/current/" in match.group(1):
            deferred.append(text)
            continue
        if "ignoring" in text.lower():
            continue
        problems.append(text)
    return problems, deferred

_HTTP_SCHEMES = frozenset({"http", "https"})
_HTTP_PORTS = frozenset({80, 443, 8000, 8080, 8443, 8888, 9000, 9090})

#: Hosts a request must never be pointed at.
_BLOCKED_HOSTNAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "localhost",
        "localhost.localdomain",
    }
)
_BLOCKED_HOST_RANGES = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
)

_HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MODE_RE = re.compile(r"0?[0-7]{3}")
_TELEGRAM_TOKEN_RE = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{20,}$")
_TELEGRAM_CHAT_RE = re.compile(r"^-?[0-9]{1,20}$")


def check_service_allowed(name: str) -> str:
    """Refuse a service name that is not on the restart whitelist."""
    for pattern in ALLOWED_SERVICE_PATTERNS:
        if fnmatch.fnmatchcase(name, pattern):
            return name
    allowed = ", ".join(ALLOWED_SERVICE_PATTERNS)
    raise ValueError(f"service {name!r} is not on the allowed list ({allowed})")


def ensure_sudo_allowed(program: str) -> str:
    if program not in ALLOWED_SUDO_RULES:
        allowed = ", ".join(ALLOWED_SUDO_RULES)
        raise ValueError(
            f"sudo is not allowed for {program!r}; the restricted list is: {allowed}"
        )
    return program


def _no_state_path(value: str) -> str:
    validate_path(value)
    for marker in ISOLATED_STATE_MARKERS:
        if marker in value:
            raise ValueError(
                f"path {value!r} is inside {marker.rstrip('/')}, which an install "
                "must never overwrite"
            )
    return value


#: Names that must never be a release id. ``current`` and ``shared`` are
#: siblings of ``releases/<id>`` under the app root, and ``latest`` is exactly
#: the unpinned reference production deploys are forbidden to use.
_RESERVED_RELEASE_IDS = frozenset({"latest", "current", "shared", "releases", "ops"})


def check_release_id(value: str) -> str:
    if not _RELEASE_ID_RE.match(value):
        raise ValueError(f"invalid release_id {value!r}")
    if value.lower() in _RESERVED_RELEASE_IDS:
        raise ValueError(
            f"release_id {value!r} is reserved; pin a concrete version, never 'latest'"
        )
    return value


def check_url(url: str, *, label: str = "url") -> str:
    """Scheme / host / port allowlist for anything that makes a request."""
    parsed = urlparse(url)
    if parsed.scheme not in _HTTP_SCHEMES:
        raise ValueError(f"{label} scheme must be http or https, got {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise ValueError(f"{label} must not embed credentials")
    if parsed.fragment:
        raise ValueError(f"{label} must not contain a fragment")

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"{label} must have a host")
    if host in _BLOCKED_HOSTNAMES:
        raise ValueError(f"{label} host {host!r} is not an acceptable target")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid port") from exc
    resolved_port = port or (443 if parsed.scheme == "https" else 80)
    if resolved_port not in _HTTP_PORTS:
        raise ValueError(f"{label} port {resolved_port} is not on the allowlist")

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        if not _HOSTNAME_RE.match(host):
            raise ValueError(f"{label} has an invalid host {host!r}") from None
    else:
        if any(addr in net for net in _BLOCKED_HOST_RANGES):
            raise ValueError(f"{label} host {host} is in a blocked range")
        if addr.is_link_local or addr.is_multicast or addr.is_unspecified:
            raise ValueError(f"{label} host {host} is not an acceptable target")
    return url


def _sha256_hex(data: str | bytes) -> str:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


class _Bases(Action[Any]):
    """Shared plumbing for every built-in."""

    def _exec(
        self,
        ctx: ExecContext,
        argv: Sequence[str],
        *,
        stdin: bytes | str | None = None,
    ) -> ExecResult:
        return self.port.run(ctx, ensure_argv(argv), stdin=stdin)

    def _sudo(self, ctx: ExecContext, program: str, *args: str) -> ExecResult:
        """Run *program* through the restricted sudo list — never anything else."""
        ensure_sudo_allowed(program)
        return self._exec(ctx, ["sudo", "-n", program, *args])

    def _from_exec(self, result: ExecResult, summary: str = "") -> ActionResult:
        return ActionResult.from_exec(result, summary)

    def _exists(self, ctx: ExecContext, path: str) -> bool:
        """Does *path* exist on the host?

        No ``--`` before the operand, deliberately. POSIX ``test`` is not a
        regular GNU utility: it does **not** treat ``--`` as an end-of-options
        marker. ``test -e -- /some/path`` has three operands, so ``test`` reads
        ``--`` as a binary operator and exits 2 with "binary operator expected"
        — which looks exactly like "the path is not there". That silently
        disabled every existence probe: nothing was ever backed up, releases
        were always re-unpacked, lock files always looked missing, and
        ``app.switch_current`` refused to move ``current``.

        The operand cannot be mistaken for an option: every path that reaches
        here is validated absolute (see :func:`shared.actions.render.validate_read_path`)
        and therefore begins with ``/``.
        """
        return self._exec(ctx, ["test", "-e", path]).ok


# ==========================================================================
# system.*
# ==========================================================================
class CheckOsParams(ParamsModel):
    expect: list[str] = Field(default_factory=list, max_length=32)


@register(
    "system.check_os",
    params=CheckOsParams,
    safe=True,
    idempotency_class="SAFE",
    description="Read the distribution and version; never changes the host.",
)
class SystemCheckOs(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)
        result = self._exec(ctx, ["cat", "/etc/os-release"])
        release = parse_os_release(result.stdout)

        pretty = release.get("PRETTY_NAME", "")
        os_id = release.get("ID", "").lower()
        version = release.get("VERSION_ID", "")
        # The vocabulary callers use is ``<id>-<version>`` — 'ubuntu-24.04',
        # 'debian-12' — which is not a substring of /etc/os-release and cannot
        # be matched against the raw file. Build the id and compare to that.
        ident = f"{os_id}-{version}" if version else os_id

        if p.expect and not (set(p.expect) & {ident, os_id}):
            return ActionResult.failure(
                f"OS {pretty or 'unknown'} ({ident or 'unknown'}) is not one of {p.expect}",
                pretty_name=pretty,
                platform=ident,
                expect=list(p.expect),
            )
        return ActionResult(
            ok=True,
            summary=f"OS is {pretty or ident or 'unknown'}",
            data={
                "pretty_name": pretty,
                "platform": ident,
                "exit_code": result.exit_code,
            },
            changed=False,
            argv=result.argv,
        )


class CheckDiskParams(ParamsModel):
    path: ReadPath = "/opt/opspilot/apps"
    min_available_mb: positive_int = 1024


@register(
    "system.check_disk",
    params=CheckDiskParams,
    safe=True,
    idempotency_class="SAFE",
    description="Check free space under a path; read-only.",
)
class SystemCheckDisk(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)
        # ``df`` cannot stat a path that does not exist yet, and a free-space
        # precheck on a release directory is run *before* the plan creates it.
        # Walk up to the nearest existing ancestor: the space that matters is
        # the filesystem the directory will be created on.
        target = p.path
        result = self._exec(ctx, ["df", "-Pm", "--", target])
        for _ in range(16):
            if result.ok:
                break
            stripped = target.rstrip("/")
            if "/" not in stripped:
                break
            parent = stripped.rsplit("/", 1)[0] or "/"
            if parent == target:
                break
            target = parent
            result = self._exec(ctx, ["df", "-Pm", "--", target])

        if not result.ok:
            return ActionResult.failure(f"could not read free space for {p.path}")

        rows = [ln for ln in result.stdout.splitlines() if ln.strip()]
        if len(rows) < 2:
            return ActionResult.failure("df produced no data row")
        fields = rows[-1].split()
        if len(fields) < 4 or not fields[3].isdigit():
            return ActionResult.failure(f"could not parse df output: {rows[-1]!r}")
        available_mb = int(fields[3])

        if available_mb < p.min_available_mb:
            return ActionResult.failure(
                f"{target} has {available_mb} MB free, need {p.min_available_mb} MB",
                available_mb=available_mb,
                required_mb=p.min_available_mb,
            )
        measured = f"{p.path} (via {target})" if target != p.path else p.path
        return ActionResult(
            ok=True,
            summary=f"{measured} has {available_mb} MB free",
            data={"path": p.path, "measured_path": target, "available_mb": available_mb},
            changed=False,
            argv=result.argv,
        )


class EnsureUserParams(ParamsModel):
    username: UserName
    shell: str = "/usr/sbin/nologin"
    create_home: bool = False


@register(
    "system.ensure_user",
    params=EnsureUserParams,
    idempotency_class="CONDITIONAL",
    requires_sudo=True,
    description="Create a system user if missing; skips when it exists.",
)
class SystemEnsureUser(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)
        if not p.shell.startswith("/"):
            raise ValueError("shell must be an absolute path")

        probe = self._exec(ctx, ["id", "-u", "--", p.username])
        if probe.ok:
            return ActionResult.skipped(f"user {p.username} already exists")

        argv = [
            "/usr/sbin/useradd",
            "--system",
            "--shell",
            p.shell,
            "--create-home" if p.create_home else "--no-create-home",
            "--",
            p.username,
        ]
        result = self._sudo(ctx, *argv)
        return self._from_exec(result, f"created user {p.username}")


# ==========================================================================
# package.* / python.*
# ==========================================================================
class PackageInstallParams(ParamsModel):
    packages: Packages
    update_index: bool = False


@register(
    "package.install",
    params=PackageInstallParams,
    idempotency_class="CONDITIONAL",
    requires_sudo=True,
    description="Install packages with apt; skips ones already present.",
)
class PackageInstall(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)

        # `${Package}` is dpkg-query's own field syntax, not shell expansion:
        # the string is one quoted argv token and the shell never sees it.
        check = self._exec(
            ctx, ["dpkg-query", "-W", "-f=${Package}\\n", "--", *p.packages]
        )
        installed = {ln.strip() for ln in check.stdout.splitlines() if ln.strip()}
        missing = [name for name in p.packages if name not in installed]
        if not missing:
            return ActionResult.skipped("all requested packages are already installed")

        if p.update_index:
            update = self._sudo(ctx, "/usr/bin/apt-get", "-y", "update")
            if not update.ok:
                return ActionResult.failure(
                    "apt-get update failed", stderr=update.stderr[:500]
                )

        result = self._sudo(
            ctx,
            "/usr/bin/apt-get",
            "-y",
            "--no-install-recommends",
            "install",
            "--",
            *missing,
        )
        return self._from_exec(result, f"installed {', '.join(missing)}")


class PythonRuntimeParams(ParamsModel):
    version: Version
    # Empty means "derive python<version>-venv"; the action does that below.
    package: str = ""


@register(
    "python.ensure_runtime",
    params=PythonRuntimeParams,
    idempotency_class="CONDITIONAL",
    requires_sudo=True,
    description="Ensure a Python interpreter of the requested version exists.",
)
class PythonEnsureRuntime(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)
        marker = f"Python {p.version}"

        probe = self._exec(ctx, [f"python{p.version}", "--version"])
        if marker in probe.stdout or marker in probe.stderr:
            return ActionResult.skipped(f"python{p.version} is already present")

        package = p.package or f"python{p.version}-venv"
        result = self._sudo(
            ctx,
            "/usr/bin/apt-get",
            "-y",
            "--no-install-recommends",
            "install",
            "--",
            package,
        )
        return self._from_exec(result, f"installed {package}")


# ==========================================================================
# file.*
# ==========================================================================
class FileRenderParams(ParamsModel):
    path: RemotePath
    kind: Literal["file", "directory"] = "file"
    content: str = ""
    mode: str = "0644"
    owner: OptionalUserName = ""
    backup: bool = True

    @model_validator(mode="after")
    def _check(self) -> FileRenderParams:
        if not _MODE_RE.match(self.mode):
            raise ValueError(f"mode must be 3 or 4 octal digits, got {self.mode!r}")
        return self


@register(
    "file.render",
    params=FileRenderParams,
    idempotency_class="CONDITIONAL",
    description="Atomically write a file or create a directory under an allowed path.",
)
class FileRender(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)

        if p.kind == "directory":
            made = self._exec(ctx, ["mkdir", "-p", "--", p.path])
            self._exec(ctx, ["chmod", p.mode, "--", p.path])
            if p.owner:
                self._exec(ctx, ["chown", p.owner, "--", p.path])
            return self._from_exec(made, f"ensured directory {p.path}")

        # No sudo anywhere in here. This action writes where the SSH user can
        # already write; a config that needs root is a privileged install and
        # goes through systemd.render_unit instead.
        backed_up = False
        if p.backup and self._exists(ctx, p.path):
            copy = self._exec(ctx, ["cp", "-p", "--", p.path, f"{p.path}.opspilot-bak"])
            if not copy.ok:
                return ActionResult.failure(
                    f"could not back up {p.path}", stderr=copy.stderr[:500]
                )
            backed_up = True

        tmp = f"{p.path}.opspilot-new"
        # ``dd of=`` cannot create the directory it writes into either, and the
        # deploy's env file lives at ``shared/config/app.env`` — a tree that a
        # first deploy has not built. Create the parent before writing.
        parent = posixpath.dirname(p.path)
        made = self._exec(ctx, ["mkdir", "-p", "--", parent])
        if not made.ok:
            return ActionResult.failure(
                f"could not create {parent}", stderr=made.stderr[:500]
            )

        # `dd of=` consumes stdin and writes nothing back. `tee` would echo the
        # contents into stdout, and stdout is part of what we persist.
        write = self._exec(ctx, ["dd", f"of={tmp}", "status=none"], stdin=p.content)
        if not write.ok:
            return ActionResult.failure(f"could not write {tmp}", stderr=write.stderr[:500])

        self._exec(ctx, ["chmod", p.mode, "--", tmp])
        if p.owner:
            self._exec(ctx, ["chown", p.owner, "--", tmp])
        rename = self._exec(ctx, ["mv", "-f", "--", tmp, p.path])
        if not rename.ok:
            return ActionResult.failure(
                f"could not replace {p.path}", stderr=rename.stderr[:500]
            )

        return ActionResult(
            ok=True,
            summary=f"wrote {p.path}",
            data={
                "path": p.path,
                "kind": "file",
                "mode": p.mode,
                "owner": p.owner,
                "backup": backed_up,
                "sha256": _sha256_hex(p.content),
                "bytes": len(p.content.encode("utf-8")),
            },
            changed=True,
            argv=rename.argv,
        )


# ==========================================================================
# app.*
# ==========================================================================
class FetchReleaseParams(ParamsModel):
    app_root: RemotePath
    release_id: NonEmpty
    source: NonEmpty
    checksum: NonEmpty = ""

    @model_validator(mode="after")
    def _check(self) -> FetchReleaseParams:
        check_release_id(self.release_id)
        check_url(self.source, label="source")
        _no_state_path(f"{self.app_root}/releases/{self.release_id}")
        return self


@register(
    "app.fetch_release",
    params=FetchReleaseParams,
    idempotency_class="CONDITIONAL",
    description="Fetch a pinned artifact and verify its checksum.",
)
class AppFetchRelease(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)
        target = f"{p.app_root}/releases/{p.release_id}"
        artifact = f"{target}.artifact"

        # ``curl -o`` cannot create the directory it writes into. On a first
        # deploy nothing has created ``releases/`` yet — ``system.check_disk``
        # only *measures* its parent — and curl fails with exit 23 ("Failure
        # writing output to destination") for want of a mkdir nobody ran.
        # Unconditional rather than exists-checked: ``mkdir -p`` is idempotent,
        # and probing first would burn a ``test`` response for nothing.
        made = self._exec(ctx, ["mkdir", "-p", "--", f"{p.app_root}/releases"])
        if not made.ok:
            return ActionResult.failure(
                f"could not create {p.app_root}/releases", stderr=made.stderr[:500]
            )

        fetch = self._exec(
            ctx,
            [
                "curl",
                "-fsSL",
                "--proto",
                "=http,https",
                "--max-time",
                "600",
                "-o",
                artifact,
                "--",
                p.source,
            ],
        )
        if not fetch.ok:
            return ActionResult.failure(
                f"fetch failed for {p.source}", stderr=fetch.stderr[:500]
            )

        actual = ""
        if p.checksum:
            digest = self._exec(ctx, ["sha256sum", "--", artifact])
            parts = digest.stdout.split()
            actual = parts[0] if parts else ""
            if actual != p.checksum:
                return ActionResult.failure(
                    "checksum mismatch; refusing to install",
                    expected=p.checksum,
                    actual=actual,
                )

        return ActionResult(
            ok=True,
            summary=f"fetched {p.release_id}",
            data={
                "release_id": p.release_id,
                "artifact": artifact,
                "source": p.source,
                "sha256": actual or None,
                "checksum_verified": bool(p.checksum),
            },
            changed=True,
            argv=fetch.argv,
        )


class InstallReleaseParams(ParamsModel):
    app_root: RemotePath
    release_id: NonEmpty
    owner: OptionalUserName = ""
    #: Build the release's own virtualenv from this interpreter version. Empty
    #: means "no venv" — the release ships one of its own, or is not Python.
    python_version: OptionalVersion = ""
    #: Lock file *inside* the release, relative to its root. Relative on
    #: purpose: an absolute path here would be a way to pip-install from
    #: anywhere on the host.
    requirements: str = "requirements.lock"

    @model_validator(mode="after")
    def _check(self) -> InstallReleaseParams:
        check_release_id(self.release_id)
        _no_state_path(f"{self.app_root}/releases/{self.release_id}")
        if self.requirements and (
            self.requirements.startswith("/") or ".." in self.requirements.split("/")
        ):
            raise ValueError(
                f"requirements must be a path inside the release, got {self.requirements!r}"
            )
        return self


@register(
    "app.install_release",
    params=InstallReleaseParams,
    idempotency_class="CONDITIONAL",
    description="Unpack a release into releases/<id>; never touches shared/.",
)
class AppInstallRelease(_Bases):
    """Unpack a release and, optionally, give it its own virtualenv.

    The venv lives inside ``releases/<id>/`` rather than beside ``shared/``,
    so two releases can be ready at the same time and a rollback does not have
    to rebuild dependencies. Re-running finds the venv already there and leaves
    it alone.
    """

    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)
        target = f"{p.app_root}/releases/{p.release_id}"
        artifact = f"{target}.artifact"

        already = self._exists(ctx, target)
        if not already:
            made = self._exec(ctx, ["mkdir", "-p", "--", target])
            if not made.ok:
                return ActionResult.failure(
                    f"could not create {target}", stderr=made.stderr[:500]
                )

            unpack = self._exec(
                ctx, ["tar", "-xzf", artifact, "-C", target, "--strip-components=1"]
            )
            if not unpack.ok:
                # Leave nothing half-unpacked behind: a partial release directory
                # is worse than a missing one, because the symlink switch would
                # happily point at it.
                self._exec(ctx, ["rm", "-rf", "--", target])
                return ActionResult.failure("unpack failed", stderr=unpack.stderr[:500])

            if p.owner:
                self._exec(ctx, ["chown", "-R", p.owner, "--", target])

        built = self._ensure_venv(ctx, p, target)
        if built is not None:
            return built

        if already:
            return ActionResult.skipped(
                f"release {p.release_id} is already installed",
                venv=bool(p.python_version),
            )
        return ActionResult(
            ok=True,
            summary=f"installed release {p.release_id}",
            data={
                "release_id": p.release_id,
                "path": target,
                "venv": bool(p.python_version),
                "requirements": p.requirements if p.python_version else None,
            },
            changed=True,
            argv=(),
        )

    def _ensure_venv(
        self, ctx: ExecContext, p: InstallReleaseParams, target: str
    ) -> ActionResult | None:
        """Create the release venv and install the locked deps.

        Returns ``None`` when the install should report its own outcome, or a
        terminal result when the venv work failed or there is nothing left to
        say about it.
        """
        if not p.python_version:
            return None

        venv = f"{target}/venv"
        if self._exists(ctx, venv):
            return ActionResult.skipped(
                f"venv for {p.release_id} is already built", venv=True
            )

        made = self._exec(ctx, [f"python{p.python_version}", "-m", "venv", "--", venv])
        if not made.ok:
            return ActionResult.failure(
                f"could not create the venv for {p.release_id}", stderr=made.stderr[:500]
            )

        if p.owner:
            self._exec(ctx, ["chown", "-R", p.owner, "--", venv])

        if not p.requirements:
            return None
        lock = f"{target}/{p.requirements}"
        if not self._exists(ctx, lock):
            # A release with no lock file is not an error: it may ship a venv of
            # its own, or need nothing installed. Worth saying so, though.
            return None

        # --no-deps is the point of a lock file: it already lists every
        # transitive requirement, so letting pip resolve anything would mean
        # the deployed set is not the reviewed set.
        install = self._exec(
            ctx,
            [
                f"{venv}/bin/pip",
                "install",
                "--no-deps",
                "--disable-pip-version-check",
                "-r",
                lock,
            ],
        )
        if not install.ok:
            return ActionResult.failure(
                f"could not install locked dependencies for {p.release_id}",
                stderr=install.stderr[:500],
            )
        return None


class SwitchCurrentParams(ParamsModel):
    app_root: RemotePath
    release_id: NonEmpty

    @model_validator(mode="after")
    def _check(self) -> SwitchCurrentParams:
        check_release_id(self.release_id)
        _no_state_path(f"{self.app_root}/releases/{self.release_id}")
        return self


@register(
    "app.switch_current",
    params=SwitchCurrentParams,
    idempotency_class="CONDITIONAL",
    description="Atomically point current at releases/<id>; never touches shared/.",
)
class AppSwitchCurrent(_Bases):
    """Make one release live.

    The switch has to be a single rename, not "unlink then symlink": between
    those two a process that restarts finds no ``current`` at all and dies. So
    the new link is built under a temporary name and ``mv -T`` renames it over
    the old one in one syscall. ``-T`` is load-bearing — without it ``mv``
    would move the link *into* the directory the old link points at.

    Re-pointing at the release ``current`` already names is a no-op, which is
    what makes a retried deploy and a rollback-to-current safe to run twice.
    """

    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)
        target = f"{p.app_root}/releases/{p.release_id}"
        link = f"{p.app_root}/current"
        wanted = f"releases/{p.release_id}"

        if not self._exists(ctx, target):
            return ActionResult.failure(
                f"release {p.release_id} is not installed; refusing to move current onto it"
            )

        read = self._exec(ctx, ["readlink", "--", link])
        was = read.stdout.strip()
        if was in {wanted, target}:
            return ActionResult.skipped(
                f"current already points at {p.release_id}", previous=was or None
            )

        scratch = f"{p.app_root}/.current.opspilot-new"
        self._exec(ctx, ["rm", "-f", "--", scratch])
        made = self._exec(ctx, ["ln", "-s", "--", wanted, scratch])
        if not made.ok:
            return ActionResult.failure(
                "could not stage the new current link", stderr=made.stderr[:500]
            )

        swap = self._exec(ctx, ["mv", "-Tf", "--", scratch, link])
        if not swap.ok:
            self._exec(ctx, ["rm", "-f", "--", scratch])
            return ActionResult.failure(
                "could not switch current", stderr=swap.stderr[:500]
            )

        after = self._exec(ctx, ["readlink", "--", link]).stdout.strip()
        if after not in {wanted, target}:
            return ActionResult.failure(
                f"current is {after!r} after the switch, expected {wanted!r}"
            )

        return ActionResult(
            ok=True,
            summary=f"current -> {p.release_id}",
            data={
                "app_root": p.app_root,
                "release_id": p.release_id,
                "previous": was or None,
                "current": after,
            },
            changed=True,
            argv=swap.argv,
        )


# ==========================================================================
# systemd.* / supervisor.*
# ==========================================================================
class RenderUnitParams(ParamsModel):
    unit_path: RemotePath
    content: NonEmpty
    mode: str = "0644"

    @model_validator(mode="after")
    def _check(self) -> RenderUnitParams:
        if not self.unit_path.startswith("/etc/systemd/system/"):
            raise ValueError("unit_path must live under /etc/systemd/system/")
        if not self.unit_path.endswith(".service"):
            raise ValueError("unit_path must be a .service file")
        if "/" in self.unit_path[len("/etc/systemd/system/") :]:
            raise ValueError("unit_path must not create subdirectories")
        if not _MODE_RE.match(self.mode):
            raise ValueError(f"mode must be 3 or 4 octal digits, got {self.mode!r}")
        return self


@register(
    "systemd.render_unit",
    params=RenderUnitParams,
    idempotency_class="CONDITIONAL",
    requires_sudo=True,
    description="Install a systemd unit after backing up any existing one.",
)
class SystemdRenderUnit(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)
        backup_path = f"{p.unit_path}.bak"

        # Park a copy of the current unit before touching it. The destination is
        # root-owned, so this goes through the same restricted install rule as
        # the write itself — never a general-purpose `cp` with root.
        backed_up = False
        if self._exists(ctx, p.unit_path):
            backup = self._sudo(
                ctx, "/usr/bin/install", "-m", p.mode, p.unit_path, backup_path
            )
            if not backup.ok:
                return ActionResult.failure(
                    f"could not back up {p.unit_path}", stderr=backup.stderr[:500]
                )
            backed_up = True

        # Stage the content where the SSH user can write, then install it.
        # The mkdir is checked: a stage dir the SSH user cannot create produces
        # a confusing dd error otherwise, which sends an operator looking at
        # the wrong thing. The stage dir is a fixed scratch area, not a caller
        # input, so there is no path to validate here.
        made = self._exec(ctx, ["mkdir", "-p", "--", STAGE_DIR])
        if not made.ok:
            return ActionResult.failure(
                f"could not create the staging area {STAGE_DIR}",
                stderr=made.stderr[:500],
            )
        staged = f"{STAGE_DIR}/.unit-{p.unit_path.rsplit('/', 1)[-1]}.new"
        write = self._exec(ctx, ["dd", f"of={staged}", "status=none"], stdin=p.content)
        if not write.ok:
            return ActionResult.failure(
                "could not stage the unit file", stderr=write.stderr[:500]
            )

        install = self._sudo(ctx, "/usr/bin/install", "-m", p.mode, staged, p.unit_path)
        self._exec(ctx, ["rm", "-f", "--", staged])
        if not install.ok:
            return ActionResult.failure(
                f"could not install {p.unit_path}", stderr=install.stderr[:500]
            )

        # Syntax check after the write. A unit systemd refuses to parse must not
        # be left in place — put the backup back. See `classify_verify_output`
        # for why a not-yet-flipped `current` path is not a parse failure.
        verify = self._exec(
            ctx, ["/usr/bin/systemd-analyze", "verify", "--", p.unit_path]
        )
        problems, deferred = classify_verify_output(verify.stderr)
        if not verify.ok and not problems and not deferred:
            # Exited badly with nothing to classify — do not guess that it is
            # fine. An empty stderr is not the same as an explained exit.
            problems = [
                verify.stderr.strip()
                or f"systemd-analyze verify exited {verify.exit_code}"
            ]
        if problems:
            restored = False
            if backed_up:
                restored = self._sudo(
                    ctx, "/usr/bin/install", "-m", p.mode, backup_path, p.unit_path
                ).ok
            return ActionResult.failure(
                f"unit {p.unit_path} failed systemd-analyze verify"
                + ("; previous unit restored" if restored else ""),
                stderr="\n".join(problems)[:500],
            )

        reload = self._sudo(ctx, "/usr/bin/systemctl", "daemon-reload")
        return ActionResult(
            ok=reload.ok,
            summary=f"installed unit {p.unit_path}",
            data={
                "unit_path": p.unit_path,
                "mode": p.mode,
                "backup": backed_up,
                "sha256": _sha256_hex(p.content),
                "daemon_reloaded": reload.ok,
                "syntax_checked": True,
                "exec_start_pending": bool(deferred),
            },
            changed=True,
            argv=install.argv,
        )


class RestartParams(ParamsModel):
    service: ServiceName

    @model_validator(mode="after")
    def _check(self) -> RestartParams:
        check_service_allowed(self.service)
        return self


@register(
    "systemd.restart",
    params=RestartParams,
    idempotency_class="DESTRUCTIVE",
    requires_sudo=True,
    description="Restart a whitelisted systemd unit.",
)
class SystemdRestart(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)
        result = self._sudo(ctx, "/usr/bin/systemctl", "restart", "--", p.service)
        return self._from_exec(result, f"restarted {p.service}")


class SupervisorRestartParams(ParamsModel):
    program: ServiceName

    @model_validator(mode="after")
    def _check(self) -> SupervisorRestartParams:
        check_service_allowed(self.program)
        return self


@register(
    "supervisor.restart",
    params=SupervisorRestartParams,
    idempotency_class="DESTRUCTIVE",
    requires_sudo=True,
    description="Restart a whitelisted Supervisor program group.",
)
class SupervisorRestart(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)
        result = self._sudo(ctx, "/usr/bin/supervisorctl", "restart", "--", p.program)
        return self._from_exec(result, f"restarted supervisor program {p.program}")


# ==========================================================================
# http.*
# ==========================================================================
class HttpCheckParams(ParamsModel):
    url: NonEmpty
    expect_status: int = 200
    timeout_seconds: int = Field(default=10, ge=1, le=60)

    @model_validator(mode="after")
    def _check(self) -> HttpCheckParams:
        check_url(self.url)
        return self


@register(
    "http.check",
    params=HttpCheckParams,
    safe=True,
    idempotency_class="SAFE",
    description="HTTP health check against an allowlisted scheme, host and port.",
)
class HttpCheck(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)
        result = self._exec(
            ctx,
            [
                "curl",
                "-sS",
                "--proto",
                "=http,https",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--max-time",
                str(p.timeout_seconds),
                "--max-redirs",
                "3",
                "--",
                p.url,
            ],
        )
        code = result.stdout.strip()
        if code != str(p.expect_status):
            return ActionResult.failure(
                f"health check returned {code or 'no status'}, expected {p.expect_status}",
                status=code,
                expected=p.expect_status,
            )
        return ActionResult(
            ok=True,
            summary=f"health check returned {code}",
            data={"status": code, "expected": p.expect_status},
            changed=False,
            argv=result.argv,
        )


# ==========================================================================
# mysql.* / redis.*  — read-only, and deliberately so
# ==========================================================================
class MysqlPrecheckParams(ParamsModel):
    host: NonEmpty = "127.0.0.1"
    port: tcp_port = 3306
    #: Read-only probe of a data directory. Never created, never initialised.
    datadir: ReadPath = ""
    expect_existing_datadir: bool = True


@register(
    "mysql.precheck",
    params=MysqlPrecheckParams,
    safe=True,
    idempotency_class="SAFE",
    description="Read-only MySQL probe. Never initialises or resets a datadir.",
)
class MysqlPrecheck(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)

        if p.datadir:
            if self._exists(ctx, p.datadir):
                listing = self._exec(ctx, ["ls", "-A", "--", p.datadir])
                entries = [ln for ln in listing.stdout.splitlines() if ln.strip()]
                return ActionResult(
                    ok=True,
                    summary=f"datadir {p.datadir} exists",
                    data={
                        "datadir": p.datadir,
                        "exists": True,
                        "nonempty": bool(entries),
                        "initialised": False,
                    },
                    changed=False,
                    argv=listing.argv,
                )
            if p.expect_existing_datadir:
                # Explicitly refusing to initialise. Creating a data directory is
                # exactly the irreversible act the design document forbids, so
                # this is a hard stop, not a warning.
                return ActionResult.failure(
                    f"datadir {p.datadir} does not exist; "
                    "OpsPilot will never initialise it",
                    datadir=p.datadir,
                    initialised=False,
                )

        result = self._exec(
            ctx,
            [
                "mysqladmin",
                "--connect-timeout=5",
                "-h",
                p.host,
                "-P",
                str(p.port),
                "ping",
            ],
        )
        reachable = "mysqld is alive" in result.stdout
        return ActionResult(
            ok=reachable,
            summary="mysqld is reachable" if reachable else "mysqld is not reachable",
            data={
                "host": p.host,
                "port": p.port,
                "reachable": reachable,
                "initialised": False,
            },
            changed=False,
            argv=result.argv,
        )


class RedisPrecheckParams(ParamsModel):
    host: NonEmpty = "127.0.0.1"
    port: tcp_port = 6379
    #: Read-only existence probe. Never written, never replaced.
    config_path: ReadPath = ""


@register(
    "redis.precheck",
    params=RedisPrecheckParams,
    safe=True,
    idempotency_class="SAFE",
    description="Read-only Redis probe. Never overwrites an existing config.",
)
class RedisPrecheck(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)

        if p.config_path and self._exists(ctx, p.config_path):
            return ActionResult(
                ok=True,
                summary=f"config {p.config_path} already exists; left untouched",
                data={
                    "config_path": p.config_path,
                    "config_exists": True,
                    "config_overwritten": False,
                },
                changed=False,
                argv=("test", "-e", p.config_path),
            )

        result = self._exec(
            ctx,
            ["redis-cli", "-h", p.host, "-p", str(p.port), "--no-auth-warning", "PING"],
        )
        alive = result.stdout.strip() == "PONG"
        return ActionResult(
            ok=alive,
            summary="redis answered PING" if alive else "redis did not answer",
            data={
                "host": p.host,
                "port": p.port,
                "alive": alive,
                "config_exists": bool(p.config_path) and False,
                "config_overwritten": False,
            },
            changed=False,
            argv=result.argv,
        )


# ==========================================================================
# notification.*
# ==========================================================================
class TelegramParams(ParamsModel):
    bot_token: NonEmpty
    chat_id: NonEmpty
    text: NonEmpty

    @model_validator(mode="after")
    def _check(self) -> TelegramParams:
        if not _TELEGRAM_TOKEN_RE.match(self.bot_token):
            raise ValueError("bot_token does not look like a Telegram bot token")
        if not _TELEGRAM_CHAT_RE.match(self.chat_id):
            raise ValueError(f"invalid chat_id {self.chat_id!r}")
        return self


@register(
    "notification.telegram",
    params=TelegramParams,
    idempotency_class="SAFE",
    description="Send a deployment result to Telegram. The token is never logged.",
)
class NotificationTelegram(_Bases):
    def run(self, ctx: ExecContext, params: Mapping[str, Any]) -> ActionResult:
        p = self.build_params(params)

        # The token lives inside a URL, so argument ordering cannot keep it out
        # of argv — and argv is logged and persisted. It goes into curl's config
        # on **stdin** instead: same request, nothing written down.
        curl_config = (
            f'url = "https://api.telegram.org/bot{p.bot_token}/sendMessage"\n'
            'output = "/dev/null"\n'
            "silent\n"
            "show-error\n"
            'write-out = "%{http_code}"\n'
        )
        result = self._exec(
            ctx,
            [
                "curl",
                "--proto",
                "=https",
                "--max-time",
                "15",
                "-X",
                "POST",
                "-d",
                f"chat_id={p.chat_id}",
                "--data-urlencode",
                f"text={p.text}",
                "--config",
                "-",
            ],
            stdin=curl_config,
        )

        code = result.stdout.strip()
        if code != "200":
            return ActionResult.failure(
                f"telegram send failed with HTTP {code or 'no status'}",
                status=code,
            )
        return ActionResult(
            ok=True,
            summary="sent",
            data={"status": code, "chat_id": p.chat_id},
            changed=False,
            # The real argv contains no token — that is the whole point — so the
            # audit trail can record it verbatim.
            argv=result.argv,
        )
