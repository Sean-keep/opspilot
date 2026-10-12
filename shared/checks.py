"""Read-only system checks.

``system.check_os`` and ``system.check_disk`` are the two S1 actions. Both are
read-only (``safe=True`` in the eventual Action Registry) and both are
idempotent: running them twice produces two facts snapshots and mutates
nothing on the host.

Every remote command is an argv list. Nothing here builds a shell line from
user input, and nothing here runs as root — the SSH user is whatever the
server record says, and that is a non-root account by policy.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from shared.executor.port import ExecContext, ExecPort, ExecResult

log = structlog.get_logger(__name__)

__all__ = ["CheckError", "SystemFacts", "collect_facts"]


class CheckError(Exception):
    """A check could not produce usable facts (not a transport failure).

    Lives in ``shared/`` so the worker can raise it without importing
    ``backend.app``. The API catches it and re-raises its own ``ApiError``
    with the same ``status_code`` / ``code``.
    """

    status_code = 502
    code = "check_failed"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        # Diagnostic context for the operator. Callers must not put secrets here.
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class SystemFacts:
    """Parsed result of one facts collection."""

    os_name: str = ""
    os_version: str = ""
    kernel: str = ""
    arch: str = ""
    cpu_count: int = 0
    cpu_model: str = ""
    memory_total_mb: int = 0
    memory_available_mb: int = 0
    disk_total_gb: float = 0.0
    disk_available_gb: float = 0.0
    collected_at: dt.datetime | None = None
    # Redacted raw command output, for debugging. Never secrets.
    raw: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# argv: exactly what runs. No substitutions, no shell metacharacters.
# --------------------------------------------------------------------------
_OS_RELEASE_ARGV = ("cat", "/etc/os-release")
_UNAME_ARGV = ("uname", "-srm")
_NPROC_ARGV = ("nproc",)
_MEMINFO_ARGV = ("cat", "/proc/meminfo")
# grep's pattern is an ordinary argv element — no shell, no ``sh -c``.
_CPU_MODEL_ARGV = ("grep", "-m1", "model name", "/proc/cpuinfo")
_DISK_ARGV = ("df", "-P", "-B1", "/")

# Commands that inspect the system and must never modify it.
SAFE_READ_ONLY = (
    _OS_RELEASE_ARGV,
    _UNAME_ARGV,
    _NPROC_ARGV,
    _MEMINFO_ARGV,
    _CPU_MODEL_ARGV,
    _DISK_ARGV,
)


def _run(port: ExecPort, ctx: ExecContext, argv: tuple[str, ...]) -> ExecResult:
    return port.run(ctx, list(argv))


def _parse_os_release(text: str) -> dict[str, str]:
    """Parse ``/etc/os-release`` without a shell.

    Format is ``KEY=VALUE`` per line, value optionally quoted. No nesting, no
    expansion — a plain line parser is both sufficient and safer than anything
    that evaluates.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def _parse_meminfo(text: str) -> tuple[int, int]:
    """Return (total_mb, available_mb) from ``/proc/meminfo``."""
    total_kb = 0
    available_kb = 0
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total_kb = _first_int(line)
        elif line.startswith("MemAvailable:"):
            available_kb = _first_int(line)
    return total_kb // 1024, available_kb // 1024


def _first_int(text: str) -> int:
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else 0


def _parse_disk(text: str) -> tuple[float, float]:
    """Return (total_gb, available_gb) from ``df -P -B1 /``."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return 0.0, 0.0
    # Filesystem 1024-blocks Used Available Capacity Mounted
    parts = lines[-1].split()
    if len(parts) < 4:
        return 0.0, 0.0
    try:
        total = int(parts[1]) / (1024**3)
        available = int(parts[3]) / (1024**3)
    except ValueError:
        return 0.0, 0.0
    return round(total, 2), round(available, 2)


def collect_facts(port: ExecPort, ctx: ExecContext) -> SystemFacts:
    """Run the read-only checks and parse the result.

    Raises :class:`CheckError` if the essential pieces are missing. Partial
    results are kept — a host whose ``/proc/cpuinfo`` is odd should still get a
    facts row for the rest.
    """
    os_release_raw = _run(port, ctx, _OS_RELEASE_ARGV)
    uname_raw = _run(port, ctx, _UNAME_ARGV)
    nproc_raw = _run(port, ctx, _NPROC_ARGV)
    meminfo_raw = _run(port, ctx, _MEMINFO_ARGV)
    cpu_model_raw = _run(port, ctx, _CPU_MODEL_ARGV)
    disk_raw = _run(port, ctx, _DISK_ARGV)

    if not os_release_raw.ok and not uname_raw.ok:
        raise CheckError(
            "could not read OS information",
            details={
                "os_release_exit": os_release_raw.exit_code,
                "uname_exit": uname_raw.exit_code,
            },
        )

    release = _parse_os_release(os_release_raw.stdout)
    uname_parts = uname_raw.stdout.strip().split()
    kernel = uname_parts[0] if uname_parts else ""
    arch = uname_parts[-1] if len(uname_parts) >= 3 else ""

    mem_total, mem_available = _parse_meminfo(meminfo_raw.stdout)
    disk_total, disk_available = _parse_disk(disk_raw.stdout)

    model_match = re.search(r"model name\s*:\s*(.+)", cpu_model_raw.stdout)
    cpu_model = model_match.group(1).strip() if model_match else ""

    facts = SystemFacts(
        os_name=release.get("ID", "") or release.get("NAME", ""),
        os_version=release.get("VERSION_ID", "") or release.get("VERSION", ""),
        kernel=kernel,
        arch=arch,
        cpu_count=_first_int(nproc_raw.stdout),
        cpu_model=cpu_model,
        memory_total_mb=mem_total,
        memory_available_mb=mem_available,
        disk_total_gb=disk_total,
        disk_available_gb=disk_available,
        raw={
            # Only the parsed source text, already free of secrets by
            # construction (these files hold no credentials).
            "os_release": os_release_raw.stdout[:4000],
            "uname": uname_raw.stdout[:500],
            "nproc": nproc_raw.stdout[:100],
            "meminfo": meminfo_raw.stdout[:2000],
            "disk": disk_raw.stdout[:1000],
        },
    )
    log.info(
        "inventory.facts_collected",
        host=ctx.host,
        os=f"{facts.os_name}-{facts.os_version}",
        arch=facts.arch,
        cpu_count=facts.cpu_count,
    )
    return facts


def facts_to_json(facts: SystemFacts) -> dict[str, Any]:
    """Serialise for the ``raw_json`` column and for API responses."""
    return {
        "os_name": facts.os_name,
        "os_version": facts.os_version,
        "kernel": facts.kernel,
        "arch": facts.arch,
        "cpu_count": facts.cpu_count,
        "cpu_model": facts.cpu_model,
        "memory_total_mb": facts.memory_total_mb,
        "memory_available_mb": facts.memory_available_mb,
        "disk_total_gb": facts.disk_total_gb,
        "disk_available_gb": facts.disk_available_gb,
        "raw": facts.raw,
    }


def dump_safe(argv: tuple[str, ...]) -> str:
    """JSON form of an argv, for audit snapshots. Args only — no shell string."""
    return json.dumps(list(argv), ensure_ascii=False)
