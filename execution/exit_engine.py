from __future__ import annotations
import logging
import uuid
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from brokers.paper import PaperBroker
from execution.fill_tracker import FillTracker
from execution.models import (
    ExitTrigger, LegPosition, Order, RiskConfig, Side,
    StrategyConfig, StrategyState,
)
from execution.order_manager import OrderManager
from execution.quote_engine import QuoteEngine
from execution.state_machine import StateMachine
from monitoring.alerts import AlertManager
from execution.models import AlertLevel
from risk.kill_switch import KillSwitch, KillSwitchError

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


class ExitEngine:
    """
    Controlled close of all strategy legs.

    Order:
    1. Buy back short legs (close naked risk first).
    2. Sell long hedge legs.
    3. Verify all positions reach zero.
    4. Log final P&L.
    """

    def __init__(
        self,
        strategy: StrategyConfig,
        positions: list[LegPosition],
        broker: PaperBroker,
        risk: RiskConfig,
        kill_switch: KillSwitch,
        alert_manager: AlertManager,
        state_machine: StateMachine,
        exit_trigger: ExitTrigger,
        order_manager: Optional[OrderManager] = None,
        order_repo=None,
        fill_repo=None,
        strategy_repo=None,
        strategy_run_id: Optional[str] = None,
    ) -> None:
        self._strategy = strategy
        self._positions = positions
        self._broker = broker
        self._risk = risk
        self._ks = kill_switch
        self._alerts = alert_manager
        self._sm = state_machine
        self._trigger = exit_trigger
        self._run_id = strategy_run_id or str(uuid.uuid4())
        self._quote_engine = QuoteEngine(broker)
        self._order_manager = order_manager or OrderManager(
            broker, risk, kill_switch, order_repo, fill_repo
        )
        self._strategy_repo = strategy_repo

    def _quote_fn(self, position: LegPosition):
        def fetch():
            quotes = self._quote_engine.fetch_all(
                [(position.instrument_token, position.trading_symbol)]
            )
            return quotes[position.trading_symbol]
        return fetch

    def run(self) -> FillTracker:
        """Execute full close. Returns FillTracker."""
        tracker = FillTracker(len(self._positions))
        sides = {p.leg_index: p.side for p in self._positions}

        try:
            self._sm.transition(StrategyState.EXIT_TRIGGERED, f"Exit trigger: {self._trigger.value}")
            self._alerts.info(
                "exit_initiated",
                f"Exit triggered: {self._trigger.value}",
                self._run_id,
            )
            self._sm.transition(StrategyState.EXIT_SUBMITTED)

            # Close shorts first (buy them back), then close longs
            shorts = [p for p in self._positions if p.side == Side.SELL]
            longs = [p for p in self._positions if p.side == Side.BUY]

            all_positions = shorts + longs
            failures = []

            for pos in all_positions:
                self._ks.check()
                close_side = Side.BUY if pos.side == Side.SELL else Side.SELL
                close_order = Order(
                    strategy_run_id=self._run_id,
                    leg_index=pos.leg_index,
                    trading_symbol=pos.trading_symbol,
                    instrument_token=pos.instrument_token,
                    side=close_side,
                    quantity=pos.quantity,
                    limit_price=Decimal("0.05"),
                    notes=f"Exit: {self._trigger.value}",
                )
                updated, fill = self._order_manager.execute_with_adaptive_limit(
                    close_order, self._quote_fn(pos)
                )
                if fill:
                    tracker.record_fill(pos.leg_index, updated, fill)
                    self._alerts.info(
                        "exit_leg_filled",
                        f"Exit leg {pos.leg_index} {pos.trading_symbol} filled @ {fill.price}",
                        self._run_id,
                    )
                else:
                    tracker.record_unfilled(pos.leg_index, updated)
                    failures.append(pos.leg_index)
                    self._alerts.critical(
                        "exit_leg_failed",
                        f"Exit leg {pos.leg_index} {pos.trading_symbol} did NOT fill",
                        self._run_id,
                    )

            if failures:
                self._sm.transition(StrategyState.EXIT_PARTIALLY_FILLED)
                self._alerts.emergency(
                    "exit_incomplete",
                    f"Exit incomplete. Unfilled legs: {failures}. Manual intervention required.",
                    self._run_id,
                )
            else:
                self._sm.transition(StrategyState.EXIT_COMPLETE)
                self._alerts.info(
                    "exit_complete",
                    "All exit legs filled. Position closed.",
                    self._run_id,
                )

            if self._strategy_repo:
                final_state = (
                    StrategyState.EXIT_COMPLETE if not failures
                    else StrategyState.EXIT_PARTIALLY_FILLED
                )
                self._strategy_repo.update_state(self._run_id, final_state)

            return tracker

        except KillSwitchError:
            self._alerts.emergency("kill_switch", "Kill switch during exit", self._run_id)
            self._sm.force_abort("Kill switch during exit")
            return tracker
        except Exception as exc:
            logger.exception("exit_engine_error")
            self._alerts.critical("exit_error", str(exc), self._run_id)
            self._sm.force_error(str(exc))
            return tracker
