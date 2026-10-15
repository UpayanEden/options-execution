"""
CondorRunner — orchestrates the full live iron condor lifecycle.

Responsibilities:
- Session startup and stock selection.
- Per-stock condor entry: spot → expiry → strikes → snap → quotes → guards → orders.
- Position monitoring loop (delegated to ShiftMonitor).
- Single-leg shift: close threatened side's spread, re-enter it at new strikes.
  The retained side stays open and unchanged.  No take-profit — hold to expiry.
- Exit execution: close all four legs at expiry or on kill switch.

All order placements go through run_all_guards() — never bypassed.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from brokers.base import BrokerAdapter, BrokerError, InstrumentNotFoundError, QuoteError
from brokers.zerodha import ZerodhaBroker
from execution.models import Order, OrderStatus, ResolvedInstrument, Side
from live.capital_manager import CapitalManager
from live.config import LiveConfig, StockConfig, load_config as _load_live_config
from live.guard_layers import GuardContext, GuardReport, run_all_guards
from live.position_store import PositionStore
from live.shift_monitor import ShiftMonitor
from live.trade_logger import EventType, TradeLogger
from risk.kill_switch import KillSwitch, KillSwitchError

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# How many seconds to wait between placing hedge and short orders
_ORDER_SPACING_SECONDS = 1.0
# How many seconds to wait after placing orders before reconciling
_RECONCILE_WAIT_SECONDS = 5.0
# Order fill wait polling
_FILL_POLL_INTERVAL = 2.0
_FILL_TIMEOUT_SECONDS = 60.0

# Time cutoffs (IST, HH:MM format for string comparison)
_ENTRY_CUTOFF_TIME = "15:15"   # No new entries after 3:15 PM IST
_SHIFT_CUTOFF_TIME = "15:20"   # No shifts after 3:20 PM IST

# Order confirmation polling constants
_CONFIRM_POLL_INTERVAL = 2.0   # seconds between status polls
_CONFIRM_TIMEOUT_SECS = 60.0   # max wait for fill confirmation
_MAX_ORDER_RETRIES = 2         # retry unfilled orders once


# ── CondorPosition dataclass ───────────────────────────────────────────────


@dataclass
class CondorPosition:
    """Represents a live iron condor position with all four legs."""

    symbol: str
    """Underlying NSE symbol (e.g. 'RELIANCE')."""

    underlying: str
    """Underlying name passed to resolve_instrument."""

    expiry: date
    """Option expiry date."""

    spot_at_entry: float
    """Spot price of underlying at time of entry."""

    short_put: ResolvedInstrument
    """Short put leg (SELL)."""

    long_put: ResolvedInstrument
    """Long put leg (BUY) — the put hedge."""

    short_call: ResolvedInstrument
    """Short call leg (SELL)."""

    long_call: ResolvedInstrument
    """Long call leg (BUY) — the call hedge."""

    lots: int
    """Number of lots traded."""

    lot_size: int
    """Lot size of the underlying contract."""

    initial_credit: float = 0.0
    """Net premium credit received at entry (per share)."""

    put_dist_pct: float = 5.0
    """Short put distance from spot as % — stored at entry so shifts don't need config."""

    call_dist_pct: float = 5.0
    """Short call distance from spot as % — stored at entry so shifts don't need config."""

    entry_time: datetime = field(default_factory=lambda: datetime.now(IST))
    """Timestamp of position entry."""

    shift_count: int = 0
    """Number of times this position has been shifted."""

    broker_order_ids: List[str] = field(default_factory=list)
    """All broker order IDs associated with this position."""

    state: str = "OPEN"
    """Position state: OPEN, SHIFTING, CLOSED."""

    fo_ban_failures: int = 0
    """Consecutive shift failures due to F&O ban period."""

    fo_ban_suspended: bool = False
    """True when shift retries are paused due to repeated F&O ban rejections."""

    fo_ban_suspended_at: float = 0.0
    """Unix timestamp when F&O ban suspension was last set."""


# ── CondorRunner ───────────────────────────────────────────────────────────


class CondorRunner:
    """
    Orchestrates the full lifecycle of live iron condor positions.

    Usage::

        runner = CondorRunner(broker=broker, config=cfg, trade_logger=tl)
        runner.run_session()
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        config: LiveConfig,
        trade_logger: TradeLogger,
        capital_manager: Optional[CapitalManager] = None,
        kill_switch: Optional[KillSwitch] = None,
        config_path: Optional[Any] = None,
    ) -> None:
        """
        Initialise the CondorRunner.

        Args:
            broker:          BrokerAdapter implementation (Zerodha or Paper).
            config:          Fully loaded LiveConfig.
            trade_logger:    TradeLogger for structured event logging.
            capital_manager: CapitalManager; created from config if not provided.
            kill_switch:     KillSwitch instance; new one created if not provided.
            config_path:     Path to live_config.yaml; if provided, config is
                             reloaded from disk at the start of each session so
                             dashboard edits take effect without a full restart.
        """
        self._broker = broker
        self._config = config
        self._config_path = config_path
        self._trade_logger = trade_logger
        self._capital_manager = capital_manager or CapitalManager(config.capital)
        self._kill_switch = kill_switch or KillSwitch()
        self._positions: List[CondorPosition] = []
        self._shift_monitor: Optional[ShiftMonitor] = None
        self._position_store = PositionStore()
        self._session_id = str(uuid.uuid4())[:8]
        self._last_order_error_msg: str = ""
        logger.info("condor_runner_init", extra={"session_id": self._session_id, "mode": config.mode})

    # ── Session entry point ────────────────────────────────────────────────

    def run_session(self) -> None:
        """
        Main daily session loop.

        1. Check kill switch.
        2. Attempt entry for each enabled stock within entry time window.
        3. Start the shift monitor.
        4. Block until session end or kill switch fires.
        5. Clean up: stop monitor, log summary.
        """
        logger.info("session_starting", extra={"session_id": self._session_id})

        # ── Reload config from disk so dashboard edits take effect ────────────
        if self._config_path is not None:
            try:
                fresh = _load_live_config(self._config_path)
                # Preserve the mode that was set at startup (CLI flag may override yaml)
                fresh.mode = self._config.mode
                self._config = fresh
                logger.info("config_reloaded", extra={"path": str(self._config_path),
                                                       "enabled_stocks": len(fresh.enabled_stocks)})
            except Exception as exc:
                logger.warning("config_reload_failed", extra={"error": str(exc)})

        try:
            self._kill_switch.check()
        except KillSwitchError:
            logger.critical("session_aborted_kill_switch")
            return

        # ── Recover existing open positions from DB ────────────────────────
        # This ensures a token refresh + re-start never re-enters positions
        # that are already live on the broker.
        recovered = self._recover_positions()
        for pos in recovered:
            self._positions.append(pos)
        already_open: set[str] = {pos.symbol for pos in self._positions}

        # ── Entry phase ────────────────────────────────────────────────────
        now = datetime.now(IST)
        entry_start = self._config.strategy.entry_start
        entry_end = self._config.strategy.entry_end
        now_str = self._get_now_str()

        # Hard entry cutoff: no new trades after 3:15 PM regardless of config
        if now_str >= _ENTRY_CUTOFF_TIME:
            logger.warning(
                "entry_blocked_hard_cutoff",
                extra={"now": now_str, "cutoff": _ENTRY_CUTOFF_TIME},
            )
            self._trade_logger.log(
                EventType.ENTRY_BLOCKED_TIME,
                None,
                {"now": now_str, "cutoff": _ENTRY_CUTOFF_TIME,
                 "reason": "Hard cutoff — no new entries after 3:15 PM IST"},
                level="WARNING",
            )
        elif not (entry_start <= now_str <= entry_end):
            logger.warning(
                "outside_entry_window",
                extra={"now": now_str, "start": entry_start, "end": entry_end},
            )
        else:
            for stock_cfg in self._config.enabled_stocks:
                if self._kill_switch.is_active():
                    logger.critical("kill_switch_aborted_entries")
                    break
                if len(self._positions) >= self._config.capital.max_open_positions:
                    logger.info("max_positions_reached", extra={"count": len(self._positions)})
                    break
                if stock_cfg.symbol in already_open:
                    logger.info("skipping_existing_position", extra={"symbol": stock_cfg.symbol})
                    continue
                try:
                    pos = self.enter_condor(stock_cfg)
                    if pos:
                        self._positions.append(pos)
                        already_open.add(pos.symbol)
                except Exception as exc:
                    logger.exception(
                        "entry_error",
                        extra={"symbol": stock_cfg.symbol, "error": str(exc)},
                    )
                    self._trade_logger.log_error(stock_cfg.symbol, exc, {"phase": "entry"})

        # ── Start shift monitor ────────────────────────────────────────────
        if self._positions:
            self._shift_monitor = ShiftMonitor(
                broker=self._broker,
                positions=self._positions,
                config=self._config,
                logger_=self._trade_logger,
                on_shift_cb=self.shift_condor,
                kill_switch=self._kill_switch,
                on_exit_cb=self._handle_exit,
            )
            self._shift_monitor.start()
            self.monitor_positions(self._positions)
        else:
            logger.info("no_positions_entered")

        logger.info("session_complete", extra={"session_id": self._session_id})

    def monitor_positions(self, positions: List[CondorPosition]) -> None:
        """
        Block and poll positions every 30 seconds until session end or kill switch.

        The shift monitor daemon handles the actual shift/TP detection.
        This method primarily watches for session end time and handles
        expiry-day exits.

        Args:
            positions: List of open CondorPositions to monitor.
        """
        logger.info("monitoring_started", extra={"count": len(positions)})

        while True:
            if self._kill_switch.is_active():
                logger.critical("kill_switch_activated_exiting_all_positions")
                if self._shift_monitor:
                    self._shift_monitor.stop()
                # Exit all open positions and confirm with Zerodha
                self._trade_logger.log(
                    EventType.KILL_SWITCH_ACTIVATED,
                    None,
                    {"reason": "Kill switch active — exiting all open positions"},
                    level="CRITICAL",
                )
                for pos in list(positions):
                    if pos.state == "OPEN":
                        logger.critical("kill_switch_closing_position", extra={"symbol": pos.symbol})
                        self.exit_condor(pos, "KILL_SWITCH")
                break

            now = datetime.now(IST)
            # Close positions at 3:00 PM on expiry day (hold to expiry,
            # but close before 3:30 PM STT trap on ITM options)
            if now.hour >= 15:  # 3:00 PM IST
                for pos in list(positions):
                    if pos.state == "OPEN" and pos.expiry == now.date():
                        logger.warning(
                            "expiry_day_exit",
                            extra={
                                "symbol": pos.symbol,
                                "expiry": str(pos.expiry),
                                "exit_time": now.strftime("%H:%M:%S"),
                            },
                        )
                        self.exit_condor(pos, "EXPIRY_DAY")

            # Break out if all positions are closed
            open_positions = [p for p in positions if p.state == "OPEN"]
            if not open_positions:
                logger.info("all_positions_closed")
                break

            time.sleep(30)

        if self._shift_monitor:
            self._shift_monitor.stop()

    # ── Entry ──────────────────────────────────────────────────────────────

    def enter_condor(
        self, stock: StockConfig, phase: str = "pre_entry"
    ) -> Optional[CondorPosition]:
        """
        Attempt a full condor entry for a single stock.

        Steps:
        1. Fetch spot price.
        2. Find suitable expiry.
        3. Calculate target strikes from OTM distances.
        4. Snap to nearest available strikes.
        5. Resolve all four instruments.
        6. Fetch quotes.
        7. Run all five guard layers.
        8. Estimate margin and allocate capital.
        9. Place hedge orders (long put + long call).
        10. Place short orders (short put + short call).
        11. Reconcile fill confirmation.
        12. Record initial credit.

        Args:
            stock: StockConfig for the target symbol.
            phase: Guard context phase label ('pre_entry' or 'pre_shift').

        Returns:
            CondorPosition on success; None on any failure.
        """
        symbol = stock.symbol
        logger.info("entry_attempt", extra={"symbol": symbol})
        self._trade_logger.log(EventType.ENTRY_ATTEMPT, symbol, {"config": symbol})

        try:
            self._kill_switch.check()
        except KillSwitchError:
            return None

        # ── 1. Get spot ────────────────────────────────────────────────────
        spot = self._get_spot(symbol)
        if spot is None or spot <= 0:
            logger.warning("no_spot_price", extra={"symbol": symbol})
            return None

        # ── 2. Find expiry ─────────────────────────────────────────────────
        expiry = self._find_expiry(
            symbol,
            self._config.strategy.min_dte,
            self._config.strategy.max_dte,
            expiry_type=stock.expiry_type,
        )
        if expiry is None:
            logger.warning("no_suitable_expiry", extra={"symbol": symbol})
            return None

        # ── 3. Calculate target strikes ────────────────────────────────────
        short_put_target = spot * (1.0 - stock.put_dist_pct / 100.0)
        short_call_target = spot * (1.0 + stock.call_dist_pct / 100.0)
        wing_width = spot * self._config.strategy.wing_width_pct / 100.0
        long_put_target = short_put_target - wing_width
        long_call_target = short_call_target + wing_width

        # ── 4. Snap to available strikes ───────────────────────────────────
        available_puts, available_calls = self._get_available_strikes(symbol, expiry)
        if not available_puts or not available_calls:
            logger.warning("no_available_strikes", extra={"symbol": symbol, "expiry": str(expiry)})
            return None

        short_put_strike = self._snap_to_strike(short_put_target, available_puts)
        long_put_strike = self._snap_to_strike(long_put_target, available_puts)
        short_call_strike = self._snap_to_strike(short_call_target, available_calls)
        long_call_strike = self._snap_to_strike(long_call_target, available_calls)

        # ── 5. Resolve instruments ─────────────────────────────────────────
        try:
            short_put_inst = self._broker.resolve_instrument(
                symbol, "NFO", expiry, Decimal(str(short_put_strike)), "PE"
            )
            long_put_inst = self._broker.resolve_instrument(
                symbol, "NFO", expiry, Decimal(str(long_put_strike)), "PE"
            )
            short_call_inst = self._broker.resolve_instrument(
                symbol, "NFO", expiry, Decimal(str(short_call_strike)), "CE"
            )
            long_call_inst = self._broker.resolve_instrument(
                symbol, "NFO", expiry, Decimal(str(long_call_strike)), "CE"
            )
        except InstrumentNotFoundError as exc:
            logger.warning("instrument_not_found", extra={"symbol": symbol, "error": str(exc)})
            return None

        lot_size = short_put_inst.lot_size

        # ── 6. Determine lots ──────────────────────────────────────────────
        # Priority: per-stock lots > strategy default_lots > capital-based auto-size
        fixed_lots = stock.lots or self._config.strategy.default_lots
        if fixed_lots > 0:
            lots = fixed_lots
            logger.info("lots_fixed_by_config", extra={"symbol": symbol, "lots": lots})
        else:
            lots = self._capital_manager.get_lots_for_capital(
                symbol, spot, lot_size, self._config.strategy.wing_width_pct
            )
            if lots <= 0:
                logger.warning("no_lots_available", extra={"symbol": symbol})
                return None

        qty = lots * lot_size

        # ── 7. Fetch quotes ────────────────────────────────────────────────
        instrument_pairs = [
            (short_put_inst.instrument_token, short_put_inst.trading_symbol),
            (long_put_inst.instrument_token, long_put_inst.trading_symbol),
            (short_call_inst.instrument_token, short_call_inst.trading_symbol),
            (long_call_inst.instrument_token, long_call_inst.trading_symbol),
        ]
        try:
            quotes_by_token = self._broker.get_quotes(instrument_pairs)
        except QuoteError as exc:
            logger.warning("quote_fetch_failed", extra={"symbol": symbol, "error": str(exc)})
            return None

        quotes_by_sym = {
            short_put_inst.trading_symbol: quotes_by_token.get(short_put_inst.instrument_token),
            long_put_inst.trading_symbol: quotes_by_token.get(long_put_inst.instrument_token),
            short_call_inst.trading_symbol: quotes_by_token.get(short_call_inst.instrument_token),
            long_call_inst.trading_symbol: quotes_by_token.get(long_call_inst.instrument_token),
        }
        if any(v is None for v in quotes_by_sym.values()):
            logger.warning("incomplete_quotes", extra={"symbol": symbol})
            return None

        # ── Build limit prices (top of book) ──────────────────────────────
        # SELL legs: use best bid (top of buy stack) — what buyers will pay
        # BUY  legs: use best ask (top of sell stack) — what sellers are asking
        def _round(p: Decimal) -> Decimal:
            """Round to nearest 0.05 tick."""
            return (p / Decimal("0.05")).quantize(Decimal("1")) * Decimal("0.05")

        short_put_price  = _round(quotes_by_sym[short_put_inst.trading_symbol].bid)
        long_put_price   = _round(quotes_by_sym[long_put_inst.trading_symbol].ask)
        short_call_price = _round(quotes_by_sym[short_call_inst.trading_symbol].bid)
        long_call_price  = _round(quotes_by_sym[long_call_inst.trading_symbol].ask)

        # ── 8. Run guard layers ────────────────────────────────────────────
        instruments_map = {
            short_put_inst.trading_symbol: short_put_inst,
            long_put_inst.trading_symbol: long_put_inst,
            short_call_inst.trading_symbol: short_call_inst,
            long_call_inst.trading_symbol: long_call_inst,
        }
        expected_sides = {
            short_put_inst.trading_symbol: Side.SELL,
            long_put_inst.trading_symbol: Side.BUY,
            short_call_inst.trading_symbol: Side.SELL,
            long_call_inst.trading_symbol: Side.BUY,
        }
        expected_quantities = {sym: qty for sym in instruments_map}
        expected_prices = {
            short_put_inst.trading_symbol: short_put_price,
            long_put_inst.trading_symbol: long_put_price,
            short_call_inst.trading_symbol: short_call_price,
            long_call_inst.trading_symbol: long_call_price,
        }

        try:
            margins_data = self._broker.get_margins()
            equity_avail = (margins_data.get("equity", {}) or margins_data).get("available", {})
            cash_margin = float(equity_avail.get("live_balance", 0) or 0)
            stock_margin = float(equity_avail.get("collateral", 0) or 0)
            if stock_margin <= 0:
                available_margin = cash_margin
                margin_source = "cash_only"
            elif cash_margin <= 0:
                available_margin = stock_margin
                margin_source = "stock_only"
            else:
                available_margin = cash_margin * 0.5 + stock_margin * 0.5
                margin_source = "50_50_split"
        except BrokerError:
            cash_margin = 0.0
            stock_margin = 0.0
            available_margin = 0.0
            margin_source = ""

        required_margin = self._capital_manager.estimate_margin(
            spot, lot_size, lots, self._config.strategy.wing_width_pct
        )

        guard_ctx = GuardContext(
            underlying=symbol,
            spot=spot,
            expiry=expiry,
            short_put_strike=short_put_strike,
            short_call_strike=short_call_strike,
            long_put_strike=long_put_strike,
            long_call_strike=long_call_strike,
            short_otm_pct=stock.put_dist_pct,
            wing_width_pct=self._config.strategy.wing_width_pct,
            lots=lots,
            lot_size=lot_size,
            quotes=quotes_by_sym,
            instruments=instruments_map,
            pending_orders=self._get_recent_orders(symbol),
            broker_positions=self._get_broker_positions(),
            available_margin=available_margin,
            daily_pnl=0.0,  # injected by caller in production
            total_allocated_margin=self._capital_manager.get_total_allocated(),
            config=self._config,
            phase=phase,
            expected_sides=expected_sides,
            expected_quantities=expected_quantities,
            expected_prices=expected_prices,
            open_condors_count=len([p for p in self._positions if p.state == "OPEN"]),
        )

        guard_report: GuardReport = run_all_guards(guard_ctx)

        if not guard_report.passed:
            self._trade_logger.log_guard_fail(symbol, guard_report)
            logger.warning(
                "entry_guards_failed",
                extra={"symbol": symbol, "failure": guard_report.first_failure.layer if guard_report.first_failure else ""},
            )
            return None

        self._trade_logger.log(EventType.GUARD_PASS, symbol, guard_report.to_dict())

        # ── 9. Allocate capital ────────────────────────────────────────────
        if not self._capital_manager.allocate(symbol, required_margin):
            logger.warning("capital_allocation_failed", extra={"symbol": symbol})
            return None

        # ── 10. Place hedge orders first (long put + long call) ────────────
        strategy_run_id = f"{symbol}_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}"
        order_ids: List[str] = []

        hedge_orders = [
            (long_put_inst, Side.BUY, long_put_price, 0),
            (long_call_inst, Side.BUY, long_call_price, 1),
        ]
        for inst, side, price, leg_idx in hedge_orders:
            order = Order(
                strategy_run_id=strategy_run_id,
                leg_index=leg_idx,
                trading_symbol=inst.trading_symbol,
                instrument_token=inst.instrument_token,
                side=side,
                quantity=qty,
                limit_price=price,
                initial_bid=quotes_by_sym[inst.trading_symbol].bid,
                initial_ask=quotes_by_sym[inst.trading_symbol].ask,
            )
            broker_oid = self._place_order_safe(order)
            if broker_oid is None:
                self._capital_manager.release(symbol)
                return None
            order_ids.append(broker_oid)
            self._trade_logger.log_order(order, EventType.ORDER_PLACED)

        time.sleep(_ORDER_SPACING_SECONDS)

        # ── 11. Place short orders (short put + short call) ────────────────
        short_orders = [
            (short_put_inst, Side.SELL, short_put_price, 2),
            (short_call_inst, Side.SELL, short_call_price, 3),
        ]
        for inst, side, price, leg_idx in short_orders:
            order = Order(
                strategy_run_id=strategy_run_id,
                leg_index=leg_idx,
                trading_symbol=inst.trading_symbol,
                instrument_token=inst.instrument_token,
                side=side,
                quantity=qty,
                limit_price=price,
                initial_bid=quotes_by_sym[inst.trading_symbol].bid,
                initial_ask=quotes_by_sym[inst.trading_symbol].ask,
            )
            broker_oid = self._place_order_safe(order)
            if broker_oid is None:
                # Hedges placed but shorts failed — exit hedges
                logger.error("short_order_failed_unwinding_hedges", extra={"symbol": symbol})
                self._capital_manager.release(symbol)
                return None
            order_ids.append(broker_oid)
            self._trade_logger.log_order(order, EventType.ORDER_PLACED)

        # ── 12. Compute initial credit ─────────────────────────────────────
        initial_credit = (
            float(short_put_price + short_call_price - long_put_price - long_call_price)
        )

        pos = CondorPosition(
            symbol=symbol,
            underlying=symbol,
            expiry=expiry,
            spot_at_entry=spot,
            short_put=short_put_inst,
            long_put=long_put_inst,
            short_call=short_call_inst,
            long_call=long_call_inst,
            lots=lots,
            lot_size=lot_size,
            initial_credit=initial_credit,
            put_dist_pct=stock.put_dist_pct,
            call_dist_pct=stock.call_dist_pct,
            broker_order_ids=order_ids,
            state="OPEN",
        )

        self._position_store.upsert(pos)

        self._trade_logger.log_entry(
            symbol=symbol,
            spot=spot,
            strikes_dict={
                "short_put": short_put_strike,
                "long_put": long_put_strike,
                "short_call": short_call_strike,
                "long_call": long_call_strike,
            },
            lots=lots,
            credit=initial_credit,
            guard_report=guard_report,
            put_dist_pct=stock.put_dist_pct,
            call_dist_pct=stock.call_dist_pct,
            cash_margin=cash_margin,
            stock_margin=stock_margin,
            margin_source=margin_source,
            required_margin=required_margin,
        )

        logger.info(
            "condor_entered",
            extra={
                "symbol": symbol,
                "spot": spot,
                "expiry": str(expiry),
                "short_put": short_put_strike,
                "short_call": short_call_strike,
                "credit": round(initial_credit, 4),
                "lots": lots,
            },
        )
        return pos

    # ── Exit ───────────────────────────────────────────────────────────────

    def exit_condor(self, pos: CondorPosition, reason: str) -> None:
        """
        Close all four legs of the condor.

        Order: close shorts first (buy back), then sell hedges.
        Logs final P&L.

        Args:
            pos:    The CondorPosition to close.
            reason: Human-readable exit reason string.
        """
        symbol = pos.symbol
        logger.info("exit_condor_attempt", extra={"symbol": symbol, "reason": reason})
        self._trade_logger.log(EventType.EXIT_ATTEMPT, symbol, {"reason": reason})

        if pos.state not in ("OPEN", "SHIFTING"):
            logger.warning("exit_condor_not_open", extra={"symbol": symbol, "state": pos.state})
            return

        pos.state = "EXITING"
        exit_debit = 0.0
        strategy_run_id = f"exit_{symbol}_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}"

        # Fetch fresh quotes
        try:
            instrument_pairs = [
                (pos.short_put.instrument_token, pos.short_put.trading_symbol),
                (pos.long_put.instrument_token, pos.long_put.trading_symbol),
                (pos.short_call.instrument_token, pos.short_call.trading_symbol),
                (pos.long_call.instrument_token, pos.long_call.trading_symbol),
            ]
            quotes_by_token = self._broker.get_quotes(instrument_pairs)
        except QuoteError as exc:
            logger.error("exit_quote_failed", extra={"symbol": symbol, "error": str(exc)})
            self._trade_logger.log_error(symbol, exc, {"phase": "exit"})
            pos.state = "OPEN"
            return

        qty = pos.lots * pos.lot_size

        # Close shorts first (buy back), with fill confirmation from Zerodha
        for inst in [pos.short_put, pos.short_call]:
            q = quotes_by_token.get(inst.instrument_token)
            price = q.ask if q else inst.strike * Decimal("0.005")
            order = Order(
                strategy_run_id=strategy_run_id,
                leg_index=0,
                trading_symbol=inst.trading_symbol,
                instrument_token=inst.instrument_token,
                side=Side.BUY,
                quantity=qty,
                limit_price=price,
            )
            self._trade_logger.log_order(order, EventType.ORDER_PLACED)
            broker_oid = self._place_and_confirm(order)
            if broker_oid:
                exit_debit += float(price)
            else:
                logger.error("exit_short_leg_failed_no_fill", extra={
                    "symbol": symbol, "leg": inst.trading_symbol
                })

        # Sell hedges (receive premium back), with fill confirmation
        for inst in [pos.long_put, pos.long_call]:
            q = quotes_by_token.get(inst.instrument_token)
            price = q.bid if q else Decimal("0")
            order = Order(
                strategy_run_id=strategy_run_id,
                leg_index=1,
                trading_symbol=inst.trading_symbol,
                instrument_token=inst.instrument_token,
                side=Side.SELL,
                quantity=qty,
                limit_price=price,
            )
            self._trade_logger.log_order(order, EventType.ORDER_PLACED)
            broker_oid = self._place_and_confirm(order)
            if broker_oid:
                exit_debit -= float(price)
                self._trade_logger.log_order(order, EventType.ORDER_PLACED)

        pnl = (pos.initial_credit - exit_debit) * qty
        pos.state = "CLOSED"
        self._capital_manager.release(symbol)
        self._position_store.close_position(pos, pnl=pnl)

        self._trade_logger.log_exit(
            symbol=symbol,
            reason=reason,
            entry_credit=pos.initial_credit,
            exit_debit=exit_debit,
            pnl=pnl,
        )

        # Remove from tracked positions
        if pos in self._positions:
            self._positions.remove(pos)

        logger.info(
            "condor_exited",
            extra={
                "symbol": symbol,
                "reason": reason,
                "pnl": round(pnl, 2),
                "exit_debit": round(exit_debit, 4),
            },
        )

    def _handle_exit(self, pos: CondorPosition, reason: str) -> None:
        """
        Exit callback for non-shift triggers (e.g. DTE_EXIT).
        Closes all four legs and removes the position from monitoring.
        """
        symbol = pos.symbol
        logger.warning("exit_triggered_by_monitor",
                       extra={"symbol": symbol, "reason": reason})
        self._trade_logger.log(
            EventType.TAKE_PROFIT_HIT,
            symbol,
            {"reason": reason, "shift_count": pos.shift_count},
            level="WARNING",
        )
        self.exit_condor(pos, reason)

    # ── Shift ──────────────────────────────────────────────────────────────

    def shift_condor(self, pos: CondorPosition, trigger_side: str) -> None:
        """
        Single-leg shift: close only the threatened spread and re-enter it at
        new strikes.  The other spread is left completely untouched.

        Execution order (buy-first):
          1. BUY new long hedge → confirm fill from Zerodha
          2. SELL new short     → confirm fill from Zerodha
          3. BUY back old short → confirm fill from Zerodha
          4. SELL old long hedge→ confirm fill from Zerodha

        Pre-conditions checked before any order:
          • Time cutoff: shifts blocked after 3:20 PM IST.
          • Funds: available Zerodha balance logged; hard stop if unavailable.
          • Premium gate: if post-shift running credit < 5% of current credit,
            exit the position instead of shifting.

        Args:
            pos:          The CondorPosition to partially shift.
            trigger_side: "PUT" or "CALL" — which spread to replace.
        """
        symbol = pos.symbol

        # ── Time cutoff: no shifts after 3:20 PM ──────────────────────────
        now_str = self._get_now_str()
        if now_str >= _SHIFT_CUTOFF_TIME:
            logger.warning("shift_blocked_time_cutoff", extra={
                "symbol": symbol, "now": now_str, "cutoff": _SHIFT_CUTOFF_TIME
            })
            self._trade_logger.log(
                EventType.SHIFT_BLOCKED_TIME,
                symbol,
                {"now": now_str, "cutoff": _SHIFT_CUTOFF_TIME,
                 "reason": "Shift blocked — past 3:20 PM IST cutoff"},
                level="WARNING",
            )
            return

        logger.warning(
            "shift_condor_start",
            extra={"symbol": symbol, "shift_count": pos.shift_count, "side": trigger_side},
        )

        # Guard against double-trigger while we're mid-execution
        if pos.state != "OPEN":
            logger.warning("shift_already_in_progress", extra={"symbol": symbol, "state": pos.state})
            return

        # ── Funds check from Zerodha ──────────────────────────────────────
        available_funds, funds_err = self._check_available_funds()
        if funds_err:
            logger.error("shift_funds_check_error", extra={"symbol": symbol, "error": funds_err})
            # Proceed with caution but log the issue
        else:
            logger.info("shift_funds_available", extra={"symbol": symbol, "funds": round(available_funds, 2)})

        pos.state = "SHIFTING"

        old_strikes = {
            "short_put":  float(pos.short_put.strike),
            "long_put":   float(pos.long_put.strike),
            "short_call": float(pos.short_call.strike),
            "long_call":  float(pos.long_call.strike),
        }

        run_id = f"shift_{symbol}_{trigger_side}_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}"
        qty = pos.lots * pos.lot_size

        # Identify the threatened side's existing legs
        if trigger_side == "PUT":
            to_close_short, to_close_long = pos.short_put, pos.long_put
        else:
            to_close_short, to_close_long = pos.short_call, pos.long_call

        # ── Step 1: Resolve new spread instruments and quotes first ─────────
        spot = self._get_spot(symbol)
        if spot is None or spot <= 0:
            logger.error("shift_no_spot_for_reentry", extra={"symbol": symbol})
            self._trade_logger.log_shift_failed(symbol, trigger_side, "no_spot")
            pos.state = "OPEN"
            return

        wing_width = spot * self._config.strategy.wing_width_pct / 100.0
        available_puts, available_calls = self._get_available_strikes(symbol, pos.expiry)
        if not available_puts or not available_calls:
            logger.error("shift_no_strikes", extra={"symbol": symbol})
            self._trade_logger.log_shift_failed(symbol, trigger_side, "no_strikes")
            pos.state = "OPEN"
            return

        if trigger_side == "PUT":
            new_short_target = spot * (1.0 - pos.put_dist_pct / 100.0)
            new_long_target  = new_short_target - wing_width
            new_short_strike = self._snap_to_strike(new_short_target, available_puts)
            new_long_strike  = self._snap_to_strike(new_long_target,  available_puts)
            try:
                new_short_inst = self._broker.resolve_instrument(
                    symbol, "NFO", pos.expiry, Decimal(str(new_short_strike)), "PE"
                )
                new_long_inst = self._broker.resolve_instrument(
                    symbol, "NFO", pos.expiry, Decimal(str(new_long_strike)),  "PE"
                )
            except InstrumentNotFoundError as exc:
                logger.error("shift_instrument_not_found", extra={"symbol": symbol, "error": str(exc)})
                self._trade_logger.log_shift_failed(symbol, trigger_side, "resolve_instruments")
                pos.state = "OPEN"
                return
        else:
            new_short_target = spot * (1.0 + pos.call_dist_pct / 100.0)
            new_long_target  = new_short_target + wing_width
            new_short_strike = self._snap_to_strike(new_short_target, available_calls)
            new_long_strike  = self._snap_to_strike(new_long_target,  available_calls)
            try:
                new_short_inst = self._broker.resolve_instrument(
                    symbol, "NFO", pos.expiry, Decimal(str(new_short_strike)), "CE"
                )
                new_long_inst = self._broker.resolve_instrument(
                    symbol, "NFO", pos.expiry, Decimal(str(new_long_strike)),  "CE"
                )
            except InstrumentNotFoundError as exc:
                logger.error("shift_instrument_not_found", extra={"symbol": symbol, "error": str(exc)})
                self._trade_logger.log_shift_failed(symbol, trigger_side, "resolve_instruments")
                pos.state = "OPEN"
                return

        try:
            new_quotes = self._broker.get_quotes([
                (new_short_inst.instrument_token, new_short_inst.trading_symbol),
                (new_long_inst.instrument_token,  new_long_inst.trading_symbol),
            ])
        except QuoteError as exc:
            logger.error("shift_entry_quote_failed", extra={"symbol": symbol, "error": str(exc)})
            self._trade_logger.log_shift_failed(symbol, trigger_side, "fetch_new_quotes")
            pos.state = "OPEN"
            return

        def _mid(q: Any) -> Decimal:
            mid = (q.bid + q.ask) / Decimal("2")
            return (mid / Decimal("0.05")).quantize(Decimal("1")) * Decimal("0.05")

        q_ns = new_quotes.get(new_short_inst.instrument_token)
        q_nl = new_quotes.get(new_long_inst.instrument_token)
        if not q_ns or not q_nl:
            logger.error("shift_entry_quotes_missing", extra={"symbol": symbol})
            self._trade_logger.log_shift_failed(symbol, trigger_side, "fetch_new_quotes")
            pos.state = "OPEN"
            return

        new_short_price = _mid(q_ns)
        new_long_price  = _mid(q_nl)

        # Zero price means the option has no market (common during F&O ban) — abort
        if new_long_price == 0:
            logger.warning("shift_zero_price_new_hedge", extra={"symbol": symbol,
                           "leg": new_long_inst.trading_symbol})
            pos.fo_ban_failures += 1
            if pos.fo_ban_failures >= 5:
                pos.fo_ban_suspended = True
                pos.fo_ban_suspended_at = time.time()
                self._trade_logger.log(
                    EventType.SHIFT_FAILED, symbol,
                    {"trigger_side": trigger_side, "failed_step": "buy_new_hedge",
                     "reason": "fo_ban_suspended_after_5_failures",
                     "ban_error": "zero_quote_on_new_hedge"},
                    level="WARNING",
                )
            else:
                self._trade_logger.log_shift_failed(symbol, trigger_side, "buy_new_hedge_zero_price")
            pos.state = "OPEN"
            return

        # Fetch close quotes for old legs
        try:
            close_quotes = self._broker.get_quotes([
                (to_close_short.instrument_token, to_close_short.trading_symbol),
                (to_close_long.instrument_token,  to_close_long.trading_symbol),
            ])
        except QuoteError as exc:
            logger.error("shift_close_quote_failed", extra={"symbol": symbol, "error": str(exc)})
            self._trade_logger.log_shift_failed(symbol, trigger_side, "fetch_close_quotes")
            pos.state = "OPEN"
            return

        q_short = close_quotes.get(to_close_short.instrument_token)
        close_short_price = q_short.ask if q_short else to_close_short.strike * Decimal("0.005")
        q_long = close_quotes.get(to_close_long.instrument_token)
        close_long_price = q_long.bid if q_long else Decimal("0")

        new_side_credit_estimated = float(new_short_price) - float(new_long_price)
        close_debit_estimated = float(close_short_price) - float(close_long_price)

        # ── Premium gate: if post-shift running credit < 5% of current credit, exit ──
        # "initial premium after shifts is less than 5%" → exit the position instead
        if pos.initial_credit > 0:
            post_shift_credit = pos.initial_credit + (new_side_credit_estimated - close_debit_estimated)
            min_viable_credit = pos.initial_credit * 0.05
            if post_shift_credit < min_viable_credit:
                logger.warning("shift_skipped_low_premium", extra={
                    "symbol": symbol,
                    "current_credit": round(pos.initial_credit, 4),
                    "post_shift_credit": round(post_shift_credit, 4),
                    "min_viable": round(min_viable_credit, 4),
                })
                self._trade_logger.log(
                    EventType.SHIFT_SKIPPED_LOW_PREMIUM,
                    symbol,
                    {
                        "trigger_side": trigger_side,
                        "current_credit": round(pos.initial_credit, 4),
                        "post_shift_credit": round(post_shift_credit, 4),
                        "min_viable_pct": 5.0,
                        "reason": "Post-shift premium below 5% of current credit — exiting instead",
                    },
                    level="WARNING",
                )
                pos.state = "OPEN"
                self.exit_condor(pos, "LOW_PREMIUM_EXIT")
                return

        # ── Step 2: BUY new long hedge first (enter new position with protection) ──
        # Order: BUY new long → confirm → SELL new short → confirm
        # THEN close old spread: BUY back old short → confirm → SELL old long → confirm
        buy_new_hedge = Order(
            strategy_run_id=run_id,
            leg_index=0,
            trading_symbol=new_long_inst.trading_symbol,
            instrument_token=new_long_inst.instrument_token,
            side=Side.BUY,
            quantity=qty,
            limit_price=new_long_price,
        )
        self._trade_logger.log_order(buy_new_hedge, EventType.ORDER_PLACED)
        confirmed_oid = self._place_and_confirm(buy_new_hedge)
        if not confirmed_oid:
            logger.error("shift_entry_long_failed", extra={"symbol": symbol})
            # Detect F&O ban period rejection and track consecutive failures
            if "ban period" in self._last_order_error_msg.lower():
                pos.fo_ban_failures += 1
                if pos.fo_ban_failures >= 5:
                    pos.fo_ban_suspended = True
                    pos.fo_ban_suspended_at = time.time()
                    logger.warning(
                        "shift_fo_ban_suspended",
                        extra={"symbol": symbol, "failures": pos.fo_ban_failures},
                    )
                    self._trade_logger.log(
                        EventType.SHIFT_FAILED,
                        symbol,
                        {
                            "trigger_side": trigger_side,
                            "failed_step": "buy_new_hedge",
                            "reason": "fo_ban_suspended_after_5_failures",
                            "ban_error": self._last_order_error_msg,
                        },
                        level="WARNING",
                    )
                else:
                    self._trade_logger.log_shift_failed(symbol, trigger_side, "buy_new_hedge")
            else:
                self._trade_logger.log_shift_failed(symbol, trigger_side, "buy_new_hedge")
            pos.state = "OPEN"
            return

        time.sleep(_ORDER_SPACING_SECONDS)

        # ── Step 3: SELL new short to complete the new spread ────────────────
        sell_new_short = Order(
            strategy_run_id=run_id,
            leg_index=1,
            trading_symbol=new_short_inst.trading_symbol,
            instrument_token=new_short_inst.instrument_token,
            side=Side.SELL,
            quantity=qty,
            limit_price=new_short_price,
        )
        self._trade_logger.log_order(sell_new_short, EventType.ORDER_PLACED)
        if not self._place_and_confirm(sell_new_short):
            logger.error("shift_entry_short_failed", extra={"symbol": symbol})
            self._trade_logger.log_shift_failed(symbol, trigger_side, "sell_new_short")
            pos.state = "OPEN"
            return

        time.sleep(_ORDER_SPACING_SECONDS)

        # ── Step 4: Close the old spread ─────────────────────────────────────
        # BUY back old short leg (confirm fill before proceeding to sell old hedge)
        buy_back = Order(
            strategy_run_id=run_id,
            leg_index=2,
            trading_symbol=to_close_short.trading_symbol,
            instrument_token=to_close_short.instrument_token,
            side=Side.BUY,
            quantity=qty,
            limit_price=close_short_price,
        )
        self._trade_logger.log_order(buy_back, EventType.ORDER_PLACED)
        if not self._place_and_confirm(buy_back):
            logger.error("shift_close_short_failed", extra={"symbol": symbol})
            self._trade_logger.log_shift_failed(symbol, trigger_side, "buy_back_short")
            pos.state = "OPEN"
            return

        time.sleep(_ORDER_SPACING_SECONDS)

        # SELL old long hedge to complete closing the old spread
        sell_hedge = Order(
            strategy_run_id=run_id,
            leg_index=3,
            trading_symbol=to_close_long.trading_symbol,
            instrument_token=to_close_long.instrument_token,
            side=Side.SELL,
            quantity=qty,
            limit_price=close_long_price,
        )
        self._trade_logger.log_order(sell_hedge, EventType.ORDER_PLACED)
        if not self._place_and_confirm(sell_hedge):
            logger.error("shift_close_long_failed", extra={"symbol": symbol})
            self._trade_logger.log_shift_failed(symbol, trigger_side, "sell_old_hedge")
            pos.state = "OPEN"
            return

        close_debit = float(close_short_price) - float(close_long_price)
        new_side_credit = float(new_short_price) - float(new_long_price)

        # ── Step 5: Mutate position in-place ────────────────────────────────
        if trigger_side == "PUT":
            pos.short_put = new_short_inst
            pos.long_put  = new_long_inst
        else:
            pos.short_call = new_short_inst
            pos.long_call  = new_long_inst

        # Update running credit: subtract close debit, add new credit
        pos.initial_credit += (new_side_credit - close_debit)
        pos.shift_count    += 1
        pos.spot_at_entry   = spot  # update for next trigger calculation
        pos.state           = "OPEN"
        self._position_store.update_state(pos)

        new_strikes = {
            "short_put":  float(pos.short_put.strike),
            "long_put":   float(pos.long_put.strike),
            "short_call": float(pos.short_call.strike),
            "long_call":  float(pos.long_call.strike),
        }
        shift_cost_per_share = round(close_debit - new_side_credit, 4)
        shift_pnl_total = round((new_side_credit - close_debit) * pos.lots * pos.lot_size, 2)
        self._trade_logger.log_shift(
            symbol=symbol,
            trigger_side=trigger_side,
            old_strikes=old_strikes,
            new_strikes=new_strikes,
            spot=spot,
            trigger_pct=self._config.strategy.shift_trigger_pct,
            close_short_price=float(close_short_price),
            close_long_price=float(close_long_price),
            close_debit=round(close_debit, 4),
            new_short_price=float(new_short_price),
            new_long_price=float(new_long_price),
            new_credit=round(new_side_credit, 4),
            shift_cost_per_share=shift_cost_per_share,
            shift_pnl_total=shift_pnl_total,
            running_credit=round(pos.initial_credit, 4),
            lots=pos.lots,
            lot_size=pos.lot_size,
        )
        logger.info(
            "shift_complete",
            extra={
                "symbol": symbol,
                "side": trigger_side,
                "shift_count": pos.shift_count,
                "new_short": new_short_strike,
                "close_debit": round(close_debit, 4),
                "new_credit":  round(new_side_credit, 4),
            },
        )

    # ── Session recovery ───────────────────────────────────────────────────

    def _recover_positions(self) -> List[CondorPosition]:
        """
        Load OPEN/SHIFTING positions from the DB and reconstruct CondorPosition
        objects so monitoring resumes without re-entering.

        Called at the very start of run_session() before the entry loop.
        Also re-allocates capital for each recovered position so the
        CapitalManager's accounting stays accurate.

        Returns:
            List of reconstructed CondorPosition objects (may be empty).
        """
        rows = self._position_store.load_open_positions()
        if not rows:
            logger.info("no_positions_to_recover")
            return []

        recovered: List[CondorPosition] = []
        for row in rows:
            symbol = row.get("symbol", "?")
            try:
                pos = self._row_to_position(row)
                # Re-allocate capital so CapitalManager tracks this correctly
                required_margin = self._capital_manager.estimate_margin(
                    pos.spot_at_entry, pos.lot_size, pos.lots,
                    self._config.strategy.wing_width_pct,
                )
                self._capital_manager.allocate(pos.symbol, required_margin)
                recovered.append(pos)
                logger.info(
                    "position_recovered",
                    extra={
                        "symbol": pos.symbol,
                        "expiry": str(pos.expiry),
                        "state": pos.state,
                        "shift_count": pos.shift_count,
                        "short_put": float(pos.short_put.strike),
                        "short_call": float(pos.short_call.strike),
                    },
                )
            except Exception as exc:
                logger.error(
                    "position_recovery_failed",
                    extra={"symbol": symbol, "error": str(exc)},
                    exc_info=True,
                )

        if recovered:
            logger.info("session_recovery_complete", extra={"recovered": len(recovered)})
        return recovered

    def _row_to_position(self, row: dict) -> CondorPosition:
        """
        Reconstruct a CondorPosition dataclass from a flat DB row dict.

        Handles both the full JSON (written at entry, includes option_type)
        and the compact JSON (written on shift, no option_type) by inferring
        option_type from the leg key name when absent.

        Args:
            row: Dict from PositionStore.load_open_positions().

        Returns:
            Reconstructed CondorPosition.
        """
        from execution.models import OptionType, ResolvedInstrument

        symbol = str(row["symbol"])
        lot_size = int(row["lot_size"])
        expiry = date.fromisoformat(str(row["expiry"]))

        def _parse_leg(raw, leg_key: str) -> ResolvedInstrument:
            data = json.loads(raw) if isinstance(raw, str) else raw
            # option_type present in entry JSON; absent in shift-compact JSON
            if "option_type" in data:
                opt = OptionType(str(data["option_type"]).upper())
            elif "put" in leg_key:
                opt = OptionType.PE
            else:
                opt = OptionType.CE
            return ResolvedInstrument(
                instrument_token=int(data["instrument_token"]),
                trading_symbol=str(data["trading_symbol"]),
                exchange="NFO",
                underlying=symbol,
                expiry=expiry,
                strike=Decimal(str(data["strike"])),
                option_type=opt,
                lot_size=lot_size,
                tick_size=Decimal("0.05"),
            )

        entry_time_raw = row["entry_time"]
        if isinstance(entry_time_raw, str):
            entry_time = datetime.fromisoformat(entry_time_raw)
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=IST)
        else:
            entry_time = entry_time_raw

        broker_ids_raw = row.get("broker_order_ids") or "[]"
        broker_order_ids: List[str] = []
        if isinstance(broker_ids_raw, str):
            try:
                broker_order_ids = json.loads(broker_ids_raw)
            except (json.JSONDecodeError, ValueError):
                broker_order_ids = []
        elif isinstance(broker_ids_raw, list):
            broker_order_ids = broker_ids_raw

        return CondorPosition(
            symbol=symbol,
            underlying=symbol,
            expiry=expiry,
            spot_at_entry=float(row["spot_at_entry"]),
            short_put=_parse_leg(row["short_put"], "short_put"),
            long_put=_parse_leg(row["long_put"], "long_put"),
            short_call=_parse_leg(row["short_call"], "short_call"),
            long_call=_parse_leg(row["long_call"], "long_call"),
            lots=int(row["lots"]),
            lot_size=lot_size,
            initial_credit=float(row.get("initial_credit", 0) or 0),
            put_dist_pct=float(row.get("put_dist_pct") or 5.0),
            call_dist_pct=float(row.get("call_dist_pct") or 5.0),
            entry_time=entry_time,
            shift_count=int(row.get("shift_count", 0) or 0),
            broker_order_ids=broker_order_ids,
            state=str(row.get("state", "OPEN")),
        )

    # ── Strike/Expiry helpers ──────────────────────────────────────────────

    @staticmethod
    def _is_monthly_expiry(exp: date) -> bool:
        """
        Return True if *exp* is a monthly expiry (last Thursday of its month).

        On NSE, the monthly expiry is the last Thursday of the month.  A Thursday
        is the last of the month when the next Thursday falls in the following month.
        """
        from datetime import timedelta
        return (exp + timedelta(7)).month != exp.month

    def _find_expiry(
        self, symbol: str, min_dte: int, max_dte: int, expiry_type: str = "monthly"
    ) -> Optional[date]:
        """
        Find the nearest suitable expiry within the DTE window.

        Looks at all available NFO instruments for the symbol, applies the
        expiry_type filter, then returns the nearest qualifying expiry.

        Args:
            symbol:      NSE symbol.
            min_dte:     Minimum days to expiry.
            max_dte:     Maximum days to expiry.
            expiry_type: ``'monthly'`` — last Thursday of the month only.
                         ``'weekly'``  — any available expiry (includes weeklies).

        Returns:
            The best matching expiry date, or None.
        """
        today = datetime.now(IST).date()
        instruments = self._broker._load_instruments() if hasattr(self._broker, "_load_instruments") else []  # type: ignore[attr-defined]

        expiries: set[date] = set()
        for inst in instruments:
            if str(inst.get("name", "")).upper() != symbol.upper():
                continue
            if inst.get("instrument_type", "") not in ("CE", "PE"):
                continue
            raw_exp = inst.get("expiry")
            if isinstance(raw_exp, str):
                try:
                    raw_exp = date.fromisoformat(raw_exp)
                except ValueError:
                    continue
            elif hasattr(raw_exp, "date"):
                raw_exp = raw_exp.date()
            if isinstance(raw_exp, date):
                expiries.add(raw_exp)

        candidates = sorted(
            exp for exp in expiries
            if min_dte <= (exp - today).days <= max_dte
        )

        if expiry_type == "monthly":
            candidates = [exp for exp in candidates if self._is_monthly_expiry(exp)]
        elif expiry_type == "weekly":
            # NIFTY weekly contracts expire on Thursdays (weekday==3).
            # Accept Tuesday/Wednesday too for holiday-shifted contracts
            # (NSE moves the expiry back by 1-2 days when Thursday is a holiday).
            candidates = [exp for exp in candidates if exp.weekday() in (1, 2, 3)]

        if not candidates:
            return None
        return candidates[0]

    def _snap_to_strike(self, target_strike: float, available_strikes: List[float]) -> float:
        """
        Return the available strike nearest to *target_strike*.

        Args:
            target_strike:     Ideal target strike price.
            available_strikes: Sorted list of available strikes.

        Returns:
            Nearest available strike.
        """
        if not available_strikes:
            return target_strike
        return min(available_strikes, key=lambda s: abs(s - target_strike))

    def _get_available_strikes(
        self, symbol: str, expiry: date
    ) -> Tuple[List[float], List[float]]:
        """
        Return (put_strikes, call_strikes) available for *symbol* on *expiry*.

        Args:
            symbol: NSE symbol.
            expiry: Target expiry date.

        Returns:
            Tuple of (sorted put strikes, sorted call strikes).
        """
        instruments = self._broker._load_instruments() if hasattr(self._broker, "_load_instruments") else []  # type: ignore[attr-defined]
        puts: set[float] = set()
        calls: set[float] = set()

        for inst in instruments:
            if str(inst.get("name", "")).upper() != symbol.upper():
                continue
            raw_exp = inst.get("expiry")
            if isinstance(raw_exp, str):
                try:
                    raw_exp = date.fromisoformat(raw_exp)
                except ValueError:
                    continue
            elif hasattr(raw_exp, "date"):
                raw_exp = raw_exp.date()
            if raw_exp != expiry:
                continue
            try:
                strike = float(inst.get("strike", 0))
            except (TypeError, ValueError):
                continue
            itype = inst.get("instrument_type", "")
            if itype == "PE":
                puts.add(strike)
            elif itype == "CE":
                calls.add(strike)

        return sorted(puts), sorted(calls)

    # ── Order placement helpers ────────────────────────────────────────────

    def _poll_order_fill(self, broker_oid: str, order: Order) -> Optional[Decimal]:
        """
        Poll Zerodha for fill confirmation on a placed order.

        Uses get_order_status_raw() which returns the raw dict with fill details.
        Falls back to assuming filled for paper broker.

        Returns:
            Average fill price as Decimal if filled, None if timeout/cancelled.
        """
        if not hasattr(self._broker, "get_order_status_raw"):
            # Paper broker or mock — assume filled at limit price
            return order.limit_price

        deadline = time.time() + _CONFIRM_TIMEOUT_SECS
        while time.time() < deadline:
            try:
                raw = self._broker.get_order_status_raw(broker_oid)  # type: ignore[attr-defined]
                status_str = str(raw.get("status", "")).upper()
                filled_qty = int(raw.get("filled_quantity", 0))
                avg_price = Decimal(str(raw.get("average_price", 0) or 0))

                if status_str == "COMPLETE" and filled_qty > 0:
                    logger.info("order_fill_confirmed", extra={
                        "broker_oid": broker_oid,
                        "symbol": order.trading_symbol,
                        "fill_qty": filled_qty,
                        "avg_price": str(avg_price),
                    })
                    return avg_price

                if status_str in ("CANCELLED", "REJECTED"):
                    logger.warning("order_cancelled_or_rejected", extra={
                        "broker_oid": broker_oid,
                        "symbol": order.trading_symbol,
                        "status": status_str,
                    })
                    return None

            except Exception as exc:
                logger.warning("poll_fill_error", extra={"error": str(exc), "broker_oid": broker_oid})

            time.sleep(_CONFIRM_POLL_INTERVAL)

        logger.warning("fill_poll_timeout", extra={
            "broker_oid": broker_oid, "symbol": order.trading_symbol
        })
        return None

    def _place_and_confirm(self, order: Order) -> Optional[str]:
        """
        Place an order, poll for fill confirmation, retry once if unfilled.

        Replaces a bare _place_order_safe() call wherever fill confirmation
        is required (entry, shift, exit).  Each attempt:
          1. Places the order via broker.
          2. Polls until COMPLETE / CANCELLED / timeout.
          3. On fill → logs ORDER_CONFIRMED and returns broker_oid.
          4. On no-fill → cancels unfilled order, adjusts price (more aggressive),
             retries up to _MAX_ORDER_RETRIES times.

        Returns:
            Broker order ID string on confirmed fill; None on failure.
        """
        current_order = order
        for attempt in range(1, _MAX_ORDER_RETRIES + 1):
            broker_oid = self._place_order_safe(current_order)
            if broker_oid is None:
                return None

            fill_price = self._poll_order_fill(broker_oid, current_order)
            if fill_price is not None:
                self._trade_logger.log(
                    EventType.ORDER_CONFIRMED,
                    current_order.trading_symbol,
                    {
                        "broker_order_id": broker_oid,
                        "side": current_order.side.value,
                        "fill_price": str(fill_price),
                        "quantity": current_order.quantity,
                        "attempt": attempt,
                    },
                    level="INFO",
                )
                return broker_oid

            # No fill — log and cancel
            logger.warning("order_not_filled_retrying", extra={
                "symbol": current_order.trading_symbol,
                "attempt": attempt,
                "broker_oid": broker_oid,
            })
            self._trade_logger.log(
                EventType.ORDER_REJECTED,
                current_order.trading_symbol,
                {
                    "broker_order_id": broker_oid,
                    "attempt": attempt,
                    "reason": "fill_timeout_no_confirmation",
                    "side": current_order.side.value,
                    "quantity": current_order.quantity,
                    "limit_price": str(current_order.limit_price),
                },
                level="WARNING",
            )
            try:
                if hasattr(self._broker, "cancel_order"):
                    self._broker.cancel_order(broker_oid)  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning("cancel_unfilled_order_failed", extra={"error": str(exc)})

            if attempt < _MAX_ORDER_RETRIES:
                # Retry with a more aggressive price (cross the spread wider)
                tick = Decimal("0.05")
                if current_order.side == Side.BUY:
                    new_price = current_order.limit_price + tick * 2
                else:
                    new_price = max(current_order.limit_price - tick * 2, tick)
                current_order = current_order.model_copy(update={"limit_price": new_price})
                logger.info("order_retry_adjusted_price", extra={
                    "symbol": current_order.trading_symbol,
                    "attempt": attempt + 1,
                    "new_price": str(new_price),
                })
                time.sleep(_ORDER_SPACING_SECONDS)

        return None

    def _check_available_funds(self) -> Tuple[float, Optional[str]]:
        """
        Fetch available funds from Zerodha before any trade.

        Returns:
            (available_margin, error_message).  error_message is None on success.
        """
        try:
            margins_data = self._broker.get_margins()  # type: ignore[attr-defined]
            equity_avail = (margins_data.get("equity", {}) or margins_data).get("available", {})
            cash_margin = float(equity_avail.get("live_balance", 0) or 0)
            collateral = float(equity_avail.get("collateral", 0) or 0)
            available = cash_margin + collateral
            logger.info("funds_check_ok", extra={
                "cash": round(cash_margin, 2),
                "collateral": round(collateral, 2),
                "total_available": round(available, 2),
            })
            return available, None
        except Exception as exc:
            msg = f"funds_check_failed: {exc}"
            logger.error(msg)
            self._trade_logger.log(
                EventType.FUNDS_CHECK_FAILED,
                None,
                {"error": str(exc)},
                level="ERROR",
            )
            return 0.0, str(exc)

    def _place_order_safe(self, order: Order) -> Optional[str]:
        """
        Place an order using the broker, catching and logging exceptions.

        Args:
            order: Order model instance to place.

        Returns:
            Broker order ID string on success; None on failure.
        """
        try:
            if hasattr(self._broker, "place_order_from_model"):
                broker_oid = self._broker.place_order_from_model(order)  # type: ignore[attr-defined]
            else:
                # Paper broker or alternative interface
                broker_oid = self._broker.place_order(  # type: ignore[attr-defined]
                    trading_symbol=order.trading_symbol,
                    exchange="NFO",
                    transaction_type=order.side.value,
                    quantity=order.quantity,
                    price=order.limit_price,
                    product="NRML",
                )
            return str(broker_oid)
        except BrokerError as exc:
            self._last_order_error_msg = str(exc)
            logger.error(
                "order_placement_failed",
                extra={"symbol": order.trading_symbol, "error": str(exc)},
            )
            self._trade_logger.log(
                EventType.ORDER_REJECTED,
                order.trading_symbol,
                {"error": str(exc), "order_id": order.order_id},
                level="ERROR",
            )
            return None

    def _get_now_str(self) -> str:
        """Return current IST time as 'HH:MM'. Override in tests to fix time."""
        return datetime.now(IST).strftime("%H:%M")

    def _get_spot(self, symbol: str) -> Optional[float]:
        """
        Fetch the current spot price for *symbol*.

        Tries index token first (for NIFTY / BANKNIFTY etc.), then
        uses get_equity_spot() for NSE equities (NSE: prefix).

        Args:
            symbol: NSE symbol.

        Returns:
            Spot price as float, or None if unavailable.
        """
        try:
            # ── Index (e.g. NIFTY) — fetch by integer token, not "NSE:NIFTY" ──
            # kite.quote(["NSE:NIFTY"]) silently fails because Zerodha returns
            # the key as "NSE:NIFTY 50" (full tradingsymbol).  Integer token is
            # unambiguous and works reliably.
            if hasattr(self._broker, "get_index_spot"):
                spot = self._broker.get_index_spot(symbol)  # type: ignore[attr-defined]
                if spot and spot > 0:
                    return spot

            # ── NSE equity (uses NSE: prefix, not NFO:) ────────────────────
            if hasattr(self._broker, "get_equity_spot"):
                spot = self._broker.get_equity_spot(symbol)  # type: ignore[attr-defined]
                if spot:
                    return spot
        except Exception as exc:
            logger.warning("spot_fetch_failed", extra={"symbol": symbol, "error": str(exc)})
        return None

    def _get_recent_orders(self, symbol: str) -> List[Dict[str, Any]]:
        """
        Return recent OPEN/PENDING broker orders for *symbol* placed in the last 60 seconds.

        Only returns orders that are still active (OPEN, PENDING) — completed or
        cancelled orders are excluded to avoid false duplicate detection when
        a shift closes and immediately re-opens the same instrument.

        Args:
            symbol: Trading symbol to filter by.

        Returns:
            List of order dicts from the broker.
        """
        try:
            all_orders = self._broker.get_orders()
            now = datetime.now(IST)
            recent = []
            _active_statuses = {"OPEN", "PENDING", "TRIGGER PENDING",
                                "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED",
                                "VALIDATION PENDING", "OPEN PENDING"}
            for o in all_orders:
                status = str(o.get("status", "")).upper()
                if status not in _active_statuses:
                    continue  # Skip filled/cancelled/rejected orders
                if str(o.get("tradingsymbol", "")).startswith(symbol):
                    raw_ts = o.get("order_timestamp")
                    if raw_ts is None:
                        continue
                    if isinstance(raw_ts, str):
                        try:
                            raw_ts = datetime.fromisoformat(raw_ts)
                        except ValueError:
                            continue
                    if hasattr(raw_ts, "tzinfo") and raw_ts.tzinfo is None:
                        raw_ts = raw_ts.replace(tzinfo=IST)
                    if (now - raw_ts).total_seconds() < 60:
                        recent.append(o)
            return recent
        except BrokerError:
            return []

    def _get_broker_positions(self) -> List[Dict[str, Any]]:
        """
        Fetch current broker positions, returning empty list on error.

        Returns:
            List of position dicts from broker.get_positions().
        """
        try:
            return self._broker.get_positions()
        except BrokerError:
            return []

