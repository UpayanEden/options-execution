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
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from brokers.base import BrokerAdapter, BrokerError, QuoteError
from live.config import LiveConfig
from live.trade_logger import EventType, TradeLogger
from risk import kill_switch as _ks_module
from risk.kill_switch import KillSwitch, KillSwitchError

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 30
# Number of poll cycles between periodic reconciliation checks
_RECON_CYCLE_COUNT = 10


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
        on_exit_cb: Optional[Callable[[Any, str], None]] = None,
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
            on_exit_cb:   Optional callback for non-shift exits (DTE_EXIT).
                          Called as on_exit_cb(pos, reason) where reason is e.g.
                          "DTE_EXIT".  If None, exit triggers are logged but not acted on.
        """
        self._broker = broker
        self._positions = positions
        self._config = config
        self._trade_logger = logger_
        self._on_shift_cb = on_shift_cb
        self._on_exit_cb = on_exit_cb
        self._kill_switch = kill_switch or KillSwitch()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        # Per-symbol consecutive spot-miss counters (Section 6)
        self._spot_miss_count: Dict[str, int] = {}
        # Track which symbols had FNO_BAN_HOLD logged today (Section 7)
        self._ban_hold_logged: set = set()
        # Periodic reconciliation cycle counter (Section 5)
        self._recon_cycle: int = 0
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

        # Configure Telegram alerts if tokens are set
        try:
            from live import alerts as _alerts
            tok = getattr(self._config, "telegram_bot_token", "")
            cid = getattr(self._config, "telegram_chat_id", "")
            if tok and cid:
                _alerts.configure(tok, cid)
        except Exception as exc:
            logger.warning("shift_monitor_alerts_config_failed", extra={"error": str(exc)})

        # Pre-fetch ban list at startup
        try:
            from live import ban_list
            ban_list.refresh_if_stale()
        except Exception as exc:
            logger.warning("shift_monitor_ban_list_init_failed", extra={"error": str(exc)})

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

            # ── Refresh F&O ban list at top of each iteration ─────────────
            try:
                from live import ban_list
                ban_list.refresh_if_stale()
            except Exception as exc:
                logger.warning("ban_list_refresh_failed", extra={"error": str(exc)})

            # ── Snapshot positions list ────────────────────────────────────
            with self._lock:
                current_positions = list(self._positions)

            if not current_positions:
                logger.debug("shift_monitor_no_positions")
            else:
                # Pre-fetch all equity spots in ONE batch API call to avoid
                # Zerodha's 3 req/second rate limit when monitoring multiple positions.
                spot_cache: Dict[str, Optional[float]] = {}
                all_spots_null = True
                if hasattr(self._broker, "get_equity_spots_batch"):
                    underlyings = list({
                        getattr(p, "underlying", "") for p in current_positions
                        if getattr(p, "underlying", "")
                    })
                    if underlyings:
                        spot_cache = self._broker.get_equity_spots_batch(underlyings)  # type: ignore[attr-defined]
                        all_spots_null = all(v is None or v <= 0 for v in spot_cache.values()) if spot_cache else True

                # Build options_cache: single batch quote call for all 4 legs of each position
                options_cache: Optional[Dict[str, float]] = None
                try:
                    instrument_pairs: List[tuple] = []
                    for p in current_positions:
                        for leg_attr in ("short_put", "long_put", "short_call", "long_call"):
                            inst = getattr(p, leg_attr, None)
                            if inst is not None:
                                tok = getattr(inst, "instrument_token", None)
                                tsym = getattr(inst, "trading_symbol", None)
                                if tok and tsym:
                                    instrument_pairs.append((tok, tsym))
                    if instrument_pairs and hasattr(self._broker, "get_quotes"):
                        raw_quotes = self._broker.get_quotes(instrument_pairs)  # type: ignore[attr-defined]
                        # Build keyed by trading_symbol for easy lookup
                        options_cache = {}
                        for p in current_positions:
                            for leg_attr in ("short_put", "long_put", "short_call", "long_call"):
                                inst = getattr(p, leg_attr, None)
                                if inst is not None:
                                    tok = getattr(inst, "instrument_token", None)
                                    tsym = getattr(inst, "trading_symbol", None)
                                    if tok and tsym and tok in raw_quotes:
                                        q = raw_quotes[tok]
                                        # Store both bid and ask keyed by trading_symbol
                                        options_cache[f"{tsym}:bid"] = float(getattr(q, "bid", 0) or 0)
                                        options_cache[f"{tsym}:ask"] = float(getattr(q, "ask", 0) or 0)
                                        ltp = float(getattr(q, "ltp", 0) or 0)
                                        options_cache[f"{tsym}:ltp"] = ltp
                except QuoteError:
                    options_cache = None
                except Exception as _exc:
                    logger.warning("options_cache_build_failed", extra={"error": str(_exc)})
                    options_cache = None

                for pos in current_positions:
                    # Kill switch re-check between positions
                    if self._kill_switch.is_active():
                        logger.critical("kill_switch_activated_mid_loop")
                        self._stop_event.set()
                        break
                    self._process_position(pos, spot_cache=spot_cache, all_spots_null=all_spots_null,
                                           options_cache=options_cache)

                # ── Periodic reconciliation every N cycles ─────────────────
                self._recon_cycle += 1
                if self._recon_cycle >= _RECON_CYCLE_COUNT:
                    self._recon_cycle = 0
                    self._periodic_reconcile(current_positions)

            # ── Wait for next poll ──────────────────────────────────────────
            self._stop_event.wait(timeout=_POLL_INTERVAL_SECONDS)

        logger.info("shift_monitor_loop_ended")

    def _process_position(
        self,
        pos: Any,
        spot_cache: Optional[Dict[str, Optional[float]]] = None,
        all_spots_null: bool = False,
        options_cache: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Evaluate a single position for shift or take-profit conditions.

        Args:
            pos:             CondorPosition dataclass instance.
            spot_cache:      Pre-fetched spot prices keyed by underlying.upper().
            all_spots_null:  True if every spot in the batch was None/0.
            options_cache:   Pre-fetched option quotes keyed by "<trading_symbol>:bid/ask/ltp".
        """
        symbol = getattr(pos, "symbol", "UNKNOWN")

        # ── NEEDS_RECON: skip all automatic action, wait for manual recon ──
        if getattr(pos, "state", "") == "NEEDS_RECON":
            logger.warning("shift_monitor_skip_needs_recon", extra={"symbol": symbol})
            return

        # ── F&O ban suspension check ───────────────────────────────────────
        if getattr(pos, "fo_ban_suspended", False):
            suspended_at = getattr(pos, "fo_ban_suspended_at", 0.0)
            elapsed = time.time() - suspended_at
            if elapsed < 3600:  # wait 1 hour before retrying
                logger.debug(
                    "shift_skipped_fo_ban_suspended",
                    extra={"symbol": symbol, "retry_in_mins": round((3600 - elapsed) / 60, 1)},
                )
                return
            # 1 hour has passed — clear suspension and let normal flow try again
            pos.fo_ban_suspended = False
            pos.fo_ban_failures = 0
            pos.fo_ban_suspended_at = 0.0
            logger.info("fo_ban_suspension_cleared_retrying", extra={"symbol": symbol})

        try:
            action, reason = self._check_position(
                pos, spot_cache=spot_cache, all_spots_null=all_spots_null,
                options_cache=options_cache,
            )

            if action == "":
                logger.debug("position_ok", extra={"symbol": symbol})
                return

            if action in ("DATA_UNAVAILABLE", "BAN_HOLD", "BAN_HOLD_DTE_WARN", "NEEDS_RECON"):
                # Nothing to do — logged inside _check_position
                return

            if action.startswith("SHIFT"):
                trigger_side = "PUT" if "PUT" in action else "CALL"
                logger.warning("shift_triggered", extra={"symbol": symbol, "reason": reason, "side": trigger_side})
                self._on_shift_cb(pos, trigger_side)
            elif action == "DTE_EXIT":
                logger.warning("dte_exit_triggered_by_monitor",
                               extra={"symbol": symbol, "reason": reason})
                if self._on_exit_cb is not None:
                    self._on_exit_cb(pos, "DTE_EXIT")
                else:
                    self._trade_logger.log(
                        EventType.SHIFT_TRIGGERED,
                        symbol,
                        {"reason": reason, "action": "exit_cb_not_set"},
                        level="WARNING",
                    )
            elif action == "PROFIT_TARGET_EXIT":
                logger.warning("profit_target_exit_triggered",
                               extra={"symbol": symbol, "reason": reason})
                if self._on_exit_cb is not None:
                    self._on_exit_cb(pos, "PROFIT_TARGET")
                else:
                    self._trade_logger.log(
                        EventType.PROFIT_TARGET_EXIT,
                        symbol,
                        {"reason": reason, "action": "exit_cb_not_set"},
                        level="WARNING",
                    )
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

    def _check_position(
        self,
        pos: Any,
        spot_cache: Optional[Dict[str, Optional[float]]] = None,
        all_spots_null: bool = False,
        options_cache: Optional[Dict[str, float]] = None,
    ) -> Tuple[str, str]:
        """
        Evaluate a single CondorPosition for shift or exit triggers.

        Checks in priority order:
          0. NEEDS_RECON state → return immediately.
          1. F&O ban list check → BAN_HOLD (DTE exit may still fire if configured).
          2. DTE_EXIT: expiry within dte_exit days → trigger exit.
          3. Spot missing check → DATA_UNAVAILABLE with logging.
          3b. PROFIT_TARGET_EXIT: 85% of original credit captured.
          4. SHIFT_PUT / SHIFT_CALL: spot within trigger zone.

        Args:
            pos:            CondorPosition dataclass.
            spot_cache:     Optional pre-fetched spot prices from get_equity_spots_batch().
            all_spots_null: True if all symbols returned null spot this cycle.
            options_cache:  Optional pre-fetched option quotes for profit target check.

        Returns:
            Tuple (action: str, reason: str).
            action is one of: "SHIFT_PUT", "SHIFT_CALL", "DTE_EXIT", "PROFIT_TARGET_EXIT",
            "DATA_UNAVAILABLE", "NEEDS_RECON", "BAN_HOLD", "BAN_HOLD_DTE_WARN", or "".
        """
        from datetime import date as _date
        symbol: str = getattr(pos, "symbol", "UNKNOWN")

        # ── 0. NEEDS_RECON short-circuit ──────────────────────────────────
        if getattr(pos, "state", "") == "NEEDS_RECON":
            return "NEEDS_RECON", ""

        # ── 1. F&O ban list check ──────────────────────────────────────────
        try:
            from live import ban_list
            banned = ban_list.is_banned(symbol)
        except Exception:
            banned = False

        if banned:
            if symbol not in self._ban_hold_logged:
                self._ban_hold_logged.add(symbol)
                pos.ban_hold = True
                self._trade_logger.log(
                    EventType.FNO_BAN_HOLD, symbol,
                    {"reason": "symbol_on_fno_ban_list"},
                    level="WARNING",
                )
            # Check if DTE exit should still fire during ban
            expiry = getattr(pos, "expiry", None)
            if expiry is not None:
                dte_exit_threshold = getattr(self._config.strategy, "dte_exit", 2)
                try:
                    dte_remaining = (expiry - _date.today()).days
                except Exception:
                    dte_remaining = 999
                if dte_remaining <= dte_exit_threshold:
                    allow_exit = getattr(self._config.strategy, "allow_dte_exit_during_ban", True)
                    if allow_exit:
                        return "DTE_EXIT", f"DTE_EXIT_DURING_BAN: dte={dte_remaining}"
                    else:
                        logger.warning("dte_exit_blocked_by_ban",
                                       extra={"symbol": symbol, "dte": dte_remaining})
                        self._send_alert(
                            f"WARNING: {symbol} DTE={dte_remaining} needs exit but on F&O ban list. "
                            "allow_dte_exit_during_ban=False. Manual action required."
                        )
                        return "BAN_HOLD_DTE_WARN", ""
            return "BAN_HOLD", ""
        else:
            if getattr(pos, "ban_hold", False):
                pos.ban_hold = False
                self._ban_hold_logged.discard(symbol)
                self._trade_logger.log(EventType.FNO_BAN_LIFTED, symbol, {})

        # ── 2. DTE exit check ─────────────────────────────────────────────
        expiry = getattr(pos, "expiry", None)
        if expiry is not None:
            dte_exit_threshold = getattr(self._config.strategy, "dte_exit", 2)
            try:
                dte_remaining = (expiry - _date.today()).days
            except Exception:
                dte_remaining = 999
            if dte_remaining <= dte_exit_threshold:
                logger.warning("dte_exit_threshold_reached",
                               extra={"symbol": symbol,
                                      "dte_remaining": dte_remaining,
                                      "threshold": dte_exit_threshold})
                return "DTE_EXIT", f"DTE_EXIT: dte={dte_remaining} ≤ threshold={dte_exit_threshold}"

        # ── 3. Fetch live spot price (use cache if available) ──────────────
        spot: Optional[float] = None
        if spot_cache is not None:
            underlying = getattr(pos, "underlying", "")
            spot = spot_cache.get(underlying.upper())
        if spot is None or spot <= 0:
            spot = self._get_current_spot(pos, {})
        if spot is None or spot <= 0:
            # Section 6: Missing spot — track count and alert
            miss_count = self._spot_miss_count.get(symbol, 0) + 1
            self._spot_miss_count[symbol] = miss_count
            self._trade_logger.log(
                EventType.SPOT_MISSING, symbol,
                {"miss_count": miss_count},
                level="WARNING",
            )
            spot_missing_alert_cycles = getattr(
                self._config.strategy, "spot_missing_alert_cycles", 4
            )
            if all_spots_null:
                self._trade_logger.log(
                    EventType.ERROR, None,
                    {"reason": "all_spots_null_possible_auth_failure"},
                    level="ERROR",
                )
                self._send_alert("Feed failure: all spot prices null. Checking token...")
                self._check_token_health()
            elif miss_count >= spot_missing_alert_cycles:
                self._send_alert(f"Spot missing for {symbol} for {miss_count} cycles")
            return "DATA_UNAVAILABLE", ""

        # Reset miss counter on success
        self._spot_miss_count[symbol] = 0

        # ── 3b. Profit target check ───────────────────────────────────────
        if options_cache is not None:
            try:
                sp_bid_key  = f"{pos.short_put.trading_symbol}:bid"
                sc_bid_key  = f"{pos.short_call.trading_symbol}:bid"
                lp_ask_key  = f"{pos.long_put.trading_symbol}:ask"
                lc_ask_key  = f"{pos.long_call.trading_symbol}:ask"

                sp_ltp_key  = f"{pos.short_put.trading_symbol}:ltp"
                sc_ltp_key  = f"{pos.short_call.trading_symbol}:ltp"
                lp_ltp_key  = f"{pos.long_put.trading_symbol}:ltp"
                lc_ltp_key  = f"{pos.long_call.trading_symbol}:ltp"

                keys_present = all(k in options_cache for k in (sp_bid_key, sc_bid_key, lp_ask_key, lc_ask_key))
                if keys_present:
                    def _leg_val(bid_key: str, ltp_key: str) -> float:
                        bid = options_cache.get(bid_key, 0.0)
                        ltp = options_cache.get(ltp_key, 0.0)
                        return bid if bid > 0 else ltp

                    short_put_bid  = _leg_val(sp_bid_key, sp_ltp_key)
                    short_call_bid = _leg_val(sc_bid_key, sc_ltp_key)
                    long_put_ask   = options_cache.get(lp_ask_key, 0.0) or options_cache.get(lp_ltp_key, 0.0)
                    long_call_ask  = options_cache.get(lc_ask_key, 0.0) or options_cache.get(lc_ltp_key, 0.0)

                    current_credit = (short_put_bid + short_call_bid) - (long_put_ask + long_call_ask)
                    original_credit = float(getattr(pos, "original_credit", 0.0) or 0.0)
                    profit_captured = original_credit - max(current_credit, 0.0)
                    profit_target_pct = getattr(self._config.strategy, "profit_target_pct", 85.0)
                    target = original_credit * (profit_target_pct / 100.0)

                    if original_credit > 0 and profit_captured >= target:
                        self._trade_logger.log(
                            EventType.PROFIT_TARGET_EXIT,
                            symbol,
                            {
                                "original_credit": round(original_credit, 4),
                                "current_credit": round(current_credit, 4),
                                "profit_captured": round(profit_captured, 4),
                                "target": round(target, 4),
                                "profit_target_pct": profit_target_pct,
                            },
                            level="WARNING",
                        )
                        return "PROFIT_TARGET_EXIT", (
                            f"profit={profit_captured:.4f} >= target={target:.4f}"
                        )
            except Exception as _exc:
                logger.debug("profit_target_check_error", extra={"symbol": symbol, "error": str(_exc)})

        # ── 4. Shift checks ────────────────────────────────────────────────
        shift_trigger_pct = self._config.strategy.shift_trigger_pct
        short_put_strike = float(pos.short_put.strike)
        short_call_strike = float(pos.short_call.strike)

        put_breach_level  = short_put_strike  * (1.0 + shift_trigger_pct / 100.0)
        call_breach_level = short_call_strike * (1.0 - shift_trigger_pct / 100.0)

        if spot < put_breach_level:
            pct_away = (spot - short_put_strike) / short_put_strike * 100.0
            self._trade_logger.log(
                EventType.SHIFT_TRIGGERED,
                symbol,
                {
                    "direction": "PUT",
                    "spot": round(spot, 2),
                    "short_put_strike": short_put_strike,
                    "breach_level": round(put_breach_level, 2),
                    "pct_away_from_strike": round(pct_away, 2),
                },
                level="WARNING",
            )
            return "SHIFT_PUT", f"SHIFT_PUT: spot={spot:.2f} < breach={put_breach_level:.2f}"

        if spot > call_breach_level:
            pct_away = (short_call_strike - spot) / short_call_strike * 100.0
            self._trade_logger.log(
                EventType.SHIFT_TRIGGERED,
                symbol,
                {
                    "direction": "CALL",
                    "spot": round(spot, 2),
                    "short_call_strike": short_call_strike,
                    "breach_level": round(call_breach_level, 2),
                    "pct_away_from_strike": round(pct_away, 2),
                },
                level="WARNING",
            )
            return "SHIFT_CALL", f"SHIFT_CALL: spot={spot:.2f} > breach={call_breach_level:.2f}"

        return "", ""

    # ── Alert / health helpers ─────────────────────────────────────────────

    def _send_alert(self, msg: str) -> None:
        """Send a Telegram alert. Silently skips if alerts not configured."""
        try:
            from live import alerts as _alerts
            _alerts.send_alert(msg)
        except Exception as exc:
            logger.warning("alert_send_failed", extra={"error": str(exc)})

    def _check_token_health(self) -> None:
        """Check if the Kite auth token is still valid. Alert if not."""
        try:
            if hasattr(self._broker, "get_margins"):
                self._broker.get_margins()  # type: ignore[attr-defined]
            elif hasattr(self._broker, "profile"):
                self._broker.profile()  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error("token_health_check_failed", extra={"error": str(exc)})
            self._trade_logger.log(
                EventType.ERROR, None,
                {"reason": "kite_auth_failed", "error": str(exc)},
                level="ERROR",
            )
            self._send_alert("Kite auth failed — positions unmonitored")

    def _periodic_reconcile(self, positions: list) -> None:
        """
        Every N cycles, compare each open position's expected legs against
        broker.get_positions(). Log POSITION_MISMATCH and alert if any qty differs.
        """
        try:
            broker_positions = self._broker.get_positions()  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("periodic_recon_broker_call_failed", extra={"error": str(exc)})
            return

        # Build a dict: trading_symbol → net_qty from broker
        broker_qty: Dict[str, int] = {}
        for bp in (broker_positions if isinstance(broker_positions, list) else []):
            ts = str(bp.get("tradingsymbol", bp.get("trading_symbol", "")))
            qty = int(bp.get("quantity", bp.get("net_quantity", 0)) or 0)
            if ts:
                broker_qty[ts] = broker_qty.get(ts, 0) + qty

        for pos in positions:
            if getattr(pos, "state", "") not in ("OPEN", "SHIFTING"):
                continue
            symbol = getattr(pos, "symbol", "?")
            expected_qty = getattr(pos, "lots", 0) * getattr(pos, "lot_size", 0)
            mismatches: Dict[str, Any] = {}

            for leg_name, inst_attr in [
                ("short_put", "short_put"), ("long_put", "long_put"),
                ("short_call", "short_call"), ("long_call", "long_call"),
            ]:
                inst = getattr(pos, inst_attr, None)
                if inst is None:
                    continue
                ts = getattr(inst, "trading_symbol", "")
                actual = broker_qty.get(ts, 0)
                # Short legs have negative qty in broker; long legs positive
                exp = expected_qty if leg_name.startswith("long") else -expected_qty
                if actual != exp:
                    mismatches[leg_name] = {"expected": exp, "actual": actual, "symbol": ts}

            if mismatches:
                logger.warning("periodic_position_mismatch", extra={"symbol": symbol, "mismatches": mismatches})
                self._trade_logger.log(
                    EventType.POSITION_MISMATCH, symbol,
                    {"mismatches": mismatches, "source": "periodic_reconcile"},
                    level="WARNING",
                )
                self._send_alert(
                    f"Position mismatch for {symbol}: {list(mismatches.keys())} differ from broker. "
                    "Run recon_cli.py to resolve."
                )

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
            # Index underlyings (NIFTY, BANKNIFTY, etc.) — use integer token
            # to avoid "NSE:NIFTY 50" naming mismatch in Zerodha API response.
            if hasattr(self._broker, "get_index_spot"):
                spot = self._broker.get_index_spot(underlying)
                if spot and spot > 0:
                    return spot

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
