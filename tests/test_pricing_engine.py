"""Tests for adaptive limit pricing."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from execution.models import Quote, Side
from execution.pricing import initial_limit_price, next_limit_price, round_to_tick

IST = ZoneInfo("Asia/Kolkata")

def _q(bid: str, ask: str) -> Quote:
    return Quote(
        instrument_token=1, trading_symbol="TEST", exchange="NFO",
        timestamp=datetime.now(IST),
        ltp=Decimal(bid), bid=Decimal(bid), bid_qty=100,
        ask=Decimal(ask), ask_qty=100,
    )


class TestRoundToTick:
    def test_rounds_down_to_nearest_tick(self) -> None:
        assert round_to_tick(Decimal("10.07")) == Decimal("10.05")

    def test_exact_tick_unchanged(self) -> None:
        assert round_to_tick(Decimal("10.10")) == Decimal("10.10")


class TestInitialLimitPrice:
    def test_buy_starts_at_or_below_midpoint(self) -> None:
        q = _q("10.00", "11.00")  # midpoint = 10.50
        limit = initial_limit_price(q, Side.BUY)
        assert limit <= q.midpoint
        assert limit >= q.bid

    def test_sell_starts_at_or_above_midpoint(self) -> None:
        q = _q("10.00", "11.00")  # midpoint = 10.50
        limit = initial_limit_price(q, Side.SELL)
        assert limit >= q.midpoint
        assert limit <= q.ask


class TestNextLimitPrice:
    def test_buy_moves_toward_ask(self) -> None:
        q = _q("10.00", "11.00")
        current = Decimal("10.25")
        next_p = next_limit_price(current, q, Side.BUY, step=Decimal("0.05"))
        assert next_p > current

    def test_buy_never_exceeds_ask(self) -> None:
        q = _q("10.00", "10.10")
        current = Decimal("10.05")
        next_p = next_limit_price(current, q, Side.BUY, step=Decimal("1.00"))
        assert next_p <= q.ask

    def test_sell_moves_toward_bid(self) -> None:
        q = _q("10.00", "11.00")
        current = Decimal("10.75")
        next_p = next_limit_price(current, q, Side.SELL, step=Decimal("0.05"))
        assert next_p < current

    def test_sell_never_goes_below_bid(self) -> None:
        q = _q("10.00", "11.00")
        current = Decimal("10.05")
        next_p = next_limit_price(current, q, Side.SELL, step=Decimal("5.00"))
        assert next_p >= q.bid
