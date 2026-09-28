from __future__ import annotations
import logging
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from execution.models import (
    AlertLevel, ExitTrigger, LegPosition, RiskConfig,
    StrategyPosition, StrategyState,
)
from execution.quote_engine import QuoteEngine
from execution.state_machine import StateMachine
from monitoring.alerts import AlertManager
from risk.kill_switch import KillSwitch, KillSwitchError
from risk.limits import RuntimeLimits
from risk.payoff import calculate_cost_to_close, calculate_mtm_pnl

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


class PositionMonitor:
    """
    Monitors an open iron condor position.
    Polls quotes at configured frequency, checks exit conditions, fires alerts.
    """

    def __init__(
        self,
        position: StrategyPosition,
        quote_engine: QuoteEngine,
        risk: RiskConfig,
        runtime_limits: RuntimeLimits,
        kill_switch: KillSwitch,
        alert_manager: AlertManager,
        state_machine: StateMachine,
        poll_interval_seconds: int = 60,
        exit_callback: Optional[Callable[[ExitTrigger], None]] = None,
        strategy_repo=None,
        pnl_repo=None,
    ) -> None:
        self._position = position
        self._qe = quote_engine
        self._risk = risk
        self._limits = runtime_limits
        self._ks = kill_switch
        self._alerts = alert_manager
        self._sm = state_machine
        self._poll_interval = poll_interval_seconds
        self._exit_callback = exit_callback
        self._strategy_repo = strategy_repo
        self._pnl_repo = pnl_repo

    def _instruments(self) -> list[tuple[int, str]]:
        return [(p.instrument_token, p.trading_symbol) for p in self._position.legs]

    def _symbol_to_leg(self) -> dict[str, LegPosition]:
        return {p.trading_symbol: p for p in self._position.legs}

    def _dte(self) -> int:
        today = date.today()
        expiry = self._position.expiry
        return max(0, (expiry - today).days)

    def monitor_loop(self, max_cycles: int = 0) -> None:
        """
        Run monitoring loop. If max_cycles > 0, stop after that many cycles.
        Otherwise runs until exit condition or kill switch.
        """
        if not self._sm.can_transition(StrategyState.MONITORING):
            logger.warning("Cannot enter MONITORING from current state")
            return

        self._sm.transition(StrategyState.MONITORING)
        self._alerts.info("monitoring_started", "Position monitoring active", self._position.strategy_run_id)

        cycle = 0
        while True:
            self._ks.check()
            cycle += 1

            try:
                quotes = self._qe.fetch_all(self._instruments())
                errors = self._qe.validate_basket(quotes, self._risk)

                if errors:
                    self._alerts.warning(
                        "quote_error_during_monitoring",
                        f"Quote errors: {errors}",
                        self._position.strategy_run_id,
                    )
                    if max_cycles and cycle >= max_cycles:
                        break
                    time.sleep(self._poll_interval)
                    continue

                # Update positions with current prices
                sym_map = self._symbol_to_leg()
                for sym, q in quotes.items():
                    if sym in sym_map:
                        leg = sym_map[sym]
                        # Update in-place (positions are mutable here)
                        leg.current_bid = q.bid
                        leg.current_ask = q.ask
                        leg.current_ltp = q.ltp

                # Calculate MTM
                try:
                    cost_to_close = calculate_cost_to_close(self._position.legs)
                    entry_credit = self._position.entry_credit or Decimal("0")
                    unrealized_pnl = calculate_mtm_pnl(entry_credit, cost_to_close)
                    self._position.cost_to_close = cost_to_close
                    self._position.unrealized_pnl = unrealized_pnl
                    self._position.last_updated = datetime.now(IST)
                except Exception as e:
                    logger.warning(f"MTM calculation error: {e}")
                    unrealized_pnl = None
                    cost_to_close = None

                dte = self._dte()
                self._log_monitor_snapshot(quotes, unrealized_pnl, cost_to_close, dte)

                # Check exit conditions
                exit_trigger = self._limits.check_exit_conditions(
                    position=self._position,
                    dte=dte,
                    quotes=quotes,
                )
                if exit_trigger:
                    self._alerts.warning(
                        "exit_condition_triggered",
                        f"Exit trigger: {exit_trigger.value}",
                        self._position.strategy_run_id,
                    )
                    if self._exit_callback:
                        self._exit_callback(exit_trigger)
                    break

            except KillSwitchError:
                self._alerts.emergency("kill_switch_monitoring", "Kill switch during monitoring",
                                       self._position.strategy_run_id)
                self._sm.force_abort("Kill switch")
                break
            except Exception as exc:
                logger.exception("monitor_loop_error")
                self._alerts.critical("monitor_error", str(exc), self._position.strategy_run_id)

            if max_cycles and cycle >= max_cycles:
                break

            time.sleep(self._poll_interval)

    def _log_monitor_snapshot(
        self,
        quotes: dict,
        unrealized_pnl: Optional[Decimal],
        cost_to_close: Optional[Decimal],
        dte: int,
    ) -> None:
        entry_credit = self._position.entry_credit or Decimal("0")
        pnl_str = str(unrealized_pnl) if unrealized_pnl is not None else "N/A"
        ctc_str = str(cost_to_close) if cost_to_close is not None else "N/A"
        logger.info(
            "position_snapshot",
            extra={
                "strategy_id": self._position.strategy_run_id,
                "unrealized_pnl": pnl_str,
                "cost_to_close": ctc_str,
                "entry_credit": str(entry_credit),
                "dte": dte,
            },
        )
        print(
            f"\n[MONITOR] {datetime.now(IST).strftime('%H:%M:%S IST')}  "
            f"DTE={dte}  MTM P&L=\u20b9{pnl_str}  "
            f"Cost-to-close={ctc_str}  Entry credit={entry_credit}"
        )
        for leg in self._position.legs:
            print(
                f"  {leg.trading_symbol:30s}  "
                f"bid={leg.current_bid or 'N/A':>8}  "
                f"ask={leg.current_ask or 'N/A':>8}  "
                f"ltp={leg.current_ltp or 'N/A':>8}"
            )
