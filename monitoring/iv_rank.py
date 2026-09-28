"""
IV Rank and IV Percentile engine.

IV Rank = (Current IV - 52-week Low) / (52-week High - 52-week Low) × 100
IV Percentile = % of past-year days where IV < current IV

Readings are persisted in the iv_history DB table. On day 1, falls back to the
configured iv_assumption. After 30 days the rank becomes meaningful; after 252
days it is fully reliable.

Also exposes: Black-Scholes IV computation via Newton-Raphson, used to derive
actual market-implied volatility from ATM option prices.
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# ── Black-Scholes helpers ─────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(S: float, K: float, T: float, r: float, sigma: float, opt: str) -> float:
    """Black-Scholes price for European CE or PE."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, S - K) if opt == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega (∂price/∂sigma) — same for CE and PE."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return S * math.exp(-0.5 * d1 ** 2) / math.sqrt(2 * math.pi) * math.sqrt(T)


def compute_bs_iv(
    market_price: float,
    spot: float,
    strike: float,
    dte: int,
    option_type: str,          # "CE" or "PE"
    risk_free: float = 0.065,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> Optional[float]:
    """
    Newton-Raphson implied volatility from observed option price.
    Returns IV as a decimal (e.g. 0.20 = 20%) or None if it cannot converge.
    """
    T = dte / 365.0
    if T <= 0 or market_price <= 0 or spot <= 0 or strike <= 0:
        return None
    sigma = 0.30  # initial guess
    for _ in range(max_iter):
        price = _bs_price(spot, strike, T, risk_free, sigma, option_type)
        vega  = _bs_vega(spot, strike, T, risk_free, sigma)
        if vega < 1e-10:
            break
        diff  = price - market_price
        if abs(diff) < tol:
            break
        sigma -= diff / vega
        sigma = max(0.01, min(sigma, 5.0))   # keep in [1%, 500%]
    return sigma if 0.01 <= sigma <= 5.0 else None


# ── IV History Storage ────────────────────────────────────────────────────────

@dataclass
class IVReading:
    date: date
    underlying: str
    iv: float                  # annualised, decimal fraction
    spot: float
    source: str                # "computed" | "configured"


def store_iv_reading(db_path: str, reading: IVReading) -> None:
    """Upsert one IV reading into iv_history (one row per underlying per date)."""
    try:
        from sqlalchemy import text
        from storage.database import create_db_engine
        engine = create_db_engine(db_path)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO iv_history "
                    "(date, underlying, iv_value, spot, source) "
                    "VALUES (:d, :ul, :iv, :spot, :src)"
                ),
                {"d": reading.date.isoformat(), "ul": reading.underlying,
                 "iv": reading.iv, "spot": reading.spot, "src": reading.source},
            )
            conn.commit()
    except Exception as exc:
        logger.warning("iv_store_failed", extra={"error": str(exc)})


def get_iv_history(db_path: str, underlying: str, lookback_days: int = 252) -> list[float]:
    """Return list of IV values (oldest→newest) from DB for lookback_days."""
    try:
        from sqlalchemy import text
        from storage.database import create_db_engine
        engine = create_db_engine(db_path)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT iv_value FROM iv_history "
                    "WHERE underlying = :ul "
                    "ORDER BY date DESC LIMIT :n"
                ),
                {"ul": underlying, "n": lookback_days},
            ).fetchall()
        return [float(r[0]) for r in reversed(rows)]
    except Exception as exc:
        logger.warning("iv_history_fetch_failed", extra={"error": str(exc)})
        return []


# ── IV Rank Engine ────────────────────────────────────────────────────────────

@dataclass
class IVRankResult:
    underlying: str
    current_iv: float          # decimal fraction
    iv_rank: float             # 0–100 (higher = richer premium)
    iv_percentile: float       # 0–100
    history_days: int          # how many days of data we have
    iv_52w_high: float
    iv_52w_low: float
    confidence: str            # "high" (≥252d) | "medium" (≥30d) | "low" (<30d)
    recommendation: str        # "ENTER" | "CAUTION" | "SKIP"


def compute_iv_rank(
    underlying: str,
    current_iv: float,
    db_path: str,
    min_iv_rank_to_enter: float = 30.0,
    lookback_days: int = 252,
) -> IVRankResult:
    """
    Compute IV rank and percentile for an underlying.
    Uses stored history; stores the current reading if not already stored today.
    """
    history = get_iv_history(db_path, underlying, lookback_days)

    # Include current reading for computation even if not yet stored
    all_ivs = history + [current_iv]
    hi  = max(all_ivs)
    lo  = min(all_ivs)
    n   = len(all_ivs)

    iv_rank = ((current_iv - lo) / (hi - lo) * 100) if hi > lo else 50.0
    iv_pct  = sum(1 for v in all_ivs if v < current_iv) / n * 100

    if n >= 252:
        confidence = "high"
    elif n >= 30:
        confidence = "medium"
    else:
        confidence = "low"

    if iv_rank >= min_iv_rank_to_enter:
        recommendation = "ENTER"
    elif iv_rank >= min_iv_rank_to_enter * 0.7:
        recommendation = "CAUTION"
    else:
        recommendation = "SKIP"

    return IVRankResult(
        underlying=underlying,
        current_iv=current_iv,
        iv_rank=round(iv_rank, 1),
        iv_percentile=round(iv_pct, 1),
        history_days=n,
        iv_52w_high=round(hi, 4),
        iv_52w_low=round(lo, 4),
        confidence=confidence,
        recommendation=recommendation,
    )


def fetch_and_store_atm_iv(
    underlying: str,
    spot: float,
    broker,
    cache_path: str,
    db_path: str,
    expiry: "date | None" = None,
    risk_free: float = 0.065,
) -> Optional[float]:
    """
    Fetch the ATM straddle from Zerodha, compute implied volatility,
    store in iv_history, and return the IV value.
    Falls back gracefully; returns None on failure.
    """
    from datetime import date as _date
    from stock_picker import (
        get_expiries_for_underlying,
        get_strikes_for_underlying_expiry,
        get_lot_size_for_underlying,
    )
    from execution.models import OptionType
    from execution.quote_engine import QuoteEngine

    try:
        today = _date.today()
        if expiry is None:
            expiries = get_expiries_for_underlying(cache_path, underlying)
            valid    = [e for e in expiries if 15 <= (e - today).days <= 45]
            if not valid:
                return None
            expiry = min(valid, key=lambda e: abs((e - today).days - 30))

        dte = max(1, (expiry - today).days)

        # Find ATM strike for both CE and PE
        ce_strikes = get_strikes_for_underlying_expiry(cache_path, underlying, expiry, "CE")
        pe_strikes = get_strikes_for_underlying_expiry(cache_path, underlying, expiry, "PE")
        if not ce_strikes or not pe_strikes:
            return None

        atm_ce = min(ce_strikes, key=lambda s: abs(float(s) - spot))
        atm_pe = min(pe_strikes, key=lambda s: abs(float(s) - spot))

        # Resolve and fetch quotes
        instruments = []
        for strike, opt_type in [(atm_ce, "CE"), (atm_pe, "PE")]:
            try:
                inst = broker.resolve_instrument(
                    underlying=underlying, exchange="NFO",
                    expiry=expiry, strike=strike, option_type=opt_type,
                )
                instruments.append((inst.instrument_token, inst.trading_symbol))
            except Exception:
                pass

        if not instruments:
            return None

        qe = QuoteEngine(broker)
        quotes = qe.fetch_all(instruments)
        if not quotes:
            return None

        # Average IV from both legs
        ivs = []
        for (tok, sym), (strike, opt_type) in zip(instruments, [(atm_ce, "CE"), (atm_pe, "PE")]):
            q = quotes.get(sym)
            if q is None:
                continue
            mid = float(q.midpoint)
            if mid <= 0:
                mid = float(q.ltp)
            if mid <= 0:
                continue
            iv = compute_bs_iv(mid, spot, float(strike), dte, opt_type, risk_free)
            if iv is not None:
                ivs.append(iv)

        if not ivs:
            return None

        avg_iv = sum(ivs) / len(ivs)

        # Store
        store_iv_reading(db_path, IVReading(
            date=today, underlying=underlying,
            iv=avg_iv, spot=spot, source="computed",
        ))
        logger.info("iv_computed", extra={
            "underlying": underlying, "iv": f"{avg_iv:.3f}",
            "dte": dte, "source": "computed",
        })
        return avg_iv

    except Exception as exc:
        logger.warning("iv_fetch_failed", extra={"underlying": underlying, "error": str(exc)})
        return None
