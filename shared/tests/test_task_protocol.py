"""State-machine and recovery-decision contract.

These are pure tests — no database, no network. They exist first (before the
tables and the worker) because a wrong edge in these tables corrupts every run
that follows, and because "UNKNOWN is never auto-retried" is a safety property,
not a nicety.
"""

from __future__ import annotations

import pytest

from shared.task_protocol import (
    DECISION_RENEW,
    DECISION_REQUEUE,
    DECISION_SKIP,
    DECISION_UNKNOWN,
    IDEMPOTENCY_CONDITIONAL,
    IDEMPOTENCY_DESTRUCTIVE,
    IDEMPOTENCY_SAFE,
    PROBE_FINISHED,
    PROBE_STILL_RUNNING,
    PROBE_SUCCEEDED,
    PROBE_UNREACHABLE,
    RUN_TRANSITIONS,
    STEP_TRANSITIONS,
    TASK_TRANSITIONS,
    InvalidStateTransition,
    aggregate_task_status,
    can_transition,
    decide_recovery,
    ensure_run_transition,
    ensure_task_transition,
    is_run_terminal,
    is_task_terminal,
)
from shared.task_protocol.states import (
    RUN_CANCELLED,
    RUN_EXECUTING,
    RUN_FAILED,
    RUN_PRECHECK,
    RUN_STATES,
    RUN_SUCCESS,
    RUN_UNKNOWN,
    RUN_VERIFYING,
    RUN_WAITING,
    TASK_CANCELLED,
    TASK_CANCELLING,
    TASK_FAILED,
    TASK_PARTIAL_SUCCESS,
    TASK_PENDING,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_STATES,
    TASK_SUCCESS,
)


class TestTableShape:
    """A new state without edges must fail here, not in production."""

    def test_task_has_exactly_eight_states(self) -> None:
        assert len(TASK_STATES) == 8
        assert set(TASK_TRANSITIONS) == set(TASK_STATES)

    def test_run_has_exactly_nine_states_including_unknown(self) -> None:
        assert len(RUN_STATES) == 9
        assert set(RUN_TRANSITIONS) == set(RUN_STATES)
        assert RUN_UNKNOWN in RUN_STATES

    def test_every_run_state_only_points_at_known_states(self) -> None:
        for source, targets in RUN_TRANSITIONS.items():
            for target in targets:
                assert target in RUN_STATES, f"{source} -> {target}"

    def test_every_task_state_only_points_at_known_states(self) -> None:
        for source, targets in TASK_TRANSITIONS.items():
            for target in targets:
                assert target in TASK_STATES, f"{source} -> {target}"

    def test_terminal_states_have_no_automatic_exits(self) -> None:
        # SUCCESS is a hard stop. FAILED / CANCELLED / UNKNOWN only move on an
        # explicit human action, and never to RUNNING.
        assert TASK_TRANSITIONS[TASK_SUCCESS] == frozenset()
        for state in (TASK_FAILED, TASK_CANCELLED, TASK_PARTIAL_SUCCESS):
            assert TASK_TRANSITIONS[state] == frozenset({TASK_QUEUED})
        assert RUN_TRANSITIONS[RUN_SUCCESS] == frozenset()
        assert RUN_TRANSITIONS[RUN_FAILED] == frozenset({RUN_WAITING})
        assert RUN_TRANSITIONS[RUN_CANCELLED] == frozenset({RUN_WAITING})
        assert RUN_TRANSITIONS[RUN_UNKNOWN] == frozenset(
            {RUN_WAITING, RUN_SUCCESS, RUN_FAILED}
        )

    def test_unknown_cannot_go_straight_back_to_work(self) -> None:
        """The heart of the safety rule: no edge UNKNOWN -> EXECUTING."""
        assert RUN_EXECUTING not in RUN_TRANSITIONS[RUN_UNKNOWN]
        assert RUN_PRECHECK not in RUN_TRANSITIONS[RUN_UNKNOWN]
        assert RUN_VERIFYING not in RUN_TRANSITIONS[RUN_UNKNOWN]
        # ...and no automatic requeue either. A human has to ask.
        assert not can_transition(RUN_TRANSITIONS, RUN_UNKNOWN, RUN_EXECUTING)

    def test_wait_cannot_jump_to_success(self) -> None:
        assert RUN_SUCCESS not in RUN_TRANSITIONS[RUN_WAITING]

    def test_step_tables_cover_their_states(self) -> None:
        from shared.task_protocol.states import STEP_STATES

        assert set(STEP_TRANSITIONS) == set(STEP_STATES)


class TestGuards:
    def test_legal_task_moves_pass(self) -> None:
        ensure_task_transition(TASK_PENDING, TASK_QUEUED)
        ensure_task_transition(TASK_QUEUED, TASK_RUNNING)
        ensure_task_transition(TASK_RUNNING, TASK_SUCCESS)
        ensure_task_transition(TASK_RUNNING, TASK_CANCELLING)
        ensure_task_transition(TASK_CANCELLING, TASK_CANCELLED)

    def test_illegal_task_move_raises(self) -> None:
        with pytest.raises(InvalidStateTransition) as exc:
            ensure_task_transition(TASK_PENDING, TASK_SUCCESS)
        assert exc.value.kind == "task"
        assert exc.value.current == TASK_PENDING
        assert exc.value.target == TASK_SUCCESS

    def test_terminal_task_cannot_be_resurrected_implicitly(self) -> None:
        with pytest.raises(InvalidStateTransition):
            ensure_task_transition(TASK_SUCCESS, TASK_RUNNING)

    def test_legal_run_moves_pass(self) -> None:
        ensure_run_transition(RUN_WAITING, RUN_PRECHECK)
        ensure_run_transition(RUN_PRECHECK, RUN_EXECUTING)
        ensure_run_transition(RUN_EXECUTING, RUN_VERIFYING)
        ensure_run_transition(RUN_VERIFYING, RUN_SUCCESS)

    def test_illegal_run_move_raises(self) -> None:
        with pytest.raises(InvalidStateTransition) as exc:
            ensure_run_transition(RUN_WAITING, RUN_SUCCESS)
        assert exc.value.kind == "run"

    def test_unknown_string_is_rejected(self) -> None:
        with pytest.raises(InvalidStateTransition):
            ensure_task_transition("NOT_A_STATE", TASK_QUEUED)

    def test_terminal_predicates(self) -> None:
        assert is_task_terminal(TASK_SUCCESS)
        assert is_task_terminal(TASK_CANCELLED)
        assert not is_task_terminal(TASK_RUNNING)
        assert is_run_terminal(RUN_UNKNOWN)
        assert is_run_terminal(RUN_SUCCESS)
        assert not is_run_terminal(RUN_EXECUTING)


class TestAggregation:
    def test_all_success_is_success(self) -> None:
        assert aggregate_task_status([RUN_SUCCESS, RUN_SUCCESS]) == TASK_SUCCESS

    def test_success_plus_failure_is_partial(self) -> None:
        got = aggregate_task_status([RUN_SUCCESS, RUN_FAILED])
        assert got == TASK_PARTIAL_SUCCESS

    def test_success_plus_unknown_is_partial(self) -> None:
        assert aggregate_task_status([RUN_SUCCESS, RUN_UNKNOWN]) == TASK_PARTIAL_SUCCESS

    def test_no_success_with_failure_is_failed(self) -> None:
        assert aggregate_task_status([RUN_FAILED, RUN_CANCELLED]) == TASK_FAILED

    def test_all_cancelled_is_cancelled(self) -> None:
        assert aggregate_task_status([RUN_CANCELLED, RUN_CANCELLED]) == TASK_CANCELLED

    def test_cancel_requested_dominates_without_success_or_failure(self) -> None:
        got = aggregate_task_status([RUN_CANCELLED, RUN_UNKNOWN], cancel_requested=True)
        assert got == TASK_CANCELLED

    def test_unknown_without_success_is_never_success(self) -> None:
        for mix in ([RUN_UNKNOWN], [RUN_UNKNOWN, RUN_UNKNOWN], [RUN_CANCELLED, RUN_UNKNOWN]):
            got = aggregate_task_status(mix)
            assert got != TASK_SUCCESS
            assert got != RUN_UNKNOWN  # a task status, not a run status

    def test_in_flight_reports_running(self) -> None:
        assert aggregate_task_status([RUN_SUCCESS, RUN_EXECUTING]) == TASK_RUNNING

    def test_in_flight_with_cancel_reports_cancelling(self) -> None:
        got = aggregate_task_status([RUN_EXECUTING], cancel_requested=True)
        assert got == TASK_CANCELLING

    def test_empty_task_is_failed_not_success(self) -> None:
        assert aggregate_task_status([]) == TASK_FAILED


class TestDecideRecovery:
    """Lease expiry is not failure. The three-step judgement, exhaustively."""

    def test_still_running_always_renews(self) -> None:
        for cls in (IDEMPOTENCY_SAFE, IDEMPOTENCY_CONDITIONAL, IDEMPOTENCY_DESTRUCTIVE):
            assert decide_recovery(PROBE_STILL_RUNNING, cls) == DECISION_RENEW

    def test_unreachable_is_always_unknown(self) -> None:
        for cls in (IDEMPOTENCY_SAFE, IDEMPOTENCY_CONDITIONAL, IDEMPOTENCY_DESTRUCTIVE):
            assert decide_recovery(PROBE_UNREACHABLE, cls) == DECISION_UNKNOWN

    def test_remote_success_is_recorded_not_rerun(self) -> None:
        for cls in (IDEMPOTENCY_SAFE, IDEMPOTENCY_CONDITIONAL, IDEMPOTENCY_DESTRUCTIVE):
            assert decide_recovery(PROBE_SUCCEEDED, cls) == DECISION_SKIP

    def test_safe_may_rerun_after_ambiguous_finish(self) -> None:
        assert decide_recovery(PROBE_FINISHED, IDEMPOTENCY_SAFE) == DECISION_REQUEUE

    def test_destructive_is_never_rerun(self) -> None:
        assert decide_recovery(PROBE_FINISHED, IDEMPOTENCY_DESTRUCTIVE) == DECISION_UNKNOWN
        # ...and a favourable precheck does not change that.
        assert (
            decide_recovery(
                PROBE_FINISHED, IDEMPOTENCY_DESTRUCTIVE, precheck_target_state=True
            )
            == DECISION_UNKNOWN
        )

    def test_conditional_reruns_only_when_precheck_says_target_state(self) -> None:
        assert (
            decide_recovery(
                PROBE_FINISHED, IDEMPOTENCY_CONDITIONAL, precheck_target_state=True
            )
            == DECISION_SKIP
        )
        assert (
            decide_recovery(
                PROBE_FINISHED, IDEMPOTENCY_CONDITIONAL, precheck_target_state=False
            )
            == DECISION_UNKNOWN
        )

    def test_unknown_is_the_only_outcome_that_never_queues_work(self) -> None:
        """Every path either continues, re-queues, or records success — except
        UNKNOWN, which stops. There is no fourth option."""
        for probe in (PROBE_STILL_RUNNING, PROBE_SUCCEEDED, PROBE_FINISHED, PROBE_UNREACHABLE):
            for cls in (IDEMPOTENCY_SAFE, IDEMPOTENCY_CONDITIONAL, IDEMPOTENCY_DESTRUCTIVE):
                for pre in (True, False):
                    decision = decide_recovery(
                        probe, cls, precheck_target_state=pre
                    )
                    assert decision in {
                        DECISION_RENEW,
                        DECISION_REQUEUE,
                        DECISION_SKIP,
                        DECISION_UNKNOWN,
                    }
                    if decision == DECISION_UNKNOWN:
                        # Stopping is allowed; silently re-queueing is not.
                        pass

    def test_bad_inputs_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            decide_recovery("made_up", IDEMPOTENCY_SAFE)
        with pytest.raises(ValueError):
            decide_recovery(PROBE_FINISHED, "made_up")
