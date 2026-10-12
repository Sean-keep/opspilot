"""Task / Run / Step state vocabularies.

These are the **stored** strings. They are deliberately plain ASCII words in
``VARCHAR(32)`` columns: indexable, readable in a SQL console, and free of the
migration hell that MySQL ``ENUM`` brings when a state is added.

Adding a state means adding it here **and** to the transition tables in
:mod:`shared.task_protocol.transitions`. Nothing else in the codebase is
allowed to invent a status string.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Task — the user-visible unit of work ("install nginx on these 3 hosts")
# --------------------------------------------------------------------------
TASK_PENDING = "PENDING"
TASK_QUEUED = "QUEUED"
TASK_RUNNING = "RUNNING"
TASK_SUCCESS = "SUCCESS"
TASK_PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
TASK_FAILED = "FAILED"
TASK_CANCELLING = "CANCELLING"
TASK_CANCELLED = "CANCELLED"

TASK_STATES: frozenset[str] = frozenset(
    {
        TASK_PENDING,
        TASK_QUEUED,
        TASK_RUNNING,
        TASK_SUCCESS,
        TASK_PARTIAL_SUCCESS,
        TASK_FAILED,
        TASK_CANCELLING,
        TASK_CANCELLED,
    }
)

#: States from which nothing further happens without a human.
TASK_TERMINAL: frozenset[str] = frozenset(
    {
        TASK_SUCCESS,
        TASK_PARTIAL_SUCCESS,
        TASK_FAILED,
        TASK_CANCELLED,
    }
)

# --------------------------------------------------------------------------
# Run — one task on one server
# --------------------------------------------------------------------------
RUN_WAITING = "WAITING"
RUN_PRECHECK = "PRECHECK"
RUN_EXECUTING = "EXECUTING"
RUN_VERIFYING = "VERIFYING"
RUN_SUCCESS = "SUCCESS"
RUN_FAILED = "FAILED"
RUN_CANCELLING = "CANCELLING"
RUN_CANCELLED = "CANCELLED"
#: The remote outcome could not be determined. NEVER auto-retried.
RUN_UNKNOWN = "UNKNOWN"

RUN_STATES: frozenset[str] = frozenset(
    {
        RUN_WAITING,
        RUN_PRECHECK,
        RUN_EXECUTING,
        RUN_VERIFYING,
        RUN_SUCCESS,
        RUN_FAILED,
        RUN_CANCELLING,
        RUN_CANCELLED,
        RUN_UNKNOWN,
    }
)

RUN_TERMINAL: frozenset[str] = frozenset(
    {
        RUN_SUCCESS,
        RUN_FAILED,
        RUN_CANCELLED,
        RUN_UNKNOWN,
    }
)

#: Run states that count as "a worker is actively on this".
RUN_ACTIVE: frozenset[str] = frozenset(
    {
        RUN_PRECHECK,
        RUN_EXECUTING,
        RUN_VERIFYING,
        RUN_CANCELLING,
    }
)

# --------------------------------------------------------------------------
# Step — one action inside a run
# --------------------------------------------------------------------------
STEP_PENDING = "PENDING"
STEP_RUNNING = "RUNNING"
STEP_SUCCESS = "SUCCESS"
STEP_FAILED = "FAILED"
STEP_SKIPPED = "SKIPPED"
STEP_CANCELLED = "CANCELLED"
STEP_UNKNOWN = "UNKNOWN"

STEP_STATES: frozenset[str] = frozenset(
    {
        STEP_PENDING,
        STEP_RUNNING,
        STEP_SUCCESS,
        STEP_FAILED,
        STEP_SKIPPED,
        STEP_CANCELLED,
        STEP_UNKNOWN,
    }
)

STEP_TERMINAL: frozenset[str] = frozenset(
    {
        STEP_SUCCESS,
        STEP_FAILED,
        STEP_SKIPPED,
        STEP_CANCELLED,
        STEP_UNKNOWN,
    }
)

# --------------------------------------------------------------------------
# How a step is allowed to be re-run after an ambiguous outcome. Declared by
# every Action; the recovery path consults it and NEVER overrides it.
# --------------------------------------------------------------------------
IDEMPOTENCY_SAFE = "SAFE"
IDEMPOTENCY_CONDITIONAL = "CONDITIONAL"
IDEMPOTENCY_DESTRUCTIVE = "DESTRUCTIVE"

IDEMPOTENCY_CLASSES: frozenset[str] = frozenset(
    {
        IDEMPOTENCY_SAFE,
        IDEMPOTENCY_CONDITIONAL,
        IDEMPOTENCY_DESTRUCTIVE,
    }
)
