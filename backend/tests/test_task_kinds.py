"""The API and the worker must agree on what a task kind even is.

``TASK_KINDS`` is what the API accepts at submit; ``SUPPORTED_KINDS`` is what
the worker will execute. A kind present in only one of them is the worst
failure mode this engine has: the submit succeeds, the run is claimed, and the
work sits there forever because nobody handles it. So the two sets are pinned
equal here rather than left to a comment.
"""

from __future__ import annotations

from worker.runner import SUPPORTED_KINDS

from backend.app.orchestrator.service import TASK_KINDS


def test_the_api_and_the_worker_agree_on_task_kinds() -> None:
    assert TASK_KINDS == SUPPORTED_KINDS, (
        "update backend.app.orchestrator.service.TASK_KINDS and "
        "worker.runner.SUPPORTED_KINDS together; a kind in only one of them "
        "queues forever"
    )


def test_the_kinds_cover_the_work_the_product_can_do() -> None:
    """A new capability is a new kind, not a new meaning for an old one."""
    assert {
        "server.check",
        "app.deploy",
        "app.rollback",
        "template.apply",
    } == TASK_KINDS
