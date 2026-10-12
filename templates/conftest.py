"""Pytest fixtures for the per-template suites.

The reusable pieces live in :mod:`templates.harness`; this module only wires
them up as fixtures and points them at the template a test file belongs to.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.actions.base import register_all  # noqa: E402
from shared.templates import LoadedTemplate, load_template  # noqa: E402


@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    register_all()
    yield


@pytest.fixture()
def template_dir(request: pytest.FixtureRequest) -> Path:
    """The template directory the calling test module belongs to.

    Tests live at ``templates/<category>/<name>/tests/test_*.py``, so the
    template root is two levels up from the test file.
    """
    return Path(request.node.fspath).resolve().parent.parent


@pytest.fixture()
def loaded(template_dir: Path) -> LoadedTemplate:
    return load_template(template_dir)
