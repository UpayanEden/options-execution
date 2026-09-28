"""
Fully automated trading engine — paper and live modes.

Strategies evaluated each scan cycle:
  - Iron Condor           (IC)  — 4-leg, collects from both wings
  - Broken Wing Butterfly (BWB) — 4-leg, skewed butterfly
  - Bull Put Spread       (BPS) — 2-leg put spread, neutral-to-bullish
  - Bear Call Spread      (BCS) — 2-leg call spread, neutral-to-bearish
  - Calendar Spread       (CAL) — 2-leg, two expiries

Lifecycle per day:
  09:30 IST  — scan all configured underlyings × strategy types;
               auto-select strikes until credit ≥ minimum;
               enter the single best candidate by composite opportunity score
  every 15m  — check open positions:
               exit if profit_target_pct reached
               exit if loss > stop_loss_mult × credit
               exit if DTE ≤ dte_exit
  every 1h   — reconcile local DB positions vs broker positions
  end of day — log P&L, update monthly tracker

Run with:
  python run_auto_trader.py
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


# ── Config ──────────────────────────────────────────────────────────────────

class AutoTraderConfig:
    def __init__(self, raw: dict) -> None:
        at = raw.get("auto_trader", {})
        self.enabled:    bool  = at.get("enabled", False)
        self.mode:       str   = at.get("mode", "paper")
        raw_ul = at.get("underlyings", ["NIFTY", "BANKNIFTY", "FINNIFTY"])
        self.underlyings_auto: bool = (raw_ul == "auto" or raw_ul == ["auto"])
        self.underlyings: list[str] = [] if self.underlyings_auto else raw_ul
        self.exchange:   str   = at.get("exchange", "NFO")

        exp = at.get("expiry", {})
        self.min_dte:       int = int(exp.get("min_dte", 15))
        self.max_dte:       int = int(exp.get("max_dte", 45))
        self.preferred_dte: int = int(exp.get("preferred_dte", 30))

        entry = at.get("entry", {})
        self.entry_time:        str     = entry.get("time", "09:30")
        self.iv_assumption:     Decimal = Decimal(str(entry.get("iv_assumption", 0.30)))
        self.short_mult_start:  float   = float(entry.get("short_mult_start", 1.0))
        self.short_mult_min:    float   = float(entry.get("short_mult_min", 0.30))
        self.max_concurrent:    int     = int(entry.get("max_concurrent_positions", 1))
        self.require_confirmation: bool = bool(entry.get("require_confirmation", False))
        self.max_losing_trades:    int  = int(entry.get("max_losing_trades_per_day", 2))

        ex = at.get("exit", {})
        self.profit_target_pct:   Decimal = Decimal(str(ex.get("profit_target_pct", 50))) / 100
        self.stop_loss_mult:      Decimal = Decimal(str(ex.get("stop_loss_mult", 2.0)))
        self.dte_exit:            int     = int(ex.get("dte_exit", 2))
        self.monitor_interval:    int     = int(ex.get("monitor_interval_seconds", 900))

        self.monthly_target_pct: Decimal = Decimal(str(at.get("monthly_target_pct", 10.0)))

        # ── Intelligence filters ─────────────────────────────────────────────
        filters = at.get("filters", {})
        iv_r = filters.get("iv_rank", {})
        self.iv_rank_enabled:  bool  = iv_r.get("enabled", True)
        self.iv_rank_min:      float = float(iv_r.get("min_rank", 30.0))
        self.iv_rank_ideal:    float = float(iv_r.get("ideal_min", 50.0))

        vix_f = filters.get("vix", {})
        self.vix_enabled:  bool  = vix_f.get("enabled", True)
        self.vix_cfg:      dict  = vix_f

        evt = filters.get("event_blackout", {})
        self.event_enabled:   bool = evt.get("enabled", True)
        self.event_blackout:  int  = int(evt.get("days_before", 2))

        timing = at.get("timing", {})
        self.respect_timing_windows: bool  = timing.get("respect_windows", True)
        self.min_timing_score:       float = float(timing.get("min_timing_score", 40.0))

        self.notifications: dict = at.get("notifications", {})

        # Strategy types to attempt (order defines preference within scan loop)
        raw_strats = at.get("strategies", ["iron_condor", "bull_put_spread", "bear_call_spread"])
        self.enabled_strategies: list[str] = [s.lower() for s in raw_strats]


def load_auto_trader_config(path: str = "config/auto_trader.yaml") -> AutoTraderConfig:
    import yaml
    with open(path) as f:
        return AutoTraderConfig(yaml.safe_load(f))


# ── AutoTrader ───────────────────────────────────────────────────────────────

class AutoTrader:
    """
    Manages the full automated trading lifecycle (paper or live).
    One instance per process. Call .run() to start the loop.
    """

    def __init__(
        self,
        cfg: AutoTraderConfig,
        risk,                   # RiskConfig
        broker,                 # ZerodhaBroker
        cache_path: str,
        db_path: str,
        paper_cfg=None,         # PaperConfig — only used when mode=paper
    ) -> None:
        self._cfg       = cfg
        self._risk      = risk
        self._paper_cfg = paper_cfg
        self._broker    = broker
        self._cache     = cache_path
        self._db        = db_path

        if cfg.underlyings_auto:
            from stock_picker import get_all_nfo_underlyings
            cfg.underlyings = get_all_nfo_underlyings(cache_path)
            logger.info("underlyings_auto_loaded", extra={"count": len(cfg.underlyings)})

        self._entered_today:   bool          = False
        self._last_entry_date: Optional[date] = None
        self._losing_trades_today: int        = 0
        self._last_monitor_ts: float         = 0.0

        # Trailing stop: track highest % of credit captured per open position.
        # Once a position captures ≥ trailing_lock_pct it switches to
        # a breakeven stop (never let a winner become a loser).
        self._position_hwm: dict[str, Decimal] = {}   # strategy_id → max pct captured

        # Intelligence modules
        from monitoring.notifier import Notifier
        self._notifier = Notifier.from_config(cfg.notifications)

        # Shared alert manager — wires Telegram when configured
        self._alert_manager = self._build_alert_manager()

        # State tracking
        self._daily_loss_halted: bool = False
        self._last_reconcile_ts: float = 0.0

        # Adaptive learner — adjusts quality thresholds based on trade outcomes
        from monitoring.adaptive_learner import AdaptiveLearner
        self._learner = AdaptiveLearner(
            iv_rank_baseline=cfg.iv_rank_min,
            timing_baseline=cfg.min_timing_score,
            ror_baseline=float(risk.min_ror_pct),
        )
        self._learner.apply_to_config(self._cfg, self._risk)

    # ── Alert manager factory ────────────────────────────────────────────────

    def _build_alert_manager(self):
        from monitoring.alerts import (
            AlertManager, ConsoleAlertChannel, LogAlertChannel, TelegramAlertChannel,
        )
        from execution.models import AlertLevel
        channels = [ConsoleAlertChannel(), LogAlertChannel()]
        tg = self._cfg.notifications.get("telegram", {})
        if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
            channels.append(TelegramAlertChannel(
                bot_token=tg["bot_token"],
                chat_id=str(tg["chat_id"]),
                min_level=AlertLevel.CRITICAL,
            ))
        return AlertManager(channels=channels)

    # ── Trade confirmation ───────────────────────────────────────────────────

    def _print_trade_summary(self, best: dict, legs_cfg: list) -> None:
        from execution.models import Side
        strat_type = best.get("strategy_type", "")
        print("\n" + "=" * 60)
        print("  TRADE CONFIRMATION REQUIRED")
        print("=" * 60)
        print(f"  Underlying : {best['underlying']}")
        print(f"  Strategy   : {strat_type.value if hasattr(strat_type, 'value') else strat_type}")
        print(f"  Expiry     : {best['expiry']}  (DTE {best['dte']})")
        print(f"  Credit     : {float(best['credit']):.2f} pts")
        print(f"  RoR        : {float(best['ror']):.1f}%")
        print(f"  Max loss   : ₹{float(best['max_loss']):.0f}")
        print(f"  IV rank    : {best.get('iv_rank', '?')}")
        print(f"  Score      : {best.get('opp_score', '?')} [{best.get('opp_grade', '?')}]")
        print("  Legs:")
        for leg in legs_cfg:
            side_str = "BUY " if leg.side == Side.BUY else "SELL"
            print(f"    {side_str}  {leg.option_type.value}  {leg.strike}")
        print("=" * 60)

    def _await_confirmation(self, timeout: int = 60) -> bool:
        import select, sys
        print(f"\n  Approve this trade? [y/N]  ({timeout}s to respond, default=skip) ", end="", flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if ready:
            answer = sys.stdin.readline().strip().lower()
            if answer in ("y", "yes"):
                print("  >>> Trade APPROVED\n")
                logger.info("trade_approved_by_user")
                return True
        print("  >>> Trade SKIPPED\n")
        return False

    # ── Daily P&L ────────────────────────────────────────────────────────────

    def _daily_pnl(self) -> Decimal:
        """Return total realized P&L for today's completed trades."""
        try:
            from sqlalchemy import text
            from storage.database import create_db_engine
            today = date.today().isoformat()
            engine = create_db_engine(self._db)
            with engine.connect() as conn:
                result = conn.execute(
                    text(
                        "SELECT COALESCE(SUM(final_pnl), 0) FROM strategies "
                        "WHERE state = 'EXIT_COMPLETE' "
                        "AND date(created_at) = :today"
                    ),
                    {"today": today},
                )
                return Decimal(str(result.scalar() or 0))
        except Exception:
            return Decimal("0")

    def _count_losing_trades_today(self) -> int:
        """Count completed trades with a net loss today."""
        try:
            from sqlalchemy import text
            from storage.database import create_db_engine
            today = date.today().isoformat()
            engine = create_db_engine(self._db)
            with engine.connect() as conn:
                result = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM strategies "
                        "WHERE state = 'EXIT_COMPLETE' "
                        "AND final_pnl < 0 "
                        "AND date(created_at) = :today "
                        f"AND trading_mode = '{self._cfg.mode.upper()}'"
                    ),
                    {"today": today},
                )
                return result.scalar() or 0
        except Exception:
            return 0

    # ── Reconciliation ───────────────────────────────────────────────────────

    def _reconcile(self) -> None:
        """Compare local DB positions against broker-reported positions and alert on mismatches."""
        try:
            from execution.models import LegPosition, OptionType, Side
            from execution.reconciliation import Reconciliation

            open_pos = self._get_open_positions()
            if not open_pos:
                return

            broker_positions = self._broker.get_positions()

            # Flatten local positions to a list of LegPosition objects
            local: list[LegPosition] = []
            for pos in open_pos:
                for row in pos["legs"]:
                    sym = str(row["trading_symbol"])
                    opt = OptionType.CE if sym.endswith("CE") else OptionType.PE
                    local.append(LegPosition(
                        leg_index=int(row["leg_index"]),
                        trading_symbol=sym,
                        instrument_token=int(row["instrument_token"]),
                        side=Side(str(row["side"])),
                        option_type=opt,
                        strike=Decimal("0"),
                        lot_size=1, lots=1,
                        quantity=int(row["quantity"]),
                        average_entry_price=Decimal(str(row["average_entry_price"])),
                    ))

            result = Reconciliation().reconcile(local, broker_positions)
            if not result.matched:
                for mismatch in result.mismatches:
                    self._alert_manager.critical(
                        "reconciliation_mismatch",
                        f"Position mismatch detected: {mismatch}",
                    )
                logger.critical("reconciliation_failed", extra={"mismatches": result.mismatches})
        except Exception as exc:
            logger.warning("reconciliation_error", extra={"error": str(exc)})

    # ── DTE-aware profit target ──────────────────────────────────────────────

    def _dyn_profit_target(self, dte: int) -> Decimal:
        """
        Return the profit-target fraction (of entry credit) that applies for
        the given DTE.  Tighter targets near expiry let theta decay work faster
        while still banking gains before gamma risk spikes.

        DTE > 20 : 50%  (default — plenty of time, wait for big capture)
        10-20    : 40%  (start locking in gains)
        5-10     : 30%  (theta is high, lock quicker)
        ≤ 5      : 20%  (gamma is dangerous, take any profit)
        """
        if dte > 20:
            return self._cfg.profit_target_pct          # configured value (default 50%)
        elif dte > 10:
            return Decimal("0.40")
        elif dte > 5:
            return Decimal("0.30")
        else:
            return Decimal("0.20")

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _target_expiry(self, underlying: str) -> Optional[date]:
        from stock_picker import get_expiries_for_underlying
        today = date.today()
        candidates = [
            e for e in get_expiries_for_underlying(self._cache, underlying)
            if self._cfg.min_dte <= (e - today).days <= self._cfg.max_dte
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda e: abs((e - today).days - self._cfg.preferred_dte))

    # NFO underlying name → Zerodha NSE quote symbol
    _INDEX_SYMBOLS: dict[str, str] = {
        "NIFTY":      "NSE:NIFTY 50",
        "BANKNIFTY":  "NSE:NIFTY BANK",
        "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
        "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
        "SENSEX":     "BSE:SENSEX",
    }

    def _live_spot(self, underlying: str) -> Optional[Decimal]:
        quote_sym = self._INDEX_SYMBOLS.get(underlying.upper(), f"NSE:{underlying}")
        try:
            raw = self._broker._kite.quote([quote_sym])
            v = raw.get(quote_sym, {}).get("last_price")
            if v:
                return Decimal(str(v))
            # fallback: try LTP endpoint
            ltp_raw = self._broker._kite.ltp([quote_sym])
            ltp_v = ltp_raw.get(quote_sym, {}).get("last_price")
            return Decimal(str(ltp_v)) if ltp_v else None
        except Exception as exc:
            logger.warning("spot_fetch_failed", extra={"underlying": underlying, "symbol": quote_sym, "error": str(exc)})
            return None

    def _open_position_count(self) -> int:
        try:
            from sqlalchemy import text
            from storage.database import create_db_engine
            engine = create_db_engine(self._db)
            with engine.connect() as conn:
                result = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM strategies "
                        "WHERE state IN ('ENTRY_COMPLETE','MONITORING') "
                        f"AND trading_mode='{self._cfg.mode.upper()}'"
                    )
                )
                return result.scalar() or 0
        except Exception:
            return 0

    # ── Entry scan ───────────────────────────────────────────────────────────

    def _compute_iv_for_underlying(self, ul: str, spot: float, expiry: "date") -> float:
        """
        Attempt to compute actual IV from ATM option prices.
        Falls back to configured iv_assumption on any failure.
        """
        try:
            from monitoring.iv_rank import fetch_and_store_atm_iv
            iv = fetch_and_store_atm_iv(
                underlying=ul, spot=spot, broker=self._broker,
                cache_path=self._cache, db_path=self._db, expiry=expiry,
            )
            if iv is not None:
                return iv
        except Exception as exc:
            logger.debug("iv_compute_fallback", extra={"underlying": ul, "error": str(exc)})
        # fallback: store configured assumption so history builds up
        from monitoring.iv_rank import store_iv_reading, IVReading
        store_iv_reading(self._db, IVReading(
            date=date.today(), underlying=ul,
            iv=float(self._cfg.iv_assumption), spot=spot, source="configured",
        ))
        return float(self._cfg.iv_assumption)

    def scan_underlyings(self) -> list[dict]:
        """
        Scan all configured underlyings × strategy types and return candidates
        sorted by composite opportunity score (IV rank + market + timing + credit).
        IC, Bull Put Spread, and Bear Call Spread all compete in one ranked list.
        """
        from stock_picker import (
            auto_select_ic_strikes,
            auto_select_vertical_spread,
            auto_select_calendar_spread,
            auto_select_bwb,
            get_strikes_for_underlying_expiry,
            get_expiries_for_underlying,
            get_lot_size_for_underlying,
        )
        from monitoring.iv_rank import compute_iv_rank
        from monitoring.market_filter import compute_market_score
        from monitoring.entry_timing import score_entry_time, compute_opportunity_score
        from monitoring.strategy_selector import rank_strategy_types
        from execution.models import StrategyType

        timing = score_entry_time()
        results = []

        for ul in self._cfg.underlyings:
            expiry = self._target_expiry(ul)
            if not expiry:
                logger.info("no_valid_expiry", extra={"underlying": ul})
                continue

            spot = self._live_spot(ul)
            if not spot:
                logger.warning("no_spot", extra={"underlying": ul})
                continue

            spot_f = float(spot)
            dte    = (expiry - date.today()).days

            # ── Step 1: compute actual IV and IV rank ──────────────────────
            actual_iv = self._compute_iv_for_underlying(ul, spot_f, expiry)
            iv_result = compute_iv_rank(
                underlying=ul, current_iv=actual_iv,
                db_path=self._db,
                min_iv_rank_to_enter=self._cfg.iv_rank_min,
            )

            # ── Step 2: market conditions (VIX + events) ──────────────────
            market = compute_market_score(
                underlying=ul, expiry=expiry,
                broker=self._broker if self._cfg.vix_enabled else None,
                vix_cfg=self._cfg.vix_cfg if self._cfg.vix_enabled else {},
                event_cfg={"enabled": self._cfg.event_enabled,
                           "days_before": self._cfg.event_blackout},
            )

            # ── Step 3: IV rank gate ───────────────────────────────────────
            if self._cfg.iv_rank_enabled and iv_result.recommendation == "SKIP":
                logger.info("iv_rank_skip", extra={
                    "underlying": ul,
                    "iv_rank": iv_result.iv_rank,
                    "threshold": self._cfg.iv_rank_min,
                })
                self._notifier.send_blocked(
                    ul,
                    f"IV rank {iv_result.iv_rank:.0f} < threshold {self._cfg.iv_rank_min:.0f}",
                    0.0,
                )
                continue

            # ── Step 4: event blackout gate ────────────────────────────────
            if market.event_check and market.event_check.skip:
                evt = market.event_check.events[0]
                logger.info("event_blackout_skip", extra={
                    "underlying": ul, "event": evt.name,
                    "days_to": market.event_check.days_to_nearest,
                })
                self._notifier.send_blocked(
                    ul,
                    f"Event blackout: {evt.name} in {market.event_check.days_to_nearest} day(s)",
                    0.0,
                )
                continue

            # ── Step 5: timing gate ────────────────────────────────────────
            if self._cfg.respect_timing_windows and timing.score < self._cfg.min_timing_score:
                logger.info("timing_skip", extra={
                    "underlying": ul,
                    "timing_score": timing.score,
                    "window": timing.window_label,
                })
                # Don't block all underlyings — just warn; we still scan but score low
                # (AutoTrader may still enter if no better opportunity exists later)

            # ── Step 6: strike selection across all viable strategies ──────
            pe_strikes  = get_strikes_for_underlying_expiry(self._cache, ul, expiry, "PE")
            ce_strikes  = get_strikes_for_underlying_expiry(self._cache, ul, expiry, "CE")
            all_strikes = sorted(set(pe_strikes + ce_strikes))
            lot_size    = get_lot_size_for_underlying(self._cache, ul) or 1
            iv_decimal  = Decimal(str(round(actual_iv, 4)))
            vix_val     = market.vix_reading.value if market.vix_reading else None

            viable_strategies = rank_strategy_types(
                iv_rank=iv_result.iv_rank,
                vix=vix_val,
                enabled=self._cfg.enabled_strategies,
            )
            if not viable_strategies:
                logger.info("no_viable_strategy", extra={
                    "underlying": ul, "iv_rank": iv_result.iv_rank, "vix": vix_val,
                })
                continue

            found_any_strategy = False
            for strat_type in viable_strategies:
                if strat_type == StrategyType.IRON_CONDOR:
                    winner, sel_log = auto_select_ic_strikes(
                        underlying=ul, expiry=expiry, spot=spot, iv=iv_decimal,
                        available_strikes=all_strikes, lot_size=lot_size, lots=1,
                        min_credit=self._risk.min_net_credit,
                        broker=self._broker, exchange=self._cfg.exchange,
                        short_mult_start=self._cfg.short_mult_start,
                        short_mult_min=self._cfg.short_mult_min,
                        min_ror_pct=self._risk.min_ror_pct,
                    )
                    if not winner:
                        logger.info("no_winner", extra={"underlying": ul, "strategy": strat_type.value})
                        continue
                    win_row = next((r for r in sel_log if r["status"].startswith("✅")), None)
                    if not win_row:
                        continue
                    credit = Decimal(str(win_row["credit"]))
                    spread_width = min(
                        winner.sell_put_strike - winner.buy_put_strike,
                        winner.buy_call_strike - winner.sell_call_strike,
                    )

                elif strat_type == StrategyType.CALENDAR_SPREAD:
                    # Need two expiries for calendar spread
                    all_expiries = get_expiries_for_underlying(self._cache, ul)
                    cal_expiries = sorted([e for e in all_expiries if (e - date.today()).days >= 7])
                    if len(cal_expiries) < 2:
                        logger.info("no_winner", extra={"underlying": ul, "strategy": strat_type.value, "reason": "not enough expiries"})
                        continue
                    near_exp = cal_expiries[0]
                    far_exp  = cal_expiries[1]
                    # Try CE and PE, pick whichever gives better credit
                    best_cal_winner = None
                    best_cal_log    = []
                    for opt_t in ("CE", "PE"):
                        opt_strikes = sorted(set(
                            get_strikes_for_underlying_expiry(self._cache, ul, near_exp, opt_t) +
                            get_strikes_for_underlying_expiry(self._cache, ul, far_exp, opt_t)
                        ))
                        w, l = auto_select_calendar_spread(
                            underlying=ul, near_expiry=near_exp, far_expiry=far_exp,
                            spot=spot, available_strikes=opt_strikes, lot_size=lot_size, lots=1,
                            option_type=opt_t, min_credit=self._risk.min_net_credit,
                            broker=self._broker, exchange=self._cfg.exchange,
                            min_ror_pct=Decimal("30.0"),
                        )
                        if w and (best_cal_winner is None or (w.net_credit or Decimal("0")) > (best_cal_winner.net_credit or Decimal("0"))):
                            best_cal_winner = w
                            best_cal_log    = l
                    winner, sel_log = best_cal_winner, best_cal_log
                    if not winner:
                        logger.info("no_winner", extra={"underlying": ul, "strategy": strat_type.value})
                        continue
                    win_row = next((r for r in sel_log if r["status"].startswith("✅")), None)
                    if not win_row:
                        continue
                    credit = Decimal(str(win_row["credit"]))
                    # For calendar, max loss ≈ far_ask (cost of far leg)
                    spread_width = winner.far_ask if winner.far_ask else Decimal("50")

                elif strat_type == StrategyType.BROKEN_WING_BUTTERFLY:
                    winner, sel_log = auto_select_bwb(
                        underlying=ul, expiry=expiry, spot=spot, iv=iv_decimal,
                        available_strikes=all_strikes, lot_size=lot_size, lots=1,
                        min_credit=self._risk.min_net_credit,
                        broker=self._broker, exchange=self._cfg.exchange,
                        center_mult_start=self._cfg.short_mult_start,
                        center_mult_min=self._cfg.short_mult_min,
                        min_ror_pct=self._risk.min_ror_pct,
                    )
                    if not winner:
                        logger.info("no_winner", extra={"underlying": ul, "strategy": strat_type.value})
                        continue
                    win_row = next((r for r in sel_log if r["status"].startswith("✅")), None)
                    if not win_row:
                        continue
                    credit = Decimal(str(win_row["credit"]))
                    spread_width = winner.lower_wing_width if winner.lower_wing_width else Decimal("100")

                else:  # BULL_PUT_SPREAD or BEAR_CALL_SPREAD
                    opt_type = "PE" if strat_type == StrategyType.BULL_PUT_SPREAD else "CE"
                    winner, sel_log = auto_select_vertical_spread(
                        underlying=ul, expiry=expiry, spot=spot, iv=iv_decimal,
                        available_strikes=all_strikes, lot_size=lot_size, lots=1,
                        option_type=opt_type,
                        min_credit=self._risk.min_net_credit,
                        broker=self._broker, exchange=self._cfg.exchange,
                        short_mult_start=self._cfg.short_mult_start,
                        short_mult_min=self._cfg.short_mult_min,
                        min_ror_pct=self._risk.min_ror_pct,
                    )
                    if not winner:
                        logger.info("no_winner", extra={"underlying": ul, "strategy": strat_type.value})
                        continue
                    win_row = next((r for r in sel_log if r["status"].startswith("✅")), None)
                    if not win_row:
                        continue
                    credit = Decimal(str(win_row["credit"]))
                    spread_width = abs(winner.sell_strike - winner.buy_strike)

                max_loss_per_lot = (spread_width - credit) * lot_size
                ror = (credit * lot_size / max_loss_per_lot * 100) if max_loss_per_lot > 0 else Decimal("0")

                # ── Step 7: composite opportunity score ────────────────────
                opp = compute_opportunity_score(
                    timing_score  = timing.score,
                    iv_rank       = iv_result.iv_rank,
                    market_score  = market.composite,
                    net_credit    = float(credit),
                    target_credit = float(self._risk.target_net_credit),
                    tradeable     = market.tradeable,
                    min_iv_rank   = self._cfg.iv_rank_min,
                )

                sig = self._notifier.send_opportunity(
                    underlying      = ul,
                    composite_score = opp.composite,
                    grade           = opp.grade,
                    action          = opp.action,
                    iv_rank         = iv_result.iv_rank,
                    vix             = vix_val,
                    timing_window   = timing.window_label,
                    timing_score    = timing.score,
                    credit          = float(credit),
                    ror             = float(ror),
                    reason          = opp.reason,
                    strategy_type   = strat_type.value,
                )

                extra_log: dict = {
                    "underlying": ul, "strategy": strat_type.value,
                    "credit": str(credit), "ror": str(ror), "dte": dte,
                    "iv_rank": iv_result.iv_rank,
                    "opp_score": opp.composite, "grade": opp.grade,
                    "vix": vix_val, "timing_window": timing.window_label,
                }
                if strat_type == StrategyType.IRON_CONDOR:
                    extra_log["sell_pe"] = str(winner.sell_put_strike)
                    extra_log["sell_ce"] = str(winner.sell_call_strike)
                else:
                    extra_log["sell"] = str(winner.sell_strike)
                    extra_log["buy"]  = str(winner.buy_strike)
                logger.info("candidate_scored", extra=extra_log)

                results.append({
                    "underlying":       ul,
                    "expiry":           expiry,
                    "dte":              dte,
                    "spot":             spot,
                    "lot_size":         lot_size,
                    "strategy_type":    strat_type,
                    "candidate":        winner,
                    "resolved":         win_row["resolved"],
                    "quotes":           win_row["quotes"],
                    "credit":           credit,
                    "max_loss":         max_loss_per_lot,
                    "ror":              ror,
                    "log":              sel_log,
                    "signal_id":        sig.signal_id,
                    # Intelligence data
                    "iv":               actual_iv,
                    "iv_rank":          iv_result.iv_rank,
                    "iv_percentile":    iv_result.iv_percentile,
                    "iv_confidence":    iv_result.confidence,
                    "iv_52w_high":      iv_result.iv_52w_high,
                    "iv_52w_low":       iv_result.iv_52w_low,
                    "vix":              vix_val,
                    "market_score":     market.composite,
                    "market_tradeable": market.tradeable,
                    "market_summary":   market.summary,
                    "timing_score":     timing.score,
                    "timing_window":    timing.window_label,
                    "opp_score":        opp.composite,
                    "opp_grade":        opp.grade,
                    "opp_action":       opp.action,
                    "opp_reason":       opp.reason,
                })
                found_any_strategy = True

            if not found_any_strategy:
                logger.info("no_winner_any_strategy", extra={"underlying": ul})

        # Sort by composite opportunity score (best first), not just RoR
        results.sort(key=lambda r: r["opp_score"], reverse=True)
        return results

    def enter_best(self, candidates: list[dict]) -> Optional[str]:
        """Enter the top-ranked candidate. Returns run_id or None."""
        if not candidates:
            return None

        best        = candidates[0]
        ul          = best["underlying"]
        winner      = best["candidate"]
        strat_type  = best.get("strategy_type")

        from execution.entry_engine import EntryEngine
        from execution.models import (
            LegConfig, OptionType, Side, StrategyConfig, StrategyType,
        )
        from execution.state_machine import StateMachine
        from risk.kill_switch import KillSwitch

        if strat_type == StrategyType.BULL_PUT_SPREAD:
            # Leg index 0 = BUY lower put, index 1 = SELL higher put
            # (matches resolved_map indices from auto_select_vertical_spread)
            legs_cfg = [
                LegConfig(side=Side.BUY,  option_type=OptionType.PE, strike=winner.buy_strike),
                LegConfig(side=Side.SELL, option_type=OptionType.PE, strike=winner.sell_strike),
            ]
            strategy_name = "auto_bps"
        elif strat_type == StrategyType.BEAR_CALL_SPREAD:
            # Leg index 0 = SELL lower call, index 1 = BUY higher call
            legs_cfg = [
                LegConfig(side=Side.SELL, option_type=OptionType.CE, strike=winner.sell_strike),
                LegConfig(side=Side.BUY,  option_type=OptionType.CE, strike=winner.buy_strike),
            ]
            strategy_name = "auto_bcs"
        elif strat_type == StrategyType.CALENDAR_SPREAD:
            # Leg index 0 = SELL near-expiry, index 1 = BUY far-expiry
            opt_type = OptionType.CE if winner.option_type == "CE" else OptionType.PE
            legs_cfg = [
                LegConfig(side=Side.SELL, option_type=opt_type, strike=winner.strike,
                          expiry=winner.near_expiry),
                LegConfig(side=Side.BUY,  option_type=opt_type, strike=winner.strike,
                          expiry=winner.far_expiry),
            ]
            strategy_name = "auto_cal"
        elif strat_type == StrategyType.BROKEN_WING_BUTTERFLY:
            # Leg indices: 0=BUY upper, 1=SELL mid, 2=SELL mid, 3=BUY lower
            opt_type = OptionType.PE if winner.option_type == "PE" else OptionType.CE
            legs_cfg = [
                LegConfig(side=Side.BUY,  option_type=opt_type, strike=winner.upper_strike),
                LegConfig(side=Side.SELL, option_type=opt_type, strike=winner.mid_strike),
                LegConfig(side=Side.SELL, option_type=opt_type, strike=winner.mid_strike),
                LegConfig(side=Side.BUY,  option_type=opt_type, strike=winner.lower_strike),
            ]
            strategy_name = "auto_bwb"
        else:
            # Iron Condor (default)
            legs_cfg = [
                LegConfig(side=Side.BUY,  option_type=OptionType.PE, strike=winner.buy_put_strike),
                LegConfig(side=Side.SELL, option_type=OptionType.PE, strike=winner.sell_put_strike),
                LegConfig(side=Side.SELL, option_type=OptionType.CE, strike=winner.sell_call_strike),
                LegConfig(side=Side.BUY,  option_type=OptionType.CE, strike=winner.buy_call_strike),
            ]
            strategy_name = "auto_ic"

        strategy = StrategyConfig(
            name=strategy_name,
            strategy_type=strat_type or StrategyType.IRON_CONDOR,
            underlying=ul,
            exchange=self._cfg.exchange,
            expiry=best["expiry"],
            lots=1,
            legs=legs_cfg,
        )

        # ── Pre-trade margin check (live mode only) ──────────────────────────
        if self._cfg.mode.lower() == "live":
            try:
                basket_orders = []
                for leg_i, inst in best["resolved"].items():
                    leg = legs_cfg[leg_i]
                    q = best["quotes"].get(inst.trading_symbol)
                    price = float(q.midpoint) if q else 0.0
                    basket_orders.append({
                        "exchange": "NFO",
                        "tradingsymbol": inst.trading_symbol,
                        "transaction_type": leg.side.value,
                        "variety": "regular",
                        "product": "MIS",
                        "order_type": "LIMIT",
                        "quantity": inst.lot_size * strategy.lots,
                        "price": price,
                    })
                # Available funds: live cash balance from Zerodha
                funds_raw = self._broker.get_margins()
                available = Decimal(str(
                    funds_raw.get("equity", {}).get("available", {}).get("cash", 0) or 0
                ))

                margin_result = self._broker.get_basket_margin(basket_orders)
                required = Decimal(str(margin_result.get("required", 0) or 0))

                logger.info("margin_check", extra={
                    "required": str(required), "available": str(available),
                })
                if required > available:
                    msg = (f"Margin required ₹{float(required):,.0f} exceeds "
                           f"available funds ₹{float(available):,.0f}. Skipping entry.")
                    logger.warning("margin_insufficient", extra={
                        "required": str(required), "available": str(available),
                    })
                    self._alert_manager.warning("margin_insufficient", msg)
                    return None
            except Exception as exc:
                logger.warning("basket_margin_check_failed", extra={"error": str(exc)})
                # Proceed if margin API is unavailable — don't block entry on API errors

        # ── Manual confirmation gate ─────────────────────────────────────────
        if self._cfg.require_confirmation:
            self._print_trade_summary(best, legs_cfg)
            if not self._await_confirmation(timeout=60):
                logger.warning("trade_rejected_by_user", extra={"underlying": ul})
                return None

        # ── Mode-based order manager ─────────────────────────────────────────
        ks = KillSwitch()
        sm = StateMachine()
        run_id = str(uuid.uuid4())

        if self._cfg.mode.lower() == "live":
            from execution.live_order_manager import LiveOrderManager
            om          = LiveOrderManager(self._broker, self._risk, ks)
            exec_broker = self._broker
        else:
            from brokers.paper import PaperBroker, PaperFillMode
            from execution.order_manager import OrderManager
            paper = PaperBroker(
                live_quote_source=self._broker,
                fill_mode=PaperFillMode.TOUCH,
                fill_delay_seconds=0,
                slippage_per_order=self._paper_cfg.slippage_per_order,
            )
            om          = OrderManager(paper, self._risk, ks)
            exec_broker = paper

        entry = EntryEngine(
            strategy=strategy,
            resolved=best["resolved"],
            broker=exec_broker,
            risk=self._risk,
            kill_switch=ks,
            alert_manager=self._alert_manager,
            state_machine=sm,
            order_manager=om,
            strategy_run_id=run_id,
        )
        tracker = entry.run()

        if tracker.all_filled:
            sides  = {i: l.side for i, l in enumerate(legs_cfg)}
            credit = tracker.actual_net_credit(sides)
            self._db_save_entry(run_id, strategy, best["lot_size"], credit, sm, tracker, best["resolved"])
            from monitoring.notifier import mark_signal_acted_on
            if best.get("signal_id"):
                mark_signal_acted_on(best["signal_id"])
            logger.info("auto_entry_complete", extra={
                "run_id": run_id[:16], "underlying": ul,
                "strategy": strategy_name,
                "credit": str(credit), "state": sm.state.value,
                "opp_score": best.get("opp_score", 0),
                "grade": best.get("opp_grade", "?"),
            })
            return run_id

        logger.warning("auto_entry_failed", extra={"state": sm.state.value})
        return None

    def _db_save_entry(self, run_id, strategy, lot_size, credit, sm, tracker=None, resolved=None) -> None:
        try:
            from datetime import datetime as _dt
            from sqlalchemy import text
            from storage.database import create_db_engine
            from storage.repositories import StrategyRepository
            engine = create_db_engine(self._db)
            with engine.connect() as conn:
                sr = StrategyRepository(conn)
                sr.create(run_id, strategy.name, strategy.underlying,
                          strategy.exchange, str(strategy.expiry), strategy.lots,
                          self._cfg.mode.upper())
                if credit:
                    sr.update_pnl(run_id, entry_credit=credit * lot_size)
                sr.update_state(run_id, sm.state)

                # Save per-leg position records so exit monitor can find them
                if tracker and resolved:
                    now = _dt.now(IST)
                    for leg_i, fill in tracker._fills.items():
                        order = tracker._orders[leg_i]
                        inst  = resolved[leg_i]
                        conn.execute(
                            text(
                                "INSERT INTO positions "
                                "(strategy_id, leg_index, trading_symbol, instrument_token, "
                                "side, quantity, average_entry_price, updated_at) "
                                "VALUES (:sid, :li, :sym, :tok, :side, :qty, :price, :ts)"
                            ),
                            {
                                "sid":   run_id,
                                "li":    leg_i,
                                "sym":   inst.trading_symbol,
                                "tok":   inst.instrument_token,
                                "side":  order.side.value,
                                "qty":   order.quantity,
                                "price": float(fill.price),
                                "ts":    now,
                            },
                        )
                    conn.commit()
        except Exception as exc:
            logger.warning("db_save_entry_failed", extra={"error": str(exc)})

    # ── Exit monitoring ──────────────────────────────────────────────────────

    def _get_open_positions(self) -> list[dict]:
        """Read all open positions (ENTRY_COMPLETE or MONITORING) from DB."""
        try:
            import pandas as pd
            from sqlalchemy import text
            from storage.database import create_db_engine

            engine = create_db_engine(self._db)
            with engine.connect() as conn:
                df = pd.read_sql(
                    text(
                        "SELECT s.id AS strategy_id, s.name AS strategy_name, s.underlying, s.expiry, "
                        "s.entry_credit, s.lots, "
                        "p.leg_index, p.trading_symbol, p.instrument_token, "
                        "p.side, p.quantity, p.average_entry_price "
                        "FROM strategies s "
                        "JOIN positions p ON p.strategy_id = s.id "
                        "WHERE s.state IN ('ENTRY_COMPLETE','MONITORING') "
                        f"AND s.trading_mode = '{self._cfg.mode.upper()}' "
                        "ORDER BY s.id, p.leg_index"
                    ),
                    conn,
                )
            if df.empty:
                return []

            grouped: list[dict] = []
            for sid, grp in df.groupby("strategy_id"):
                grouped.append({
                    "strategy_id":   str(sid),
                    "strategy_name": grp.iloc[0]["strategy_name"],
                    "underlying":    grp.iloc[0]["underlying"],
                    "expiry":        grp.iloc[0]["expiry"],
                    "entry_credit":  float(grp.iloc[0]["entry_credit"] or 0),
                    "legs":          grp.to_dict("records"),
                })
            return grouped
        except Exception as exc:
            logger.warning("open_positions_error", extra={"error": str(exc)})
            return []

    def check_and_exit(self, position: dict) -> Optional[str]:
        """
        Evaluate exit conditions for one open position.
        Returns trigger name if an exit was executed, else None.
        """
        from execution.models import ExitTrigger, LegPosition, OptionType, Side
        from risk.payoff import calculate_cost_to_close, calculate_mtm_pnl
        from execution.quote_engine import QuoteEngine

        raw_expiry = position["expiry"]
        expiry = (
            date.fromisoformat(str(raw_expiry)[:10])
            if isinstance(raw_expiry, str)
            else raw_expiry
        )
        dte = (expiry - date.today()).days

        instruments = [
            (int(row["instrument_token"]), str(row["trading_symbol"]))
            for row in position["legs"]
        ]
        try:
            quotes = QuoteEngine(self._broker).fetch_all(instruments)
        except Exception as exc:
            logger.warning("monitor_quote_fail", extra={"error": str(exc)})
            return None

        # Build LegPosition list
        leg_positions: list[LegPosition] = []
        for row in position["legs"]:
            sym = str(row["trading_symbol"])
            q   = quotes.get(sym)
            if q is None:
                return None
            opt = OptionType.CE if sym.endswith("CE") else OptionType.PE
            leg_positions.append(LegPosition(
                leg_index=int(row["leg_index"]),
                trading_symbol=sym,
                instrument_token=int(row["instrument_token"]),
                side=Side(str(row["side"])),
                option_type=opt,
                strike=Decimal("0"),
                lot_size=1, lots=1,
                quantity=int(row["quantity"]),
                average_entry_price=Decimal(str(row["average_entry_price"])),
                current_bid=q.bid,
                current_ask=q.ask,
                current_ltp=q.ltp,
            ))

        entry_credit = Decimal(str(position["entry_credit"]))
        cost_to_close = calculate_cost_to_close(leg_positions)
        mtm_pnl = calculate_mtm_pnl(entry_credit, cost_to_close)
        pct_captured = (mtm_pnl / entry_credit * 100) if entry_credit > 0 else Decimal("0")

        # Update high-water mark for trailing stop
        sid = position["strategy_id"]
        if sid not in self._position_hwm:
            self._position_hwm[sid] = Decimal("0")
        if pct_captured > self._position_hwm[sid]:
            self._position_hwm[sid] = pct_captured
        hwm = self._position_hwm[sid]

        # DTE-aware profit target (tighter near expiry)
        dyn_target = self._dyn_profit_target(dte)

        logger.info("position_monitor_tick", extra={
            "strategy_id": position["strategy_id"][:16],
            "underlying":  position["underlying"],
            "dte":         dte,
            "mtm_pnl":     str(mtm_pnl),
            "pct_captured": str(pct_captured),
            "hwm_pct":     str(hwm),
            "dyn_target":  str(dyn_target),
        })

        # ── Trailing stop: once we've locked ≥25% profit, protect breakeven ──
        # If HWM ever reached 25%, we never let P&L drop below 0.
        trailing_lock_pct = Decimal("25")
        trailing_stop_triggered = (
            hwm >= trailing_lock_pct and mtm_pnl < Decimal("0")
        )

        # Determine trigger
        trigger: Optional[ExitTrigger] = None
        if dte <= self._cfg.dte_exit:
            trigger = ExitTrigger.DTE_EXIT
        elif mtm_pnl >= entry_credit * dyn_target:
            trigger = ExitTrigger.PROFIT_TARGET
        elif trailing_stop_triggered:
            # Treat as stop loss (breakeven protection)
            trigger = ExitTrigger.STOP_LOSS
            logger.info("trailing_stop_fired", extra={
                "strategy_id": sid[:16],
                "hwm_pct": str(hwm),
                "current_pct": str(pct_captured),
            })
        elif mtm_pnl <= -(entry_credit * self._cfg.stop_loss_mult):
            trigger = ExitTrigger.STOP_LOSS

        if trigger:
            logger.info("exit_trigger_fired", extra={
                "strategy_id": position["strategy_id"][:16],
                "trigger": trigger.value,
                "mtm_pnl": str(mtm_pnl),
                "dte": dte,
            })
            self._execute_exit(position, leg_positions, expiry, trigger)
            # Clean up HWM tracking for this position
            self._position_hwm.pop(sid, None)
            return trigger.value

        return None

    def _execute_exit(self, position, leg_positions, expiry, trigger) -> None:
        from execution.exit_engine import ExitEngine
        from execution.models import (
            LegConfig, OptionType, Side, StrategyConfig, StrategyState,
        )
        from execution.state_machine import StateMachine
        from risk.kill_switch import KillSwitch

        legs_cfg = [
            LegConfig(
                side=lp.side,
                option_type=OptionType.CE if lp.trading_symbol.endswith("CE") else OptionType.PE,
                strike=lp.strike,
            )
            for lp in leg_positions
        ]
        strategy = StrategyConfig(
            name=position.get("strategy_name", "auto_ic"),
            underlying=position["underlying"],
            exchange=self._cfg.exchange,
            expiry=expiry,
            lots=1,
            legs=legs_cfg,
        )

        ks = KillSwitch()
        sm = StateMachine(initial=StrategyState.MONITORING)

        if self._cfg.mode.lower() == "live":
            from execution.live_order_manager import LiveOrderManager
            om          = LiveOrderManager(self._broker, self._risk, ks)
            exec_broker = self._broker
        else:
            from brokers.paper import PaperBroker, PaperFillMode
            from execution.order_manager import OrderManager
            paper = PaperBroker(
                live_quote_source=self._broker,
                fill_delay_seconds=0,
                fill_mode=PaperFillMode.TOUCH,
                slippage_per_order=self._paper_cfg.slippage_per_order,
            )
            om          = OrderManager(paper, self._risk, ks)
            exec_broker = paper

        exit_engine = ExitEngine(
            strategy=strategy,
            positions=leg_positions,
            broker=exec_broker,
            risk=self._risk,
            kill_switch=ks,
            alert_manager=self._alert_manager,
            state_machine=sm,
            exit_trigger=trigger,
            order_manager=om,
            strategy_run_id=position["strategy_id"],
        )
        tracker = exit_engine.run()

        if tracker.all_filled:
            sides    = {lp.leg_index: lp.side for lp in leg_positions}
            exit_deb = tracker.actual_net_credit(sides) or Decimal("0")
            # For a closing trade: entry_credit (received) - cost_to_close (paid) = P&L
            pnl = Decimal(str(position["entry_credit"])) + exit_deb
            self._db_save_exit(position["strategy_id"], pnl, sm)
            self._notifier.send_exit(
                underlying=position["underlying"],
                trigger=trigger.value,
                pnl=float(pnl),
                strategy_name=position.get("strategy_name", ""),
            )
            logger.info("auto_exit_complete", extra={
                "strategy_id": position["strategy_id"][:16],
                "pnl": str(pnl),
                "trigger": trigger.value,
            })
            self._learner.record_outcome(
                pnl=float(pnl),
                underlying=position["underlying"],
                strategy=position.get("strategy_name", ""),
            )

    def _db_save_exit(self, strategy_id, pnl, sm) -> None:
        try:
            from storage.database import create_db_engine
            from storage.repositories import StrategyRepository
            engine = create_db_engine(self._db)
            with engine.connect() as conn:
                sr = StrategyRepository(conn)
                sr.update_state(strategy_id, sm.state)
                sr.update_pnl(strategy_id, final_pnl=pnl)
        except Exception as exc:
            logger.warning("db_save_exit_failed", extra={"error": str(exc)})

    # ── Monthly P&L tracker ──────────────────────────────────────────────────

    def monthly_summary(self) -> dict:
        """Return P&L stats for the current calendar month."""
        try:
            import pandas as pd
            from sqlalchemy import text
            from storage.database import create_db_engine

            first_of_month = date.today().replace(day=1).isoformat()
            engine = create_db_engine(self._db)
            with engine.connect() as conn:
                df = pd.read_sql(
                    text(
                        "SELECT final_pnl, entry_credit FROM strategies "
                        f"WHERE trading_mode='{self._cfg.mode.upper()}' "
                        "AND state IN ('EXIT_COMPLETE','ABORTED') "
                        "AND created_at >= :start"
                    ),
                    conn, params={"start": first_of_month},
                )
            trades   = len(df)
            total    = float(df["final_pnl"].sum()) if trades else 0.0
            wins     = int((df["final_pnl"] > 0).sum()) if trades else 0
            capital  = float(self._risk.capital_limit)
            ret_pct  = total / capital * 100 if capital else 0.0
            target   = float(self._risk.capital_limit * self._cfg.monthly_target_pct / 100)
            return {
                "trades":    trades,
                "wins":      wins,
                "total_pnl": total,
                "return_pct": ret_pct,
                "target_pnl": target,
                "target_pct": float(self._cfg.monthly_target_pct),
                "on_track":  ret_pct >= float(self._cfg.monthly_target_pct) * 0.5,
            }
        except Exception as exc:
            logger.warning("monthly_summary_error", extra={"error": str(exc)})
            return {}

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Main automation loop. Blocks until KeyboardInterrupt.
        Responsibilities:
          - Once per day at entry_time: scan + enter
          - Every monitor_interval seconds: check exits
        """
        logger.info("auto_trader_started", extra={
            "underlyings":  self._cfg.underlyings,
            "entry_time":   self._cfg.entry_time,
            "profit_target": str(self._cfg.profit_target_pct),
            "stop_loss_mult": str(self._cfg.stop_loss_mult),
        })
        print(f"\n{'='*60}")
        print(f"  AutoTrader started — MODE: {self._cfg.mode.upper()}")
        print(f"  Underlyings : {', '.join(self._cfg.underlyings)}")
        print(f"  Entry time  : {self._cfg.entry_time} IST")
        print(f"  Profit exit : {float(self._cfg.profit_target_pct)*100:.0f}% of credit")
        print(f"  Stop loss   : {float(self._cfg.stop_loss_mult):.1f}× credit")
        print(f"  Monthly goal: {float(self._cfg.monthly_target_pct):.0f}%")
        print(f"{'='*60}\n")

        while True:
            try:
                now          = datetime.now(IST)
                today        = now.date()
                current_hhmm = now.strftime("%H:%M")
                is_market    = "09:15" <= current_hhmm <= "15:30"

                # Reset daily flags at midnight
                if self._last_entry_date != today:
                    self._entered_today        = False
                    self._last_entry_date      = today
                    self._daily_loss_halted    = False
                    self._losing_trades_today  = self._count_losing_trades_today()

                # ── Periodic reconciliation (once per hour during market) ─
                if is_market and (time.time() - self._last_reconcile_ts) >= 3600:
                    self._last_reconcile_ts = time.time()
                    self._reconcile()

                # ── Daily circuit breakers ───────────────────────────────
                daily_pnl = self._daily_pnl()
                self._losing_trades_today = self._count_losing_trades_today()

                if daily_pnl <= -self._risk.max_daily_loss:
                    if not self._daily_loss_halted:
                        msg = (
                            f"Daily loss limit hit: ₹{float(daily_pnl):,.0f} ≥ "
                            f"-₹{float(self._risk.max_daily_loss):,.0f}. "
                            "No new entries for the rest of the day."
                        )
                        print(f"[{current_hhmm}] HALT: {msg}")
                        self._alert_manager.critical("daily_loss_halt", msg)
                        logger.critical("daily_loss_halt", extra={
                            "daily_pnl": str(daily_pnl),
                            "max_daily_loss": str(self._risk.max_daily_loss),
                        })
                        self._daily_loss_halted = True
                elif self._losing_trades_today >= self._cfg.max_losing_trades:
                    if not self._daily_loss_halted:
                        msg = (
                            f"{self._losing_trades_today} losing trades today "
                            f"(limit {self._cfg.max_losing_trades}). "
                            "No new entries for the rest of the day."
                        )
                        print(f"[{current_hhmm}] HALT: {msg}")
                        self._alert_manager.critical("losing_trade_halt", msg)
                        logger.critical("losing_trade_halt", extra={
                            "losing_trades": self._losing_trades_today,
                            "limit": self._cfg.max_losing_trades,
                        })
                        self._daily_loss_halted = True
                else:
                    self._daily_loss_halted = False

                # ── Entry scan ───────────────────────────────────────────
                _open = self._open_position_count()
                if is_market and current_hhmm >= self._cfg.entry_time and current_hhmm <= "14:30":
                    if _open >= self._cfg.max_concurrent:
                        print(f"[{current_hhmm}] Position already open ({_open}/{self._cfg.max_concurrent}) — no new entry until it is closed.")
                    elif self._entered_today:
                        print(f"[{current_hhmm}] Already entered today — skipping scan.")
                    elif self._daily_loss_halted:
                        print(f"[{current_hhmm}] Trading halted for today — daily loss or losing-trade limit reached.")

                if (
                    is_market
                    and not self._entered_today
                    and not self._daily_loss_halted
                    and current_hhmm >= self._cfg.entry_time
                    and current_hhmm <= "14:30"
                    and _open < self._cfg.max_concurrent
                ):
                    print(f"[{current_hhmm}] Running entry scan...  [{self._learner.summary()}]")
                    self._learner.apply_to_config(self._cfg, self._risk)
                    candidates = self.scan_underlyings()

                    if candidates:
                        best = candidates[0]
                        print(
                            f"[{current_hhmm}] Best: {best['underlying']} "
                            f"Score={best.get('opp_score', '?'):.0f} [{best.get('opp_grade','?')}]  "
                            f"credit={float(best['credit']):.2f}pts  "
                            f"RoR={float(best['ror']):.1f}%  "
                            f"IVrank={best.get('iv_rank','?'):.0f}  "
                            f"VIX={best.get('vix','?')}  "
                            f"DTE={best['dte']}  "
                            f"Window={best.get('timing_window','?')}"
                        )
                        run_id = self.enter_best(candidates)
                        if run_id:
                            print(f"[{current_hhmm}] ✅ Entry complete — {run_id[:16]}")
                            self._entered_today = True
                        else:
                            print(f"[{current_hhmm}] ⚠️  Entry failed. Will retry.")
                    else:
                        print(f"[{current_hhmm}] No valid candidates. Will retry.")

                # ── Exit monitor ─────────────────────────────────────────
                if is_market:
                    elapsed = time.time() - self._last_monitor_ts
                    if elapsed >= self._cfg.monitor_interval:
                        self._last_monitor_ts = time.time()
                        positions = self._get_open_positions()
                        if positions:
                            print(f"[{current_hhmm}] Monitoring {len(positions)} open position(s)...")
                        for pos in positions:
                            trigger = self.check_and_exit(pos)
                            if trigger:
                                print(
                                    f"[{current_hhmm}] 🏁 EXIT {pos['underlying']} — {trigger}"
                                )

                time.sleep(60)

            except KeyboardInterrupt:
                print("\n[AutoTrader] Stopped.")
                summary = self.monthly_summary()
                if summary:
                    print(f"\nMonth-to-date: {summary['trades']} trades | "
                          f"P&L ₹{summary['total_pnl']:,.0f} | "
                          f"Return {summary['return_pct']:.1f}% "
                          f"(target {summary['target_pct']:.0f}%)")
                break
            except Exception as exc:
                logger.exception("auto_trader_loop_error")
                print(f"[AutoTrader] Error: {exc}. Resuming in 60s...")
                time.sleep(60)
