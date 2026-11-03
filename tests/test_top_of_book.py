"""
Tests for TopOfBook dataclass in execution/models.py.

Covers:
  1. from_quote() builds correct TopOfBook from depth data.
  2. from_quote() falls back to bid/ask when no depth.
  3. Validation: one-sided, crossed, wide spread, thin qty, old age.
  4. limit_price(): SELL and BUY, narrow spread (no improvement), wide spread (+/- 1 tick).
  5. to_snapshot() round-trips correctly.
  6. spread_pct is a decimal ratio (not percentage).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from execution.models import DepthLevel, Quote, Side, TopOfBook

IST = ZoneInfo("Asia/Kolkata")


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_quote(
    token: int = 1,
    symbol: str = "TEST26NOV100CE",
    bid: float = 5.0,
    ask: float = 5.5,
    bid_qty: int = 500,
    ask_qty: int = 500,
    *,
    with_depth: bool = True,
    received_age: float = 0.5,
) -> Quote:
    recv = time.monotonic() - received_age
    depth_bids = (
        [DepthLevel(price=Decimal(str(bid)), quantity=bid_qty, orders=5)]
        if with_depth
        else []
    )
    depth_asks = (
        [DepthLevel(price=Decimal(str(ask)), quantity=ask_qty, orders=5)]
        if with_depth
        else []
    )
    return Quote(
        instrument_token=token,
        trading_symbol=symbol,
        exchange="NFO",
        timestamp=datetime.now(IST),
        ltp=Decimal(str(bid)),
        bid=Decimal(str(bid)),
        bid_qty=bid_qty,
        ask=Decimal(str(ask)),
        ask_qty=ask_qty,
        received_at=recv,
        depth_bids=depth_bids,
        depth_asks=depth_asks,
    )


# ── 1. from_quote with depth ───────────────────────────────────────────────────


class TestFromQuoteWithDepth:
    def test_bid_ask_from_depth_level_0(self) -> None:
        q = _make_quote(bid=5.0, ask=5.5, bid_qty=200, ask_qty=300, with_depth=True)
        tob = TopOfBook.from_quote(q)
        assert tob.bid == Decimal("5.0")
        assert tob.ask == Decimal("5.5")
        assert tob.bid_qty == 200
        assert tob.ask_qty == 300
        assert tob.has_depth is True

    def test_received_at_propagated(self) -> None:
        q = _make_quote(received_age=1.0)
        tob = TopOfBook.from_quote(q)
        assert tob.received_at is not None
        age = tob.age_seconds()
        assert 0.9 <= age <= 3.0


# ── 2. from_quote without depth ───────────────────────────────────────────────


class TestFromQuoteWithoutDepth:
    def test_falls_back_to_bid_ask_fields(self) -> None:
        q = _make_quote(bid=4.0, ask=4.5, bid_qty=100, ask_qty=150, with_depth=False)
        tob = TopOfBook.from_quote(q)
        assert tob.bid == Decimal("4.0")
        assert tob.ask == Decimal("4.5")
        assert tob.bid_qty == 100
        assert tob.has_depth is False


# ── 3. Validation ──────────────────────────────────────────────────────────────


class TestValidation:
    def test_valid_book_passes(self) -> None:
        q = _make_quote(bid=5.0, ask=5.5, received_age=0.5)
        tob = TopOfBook.from_quote(q)
        assert tob.validate() == []

    def test_one_sided_bid_zero_fails(self) -> None:
        q = _make_quote(bid=0.0, ask=5.5, with_depth=False)
        tob = TopOfBook.from_quote(q)
        failures = tob.validate()
        assert any("one-sided" in f for f in failures)

    def test_one_sided_ask_zero_fails(self) -> None:
        q = _make_quote(bid=5.0, ask=0.0, with_depth=False)
        tob = TopOfBook.from_quote(q)
        failures = tob.validate()
        assert any("one-sided" in f for f in failures)

    def test_crossed_book_fails(self) -> None:
        # bid > ask
        tob = TopOfBook(
            trading_symbol="TEST",
            bid=Decimal("5.5"),
            bid_qty=100,
            ask=Decimal("5.0"),
            ask_qty=100,
            has_depth=True,
            received_at=time.monotonic() - 0.1,
        )
        failures = tob.validate()
        assert any("crossed" in f for f in failures)

    def test_wide_spread_fails(self) -> None:
        # spread = 3 / mid = 7.5 → 40% → > 0.15
        tob = TopOfBook(
            trading_symbol="TEST",
            bid=Decimal("6.0"),
            bid_qty=100,
            ask=Decimal("9.0"),
            ask_qty=100,
            has_depth=True,
            received_at=time.monotonic() - 0.1,
        )
        failures = tob.validate(max_spread_pct=0.15)
        assert any("spread" in f for f in failures)

    def test_narrow_spread_passes(self) -> None:
        # spread = 0.5 / mid = 5.25 → 9.5% < 15%
        q = _make_quote(bid=5.0, ask=5.5, received_age=0.1)
        tob = TopOfBook.from_quote(q)
        failures = tob.validate(max_spread_pct=0.15)
        assert failures == []

    def test_thin_bid_qty_fails(self) -> None:
        tob = TopOfBook(
            trading_symbol="TEST",
            bid=Decimal("5.0"),
            bid_qty=0,
            ask=Decimal("5.5"),
            ask_qty=100,
            has_depth=True,
            received_at=time.monotonic() - 0.1,
        )
        failures = tob.validate(min_qty=1)
        assert any("bid_qty" in f for f in failures)

    def test_stale_age_fails(self) -> None:
        tob = TopOfBook(
            trading_symbol="TEST",
            bid=Decimal("5.0"),
            bid_qty=100,
            ask=Decimal("5.5"),
            ask_qty=100,
            has_depth=True,
            received_at=time.monotonic() - 20.0,  # 20 s ago
        )
        failures = tob.validate(max_age_s=10.0)
        assert any("age" in f for f in failures)

    def test_no_depth_passes_without_hard_failure(self) -> None:
        """
        No depth (LTP used) is NOT a hard failure — spread/qty checks are skipped
        but entry is allowed.  The caller can inspect tob.has_depth separately.
        """
        q = _make_quote(bid=5.0, ask=5.5, with_depth=False, received_age=0.1)
        tob = TopOfBook.from_quote(q)
        assert not tob.has_depth
        failures = tob.validate()
        # No hard failures — entry should proceed (conservatively priced)
        assert failures == [], f"Unexpected failures: {failures}"

    def test_no_received_at_skips_age_check(self) -> None:
        """PaperBroker quotes without received_at should not fail the age check."""
        q = _make_quote(bid=5.0, ask=5.5, with_depth=True)
        tob = TopOfBook.from_quote(q)
        tob = tob.model_copy(update={"received_at": None})
        failures = tob.validate(max_age_s=0.001)  # ultra-tight threshold
        assert not any("age" in f for f in failures)


# ── 4. limit_price ─────────────────────────────────────────────────────────────


class TestLimitPrice:
    """
    narrow spread (≤ 3 ticks): no improvement, use exact bid/ask.
    wide spread (> 3 ticks):   SELL gets bid+1 tick, BUY gets ask-1 tick.
    """

    TICK = Decimal("0.05")

    def test_sell_narrow_spread_uses_bid(self) -> None:
        # spread = 0.10 = 2 ticks of 0.05 → narrow → exact bid
        tob = TopOfBook(
            trading_symbol="T", bid=Decimal("5.00"), bid_qty=100,
            ask=Decimal("5.10"), ask_qty=100, has_depth=True,
            received_at=time.monotonic() - 0.1,
        )
        p = tob.limit_price(Side.SELL, self.TICK)
        assert p == Decimal("5.00")

    def test_buy_narrow_spread_uses_ask(self) -> None:
        tob = TopOfBook(
            trading_symbol="T", bid=Decimal("5.00"), bid_qty=100,
            ask=Decimal("5.10"), ask_qty=100, has_depth=True,
            received_at=time.monotonic() - 0.1,
        )
        p = tob.limit_price(Side.BUY, self.TICK)
        assert p == Decimal("5.10")

    def test_sell_wide_spread_improves_one_tick(self) -> None:
        # spread = 5.0 - 4.0 = 1.0 > 3*0.05=0.15 → improve bid → 4.05
        tob = TopOfBook(
            trading_symbol="T", bid=Decimal("4.00"), bid_qty=100,
            ask=Decimal("5.00"), ask_qty=100, has_depth=True,
            received_at=time.monotonic() - 0.1,
        )
        p = tob.limit_price(Side.SELL, self.TICK)
        assert p == Decimal("4.05")

    def test_buy_wide_spread_improves_one_tick(self) -> None:
        # ask - 1 tick = 5.0 - 0.05 = 4.95
        tob = TopOfBook(
            trading_symbol="T", bid=Decimal("4.00"), bid_qty=100,
            ask=Decimal("5.00"), ask_qty=100, has_depth=True,
            received_at=time.monotonic() - 0.1,
        )
        p = tob.limit_price(Side.BUY, self.TICK)
        assert p == Decimal("4.95")

    def test_sell_improvement_stays_below_ask(self) -> None:
        # If bid+1tick would equal ask, should NOT improve (would be crossed)
        tob = TopOfBook(
            trading_symbol="T", bid=Decimal("4.95"), bid_qty=100,
            ask=Decimal("5.00"), ask_qty=100, has_depth=True,
            received_at=time.monotonic() - 0.1,
        )
        p = tob.limit_price(Side.SELL, self.TICK)
        # spread = 0.05 = exactly 1 tick, ≤ 3 ticks → no improvement
        assert p == Decimal("4.95")

    def test_rounds_to_tick(self) -> None:
        # price 4.03 should round to nearest 0.05 = 4.05
        tob = TopOfBook(
            trading_symbol="T", bid=Decimal("4.03"), bid_qty=100,
            ask=Decimal("4.53"), ask_qty=100, has_depth=True,
            received_at=time.monotonic() - 0.1,
        )
        p = tob.limit_price(Side.SELL, Decimal("0.05"))
        assert p % Decimal("0.05") == 0


# ── 5. to_snapshot ─────────────────────────────────────────────────────────────


class TestToSnapshot:
    def test_keys_present(self) -> None:
        q = _make_quote()
        tob = TopOfBook.from_quote(q)
        snap = tob.to_snapshot()
        assert "symbol" in snap
        assert "bid" in snap
        assert "ask" in snap
        assert "spread_pct" in snap
        assert "has_depth" in snap
        assert isinstance(snap["bid"], float)
        assert isinstance(snap["spread_pct"], float)

    def test_spread_pct_is_ratio(self) -> None:
        # bid=5, ask=5.5 → spread=0.5, mid=5.25 → ratio=0.095...
        q = _make_quote(bid=5.0, ask=5.5)
        tob = TopOfBook.from_quote(q)
        snap = tob.to_snapshot()
        assert snap["spread_pct"] < 1.0, "spread_pct should be < 1.0 (ratio, not %)"


# ── 6. spread_pct is a decimal ratio ──────────────────────────────────────────


class TestSpreadPct:
    def test_ratio_not_percentage(self) -> None:
        # bid=5, ask=5.5 → spread=0.5, mid=5.25 → 0.5/5.25 ≈ 0.0952
        tob = TopOfBook(
            trading_symbol="T", bid=Decimal("5.00"), bid_qty=100,
            ask=Decimal("5.50"), ask_qty=100, has_depth=True,
        )
        assert Decimal("0.09") < tob.spread_pct < Decimal("0.11")

    def test_zero_mid_returns_sentinel(self) -> None:
        tob = TopOfBook(
            trading_symbol="T", bid=Decimal("0"), bid_qty=0,
            ask=Decimal("0"), ask_qty=0, has_depth=False,
        )
        assert tob.spread_pct == Decimal("999")
