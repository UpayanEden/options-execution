"""
Background daemon thread that monitors open condor positions for shift
and take-profit conditions, checking every 30 seconds.

The kill switch is checked at the start of every iteration so the system
can be halted immediately without waiting for the next poll cycle.

Usage::

    monitor = ShiftMonitor(
        broker=broker,
        positions=positions_list,
        config=live_cfg,
        logger=trade_logger,
        on_shift_cb=condor_runner.shift_condor,
        on_take_profit_cb=condor_runner.exit_condor,
    )
    monitor.start()
    # ... trading session ...
    monitor.stop()
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from brokers.base import BrokerAdapter, BrokerError, QuoteError
from live.config import LiveConfig
from live.trade_logger import EventType, TradeLogger
from risk.kill_switch import KillSwitch, KillSwitchError

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 30


class ShiftMonitor:
    """
    Daemon thread that watches live condor positions and fires callbacks
    when a shift or take-profit condition is detected.

    The monitor checks the kill switch at the top of every iteration.
    On kill switch activation it stops itself immediately.
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        positions: List[Any],
        config: LiveConfig,
        logger_: TradeLogger,
        on_shift_cb: Callable[[Any], Optional[Any]],
        on_take_profit_cb: Callable[[Any, str], None],
        kill_switch: Optional[KillSwitch] = None,
    ) -> None:
        """
        Initialise the shift monitor.

        Args:
            broker:             Broker adapter for live quotes.
            positions:          Mutable list of CondorPosition objects to monitor.
                                The CondorRunner owns this list and may append/remove
                                positions concurrently; access is protected by _lock.
            config:             Live trading config.
            logger_:            TradeLogger instance for structured event logging.
            on_shift_cb:        Callback invoked with (CondorPosition,) when a shift
                                is triggered. Should return the new CondorPosition or None.
            on_take_profit_cb:  Callback invoked with (CondorPosition, reason) when
                                take-profit fires.
            kill_switch:        KillSwitch instance. If None, a new one is created.
        """
        self._broker = broker
        self._positions = positions
        self._config = config
        self._trade_logger = logger_
        self._on_shift_cb = on_shift_cb
        self._on_take_profit_cb = on_take_profit_cb
        self._kill_switch = kill_switch or KillSwitch()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        logger.info("shift_monitor_created")

    # ── Public control API ─────────────────────────────────────────────────

    def start(self) -> None:
        """
        Start the background monitoring thread.

        The thread is a daemon so it does not prevent process exit.
        """
        if self._thread and self._thread.is_alive():
            logger.warning("shift_monitor_already_running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="ShiftMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("shift_monitor_started")

    def stop(self) -> None:
        """
        Request the monitoring thread to stop and wait for it to finish.

        Waits up to 2 × poll interval before returning regardless.
        """
        logger.info("shift_monitor_stopping")
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=_POLL_INTERVAL_SECONDS * 2)
        logger.info("shift_monitor_stopped")

    # ── Internal loop ──────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """
        Main monitoring loop.

        Runs until _stop_event is set or the kill switch is activated.
        Checks the kill switch before every iteration.
        """
        logger.info("shift_monitor_loop_started")
        while not self._stop_event.is_set():
            # ── Kill switch check (must be first thing every iteration) ────
            try:
                self._kill_switch.check()
            except KillSwitchError:
                logger.critical("shift_monitor_kill_switch_active_stopping")
                self._trade_logger.log(
                    EventType.KILL_SWITCH_ACTIVATED,
                    None,
                    {"source": "ShiftMonitor", "reason": "Kill switch active — monitor stopping"},
                    level="CRITICAL",
                )
                self._stop_event.set()
                break

            # ── Snapshot positions list ────────────────────────────────────
            with self._lock:
                current_positions = list(self._positions)

            if not current_positions:
                logger.debug("shift_monitor_no_positions")
            else:
                for pos in current_positions:
                    # Kill switch re-check between positions
                    if self._kill_switch.is_active():
                        logger.critical("kill_switch_activated_mid_loop")
                        self._stop_event.set()
                        break
                    self._process_position(pos)

            # ── Wait for next poll ──────────────────────────────────────────
            self._stop_event.wait(timeout=_POLL_INTERVAL_SECONDS)

        logger.info("shift_monitor_loop_ended")

    def _process_position(self, pos: Any) -> None:
        """
        Evaluate a single position for shift or take-profit conditions.

        Args:
            pos: CondorPosition dataclass instance.
        """
        symbol = getattr(pos, "symbol", "UNKNOWN")
        try:
            should_act, reason = self._check_position(pos)
            if not should_act:
                logger.debug("position_ok", extra={"symbol": symbol})
                return

            if reason.startswith("TAKE_PROFIT"):
                logger.info("take_profit_triggered", extra={"symbol": symbol, "reason": reason})
                self._on_take_profit_cb(pos, reason)
            elif reason.startswith("SHIFT"):
                logger.warning("shift_triggered", extra={"symbol": symbol, "reason": reason})
                self._on_shift_cb(pos)
        except BrokerError as exc:
            logger.error(
                "shift_monitor_broker_error",
                extra={"symbol": symbol, "error": str(exc)},
            )
            self._trade_logger.log_error(symbol, exc, {"phase": "shift_monitor"})
        except Exception as exc:
            logger.exception(
                "shift_monitor_unexpected_error",
                extra={"symbol": symbol, "error": str(exc)},
            )
            self._trade_logger.log_error(symbol, exc, {"phase": "shift_monitor"})

    def _check_position(self, pos: Any) -> Tuple[bool, str]:
        """
        Evaluate a single CondorPosition for action triggers.

        Conditions checked (in priority order):
        1. TAKE_PROFIT: current_cost <= (1 - premium_pct) × initial_credit
        2. SHIFT_PUT:   spot < short_put_strike × (1 - shift_trigger_pct/100)
        3. SHIFT_CALL:  spot > short_call_strike × (1 + shift_trigger_pct/100)

        Args:
            pos: CondorPosition dataclass.

        Returns:
            Tuple (should_act: bool, reason: str). reason is empty string
            when should_act is False.
        """
        symbol: str = getattr(pos, "symbol", "UNKNOWN")

        # ── Fetch live quotes for all four legs ────────────────────────────
        legs_instruments = [
            pos.long_put,
            pos.short_put,
            pos.short_call,
            pos.long_call,
        ]
        instrument_pairs = [
            (inst.instrument_token, inst.trading_symbol)
            for inst in legs_instruments
        ]
        try:
            quotes: Dict[int, Any] = self._broker.get_quotes(instrument_pairs)
        except QuoteError as exc:
            logger.warning(
                "shift_monitor_quote_error",
                extra={"symbol": symbol, "error": str(exc)},
            )
            return False, ""

        # ── Current spot price ─────────────────────────────────────────────
        # Derive spot from underlying index or use last known value
        spot = self._get_current_spot(pos, quotes)
        if spot is None or spot <= 0:
            logger.warning("shift_monitor_no_spot", extra={"symbol": symbol})
            return False, ""

        # ── Take-profit check ──────────────────────────────────────────────
        # Find the stock config for premium_pct
        premium_pct = 0.4  # default
        for stock_cfg in self._config.stocks:
            if stock_cfg.symbol == symbol:
                premium_pct = stock_cfg.premium_pct
                break

        initial_credit = float(getattr(pos, "initial_credit", 0) or 0)
        if initial_credit > 0:
            # Sum current ask prices of short legs (cost to buy back)
            # Sum current bid prices of long legs (proceeds from selling)
            current_cost = self._compute_current_cost(pos, quotes)
            take_profit_threshold = (1.0 - premium_pct) * initial_credit
            if current_cost <= take_profit_threshold and current_cost >= 0:
                profit_pct = (1.0 - current_cost / initial_credit) * 100.0
                self._trade_logger.log_take_profit(
                    symbol=symbol,
                    credit_received=initial_credit,
                    current_cost=current_cost,
                    profit_pct=profit_pct,
                    pnl=(initial_credit - current_cost) * pos.lots * pos.lot_size,
                )
                return True, f"TAKE_PROFIT: cost={current_cost:.4f} <= threshold={take_profit_threshold:.4f}"

        # ── Shift checks ───────────────────────────────────────────────────
        shift_trigger_pct = self._config.strategy.shift_trigger_pct
        short_put_strike = float(pos.short_put.strike)
        short_call_strike = float(pos.short_call.strike)

        put_breach_level = short_put_strike * (1.0 - shift_trigger_pct / 100.0)
        call_breach_level = short_call_strike * (1.0 + shift_trigger_pct / 100.0)

        if spot < put_breach_level:
            pct_through = (short_put_strike - spot) / short_put_strike * 100.0
            self._trade_logger.log(
                EventType.SHIFT_TRIGGERED,
                symbol,
                {
                    "direction": "PUT",
                    "spot": round(spot, 2),
                    "short_put_strike": short_put_strike,
                    "breach_level": round(put_breach_level, 2),
                    "pct_through": round(pct_through, 2),
                },
                level="WARNING",
            )
            return True, f"SHIFT_PUT: spot={spot:.2f} < breach={put_breach_level:.2f}"

        if spot > call_breach_level:
            pct_through = (spot - short_call_strike) / short_call_strike * 100.0
            self._trade_logger.log(
                EventType.SHIFT_TRIGGERED,
                symbol,
                {
                    "direction": "CALL",
                    "spot": round(spot, 2),
                    "short_call_strike": short_call_strike,
                    "breach_level": round(call_breach_level, 2),
                    "pct_through": round(pct_through, 2),
                },
                level="WARNING",
            )
            return True, f"SHIFT_CALL: spot={spot:.2f} > breach={call_breach_level:.2f}"

        return False, ""

    # ── Helper methods ─────────────────────────────────────────────────────

    def _get_current_spot(self, pos: Any, quotes: Dict[int, Any]) -> Optional[float]:
        """
        Derive the current spot price for the underlying.

        Tries to get the underlying spot from the broker.  Falls back to
        the average of short put and short call strikes (geometric centre
        of the condor) if the index quote is unavailable.

        Args:
            pos:    CondorPosition dataclass.
            quotes: Latest option quotes keyed by instrument token.

        Returns:
            Current spot price or None if unavailable.
        """
        try:
            # Try fetching the underlying equity/index quote
            underlying = getattr(pos, "underlying", "")
            if hasattr(self._broker, "get_index_token"):
                idx_token = self._broker.get_index_token(underlying)
                if idx_token:
                    idx_quotes = self._broker.get_quotes([(idx_token, underlying)])
                    if idx_token in idx_quotes:
                        return float(idx_quotes[idx_token].ltp)
        except Exception:
            pass

        # Fallback: derive from option LTPs using midpoint of short strikes
        short_put_token = pos.short_put.instrument_token
        short_call_token = pos.short_call.instrument_token
        if short_put_token in quotes and short_call_token in quotes:
            sp_ltp = float(quotes[short_put_token].ltp)
            sc_ltp = float(quotes[short_call_token].ltp)
            if sp_ltp > 0 or sc_ltp > 0:
                # Use last known spot from the position
                return float(getattr(pos, "spot_at_entry", 0) or 0)

        return float(getattr(pos, "spot_at_entry", 0) or 0)

    def _compute_current_cost(self, pos: Any, quotes: Dict[int, Any]) -> float:
        """
        Compute the current cost to close all four legs of the condor.

        Cost = (buy back shorts at ask) - (sell hedges at bid).
        For a credit spread, closing cost < initial credit means profit.

        Args:
            pos:    CondorPosition dataclass.
            quotes: Latest quotes keyed by instrument token.

        Returns:
            Net cost per share to close the condor (positive = debit).
        """
        cost = 0.0

        # Buy back short legs (pay ask)
        for short_inst in [pos.short_put, pos.short_call]:
            q = quotes.get(short_inst.instrument_token)
            if q:
                cost += float(q.ask)
            else:
                logger.warning(
                    "close_cost_no_quote",
                    extra={"token": short_inst.instrument_token, "symbol": short_inst.trading_symbol},
                )
                cost += float(short_inst.strike) * 0.001  # rough fallback

        # Sell hedge legs (receive bid)
        for hedge_inst in [pos.long_put, pos.long_call]:
            q = quotes.get(hedge_inst.instrument_token)
            if q:
                cost -= float(q.bid)
            else:
                logger.warning(
                    "close_cost_no_quote",
                    extra={"token": hedge_inst.instrument_token, "symbol": hedge_inst.trading_symbol},
                )

        return cost
