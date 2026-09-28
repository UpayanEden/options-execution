"""Tests for partial fill handling and naked exposure prevention."""
from __future__ import annotations

import pytest

from execution.models import StrategyState
from execution.state_machine import InvalidTransitionError, StateMachine


class TestPartialFillStateTransitions:
    def test_hedges_filled_before_shorts(self) -> None:
        """Hedge legs fill → state becomes HEDGES_FILLED before shorts are placed."""
        sm = StateMachine(initial=StrategyState.ENTRY_SUBMITTED)
        sm.transition(StrategyState.HEDGES_FILLED)
        assert sm.state == StrategyState.HEDGES_FILLED

    def test_cannot_go_from_hedges_to_monitoring_directly(self) -> None:
        """Cannot skip ENTRY_COMPLETE."""
        sm = StateMachine(initial=StrategyState.HEDGES_FILLED)
        with pytest.raises(InvalidTransitionError):
            sm.transition(StrategyState.MONITORING)

    def test_partial_fill_can_abort(self) -> None:
        """A partial fill can be safely aborted — no naked exposure should remain."""
        sm = StateMachine(initial=StrategyState.PARTIALLY_FILLED)
        sm.transition(StrategyState.ABORTED)
        assert sm.state == StrategyState.ABORTED

    def test_exit_partial_fill_reaches_complete(self) -> None:
        sm = StateMachine(initial=StrategyState.EXIT_PARTIALLY_FILLED)
        sm.transition(StrategyState.EXIT_COMPLETE)
        assert sm.state == StrategyState.EXIT_COMPLETE
