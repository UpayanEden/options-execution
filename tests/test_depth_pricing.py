"""
Tests for depth-aware pricing logic.

Queue logic:
  BUY order  → sits in the BID book (depth_bids).
               Few sellers at best ASK (depth_asks) = counterparties scarce → cross to ask.
               Many sellers at best ASK                                     → wait at midpoint.

  SELL order → sits in the ASK book (depth_asks).
               Few buyers at best BID (depth_bids) = counterparties scarce → cross to bid.
               Many buyers at best BID                                      → wait at midpoint.

NOTE: in analyse_depth "our_levels" means the book OUR order goes into.
That is the BID book for BUY and the ASK book for SELL.
"is_thin" = few orders in OUR book at the best price = we'll be near the front = aggressive.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock, call
from zoneinfo import ZoneInfo

import pytest

from execution.models import DepthLevel, Order, OrderStatus, Quote, RiskConfig, Side
from execution.pricing import (
    analyse_depth,
    depth_aware_initial_price,
    initial_limit_price,
    queue_qty_at_price,
)

IST = ZoneInfo("Asia/Kolkata")
TICK = Decimal("0.05")


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_depth(prices_qtys: list[tuple[float, int, int]]) -> list[DepthLevel]:
    return [
        DepthLevel(price=Decimal(str(p)), quantity=q, orders=o)
        for p, q, o in prices_qtys
    ]


def make_quote(
    bid: float,
    ask: float,
    bid_qty: int = 100,
    ask_qty: int = 100,
    depth_bids: list[tuple] | None = None,
    depth_asks: list[tuple] | None = None,
) -> Quote:
    db = make_depth(depth_bids) if depth_bids else [
        DepthLevel(price=Decimal(str(bid)), quantity=bid_qty, orders=5)
    ]
    da = make_depth(depth_asks) if depth_asks else [
        DepthLevel(price=Decimal(str(ask)), quantity=ask_qty, orders=5)
    ]
    return Quote(
        instrument_token=1001,
        trading_symbol="TEST26AUG100PE",
        exchange="NFO",
        timestamp=datetime.now(IST),
        ltp=Decimal(str((bid + ask) / 2)),
        bid=Decimal(str(bid)),
        bid_qty=bid_qty,
        ask=Decimal(str(ask)),
        ask_qty=ask_qty,
        depth_bids=db,
        depth_asks=da,
    )


def make_risk(**overrides) -> RiskConfig:
    defaults = dict(
        capital_limit=Decimal("200000"),
        max_strategy_loss=Decimal("5000"),
        max_daily_loss=Decimal("8000"),
        min_net_credit=Decimal("8"),
        target_net_credit=Decimal("12"),
        max_bid_ask_spread_pct=Decimal("8"),
        max_total_slippage=Decimal("5"),
        quote_staleness_seconds=30,
        entry_start_time="09:20",
        entry_end_time="14:30",
        exit_start_time="09:20",
        exit_end_time="15:15",
        limit_step=Decimal("0.05"),
        order_wait_seconds=0,
        max_repricing_attempts=2,
        thin_queue_threshold=500,
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


# ── queue_qty_at_price ────────────────────────────────────────────────────────

def test_queue_qty_found():
    levels = make_depth([(30.00, 400, 3), (29.95, 200, 2), (29.90, 100, 1)])
    qty, orders = queue_qty_at_price(levels, Decimal("29.95"))
    assert qty == 200
    assert orders == 2


def test_queue_qty_not_found():
    levels = make_depth([(30.00, 400, 3)])
    qty, orders = queue_qty_at_price(levels, Decimal("29.50"))
    assert qty == 0
    assert orders == 0


def test_queue_qty_empty_levels():
    qty, orders = queue_qty_at_price([], Decimal("30.00"))
    assert qty == 0
    assert orders == 0


# ── analyse_depth ─────────────────────────────────────────────────────────────

def test_analyse_depth_sell_thin():
    """SELL order sits in ASK book. Few sellers (thin asks) → is_thin True."""
    quote = make_quote(
        bid=30.00, ask=31.00,
        depth_bids=[(30.00, 800, 5)],    # many buyers (not relevant for SELL queue)
        depth_asks=[(31.00, 200, 3)],    # thin asks — OUR side
    )
    info = analyse_depth(quote, Side.SELL, thin_threshold=500)
    assert info["is_thin"] is True
    assert info["our_best_qty"] == 200
    assert info["our_best_price"] == Decimal("31.00")  # best ask


def test_analyse_depth_sell_thick():
    """SELL order: many sellers at best ask → is_thin False."""
    quote = make_quote(
        bid=30.00, ask=31.00,
        depth_bids=[(30.00, 200, 3)],
        depth_asks=[(31.00, 1500, 10)],  # thick asks — OUR side
    )
    info = analyse_depth(quote, Side.SELL, thin_threshold=500)
    assert info["is_thin"] is False
    assert info["our_best_qty"] == 1500


def test_analyse_depth_buy_thin():
    """BUY order sits in BID book. Few buyers (thin bids) → is_thin True."""
    quote = make_quote(
        bid=30.00, ask=31.00,
        depth_bids=[(30.00, 300, 4)],    # thin bids — OUR side
        depth_asks=[(31.00, 200, 3)],
    )
    info = analyse_depth(quote, Side.BUY, thin_threshold=500)
    assert info["is_thin"] is True
    assert info["our_best_qty"] == 300
    assert info["our_best_price"] == Decimal("30.00")  # best bid


def test_analyse_depth_buy_thick():
    """BUY order: many buyers at best bid → is_thin False."""
    quote = make_quote(
        bid=30.00, ask=31.00,
        depth_bids=[(30.00, 3000, 20)],  # thick bids — OUR side
        depth_asks=[(31.00, 200, 3)],
    )
    info = analyse_depth(quote, Side.BUY, thin_threshold=500)
    assert info["is_thin"] is False
    assert info["our_best_qty"] == 3000


def test_analyse_depth_no_depth_data():
    """Empty depth → treated as thin (safe default, avoids blocking entry)."""
    quote = make_quote(bid=30.00, ask=31.00)
    quote = quote.model_copy(update={"depth_bids": [], "depth_asks": []})
    info = analyse_depth(quote, Side.SELL, thin_threshold=500)
    assert info["is_thin"] is True
    assert info["our_best_qty"] == 0


# ── depth_aware_initial_price ─────────────────────────────────────────────────

def test_sell_always_places_at_best_ask():
    """SELL always starts at best ask regardless of queue depth — top of ask book."""
    for ask_qty in [50, 200, 2000, 10000]:
        quote = make_quote(
            bid=30.00, ask=31.00,
            depth_bids=[(30.00, 800, 5)],
            depth_asks=[(31.00, ask_qty, max(1, ask_qty // 10))],
        )
        price, info = depth_aware_initial_price(quote, Side.SELL, TICK, thin_threshold=500)
        assert price == Decimal("31.00"), (
            f"SELL should start at best ask (31.00) got {price} for ask_qty={ask_qty}"
        )
        assert info["strategy"] == "top_of_queue"


def test_buy_always_places_at_best_bid():
    """BUY always starts at best bid regardless of queue depth — top of bid book."""
    for bid_qty in [50, 200, 2000, 10000]:
        quote = make_quote(
            bid=30.00, ask=31.00,
            depth_bids=[(30.00, bid_qty, max(1, bid_qty // 10))],
            depth_asks=[(31.00, 200, 3)],
        )
        price, info = depth_aware_initial_price(quote, Side.BUY, TICK, thin_threshold=500)
        assert price == Decimal("30.00"), (
            f"BUY should start at best bid (30.00) got {price} for bid_qty={bid_qty}"
        )
        assert info["strategy"] == "top_of_queue"


def test_price_rounded_to_tick():
    """Aggressive price is always snapped to a valid tick boundary."""
    quote = make_quote(
        bid=30.03, ask=31.07,
        depth_bids=[(30.03, 100, 2)],
        depth_asks=[(31.07, 100, 2)],
    )
    price, _ = depth_aware_initial_price(quote, Side.SELL, TICK, thin_threshold=500)
    remainder = price % TICK
    assert remainder == Decimal("0.00"), f"Price {price} not on tick boundary"


# ── OrderManager hold-vs-reprice (observable behaviour) ───────────────────────

def test_order_manager_holds_when_queue_thinning():
    """
    Scenario for a SELL order:
      q1  — thin asks (200 qty) → aggressive price = 31.00 (best ask), prev_qty=200
      sim0 — no fill
      q2  — asks thin further (100 qty) → thinning (100 < 200*0.7=140) → HOLD, no reprice
      sim1 — no fill (still same price)
      q3  — asks static (100 qty) → not thinning → REPRICE (new limit)
      sim2 — fill

    Observable: limit price unchanged between sim0 and sim1 (hold);
                limit price changes between sim1 and sim2 (reprice).
                3 simulate_order calls total.
    """
    from brokers.paper import PaperBroker
    from execution.models import Fill
    from execution.order_manager import OrderManager
    from risk.kill_switch import KillSwitch

    risk = make_risk()

    q1 = make_quote(bid=30.00, ask=31.00,
                    depth_bids=[(30.00, 800, 5)],
                    depth_asks=[(31.00, 200, 3)])   # thin → aggressive at 31.00, prev=200

    q2 = make_quote(bid=30.00, ask=31.00,
                    depth_bids=[(30.00, 800, 5)],
                    depth_asks=[(31.00, 100, 2)])   # thinned 200→100 → HOLD

    q3 = make_quote(bid=30.00, ask=31.20,
                    depth_bids=[(30.00, 800, 5)],
                    depth_asks=[(31.20, 100, 2)])   # static at 100 → REPRICE

    quote_seq = iter([q1, q2, q3])

    # Mock broker: captures (order, quote) pairs passed to simulate_order
    mock_broker = MagicMock(spec=PaperBroker)
    captured_orders = []

    fill_obj = Fill(
        order_id="fill-001",
        broker_fill_id="PAPER_XYZ",
        quantity=65,
        price=Decimal("30.95"),
        timestamp=datetime.now(IST),
    )

    def side_effect(order, quote):
        captured_orders.append((order.model_copy(), quote))
        call_n = len(captured_orders)
        if call_n < 3:
            return order.model_copy(update={"status": OrderStatus.OPEN}), None
        # Third call → fill
        filled = order.model_copy(update={
            "status": OrderStatus.COMPLETE,
            "filled_quantity": order.quantity,
            "average_fill_price": fill_obj.price,
        })
        return filled, fill_obj

    mock_broker.simulate_order.side_effect = side_effect

    ks = KillSwitch()
    om = OrderManager(mock_broker, risk, ks)

    order = Order(
        strategy_run_id="test-run",
        leg_index=1,
        trading_symbol="TEST26AUG100PE",
        instrument_token=1001,
        side=Side.SELL,
        quantity=65,
        limit_price=Decimal("0.05"),
    )

    updated, fill = om.execute_with_adaptive_limit(order, lambda: next(quote_seq))

    # Must have filled
    assert fill is not None, "Expected a fill on the third attempt"

    # Three broker calls
    assert mock_broker.simulate_order.call_count == 3, (
        f"Expected 3 simulate_order calls, got {mock_broker.simulate_order.call_count}"
    )

    price_0 = captured_orders[0][0].limit_price
    price_1 = captured_orders[1][0].limit_price
    price_2 = captured_orders[2][0].limit_price

    # Hold: price unchanged between call 0 and call 1
    assert price_0 == price_1, (
        f"Expected HOLD (same price) between call 0 and 1, got {price_0} → {price_1}"
    )

    # Reprice: price changed between call 1 and call 2
    assert price_1 != price_2, (
        f"Expected REPRICE (price change) between call 1 and 2, got {price_1} → {price_2}"
    )
