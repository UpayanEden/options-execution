"""
Background daemon thread that monitors open condor positions for shift
conditions, checking every 30 seconds.

Strategy: hold to expiry, no take-profit.  Only shift when spot breaches
the 1% trigger zone around a short strike.  Only the threatened side's
spread is closed and re-entered; the other spread is left untouched.

The kill switch is checked at the start of every iteration so the system
can be halted immediately without waiting for the next poll cycle.

Usage::

    monitor = ShiftMonitor(
        broker=broker,
        positions=positions_list,
        config=live_cfg,
        logger=trade_logger,
        on_shift_cb=condor_runner.shift_condor,   # called as on_shift_cb(pos, "PUT"|"CALL")
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
        on_shift_cb: Callable[[Any, str], None],
        kill_switch: Optional[KillSwitch] = None,
    ) -> None:
        """
        Initialise the shift monitor.

        Args:
            broker:       Broker adapter for live quotes.
            positions:    Mutable list of CondorPosition objects to monitor.
                          The CondorRunner owns this list and may append/remove
                          positions concurrently; access is protected by _lock.
            config:       Live trading config.
            logger_:      TradeLogger instance for structured event logging.
            on_shift_cb:  Callback invoked as on_shift_cb(pos, trigger_side) where
                          trigger_side is "PUT" or "CALL".  The runner closes only
                          the threatened spread and re-enters it at new strikes.
            kill_switch:  KillSwitch instance. If None, a new one is created.
        """
        self._broker = broker
        self._positions = positions
        self._config = config
        self._trade_logger = logger_
        self._on_shift_cb = on_shift_cb
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

            if reason.startswith("SHIFT"):
                # Extract which side triggered: "SHIFT_PUT:..." → "PUT"
                trigger_side = "PUT" if "PUT" in reason else "CALL"
                logger.warning("shift_triggered", extra={"symbol": symbol, "reason": reason, "side": trigger_side})
                self._on_shift_cb(pos, trigger_side)
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
        Evaluate a single CondorPosition for shift triggers.

        No take-profit — the strategy holds to expiry.  Only checks:
          SHIFT_PUT:   spot < short_put_strike × (1 - shift_trigger_pct/100)
          SHIFT_CALL:  spot > short_call_strike × (1 + shift_trigger_pct/100)

        Args:
            pos: CondorPosition dataclass.

        Returns:
            Tuple (should_act: bool, reason: str). reason is empty string
            when should_act is False.
        """
        symbol: str = getattr(pos, "symbol", "UNKNOWN")

        # ── Fetch live spot price ──────────────────────────────────────────
        spot = self._get_current_spot(pos, {})
        if spot is None or spot <= 0:
            logger.warning("shift_monitor_no_spot", extra={"symbol": symbol})
            return False, ""

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

    def _get_current_spot(self, pos: Any, _quotes: Dict[int, Any]) -> Optional[float]:
        """
        Fetch the current spot price for the underlying equity.

        For NSE equities, uses get_equity_spot() which correctly applies the
        NSE: exchange prefix.  For index underlyings (NIFTY etc.), tries
        get_index_token() + get_quotes().  Falls back to spot_at_entry on
        any failure so monitoring continues even if quotes are temporarily
        unavailable.

        Args:
            pos:     CondorPosition dataclass.
            _quotes: Unused; kept for call-site compatibility.

        Returns:
            Current spot price or None if unavailable.
        """
        underlying = getattr(pos, "underlying", "")
        try:
            # Index underlyings (NIFTY, BANKNIFTY, etc.)
            if hasattr(self._broker, "get_index_token"):
                idx_token = self._broker.get_index_token(underlying)
                if idx_token:
                    quotes = self._broker.get_quotes([(idx_token, underlying)])
                    if idx_token in quotes:
                        ltp = float(quotes[idx_token].ltp)
                        if ltp > 0:
                            return ltp

            # NSE equities — must use NSE: prefix, not NFO:
            if hasattr(self._broker, "get_equity_spot"):
                spot = self._broker.get_equity_spot(underlying)
                if spot and spot > 0:
                    return spot
        except Exception as exc:
            logger.warning(
                "shift_monitor_spot_fetch_failed",
                extra={"symbol": underlying, "error": str(exc)},
            )

        # Last resort: use stale entry price so monitoring doesn't halt
        fallback = float(getattr(pos, "spot_at_entry", 0) or 0)
        if fallback > 0:
            logger.warning(
                "shift_monitor_using_stale_spot",
                extra={"symbol": underlying, "spot_at_entry": fallback},
            )
        return fallback or None
