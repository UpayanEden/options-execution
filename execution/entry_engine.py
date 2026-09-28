from __future__ import annotations
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from brokers.paper import PaperBroker
from execution.fill_tracker import FillTracker
from execution.models import (
    LegConfig, Order, OrderStatus, ResolvedInstrument, RiskConfig,
    Side, StrategyConfig, StrategyState,
)
from execution.order_manager import OrderManager
from execution.quote_engine import QuoteEngine
from execution.state_machine import StateMachine
from monitoring.alerts import AlertManager
from execution.models import AlertLevel
from risk.kill_switch import KillSwitch, KillSwitchError
from risk.validator import PreTradeValidator

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


class EntryEngine:
    """
    Orchestrates hedge-first iron condor entry.

    Order of execution:
    1. Validate all instruments are resolved.
    2. Fetch quotes and validate basket.
    3. Check pre-trade risk limits.
    4. Submit BUY (hedge) legs first; wait for fills.
    5. If both hedges filled, submit SELL (short) legs.
    6. If shorts fail, cancel them and close hedges (no naked shorts).
    7. Transition state machine through the full entry sequence.
    """

    def __init__(
        self,
        strategy: StrategyConfig,
        resolved: dict[int, ResolvedInstrument],   # leg_index -> ResolvedInstrument
        broker: PaperBroker,
        risk: RiskConfig,
        kill_switch: KillSwitch,
        alert_manager: AlertManager,
        state_machine: StateMachine,
        order_manager: Optional[OrderManager] = None,
        order_repo=None,
        fill_repo=None,
        strategy_repo=None,
        strategy_run_id: Optional[str] = None,
    ) -> None:
        self._strategy = strategy
        self._resolved = resolved
        self._broker = broker
        self._risk = risk
        self._ks = kill_switch
        self._alerts = alert_manager
        self._sm = state_machine
        self._run_id = strategy_run_id or str(uuid.uuid4())
        self._quote_engine = QuoteEngine(broker)
        self._order_manager = order_manager or OrderManager(
            broker, risk, kill_switch, order_repo, fill_repo
        )
        self._order_repo = order_repo
        self._fill_repo = fill_repo
        self._strategy_repo = strategy_repo

    @property
    def run_id(self) -> str:
        return self._run_id

    def _instruments_list(self) -> list[tuple[int, str]]:
        return [(r.instrument_token, r.trading_symbol) for r in self._resolved.values()]

    def _resolved_symbols(self) -> dict[int, str]:
        return {i: r.trading_symbol for i, r in self._resolved.items()}

    def _lot_size(self) -> int:
        return next(iter(self._resolved.values())).lot_size

    def _make_order(self, leg_index: int, quote_now=None) -> Order:
        leg = self._strategy.legs[leg_index]
        inst = self._resolved[leg_index]
        quantity = inst.lot_size * self._strategy.lots
        # Initial limit price will be set by OrderManager on execution
        return Order(
            strategy_run_id=self._run_id,
            leg_index=leg_index,
            trading_symbol=inst.trading_symbol,
            instrument_token=inst.instrument_token,
            side=leg.side,
            quantity=quantity,
            limit_price=Decimal("0.05"),  # placeholder; OrderManager sets real price
        )

    def _quote_fn(self, leg_index: int):
        """Returns a callable that fetches a fresh quote for this leg."""
        inst = self._resolved[leg_index]
        def fetch():
            quotes = self._quote_engine.fetch_all([(inst.instrument_token, inst.trading_symbol)])
            return quotes[inst.trading_symbol]
        return fetch

    def run(self) -> FillTracker:
        """
        Execute full hedge-first entry. Returns FillTracker with results.
        Caller must check fill_tracker.all_filled to determine success.
        """
        legs = self._strategy.legs
        hedge_indices = [i for i, l in enumerate(legs) if l.side == Side.BUY]
        short_indices = [i for i, l in enumerate(legs) if l.side == Side.SELL]
        sides = {i: l.side for i, l in enumerate(legs)}
        tracker = FillTracker(len(legs))

        try:
            self._sm.transition(StrategyState.VALIDATING, "Starting entry validation")

            # Phase 1: fetch quotes and validate
            self._sm.transition(StrategyState.WAITING_FOR_QUOTES)
            quotes = self._quote_engine.fetch_all(self._instruments_list())
            errors = self._quote_engine.validate_basket(quotes, self._risk)
            if errors:
                for e in errors:
                    self._alerts.warning("quote_validation_failed", e, self._run_id)
                self._sm.transition(StrategyState.ABORTED, "Quote validation failed")
                return tracker

            # Credit check
            credit = self._quote_engine.calculate_executable_basket_credit(
                legs, quotes, self._lot_size(), self._strategy.lots,
                self._resolved_symbols(),
            )
            if credit.net_credit < self._risk.min_net_credit:
                self._alerts.warning(
                    "credit_below_minimum",
                    f"Net credit {credit.net_credit} < minimum {self._risk.min_net_credit}",
                    self._run_id,
                )
                self._sm.transition(StrategyState.ABORTED, "Net credit below minimum")
                return tracker

            # Pre-trade validation (time window, spreads, max loss, etc.)
            from risk.payoff import calculate_payoff_from_credit
            payoff = calculate_payoff_from_credit(
                self._strategy, credit.net_credit, self._lot_size()
            )
            ptv_report = PreTradeValidator(self._risk).validate(
                self._strategy, quotes, payoff, estimated_margin=None
            )
            if not ptv_report.passed:
                for failure in ptv_report.failures:
                    self._alerts.warning(
                        "pretrade_check_failed",
                        f"{failure.name}: {failure.message}",
                        self._run_id,
                    )
                self._sm.transition(StrategyState.ABORTED, "Pre-trade validation failed")
                return tracker

            self._sm.transition(StrategyState.READY_TO_ENTER)
            self._alerts.info("strategy_ready", f"Net credit {credit.net_credit} pts", self._run_id)
            self._sm.transition(StrategyState.ENTRY_SUBMITTED)

            # Phase 2: fill hedge legs first
            for leg_index in hedge_indices:
                self._ks.check()
                order = self._make_order(leg_index)
                updated, fill = self._order_manager.execute_with_adaptive_limit(
                    order, self._quote_fn(leg_index), tick=self._lot_size_tick()
                )
                if fill:
                    tracker.record_fill(leg_index, updated, fill)
                    self._alerts.info(
                        "hedge_filled",
                        f"Leg {leg_index} {updated.trading_symbol} filled @ {fill.price}",
                        self._run_id,
                    )
                else:
                    tracker.record_unfilled(leg_index, updated)
                    self._alerts.critical(
                        "hedge_fill_failed",
                        f"Leg {leg_index} {updated.trading_symbol} did not fill — aborting",
                        self._run_id,
                    )
                    self._sm.transition(StrategyState.ABORTED, f"Hedge leg {leg_index} not filled")
                    return tracker

            self._sm.transition(StrategyState.HEDGES_FILLED)
            self._alerts.info("hedges_complete", "All hedge legs filled", self._run_id)

            # Phase 3: fill short legs
            short_failures = []
            for leg_index in short_indices:
                self._ks.check()
                order = self._make_order(leg_index)
                updated, fill = self._order_manager.execute_with_adaptive_limit(
                    order, self._quote_fn(leg_index), tick=self._lot_size_tick()
                )
                if fill:
                    tracker.record_fill(leg_index, updated, fill)
                    self._alerts.info(
                        "short_filled",
                        f"Leg {leg_index} {updated.trading_symbol} filled @ {fill.price}",
                        self._run_id,
                    )
                else:
                    tracker.record_unfilled(leg_index, updated)
                    short_failures.append(leg_index)
                    self._alerts.critical(
                        "short_fill_failed",
                        f"Leg {leg_index} {updated.trading_symbol} did not fill",
                        self._run_id,
                    )

            if short_failures:
                # Short legs failed — close already-filled shorts and hedges to avoid naked positions
                self._alerts.emergency(
                    "naked_exposure_risk",
                    f"Short legs {short_failures} failed. Closing all filled legs to prevent naked exposure.",
                    self._run_id,
                )
                self._close_filled_legs(tracker, sides, hedge_indices, short_indices)
                self._sm.transition(StrategyState.ABORTED, "Short legs failed — all positions closed")
                return tracker

            self._sm.transition(StrategyState.SHORTS_FILLED)

            # Compute actual net credit
            actual_credit = tracker.actual_net_credit(sides)
            self._sm.transition(StrategyState.ENTRY_COMPLETE)
            self._alerts.info(
                "entry_complete",
                f"All legs filled. Actual net credit: {actual_credit} pts/share",
                self._run_id,
            )
            if self._strategy_repo:
                self._strategy_repo.update_state(self._run_id, StrategyState.ENTRY_COMPLETE)

            return tracker

        except KillSwitchError:
            self._alerts.emergency("kill_switch", "Kill switch activated during entry", self._run_id)
            self._sm.force_abort("Kill switch")
            return tracker
        except Exception as exc:
            logger.exception("entry_engine_error")
            self._alerts.critical("entry_error", str(exc), self._run_id)
            self._sm.force_error(str(exc))
            return tracker

    def _lot_size_tick(self) -> Decimal:
        return self._resolved[0].tick_size

    def _close_filled_legs(
        self,
        tracker: FillTracker,
        sides: dict[int, Side],
        hedge_indices: list[int],
        short_indices: list[int],
    ) -> None:
        """Close any filled legs to prevent naked exposure. Best-effort — log failures."""
        for leg_index in tracker.filled_legs:
            original_side = sides[leg_index]
            # Reverse the side to close
            close_side = Side.SELL if original_side == Side.BUY else Side.BUY
            inst = self._resolved[leg_index]
            quantity = inst.lot_size * self._strategy.lots
            close_order = Order(
                strategy_run_id=self._run_id,
                leg_index=leg_index,
                trading_symbol=inst.trading_symbol,
                instrument_token=inst.instrument_token,
                side=close_side,
                quantity=quantity,
                limit_price=Decimal("0.05"),
                notes="Emergency close after partial entry failure",
            )
            try:
                _, fill = self._order_manager.execute_with_adaptive_limit(
                    close_order, self._quote_fn(leg_index)
                )
                if fill:
                    self._alerts.info(
                        "emergency_close_filled",
                        f"Leg {leg_index} emergency close filled @ {fill.price}",
                        self._run_id,
                    )
                else:
                    self._alerts.emergency(
                        "emergency_close_failed",
                        f"URGENT: Leg {leg_index} emergency close DID NOT fill. Manual intervention required.",
                        self._run_id,
                    )
            except Exception as exc:
                self._alerts.emergency(
                    "emergency_close_error",
                    f"URGENT: Exception during emergency close of leg {leg_index}: {exc}",
                    self._run_id,
                )
