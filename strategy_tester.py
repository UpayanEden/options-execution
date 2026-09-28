"""
Quick strategy tester using cached NSE F&O bhavcopy data.
Reads from the existing bhavcopy cache in Quant Projects.
Does NOT modify any Quant Projects files.
"""
from __future__ import annotations

import glob
import math
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional


# Default path to the bhavcopy cache — can be overridden
DEFAULT_CACHE_DIR = "/media/upayan/Work/Quant Projects/projects/project1/nse_bhavcopy_cache"


@dataclass
class BacktestTrade:
    underlying: str
    expiry: date
    entry_date: date
    dte_at_entry: int
    spot_at_entry: float
    sell_put_strike: float
    sell_call_strike: float
    buy_put_strike: float
    buy_call_strike: float
    net_credit: float
    put_spread_width: float
    call_spread_width: float
    max_profit: float
    max_loss: float
    exit_reason: str   # EXPIRY, STOP_LOSS, PROFIT_TARGET
    exit_pnl: float
    return_on_risk: float


@dataclass
class BacktestResult:
    underlying: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    avg_credit: float = 0.0
    avg_return_on_risk: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    trades: list[BacktestTrade] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def load_bhavcopy_data(
    underlying: str,
    cache_dir: str = DEFAULT_CACHE_DIR,
    max_files: int = 200,
) -> dict[date, dict]:
    """
    Load NSE bhavcopy data for a given underlying.
    Returns dict: {trading_date: {(expiry, strike, opt_type): close_price}}
    Only reads up to max_files most recent files.
    """
    import csv

    cache_path = Path(cache_dir)
    if not cache_path.exists():
        return {}

    csv_files = sorted(cache_path.glob("fo_*.csv"))[-max_files:]
    data: dict[date, dict] = {}

    for csv_file in csv_files:
        try:
            with open(csv_file, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Handle both old and new NSE bhavcopy column formats
                    sym = (
                        row.get("TCKRSYMB") or row.get("SYMBOL") or
                        row.get("TckrSymb") or ""
                    ).strip().upper()
                    if sym != underlying.upper():
                        continue

                    opt_type = (
                        row.get("OPTNTP") or row.get("OPTION_TYPE") or
                        row.get("OptnTp") or ""
                    ).strip().upper()
                    if opt_type not in ("CE", "PE"):
                        continue

                    # Parse expiry
                    exp_raw = (
                        row.get("XPRYDT") or row.get("EXPIRY_DATE") or
                        row.get("XpryDt") or ""
                    ).strip()
                    try:
                        if "-" in exp_raw:
                            exp_date = date.fromisoformat(exp_raw[:10])
                        elif len(exp_raw) == 8:
                            exp_date = date(int(exp_raw[:4]), int(exp_raw[4:6]), int(exp_raw[6:8]))
                        else:
                            continue
                    except Exception:
                        continue

                    # Parse strike
                    strike_raw = (
                        row.get("STRKPRIC") or row.get("STRIKE_PRICE") or
                        row.get("StrkPric") or "0"
                    ).strip()
                    try:
                        strike = float(strike_raw)
                    except Exception:
                        continue

                    # Parse close price (settlement price)
                    close_raw = (
                        row.get("STTLMPRIC") or row.get("CLOSE") or
                        row.get("CLOSE_PRICE") or row.get("SttlmPric") or
                        row.get("CLSPRIC") or "0"
                    ).strip()
                    try:
                        close = float(close_raw)
                    except Exception:
                        close = 0.0

                    # Parse underlying price
                    spot_raw = (
                        row.get("UNDRLYGPRIC") or row.get("UNDERLYING_PRICE") or
                        row.get("UndrlygPric") or "0"
                    ).strip()
                    try:
                        spot = float(spot_raw) if spot_raw else 0.0
                    except Exception:
                        spot = 0.0

                    # Parse trading date from filename: fo_YYYYMMDD.csv
                    try:
                        fname = csv_file.stem  # fo_20260101
                        date_part = fname.split("_")[-1]
                        tdate = date(int(date_part[:4]), int(date_part[4:6]), int(date_part[6:8]))
                    except Exception:
                        continue

                    if tdate not in data:
                        data[tdate] = {}
                    data[tdate][(exp_date, round(strike, 2), opt_type)] = {
                        "close": close,
                        "spot": spot,
                    }
        except Exception:
            continue

    return data


def run_backtest(
    underlying: str,
    iv: float = 0.35,
    short_mult: float = 1.0,
    long_mult: float = 1.3,
    min_dte: int = 15,
    max_dte: int = 45,
    lots: int = 1,
    lot_size: int = 100,  # placeholder if not known
    stop_loss_mult: float = 2.0,    # exit if loss > stop_loss_mult × credit
    profit_target_pct: float = 80.0, # exit if profit >= this % of max profit
    cache_dir: str = DEFAULT_CACHE_DIR,
) -> BacktestResult:
    """
    Simulate iron condor entries on historical bhavcopy data.
    Entry: first available date where DTE is between min_dte and max_dte.
    Exit: expiry, stop loss, or profit target — whichever comes first.
    """
    result = BacktestResult(underlying=underlying)

    data = load_bhavcopy_data(underlying, cache_dir)
    if not data:
        result.errors.append(f"No bhavcopy data found for {underlying} in {cache_dir}")
        return result

    trading_dates = sorted(data.keys())
    if not trading_dates:
        result.errors.append("No trading dates loaded")
        return result

    # Find all expiries across the dataset
    all_expiries: set[date] = set()
    for day_data in data.values():
        for (exp, strike, opt_type) in day_data.keys():
            all_expiries.add(exp)

    # For each expiry, find an entry date
    equity_curve = [0.0]
    processed_expiries = set()

    for expiry in sorted(all_expiries):
        if expiry in processed_expiries:
            continue

        # Find entry date: first date where DTE is in [min_dte, max_dte]
        entry_date = None
        entry_spot = None
        entry_dte = None
        for tdate in trading_dates:
            if tdate >= expiry:
                break
            dte = (expiry - tdate).days
            if min_dte <= dte <= max_dte:
                # Check if we have data for this underlying on this date
                day_data = data.get(tdate, {})
                spots = [v["spot"] for (e, s, o), v in day_data.items()
                         if e == expiry and v["spot"] > 0]
                if not spots:
                    continue
                entry_date = tdate
                entry_spot = sum(spots) / len(spots)
                entry_dte = dte
                break

        if not entry_date or not entry_spot:
            continue

        # Compute expected move
        em = iv * entry_spot * math.sqrt(entry_dte / 365.0)

        # Determine ideal strikes
        ideal_sell_put = entry_spot * (1 - short_mult * (em / entry_spot))
        ideal_sell_call = entry_spot * (1 + short_mult * (em / entry_spot))
        ideal_buy_put = entry_spot * (1 - long_mult * (em / entry_spot))
        ideal_buy_call = entry_spot * (1 + long_mult * (em / entry_spot))

        # Find nearest available strikes
        day_data = data[entry_date]
        pe_strikes = sorted(set(s for (e, s, o) in day_data.keys() if e == expiry and o == "PE"))
        ce_strikes = sorted(set(s for (e, s, o) in day_data.keys() if e == expiry and o == "CE"))

        if not pe_strikes or not ce_strikes:
            continue

        def nearest(target: float, strikes: list[float]) -> float:
            return min(strikes, key=lambda s: abs(s - target))

        sell_put = nearest(ideal_sell_put, pe_strikes)
        sell_call = nearest(ideal_sell_call, ce_strikes)
        buy_put_candidates = [s for s in pe_strikes if s < sell_put]
        buy_call_candidates = [s for s in ce_strikes if s > sell_call]

        if not buy_put_candidates or not buy_call_candidates:
            continue

        buy_put = nearest(ideal_buy_put, buy_put_candidates)
        buy_call = nearest(ideal_buy_call, buy_call_candidates)

        # Get entry prices (use close as proxy for premium)
        def get_price(strike: float, opt_type: str, tdate: date) -> float:
            key = (expiry, round(strike, 2), opt_type)
            entry = data.get(tdate, {}).get(key)
            if entry:
                return entry["close"]
            return 0.0

        bp_price = get_price(buy_put, "PE", entry_date)
        sp_price = get_price(sell_put, "PE", entry_date)
        sc_price = get_price(sell_call, "CE", entry_date)
        bc_price = get_price(buy_call, "CE", entry_date)

        if not all([bp_price > 0, sp_price > 0, sc_price > 0, bc_price > 0]):
            continue

        net_credit = (sp_price + sc_price) - (bp_price + bc_price)
        if net_credit <= 0:
            continue

        put_spread = sell_put - buy_put
        call_spread = buy_call - sell_call
        spread_width = min(put_spread, call_spread)
        max_profit = net_credit * lot_size * lots
        max_loss = (spread_width - net_credit) * lot_size * lots

        if max_loss <= 0:
            continue

        # Simulate day-by-day until exit
        exit_reason = "EXPIRY"
        exit_pnl = max_profit  # if held to expiry and OTM, keep full credit

        stop_loss_threshold = -net_credit * stop_loss_mult * lot_size * lots
        profit_target_threshold = max_profit * (profit_target_pct / 100)

        for tdate in trading_dates:
            if tdate <= entry_date:
                continue
            if tdate > expiry:
                # Expiry — check if any short is ITM
                # Use last available spot
                last_day = max(d for d in trading_dates if d <= expiry)
                last_data = data.get(last_day, {})
                exp_spots = [v["spot"] for (e, s, o), v in last_data.items()
                             if e == expiry and v["spot"] > 0]
                if exp_spots:
                    final_spot = sum(exp_spots) / len(exp_spots)
                    sp_intrinsic = max(0, sell_put - final_spot)
                    bp_intrinsic = max(0, final_spot - buy_put) if final_spot < sell_put else 0
                    sc_intrinsic = max(0, final_spot - sell_call)
                    bc_intrinsic = max(0, buy_call - final_spot) if final_spot > sell_call else 0
                    settlement_loss = (sp_intrinsic - bp_intrinsic + sc_intrinsic - bc_intrinsic) * lot_size * lots
                    exit_pnl = max_profit - settlement_loss
                exit_reason = "EXPIRY"
                break

            # MTM check
            bp_now = get_price(buy_put, "PE", tdate)
            sp_now = get_price(sell_put, "PE", tdate)
            sc_now = get_price(sell_call, "CE", tdate)
            bc_now = get_price(buy_call, "CE", tdate)

            if not all([sp_now > 0, sc_now > 0]):
                continue

            cost_to_close = (sp_now + sc_now) - (bp_now + bc_now) if bp_now > 0 and bc_now > 0 else (sp_now + sc_now)
            mtm_pnl = (net_credit - cost_to_close) * lot_size * lots

            if mtm_pnl <= stop_loss_threshold:
                exit_pnl = mtm_pnl
                exit_reason = "STOP_LOSS"
                break
            if mtm_pnl >= profit_target_threshold:
                exit_pnl = mtm_pnl
                exit_reason = "PROFIT_TARGET"
                break

        return_on_risk_val = (exit_pnl / max_loss * 100) if max_loss > 0 else 0

        trade = BacktestTrade(
            underlying=underlying,
            expiry=expiry,
            entry_date=entry_date,
            dte_at_entry=entry_dte,
            spot_at_entry=entry_spot,
            sell_put_strike=sell_put,
            sell_call_strike=sell_call,
            buy_put_strike=buy_put,
            buy_call_strike=buy_call,
            net_credit=net_credit,
            put_spread_width=put_spread,
            call_spread_width=call_spread,
            max_profit=max_profit,
            max_loss=max_loss,
            exit_reason=exit_reason,
            exit_pnl=exit_pnl,
            return_on_risk=return_on_risk_val,
        )
        result.trades.append(trade)
        processed_expiries.add(expiry)

    if not result.trades:
        result.errors.append("No valid trades found in data")
        return result

    # Compute summary stats
    result.total_trades = len(result.trades)
    pnls = [t.exit_pnl for t in result.trades]
    result.wins = sum(1 for p in pnls if p > 0)
    result.losses = sum(1 for p in pnls if p <= 0)
    result.win_rate = result.wins / result.total_trades * 100
    result.total_pnl = sum(pnls)
    result.avg_pnl = result.total_pnl / result.total_trades
    result.avg_credit = sum(t.net_credit for t in result.trades) / result.total_trades
    result.avg_return_on_risk = sum(t.return_on_risk for t in result.trades) / result.total_trades
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    result.best_trade = max(pnls)
    result.worst_trade = min(pnls)

    # Max drawdown
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)
    result.max_drawdown = max_dd

    return result
