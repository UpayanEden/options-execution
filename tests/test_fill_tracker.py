"""Tests for FillTracker."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from execution.fill_tracker import FillTracker
from execution.models import Fill, Order, OrderStatus, Side

IST = ZoneInfo("Asia/Kolkata")


def _fill(leg_idx: int, price: str) -> tuple[Order, Fill]:
    order = Order(
        strategy_run_id="test", leg_index=leg_idx,
        trading_symbol=f"SYM{leg_idx}", instrument_token=leg_idx,
        side=Side.BUY if leg_idx % 2 == 0 else Side.SELL,
        quantity=100, limit_price=Decimal(price),
        status=OrderStatus.COMPLETE, filled_quantity=100,
        average_fill_price=Decimal(price),
    )
    fill = Fill(
        order_id=order.order_id, quantity=100,
        price=Decimal(price), timestamp=datetime.now(IST)
    )
    return order, fill


class TestFillTracker:
    def test_all_filled_when_all_legs_recorded(self) -> None:
        tracker = FillTracker(4)
        for i in range(4):
            order, fill = _fill(i, str(10 + i))
            tracker.record_fill(i, order, fill)
        assert tracker.all_filled

    def test_unfilled_legs_tracked(self) -> None:
        tracker = FillTracker(4)
        order, fill = _fill(0, "8.00")
        tracker.record_fill(0, order, fill)
        assert tracker.unfilled_legs == [1, 2, 3]

    def test_net_credit_calculated_correctly(self) -> None:
        # Iron condor: leg 0 BUY 8, leg 1 SELL 12, leg 2 SELL 13, leg 3 BUY 10
        tracker = FillTracker(4)
        prices = {"0": "8.00", "1": "12.00", "2": "13.00", "3": "10.00"}
        sides = {0: Side.BUY, 1: Side.SELL, 2: Side.SELL, 3: Side.BUY}
        for i in range(4):
            o = Order(
                strategy_run_id="t", leg_index=i, trading_symbol=f"S{i}",
                instrument_token=i, side=sides[i], quantity=100,
                limit_price=Decimal(prices[str(i)]),
                status=OrderStatus.COMPLETE, filled_quantity=100,
                average_fill_price=Decimal(prices[str(i)]),
            )
            f = Fill(order_id=o.order_id, quantity=100,
                     price=Decimal(prices[str(i)]), timestamp=datetime.now(IST))
            tracker.record_fill(i, o, f)
        credit = tracker.actual_net_credit(sides)
        # sell: 12 + 13 = 25; buy: 8 + 10 = 18; net = 7
        assert credit == Decimal("7.00")

    def test_net_credit_none_when_not_all_filled(self) -> None:
        tracker = FillTracker(4)
        sides = {0: Side.BUY, 1: Side.SELL, 2: Side.SELL, 3: Side.BUY}
        order, fill = _fill(0, "8.00")
        tracker.record_fill(0, order, fill)
        assert tracker.actual_net_credit(sides) is None
