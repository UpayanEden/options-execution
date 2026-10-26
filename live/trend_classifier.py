"""
Trend classifier for shift distance adjustment.

classify_trend(symbol, broker, config) -> "UP" | "DOWN" | "NEUTRAL"

Uses:
- Signal A: daily EMA20 structure (60 daily candles on near-month futures)
- Signal B: intraday EMA9/EMA21 on 15-min candles (last 10 sessions)
- Signal C: last price vs today's VWAP (from 5-min futures candles)
- Gate S: ADX(14) on 15-min candles >= trend_adx_min

Data comes from kite.historical_data() on the near-month futures contract.
Candles cached 60 seconds per symbol.
Throttle: max 3 requests/second (use time.sleep between calls).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Module-level candle cache: cache_key → (timestamp, candles)
_candle_cache: Dict[str, Tuple[float, List[dict]]] = {}
_CACHE_TTL = 60.0  # seconds

# Rate limiter: track time of last historical_data call
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 0.34  # seconds (≤ 3 req/s)


def _throttle() -> None:
    """Sleep if necessary to maintain <= 3 requests/second."""
    global _last_request_time
    now = time.time()
    gap = now - _last_request_time
    if gap < _MIN_REQUEST_GAP:
        time.sleep(_MIN_REQUEST_GAP - gap)
    _last_request_time = time.time()


def find_futures_symbol(symbol: str, broker: Any) -> Optional[str]:
    """
    Scan instrument master for FUT contracts for symbol.

    Args:
        symbol: NSE underlying symbol (e.g. "RELIANCE").
        broker: BrokerAdapter instance with _load_instruments().

    Returns:
        Trading symbol of the nearest-expiry futures contract, or None.
    """
    try:
        instruments = broker._load_instruments() if hasattr(broker, "_load_instruments") else []
    except Exception as exc:
        logger.warning("find_futures_symbol_load_failed", extra={"error": str(exc)})
        return None

    today = date.today()
    candidates: List[Tuple[date, str]] = []
    for inst in instruments:
        if str(inst.get("name", "")).upper() != symbol.upper():
            continue
        if inst.get("instrument_type", "") != "FUT":
            continue
        raw_exp = inst.get("expiry")
        if isinstance(raw_exp, str):
            try:
                raw_exp = date.fromisoformat(raw_exp)
            except ValueError:
                continue
        elif hasattr(raw_exp, "date"):
            raw_exp = raw_exp.date()
        if isinstance(raw_exp, date) and raw_exp >= today:
            ts = str(inst.get("tradingsymbol", ""))
            if ts:
                candidates.append((raw_exp, ts))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _get_futures_token(symbol: str, broker: Any) -> Optional[int]:
    """Return instrument token for the near-month futures contract."""
    try:
        instruments = broker._load_instruments() if hasattr(broker, "_load_instruments") else []
    except Exception:
        return None

    today = date.today()
    best_expiry: Optional[date] = None
    best_token: Optional[int] = None

    for inst in instruments:
        if str(inst.get("name", "")).upper() != symbol.upper():
            continue
        if inst.get("instrument_type", "") != "FUT":
            continue
        raw_exp = inst.get("expiry")
        if isinstance(raw_exp, str):
            try:
                raw_exp = date.fromisoformat(raw_exp)
            except ValueError:
                continue
        elif hasattr(raw_exp, "date"):
            raw_exp = raw_exp.date()
        if isinstance(raw_exp, date) and raw_exp >= today:
            if best_expiry is None or raw_exp < best_expiry:
                best_expiry = raw_exp
                best_token = int(inst.get("instrument_token", 0))

    return best_token


def get_candles(
    symbol: str,
    broker: Any,
    interval: str,
    from_date: date,
    to_date: date,
) -> List[dict]:
    """
    Fetch OHLCV candles for the near-month futures instrument via kite.historical_data().

    Args:
        symbol:    NSE symbol.
        broker:    BrokerAdapter with kite.historical_data access.
        interval:  Kite interval string: "day", "15minute", "5minute", etc.
        from_date: Start date for the candle range.
        to_date:   End date for the candle range.

    Returns:
        List of dicts with keys: date, open, high, low, close, volume.
        Returns [] on failure.
    """
    cache_key = f"{symbol}:{interval}:{from_date}:{to_date}"
    now = time.time()
    if cache_key in _candle_cache:
        cached_ts, cached_data = _candle_cache[cache_key]
        if now - cached_ts < _CACHE_TTL:
            return cached_data

    token = _get_futures_token(symbol, broker)
    if token is None:
        logger.warning("get_candles_no_futures_token", extra={"symbol": symbol})
        return []

    _throttle()
    try:
        kite = broker._kite if hasattr(broker, "_kite") else None  # type: ignore[attr-defined]
        if kite is None:
            return []
        raw = kite.historical_data(
            instrument_token=token,
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            interval=interval,
        )
        candles: List[dict] = []
        for r in raw:
            candles.append({
                "date": r.get("date", r[0]) if isinstance(r, dict) else r[0],
                "open": float(r.get("open", r[1]) if isinstance(r, dict) else r[1]),
                "high": float(r.get("high", r[2]) if isinstance(r, dict) else r[2]),
                "low": float(r.get("low", r[3]) if isinstance(r, dict) else r[3]),
                "close": float(r.get("close", r[4]) if isinstance(r, dict) else r[4]),
                "volume": int(r.get("volume", r[5]) if isinstance(r, dict) else r[5]),
            })
        _candle_cache[cache_key] = (now, candles)
        return candles
    except Exception as exc:
        logger.warning("get_candles_failed", extra={"symbol": symbol, "interval": interval, "error": str(exc)})
        return []


def compute_ema(closes: List[float], period: int) -> List[float]:
    """
    Compute Exponential Moving Average.

    First value is SMA of first `period` closes; subsequent values use EMA formula.
    Returns list same length as input; first (period-1) values are 0.0.

    Args:
        closes: List of closing prices.
        period: EMA period.

    Returns:
        List of EMA values (0.0 for first period-1 elements).
    """
    if len(closes) < period:
        return [0.0] * len(closes)

    result = [0.0] * len(closes)
    # First EMA = SMA of first period values
    sma = sum(closes[:period]) / period
    result[period - 1] = sma

    multiplier = 2.0 / (period + 1)
    for i in range(period, len(closes)):
        result[i] = (closes[i] - result[i - 1]) * multiplier + result[i - 1]

    return result


def compute_adx(candles: List[dict], period: int = 14) -> Optional[float]:
    """
    Compute ADX(period) from OHLC candles.

    Standard Wilder ADX: +DM, -DM, TR → ATR → +DI, -DI → DX → smoothed ADX.

    Args:
        candles: List of OHLCV dicts with keys: high, low, close.
        period:  ADX smoothing period (default 14).

    Returns:
        ADX value as float, or None if insufficient data.
    """
    if len(candles) < period * 2:
        return None

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]

    n = len(candles)
    plus_dm: List[float] = []
    minus_dm: List[float] = []
    tr_list: List[float] = []

    for i in range(1, n):
        h_diff = highs[i] - highs[i - 1]
        l_diff = lows[i - 1] - lows[i]

        pdm = h_diff if h_diff > l_diff and h_diff > 0 else 0.0
        mdm = l_diff if l_diff > h_diff and l_diff > 0 else 0.0
        plus_dm.append(pdm)
        minus_dm.append(mdm)

        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)

    def _smooth(values: List[float], p: int) -> List[float]:
        result = [0.0] * len(values)
        result[p - 1] = sum(values[:p])
        for i in range(p, len(values)):
            result[i] = result[i - 1] - result[i - 1] / p + values[i]
        return result

    atr = _smooth(tr_list, period)
    spdm = _smooth(plus_dm, period)
    smdm = _smooth(minus_dm, period)

    dx_list: List[float] = []
    for i in range(period - 1, len(tr_list)):
        if atr[i] == 0:
            continue
        pdi = 100.0 * spdm[i] / atr[i]
        mdi = 100.0 * smdm[i] / atr[i]
        dsum = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / dsum if dsum != 0 else 0.0
        dx_list.append(dx)

    if len(dx_list) < period:
        return None

    # Smooth DX to get ADX
    adx = sum(dx_list[:period]) / period
    for i in range(period, len(dx_list)):
        adx = (adx * (period - 1) + dx_list[i]) / period

    return round(adx, 2)


def compute_vwap(candles: List[dict]) -> Optional[float]:
    """
    Compute VWAP from intraday candles (today's candles only).

    VWAP = sum(typical_price * volume) / sum(volume)
    typical_price = (high + low + close) / 3

    Args:
        candles: List of OHLCV dicts. Only candles from today are used.

    Returns:
        VWAP value, or None if no candles or zero volume.
    """
    today = date.today()
    today_candles = []
    for c in candles:
        cdate = c.get("date")
        if cdate is None:
            continue
        if hasattr(cdate, "date"):
            cdate = cdate.date()
        elif isinstance(cdate, str):
            try:
                cdate = date.fromisoformat(cdate[:10])
            except ValueError:
                continue
        if cdate == today:
            today_candles.append(c)

    if not today_candles:
        return None

    total_tp_vol = 0.0
    total_vol = 0.0
    for c in today_candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3.0
        vol = float(c["volume"])
        total_tp_vol += tp * vol
        total_vol += vol

    if total_vol == 0:
        return None

    return total_tp_vol / total_vol


def classify_trend(symbol: str, broker: Any, config: Any) -> str:
    """
    Classify the current trend for symbol as "UP", "DOWN", or "NEUTRAL".

    Gate S: ADX(14) on 15-min candles must be >= config.trend_adx_min.
    Signal A: Daily EMA20 of futures closes. Bullish if close > EMA20 AND EMA20[-1] > EMA20[-6].
    Signal B: 15-min EMA9 vs EMA21. Bullish if EMA9 > EMA21.
    Signal C: Last close vs today's VWAP on 5-min candles.

    Before 09:30 IST → NEUTRAL (VWAP not meaningful).
    Missing data → NEUTRAL.
    UP if gate passes and all three signals bullish.
    DOWN if gate passes and all bearish.
    NEUTRAL otherwise.

    Args:
        symbol: NSE underlying symbol.
        broker: BrokerAdapter instance.
        config: StrategyParams with trend_adx_min and trend_dist_boost fields.

    Returns:
        "UP", "DOWN", or "NEUTRAL".
    """
    now_ist = datetime.now(IST)
    # Before 09:30 IST → NEUTRAL (VWAP not meaningful)
    if now_ist.hour < 9 or (now_ist.hour == 9 and now_ist.minute < 30):
        return "NEUTRAL"

    trend_adx_min = getattr(config, "trend_adx_min", 20.0)
    today = date.today()

    # ── Signal A: daily EMA20 (60 daily candles) ──────────────────────────
    daily_from = today - timedelta(days=90)
    daily_candles = get_candles(symbol, broker, "day", daily_from, today)
    if len(daily_candles) < 25:
        return "NEUTRAL"
    daily_closes = [c["close"] for c in daily_candles]
    daily_ema20 = compute_ema(daily_closes, 20)
    # Last valid EMA value
    last_ema20 = daily_ema20[-1] if daily_ema20[-1] != 0.0 else None
    prev_ema20 = daily_ema20[-6] if len(daily_ema20) >= 6 and daily_ema20[-6] != 0.0 else None
    last_daily_close = daily_closes[-1]

    if last_ema20 is None or prev_ema20 is None:
        return "NEUTRAL"

    signal_a_bull = last_daily_close > last_ema20 and last_ema20 > prev_ema20
    signal_a_bear = last_daily_close < last_ema20 and last_ema20 < prev_ema20

    # ── Signal B: 15-min EMA9 vs EMA21 (last 10 sessions ≈ 10 * 25 candles) ──
    intraday_from = today - timedelta(days=14)
    candles_15m = get_candles(symbol, broker, "15minute", intraday_from, today)
    if len(candles_15m) < 25:
        return "NEUTRAL"
    closes_15m = [c["close"] for c in candles_15m]
    ema9 = compute_ema(closes_15m, 9)
    ema21 = compute_ema(closes_15m, 21)
    last_ema9 = ema9[-1] if ema9[-1] != 0.0 else None
    last_ema21 = ema21[-1] if ema21[-1] != 0.0 else None

    if last_ema9 is None or last_ema21 is None:
        return "NEUTRAL"

    signal_b_bull = last_ema9 > last_ema21
    signal_b_bear = last_ema9 < last_ema21

    # ── Gate S: ADX on 15-min candles ─────────────────────────────────────
    adx = compute_adx(candles_15m, 14)
    if adx is None or adx < trend_adx_min:
        return "NEUTRAL"

    # ── Signal C: last close vs VWAP on 5-min candles ─────────────────────
    candles_5m = get_candles(symbol, broker, "5minute", today, today)
    if not candles_5m:
        return "NEUTRAL"
    vwap = compute_vwap(candles_5m)
    last_5m_close = candles_5m[-1]["close"] if candles_5m else None

    if vwap is None or last_5m_close is None:
        return "NEUTRAL"

    signal_c_bull = last_5m_close > vwap
    signal_c_bear = last_5m_close < vwap

    # ── Final classification ───────────────────────────────────────────────
    if signal_a_bull and signal_b_bull and signal_c_bull:
        return "UP"
    if signal_a_bear and signal_b_bear and signal_c_bear:
        return "DOWN"
    return "NEUTRAL"
