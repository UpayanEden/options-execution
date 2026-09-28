"""
Prediction utilities for Iron Condor positions.

Provides:
  - Historical volatility from OHLC data
  - Probability of profit using Black-Scholes log-normal framework
  - Monthly P&L forecast based on completed trades + expected remaining trades
"""
from __future__ import annotations

import calendar
import math
from datetime import date
from typing import Optional


# ── Volatility ────────────────────────────────────────────────────────────────

def calculate_historical_volatility(ohlc_data: list[dict], window: int = 20) -> float:
    """
    Annualised historical volatility using log returns of close prices.

    ohlc_data: list of dicts, each with a 'close' key (as returned by
               ZerodhaBroker.get_historical_data).
    window:    number of most-recent bars to use (default 20 trading days ≈ 1 month).

    Returns annualised HV as a decimal fraction (e.g. 0.18 = 18%).
    Returns 0.0 if insufficient data.
    """
    closes = [float(d["close"]) for d in ohlc_data if d.get("close")]
    if len(closes) < 3:
        return 0.0

    # Keep only the last (window + 1) closes so we have `window` log-returns
    closes = closes[-(window + 1):]
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if len(log_returns) < 2:
        return 0.0

    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    daily_std = math.sqrt(variance)
    return daily_std * math.sqrt(252)   # annualise


# ── Probability ───────────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def probability_of_profit(
    spot: float,
    sell_put_strike: float,
    sell_call_strike: float,
    iv: float,
    dte: int,
    hv: float = 0.0,
    risk_free_rate: float = 0.065,      # ~6.5% Indian repo rate
    iv_weight: float = 0.6,             # blend: 60% IV, 40% HV when HV available
) -> dict:
    """
    Estimate the probability that the underlying expires between the two
    short strikes (the full-profit zone for an Iron Condor).

    Uses the Black-Scholes log-normal framework. When both IV and HV are
    available, blends them (IV tends to be forward-looking; HV is realised).

    Args:
        spot:             Current underlying price.
        sell_put_strike:  Lower short strike.
        sell_call_strike: Upper short strike.
        iv:               Implied volatility as a decimal (e.g. 0.20 for 20%).
        dte:              Days to expiry.
        hv:               Historical/realised volatility (0 = use IV only).
        risk_free_rate:   Annual risk-free rate (decimal).
        iv_weight:        Weight given to IV in the IV/HV blend (0–1).

    Returns a dict with:
        prob_profit:      P(sell_put < S_T < sell_call)  [0–1]
        prob_below_put:   P(S_T < sell_put)              [0–1]
        prob_above_call:  P(S_T > sell_call)             [0–1]
        sigma_used:       Effective volatility used
        iv:               IV passed in
        hv:               HV passed in
        dte:              DTE
    """
    if dte <= 0 or iv <= 0 or spot <= 0:
        return {"prob_profit": 0.0, "prob_below_put": 0.0, "prob_above_call": 0.0,
                "sigma_used": iv, "iv": iv, "hv": hv, "dte": dte}

    # Blend IV and HV for a more accurate realised-volatility estimate
    if hv > 0:
        sigma = iv_weight * iv + (1.0 - iv_weight) * hv
    else:
        sigma = iv

    T = dte / 365.0
    drift = (risk_free_rate - 0.5 * sigma ** 2) * T
    sigma_sqrt_T = sigma * math.sqrt(T)

    # Under log-normal: P(S_T > K) = N(d2_K) where
    #   d2_K = [ln(S/K) + drift] / (σ√T)
    # P(K_low < S_T < K_high) = P(S_T > K_low) - P(S_T > K_high)
    #                          = N(d2_put) - N(d2_call)

    d2_put  = (math.log(spot / sell_put_strike)  + drift) / sigma_sqrt_T
    d2_call = (math.log(spot / sell_call_strike) + drift) / sigma_sqrt_T

    prob_above_put   = _norm_cdf(d2_put)    # P(S_T > sell_put)
    prob_above_call  = _norm_cdf(d2_call)   # P(S_T > sell_call)
    prob_below_put   = 1.0 - prob_above_put
    prob_profit      = prob_above_put - prob_above_call

    return {
        "prob_profit":     max(0.0, min(1.0, prob_profit)),
        "prob_below_put":  max(0.0, min(1.0, prob_below_put)),
        "prob_above_call": max(0.0, min(1.0, prob_above_call)),
        "sigma_used":      sigma,
        "iv":              iv,
        "hv":              hv,
        "dte":             dte,
    }


def expected_pnl_at_expiry(
    prob_profit: float,
    max_profit: float,
    max_loss: float,
    partial_exit_pct: float = 0.5,
) -> float:
    """
    Simple expected-value estimate of P&L at expiry.

    Assumes:
      - Winning trades capture `partial_exit_pct` of max_profit on average
        (because most exits happen before expiry via profit target).
      - Losing trades lose `max_loss` on average.
    """
    prob_loss = 1.0 - prob_profit
    ev = prob_profit * max_profit * partial_exit_pct - prob_loss * max_loss
    return ev


# ── Monthly forecast ──────────────────────────────────────────────────────────

def monthly_forecast(
    monthly_summary: dict,
    capital: float,
    monthly_target_pct: float,
    avg_credit_per_trade: float = 0.0,
    default_win_rate: float = 0.70,
    capture_pct: float = 0.50,
) -> dict:
    """
    Project end-of-month P&L given current completed trades and calendar.

    monthly_summary: dict from AutoTrader.monthly_summary()
    capital:         Total capital (₹)
    monthly_target_pct: Monthly return target (%)
    avg_credit_per_trade: Average credit received per trade (₹). Used to
                          estimate expected per-trade P&L for remaining trades.
    default_win_rate: Prior win rate used when < 3 completed trades.
    capture_pct:     Fraction of max profit captured on winning trades.

    Returns dict with forecast metrics.
    """
    today = date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_elapsed   = today.day
    days_remaining = days_in_month - days_elapsed

    # Approximate remaining trading days (NSE ~252 days/year ≈ 5/7 calendar)
    trading_days_remaining = max(0, round(days_remaining * 5 / 7))
    # Iron condors: ~1 entry per week → ~4 per month; pro-rate to remaining weeks
    trades_remaining = max(0, round(trading_days_remaining / 5))

    current_pnl    = monthly_summary.get("total_pnl", 0.0)
    current_trades = monthly_summary.get("trades", 0)
    current_wins   = monthly_summary.get("wins", 0)

    # Observed win rate (use prior if < 3 samples)
    obs_win_rate = (current_wins / current_trades) if current_trades >= 3 else default_win_rate

    # Expected P&L per future trade
    expected_per_trade = avg_credit_per_trade * obs_win_rate * capture_pct if avg_credit_per_trade else 0.0
    projected_additional = trades_remaining * expected_per_trade
    projected_total = current_pnl + projected_additional

    target = capital * monthly_target_pct / 100.0
    shortfall = max(0.0, target - current_pnl)
    pct_of_target = (current_pnl / target * 100) if target else 0.0

    return {
        "days_remaining":          days_remaining,
        "trading_days_remaining":  trading_days_remaining,
        "trades_completed":        current_trades,
        "trades_remaining":        trades_remaining,
        "obs_win_rate":            obs_win_rate,
        "current_pnl":             current_pnl,
        "projected_total":         projected_total,
        "target":                  target,
        "shortfall":               shortfall,
        "pct_of_target":           pct_of_target,
        "on_track":                projected_total >= target * 0.8,
    }
