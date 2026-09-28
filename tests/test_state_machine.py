"""Tests for the strategy state machine."""
from __future__ import annotations

import pytest

from execution.models import StrategyState
from execution.state_machine import InvalidTransitionError, StateMachine


class TestStateMachineTransitions:
    def test_initial_state_is_created(self) -> None:
        sm = StateMachine()
        assert sm.state == StrategyState.CREATED

    def test_valid_transition_created_to_validating(self) -> None:
        sm = StateMachine()
        sm.transition(StrategyState.VALIDATING)
        assert sm.state == StrategyState.VALIDATING

    def test_invalid_transition_raises(self) -> None:
        sm = StateMachine()
        with pytest.raises(InvalidTransitionError):
            sm.transition(StrategyState.ENTRY_COMPLETE)

    def test_any_state_can_transition_to_aborted(self) -> None:
        for state in StrategyState:
            if state == StrategyState.ABORTED:
                continue
            sm = StateMachine(initial=state)
            sm.force_abort()
            assert sm.state == StrategyState.ABORTED

    def test_any_state_can_transition_to_error(self) -> None:
        for state in StrategyState:
            if state == StrategyState.ERROR:
                continue
            sm = StateMachine(initial=state)
            sm.force_error()
            assert sm.state == StrategyState.ERROR

    def test_listener_is_called_on_transition(self) -> None:
        sm = StateMachine()
        calls: list[tuple] = []
        sm.add_listener(lambda f, t: calls.append((f, t)))
        sm.transition(StrategyState.VALIDATING)
        assert calls == [(StrategyState.CREATED, StrategyState.VALIDATING)]

    def test_entry_flow(self) -> None:
        sm = StateMachine()
        flow = [
            StrategyState.VALIDATING,
            StrategyState.WAITING_FOR_QUOTES,
            StrategyState.READY_TO_ENTER,
            StrategyState.ENTRY_SUBMITTED,
            StrategyState.HEDGES_FILLED,
            StrategyState.ENTRY_COMPLETE,
            StrategyState.MONITORING,
        ]
        for state in flow:
            sm.transition(state)
        assert sm.state == StrategyState.MONITORING

    def test_exit_flow(self) -> None:
        sm = StateMachine(initial=StrategyState.MONITORING)
        sm.transition(StrategyState.EXIT_TRIGGERED)
        sm.transition(StrategyState.EXIT_SUBMITTED)
        sm.transition(StrategyState.EXIT_COMPLETE)
        assert sm.state == StrategyState.EXIT_COMPLETE
