"""Tests for position reconciliation."""
from __future__ import annotations

from decimal import Decimal
import pytest

from execution.models import LegPosition, OptionType, Side
from execution.reconciliation import Reconciliation


def _leg(symbol: str, side: Side, qty: int) -> LegPosition:
    return LegPosition(
        leg_index=0,
        trading_symbol=symbol,
        instrument_token=1001,
        side=side,
        option_type=OptionType.PE,
        strike=Decimal("1700"),
        lot_size=575,
        lots=1,
        quantity=qty,
        average_entry_price=Decimal("8.00"),
    )


class TestReconciliation:
    def test_matched_positions(self) -> None:
        local = [
            _leg("OBEROIRLTY26AUG1700PE", Side.BUY, 575),
            _leg("OBEROIRLTY26AUG1720PE", Side.SELL, 575),
        ]
        broker = [
            {"tradingsymbol": "OBEROIRLTY26AUG1700PE", "net_quantity": 575},
            {"tradingsymbol": "OBEROIRLTY26AUG1720PE", "net_quantity": -575},
        ]
        rec = Reconciliation()
        result = rec.reconcile(local, broker)
        assert result.matched
        assert result.mismatches == []

    def test_quantity_mismatch_detected(self) -> None:
        local = [_leg("OBEROIRLTY26AUG1700PE", Side.BUY, 575)]
        broker = [{"tradingsymbol": "OBEROIRLTY26AUG1700PE", "net_quantity": 100}]
        rec = Reconciliation()
        result = rec.reconcile(local, broker)
        assert not result.matched
        assert len(result.mismatches) == 1

    def test_missing_broker_position_detected(self) -> None:
        local = [_leg("OBEROIRLTY26AUG1700PE", Side.BUY, 575)]
        broker = []
        rec = Reconciliation()
        result = rec.reconcile(local, broker)
        assert not result.matched
        assert "no position" in result.mismatches[0]
