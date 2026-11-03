"""
Tests for live/quote_service.py and the received_at fix in guard_layers.py.

Key scenarios:
  1. QuoteService.get_many() returns cached quotes when fresh.
  2. QuoteService falls back to REST when cache is cold.
  3. QuoteService raises QuoteServiceError when REST returns nothing.
  4. QuoteService raises QuoteServiceError when cached quote is stale (eviction).
  5. pre_trade_refresh() forces a REST round-trip and validates freshness.
  6. MarketClosedError raised outside 09:00–15:30.
  7. TokenBucket rate limiting blocks then passes.
  8. received_at on Quote: is_stale() uses monotonic clock when set.
  9. QuoteGuard passes a quote with old exchange timestamp but fresh received_at.
  10. QuoteGuard fails a quote where received_at is genuinely old.
  11. QuoteGuard falls back to exchange timestamp when received_at is None.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from brokers.base import QuoteError
from execution.models import Quote
from live.quote_service import (
    MarketClosedError,
    QuoteService,
    QuoteServiceError,
    _TokenBucket,
)

IST = ZoneInfo("Asia/Kolkata")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_quote(
    token: int = 1,
    symbol: str = "TEST26NOV100CE",
    *,
    bid: str = "5.00",
    ask: str = "5.50",
    received_age: float = 0.5,   # seconds ago (monotonic)
    exchange_age: float = 60.0,  # seconds ago (exchange timestamp)
) -> Quote:
    """Build a Quote with controllable received_at and exchange timestamp ages."""
    return Quote(
        instrument_token=token,
        trading_symbol=symbol,
        exchange="NFO",
        timestamp=datetime.now(IST) - timedelta(seconds=exchange_age),
        ltp=Decimal(bid),
        bid=Decimal(bid),
        bid_qty=500,
        ask=Decimal(ask),
        ask_qty=500,
        received_at=time.monotonic() - received_age,
    )


def _make_service(
    quotes: dict | None = None,
    *,
    rest_error: bool = False,
    max_age: float = 8.0,
    pretrade_age: float = 3.0,
) -> QuoteService:
    broker = MagicMock()
    if rest_error:
        broker.get_quotes.side_effect = QuoteError("network timeout")
    else:
        if quotes is None:
            quotes = {}
        broker.get_quotes.return_value = quotes
    svc = QuoteService(
        broker,
        max_age_seconds=max_age,
        pretrade_age_seconds=pretrade_age,
        rest_rps=100.0,  # no rate limiting in tests
    )
    return svc


# ── 1. Cache hit ───────────────────────────────────────────────────────────────


class TestCacheHit:
    def test_returns_cached_fresh_quote(self) -> None:
        q = _make_quote(1, received_age=0.5)
        svc = _make_service()
        svc._put_cache({1: q})
        result = svc.get(1, "TEST26NOV100CE", allow_outside_hours=True)
        assert result.instrument_token == 1
        assert svc.metrics["cache_hits"] == 1
        assert svc.metrics["rest_fetches"] == 0

    def test_cache_evicts_stale_entry(self) -> None:
        # Stale: received 20 s ago, max_age=8
        q = _make_quote(1, received_age=20.0)
        svc = _make_service(quotes={1: _make_quote(1, received_age=0.1)}, max_age=8.0)
        svc._put_cache({1: q})
        # Should evict and go to REST
        result = svc.get(1, "TEST26NOV100CE", allow_outside_hours=True)
        assert result.instrument_token == 1
        assert svc.metrics["stale_evictions"] == 1
        assert svc.metrics["rest_fetches"] == 1


# ── 2. REST fallback ───────────────────────────────────────────────────────────


class TestRestFallback:
    def test_cold_cache_goes_to_rest(self) -> None:
        q = _make_quote(2, symbol="A26NOV200PE", received_age=0.1)
        svc = _make_service(quotes={2: q})
        result = svc.get(2, "A26NOV200PE", allow_outside_hours=True)
        assert result.instrument_token == 2
        assert svc.metrics["rest_fetches"] == 1

    def test_get_many_fetches_missing_tokens(self) -> None:
        q1 = _make_quote(1, received_age=0.1)
        q2 = _make_quote(2, symbol="B26NOV200CE", received_age=0.1)
        svc = _make_service(quotes={1: q1, 2: q2})
        results = svc.get_many([(1, "TEST26NOV100CE"), (2, "B26NOV200CE")],
                               allow_outside_hours=True)
        assert len(results) == 2
        assert svc.metrics["rest_fetches"] == 1

    def test_rest_stamps_received_at_if_absent(self) -> None:
        """PaperBroker quotes have no received_at — service must stamp one."""
        q_no_recv = Quote(
            instrument_token=3,
            trading_symbol="C26NOV300CE",
            exchange="NFO",
            timestamp=datetime.now(IST),
            ltp=Decimal("10"),
            bid=Decimal("10"),
            bid_qty=100,
            ask=Decimal("10.5"),
            ask_qty=100,
            received_at=None,  # PaperBroker-style: no received_at
        )
        svc = _make_service(quotes={3: q_no_recv})
        result = svc.get(3, "C26NOV300CE", allow_outside_hours=True)
        assert result.received_at is not None
        assert time.monotonic() - result.received_at < 2.0


# ── 3. Unavailable quote ───────────────────────────────────────────────────────


class TestUnavailableQuote:
    def test_raises_when_rest_returns_empty(self) -> None:
        svc = _make_service(quotes={})  # REST returns nothing
        with pytest.raises(QuoteServiceError, match="unavailable"):
            svc.get(99, "MISSING99CE", allow_outside_hours=True)
        assert svc.metrics["errors"] == 1

    def test_raises_when_rest_throws(self) -> None:
        svc = _make_service(rest_error=True)
        with pytest.raises(QuoteServiceError, match="REST quote fetch failed"):
            svc.get(99, "MISSING99CE", allow_outside_hours=True)

    def test_get_many_raises_on_partial_miss(self) -> None:
        q1 = _make_quote(1, received_age=0.1)
        svc = _make_service(quotes={1: q1})  # token 2 missing
        with pytest.raises(QuoteServiceError):
            svc.get_many([(1, "TEST26NOV100CE"), (2, "B26NOV200CE")],
                         allow_outside_hours=True)


# ── 4. pre_trade_refresh ───────────────────────────────────────────────────────


class TestPreTradeRefresh:
    def test_forces_rest_and_returns_quotes(self) -> None:
        q = _make_quote(1, received_age=0.05)
        svc = _make_service(quotes={1: q})
        result = svc.pre_trade_refresh([(1, "TEST26NOV100CE")])
        assert 1 in result
        assert svc.metrics["pretrade_refreshes"] == 1

    def test_raises_if_still_stale_after_refresh(self) -> None:
        # REST returns a quote with received_at 10 s ago; pretrade_age=3
        q_stale = _make_quote(1, received_age=10.0)
        svc = _make_service(quotes={1: q_stale}, pretrade_age=3.0)
        with pytest.raises(QuoteServiceError, match="still stale"):
            svc.pre_trade_refresh([(1, "TEST26NOV100CE")])

    def test_raises_if_quote_missing_after_refresh(self) -> None:
        svc = _make_service(quotes={})
        with pytest.raises(QuoteServiceError, match="no quote"):
            svc.pre_trade_refresh([(1, "MISSING")])


# ── 5. Market-hours gate ───────────────────────────────────────────────────────


class TestMarketHoursGate:
    def test_raises_outside_hours(self) -> None:
        svc = _make_service(quotes={1: _make_quote(1)})
        with patch("live.quote_service.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 1, 7, 0, 0, tzinfo=IST)
            with pytest.raises(MarketClosedError):
                svc.get(1, "TEST26NOV100CE")

    def test_passes_inside_hours(self) -> None:
        q = _make_quote(1, received_age=0.1)
        svc = _make_service(quotes={1: q})
        with patch("live.quote_service.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 1, 10, 30, 0, tzinfo=IST)
            svc._put_cache({1: q})
            result = svc.get(1, "TEST26NOV100CE")
        assert result.instrument_token == 1

    def test_allow_outside_hours_bypasses_gate(self) -> None:
        q = _make_quote(1, received_age=0.1)
        svc = _make_service(quotes={1: q})
        svc._put_cache({1: q})
        # Should not raise even though we're not mocking market hours
        result = svc.get(1, "TEST26NOV100CE", allow_outside_hours=True)
        assert result.instrument_token == 1


# ── 6. TokenBucket ────────────────────────────────────────────────────────────


class TestTokenBucket:
    def test_consumes_without_blocking_when_full(self) -> None:
        bucket = _TokenBucket(rate=10.0, capacity=10.0)
        start = time.monotonic()
        for _ in range(5):
            bucket.consume()
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, "Should not block when bucket is full"

    def test_blocks_when_empty(self) -> None:
        # rate=2/s, capacity=1 — second consume must wait ~0.5 s
        bucket = _TokenBucket(rate=2.0, capacity=1.0)
        bucket.consume()  # drains
        start = time.monotonic()
        bucket.consume()  # must wait
        elapsed = time.monotonic() - start
        assert elapsed >= 0.4, f"Expected ~0.5 s wait, got {elapsed:.3f} s"


# ── 7. Quote.is_stale() uses received_at ──────────────────────────────────────


class TestQuoteIsStale:
    def test_fresh_received_at_overrides_stale_exchange_ts(self) -> None:
        """A quote where exchange timestamp is 5 min old but received_at is 1 s ago."""
        q = _make_quote(1, received_age=1.0, exchange_age=300.0)
        assert not q.is_stale(staleness_seconds=10)

    def test_stale_received_at_detected(self) -> None:
        """A quote where received_at is 20 s ago."""
        q = _make_quote(1, received_age=20.0)
        assert q.is_stale(staleness_seconds=10)

    def test_fallback_to_exchange_ts_when_no_received_at(self) -> None:
        """Without received_at, is_stale() uses exchange timestamp (old behaviour)."""
        q = Quote(
            instrument_token=1,
            trading_symbol="X",
            exchange="NFO",
            timestamp=datetime.now(IST) - timedelta(seconds=300),
            ltp=Decimal("5"),
            bid=Decimal("5"),
            bid_qty=100,
            ask=Decimal("5.5"),
            ask_qty=100,
            received_at=None,
        )
        assert q.is_stale(staleness_seconds=10)

    def test_no_received_at_fresh_exchange_ts_not_stale(self) -> None:
        q = Quote(
            instrument_token=1,
            trading_symbol="X",
            exchange="NFO",
            timestamp=datetime.now(IST) - timedelta(seconds=2),
            ltp=Decimal("5"),
            bid=Decimal("5"),
            bid_qty=100,
            ask=Decimal("5.5"),
            ask_qty=100,
            received_at=None,
        )
        assert not q.is_stale(staleness_seconds=10)


# ── 8. QuoteGuard uses received_at (the root-cause fix) ─────────────────────


class TestQuoteGuardReceivedAt:
    """
    These are the critical tests for the primary stale-quote fix.

    Previously, QuoteGuard compared (now - quote.timestamp) where timestamp is
    the exchange packet timestamp.  For illiquid OTM options this can be
    5–15 min old with a live book, causing false QuoteGuard failures.

    After the fix, QuoteGuard uses received_at (monotonic) when available.
    """

    def _run_quote_guard(self, quote: Quote, symbol: str = "COAL26NOV365PE"):
        from datetime import date, timedelta
        from live.guard_layers import GuardContext, _quote_guard
        ctx = GuardContext(
            underlying="COALINDIA",
            spot=386.0,
            expiry=date.today() + timedelta(days=25),
            short_put_strike=365.0,
            short_call_strike=410.0,
            long_put_strike=355.0,
            long_call_strike=420.0,
            short_otm_pct=5.0,
            wing_width_pct=2.5,
            lots=3,
            lot_size=1350,
            quotes={symbol: quote},
            instruments={},
            pending_orders=[],
            broker_positions=[],
            available_margin=500_000.0,
            daily_pnl=0.0,
            total_allocated_margin=0.0,
            config=MagicMock(),
            expected_sides={symbol: "SELL"},
        )
        return _quote_guard(ctx)

    def test_old_exchange_ts_but_fresh_received_at_passes(self) -> None:
        """Core fix: illiquid option with 60 s exchange timestamp but fresh REST."""
        q = _make_quote(
            1, "COAL26NOV365PE",
            received_age=0.8,    # just fetched from REST
            exchange_age=60.0,   # exchange hasn't touched this strike in 60 s
        )
        result = self._run_quote_guard(q, "COAL26NOV365PE")
        assert result.passed, f"Expected pass but got: {result.reason}"

    def test_stale_received_at_fails_guard(self) -> None:
        """Quote fetched 20 s ago: should fail even if exchange ts is recent."""
        q = _make_quote(
            1, "COAL26NOV365PE",
            received_age=20.0,
            exchange_age=1.0,
        )
        result = self._run_quote_guard(q, "COAL26NOV365PE")
        assert not result.passed
        assert "age" in result.reason

    def test_no_received_at_stale_exchange_ts_fails(self) -> None:
        """Backwards compat: without received_at, old exchange ts still fails."""
        q = Quote(
            instrument_token=1,
            trading_symbol="COAL26NOV365PE",
            exchange="NFO",
            timestamp=datetime.now(IST) - timedelta(seconds=60),
            ltp=Decimal("5"),
            bid=Decimal("5"),
            bid_qty=100,
            ask=Decimal("5.5"),
            ask_qty=100,
            received_at=None,
        )
        result = self._run_quote_guard(q, "COAL26NOV365PE")
        assert not result.passed

    def test_no_received_at_fresh_exchange_ts_passes(self) -> None:
        """Backwards compat: without received_at, fresh exchange ts still passes."""
        q = Quote(
            instrument_token=1,
            trading_symbol="COAL26NOV365PE",
            exchange="NFO",
            timestamp=datetime.now(IST) - timedelta(seconds=1),
            ltp=Decimal("5"),
            bid=Decimal("5"),
            bid_qty=100,
            ask=Decimal("5.5"),
            ask_qty=100,
            received_at=None,
        )
        result = self._run_quote_guard(q, "COAL26NOV365PE")
        assert result.passed, f"Expected pass but got: {result.reason}"
