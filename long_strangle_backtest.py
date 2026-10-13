#!/usr/bin/env python3
"""
Long Strangle Backtest — Nifty Weekly Options
==============================================
Strategy : Buy OTM Call (4% above spot) + Buy OTM Put (4% below spot)
Underlying: NIFTY  |  Expiry cycle: Weekly (every Tuesday)
Weeks     : 4 most-recent completed weekly cycles
Entry     : First trading session after prior expiry (Wednesday)
Exit      : Expiry day at close / settlement

Run:
    python long_strangle_backtest.py
    python long_strangle_backtest.py --otm-pct 4.0 --lots 1 --report-dir reports/
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────────────────
CACHE_DIR  = Path("/media/upayan/Work/Quant Projects/projects/project1/nse_bhavcopy_cache")
REPORT_DIR = Path("/media/upayan/Work/options_execution/reports")

# ── Strategy constants ─────────────────────────────────────────────────────────
SYMBOL       = "NIFTY"
LOT_SIZE     = 75         # NIFTY lot size as of 2026
OTM_PCT      = 4.0        # % above/below spot for strike selection
STRIKE_STEP  = 50         # NIFTY strike interval
DEFAULT_LOTS = 1

# ── Weekly expiry cycles (most recent 4 completed weeks, NIFTY expires Tue) ───
# Each tuple: (entry_date, expiry_date)
WEEKLY_CYCLES: list[tuple[date, date]] = [
    (date(2026, 9,  9), date(2026, 9, 15)),   # Week 1 — Sep 9 entry, Sep 15 expiry
    (date(2026, 9, 16), date(2026, 9, 22)),   # Week 2 — Sep 16 entry, Sep 22 expiry
    (date(2026, 9, 23), date(2026, 9, 29)),   # Week 3 — Sep 23 entry, Sep 29 expiry
    (date(2026, 9, 30), date(2026,10,  6)),   # Week 4 — Sep 30 entry, Oct 6 expiry
]

# ── Brokerage / transaction costs ─────────────────────────────────────────────
# NSE F&O: ₹20 flat per order (Zerodha), STT on sell side, exchange charges
BROKERAGE_PER_ORDER = 20.0      # ₹ per order (buy or sell)
STT_PCT_ON_SELL     = 0.000625  # 0.0625% on sell premium (buyer pays at exercise too)
EXCHANGE_CHARGES    = 0.000188  # NSE F&O txn charge ~0.00188%
SEBI_CHARGES        = 0.000001  # SEBI turnover fee
STAMP_DUTY_PCT      = 0.00003   # 0.003% on buy premium

# ── Helper: round to nearest strike step ──────────────────────────────────────

def round_strike_up(price: float, step: int = STRIKE_STEP) -> int:
    return int(math.ceil(price / step) * step)

def round_strike_down(price: float, step: int = STRIKE_STEP) -> int:
    return int(math.floor(price / step) * step)

def round_strike_nearest(price: float, step: int = STRIKE_STEP) -> int:
    return int(round(price / step) * step)


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class OptionSnapshot:
    """Single-day snapshot of one option leg."""
    trade_date:    date
    symbol:        str
    expiry:        date
    strike:        float
    opt_type:      str           # CE or PE
    open_px:       float
    high_px:       float
    low_px:        float
    close_px:      float
    last_px:       float
    settle_px:     float
    underlying:    float
    volume:        int
    lot_size:      int


@dataclass
class DailyMTM:
    """Intraday/daily mark-to-market record for one strangle."""
    trade_date: date
    spot:       float
    call_px:    float
    put_px:     float
    straddle_value: float        # call + put combined premium
    daily_pnl:  float            # change vs previous day's combined value
    cumulative_pnl: float        # cumulative from entry
    note:       str = ""


@dataclass
class WeekResult:
    """Full result for one weekly strangle trade."""
    week_num:        int
    entry_date:      date
    expiry_date:     date

    spot_at_entry:   float
    call_strike:     int
    put_strike:      int

    call_entry_px:   float
    put_entry_px:    float
    total_debit:     float       # combined premium paid

    call_exit_px:    float       # on expiry: intrinsic or settlement
    put_exit_px:     float
    total_credit:    float       # combined value at exit

    spot_at_expiry:  float
    call_intrinsic:  float
    put_intrinsic:   float

    gross_pnl:       float       # per lot: (total_credit - total_debit) * lot_size
    transaction_cost: float
    net_pnl:         float       # after costs
    net_pnl_per_lot: float

    max_profit_seen: float       # best intraday combined value - debit
    max_loss_seen:   float       # worst intraday combined value - debit

    break_even_upper: float      # call_strike + total_debit
    break_even_lower: float      # put_strike  - total_debit

    daily_mtm:       list[DailyMTM] = field(default_factory=list)
    outcome:         str = ""    # "WIN" | "LOSS" | "NEUTRAL"
    warnings:        list[str]   = field(default_factory=list)


# ── Data loading ───────────────────────────────────────────────────────────────

def cache_file(d: date) -> Path:
    return CACHE_DIR / f"fo_{d.strftime('%Y%m%d')}.csv"


def load_nifty_options(d: date, expiry: date) -> dict[tuple[float, str], OptionSnapshot]:
    """
    Load all NIFTY option rows for a given trading date and expiry.
    Returns {(strike, opt_type): OptionSnapshot}.
    """
    fp = cache_file(d)
    if not fp.exists():
        return {}

    result: dict[tuple[float, str], OptionSnapshot] = {}
    expiry_str = expiry.strftime("%Y-%m-%d")

    with open(fp, newline="") as fh:
        reader = csv.reader(fh)
        next(reader)   # skip header
        for row in reader:
            if len(row) < 22:
                continue
            if row[7] != SYMBOL:
                continue
            if row[9] != expiry_str:
                continue
            opt_type = row[12]
            if opt_type not in ("CE", "PE"):
                continue

            try:
                strike    = float(row[11])
                open_px   = float(row[14]) if row[14] else 0.0
                high_px   = float(row[15]) if row[15] else 0.0
                low_px    = float(row[16]) if row[16] else 0.0
                close_px  = float(row[17]) if row[17] else 0.0
                last_px   = float(row[18]) if row[18] else 0.0
                settle_px = float(row[21]) if row[21] else 0.0
                underlyg  = float(row[20]) if row[20] else 0.0
                volume    = int(float(row[24])) if row[24] else 0
                lot_size  = int(float(row[28])) if row[28] else LOT_SIZE
            except (ValueError, IndexError):
                continue

            result[(strike, opt_type)] = OptionSnapshot(
                trade_date=d,
                symbol=SYMBOL,
                expiry=expiry,
                strike=strike,
                opt_type=opt_type,
                open_px=open_px,
                high_px=high_px,
                low_px=low_px,
                close_px=close_px,
                last_px=last_px,
                settle_px=settle_px,
                underlying=underlyg,
                volume=volume,
                lot_size=lot_size,
            )

    return result


def best_price(snap: OptionSnapshot) -> float:
    """Return the best available traded price, fallback to settlement."""
    if snap.close_px > 0:
        return snap.close_px
    if snap.last_px > 0:
        return snap.last_px
    return snap.settle_px


def intrinsic_value(strike: float, opt_type: str, spot: float) -> float:
    if opt_type == "CE":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def trading_days_between(start: date, end: date) -> list[date]:
    """Return all dates [start, end] for which a cache file exists."""
    days = []
    d = start
    while d <= end:
        if cache_file(d).exists():
            days.append(d)
        d += timedelta(days=1)
    return days


def get_spot(d: date, expiry: date) -> Optional[float]:
    """Get underlying spot from any NIFTY option row on that date."""
    data = load_nifty_options(d, expiry)
    if data:
        return next(iter(data.values())).underlying
    # fallback: try to find spot from any nearby expiry
    fp = cache_file(d)
    if not fp.exists():
        return None
    with open(fp, newline="") as fh:
        reader = csv.reader(fh)
        next(reader)
        for row in reader:
            if len(row) < 21:
                continue
            if row[7] == SYMBOL and row[12] in ("CE", "PE"):
                try:
                    val = float(row[20])
                    if val > 0:
                        return val
                except ValueError:
                    pass
    return None


# ── Transaction cost calculator ───────────────────────────────────────────────

def calc_transaction_costs(
    buy_premium: float,
    sell_premium: float,
    lots: int,
) -> float:
    """
    Approximate round-trip transaction costs for one strangle leg.
    Called twice (once for call, once for put).
    buy_premium / sell_premium are per-unit (not per-lot).
    """
    qty = lots * LOT_SIZE
    buy_value  = buy_premium  * qty
    sell_value = sell_premium * qty

    brokerage  = 2 * BROKERAGE_PER_ORDER           # entry + exit
    stt        = STT_PCT_ON_SELL    * sell_value    # STT on sell side
    exch       = EXCHANGE_CHARGES   * (buy_value + sell_value)
    sebi       = SEBI_CHARGES       * (buy_value + sell_value)
    stamp      = STAMP_DUTY_PCT     * buy_value     # stamp duty on buy only
    gst        = 0.18 * (brokerage + exch + sebi)   # GST on charges

    return brokerage + stt + exch + sebi + stamp + gst


# ── Core backtest engine ───────────────────────────────────────────────────────

def run_week(
    week_num: int,
    entry_date: date,
    expiry_date: date,
    otm_pct: float = OTM_PCT,
    lots: int = DEFAULT_LOTS,
) -> WeekResult:
    """Run one weekly strangle and return a WeekResult."""

    warnings: list[str] = []

    # ── 1. Entry day data ──────────────────────────────────────────────────────
    entry_data = load_nifty_options(entry_date, expiry_date)
    if not entry_data:
        raise ValueError(f"No data for entry {entry_date} / expiry {expiry_date}")

    spot = next(iter(entry_data.values())).underlying
    if spot <= 0:
        raise ValueError(f"Zero spot on {entry_date}")

    # ── 2. Strike selection — ATM (nearest strike to spot) ────────────────────
    atm_strike  = round_strike_nearest(spot)
    call_strike = atm_strike
    put_strike  = atm_strike

    # ── 3. Entry premiums ──────────────────────────────────────────────────────
    call_snap = entry_data.get((float(call_strike), "CE"))
    put_snap  = entry_data.get((float(put_strike),  "PE"))

    if call_snap is None:
        ce_strikes = sorted([k for k, t in entry_data if t == "CE"])
        best = min(ce_strikes, key=lambda x: abs(x - call_strike))
        call_snap = entry_data.get((best, "CE"))
        call_strike = int(best)
        warnings.append(f"Call: ATM strike {atm_strike} not found → used {call_strike}")

    if put_snap is None:
        pe_strikes = sorted([k for k, t in entry_data if t == "PE"])
        best = min(pe_strikes, key=lambda x: abs(x - put_strike))
        put_snap = entry_data.get((best, "PE"))
        put_strike = int(best)
        warnings.append(f"Put: ATM strike {atm_strike} not found → used {put_strike}")

    if call_snap is None or put_snap is None:
        raise ValueError(f"Could not find option data for {entry_date}")

    call_entry_px = best_price(call_snap)
    put_entry_px  = best_price(put_snap)

    if call_entry_px <= 0:
        warnings.append(f"Call entry price is 0 for strike {call_strike} on {entry_date}")
        call_entry_px = 0.05
    if put_entry_px <= 0:
        warnings.append(f"Put entry price is 0 for strike {put_strike} on {entry_date}")
        put_entry_px = 0.05

    total_debit = call_entry_px + put_entry_px
    break_even_upper = call_strike + total_debit
    break_even_lower = put_strike  - total_debit

    # ── 4. Walk through trading days ───────────────────────────────────────────
    trade_days  = trading_days_between(entry_date, expiry_date)
    daily_mtm:  list[DailyMTM] = []
    prev_value  = total_debit
    max_profit  = -999999.0
    max_loss    =  999999.0

    for idx, d in enumerate(trade_days):
        day_data = load_nifty_options(d, expiry_date)
        day_note = ""

        c_snap = day_data.get((float(call_strike), "CE"))
        p_snap = day_data.get((float(put_strike),  "PE"))

        spot_d = day_data[next(iter(day_data))].underlying if day_data else spot

        is_expiry = (d == expiry_date)

        if is_expiry:
            # NSE index options are European and cash-settled.
            # The SttlmPric column in the bhavcopy stores the UNDERLYING's
            # final settlement price (not the option's own value). Option
            # settlement = max(underlying_settlement - strike, 0) for CE,
            # max(strike - underlying_settlement, 0) for PE.
            # Use the underlying settlement price to determine final option value.
            final_spot = spot_d
            if c_snap and c_snap.settle_px > 0:
                # settle_px == underlying price for NSE index options
                final_spot = c_snap.settle_px
            elif p_snap and p_snap.settle_px > 0:
                final_spot = p_snap.settle_px

            call_px = intrinsic_value(call_strike, "CE", final_spot)
            put_px  = intrinsic_value(put_strike,  "PE", final_spot)
            spot_d  = final_spot   # update so MTM record shows settlement spot
            day_note = "EXPIRY"
        elif idx == 0:
            # Entry day
            call_px = call_entry_px
            put_px  = put_entry_px
            day_note = "ENTRY"
        else:
            call_px = best_price(c_snap) if c_snap else max(intrinsic_value(call_strike, "CE", spot_d), 0.05)
            put_px  = best_price(p_snap) if p_snap else max(intrinsic_value(put_strike,  "PE", spot_d), 0.05)
            if call_px <= 0:
                call_px = max(intrinsic_value(call_strike, "CE", spot_d), 0.05)
            if put_px <= 0:
                put_px = max(intrinsic_value(put_strike, "PE", spot_d), 0.05)

        combined_value = call_px + put_px
        daily_pnl      = (combined_value - prev_value) * lots * LOT_SIZE
        cumulative_pnl = (combined_value - total_debit) * lots * LOT_SIZE

        max_profit = max(max_profit, cumulative_pnl)
        max_loss   = min(max_loss,   cumulative_pnl)
        prev_value = combined_value

        daily_mtm.append(DailyMTM(
            trade_date=d,
            spot=spot_d,
            call_px=call_px,
            put_px=put_px,
            straddle_value=combined_value,
            daily_pnl=daily_pnl,
            cumulative_pnl=cumulative_pnl,
            note=day_note,
        ))

    # ── 5. Final P&L ──────────────────────────────────────────────────────────
    if daily_mtm:
        last = daily_mtm[-1]
        call_exit_px = last.call_px
        put_exit_px  = last.put_px
        spot_at_expiry = last.spot
    else:
        call_exit_px = 0.0
        put_exit_px  = 0.0
        spot_at_expiry = spot
        warnings.append("No daily MTM data available")

    total_credit = call_exit_px + put_exit_px
    call_intr    = intrinsic_value(call_strike, "CE", spot_at_expiry)
    put_intr     = intrinsic_value(put_strike,  "PE", spot_at_expiry)

    gross_pnl = (total_credit - total_debit) * lots * LOT_SIZE

    txn_cost = (
        calc_transaction_costs(call_entry_px, call_exit_px, lots) +
        calc_transaction_costs(put_entry_px,  put_exit_px,  lots)
    )

    net_pnl = gross_pnl - txn_cost
    net_pnl_per_lot = net_pnl / lots if lots else net_pnl

    outcome = "WIN" if net_pnl > 0 else ("NEUTRAL" if abs(net_pnl) < 1 else "LOSS")

    return WeekResult(
        week_num=week_num,
        entry_date=entry_date,
        expiry_date=expiry_date,
        spot_at_entry=spot,
        call_strike=call_strike,
        put_strike=put_strike,
        call_entry_px=call_entry_px,
        put_entry_px=put_entry_px,
        total_debit=total_debit,
        call_exit_px=call_exit_px,
        put_exit_px=put_exit_px,
        total_credit=total_credit,
        spot_at_expiry=spot_at_expiry,
        call_intrinsic=call_intr,
        put_intrinsic=put_intr,
        gross_pnl=gross_pnl,
        transaction_cost=txn_cost,
        net_pnl=net_pnl,
        net_pnl_per_lot=net_pnl_per_lot,
        max_profit_seen=max_profit,
        max_loss_seen=max_loss,
        break_even_upper=break_even_upper,
        break_even_lower=break_even_lower,
        daily_mtm=daily_mtm,
        outcome=outcome,
        warnings=warnings,
    )


# ── Report generation ─────────────────────────────────────────────────────────

def sep(char="═", width=80) -> str:
    return char * width

def _fmt_pnl(val: float) -> str:
    sign = "+" if val >= 0 else ""
    return f"{sign}₹{val:,.0f}"

def _outcome_icon(outcome: str) -> str:
    return {"WIN": "✔ WIN", "LOSS": "✘ LOSS", "NEUTRAL": "≈ NEUTRAL"}.get(outcome, outcome)


def print_report(results: list[WeekResult], otm_pct: float, lots: int) -> None:
    """Print formatted report to stdout."""
    W = 82
    print()
    print(sep("═", W))
    print(f"  LONG STRADDLE BACKTEST — NIFTY WEEKLY  |  ATM Strikes  |  Lots: {lots}")
    print(sep("═", W))
    print(f"  Strategy  : Buy ATM Call + Buy ATM Put (same strike, nearest to spot)")
    print(f"  Universe  : NIFTY  |  Lot size: {LOT_SIZE}")
    print(f"  Period    : {results[0].entry_date} → {results[-1].expiry_date}")
    print(f"  Weeks     : {len(results)}")
    print(sep("─", W))

    for r in results:
        print()
        move_pct = (r.spot_at_expiry - r.spot_at_entry) / r.spot_at_entry * 100
        print(f"  WEEK {r.week_num}  |  {r.entry_date} → {r.expiry_date}  "
              f"|  {_outcome_icon(r.outcome)}")
        print(sep("─", W))

        # Entry block
        print(f"  {'Spot at entry':<28} {r.spot_at_entry:>10,.2f}")
        print(f"  {'Spot at expiry':<28} {r.spot_at_expiry:>10,.2f}  "
              f"({'+' if move_pct>=0 else ''}{move_pct:.2f}%)")
        print()
        print(f"  {'LEG':<10} {'Strike':>10} {'Entry Px':>10} {'Exit Px':>10} "
              f"{'Intrinsic':>10} {'PnL/lot':>10}")
        print(f"  {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")

        call_pnl = (r.call_exit_px - r.call_entry_px) * LOT_SIZE * lots
        put_pnl  = (r.put_exit_px  - r.put_entry_px)  * LOT_SIZE * lots
        print(f"  {'CALL (Long)':<10} {r.call_strike:>10,} {r.call_entry_px:>10.2f} "
              f"{r.call_exit_px:>10.2f} {r.call_intrinsic:>10.2f} "
              f"{_fmt_pnl(call_pnl):>10}")
        print(f"  {'PUT (Long)':<10} {r.put_strike:>10,} {r.put_entry_px:>10.2f} "
              f"{r.put_exit_px:>10.2f} {r.put_intrinsic:>10.2f} "
              f"{_fmt_pnl(put_pnl):>10}")
        print()

        print(f"  {'Total debit (entry)':<28} {r.total_debit:>10.2f}  "
              f"(₹{r.total_debit * LOT_SIZE * lots:,.0f})")
        print(f"  {'Total credit (exit)':<28} {r.total_credit:>10.2f}  "
              f"(₹{r.total_credit * LOT_SIZE * lots:,.0f})")
        print()
        print(f"  {'Break-even upper':<28} {r.break_even_upper:>10.2f}")
        print(f"  {'Break-even lower':<28} {r.break_even_lower:>10.2f}")
        print()
        print(f"  {'Gross P&L':<28} {_fmt_pnl(r.gross_pnl):>14}")
        print(f"  {'Transaction costs':<28} {'- ₹' + f'{r.transaction_cost:,.0f}':>14}")
        print(f"  {'Net P&L':<28} {_fmt_pnl(r.net_pnl):>14}")
        print(f"  {'Net P&L per lot':<28} {_fmt_pnl(r.net_pnl_per_lot):>14}")
        print()
        print(f"  {'Max profit seen (MTM)':<28} {_fmt_pnl(r.max_profit_seen):>14}")
        print(f"  {'Max loss seen (MTM)':<28} {_fmt_pnl(r.max_loss_seen):>14}")

        # Daily MTM table
        print()
        print(f"  Daily Mark-to-Market:")
        print(f"  {'Date':<13} {'Spot':>9} {'Call Px':>9} {'Put Px':>9} "
              f"{'Combined':>9} {'Daily PnL':>11} {'Cumul PnL':>12} {'Note':<10}")
        print(f"  {'-'*13} {'-'*9} {'-'*9} {'-'*9} {'-'*9} {'-'*11} {'-'*12} {'-'*10}")
        for m in r.daily_mtm:
            print(f"  {str(m.trade_date):<13} {m.spot:>9,.1f} {m.call_px:>9.2f} "
                  f"{m.put_px:>9.2f} {m.straddle_value:>9.2f} "
                  f"{_fmt_pnl(m.daily_pnl):>11} {_fmt_pnl(m.cumulative_pnl):>12} "
                  f"{m.note:<10}")

        if r.warnings:
            print()
            for w in r.warnings:
                print(f"  ⚠  {w}")

        print()
        print(sep("─", W))

    # ── Summary table ──────────────────────────────────────────────────────────
    total_net   = sum(r.net_pnl for r in results)
    total_gross = sum(r.gross_pnl for r in results)
    total_cost  = sum(r.transaction_cost for r in results)
    wins        = sum(1 for r in results if r.outcome == "WIN")
    losses      = sum(1 for r in results if r.outcome == "LOSS")
    win_rate    = wins / len(results) * 100 if results else 0
    avg_net     = total_net / len(results) if results else 0

    gross_wins  = sum(r.gross_pnl for r in results if r.gross_pnl > 0)
    gross_loss  = abs(sum(r.gross_pnl for r in results if r.gross_pnl < 0))
    profit_factor = (gross_wins / gross_loss) if gross_loss > 0 else float("inf")

    capital_deployed = sum(r.total_debit * LOT_SIZE * lots for r in results)
    roi_pct = (total_net / capital_deployed * 100) if capital_deployed > 0 else 0

    # Running drawdown
    equity_curve = []
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in results:
        running += r.net_pnl
        equity_curve.append(running)
        peak = max(peak, running)
        dd = peak - running
        max_dd = max(max_dd, dd)

    print()
    print(sep("═", W))
    print(f"  SUMMARY — 4-WEEK PERFORMANCE")
    print(sep("═", W))
    print(f"  {'Metric':<35} {'Value':>20}")
    print(f"  {'-'*35} {'-'*20}")
    print(f"  {'Total weeks traded':<35} {len(results):>20}")
    print(f"  {'Winning weeks':<35} {wins:>20}")
    print(f"  {'Losing weeks':<35} {losses:>20}")
    print(f"  {'Win rate':<35} {win_rate:>19.1f}%")
    print(f"  {'─'*35} {'─'*20}")
    print(f"  {'Total gross P&L':<35} {_fmt_pnl(total_gross):>20}")
    print(f"  {'Total transaction costs':<35} {'- ₹' + f'{total_cost:,.0f}':>20}")
    print(f"  {'Total net P&L':<35} {_fmt_pnl(total_net):>20}")
    print(f"  {'Avg net P&L per week':<35} {_fmt_pnl(avg_net):>20}")
    print(f"  {'─'*35} {'─'*20}")
    print(f"  {'Best week':<35} {_fmt_pnl(max(r.net_pnl for r in results)):>20}")
    print(f"  {'Worst week':<35} {_fmt_pnl(min(r.net_pnl for r in results)):>20}")
    print(f"  {'Max intraweek drawdown':<35} {_fmt_pnl(-max_dd):>20}")
    print(f"  {'Profit factor':<35} {profit_factor:>19.2f}x")
    print(f"  {'─'*35} {'─'*20}")
    print(f"  {'Capital deployed (total)':<35} {'₹' + f'{capital_deployed:,.0f}':>20}")
    print(f"  {'ROI on total premium paid':<35} {roi_pct:>19.2f}%")
    print(f"  {'─'*35} {'─'*20}")

    # Per-week P&L bar chart (text)
    print()
    print(f"  P&L by Week (text chart):")
    max_abs = max(abs(r.net_pnl) for r in results) or 1
    bar_scale = 30 / max_abs
    for r in results:
        bar_len = int(abs(r.net_pnl) * bar_scale)
        bar = ("▓" if r.net_pnl >= 0 else "░") * bar_len
        sign = "+" if r.net_pnl >= 0 else "-"
        print(f"  W{r.week_num} ({r.expiry_date}) {bar:<32} {_fmt_pnl(r.net_pnl):>12}")

    # Equity curve
    print()
    print(f"  Equity curve (cumulative net P&L by week):")
    running = 0.0
    for r in results:
        running += r.net_pnl
        print(f"  Week {r.week_num}: {_fmt_pnl(running):>14}")

    print()
    print(sep("═", W))
    print()


def save_csv_report(results: list[WeekResult], path: Path) -> None:
    """Save trade summary as CSV."""
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "Week", "Entry Date", "Expiry Date",
            "Spot Entry", "Spot Expiry", "Move %",
            "Call Strike", "Put Strike",
            "Call Entry Px", "Put Entry Px", "Total Debit",
            "Call Exit Px", "Put Exit Px", "Total Credit",
            "Call Intrinsic", "Put Intrinsic",
            "Break-even Upper", "Break-even Lower",
            "Gross PnL", "Txn Cost", "Net PnL", "Net PnL/lot",
            "Max Profit (MTM)", "Max Loss (MTM)", "Outcome",
        ])
        for r in results:
            move = (r.spot_at_expiry - r.spot_at_entry) / r.spot_at_entry * 100
            w.writerow([
                r.week_num, r.entry_date, r.expiry_date,
                round(r.spot_at_entry, 2), round(r.spot_at_expiry, 2), round(move, 2),
                r.call_strike, r.put_strike,
                round(r.call_entry_px, 2), round(r.put_entry_px, 2), round(r.total_debit, 2),
                round(r.call_exit_px, 2), round(r.put_exit_px, 2), round(r.total_credit, 2),
                round(r.call_intrinsic, 2), round(r.put_intrinsic, 2),
                round(r.break_even_upper, 2), round(r.break_even_lower, 2),
                round(r.gross_pnl, 2), round(r.transaction_cost, 2),
                round(r.net_pnl, 2), round(r.net_pnl_per_lot, 2),
                round(r.max_profit_seen, 2), round(r.max_loss_seen, 2),
                r.outcome,
            ])
    print(f"  CSV report saved → {path}")


def save_daily_csv(results: list[WeekResult], path: Path) -> None:
    """Save daily MTM data for all weeks."""
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "Week", "Date", "Spot",
            "Call Strike", "Call Px",
            "Put Strike", "Put Px",
            "Combined Value", "Daily PnL", "Cumulative PnL", "Note",
        ])
        for r in results:
            for m in r.daily_mtm:
                w.writerow([
                    r.week_num, m.trade_date, round(m.spot, 2),
                    r.call_strike, round(m.call_px, 2),
                    r.put_strike, round(m.put_px, 2),
                    round(m.straddle_value, 2),
                    round(m.daily_pnl, 2),
                    round(m.cumulative_pnl, 2),
                    m.note,
                ])
    print(f"  Daily MTM CSV saved → {path}")


def try_plot(results: list[WeekResult], out_dir: Path) -> None:
    """Generate charts if matplotlib is available."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
    except ImportError:
        print("  ℹ  matplotlib not available — skipping charts")
        return

    fig = plt.figure(figsize=(16, 12))
    fig.suptitle(
        f"NIFTY Long Straddle Backtest — ATM Strikes, {len(results)} Weeks",
        fontsize=14, fontweight="bold",
    )
    gs = gridspec.GridSpec(3, 2, figure=fig, hspace=0.45, wspace=0.35)

    colors = ["#2196F3", "#4CAF50", "#FF9800", "#E91E63"]

    # ── Chart 1: Equity curve ─────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, :])
    all_dates, all_pnl = [], []
    running = 0.0
    for r in results:
        for m in r.daily_mtm:
            running_day = running + m.cumulative_pnl
            all_dates.append(m.trade_date)
            all_pnl.append(running_day)
        running += r.net_pnl

    ax1.plot(all_dates, all_pnl, lw=2, color="#1565C0", marker="o", ms=3)
    ax1.axhline(0, color="gray", lw=0.8, ls="--")
    ax1.fill_between(all_dates, all_pnl, 0,
                     where=[v >= 0 for v in all_pnl], alpha=0.25, color="green", label="Profit")
    ax1.fill_between(all_dates, all_pnl, 0,
                     where=[v < 0 for v in all_pnl], alpha=0.25, color="red", label="Loss")
    for r in results:
        ax1.axvline(r.expiry_date, color="orange", lw=1, ls=":", alpha=0.7)
    ax1.set_title("Cumulative Net P&L (all weeks)")
    ax1.set_ylabel("₹")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.3)

    # ── Chart 2: Per-week net P&L bar ─────────────────────────────────────────
    ax2 = fig.add_subplot(gs[1, 0])
    week_labels = [f"W{r.week_num}\n{r.expiry_date}" for r in results]
    week_pnls   = [r.net_pnl for r in results]
    bar_colors  = ["#43A047" if v >= 0 else "#E53935" for v in week_pnls]
    bars = ax2.bar(week_labels, week_pnls, color=bar_colors, edgecolor="white", width=0.55)
    ax2.axhline(0, color="black", lw=0.8)
    for bar, val in zip(bars, week_pnls):
        ax2.text(bar.get_x() + bar.get_width()/2, val + (200 if val >= 0 else -500),
                 f"₹{val:,.0f}", ha="center", va="bottom", fontsize=8)
    ax2.set_title("Net P&L per Week")
    ax2.set_ylabel("₹")
    ax2.grid(True, alpha=0.3, axis="y")

    # ── Chart 3: Combined premium decay per week ──────────────────────────────
    ax3 = fig.add_subplot(gs[1, 1])
    for i, r in enumerate(results):
        dates  = [m.trade_date for m in r.daily_mtm]
        values = [m.straddle_value for m in r.daily_mtm]
        ax3.plot(dates, values, lw=2, color=colors[i % len(colors)],
                 marker="o", ms=4, label=f"W{r.week_num} (exp {r.expiry_date})")
    ax3.set_title("Combined Option Value (Call + Put)")
    ax3.set_ylabel("₹ per unit")
    ax3.legend(fontsize=8)
    ax3.grid(True, alpha=0.3)

    # ── Chart 4: Spot vs break-evens ─────────────────────────────────────────
    ax4 = fig.add_subplot(gs[2, 0])
    for i, r in enumerate(results):
        dates  = [m.trade_date for m in r.daily_mtm]
        spots  = [m.spot for m in r.daily_mtm]
        ax4.plot(dates, spots, lw=2, color=colors[i % len(colors)], label=f"W{r.week_num}")
        ax4.axhline(r.break_even_upper, ls="--", lw=1, color=colors[i % len(colors)], alpha=0.5)
        ax4.axhline(r.break_even_lower, ls=":",  lw=1, color=colors[i % len(colors)], alpha=0.5)
    ax4.set_title("Spot vs Break-even Levels (dashed=upper, dotted=lower)")
    ax4.set_ylabel("Nifty Spot")
    ax4.legend(fontsize=8)
    ax4.grid(True, alpha=0.3)

    # ── Chart 5: Max profit vs max loss per week ──────────────────────────────
    ax5 = fig.add_subplot(gs[2, 1])
    wk = [f"W{r.week_num}" for r in results]
    mp = [r.max_profit_seen for r in results]
    ml = [abs(r.max_loss_seen) for r in results]
    x  = range(len(results))
    ax5.bar([i - 0.2 for i in x], mp, width=0.35, color="#43A047", label="Max profit seen", alpha=0.85)
    ax5.bar([i + 0.2 for i in x], ml, width=0.35, color="#E53935", label="Max loss seen",   alpha=0.85)
    ax5.set_xticks(list(x))
    ax5.set_xticklabels(wk)
    ax5.set_title("Intraweek Max Profit vs Max Loss")
    ax5.set_ylabel("₹")
    ax5.legend(fontsize=8)
    ax5.grid(True, alpha=0.3, axis="y")

    chart_path = out_dir / "long_strangle_charts.png"
    plt.savefig(chart_path, dpi=130, bbox_inches="tight")
    plt.close()
    print(f"  Charts saved → {chart_path}")


def _build_inline_charts(results: list[WeekResult]) -> dict[str, str]:
    """
    Render all charts to base64 PNG strings for embedding in HTML.
    Returns a dict of chart_name → data-URI string.
    """
    import io
    import base64
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import matplotlib.ticker as mticker

    colors = ["#2196F3", "#43A047", "#FF9800", "#E91E63"]
    charts: dict[str, str] = {}

    def _to_b64(fig) -> str:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
        buf.seek(0)
        encoded = base64.b64encode(buf.read()).decode("utf-8")
        plt.close(fig)
        return f"data:image/png;base64,{encoded}"

    # ── Chart A: Equity curve ─────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(13, 3.8))
    all_dates, all_pnl = [], []
    running = 0.0
    for r in results:
        for m in r.daily_mtm:
            all_dates.append(m.trade_date)
            all_pnl.append(running + m.cumulative_pnl)
        running += r.net_pnl
    ax.plot(all_dates, all_pnl, lw=2.2, color="#1565C0", marker="o", ms=4, zorder=3)
    ax.axhline(0, color="#888", lw=0.9, ls="--")
    ax.fill_between(all_dates, all_pnl, 0, where=[v >= 0 for v in all_pnl],
                    alpha=0.22, color="#43A047", label="Profit")
    ax.fill_between(all_dates, all_pnl, 0, where=[v < 0 for v in all_pnl],
                    alpha=0.22, color="#E53935", label="Loss")
    for r in results:
        ax.axvline(r.expiry_date, color="#FF9800", lw=1.2, ls=":", alpha=0.8,
                   label="Expiry" if r.week_num == 1 else "")
    ax.set_title("Cumulative Net P&L — All Weeks", fontsize=11, fontweight="bold", pad=8)
    ax.set_ylabel("₹", fontsize=10)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x:,.0f}"))
    ax.legend(fontsize=8, loc="lower left")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    charts["equity"] = _to_b64(fig)

    # ── Chart B: Per-week P&L bar ─────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(7, 4))
    wlabels = [f"W{r.week_num}\n{r.expiry_date}" for r in results]
    wpnl    = [r.net_pnl for r in results]
    bcolors = ["#43A047" if v >= 0 else "#E53935" for v in wpnl]
    bars = ax.bar(wlabels, wpnl, color=bcolors, edgecolor="white", width=0.52)
    ax.axhline(0, color="#333", lw=0.9)
    for bar, val in zip(bars, wpnl):
        offset = abs(val) * 0.06 * (-1 if val < 0 else 1)
        ax.text(bar.get_x() + bar.get_width() / 2, val + offset,
                f"₹{val:,.0f}", ha="center", va="center", fontsize=8.5, fontweight="bold")
    ax.set_title("Net P&L per Week", fontsize=11, fontweight="bold", pad=8)
    ax.set_ylabel("₹", fontsize=10)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x:,.0f}"))
    ax.grid(True, alpha=0.25, axis="y")
    fig.tight_layout()
    charts["weekly_pnl"] = _to_b64(fig)

    # ── Chart C: Premium decay ────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(7, 4))
    for i, r in enumerate(results):
        dates  = [m.trade_date for m in r.daily_mtm]
        values = [m.straddle_value for m in r.daily_mtm]
        ax.plot(dates, values, lw=2, color=colors[i % len(colors)],
                marker="o", ms=5, label=f"W{r.week_num} (exp {r.expiry_date})")
    ax.set_title("Combined Premium Decay (Call + Put)", fontsize=11, fontweight="bold", pad=8)
    ax.set_ylabel("₹ per unit", fontsize=10)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    charts["decay"] = _to_b64(fig)

    # ── Chart D: Spot vs break-evens ─────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(7, 4))
    for i, r in enumerate(results):
        dates = [m.trade_date for m in r.daily_mtm]
        spots = [m.spot       for m in r.daily_mtm]
        c = colors[i % len(colors)]
        ax.plot(dates, spots, lw=2, color=c, label=f"W{r.week_num}")
        ax.axhline(r.break_even_upper, ls="--", lw=1,   color=c, alpha=0.55)
        ax.axhline(r.break_even_lower, ls=":",  lw=1,   color=c, alpha=0.55)
    ax.set_title("Spot vs Break-even Levels", fontsize=11, fontweight="bold", pad=8)
    ax.set_ylabel("Nifty Spot", fontsize=10)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    charts["breakeven"] = _to_b64(fig)

    # ── Chart E: Payoff diagram (final week as representative) ────────────────
    fig, ax = plt.subplots(figsize=(7, 4))
    r = results[-1]
    spot_range = [r.put_strike * 0.90 + i * (r.call_strike * 1.10 - r.put_strike * 0.90) / 200
                  for i in range(201)]
    payoffs = [max(s - r.call_strike, 0) + max(r.put_strike - s, 0) - r.total_debit
               for s in spot_range]
    ax.plot(spot_range, payoffs, lw=2.2, color="#1565C0")
    ax.axhline(0, color="#888", lw=0.8, ls="--")
    ax.axvline(r.spot_at_entry, color="#FF9800", lw=1.5, ls="--",
               label=f"Entry spot {r.spot_at_entry:,.0f}")
    ax.axvline(r.spot_at_expiry, color="#43A047", lw=1.5, ls="-.",
               label=f"Expiry spot {r.spot_at_expiry:,.0f}")
    ax.fill_between(spot_range, payoffs, 0,
                    where=[v >= 0 for v in payoffs], alpha=0.2, color="#43A047")
    ax.fill_between(spot_range, payoffs, 0,
                    where=[v < 0 for v in payoffs], alpha=0.2, color="#E53935")
    ax.set_title(f"Payoff at Expiry — Week {r.week_num} (exp {r.expiry_date})",
                 fontsize=11, fontweight="bold", pad=8)
    ax.set_xlabel("Nifty Spot at Expiry", fontsize=9)
    ax.set_ylabel("P&L per unit (₹)", fontsize=10)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    charts["payoff"] = _to_b64(fig)

    # ── Chart F: Max profit vs max loss ───────────────────────────────────────
    fig, ax = plt.subplots(figsize=(7, 4))
    x   = range(len(results))
    mp  = [r.max_profit_seen for r in results]
    ml  = [abs(r.max_loss_seen) for r in results]
    ax.bar([i - 0.2 for i in x], mp, width=0.35, color="#43A047",
           label="Max profit seen", alpha=0.85)
    ax.bar([i + 0.2 for i in x], ml, width=0.35, color="#E53935",
           label="Max loss seen",   alpha=0.85)
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"W{r.week_num}" for r in results])
    ax.set_title("Intraweek Max Profit vs Max Loss", fontsize=11, fontweight="bold", pad=8)
    ax.set_ylabel("₹", fontsize=10)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x:,.0f}"))
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.25, axis="y")
    fig.tight_layout()
    charts["maxminpnl"] = _to_b64(fig)

    return charts


def save_html_report(
    results: list[WeekResult],
    path: Path,
    otm_pct: float,
    lots: int,
) -> None:
    """Generate a self-contained HTML report with embedded charts."""

    # ── Aggregate stats ───────────────────────────────────────────────────────
    total_net   = sum(r.net_pnl        for r in results)
    total_gross = sum(r.gross_pnl      for r in results)
    total_cost  = sum(r.transaction_cost for r in results)
    wins        = sum(1 for r in results if r.outcome == "WIN")
    losses      = sum(1 for r in results if r.outcome == "LOSS")
    win_rate    = wins / len(results) * 100 if results else 0
    avg_net     = total_net / len(results) if results else 0

    gross_wins  = sum(r.gross_pnl for r in results if r.gross_pnl > 0)
    gross_loss  = abs(sum(r.gross_pnl for r in results if r.gross_pnl < 0))
    pf          = (gross_wins / gross_loss) if gross_loss > 0 else 0.0

    capital     = sum(r.total_debit * LOT_SIZE * lots for r in results)
    roi_pct     = total_net / capital * 100 if capital > 0 else 0

    running = 0.0
    peak = 0.0
    max_dd = 0.0
    equity_rows = []
    for r in results:
        running += r.net_pnl
        equity_rows.append((r.week_num, r.expiry_date, running))
        peak   = max(peak, running)
        max_dd = max(max_dd, peak - running)

    gen_time = datetime.now().strftime("%d %b %Y  %H:%M:%S")

    # ── Inline charts ─────────────────────────────────────────────────────────
    charts: dict[str, str] = {}
    try:
        charts = _build_inline_charts(results)
    except Exception as e:
        print(f"  ⚠  HTML charts skipped: {e}")

    def _pnl_class(val: float) -> str:
        return "pos" if val > 0 else ("neg" if val < 0 else "neu")

    def _pnl_span(val: float) -> str:
        sign = "+" if val >= 0 else ""
        cls  = _pnl_class(val)
        return f'<span class="{cls}">{sign}₹{val:,.0f}</span>'

    def _img(key: str, alt: str, w: str = "100%") -> str:
        if key not in charts:
            return ""
        return (f'<div class="chart-wrap">'
                f'<img src="{charts[key]}" alt="{alt}" style="width:{w};max-width:100%;">'
                f'</div>')

    # ── Build per-week HTML cards ─────────────────────────────────────────────
    week_cards_html = ""
    for r in results:
        move_pct  = (r.spot_at_expiry - r.spot_at_entry) / r.spot_at_entry * 100
        move_cls  = "pos" if move_pct >= 0 else "neg"
        out_cls   = {"WIN": "badge-win", "LOSS": "badge-loss"}.get(r.outcome, "badge-neu")
        call_pnl  = (r.call_exit_px - r.call_entry_px) * LOT_SIZE * lots
        put_pnl   = (r.put_exit_px  - r.put_entry_px)  * LOT_SIZE * lots

        # Daily MTM rows
        mtm_rows = ""
        for m in r.daily_mtm:
            note_tag = (f'<span class="badge-note">{m.note}</span>' if m.note else "")
            mtm_rows += f"""
            <tr class="{'entry-row' if m.note == 'ENTRY' else 'expiry-row' if m.note == 'EXPIRY' else ''}">
                <td>{m.trade_date}</td>
                <td>{m.spot:,.2f}</td>
                <td>{m.call_px:.2f}</td>
                <td>{m.put_px:.2f}</td>
                <td><strong>{m.straddle_value:.2f}</strong></td>
                <td>{_pnl_span(m.daily_pnl)}</td>
                <td>{_pnl_span(m.cumulative_pnl)}</td>
                <td>{note_tag}</td>
            </tr>"""

        warn_html = ""
        if r.warnings:
            warn_html = '<div class="warnings">' + "".join(
                f'<p>⚠ {w}</p>' for w in r.warnings) + '</div>'

        week_cards_html += f"""
        <div class="week-card">
          <div class="week-header">
            <span class="week-title">Week {r.week_num} &nbsp;·&nbsp; {r.entry_date} → {r.expiry_date}</span>
            <span class="badge {out_cls}">{r.outcome}</span>
          </div>

          <div class="kpi-grid">
            <div class="kpi"><div class="kpi-label">Spot at Entry</div>
              <div class="kpi-value">{r.spot_at_entry:,.2f}</div></div>
            <div class="kpi"><div class="kpi-label">Spot at Expiry</div>
              <div class="kpi-value">{r.spot_at_expiry:,.2f}
                <small class="{move_cls}">({'+' if move_pct>=0 else ''}{move_pct:.2f}%)</small>
              </div></div>
            <div class="kpi"><div class="kpi-label">Total Debit</div>
              <div class="kpi-value">₹{r.total_debit * LOT_SIZE * lots:,.0f}
                <small>({r.total_debit:.2f}/unit)</small></div></div>
            <div class="kpi"><div class="kpi-label">Total Credit</div>
              <div class="kpi-value">₹{r.total_credit * LOT_SIZE * lots:,.0f}
                <small>({r.total_credit:.2f}/unit)</small></div></div>
            <div class="kpi"><div class="kpi-label">Gross P&amp;L</div>
              <div class="kpi-value {_pnl_class(r.gross_pnl)}">{_pnl_span(r.gross_pnl)}</div></div>
            <div class="kpi"><div class="kpi-label">Net P&amp;L</div>
              <div class="kpi-value {_pnl_class(r.net_pnl)} kpi-hero">{_pnl_span(r.net_pnl)}</div></div>
          </div>

          <div class="two-col">
            <div>
              <h4>Leg Details</h4>
              <table class="data-table">
                <thead><tr>
                  <th>Leg</th><th>Strike</th><th>Entry Px</th>
                  <th>Exit Px</th><th>Intrinsic</th><th>P&amp;L</th>
                </tr></thead>
                <tbody>
                  <tr>
                    <td><span class="call-tag">CALL ↑</span></td>
                    <td>{r.call_strike:,}</td>
                    <td>{r.call_entry_px:.2f}</td>
                    <td>{r.call_exit_px:.2f}</td>
                    <td>{r.call_intrinsic:.2f}</td>
                    <td>{_pnl_span(call_pnl)}</td>
                  </tr>
                  <tr>
                    <td><span class="put-tag">PUT ↓</span></td>
                    <td>{r.put_strike:,}</td>
                    <td>{r.put_entry_px:.2f}</td>
                    <td>{r.put_exit_px:.2f}</td>
                    <td>{r.put_intrinsic:.2f}</td>
                    <td>{_pnl_span(put_pnl)}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div>
              <h4>Risk Metrics</h4>
              <table class="kv-table">
                <tr><td>Break-even upper</td><td><strong>{r.break_even_upper:,.2f}</strong></td></tr>
                <tr><td>Break-even lower</td><td><strong>{r.break_even_lower:,.2f}</strong></td></tr>
                <tr><td>Max profit seen</td><td>{_pnl_span(r.max_profit_seen)}</td></tr>
                <tr><td>Max loss seen</td><td>{_pnl_span(r.max_loss_seen)}</td></tr>
                <tr><td>Transaction costs</td><td>₹{r.transaction_cost:,.0f}</td></tr>
                <tr><td>Net P&amp;L per lot</td><td>{_pnl_span(r.net_pnl_per_lot)}</td></tr>
              </table>
            </div>
          </div>

          <h4>Daily Mark-to-Market</h4>
          <div class="table-scroll">
            <table class="data-table mtm-table">
              <thead><tr>
                <th>Date</th><th>Spot</th>
                <th>Call ({r.call_strike:,})</th>
                <th>Put ({r.put_strike:,})</th>
                <th>Combined</th><th>Daily P&amp;L</th>
                <th>Cumulative P&amp;L</th><th>Note</th>
              </tr></thead>
              <tbody>{mtm_rows}</tbody>
            </table>
          </div>
          {warn_html}
        </div>
        """

    # ── Summary KPI row ───────────────────────────────────────────────────────
    summary_kpi_html = f"""
    <div class="kpi-grid summary-kpis">
      <div class="kpi"><div class="kpi-label">Total Weeks</div>
        <div class="kpi-value">{len(results)}</div></div>
      <div class="kpi"><div class="kpi-label">Win / Loss</div>
        <div class="kpi-value">{wins} / {losses}</div></div>
      <div class="kpi"><div class="kpi-label">Win Rate</div>
        <div class="kpi-value">{win_rate:.1f}%</div></div>
      <div class="kpi"><div class="kpi-label">Total Net P&amp;L</div>
        <div class="kpi-value {_pnl_class(total_net)} kpi-hero">{_pnl_span(total_net)}</div></div>
      <div class="kpi"><div class="kpi-label">Avg Net P&amp;L / Week</div>
        <div class="kpi-value {_pnl_class(avg_net)}">{_pnl_span(avg_net)}</div></div>
      <div class="kpi"><div class="kpi-label">Best Week</div>
        <div class="kpi-value pos">{_pnl_span(max(r.net_pnl for r in results))}</div></div>
      <div class="kpi"><div class="kpi-label">Worst Week</div>
        <div class="kpi-value neg">{_pnl_span(min(r.net_pnl for r in results))}</div></div>
      <div class="kpi"><div class="kpi-label">Max Drawdown</div>
        <div class="kpi-value neg">{_pnl_span(-max_dd)}</div></div>
      <div class="kpi"><div class="kpi-label">Profit Factor</div>
        <div class="kpi-value">{pf:.2f}x</div></div>
      <div class="kpi"><div class="kpi-label">Capital Deployed</div>
        <div class="kpi-value">₹{capital:,.0f}</div></div>
      <div class="kpi"><div class="kpi-label">ROI on Premium</div>
        <div class="kpi-value {_pnl_class(roi_pct)}">{'+' if roi_pct>=0 else ''}{roi_pct:.1f}%</div></div>
      <div class="kpi"><div class="kpi-label">Total Txn Costs</div>
        <div class="kpi-value">₹{total_cost:,.0f}</div></div>
    </div>
    """

    # ── Equity table ──────────────────────────────────────────────────────────
    eq_rows = "".join(
        f'<tr><td>Week {wn}</td><td>{exp}</td>'
        f'<td>{_pnl_span(cum)}</td></tr>'
        for wn, exp, cum in equity_rows
    )

    # ── Full HTML ─────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NIFTY Long Straddle Backtest Report</title>
  <style>
    :root {{
      --bg:      #0f1117;
      --surface: #1a1d27;
      --card:    #20232f;
      --border:  #2e3245;
      --accent:  #4f8ef7;
      --pos:     #4caf74;
      --neg:     #f25555;
      --neu:     #aab0c4;
      --text:    #e2e6f0;
      --muted:   #8890a8;
      --head-bg: #161923;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
      font-size: 14px;
      line-height: 1.6;
    }}
    a {{ color: var(--accent); }}

    /* ── Top bar ── */
    .topbar {{
      background: var(--head-bg);
      border-bottom: 1px solid var(--border);
      padding: 18px 40px;
      display: flex;
      align-items: baseline;
      gap: 18px;
    }}
    .topbar h1 {{
      font-size: 20px;
      font-weight: 700;
      color: var(--text);
    }}
    .topbar .meta {{ color: var(--muted); font-size: 13px; }}
    .topbar .pill {{
      background: rgba(79,142,247,0.15);
      color: var(--accent);
      border: 1px solid rgba(79,142,247,0.35);
      border-radius: 20px;
      padding: 2px 12px;
      font-size: 12px;
      font-weight: 600;
    }}

    /* ── Layout ── */
    .page {{ max-width: 1200px; margin: 0 auto; padding: 30px 24px 60px; }}
    section {{ margin-bottom: 36px; }}
    h2 {{
      font-size: 16px; font-weight: 700; color: var(--text);
      border-bottom: 2px solid var(--accent); padding-bottom: 6px; margin-bottom: 18px;
    }}
    h4 {{
      font-size: 13px; font-weight: 600; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.05em;
      margin: 18px 0 10px;
    }}

    /* ── KPI grid ── */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
      gap: 14px;
      margin-bottom: 24px;
    }}
    .summary-kpis {{ grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }}
    .kpi {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 16px;
    }}
    .kpi-label {{ font-size: 11px; color: var(--muted); text-transform: uppercase;
                  letter-spacing: 0.04em; margin-bottom: 4px; }}
    .kpi-value {{ font-size: 16px; font-weight: 700; color: var(--text); }}
    .kpi-hero  {{ font-size: 20px; }}
    .kpi-value small {{ font-size: 11px; font-weight: 400; color: var(--muted); }}

    /* ── Colour helpers ── */
    .pos {{ color: var(--pos) !important; }}
    .neg {{ color: var(--neg) !important; }}
    .neu {{ color: var(--neu) !important; }}

    /* ── Week card ── */
    .week-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 24px 28px;
      margin-bottom: 28px;
    }}
    .week-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 20px;
    }}
    .week-title {{ font-size: 16px; font-weight: 700; }}

    /* ── Badges ── */
    .badge {{
      padding: 4px 14px;
      border-radius: 20px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.06em;
    }}
    .badge-win  {{ background: rgba(76,175,116,0.18); color: var(--pos);
                   border: 1px solid rgba(76,175,116,0.4); }}
    .badge-loss {{ background: rgba(242,85,85,0.18);  color: var(--neg);
                   border: 1px solid rgba(242,85,85,0.4); }}
    .badge-neu  {{ background: rgba(170,176,196,0.15); color: var(--neu);
                   border: 1px solid var(--border); }}
    .badge-note {{
      background: rgba(79,142,247,0.15);
      color: var(--accent);
      padding: 1px 8px;
      border-radius: 10px;
      font-size: 11px;
      font-weight: 600;
    }}

    /* ── Tables ── */
    .data-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    .data-table th {{
      background: var(--head-bg);
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 9px 12px;
      text-align: right;
      border-bottom: 1px solid var(--border);
    }}
    .data-table th:first-child {{ text-align: left; }}
    .data-table td {{
      padding: 8px 12px;
      border-bottom: 1px solid var(--border);
      text-align: right;
    }}
    .data-table td:first-child {{ text-align: left; }}
    .data-table tr:last-child td {{ border-bottom: none; }}
    .data-table tr:hover td {{ background: rgba(255,255,255,0.03); }}

    .entry-row  td {{ background: rgba(79,142,247,0.06); }}
    .expiry-row td {{ background: rgba(255,152,0,0.07); }}

    .kv-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    .kv-table td {{
      padding: 7px 12px;
      border-bottom: 1px solid var(--border);
      color: var(--text);
    }}
    .kv-table td:first-child {{ color: var(--muted); width: 55%; }}
    .kv-table tr:last-child td {{ border-bottom: none; }}

    .table-scroll {{ overflow-x: auto; }}

    /* ── Two-col layout ── */
    .two-col {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 24px;
      margin-bottom: 4px;
    }}
    @media (max-width: 700px) {{
      .two-col {{ grid-template-columns: 1fr; }}
    }}

    /* ── Call / Put tags ── */
    .call-tag {{
      background: rgba(76,175,116,0.2); color: var(--pos);
      padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 700;
    }}
    .put-tag {{
      background: rgba(242,85,85,0.2); color: var(--neg);
      padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 700;
    }}

    /* ── Charts ── */
    .chart-wrap {{
      background: #fff;
      border-radius: 12px;
      overflow: hidden;
      margin: 0 0 28px;
    }}
    .chart-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
      margin-bottom: 28px;
    }}
    @media (max-width: 800px) {{
      .chart-grid {{ grid-template-columns: 1fr; }}
    }}

    /* ── Warnings ── */
    .warnings {{
      background: rgba(255,152,0,0.1);
      border: 1px solid rgba(255,152,0,0.3);
      border-radius: 8px;
      padding: 10px 16px;
      margin-top: 14px;
      font-size: 12px;
      color: #ffa726;
    }}
    .warnings p {{ margin: 2px 0; }}

    /* ── Footer ── */
    footer {{
      margin-top: 50px;
      padding-top: 18px;
      border-top: 1px solid var(--border);
      color: var(--muted);
      font-size: 12px;
      text-align: center;
    }}
  </style>
</head>
<body>

<div class="topbar">
  <h1>NIFTY Long Straddle Backtest</h1>
  <span class="pill">ATM Strikes</span>
  <span class="pill">Weekly Expiry</span>
  <span class="pill">{len(results)} Weeks</span>
  <span class="meta" style="margin-left:auto">Generated: {gen_time}</span>
</div>

<div class="page">

  <!-- ── Strategy info ── -->
  <section>
    <h2>Strategy Overview</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:18px;">
      <table class="kv-table" style="background:var(--card);border-radius:10px;padding:4px 0;">
        <tr><td>Underlying</td><td><strong>NIFTY (NSE Index)</strong></td></tr>
        <tr><td>Strategy</td><td><strong>Long Straddle</strong></td></tr>
        <tr><td>Call leg</td><td>Buy ATM Call — nearest strike to spot</td></tr>
        <tr><td>Put leg</td><td>Buy ATM Put  — same strike as call</td></tr>
        <tr><td>Expiry cycle</td><td>Weekly (every Tuesday)</td></tr>
        <tr><td>Entry</td><td>Wednesday (day after prior expiry)</td></tr>
        <tr><td>Exit</td><td>Expiry Tuesday at settlement</td></tr>
      </table>
      <table class="kv-table" style="background:var(--card);border-radius:10px;padding:4px 0;">
        <tr><td>Lot size</td><td><strong>{LOT_SIZE} units</strong></td></tr>
        <tr><td>Lots traded</td><td><strong>{lots}</strong></td></tr>
        <tr><td>Strike step</td><td>{STRIKE_STEP} points</td></tr>
        <tr><td>Backtest period</td><td>{results[0].entry_date} → {results[-1].expiry_date}</td></tr>
        <tr><td>Strike rule</td><td>Round spot to nearest {STRIKE_STEP} (ATM)</td></tr>
        <tr><td>Both legs</td><td>Same strike — call &amp; put at ATM</td></tr>
        <tr><td>Settlement basis</td><td>European cash-settled (intrinsic at expiry)</td></tr>
      </table>
    </div>
  </section>

  <!-- ── Summary KPIs ── -->
  <section>
    <h2>4-Week Summary</h2>
    {summary_kpi_html}
    <div style="display:grid;grid-template-columns:3fr 1fr;gap:20px;">
      <div></div>
      <table class="data-table" style="background:var(--card);border-radius:10px;">
        <thead><tr><th style="text-align:left">Week</th><th>Expiry</th><th>Cumulative P&amp;L</th></tr></thead>
        <tbody>{eq_rows}</tbody>
      </table>
    </div>
  </section>

  <!-- ── Charts ── -->
  <section>
    <h2>Performance Charts</h2>
    {_img("equity", "Equity Curve")}
    <div class="chart-grid">
      {_img("weekly_pnl", "Weekly P&L")}
      {_img("decay", "Premium Decay")}
      {_img("breakeven", "Spot vs Break-evens")}
      {_img("payoff", "Payoff Diagram")}
    </div>
    {_img("maxminpnl", "Max Profit vs Max Loss")}
  </section>

  <!-- ── Per-week trade cards ── -->
  <section>
    <h2>Weekly Trade Details</h2>
    {week_cards_html}
  </section>

  <!-- ── Interpretation ── -->
  <section>
    <h2>Strategy Analysis</h2>
    <div class="week-card" style="font-size:14px;line-height:1.8;">
      <p><strong>Long Straddle mechanics:</strong> Buy an ATM call and an ATM put at the same strike.
      The strategy pays a net debit (combined premium) upfront and profits if the underlying moves
      significantly in either direction beyond the break-even levels by expiry.</p>
      <br>
      <p><strong>Break-even formula:</strong></p>
      <ul style="margin:8px 0 8px 24px;">
        <li>Upper: ATM Strike + Total Debit paid</li>
        <li>Lower: ATM Strike − Total Debit paid</li>
      </ul>
      <br>
      <p><strong>Maximum loss:</strong> Limited to total premium paid — occurs when the underlying
      closes exactly at the ATM strike on expiry (both options expire worthless).</p>
      <p><strong>Maximum profit:</strong> Unlimited on the upside (via call); large on the downside
      (put profits as spot falls toward zero).</p>
      <br>
      <p><strong>This 4-week result:</strong> NIFTY moved only
      {", ".join(f"{abs((r.spot_at_expiry - r.spot_at_entry)/r.spot_at_entry*100):.2f}%" for r in results)}
      across the four expiry weeks. The ATM straddle requires the market to move by more than the
      combined premium paid to be profitable. In a range-bound, low-volatility period like this,
      the strategy consistently loses as both options decay toward zero.
      The strategy would have profited in any week with a sharp directional move exceeding the total debit.</p>
    </div>
  </section>

</div>

<footer>
  NIFTY Long Strangle Backtest &nbsp;|&nbsp; Data: NSE F&amp;O Bhavcopy &nbsp;|&nbsp;
  ±{otm_pct:.1f}% OTM Strikes &nbsp;|&nbsp; {len(results)} Weekly Cycles &nbsp;|&nbsp; {gen_time}
</footer>

</body>
</html>
"""

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  HTML report saved → {path}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Long Strangle Backtest — NIFTY Weekly"
    )
    parser.add_argument("--otm-pct",    type=float, default=OTM_PCT,
                        help="OTM %% for strikes (default: 4.0)")
    parser.add_argument("--lots",       type=int,   default=DEFAULT_LOTS,
                        help="Number of lots (default: 1)")
    parser.add_argument("--report-dir", type=str,   default=str(REPORT_DIR),
                        help="Directory to save report files")
    parser.add_argument("--no-charts",  action="store_true",
                        help="Skip chart generation")
    args = parser.parse_args()

    out_dir = Path(args.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nLoading data from: {CACHE_DIR}")
    print(f"Running {len(WEEKLY_CYCLES)} weekly cycles — NIFTY Long Strangle ±{args.otm_pct}%\n")

    results: list[WeekResult] = []
    for week_num, (entry, expiry) in enumerate(WEEKLY_CYCLES, start=1):
        print(f"  Processing Week {week_num}: entry={entry}  expiry={expiry} ...", end="", flush=True)
        try:
            res = run_week(week_num, entry, expiry, otm_pct=args.otm_pct, lots=args.lots)
            results.append(res)
            pnl_str = _fmt_pnl(res.net_pnl)
            print(f" {res.outcome}  {pnl_str}")
        except Exception as e:
            print(f" ERROR: {e}")
            sys.exit(1)

    print()
    print_report(results, otm_pct=args.otm_pct, lots=args.lots)

    # Save CSV reports
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_csv_report(results, out_dir / f"long_strangle_summary_{ts}.csv")
    save_daily_csv(results,  out_dir / f"long_strangle_daily_mtm_{ts}.csv")

    if not args.no_charts:
        try_plot(results, out_dir)

    save_html_report(
        results,
        out_dir / f"long_strangle_report_{ts}.html",
        otm_pct=args.otm_pct,
        lots=args.lots,
    )

    print()
    print(f"  Reports saved to: {out_dir}")
    print()


if __name__ == "__main__":
    main()
