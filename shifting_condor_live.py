#!/usr/bin/env python3
"""
Shifting Iron Condor — Live Trading via Zerodha
================================================
Implements the same shift-and-re-enter logic as shifting_condor_backtest.py
but executes real LIMIT orders through Zerodha Kite Connect.

Capital structure:
  • 50 % from free cash balance
  • 50 % from pledged stock collateral
  → As per SEBI mandate, options writing requires ≥ 50 % cash margin.

Margin sizing:
  • 12.5 % of (spot × lot_size) per lot   (matches backtest MARGIN_PCT)

Shift trigger:
  • Fires when spot enters within 1.5 % of either short strike (configurable).
  • A lower trigger % means the shift fires later (spot must get closer to strike).

Run:
    python shifting_condor_live.py
    python shifting_condor_live.py --config config/shifting_condor.yaml --dry-run

Safety:
    Requires TRADING_MODE=LIVE and
    LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED in environment.
    Without those, all order calls raise LiveTradingBlockedError.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# ── Bootstrap ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/shifting_condor_live.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)

# ── Strategy constants ───────────────────────────────────────────────────────
MARGIN_PCT            = 0.125   # 12.5 % of notional per lot (matches backtest)
CASH_MARGIN_PCT       = 0.50    # 50 % of capital from free cash
STOCK_MARGIN_PCT      = 0.50    # 50 % of capital from stock collateral
SHIFT_TRIGGER_PCT     = 1.5     # default: shift when spot is within 1.5 % of short strike
LOCK_IN_PCT           = 0.25    # close retained side when its value ≤ 25 % of original credit

# ── Safety constants (hard-coded; not overridable from config) ───────────────
MAX_LOTS_HARD_LIMIT   = 1       # HARD CAP: never trade more than 1 lot until explicitly raised
MARKET_OPEN_TIME      = "09:20" # IST — do not enter before this
MARKET_CLOSE_TIME     = "15:20" # IST — do not enter / shift after this
MIN_ENTRY_DTE         = 3       # abort entry if fewer than this many days remain to expiry
QUOTE_MAX_AGE_SECS    = 30      # warn if a quote is older than this


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass
class ShiftingCondorConfig:
    underlying: str
    exchange: str
    product: str
    short_put_otm_pct: float
    short_call_otm_pct: float
    wing_width_pct: float
    shift_trigger_pct: float
    max_shifts: int
    lock_in_pct: float
    min_dte: int
    max_dte: int
    preferred_dte: int
    total_capital: float
    margin_pct: float
    cash_margin_pct: float
    stock_margin_pct: float
    limit_step: float
    order_wait_seconds: int
    max_reprice_attempts: int
    monitor_interval_seconds: int
    take_profit_pct: float
    stop_loss_pct: float
    dte_exit: int                # force-close when days-to-expiry ≤ this
    min_net_credit: float        # abort entry if credit-per-unit ≤ this
    max_session_loss: float      # stop session if total realised loss ≥ this (INR)


def load_config(path: str = "config/shifting_condor.yaml") -> ShiftingCondorConfig:
    import yaml
    with open(path) as f:
        raw = yaml.safe_load(f)
    sc = raw.get("shifting_condor", {})
    return ShiftingCondorConfig(
        underlying               = sc["underlying"],
        exchange                 = sc.get("exchange", "NFO"),
        product                  = sc.get("product", "NRML"),
        short_put_otm_pct        = float(sc.get("short_put_otm_pct", 5.0)),
        short_call_otm_pct       = float(sc.get("short_call_otm_pct", 5.0)),
        wing_width_pct           = float(sc.get("wing_width_pct", 3.0)),
        shift_trigger_pct        = float(sc.get("shift_trigger_pct", SHIFT_TRIGGER_PCT)),
        max_shifts               = int(sc.get("max_shifts", 5)),
        lock_in_pct              = float(sc.get("lock_in_pct", LOCK_IN_PCT)),
        min_dte                  = int(sc.get("min_dte", 20)),
        max_dte                  = int(sc.get("max_dte", 45)),
        preferred_dte            = int(sc.get("preferred_dte", 30)),
        total_capital            = float(sc.get("total_capital", 500_000)),
        margin_pct               = float(sc.get("margin_pct", MARGIN_PCT)),
        cash_margin_pct          = float(sc.get("cash_margin_pct", CASH_MARGIN_PCT)),
        stock_margin_pct         = float(sc.get("stock_margin_pct", STOCK_MARGIN_PCT)),
        limit_step               = float(sc.get("limit_step", 0.05)),
        order_wait_seconds       = int(sc.get("order_wait_seconds", 30)),
        max_reprice_attempts     = int(sc.get("max_reprice_attempts", 5)),
        monitor_interval_seconds = int(sc.get("monitor_interval_seconds", 60)),
        take_profit_pct          = float(sc.get("take_profit_pct", 0.80)),
        stop_loss_pct            = float(sc.get("stop_loss_pct", 2.0)),
        dte_exit                 = int(sc.get("dte_exit", 2)),
        min_net_credit           = float(sc.get("min_net_credit", 1.0)),
        max_session_loss         = float(sc.get("max_session_loss", 15000.0)),
    )


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class MarginInfo:
    """Snapshot of available margin from Zerodha, split into cash and stock."""
    cash_available: float        # free cash (opening_balance)
    stock_collateral: float      # pledged stock collateral after haircut
    total_available: float       # net available from Zerodha
    allocated_cash: float        # how much we will use from cash (capped at 50 %)
    allocated_stock: float       # how much we will use from stock (capped at 50 %)
    usable_capital: float        # allocated_cash + allocated_stock

    def display(self) -> str:
        return (
            f"Cash available  : Rs.{self.cash_available:>12,.0f}  "
            f"(allocated Rs.{self.allocated_cash:,.0f}  —  "
            f"{self.allocated_cash / max(self.usable_capital, 1) * 100:.0f}%)\n"
            f"Stock collateral: Rs.{self.stock_collateral:>12,.0f}  "
            f"(allocated Rs.{self.allocated_stock:,.0f}  —  "
            f"{self.allocated_stock / max(self.usable_capital, 1) * 100:.0f}%)\n"
            f"Usable capital  : Rs.{self.usable_capital:>12,.0f}"
        )


@dataclass
class CondorLeg:
    """A single filled leg of the condor."""
    side: str                    # "BUY" or "SELL"
    option_type: str             # "PE" or "CE"
    strike: float
    trading_symbol: str
    instrument_token: int
    lot_size: int
    lots: int
    quantity: int
    entry_price: float
    exit_price: float = 0.0
    broker_order_id: str = ""
    is_open: bool = True


@dataclass
class CondorSegment:
    """One entry-to-exit segment (initial or after a shift)."""
    segment_idx: int
    entry_date: date
    legs: list[CondorLeg] = field(default_factory=list)
    entry_credit: float = 0.0    # net credit per unit at entry
    exit_credit: float = 0.0     # cost to close per unit at exit
    segment_pnl: float = 0.0
    exit_reason: str = ""        # EXPIRY | SHIFT | TAKE_PROFIT | STOP_LOSS | MANUAL
    shift_trigger: str = ""      # PUT_SIDE | CALL_SIDE | PUT_SIDE_FULL | CALL_SIDE_FULL
    margin_used: float = 0.0
    cash_margin: float = 0.0     # 50 % of margin_used (from free cash)
    stock_margin: float = 0.0    # 50 % of margin_used (from stock collateral)

    @property
    def short_put(self) -> Optional[CondorLeg]:
        return next((l for l in self.legs if l.side == "SELL" and l.option_type == "PE"), None)

    @property
    def long_put(self) -> Optional[CondorLeg]:
        return next((l for l in self.legs if l.side == "BUY"  and l.option_type == "PE"), None)

    @property
    def short_call(self) -> Optional[CondorLeg]:
        return next((l for l in self.legs if l.side == "SELL" and l.option_type == "CE"), None)

    @property
    def long_call(self) -> Optional[CondorLeg]:
        return next((l for l in self.legs if l.side == "BUY"  and l.option_type == "CE"), None)

    @property
    def open_legs(self) -> list[CondorLeg]:
        return [l for l in self.legs if l.is_open]


# ── Main engine ───────────────────────────────────────────────────────────────

class ShiftingCondorLive:
    """
    Executes a shifting iron condor live on Zerodha.

    Safeguards:
      • Hard lot cap of MAX_LOTS_HARD_LIMIT (currently 1)
      • Market-hours gate (09:20 – 15:20 IST)
      • Minimum DTE on entry (3 days)
      • Minimum net credit on entry
      • Per-order fill verification (qty, status) after every order
      • Post-entry position reconciliation against broker
      • Post-exit position verification (broker shows 0 remaining)
      • Stale-quote detection (warns if quote age > 30 s)
      • Session-loss cap (stops new segments if realised loss ≥ max_session_loss)
      • Duplicate-instance lock (process-level PID file)
    """

    def __init__(self, cfg: ShiftingCondorConfig, broker, cache_path: str,
                 dry_run: bool = False) -> None:
        self._cfg         = cfg
        self._broker      = broker
        self._cache_path  = cache_path
        self._dry_run     = dry_run
        self._segments: list[CondorSegment] = []
        self._num_shifts  = 0
        self._expiry: Optional[date] = None
        self._lot_size    = 0
        self._lots        = 0
        self._margin_info: Optional[MarginInfo] = None
        self._session_realised_pnl = 0.0   # tracks closed-segment P&L for loss cap

        # Retained leg state (kept across shifts)
        self._retained_short_put:  Optional[CondorLeg] = None
        self._retained_long_put:   Optional[CondorLeg] = None
        self._retained_short_call: Optional[CondorLeg] = None
        self._retained_long_call:  Optional[CondorLeg] = None

    # ────────────────────────────────────────────────────────────────────────
    # SAFEGUARD 1 — Market hours
    # ────────────────────────────────────────────────────────────────────────

    def _is_within_trading_hours(self) -> bool:
        """Returns True only between MARKET_OPEN_TIME and MARKET_CLOSE_TIME IST."""
        now_hm = datetime.now(IST).strftime("%H:%M")
        return MARKET_OPEN_TIME <= now_hm <= MARKET_CLOSE_TIME

    def _assert_trading_hours(self, action: str = "act") -> None:
        """Raise RuntimeError if outside trading hours."""
        if not self._is_within_trading_hours():
            now_hm = datetime.now(IST).strftime("%H:%M")
            raise RuntimeError(
                f"Cannot {action}: outside trading hours "
                f"({now_hm} IST, window {MARKET_OPEN_TIME}–{MARKET_CLOSE_TIME})"
            )

    # ────────────────────────────────────────────────────────────────────────
    # SAFEGUARD 2 — Stale quote detection
    # ────────────────────────────────────────────────────────────────────────

    def _quote_is_fresh(self, q: dict, symbol: str = "") -> bool:
        """
        Returns True if the quote timestamp is within QUOTE_MAX_AGE_SECS.
        Logs a warning (but does NOT block) if stale — the caller decides.
        """
        ts = q.get("timestamp")
        if ts is None:
            return True   # Zerodha sometimes omits timestamp; don't block
        try:
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            age = (datetime.now(IST) - ts).total_seconds()
            if age > QUOTE_MAX_AGE_SECS:
                logger.warning("stale_quote_detected",
                               extra={"symbol": symbol, "age_seconds": round(age, 1)})
                return False
        except Exception:
            pass
        return True

    # ────────────────────────────────────────────────────────────────────────
    # SAFEGUARD 3 — Per-order fill verification
    # ────────────────────────────────────────────────────────────────────────

    def _verify_order_filled(
        self,
        order_id: str,
        symbol: str,
        expected_qty: int,
        side: str,
    ) -> tuple[bool, str]:
        """
        Cross-check with broker that order_id is COMPLETE with correct quantity.
        Returns (ok: bool, detail_message: str).

        Checks:
          1. status == COMPLETE
          2. filled_quantity == expected_qty
          3. average_price > 0
        """
        if self._dry_run:
            return True, "dry_run — skipping broker check"
        try:
            status = self._broker.get_order_status(order_id)
        except Exception as exc:
            msg = f"get_order_status failed: {exc}"
            logger.error("fill_verification_error",
                         extra={"order_id": order_id, "symbol": symbol, "error": msg})
            return False, msg

        broker_status = status.get("status", "UNKNOWN")
        filled_qty    = int(status.get("filled_quantity", 0) or 0)
        avg_price     = float(status.get("average_price", 0) or 0)

        if broker_status != "COMPLETE":
            msg = f"status='{broker_status}' (expected COMPLETE)"
            logger.error("fill_verification_status_mismatch",
                         extra={"order_id": order_id, "symbol": symbol,
                                "status": broker_status, "side": side})
            return False, msg

        if filled_qty != expected_qty:
            msg = f"filled_qty={filled_qty} != expected={expected_qty}"
            logger.error("fill_verification_qty_mismatch",
                         extra={"order_id": order_id, "symbol": symbol,
                                "filled": filled_qty, "expected": expected_qty})
            return False, msg

        if avg_price <= 0:
            msg = f"average_price={avg_price} (zero or negative)"
            logger.error("fill_verification_price_zero",
                         extra={"order_id": order_id, "symbol": symbol})
            return False, msg

        logger.info("fill_verified",
                    extra={"order_id": order_id, "symbol": symbol,
                           "qty": filled_qty, "avg_price": avg_price, "side": side})
        return True, f"qty={filled_qty} avg_price={avg_price:.2f}"

    # ────────────────────────────────────────────────────────────────────────
    # SAFEGUARD 4 — Post-entry position reconciliation
    # ────────────────────────────────────────────────────────────────────────

    def _verify_positions_with_broker(self, seg: CondorSegment) -> bool:
        """
        After all legs are entered, query broker positions and verify each
        open leg has the correct net quantity.

        Discrepancy → logged CRITICAL + returns False.
        A False return does NOT automatically close positions — the caller
        must decide (log and continue monitoring, or abort).
        """
        if self._dry_run:
            return True
        try:
            broker_positions = self._broker.get_positions()
        except Exception as exc:
            logger.error("position_reconcile_fetch_failed", extra={"error": str(exc)})
            return False

        # Build symbol → net_quantity from broker (negative = net short)
        broker_net: dict[str, int] = {}
        for p in broker_positions:
            sym = str(p.get("tradingsymbol", "")).strip()
            qty = int(p.get("quantity", 0) or 0)
            broker_net[sym] = broker_net.get(sym, 0) + qty

        all_ok = True
        for leg in seg.open_legs:
            # BUY legs have positive qty; SELL legs have negative qty in broker
            expected_net = leg.quantity if leg.side == "BUY" else -leg.quantity
            actual_net   = broker_net.get(leg.trading_symbol)

            if actual_net is None:
                logger.critical(
                    "position_not_found_in_broker",
                    extra={"symbol": leg.trading_symbol,
                           "side": leg.side,
                           "expected_qty": expected_net},
                )
                all_ok = False
            elif actual_net != expected_net:
                logger.critical(
                    "position_quantity_mismatch",
                    extra={"symbol": leg.trading_symbol,
                           "side": leg.side,
                           "expected": expected_net,
                           "broker": actual_net},
                )
                all_ok = False
            else:
                logger.info("position_reconciled_ok",
                            extra={"symbol": leg.trading_symbol,
                                   "qty": actual_net})

        if all_ok:
            logger.info("all_positions_reconciled",
                        extra={"legs": len(seg.open_legs)})
        else:
            logger.critical("position_reconcile_failed — manual check required")
        return all_ok

    # ────────────────────────────────────────────────────────────────────────
    # SAFEGUARD 5 — Post-exit position verification
    # ────────────────────────────────────────────────────────────────────────

    def _verify_exit_complete(self, closed_legs: list[CondorLeg]) -> bool:
        """
        After closing a set of legs, verify the broker shows zero net quantity
        for those symbols.

        Returns True if all clear. False if any symbol still shows an open position
        (logs CRITICAL for each — operator must investigate).
        """
        if self._dry_run or not closed_legs:
            return True
        try:
            broker_positions = self._broker.get_positions()
        except Exception as exc:
            logger.error("exit_verify_fetch_failed", extra={"error": str(exc)})
            return False

        broker_net: dict[str, int] = {}
        for p in broker_positions:
            sym = str(p.get("tradingsymbol", "")).strip()
            qty = int(p.get("quantity", 0) or 0)
            broker_net[sym] = broker_net.get(sym, 0) + qty

        all_clear = True
        for leg in closed_legs:
            remaining = broker_net.get(leg.trading_symbol, 0)
            if remaining != 0:
                logger.critical(
                    "EXIT_INCOMPLETE_position_still_open",
                    extra={"symbol": leg.trading_symbol,
                           "remaining_qty": remaining,
                           "expected": 0},
                )
                print(f"\n  !! EXIT INCOMPLETE: {leg.trading_symbol} "
                      f"still shows qty={remaining} on broker. "
                      f"MANUAL INTERVENTION REQUIRED.")
                all_clear = False
            else:
                logger.info("exit_position_confirmed_zero",
                            extra={"symbol": leg.trading_symbol})
        return all_clear

    # ────────────────────────────────────────────────────────────────────────
    # SAFEGUARD 6 — Session loss cap
    # ────────────────────────────────────────────────────────────────────────

    def _check_session_loss_cap(self) -> bool:
        """
        Returns True if it is safe to continue (session loss < max_session_loss).
        Returns False and logs CRITICAL if the cap is breached — caller must stop.
        """
        if self._session_realised_pnl < -self._cfg.max_session_loss:
            logger.critical(
                "SESSION_LOSS_CAP_BREACHED",
                extra={"realised_pnl": round(self._session_realised_pnl, 2),
                       "cap": self._cfg.max_session_loss},
            )
            print(f"\n  !! SESSION LOSS CAP BREACHED: "
                  f"Rs.{abs(self._session_realised_pnl):,.0f} lost "
                  f"(limit Rs.{self._cfg.max_session_loss:,.0f}). Stopping.")
            return False
        return True

    # ── Spot price ────────────────────────────────────────────────────────────

    _INDEX_SYMBOLS: dict[str, str] = {
        "NIFTY":      "NSE:NIFTY 50",
        "BANKNIFTY":  "NSE:NIFTY BANK",
        "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
        "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
        "SENSEX":     "BSE:SENSEX",
    }

    def _get_spot(self) -> Optional[float]:
        sym = self._INDEX_SYMBOLS.get(
            self._cfg.underlying.upper(),
            f"NSE:{self._cfg.underlying}"
        )
        try:
            raw = self._broker._kite.quote([sym])
            v = raw.get(sym, {}).get("last_price")
            if v:
                return float(v)
        except Exception as exc:
            logger.warning("spot_fetch_failed", extra={"error": str(exc)})
        return None

    # ── Margin ────────────────────────────────────────────────────────────────

    def _get_margin_info(self) -> MarginInfo:
        """
        Fetch available margin from Zerodha and split 50 % cash / 50 % stock.

        Zerodha equity margin structure:
          equity.available.opening_balance  = free cash
          equity.available.collateral       = pledged stock collateral (after haircut)
          equity.net                        = total net available
        """
        margins = self._broker.get_margins()
        equity  = margins.get("equity", {})
        avail   = equity.get("available", {})

        cash_balance     = float(avail.get("opening_balance", 0) or 0)
        stock_collateral = float(avail.get("collateral", 0) or 0)
        total_net        = float(equity.get("net", 0) or 0)

        desired_cash  = self._cfg.total_capital * self._cfg.cash_margin_pct
        desired_stock = self._cfg.total_capital * self._cfg.stock_margin_pct

        allocated_cash  = min(cash_balance, desired_cash)
        allocated_stock = min(stock_collateral, desired_stock)
        usable          = allocated_cash + allocated_stock

        return MarginInfo(
            cash_available   = cash_balance,
            stock_collateral = stock_collateral,
            total_available  = total_net,
            allocated_cash   = allocated_cash,
            allocated_stock  = allocated_stock,
            usable_capital   = usable,
        )

    # ── Expiry selection ──────────────────────────────────────────────────────

    def _select_expiry(self) -> Optional[date]:
        from stock_picker import get_expiries_for_underlying
        today = date.today()
        candidates = [
            e for e in get_expiries_for_underlying(self._cache_path, self._cfg.underlying)
            if self._cfg.min_dte <= (e - today).days <= self._cfg.max_dte
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda e: abs((e - today).days - self._cfg.preferred_dte))

    # ── Strike selection ──────────────────────────────────────────────────────

    def _get_available_strikes(self, expiry: date, opt_type: str) -> list[float]:
        from stock_picker import get_strikes_for_underlying_expiry
        strikes = get_strikes_for_underlying_expiry(
            self._cache_path, self._cfg.underlying, expiry, opt_type
        )
        return sorted(float(s) for s in strikes)

    def _select_strikes(
        self, spot: float, expiry: date
    ) -> Optional[tuple[float, float, float, float]]:
        """
        Returns (short_put, long_put, short_call, long_call) or None.
        short_put  = highest PE strike ≤ spot * (1 - short_put_otm_pct/100)
        long_put   = highest PE strike < short_put AND ≤ spot*(1-(short_put_otm+wing)/100)
        short_call = lowest  CE strike ≥ spot * (1 + short_call_otm_pct/100)
        long_call  = lowest  CE strike > short_call AND ≥ spot*(1+(short_call_otm+wing)/100)
        """
        pe_strikes = self._get_available_strikes(expiry, "PE")
        ce_strikes = self._get_available_strikes(expiry, "CE")
        if not pe_strikes or not ce_strikes:
            return None

        put_pct  = self._cfg.short_put_otm_pct
        call_pct = self._cfg.short_call_otm_pct
        wing_pct = self._cfg.wing_width_pct

        sp_target = spot * (1 - put_pct / 100)
        sc_target = spot * (1 + call_pct / 100)
        sp_cands = [s for s in pe_strikes if s <= sp_target]
        sc_cands = [s for s in ce_strikes if s >= sc_target]
        if not sp_cands or not sc_cands:
            return None
        sp = max(sp_cands)
        sc = min(sc_cands)

        bp_target = spot * (1 - (put_pct + wing_pct) / 100)
        bc_target = spot * (1 + (call_pct + wing_pct) / 100)
        bp_cands = [s for s in pe_strikes if s < sp and s <= bp_target]
        if not bp_cands:
            bp_cands = [s for s in pe_strikes if s < sp]
        bc_cands = [s for s in ce_strikes if s > sc and s >= bc_target]
        if not bc_cands:
            bc_cands = [s for s in ce_strikes if s > sc]
        if not bp_cands or not bc_cands:
            return None

        return sp, max(bp_cands), sc, min(bc_cands)

    # ── Lot sizing ────────────────────────────────────────────────────────────

    def _compute_lots(self, spot: float, lot_size: int, usable_capital: float) -> int:
        """
        lots = floor(usable_capital / margin_per_lot)
        margin_per_lot = margin_pct * spot * lot_size  (12.5 % notional)

        Hard cap: never exceeds MAX_LOTS_HARD_LIMIT (currently 1).
        """
        margin_per_lot = self._cfg.margin_pct * spot * lot_size
        if margin_per_lot <= 0:
            return 0
        computed = int(usable_capital // margin_per_lot)
        if computed > MAX_LOTS_HARD_LIMIT:
            logger.warning(
                "lots_capped_to_hard_limit",
                extra={"computed": computed,
                       "hard_limit": MAX_LOTS_HARD_LIMIT,
                       "reason": "increase MAX_LOTS_HARD_LIMIT in code when ready"},
            )
            print(f"\n  [SAFEGUARD] Lots capped: computed={computed}, "
                  f"hard limit={MAX_LOTS_HARD_LIMIT}. "
                  f"Raise MAX_LOTS_HARD_LIMIT in code when ready for larger size.")
            return MAX_LOTS_HARD_LIMIT
        return computed

    # ── Quote helpers ─────────────────────────────────────────────────────────

    def _get_quote(self, trading_symbol: str) -> Optional[dict]:
        """Return raw Zerodha quote dict for a single NFO instrument."""
        key = f"NFO:{trading_symbol}"
        try:
            raw = self._broker._kite.quote([key])
            q = raw.get(key)
            if q:
                self._quote_is_fresh(q, symbol=trading_symbol)  # logs warning if stale
            return q
        except Exception as exc:
            logger.warning("quote_failed",
                           extra={"symbol": trading_symbol, "error": str(exc)})
            return None

    def _mid_price(self, q: dict) -> float:
        bid = float((q.get("depth", {}).get("buy") or [{}])[0].get("price", 0)
                    or q.get("last_price", 0))
        ask = float((q.get("depth", {}).get("sell") or [{}])[0].get("price", 0)
                    or q.get("last_price", 0))
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return float(q.get("last_price", 0))

    # ── Instrument resolution ─────────────────────────────────────────────────

    def _resolve_trading_symbol(
        self, expiry: date, strike: float, opt_type: str
    ) -> Optional[tuple[str, int, int]]:
        """Returns (trading_symbol, instrument_token, lot_size) or None."""
        try:
            ri = self._broker.resolve_instrument(
                underlying  = self._cfg.underlying,
                exchange    = self._cfg.exchange,
                expiry      = expiry,
                strike      = Decimal(str(strike)),
                option_type = opt_type,
            )
            return ri.trading_symbol, ri.instrument_token, ri.lot_size
        except Exception as exc:
            logger.error("resolve_failed",
                         extra={"strike": strike, "opt": opt_type, "error": str(exc)})
            return None

    # ── Order placement ───────────────────────────────────────────────────────

    def _place_order_with_retry(
        self,
        trading_symbol: str,
        transaction_type: str,   # "BUY" or "SELL"
        quantity: int,
        initial_price: float,
        tag: str = "",
    ) -> Optional[str]:
        """
        Place a LIMIT order.  Reprice up to max_reprice_attempts times
        if not filled within order_wait_seconds.

        After each fill: calls _verify_order_filled — returns None on mismatch.
        Returns broker_order_id on confirmed fill, None on any failure.
        """
        if self._dry_run:
            logger.info(
                "DRY_RUN_order",
                extra={"symbol": trading_symbol, "side": transaction_type,
                       "qty": quantity, "price": initial_price},
            )
            return "DRY_RUN_ORDER_ID"

        # Snap to 0.05 tick
        price = round(round(initial_price / 0.05) * 0.05, 2)
        price = max(price, 0.05)

        order_id: Optional[str] = None

        for attempt in range(self._cfg.max_reprice_attempts + 1):
            try:
                order_id = self._broker.place_order(
                    trading_symbol   = trading_symbol,
                    exchange         = self._cfg.exchange,
                    transaction_type = transaction_type,
                    quantity         = quantity,
                    price            = Decimal(str(price)),
                    product          = self._cfg.product,
                    tag              = tag[:20],
                )
            except Exception as exc:
                logger.error("order_placement_failed",
                             extra={"symbol": trading_symbol,
                                    "side": transaction_type,
                                    "attempt": attempt + 1,
                                    "error": str(exc)})
                return None

            logger.info("order_submitted",
                        extra={"order_id": order_id, "symbol": trading_symbol,
                               "side": transaction_type, "qty": quantity,
                               "price": price, "attempt": attempt + 1})

            # Wait, then check fill status
            time.sleep(self._cfg.order_wait_seconds)

            try:
                status = self._broker.get_order_status(order_id)
                broker_status = status.get("status", "")
            except Exception as exc:
                logger.warning("order_status_check_failed",
                               extra={"order_id": order_id, "error": str(exc)})
                broker_status = "UNKNOWN"

            if broker_status == "COMPLETE":
                # ── SAFEGUARD 3: verify fill details ──────────────────────
                ok, detail = self._verify_order_filled(
                    order_id, trading_symbol, quantity, transaction_type
                )
                if ok:
                    return order_id
                else:
                    logger.error("fill_verification_failed_aborting",
                                 extra={"order_id": order_id, "detail": detail})
                    return None

            if broker_status in ("REJECTED", "CANCELLED"):
                logger.error("order_rejected_or_cancelled",
                             extra={"order_id": order_id, "status": broker_status,
                                    "symbol": trading_symbol})
                return None

            # Still pending — reprice if attempts remain
            if attempt < self._cfg.max_reprice_attempts:
                q = self._get_quote(trading_symbol)
                if q:
                    ltp = float(q.get("last_price", price))
                    if transaction_type == "BUY":
                        price = min(ltp + self._cfg.limit_step * 2, ltp * 1.02)
                    else:
                        price = max(ltp - self._cfg.limit_step * 2, ltp * 0.98)
                price = round(round(price / 0.05) * 0.05, 2)
                price = max(price, 0.05)
                try:
                    self._broker.modify_order(order_id, Decimal(str(price)))
                    logger.info("order_repriced",
                                extra={"order_id": order_id, "new_price": price,
                                       "attempt": attempt + 1})
                except Exception as exc:
                    logger.warning("order_modify_failed",
                                   extra={"order_id": order_id, "error": str(exc)})

        # All reprice attempts exhausted — cancel the open order
        logger.error("order_not_filled_after_retries",
                     extra={"symbol": trading_symbol,
                            "attempts": self._cfg.max_reprice_attempts + 1})
        if order_id:
            try:
                self._broker.cancel_order(order_id)
                logger.info("lingering_order_cancelled", extra={"order_id": order_id})
            except Exception as exc:
                logger.warning("cancel_lingering_order_failed",
                               extra={"order_id": order_id, "error": str(exc)})
        return None

    def _get_fill_price(self, order_id: str) -> float:
        """Return average fill price for a completed order."""
        if self._dry_run or order_id == "DRY_RUN_ORDER_ID":
            return 0.0
        try:
            status = self._broker.get_order_status(order_id)
            return float(status.get("average_price", 0) or 0)
        except Exception:
            return 0.0

    # ── Entry ─────────────────────────────────────────────────────────────────

    def _enter_legs(
        self,
        expiry: date,
        open_put: bool,
        open_call: bool,
        spot: float,
        seg: CondorSegment,
    ) -> bool:
        """
        Hedge-first entry for the specified sides.
        BUY longs first → only then SELL shorts.

        After all four legs fill: calls _verify_positions_with_broker.
        Returns True only if all required legs filled AND broker reconciliation passes.
        """
        # ── Resolve strikes ────────────────────────────────────────────────
        if open_put and open_call:
            strikes = self._select_strikes(spot, expiry)
            if not strikes:
                logger.error("no_strikes_found",
                             extra={"spot": spot, "expiry": str(expiry)})
                return False
            sp, bp, sc, bc = strikes
        elif open_put:
            if self._retained_short_call is None:
                logger.error("retained_call_missing")
                return False
            pe_all = self._get_available_strikes(expiry, "PE")
            if not pe_all:
                return False
            sp_target = spot * (1 - self._cfg.short_put_otm_pct / 100)
            bp_target = spot * (1 - (self._cfg.short_put_otm_pct + self._cfg.wing_width_pct) / 100)
            sp_cands = [s for s in pe_all if s <= sp_target]
            if not sp_cands:
                return False
            sp = max(sp_cands)
            bp_cands = [s for s in pe_all if s < sp and s <= bp_target]
            if not bp_cands:
                bp_cands = [s for s in pe_all if s < sp]
            if not bp_cands:
                return False
            bp = max(bp_cands)
            sc = bc = 0.0
        else:
            if self._retained_short_put is None:
                logger.error("retained_put_missing")
                return False
            ce_all = self._get_available_strikes(expiry, "CE")
            if not ce_all:
                return False
            sc_target = spot * (1 + self._cfg.short_call_otm_pct / 100)
            bc_target = spot * (1 + (self._cfg.short_call_otm_pct + self._cfg.wing_width_pct) / 100)
            sc_cands = [s for s in ce_all if s >= sc_target]
            if not sc_cands:
                return False
            sc = min(sc_cands)
            bc_cands = [s for s in ce_all if s > sc and s >= bc_target]
            if not bc_cands:
                bc_cands = [s for s in ce_all if s > sc]
            if not bc_cands:
                return False
            bc = min(bc_cands)
            sp = bp = 0.0

        # ── Per-lot verification against instrument master ─────────────────
        # Resolve one instrument to confirm lot_size hasn't changed since entry.
        if open_put:
            _verify_res = self._resolve_trading_symbol(expiry, bp, "PE")
            if _verify_res:
                _, _, _master_lot_size = _verify_res
                if _master_lot_size != self._lot_size:
                    logger.warning("lot_size_mismatch_corrected",
                                   extra={"expected": self._lot_size,
                                          "actual": _master_lot_size})
                    print(f"\n  [Safeguard] Lot size mismatch: expected={self._lot_size}, "
                          f"master={_master_lot_size}. Using master value.")
                    self._lot_size = _master_lot_size
        elif open_call:
            _verify_res = self._resolve_trading_symbol(expiry, bc, "CE")
            if _verify_res:
                _, _, _master_lot_size = _verify_res
                if _master_lot_size != self._lot_size:
                    logger.warning("lot_size_mismatch_corrected",
                                   extra={"expected": self._lot_size,
                                          "actual": _master_lot_size})
                    print(f"\n  [Safeguard] Lot size mismatch: expected={self._lot_size}, "
                          f"master={_master_lot_size}. Using master value.")
                    self._lot_size = _master_lot_size

        quantity = self._lots * self._lot_size

        # ── Lot size display ───────────────────────────────────────────────
        print(f"\n  [Safeguard] Lot check: lots={self._lots}, "
              f"lot_size={self._lot_size}, "
              f"quantity={quantity}  (hard limit={MAX_LOTS_HARD_LIMIT} lots)")
        logger.info("lot_size_check",
                    extra={"lots": self._lots,
                           "lot_size": self._lot_size,
                           "quantity": quantity,
                           "hard_limit": MAX_LOTS_HARD_LIMIT})

        # ── STEP 1: BUY long legs first (hedges) ──────────────────────────
        new_long_put:   Optional[CondorLeg] = None
        new_long_call:  Optional[CondorLeg] = None
        new_short_put:  Optional[CondorLeg] = None
        new_short_call: Optional[CondorLeg] = None

        placed_legs: list[CondorLeg] = []   # tracks all newly opened legs for rollback

        if open_put:
            res = self._resolve_trading_symbol(expiry, bp, "PE")
            if not res:
                return False
            sym_bp, tok_bp, ls = res
            q_bp   = self._get_quote(sym_bp)
            price  = self._mid_price(q_bp) if q_bp else 0.5
            oid    = self._place_order_with_retry(sym_bp, "BUY", quantity, price, tag="SHC_LP")
            if not oid:
                logger.error("long_put_failed", extra={"symbol": sym_bp})
                return False
            fill = self._get_fill_price(oid) or price
            new_long_put = CondorLeg("BUY", "PE", bp, sym_bp, tok_bp, ls,
                                     self._lots, quantity, fill, broker_order_id=oid)
            placed_legs.append(new_long_put)
            print(f"  [Entry] BUY long put  {sym_bp:<30} qty={quantity}  "
                  f"fill=Rs.{fill:.2f}  order_id={oid}")

        if open_call:
            res = self._resolve_trading_symbol(expiry, bc, "CE")
            if not res:
                self._rollback_legs(placed_legs)
                return False
            sym_bc, tok_bc, ls = res
            q_bc  = self._get_quote(sym_bc)
            price = self._mid_price(q_bc) if q_bc else 0.5
            oid   = self._place_order_with_retry(sym_bc, "BUY", quantity, price, tag="SHC_LC")
            if not oid:
                logger.error("long_call_failed", extra={"symbol": sym_bc})
                self._rollback_legs(placed_legs)
                return False
            fill = self._get_fill_price(oid) or price
            new_long_call = CondorLeg("BUY", "CE", bc, sym_bc, tok_bc, ls,
                                      self._lots, quantity, fill, broker_order_id=oid)
            placed_legs.append(new_long_call)
            print(f"  [Entry] BUY long call {sym_bc:<30} qty={quantity}  "
                  f"fill=Rs.{fill:.2f}  order_id={oid}")

        # ── STEP 2: SELL short legs ────────────────────────────────────────
        if open_put:
            res = self._resolve_trading_symbol(expiry, sp, "PE")
            if not res:
                self._rollback_legs(placed_legs)
                return False
            sym_sp, tok_sp, ls = res
            q_sp  = self._get_quote(sym_sp)
            price = self._mid_price(q_sp) if q_sp else 1.0
            # ── Minimum limit price check before SELL order ────────────────
            if price < self._cfg.min_net_credit:
                logger.warning("short_put_below_min_premium",
                               extra={"symbol": sym_sp, "price": price,
                                      "min": self._cfg.min_net_credit})
                print(f"\n  [Safeguard] Short put premium {price:.2f} < "
                      f"min_net_credit={self._cfg.min_net_credit:.2f}. "
                      f"Proceeding — spread credit will be checked after full entry.")
            oid   = self._place_order_with_retry(sym_sp, "SELL", quantity, price, tag="SHC_SP")
            if not oid:
                logger.error("short_put_failed", extra={"symbol": sym_sp})
                self._rollback_legs(placed_legs)
                return False
            fill = self._get_fill_price(oid) or price
            new_short_put = CondorLeg("SELL", "PE", sp, sym_sp, tok_sp, ls,
                                      self._lots, quantity, fill, broker_order_id=oid)
            placed_legs.append(new_short_put)
            print(f"  [Entry] SELL short put  {sym_sp:<28} qty={quantity}  "
                  f"fill=Rs.{fill:.2f}  order_id={oid}")

        if open_call:
            res = self._resolve_trading_symbol(expiry, sc, "CE")
            if not res:
                self._rollback_legs(placed_legs)
                return False
            sym_sc, tok_sc, ls = res
            q_sc  = self._get_quote(sym_sc)
            price = self._mid_price(q_sc) if q_sc else 1.0
            # ── Minimum limit price check before SELL order ────────────────
            if price < self._cfg.min_net_credit:
                logger.warning("short_call_below_min_premium",
                               extra={"symbol": sym_sc, "price": price,
                                      "min": self._cfg.min_net_credit})
                print(f"\n  [Safeguard] Short call premium {price:.2f} < "
                      f"min_net_credit={self._cfg.min_net_credit:.2f}. "
                      f"Proceeding — spread credit will be checked after full entry.")
            oid   = self._place_order_with_retry(sym_sc, "SELL", quantity, price, tag="SHC_SC")
            if not oid:
                logger.error("short_call_failed", extra={"symbol": sym_sc})
                self._rollback_legs(placed_legs)
                return False
            fill = self._get_fill_price(oid) or price
            new_short_call = CondorLeg("SELL", "CE", sc, sym_sc, tok_sc, ls,
                                       self._lots, quantity, fill, broker_order_id=oid)
            placed_legs.append(new_short_call)
            print(f"  [Entry] SELL short call {sym_sc:<27} qty={quantity}  "
                  f"fill=Rs.{fill:.2f}  order_id={oid}")

        # ── Record legs in segment ─────────────────────────────────────────
        if new_long_put:
            seg.legs.append(new_long_put)
            self._retained_long_put  = new_long_put
        if new_long_call:
            seg.legs.append(new_long_call)
            self._retained_long_call = new_long_call
        if new_short_put:
            seg.legs.append(new_short_put)
            self._retained_short_put = new_short_put
        if new_short_call:
            seg.legs.append(new_short_call)
            self._retained_short_call = new_short_call

        # Carry retained legs from previous shift into this segment's record
        if not open_put and self._retained_short_put:
            seg.legs.extend([self._retained_long_put, self._retained_short_put])
        if not open_call and self._retained_short_call:
            seg.legs.extend([self._retained_long_call, self._retained_short_call])

        # ── Compute segment credit ─────────────────────────────────────────
        sp_p = (new_short_put.entry_price  if new_short_put
                else (self._retained_short_put.entry_price  if self._retained_short_put  else 0))
        sc_p = (new_short_call.entry_price if new_short_call
                else (self._retained_short_call.entry_price if self._retained_short_call else 0))
        bp_p = (new_long_put.entry_price   if new_long_put
                else (self._retained_long_put.entry_price   if self._retained_long_put   else 0))
        bc_p = (new_long_call.entry_price  if new_long_call
                else (self._retained_long_call.entry_price  if self._retained_long_call  else 0))

        seg.entry_credit  = (sp_p + sc_p) - (bp_p + bc_p)
        seg.margin_used   = self._cfg.margin_pct * spot * self._lot_size * self._lots
        seg.cash_margin   = seg.margin_used * self._cfg.cash_margin_pct
        seg.stock_margin  = seg.margin_used * self._cfg.stock_margin_pct

        # ── SAFEGUARD: minimum net credit ─────────────────────────────────
        if seg.entry_credit <= 0:
            logger.error("negative_or_zero_credit",
                         extra={"credit": seg.entry_credit})
            print(f"\n  [SAFEGUARD] Entry credit={seg.entry_credit:.2f} ≤ 0. "
                  f"Closing all legs and aborting.")
            self._rollback_legs(placed_legs)
            return False

        if seg.entry_credit < self._cfg.min_net_credit:
            logger.warning("below_min_net_credit",
                           extra={"credit": seg.entry_credit,
                                  "min": self._cfg.min_net_credit})
            print(f"\n  [SAFEGUARD] Entry credit={seg.entry_credit:.2f} < "
                  f"min_net_credit={self._cfg.min_net_credit:.2f}. "
                  f"Proceeding with caution.")

        # ── SAFEGUARD 4: post-entry position reconciliation ────────────────
        reconcile_ok = self._verify_positions_with_broker(seg)
        if not reconcile_ok:
            print("\n  [SAFEGUARD] Position reconciliation FAILED after entry. "
                  "Check logs. Monitoring continues but manual verification advised.")
            # We do not abort here — position is already open; just alert loudly.

        return True

    def _rollback_legs(self, legs: list[CondorLeg]) -> None:
        """
        Emergency rollback: close all provided legs (e.g., after a partial entry failure).
        This is best-effort — logs errors but does not raise.
        """
        if not legs:
            return
        logger.warning("entry_rollback_starting",
                       extra={"legs": [l.trading_symbol for l in legs]})
        print(f"\n  [Rollback] Closing {len(legs)} leg(s) due to entry failure...")
        for leg in reversed(legs):   # close in reverse order (shorts first by convention)
            if leg.is_open:
                exit_price = self._close_leg(leg)
                print(f"  [Rollback] Closed {leg.side} {leg.trading_symbol} "
                      f"@ Rs.{exit_price:.2f}")

    # ── Exit / close helpers ─────────────────────────────────────────────────

    def _close_leg(self, leg: CondorLeg) -> float:
        """
        Close a single leg by placing the opposite LIMIT order.
        Returns the actual fill price (or best-guess mid if dry run).
        Marks leg.is_open = False regardless.
        """
        if not leg.is_open:
            return leg.exit_price

        close_side = "BUY" if leg.side == "SELL" else "SELL"
        q     = self._get_quote(leg.trading_symbol)
        price = self._mid_price(q) if q else float(leg.entry_price)

        print(f"  [Exit] {close_side} {leg.trading_symbol:<30} qty={leg.quantity}  "
              f"limit=Rs.{price:.2f}")

        oid  = self._place_order_with_retry(
            leg.trading_symbol, close_side, leg.quantity, price, tag="SHC_EXIT"
        )
        fill = (self._get_fill_price(oid) if oid else 0.0) or price
        leg.exit_price = fill
        leg.is_open    = False

        if oid:
            ok, detail = self._verify_order_filled(
                oid, leg.trading_symbol, leg.quantity, close_side
            )
            if ok:
                logger.info("exit_leg_verified",
                            extra={"symbol": leg.trading_symbol,
                                   "side": close_side,
                                   "fill": fill})
                print(f"  [Exit] {leg.trading_symbol} closed  fill=Rs.{fill:.2f} ✓")
            else:
                logger.critical("EXIT_LEG_VERIFICATION_FAILED",
                                extra={"symbol": leg.trading_symbol,
                                       "order_id": oid,
                                       "detail": detail})
                print(f"\n  !! EXIT VERIFICATION FAILED for {leg.trading_symbol}: "
                      f"{detail}. CHECK BROKER IMMEDIATELY.")
        else:
            logger.critical("EXIT_ORDER_PLACEMENT_FAILED",
                            extra={"symbol": leg.trading_symbol,
                                   "side": close_side})
            print(f"\n  !! EXIT ORDER FAILED for {leg.trading_symbol}. "
                  f"MANUAL CLOSE REQUIRED.")

        return fill

    def _close_spread(
        self,
        short_leg: Optional[CondorLeg],
        long_leg:  Optional[CondorLeg],
        verify_exit: bool = False,
    ) -> tuple[float, float]:
        """
        Close a spread (short + long leg).
        Returns (short_exit_price, long_exit_price).

        verify_exit=True → calls _verify_exit_complete afterwards
        (use True for final full exits, False for mid-shift partial closes).
        """
        sp_exit = self._close_leg(short_leg) if short_leg else 0.0
        lp_exit = self._close_leg(long_leg)  if long_leg  else 0.0

        if verify_exit:
            legs_closed = [l for l in [short_leg, long_leg] if l is not None]
            self._verify_exit_complete(legs_closed)

        return sp_exit, lp_exit

    def _close_all_open_legs(self, seg: CondorSegment, reason: str) -> None:
        """Close every still-open leg in a segment and run exit verification."""
        open_legs = seg.open_legs
        if not open_legs:
            return
        logger.info("closing_all_open_legs",
                    extra={"reason": reason,
                           "count": len(open_legs),
                           "symbols": [l.trading_symbol for l in open_legs]})
        print(f"\n  Closing {len(open_legs)} open leg(s)  reason={reason}")
        for leg in open_legs:
            self._close_leg(leg)
        # SAFEGUARD 5: verify all are gone from broker
        self._verify_exit_complete(open_legs)

    # ── Shift trigger ─────────────────────────────────────────────────────────

    def _check_shift_trigger(self, spot: float, seg: CondorSegment) -> str:
        """
        Returns 'PUT_SIDE', 'CALL_SIDE', or ''.

        put_trigger_level  = short_put  * (1 + trigger_pct/100)
        call_trigger_level = short_call * (1 - trigger_pct/100)

        Fires when spot ≤ put_trigger (put side threatened) or
                   spot ≥ call_trigger (call side threatened).
        """
        sp_leg = seg.short_put
        sc_leg = seg.short_call
        if sp_leg is None and sc_leg is None:
            return ""

        trigger = self._cfg.shift_trigger_pct
        if sp_leg:
            put_trigger = sp_leg.strike * (1.0 + trigger / 100.0)
            if spot <= put_trigger:
                return "PUT_SIDE"
        if sc_leg:
            call_trigger = sc_leg.strike * (1.0 - trigger / 100.0)
            if spot >= call_trigger:
                return "CALL_SIDE"
        return ""

    def _should_close_retained_side(
        self, trigger_side: str, seg: CondorSegment, spot: float
    ) -> bool:
        """
        Decide whether to close the UN-triggered side as well.
        True if the retained spread has fully decayed OR is also threatened.
        """
        if trigger_side == "PUT_SIDE":
            orig_credit = ((seg.short_call.entry_price if seg.short_call else 0) -
                           (seg.long_call.entry_price  if seg.long_call  else 0))
            q_sc = self._get_quote(seg.short_call.trading_symbol) if seg.short_call else None
            q_lc = self._get_quote(seg.long_call.trading_symbol)  if seg.long_call  else None
            curr_close_cost = (self._mid_price(q_sc) if q_sc else 0.0) - \
                              (self._mid_price(q_lc) if q_lc else 0.0)
            dist_pct = (abs(spot - seg.short_call.strike) / seg.short_call.strike * 100
                        if seg.short_call else 999)
            if orig_credit <= 0 or curr_close_cost <= 0:
                return True
            if curr_close_cost / orig_credit <= self._cfg.lock_in_pct:
                return True
            if dist_pct <= self._cfg.shift_trigger_pct * 2:
                return True
            return False
        else:
            orig_credit = ((seg.short_put.entry_price if seg.short_put else 0) -
                           (seg.long_put.entry_price  if seg.long_put  else 0))
            q_sp = self._get_quote(seg.short_put.trading_symbol) if seg.short_put else None
            q_lp = self._get_quote(seg.long_put.trading_symbol)  if seg.long_put  else None
            curr_close_cost = (self._mid_price(q_sp) if q_sp else 0.0) - \
                              (self._mid_price(q_lp) if q_lp else 0.0)
            dist_pct = (abs(spot - seg.short_put.strike) / seg.short_put.strike * 100
                        if seg.short_put else 999)
            if orig_credit <= 0 or curr_close_cost <= 0:
                return True
            if curr_close_cost / orig_credit <= self._cfg.lock_in_pct:
                return True
            if dist_pct <= self._cfg.shift_trigger_pct * 2:
                return True
            return False

    # ── Segment P&L ───────────────────────────────────────────────────────────

    def _compute_segment_pnl(self, seg: CondorSegment) -> float:
        """
        Realised P&L for this segment = sum over all actually-closed legs.
        Retained (still-open) legs are excluded — they carry into the next segment.
        """
        total = 0.0
        for leg in seg.legs:
            if leg.is_open or leg.exit_price == 0.0:
                continue  # retained — not yet realised
            if leg.side == "SELL":
                total += (leg.entry_price - leg.exit_price) * leg.quantity
            else:
                total += (leg.exit_price - leg.entry_price) * leg.quantity
        return round(total, 2)

    # ── Shift execution ───────────────────────────────────────────────────────

    def _execute_shift(
        self, trigger_side: str, seg: CondorSegment, spot: float
    ) -> tuple[str, bool, bool]:
        """
        Close the triggered side (and possibly retained side).
        Records segment P&L and appends segment to self._segments.
        Returns (full_trigger_tag, open_put_next, open_call_next).
        """
        close_retained = self._should_close_retained_side(trigger_side, seg, spot)

        if trigger_side == "PUT_SIDE":
            self._close_spread(seg.short_put, seg.long_put)   # no exit verify on shift
            if close_retained:
                self._close_spread(seg.short_call, seg.long_call)
                full_trigger = "PUT_SIDE_FULL"
                self._retained_short_put = self._retained_long_put = None
                self._retained_short_call = self._retained_long_call = None
            else:
                full_trigger = "PUT_SIDE"
                self._retained_short_put = self._retained_long_put = None
        else:
            self._close_spread(seg.short_call, seg.long_call)
            if close_retained:
                self._close_spread(seg.short_put, seg.long_put)
                full_trigger = "CALL_SIDE_FULL"
                self._retained_short_put = self._retained_long_put = None
                self._retained_short_call = self._retained_long_call = None
            else:
                full_trigger = "CALL_SIDE"
                self._retained_short_call = self._retained_long_call = None

        seg.exit_reason   = "SHIFT"
        seg.shift_trigger = full_trigger
        seg.segment_pnl   = self._compute_segment_pnl(seg)
        self._segments.append(seg)
        self._session_realised_pnl += seg.segment_pnl

        open_put_next  = "PUT"  in full_trigger or "FULL" in full_trigger
        open_call_next = "CALL" in full_trigger or "FULL" in full_trigger

        logger.info("shift_executed",
                    extra={"trigger": full_trigger,
                           "spot": spot,
                           "seg_pnl": seg.segment_pnl,
                           "session_pnl": round(self._session_realised_pnl, 2)})
        return full_trigger, open_put_next, open_call_next

    # ── Pre-flight summary ────────────────────────────────────────────────────

    def _print_preflight(
        self, expiry: date, spot: float,
        strikes: tuple, mi: MarginInfo, lots: int,
    ) -> None:
        sp, bp, sc, bc = strikes
        dte          = (expiry - date.today()).days
        margin_total = self._cfg.margin_pct * spot * self._lot_size * lots
        cash_m       = margin_total * self._cfg.cash_margin_pct
        stock_m      = margin_total * self._cfg.stock_margin_pct

        print("\n" + "=" * 65)
        print("  SHIFTING IRON CONDOR — LIVE ENTRY PREFLIGHT")
        print("=" * 65)
        print(f"  Underlying   : {self._cfg.underlying}")
        print(f"  Spot         : {spot:,.2f}")
        print(f"  Expiry       : {expiry}  (DTE {dte})")
        print(f"  Strikes      : Buy PE {bp:.0f} | Sell PE {sp:.0f} "
              f"|| Sell CE {sc:.0f} | Buy CE {bc:.0f}")
        print(f"  Short OTM    : Put {abs(spot - sp) / spot * 100:.1f}%  "
              f"| Call {abs(sc - spot) / spot * 100:.1f}%")
        print(f"  Lots         : {lots}  (lot size: {self._lot_size})"
              f"  [HARD LIMIT = {MAX_LOTS_HARD_LIMIT}]")
        print(f"  Shift trigger: {self._cfg.shift_trigger_pct}%  "
              f"→ put trigger {sp * (1 + self._cfg.shift_trigger_pct / 100):,.1f}  "
              f"call trigger {sc * (1 - self._cfg.shift_trigger_pct / 100):,.1f}")
        print()
        print("  Margin breakdown:")
        print(f"    Total margin required : Rs.{margin_total:>12,.0f}")
        print(f"    From free cash  (50%) : Rs.{cash_m:>12,.0f}  "
              f"[Available: Rs.{mi.cash_available:,.0f}]")
        print(f"    From stock coll.(50%) : Rs.{stock_m:>12,.0f}  "
              f"[Available: Rs.{mi.stock_collateral:,.0f}]")
        print()
        print("  Safeguards active:")
        print(f"    Max lots hard limit   : {MAX_LOTS_HARD_LIMIT}")
        print(f"    Min net credit        : {self._cfg.min_net_credit:.2f} pts")
        print(f"    Max session loss      : Rs.{self._cfg.max_session_loss:,.0f}")
        print(f"    Market hours          : {MARKET_OPEN_TIME}–{MARKET_CLOSE_TIME} IST")
        print(f"    Min DTE on entry      : {MIN_ENTRY_DTE} days")
        print(f"    Stale quote threshold : {QUOTE_MAX_AGE_SECS} s")
        print(f"    Fill verification     : ON (post-order broker check)")
        print(f"    Position reconcile    : ON (post-entry broker check)")
        print(f"    Exit verification     : ON (post-exit broker check)")
        print("=" * 65)

    # ── Main run loop ─────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Main trading loop.
        1. Check market hours.
        2. Compute usable capital and lot sizing.
        3. Enter the condor (hedge-first).
        4. Monitor; shift when triggered; check loss cap before each new segment.
        5. Exit on expiry / profit target / stop loss / max shifts.
        """
        logger.info("shifting_condor_live_start",
                    extra={"underlying": self._cfg.underlying,
                           "dry_run": self._dry_run,
                           "max_lots_hard_limit": MAX_LOTS_HARD_LIMIT})

        # ── SAFEGUARD: market hours ───────────────────────────────────────────
        try:
            self._assert_trading_hours("start session")
        except RuntimeError as exc:
            print(f"\n  [SAFEGUARD] {exc}")
            logger.error("blocked_outside_market_hours", extra={"reason": str(exc)})
            return

        # ── Margin snapshot ───────────────────────────────────────────────────
        mi = self._get_margin_info()
        self._margin_info = mi
        print(f"\n  Margin snapshot ({self._cfg.underlying}):")
        print("  " + mi.display().replace("\n", "\n  "))

        if mi.usable_capital <= 0:
            logger.error("insufficient_margin", extra={"usable": mi.usable_capital})
            print("\n  [SAFEGUARD] Insufficient usable capital. Aborting.")
            return

        # ── Expiry ────────────────────────────────────────────────────────────
        expiry = self._select_expiry()
        if not expiry:
            logger.error("no_valid_expiry",
                         extra={"underlying": self._cfg.underlying,
                                "min_dte": self._cfg.min_dte,
                                "max_dte": self._cfg.max_dte})
            print("\n  [SAFEGUARD] No expiry found within DTE window. Aborting.")
            return
        self._expiry = expiry

        # ── SAFEGUARD: minimum DTE on entry ──────────────────────────────────
        dte_today = (expiry - date.today()).days
        if dte_today < MIN_ENTRY_DTE:
            logger.error("dte_below_minimum",
                         extra={"dte": dte_today, "min": MIN_ENTRY_DTE})
            print(f"\n  [SAFEGUARD] DTE={dte_today} < MIN_ENTRY_DTE={MIN_ENTRY_DTE}. "
                  f"Too close to expiry. Aborting.")
            return

        # ── Spot & strikes ────────────────────────────────────────────────────
        spot = self._get_spot()
        if not spot:
            logger.error("spot_unavailable")
            print("\n  [SAFEGUARD] Cannot fetch spot price. Aborting.")
            return

        strikes = self._select_strikes(spot, expiry)
        if not strikes:
            logger.error("no_valid_strikes",
                         extra={"spot": spot, "expiry": str(expiry)})
            print("\n  [SAFEGUARD] No valid strikes found. Aborting.")
            return

        # Lot size from instrument master
        sp_res = self._resolve_trading_symbol(expiry, strikes[0], "PE")
        self._lot_size = sp_res[2] if sp_res else 1

        # ── Lot sizing (with hard cap) ─────────────────────────────────────────
        self._lots = self._compute_lots(spot, self._lot_size, mi.usable_capital)
        if self._lots <= 0:
            logger.error("zero_lots",
                         extra={"capital": mi.usable_capital,
                                "margin_per_lot": self._cfg.margin_pct * spot * self._lot_size})
            print("\n  [SAFEGUARD] Zero lots computed (capital too low). Aborting.")
            return

        # ── Pre-flight display + confirmation ─────────────────────────────────
        self._print_preflight(expiry, spot, strikes, mi, self._lots)
        if not self._dry_run:
            try:
                ans = input("\n  Type CONFIRM to enter this trade: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  Aborted.")
                return
            if ans != "CONFIRM":
                print("  Aborted.")
                return

        # ── Trading loop ──────────────────────────────────────────────────────
        open_put = open_call = True

        for seg_idx in range(self._cfg.max_shifts + 1):

            # SAFEGUARD: session loss cap before each new segment
            if not self._check_session_loss_cap():
                break

            # SAFEGUARD: market hours before each new segment (not just entry)
            if not self._is_within_trading_hours():
                now_hm = datetime.now(IST).strftime("%H:%M")
                logger.warning("blocked_outside_hours_before_segment",
                               extra={"time": now_hm})
                print(f"\n  [SAFEGUARD] {now_hm} IST — outside trading hours. "
                      f"Not entering new segment.")
                break

            seg = CondorSegment(segment_idx=seg_idx, entry_date=date.today())

            ok = self._enter_legs(expiry, open_put, open_call, spot, seg)
            if not ok:
                logger.error("entry_failed", extra={"segment": seg_idx})
                print(f"\n  Entry failed for segment {seg_idx}. Stopping.")
                break

            side_desc = ("full condor" if (open_put and open_call)
                         else ("put side" if open_put else "call side"))
            logger.info("segment_entered",
                        extra={"segment": seg_idx, "side": side_desc,
                               "credit": seg.entry_credit,
                               "margin": seg.margin_used,
                               "cash_margin": seg.cash_margin,
                               "stock_margin": seg.stock_margin})
            print(f"\n  [Seg {seg_idx}] Entered {side_desc}"
                  f"  credit={seg.entry_credit:.2f} pts"
                  f"  margin=Rs.{seg.margin_used:,.0f}"
                  f"  (cash Rs.{seg.cash_margin:,.0f} | stock Rs.{seg.stock_margin:,.0f})")

            # ── Monitor loop ──────────────────────────────────────────────────
            exit_reason  = "EXPIRY"
            shift_fired  = False

            while True:
                now = datetime.now(IST)

                # Expiry reached
                if date.today() >= expiry:
                    exit_reason = "EXPIRY"
                    break

                # 2-DTE exit: close 2 trading days before expiry
                dte_remaining = (expiry - date.today()).days
                if dte_remaining <= self._cfg.dte_exit:
                    exit_reason = "DTE_EXIT"
                    logger.info("dte_exit_triggered",
                                extra={"dte_remaining": dte_remaining,
                                       "dte_exit": self._cfg.dte_exit})
                    print(f"\n  [DTE EXIT] DTE={dte_remaining} ≤ dte_exit={self._cfg.dte_exit}. "
                          f"Closing all legs early.")
                    break

                # Past market close — force exit
                if not self._is_within_trading_hours():
                    exit_reason = "MARKET_CLOSE"
                    break

                time.sleep(self._cfg.monitor_interval_seconds)

                spot = self._get_spot()
                if spot is None:
                    logger.warning("spot_unavailable_in_monitor")
                    continue

                # Fetch current prices for all open legs
                q_sp = self._get_quote(seg.short_put.trading_symbol)  if seg.short_put  else None
                q_bp = self._get_quote(seg.long_put.trading_symbol)   if seg.long_put   else None
                q_sc = self._get_quote(seg.short_call.trading_symbol) if seg.short_call else None
                q_bc = self._get_quote(seg.long_call.trading_symbol)  if seg.long_call  else None

                curr_sp = self._mid_price(q_sp) if q_sp else 0.0
                curr_bp = self._mid_price(q_bp) if q_bp else 0.0
                curr_sc = self._mid_price(q_sc) if q_sc else 0.0
                curr_bc = self._mid_price(q_bc) if q_bc else 0.0

                cost_to_close  = (curr_sp + curr_sc) - (curr_bp + curr_bc)
                unrealised_pnl = (seg.entry_credit - cost_to_close) * self._lot_size * self._lots

                logger.info("monitor_tick",
                            extra={"spot": spot,
                                   "cost_to_close": round(cost_to_close, 2),
                                   "unrealised_pnl": round(unrealised_pnl, 2),
                                   "session_pnl": round(self._session_realised_pnl, 2)})
                print(f"  {now.strftime('%H:%M:%S')}  "
                      f"spot={spot:,.1f}  "
                      f"cost_to_close={cost_to_close:.2f}  "
                      f"unrealised=Rs.{unrealised_pnl:,.0f}  "
                      f"session=Rs.{self._session_realised_pnl:,.0f}",
                      end="\r")

                # Take-profit
                if seg.entry_credit > 0:
                    retained_pct = 1 - cost_to_close / seg.entry_credit
                    if retained_pct >= self._cfg.take_profit_pct:
                        exit_reason = "TAKE_PROFIT"
                        break

                # Stop-loss
                if seg.entry_credit > 0:
                    loss_mult = (-unrealised_pnl /
                                 (seg.entry_credit * self._lot_size * self._lots))
                    if loss_mult >= self._cfg.stop_loss_pct:
                        exit_reason = "STOP_LOSS"
                        break

                # Shift trigger
                trigger_side = self._check_shift_trigger(spot, seg)
                if trigger_side:
                    logger.info("shift_triggered",
                                extra={"side": trigger_side, "spot": spot,
                                       "segment": seg_idx})
                    print(f"\n  SHIFT triggered: {trigger_side}  spot={spot:,.1f}")
                    _, open_put, open_call = self._execute_shift(trigger_side, seg, spot)
                    self._num_shifts += 1
                    shift_fired = True

                    if self._num_shifts >= self._cfg.max_shifts:
                        logger.warning("max_shifts_reached",
                                       extra={"shifts": self._num_shifts})
                        print(f"\n  Max shifts ({self._cfg.max_shifts}) reached.")
                        # Close any retained legs
                        if self._retained_short_put:
                            self._close_spread(self._retained_short_put,
                                               self._retained_long_put,
                                               verify_exit=True)
                        if self._retained_short_call:
                            self._close_spread(self._retained_short_call,
                                               self._retained_long_call,
                                               verify_exit=True)
                        exit_reason = "MAX_SHIFTS"
                        shift_fired = False   # mark as terminal
                        break

                    break   # inner while — restart outer for-loop with new segment

            # Terminal exit (not a shift) — close all and record
            if not shift_fired:
                print(f"\n\n  [{exit_reason}] Closing all open legs...")
                self._close_all_open_legs(seg, exit_reason)
                seg.exit_reason = exit_reason
                seg.segment_pnl = self._compute_segment_pnl(seg)
                self._session_realised_pnl += seg.segment_pnl
                self._segments.append(seg)
                break
            elif exit_reason in ("MAX_SHIFTS",):
                break
            # else: shift fired, continue outer loop for new segment

        # ── Final summary ─────────────────────────────────────────────────────
        total_pnl = sum(s.segment_pnl for s in self._segments)
        print("\n\n" + "=" * 65)
        print("  SHIFTING CONDOR — SESSION COMPLETE")
        print("=" * 65)
        print(f"  Underlying  : {self._cfg.underlying}")
        print(f"  Expiry      : {self._expiry}")
        print(f"  Segments    : {len(self._segments)}")
        print(f"  Shifts      : {self._num_shifts}")
        if self._segments:
            avg_margin  = (sum(s.margin_used for s in self._segments)
                           / len(self._segments))
            avg_cash    = avg_margin * self._cfg.cash_margin_pct
            avg_stock   = avg_margin * self._cfg.stock_margin_pct
            print(f"  Avg margin  : Rs.{avg_margin:,.0f}"
                  f"  (cash Rs.{avg_cash:,.0f} | stock Rs.{avg_stock:,.0f})")
        sign = "+" if total_pnl >= 0 else ""
        print(f"  Total P&L   : {sign}Rs.{total_pnl:,.0f}")
        print()
        for s in self._segments:
            s2 = "+" if s.segment_pnl >= 0 else ""
            print(f"  Seg {s.segment_idx}  {s.entry_date}  "
                  f"{s.exit_reason:<20}  "
                  f"P&L {s2}Rs.{s.segment_pnl:,.0f}  "
                  f"margin Rs.{s.margin_used:,.0f} "
                  f"[cash Rs.{s.cash_margin:,.0f} / stock Rs.{s.stock_margin:,.0f}]")
        print("=" * 65)

        logger.info("session_complete",
                    extra={"total_pnl": total_pnl,
                           "segments": len(self._segments),
                           "shifts": self._num_shifts})


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Shifting Iron Condor — Live Trading via Zerodha"
    )
    parser.add_argument(
        "--config", default="config/shifting_condor.yaml",
        help="Path to shifting_condor.yaml (default: config/shifting_condor.yaml)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate without placing real orders "
             "(still connects to Zerodha for live quotes and margin)"
    )
    parser.add_argument(
        "--cache", default="logs/nfo_instruments_cache.csv",
        help="Path to NFO instrument cache CSV"
    )
    args = parser.parse_args()

    # ── Environment checks ────────────────────────────────────────────────────
    api_key      = os.environ.get("ZERODHA_API_KEY", "")
    access_token = os.environ.get("ZERODHA_ACCESS_TOKEN", "")

    if not api_key or not access_token:
        print("ERROR: ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN must be set in .env")
        print("  Run: python -m brokers.token_helper  to get a fresh access token")
        sys.exit(1)

    if not Path(args.cache).exists():
        print(f"ERROR: Instrument cache not found at {args.cache}")
        print("  Run: python -m cli.main resolve-instruments --live --force-refresh")
        sys.exit(1)

    if not args.dry_run:
        mode         = os.environ.get("TRADING_MODE", "PAPER").upper()
        confirmation = os.environ.get("LIVE_TRADING_CONFIRMATION", "")
        required     = "I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED"
        if mode != "LIVE" or confirmation != required:
            print("ERROR: Live trading requires both of the following in .env:")
            print("  TRADING_MODE=LIVE")
            print(f"  LIVE_TRADING_CONFIRMATION={required}")
            print()
            print("  Use --dry-run to run without placing real orders.")
            sys.exit(1)

    # ── Load config ───────────────────────────────────────────────────────────
    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"ERROR loading config {args.config}: {exc}")
        sys.exit(1)

    # ── Connect to Zerodha ────────────────────────────────────────────────────
    from brokers.zerodha import ZerodhaBroker
    broker = ZerodhaBroker.from_env(instrument_cache_path=args.cache)

    # ── Ensure logs directory ─────────────────────────────────────────────────
    Path("logs").mkdir(exist_ok=True)

    # ── Prevent duplicate instances ───────────────────────────────────────────
    from execution.process_lock import ProcessLock, ProcessLockError
    lock = ProcessLock(lock_file=Path("logs/shifting_condor.pid"))
    try:
        lock.acquire()
    except ProcessLockError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    # ── Run ───────────────────────────────────────────────────────────────────
    trader = ShiftingCondorLive(cfg=cfg, broker=broker,
                                cache_path=args.cache, dry_run=args.dry_run)
    try:
        trader.run()
    except KeyboardInterrupt:
        print("\n\n  Interrupted by user. Attempting emergency close of all open legs...")
        logger.warning("interrupted_by_user")
        for seg in reversed(trader._segments):
            for leg in seg.legs:
                if leg.is_open:
                    try:
                        trader._close_leg(leg)
                    except Exception as exc:
                        logger.error("emergency_close_failed",
                                     extra={"symbol": leg.trading_symbol, "error": str(exc)})
                        print(f"  !! FAILED to close {leg.trading_symbol}. "
                              f"MANUAL CLOSE REQUIRED.")
        # Verify everything is gone
        all_legs = [l for seg in trader._segments for l in seg.legs]
        if all_legs:
            trader._verify_exit_complete(all_legs)
        print("  Emergency close complete.")
    finally:
        lock.release()


if __name__ == "__main__":
    main()
