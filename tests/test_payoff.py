"""Tests for payoff calculations."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from execution.models import LegConfig, OptionType, Side, StrategyConfig
from risk.payoff import (
    PayoffResult,
    calculate_cost_to_close,
    calculate_executable_credit,
    calculate_mtm_pnl,
    calculate_payoff_from_credit,
)


class TestPayoffFromCredit:
    def test_max_profit_equals_credit_times_quantity(self) -> None:
        strategy = StrategyConfig(
            name="test", underlying="X", exchange="NFO",
            expiry=date(2026, 8, 25), lots=1,
            legs=[
                LegConfig(side=Side.BUY, option_type=OptionType.PE, strike=Decimal("1700")),
                LegConfig(side=Side.SELL, option_type=OptionType.PE, strike=Decimal("1720")),
                LegConfig(side=Side.SELL, option_type=OptionType.CE, strike=Decimal("1920")),
                LegConfig(side=Side.BUY, option_type=OptionType.CE, strike=Decimal("1940")),
            ],
        )
        credit = Decimal("5.00")
        lot_size = 100
        result = calculate_payoff_from_credit(strategy, credit, lot_size)
        assert result.max_profit == Decimal("500.00")

    def test_max_loss_is_spread_minus_credit_times_quantity(self) -> None:
        strategy = StrategyConfig(
            name="test", underlying="X", exchange="NFO",
            expiry=date(2026, 8, 25), lots=1,
            legs=[
                LegConfig(side=Side.BUY, option_type=OptionType.PE, strike=Decimal("1700")),
                LegConfig(side=Side.SELL, option_type=OptionType.PE, strike=Decimal("1720")),
                LegConfig(side=Side.SELL, option_type=OptionType.CE, strike=Decimal("1920")),
                LegConfig(side=Side.BUY, option_type=OptionType.CE, strike=Decimal("1940")),
            ],
        )
        credit = Decimal("5.00")
        lot_size = 100
        result = calculate_payoff_from_credit(strategy, credit, lot_size)
        # spread width = min(1720-1700, 1940-1920) = 20
        # max_loss = (20 - 5) × 100 = 1500
        assert result.max_loss == Decimal("1500.00")

    def test_breakevens(self) -> None:
        strategy = StrategyConfig(
            name="test", underlying="X", exchange="NFO",
            expiry=date(2026, 8, 25), lots=1,
            legs=[
                LegConfig(side=Side.BUY, option_type=OptionType.PE, strike=Decimal("1700")),
                LegConfig(side=Side.SELL, option_type=OptionType.PE, strike=Decimal("1720")),
                LegConfig(side=Side.SELL, option_type=OptionType.CE, strike=Decimal("1920")),
                LegConfig(side=Side.BUY, option_type=OptionType.CE, strike=Decimal("1940")),
            ],
        )
        result = calculate_payoff_from_credit(strategy, Decimal("5.00"), 100)
        assert result.lower_breakeven == Decimal("1715.00")   # 1720 - 5
        assert result.upper_breakeven == Decimal("1925.00")   # 1920 + 5

    def test_zero_credit_zero_profit(self) -> None:
        strategy = StrategyConfig(
            name="test", underlying="X", exchange="NFO",
            expiry=date(2026, 8, 25), lots=1,
            legs=[
                LegConfig(side=Side.BUY, option_type=OptionType.PE, strike=Decimal("1700")),
                LegConfig(side=Side.SELL, option_type=OptionType.PE, strike=Decimal("1720")),
                LegConfig(side=Side.SELL, option_type=OptionType.CE, strike=Decimal("1920")),
                LegConfig(side=Side.BUY, option_type=OptionType.CE, strike=Decimal("1940")),
            ],
        )
        result = calculate_payoff_from_credit(strategy, Decimal("0"), 100)
        assert result.max_profit == Decimal("0")
        assert result.max_loss == Decimal("2000.00")  # full spread × 100


class TestMtmPnl:
    def test_profit_when_cost_to_close_less_than_entry_credit(self) -> None:
        pnl = calculate_mtm_pnl(
            entry_credit_total=Decimal("500"),
            cost_to_close=Decimal("200"),
        )
        assert pnl == Decimal("300")

    def test_loss_when_cost_to_close_exceeds_entry_credit(self) -> None:
        pnl = calculate_mtm_pnl(
            entry_credit_total=Decimal("500"),
            cost_to_close=Decimal("800"),
        )
        assert pnl == Decimal("-300")
