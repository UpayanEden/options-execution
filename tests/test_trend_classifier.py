"""
Tests for live/trend_classifier.py

Uses pytest with mocked broker candle data.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from live.trend_classifier import (
    classify_trend,
    compute_adx,
    compute_ema,
    compute_vwap,
    get_candles,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_candles(n: int, close_start: float = 100.0, trend: str = "flat") -> List[dict]:
    """Generate synthetic OHLCV candles."""
    candles = []
    today = date.today()
    for i in range(n):
        if trend == "up":
            close = close_start + i * 0.5
        elif trend == "down":
            close = close_start - i * 0.5
        else:
            close = close_start + (i % 3 - 1) * 0.1
        high = close + 0.5
        low = close - 0.5
        candles.append({
            "date": today,
            "open": close - 0.1,
            "high": high,
            "low": low,
            "close": close,
            "volume": 10000,
        })
    return candles


def _make_strategy_params(adx_min: float = 20.0, dist_boost: float = 0.10) -> Any:
    params = MagicMock()
    params.trend_adx_min = adx_min
    params.trend_dist_boost = dist_boost
    return params


# ── Tests ──────────────────────────────────────────────────────────────────


class TestComputeEma:
    def test_compute_ema_basic(self):
        """EMA9 first valid value should equal SMA of first 9 values."""
        closes = [float(i + 1) for i in range(20)]
        ema = compute_ema(closes, 9)
        # First EMA at index 8 = SMA of closes[0:9]
        expected_sma = sum(closes[:9]) / 9
        assert abs(ema[8] - expected_sma) < 0.001

    def test_compute_ema_insufficient_data(self):
        """Returns zeros when data is shorter than period."""
        result = compute_ema([1.0, 2.0, 3.0], 9)
        assert all(v == 0.0 for v in result)

    def test_compute_ema_length_preserved(self):
        """Output length equals input length."""
        closes = [float(i) for i in range(30)]
        ema = compute_ema(closes, 9)
        assert len(ema) == 30

    def test_compute_ema_trending_up(self):
        """EMA should be below close in a sustained uptrend."""
        closes = [float(i) for i in range(1, 51)]
        ema = compute_ema(closes, 9)
        # In an uptrend, the latest EMA should be below the latest close
        assert ema[-1] < closes[-1]


class TestComputeAdx:
    def test_compute_adx_basic(self):
        """ADX on trending candles should be > 0."""
        candles = _make_candles(60, trend="up")
        adx = compute_adx(candles, 14)
        assert adx is not None
        assert adx > 0

    def test_compute_adx_insufficient_data(self):
        """Returns None if not enough candles."""
        candles = _make_candles(10)
        adx = compute_adx(candles, 14)
        assert adx is None

    def test_compute_adx_returns_float(self):
        """ADX returns a float value."""
        candles = _make_candles(80, trend="up")
        adx = compute_adx(candles, 14)
        assert isinstance(adx, float)


class TestComputeVwap:
    def test_compute_vwap_basic(self):
        """VWAP on today's candles should be between low and high."""
        candles = _make_candles(20)
        vwap = compute_vwap(candles)
        if vwap is not None:
            min_low = min(c["low"] for c in candles)
            max_high = max(c["high"] for c in candles)
            assert min_low <= vwap <= max_high

    def test_compute_vwap_no_candles(self):
        """Returns None when candle list is empty."""
        assert compute_vwap([]) is None

    def test_compute_vwap_zero_volume(self):
        """Returns None when all volumes are zero."""
        candles = _make_candles(10)
        for c in candles:
            c["volume"] = 0
        assert compute_vwap(candles) is None

    def test_compute_vwap_different_dates_filtered(self):
        """Only uses today's candles."""
        today = date.today()
        yesterday = today - timedelta(days=1)
        candles = [
            {"date": yesterday, "high": 200.0, "low": 190.0, "close": 195.0, "volume": 1000},
            {"date": today, "high": 110.0, "low": 90.0, "close": 100.0, "volume": 500},
        ]
        vwap = compute_vwap(candles)
        # Should only use today's candle: tp = (110+90+100)/3 = 100
        assert vwap is not None
        assert abs(vwap - 100.0) < 0.01


class TestClassifyTrend:
    """Tests for the full classify_trend() function using mocked candles."""

    def _make_broker(self) -> MagicMock:
        broker = MagicMock()
        broker._kite = MagicMock()
        # Return empty instruments so find_futures_token uses the mock path
        broker._load_instruments.return_value = []
        return broker

    @patch("live.trend_classifier.get_candles")
    @patch("live.trend_classifier.compute_adx")
    def test_all_bullish_adx_above_threshold(self, mock_adx, mock_get_candles):
        """All signals bullish + ADX=25 > 20 → UP."""
        today = date.today()
        # Build daily candles with strong uptrend
        daily = _make_candles(60, close_start=100.0, trend="up")
        # Make 15-min candles with EMA9 > EMA21 (strong up trend)
        intraday_15m = _make_candles(300, close_start=150.0, trend="up")
        # Make 5-min candles with last close > VWAP
        # Use today's date for VWAP candles
        intraday_5m = []
        for i in range(80):
            close = 160.0 + i * 0.1
            intraday_5m.append({
                "date": today,
                "open": close - 0.05,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 5000,
            })

        mock_adx.return_value = 25.0  # above threshold

        def side_effect(sym, broker, interval, from_dt, to_dt):
            if interval == "day":
                return daily
            elif interval == "15minute":
                return intraday_15m
            elif interval == "5minute":
                return intraday_5m
            return []

        mock_get_candles.side_effect = side_effect

        params = _make_strategy_params(adx_min=20.0)
        broker = self._make_broker()

        with patch("live.trend_classifier.datetime") as mock_dt:
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            mock_dt.now.return_value = datetime(today.year, today.month, today.day, 10, 30, 0, tzinfo=IST)
            result = classify_trend("TESTSTOCK", broker, params)

        assert result == "UP"

    @patch("live.trend_classifier.get_candles")
    @patch("live.trend_classifier.compute_adx")
    def test_all_bullish_adx_below_threshold(self, mock_adx, mock_get_candles):
        """All signals bullish but ADX=15 < 20 → NEUTRAL (gate blocks)."""
        today = date.today()
        daily = _make_candles(60, close_start=100.0, trend="up")
        intraday_15m = _make_candles(300, close_start=150.0, trend="up")
        intraday_5m = [{"date": today, "open": 165.0, "high": 166.0, "low": 164.0, "close": 165.0, "volume": 5000}]

        mock_adx.return_value = 15.0  # below threshold

        def side_effect(sym, broker, interval, from_dt, to_dt):
            if interval == "day":
                return daily
            elif interval == "15minute":
                return intraday_15m
            elif interval == "5minute":
                return intraday_5m
            return []

        mock_get_candles.side_effect = side_effect
        params = _make_strategy_params(adx_min=20.0)
        broker = self._make_broker()

        with patch("live.trend_classifier.datetime") as mock_dt:
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            mock_dt.now.return_value = datetime(today.year, today.month, today.day, 10, 30, 0, tzinfo=IST)
            result = classify_trend("TESTSTOCK", broker, params)

        assert result == "NEUTRAL"

    @patch("live.trend_classifier.get_candles")
    @patch("live.trend_classifier.compute_adx")
    def test_mixed_signals(self, mock_adx, mock_get_candles):
        """Signal A bullish but Signal B bearish → NEUTRAL."""
        today = date.today()
        # Daily: uptrend
        daily = _make_candles(60, close_start=100.0, trend="up")
        # 15-min: downtrend (EMA9 < EMA21)
        intraday_15m = _make_candles(300, close_start=200.0, trend="down")
        intraday_5m = [{"date": today, "open": 105.0, "high": 106.0, "low": 104.0, "close": 105.0, "volume": 5000}]

        mock_adx.return_value = 25.0

        def side_effect(sym, broker, interval, from_dt, to_dt):
            if interval == "day":
                return daily
            elif interval == "15minute":
                return intraday_15m
            elif interval == "5minute":
                return intraday_5m
            return []

        mock_get_candles.side_effect = side_effect
        params = _make_strategy_params(adx_min=20.0)
        broker = self._make_broker()

        with patch("live.trend_classifier.datetime") as mock_dt:
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            mock_dt.now.return_value = datetime(today.year, today.month, today.day, 10, 30, 0, tzinfo=IST)
            result = classify_trend("TESTSTOCK", broker, params)

        assert result == "NEUTRAL"

    @patch("live.trend_classifier.get_candles")
    def test_missing_data_returns_neutral(self, mock_get_candles):
        """Empty candles → NEUTRAL."""
        mock_get_candles.return_value = []
        params = _make_strategy_params()
        broker = self._make_broker()

        today = date.today()
        with patch("live.trend_classifier.datetime") as mock_dt:
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            mock_dt.now.return_value = datetime(today.year, today.month, today.day, 10, 30, 0, tzinfo=IST)
            result = classify_trend("TESTSTOCK", broker, params)

        assert result == "NEUTRAL"
