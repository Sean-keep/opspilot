"""Worker package boundary.

The worker is a separate systemd unit: it must be able to start, run, and be
upgraded without the API package installed. That means **no** ``import
backend.app`` anywhere under ``worker/``.

This is not a style preference. If the worker imported the API, the two
processes could not be deployed independently and the executor would end up
with a circular dependency on the API's inventory service.
"""

from __future__ import annotations

import ast
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = WORKER_DIR.parent


def _python_files() -> list[Path]:
    """Production modules only — the tests themselves may import anything."""
    return sorted(p for p in WORKER_DIR.rglob("*.py") if "tests" not in p.parts)


def test_worker_package_has_no_backend_imports() -> None:
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "backend" or alias.name.startswith("backend."):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "backend" or node.module.startswith("backend.")
            ):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}: from {node.module} import ..."
                )
    assert offenders == [], "worker must not import backend.app:\n" + "\n".join(offenders)


def test_worker_only_imports_shared_and_itself() -> None:
    """Anything else would be a new coupling that needs justifying."""
    allowed_prefixes = ("shared", "worker", "sqlalchemy", "structlog")
    allowed_modules = {
        "collections",
        "contextlib",
        "dataclasses",
        "datetime",
        "hashlib",
        "json",
        "os",
        "pathlib",
        "signal",
        "socket",
        "sys",
        "tempfile",
        "time",
        "typing",
        "uuid",
        "__future__",
    }
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for mod in mods:
                top = mod.split(".")[0]
                if top in allowed_modules or mod.startswith(allowed_prefixes):
                    continue
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {mod}")
    assert offenders == [], "unexpected worker dependency:\n" + "\n".join(offenders)


def test_the_shims_are_shims_not_logic() -> None:
    """``backend.app.executor`` and friends must stay re-export-only.

    If real logic creeps back into the API copies, the worker and the API will
    drift and only one of them will be fixed.
    """
    for rel in (
        "backend/app/executor/port.py",
        "backend/app/executor/keys.py",
        "backend/app/executor/ssh.py",
        "backend/app/core/settings.py",
        "backend/app/core/logging.py",
        "backend/app/inventory/checks.py",
    ):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                raise AssertionError(f"{rel} defines {node.name}; shims must only re-export")
            if isinstance(node, ast.ClassDef) and node.name not in {"CheckError"}:
                raise AssertionError(f"{rel} defines class {node.name}; shims must only re-export")
