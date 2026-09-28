from __future__ import annotations
import logging
from decimal import Decimal
from typing import Optional
from execution.models import Fill, Order, OrderStatus, Side

logger = logging.getLogger(__name__)


class FillTracker:
    """
    Tracks fill state across all legs of an iron condor entry or exit.
    Computes average fill prices, running net credit, and identifies unfilled legs.
    """

    def __init__(self, leg_count: int) -> None:
        self._leg_count = leg_count
        self._fills: dict[int, Fill] = {}       # leg_index -> Fill
        self._orders: dict[int, Order] = {}     # leg_index -> latest Order

    def record_fill(self, leg_index: int, order: Order, fill: Fill) -> None:
        self._fills[leg_index] = fill
        self._orders[leg_index] = order
        logger.info("fill_recorded", extra={
            "leg_index": leg_index,
            "symbol": order.trading_symbol,
            "side": order.side.value,
            "quantity": fill.quantity,
            "price": str(fill.price),
        })

    def record_unfilled(self, leg_index: int, order: Order) -> None:
        self._orders[leg_index] = order
        logger.warning("leg_unfilled", extra={
            "leg_index": leg_index,
            "symbol": order.trading_symbol,
            "side": order.side.value,
        })

    @property
    def filled_legs(self) -> list[int]:
        return list(self._fills.keys())

    @property
    def unfilled_legs(self) -> list[int]:
        return [i for i in range(self._leg_count) if i not in self._fills]

    @property
    def all_filled(self) -> bool:
        return len(self._fills) == self._leg_count

    def actual_net_credit(self, sides: dict[int, Side]) -> Optional[Decimal]:
        """
        Compute actual net credit per share from fills.
        Returns None if any leg is unfilled.
        """
        if not self.all_filled:
            return None
        buy_cost = Decimal("0")
        sell_credit = Decimal("0")
        for leg_index, fill in self._fills.items():
            side = sides[leg_index]
            if side == Side.BUY:
                buy_cost += fill.price
            else:
                sell_credit += fill.price
        return sell_credit - buy_cost

    def hedge_legs_filled(self, hedge_indices: list[int]) -> bool:
        """Check if all hedge (long) legs are filled."""
        return all(i in self._fills for i in hedge_indices)

    def short_legs_filled(self, short_indices: list[int]) -> bool:
        """Check if all short legs are filled."""
        return all(i in self._fills for i in short_indices)

    def summary(self) -> dict:
        return {
            "total_legs": self._leg_count,
            "filled_legs": len(self._fills),
            "unfilled_legs": self.unfilled_legs,
            "all_filled": self.all_filled,
        }
