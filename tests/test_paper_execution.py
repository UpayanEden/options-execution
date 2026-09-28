"""Tests for paper broker fill simulation."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from brokers.paper import PaperBroker
from execution.models import (
    Order,
    OrderStatus,
    PaperFillMode,
    Quote,
    Side,
)

IST = ZoneInfo("Asia/Kolkata")


def _order(side: Side, limit: str, qty: int = 100) -> Order:
    return Order(
        strategy_run_id="test-run-1",
        leg_index=0,
        trading_symbol="OBEROIRLTY26AUG1700PE",
        instrument_token=1001,
        side=side,
        quantity=qty,
        limit_price=Decimal(limit),
    )


def _quote(bid: str, ask: str) -> Quote:
    return Quote(
        instrument_token=1001,
        trading_symbol="OBEROIRLTY26AUG1700PE",
        exchange="NFO",
        timestamp=datetime.now(IST),
        ltp=Decimal(bid),
        bid=Decimal(bid),
        bid_qty=100,
        ask=Decimal(ask),
        ask_qty=100,
    )


class TestPaperBrokerConservativeMode:
    """BUY fills at or above ask. SELL fills at or below bid."""

    def setup_method(self) -> None:
        source = MagicMock()
        self.broker = PaperBroker(
            source, PaperFillMode.CONSERVATIVE, fill_delay_seconds=0,
            slippage_per_order=Decimal("0")
        )

    def test_buy_fills_when_limit_at_ask(self) -> None:
        order = _order(Side.BUY, "10.00")
        quote = _quote("9.50", "10.00")
        updated, fill = self.broker.simulate_order(order, quote)
        assert fill is not None
        assert updated.status == OrderStatus.COMPLETE

    def test_buy_does_not_fill_when_limit_below_ask(self) -> None:
        order = _order(Side.BUY, "9.00")
        quote = _quote("9.50", "10.00")
        updated, fill = self.broker.simulate_order(order, quote)
        assert fill is None
        assert updated.status == OrderStatus.PENDING

    def test_sell_fills_when_limit_at_bid(self) -> None:
        order = _order(Side.SELL, "9.50")
        quote = _quote("9.50", "10.00")
        updated, fill = self.broker.simulate_order(order, quote)
        assert fill is not None
        assert updated.status == OrderStatus.COMPLETE

    def test_sell_does_not_fill_when_limit_above_bid(self) -> None:
        order = _order(Side.SELL, "10.50")
        quote = _quote("9.50", "10.00")
        updated, fill = self.broker.simulate_order(order, quote)
        assert fill is None

    def test_fill_records_correct_quantity(self) -> None:
        order = _order(Side.BUY, "10.00", qty=500)
        quote = _quote("9.50", "10.00")
        updated, fill = self.broker.simulate_order(order, quote)
        assert fill is not None
        assert fill.quantity == 500
        assert updated.filled_quantity == 500
        assert updated.remaining_quantity == 0


class TestPaperBrokerMidpointMode:
    def setup_method(self) -> None:
        source = MagicMock()
        self.broker = PaperBroker(
            source, PaperFillMode.MIDPOINT, fill_delay_seconds=0,
            slippage_per_order=Decimal("0")
        )

    def test_buy_fills_at_midpoint(self) -> None:
        order = _order(Side.BUY, "9.75")  # midpoint of 9.50/10.00 = 9.75
        quote = _quote("9.50", "10.00")
        updated, fill = self.broker.simulate_order(order, quote)
        assert fill is not None

    def test_buy_does_not_fill_below_midpoint(self) -> None:
        order = _order(Side.BUY, "9.50")  # below midpoint
        quote = _quote("9.50", "10.00")
        updated, fill = self.broker.simulate_order(order, quote)
        assert fill is None
