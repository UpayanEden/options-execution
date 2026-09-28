"""
Strategy state machine.
Defines allowed transitions and guard conditions.
Transitions are validated before being applied.
"""
from __future__ import annotations

import logging
from typing import Callable

from execution.models import StrategyState

logger = logging.getLogger(__name__)

# Allowed (from_state, to_state) transitions
ALLOWED_TRANSITIONS: set[tuple[StrategyState, StrategyState]] = {
    (StrategyState.CREATED, StrategyState.VALIDATING),
    (StrategyState.CREATED, StrategyState.ERROR),
    (StrategyState.VALIDATING, StrategyState.WAITING_FOR_QUOTES),
    (StrategyState.VALIDATING, StrategyState.ABORTED),
    (StrategyState.VALIDATING, StrategyState.ERROR),
    (StrategyState.WAITING_FOR_QUOTES, StrategyState.READY_TO_ENTER),
    (StrategyState.WAITING_FOR_QUOTES, StrategyState.ABORTED),
    (StrategyState.WAITING_FOR_QUOTES, StrategyState.ERROR),
    (StrategyState.READY_TO_ENTER, StrategyState.ENTRY_SUBMITTED),
    (StrategyState.READY_TO_ENTER, StrategyState.WAITING_FOR_QUOTES),
    (StrategyState.READY_TO_ENTER, StrategyState.ABORTED),
    (StrategyState.ENTRY_SUBMITTED, StrategyState.PARTIALLY_FILLED),
    (StrategyState.ENTRY_SUBMITTED, StrategyState.HEDGES_FILLED),
    (StrategyState.ENTRY_SUBMITTED, StrategyState.ENTRY_COMPLETE),
    (StrategyState.ENTRY_SUBMITTED, StrategyState.ABORTED),
    (StrategyState.ENTRY_SUBMITTED, StrategyState.ERROR),
    (StrategyState.PARTIALLY_FILLED, StrategyState.HEDGES_FILLED),
    (StrategyState.PARTIALLY_FILLED, StrategyState.SHORTS_FILLED),
    (StrategyState.PARTIALLY_FILLED, StrategyState.ENTRY_COMPLETE),
    (StrategyState.PARTIALLY_FILLED, StrategyState.ABORTED),
    (StrategyState.PARTIALLY_FILLED, StrategyState.ERROR),
    (StrategyState.HEDGES_FILLED, StrategyState.SHORTS_FILLED),
    (StrategyState.HEDGES_FILLED, StrategyState.ENTRY_COMPLETE),
    (StrategyState.HEDGES_FILLED, StrategyState.ABORTED),
    (StrategyState.HEDGES_FILLED, StrategyState.ERROR),
    (StrategyState.SHORTS_FILLED, StrategyState.ENTRY_COMPLETE),
    (StrategyState.SHORTS_FILLED, StrategyState.ABORTED),
    (StrategyState.SHORTS_FILLED, StrategyState.ERROR),
    (StrategyState.ENTRY_COMPLETE, StrategyState.MONITORING),
    (StrategyState.MONITORING, StrategyState.EXIT_TRIGGERED),
    (StrategyState.MONITORING, StrategyState.ERROR),
    (StrategyState.EXIT_TRIGGERED, StrategyState.EXIT_SUBMITTED),
    (StrategyState.EXIT_TRIGGERED, StrategyState.ERROR),
    (StrategyState.EXIT_SUBMITTED, StrategyState.EXIT_PARTIALLY_FILLED),
    (StrategyState.EXIT_SUBMITTED, StrategyState.EXIT_COMPLETE),
    (StrategyState.EXIT_SUBMITTED, StrategyState.ERROR),
    (StrategyState.EXIT_PARTIALLY_FILLED, StrategyState.EXIT_COMPLETE),
    (StrategyState.EXIT_PARTIALLY_FILLED, StrategyState.ERROR),
    # Emergency: any state → ABORTED or ERROR
    *{(s, StrategyState.ABORTED) for s in StrategyState},
    *{(s, StrategyState.ERROR) for s in StrategyState},
}


class InvalidTransitionError(Exception):
    pass


class StateMachine:
    def __init__(self, initial: StrategyState = StrategyState.CREATED) -> None:
        self._state = initial
        self._listeners: list[Callable[[StrategyState, StrategyState], None]] = []

    @property
    def state(self) -> StrategyState:
        return self._state

    def can_transition(self, to: StrategyState) -> bool:
        return (self._state, to) in ALLOWED_TRANSITIONS

    def transition(self, to: StrategyState, reason: str = "") -> None:
        if not self.can_transition(to):
            raise InvalidTransitionError(
                f"Cannot transition from {self._state} to {to}"
            )
        prev = self._state
        self._state = to
        logger.info(
            "state_transition",
            extra={"from": prev.value, "to": to.value, "reason": reason},
        )
        for listener in self._listeners:
            listener(prev, to)

    def add_listener(self, fn: Callable[[StrategyState, StrategyState], None]) -> None:
        self._listeners.append(fn)

    def force_error(self, reason: str = "") -> None:
        """Force into ERROR state regardless of current state (for unexpected exceptions)."""
        prev = self._state
        self._state = StrategyState.ERROR
        logger.error(
            "state_force_error",
            extra={"from": prev.value, "reason": reason},
        )

    def force_abort(self, reason: str = "") -> None:
        """Force into ABORTED state (kill switch, emergency exit)."""
        prev = self._state
        self._state = StrategyState.ABORTED
        logger.warning(
            "state_force_abort",
            extra={"from": prev.value, "reason": reason},
        )
