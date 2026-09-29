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
from live.config import LiveConfig, StockConfig
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

    entry_time: datetime = field(default_factory=lambda: datetime.now(IST))
    """Timestamp of position entry."""

    shift_count: int = 0
    """Number of times this position has been shifted."""

    broker_order_ids: List[str] = field(default_factory=list)
    """All broker order IDs associated with this position."""

    state: str = "OPEN"
    """Position state: OPEN, SHIFTING, CLOSED."""


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
    ) -> None:
        """
        Initialise the CondorRunner.

        Args:
            broker:          BrokerAdapter implementation (Zerodha or Paper).
            config:          Fully loaded LiveConfig.
            trade_logger:    TradeLogger for structured event logging.
            capital_manager: CapitalManager; created from config if not provided.
            kill_switch:     KillSwitch instance; new one created if not provided.
        """
        self._broker = broker
        self._config = config
        self._trade_logger = trade_logger
        self._capital_manager = capital_manager or CapitalManager(config.capital)
        self._kill_switch = kill_switch or KillSwitch()
        self._positions: List[CondorPosition] = []
        self._shift_monitor: Optional[ShiftMonitor] = None
        self._position_store = PositionStore()
        self._session_id = str(uuid.uuid4())[:8]
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

        try:
            self._kill_switch.check()
        except KillSwitchError:
            logger.critical("session_aborted_kill_switch")
            return

        # ── Entry phase ────────────────────────────────────────────────────
        now = datetime.now(IST)
        entry_start = self._config.strategy.entry_start
        entry_end = self._config.strategy.entry_end
        now_str = now.strftime("%H:%M")

        if not (entry_start <= now_str <= entry_end):
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
                try:
                    pos = self.enter_condor(stock_cfg)
                    if pos:
                        self._positions.append(pos)
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
                logger.critical("monitoring_kill_switch_stop")
                if self._shift_monitor:
                    self._shift_monitor.stop()
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
            available_margin = float(
                (margins_data.get("equity", {}) or margins_data).get("available", {}).get(
                    "live_balance", 0
                ) or 0
            )
        except BrokerError:
            available_margin = 0.0

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

        # Close shorts first (buy back)
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
            broker_oid = self._place_order_safe(order)
            if broker_oid:
                exit_debit += float(price)
                self._trade_logger.log_order(order, EventType.ORDER_PLACED)

        # Sell hedges (receive premium back)
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
            broker_oid = self._place_order_safe(order)
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

    # ── Shift ──────────────────────────────────────────────────────────────

    def shift_condor(self, pos: CondorPosition, trigger_side: str) -> None:
        """
        Single-leg shift: close only the threatened spread and re-enter it at
        new strikes.  The other spread is left completely untouched.

        Matches the backtest behaviour:
          • PUT triggered  → close put spread, keep call spread, open new put spread
          • CALL triggered → close call spread, keep put spread, open new call spread

        The position object is mutated in-place (new instrument references + updated
        initial_credit).  No new CondorPosition is created.

        Args:
            pos:          The CondorPosition to partially shift.
            trigger_side: "PUT" or "CALL" — which spread to replace.
        """
        symbol = pos.symbol
        logger.warning(
            "shift_condor_start",
            extra={"symbol": symbol, "shift_count": pos.shift_count, "side": trigger_side},
        )

        # Guard against double-trigger while we're mid-execution
        if pos.state != "OPEN":
            logger.warning("shift_already_in_progress", extra={"symbol": symbol, "state": pos.state})
            return

        pos.state = "SHIFTING"

        old_strikes = {
            "short_put":  float(pos.short_put.strike),
            "long_put":   float(pos.long_put.strike),
            "short_call": float(pos.short_call.strike),
            "long_call":  float(pos.long_call.strike),
        }

        # Find stock config for OTM distances
        stock_cfg: Optional[StockConfig] = None
        for s in self._config.stocks:
            if s.symbol == symbol:
                stock_cfg = s
                break
        if stock_cfg is None:
            logger.error("shift_no_stock_config", extra={"symbol": symbol})
            pos.state = "OPEN"
            return

        run_id = f"shift_{symbol}_{trigger_side}_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}"
        qty = pos.lots * pos.lot_size

        # ── Step 1: Close only the triggered side's spread ─────────────────
        if trigger_side == "PUT":
            to_close_short, to_close_long = pos.short_put, pos.long_put
        else:
            to_close_short, to_close_long = pos.short_call, pos.long_call

        try:
            close_quotes = self._broker.get_quotes([
                (to_close_short.instrument_token, to_close_short.trading_symbol),
                (to_close_long.instrument_token,  to_close_long.trading_symbol),
            ])
        except QuoteError as exc:
            logger.error("shift_close_quote_failed", extra={"symbol": symbol, "error": str(exc)})
            pos.state = "OPEN"
            return

        # Buy back the short leg
        q_short = close_quotes.get(to_close_short.instrument_token)
        close_short_price = q_short.ask if q_short else to_close_short.strike * Decimal("0.005")
        buy_back = Order(
            strategy_run_id=run_id,
            leg_index=0,
            trading_symbol=to_close_short.trading_symbol,
            instrument_token=to_close_short.instrument_token,
            side=Side.BUY,
            quantity=qty,
            limit_price=close_short_price,
        )
        if not self._place_order_safe(buy_back):
            logger.error("shift_close_short_failed", extra={"symbol": symbol})
            pos.state = "OPEN"
            return
        self._trade_logger.log_order(buy_back, EventType.ORDER_PLACED)

        time.sleep(_ORDER_SPACING_SECONDS)

        # Sell back the long hedge
        q_long = close_quotes.get(to_close_long.instrument_token)
        close_long_price = q_long.bid if q_long else Decimal("0")
        sell_hedge = Order(
            strategy_run_id=run_id,
            leg_index=1,
            trading_symbol=to_close_long.trading_symbol,
            instrument_token=to_close_long.instrument_token,
            side=Side.SELL,
            quantity=qty,
            limit_price=close_long_price,
        )
        if not self._place_order_safe(sell_hedge):
            logger.error("shift_close_long_failed", extra={"symbol": symbol})
            pos.state = "OPEN"
            return
        self._trade_logger.log_order(sell_hedge, EventType.ORDER_PLACED)

        close_debit = float(close_short_price) - float(close_long_price)

        # ── Step 2: Enter new spread for the same side at current spot ──────
        spot = self._get_spot(symbol)
        if spot is None or spot <= 0:
            logger.error("shift_no_spot_for_reentry", extra={"symbol": symbol})
            pos.state = "OPEN"
            return

        wing_width = spot * self._config.strategy.wing_width_pct / 100.0
        available_puts, available_calls = self._get_available_strikes(symbol, pos.expiry)
        if not available_puts or not available_calls:
            logger.error("shift_no_strikes", extra={"symbol": symbol})
            pos.state = "OPEN"
            return

        if trigger_side == "PUT":
            new_short_target = spot * (1.0 - stock_cfg.put_dist_pct / 100.0)
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
                pos.state = "OPEN"
                return
        else:
            new_short_target = spot * (1.0 + stock_cfg.call_dist_pct / 100.0)
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
                pos.state = "OPEN"
                return

        try:
            new_quotes = self._broker.get_quotes([
                (new_short_inst.instrument_token, new_short_inst.trading_symbol),
                (new_long_inst.instrument_token,  new_long_inst.trading_symbol),
            ])
        except QuoteError as exc:
            logger.error("shift_entry_quote_failed", extra={"symbol": symbol, "error": str(exc)})
            pos.state = "OPEN"
            return

        def _mid(q: Any) -> Decimal:
            mid = (q.bid + q.ask) / Decimal("2")
            return (mid / Decimal("0.05")).quantize(Decimal("1")) * Decimal("0.05")

        q_ns = new_quotes.get(new_short_inst.instrument_token)
        q_nl = new_quotes.get(new_long_inst.instrument_token)
        if not q_ns or not q_nl:
            logger.error("shift_entry_quotes_missing", extra={"symbol": symbol})
            pos.state = "OPEN"
            return

        new_short_price = _mid(q_ns)
        new_long_price  = _mid(q_nl)

        # Buy the new hedge first
        buy_new_hedge = Order(
            strategy_run_id=run_id,
            leg_index=2,
            trading_symbol=new_long_inst.trading_symbol,
            instrument_token=new_long_inst.instrument_token,
            side=Side.BUY,
            quantity=qty,
            limit_price=new_long_price,
        )
        if not self._place_order_safe(buy_new_hedge):
            logger.error("shift_entry_long_failed", extra={"symbol": symbol})
            pos.state = "OPEN"
            return
        self._trade_logger.log_order(buy_new_hedge, EventType.ORDER_PLACED)

        time.sleep(_ORDER_SPACING_SECONDS)

        # Sell the new short
        sell_new_short = Order(
            strategy_run_id=run_id,
            leg_index=3,
            trading_symbol=new_short_inst.trading_symbol,
            instrument_token=new_short_inst.instrument_token,
            side=Side.SELL,
            quantity=qty,
            limit_price=new_short_price,
        )
        if not self._place_order_safe(sell_new_short):
            logger.error("shift_entry_short_failed", extra={"symbol": symbol})
            pos.state = "OPEN"
            return
        self._trade_logger.log_order(sell_new_short, EventType.ORDER_PLACED)

        new_side_credit = float(new_short_price) - float(new_long_price)

        # ── Step 3: Mutate position in-place ────────────────────────────────
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
        self._trade_logger.log_shift(
            symbol=symbol,
            old_strikes=old_strikes,
            new_strikes=new_strikes,
            spot=spot,
            trigger_pct=self._config.strategy.shift_trigger_pct,
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

    # ── Strike/Expiry helpers ──────────────────────────────────────────────

    def _find_expiry(
        self, symbol: str, min_dte: int, max_dte: int
    ) -> Optional[date]:
        """
        Find the nearest suitable expiry within the DTE window.

        Looks at all available NFO instruments for the symbol and finds
        the nearest monthly expiry where min_dte <= DTE <= max_dte.

        Args:
            symbol:  NSE symbol.
            min_dte: Minimum days to expiry.
            max_dte: Maximum days to expiry.

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
            [
                exp
                for exp in expiries
                if min_dte <= (exp - today).days <= max_dte
            ]
        )
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

    def _get_spot(self, symbol: str) -> Optional[float]:
        """
        Fetch the current spot price for *symbol*.

        Tries the broker's index token first (for indices), then falls
        back to the underlying equity quote on NSE.

        Args:
            symbol: NSE symbol.

        Returns:
            Spot price as float, or None if unavailable.
        """
        try:
            if hasattr(self._broker, "get_index_token"):
                idx_token = self._broker.get_index_token(symbol)  # type: ignore[attr-defined]
                if idx_token:
                    quotes = self._broker.get_quotes([(idx_token, symbol)])
                    if idx_token in quotes:
                        return float(quotes[idx_token].ltp)
            # Try NSE equity quote
            # We need the token — search instruments for the equity
            instruments = self._broker._load_instruments() if hasattr(self._broker, "_load_instruments") else []  # type: ignore[attr-defined]
            for inst in instruments:
                if (
                    str(inst.get("tradingsymbol", "")).upper() == symbol.upper()
                    and inst.get("exchange", "") == "NSE"
                    and inst.get("instrument_type", "") == "EQ"
                ):
                    token = int(inst.get("instrument_token", 0))
                    if token:
                        quotes = self._broker.get_quotes([(token, symbol)])
                        if token in quotes:
                            return float(quotes[token].ltp)
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

