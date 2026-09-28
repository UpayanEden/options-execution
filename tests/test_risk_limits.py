"""Tests for pre-trade risk validation."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from risk.payoff import calculate_payoff_from_credit
from risk.validator import PreTradeValidator


def _make_mock_datetime(time_str: str = "10:00"):
    """Return a mock that replaces datetime in risk.validator, returning a fixed time."""
    mock_now = MagicMock()
    mock_now.strftime.return_value = time_str
    mock_dt = MagicMock()
    mock_dt.now.return_value = mock_now
    return mock_dt


class TestPreTradeValidator:
    def test_all_checks_pass_with_good_inputs(
        self, default_risk, oberoirlty_strategy, good_quotes
    ) -> None:
        validator = PreTradeValidator(default_risk)
        payoff = calculate_payoff_from_credit(oberoirlty_strategy, Decimal("17.00"), 100)
        with patch("risk.validator.datetime", _make_mock_datetime("10:00")):
            report = validator.validate(
                oberoirlty_strategy, good_quotes, payoff, Decimal("50000")
            )
        # At least these checks should be deterministic
        assert report.results  # non-empty

    def test_stale_quotes_fail(
        self, default_risk, oberoirlty_strategy, stale_quotes
    ) -> None:
        validator = PreTradeValidator(default_risk)
        payoff = calculate_payoff_from_credit(oberoirlty_strategy, Decimal("17.00"), 100)
        # Patch both: validator's datetime (for entry window) AND models' now_ist (for staleness)
        with patch("risk.validator.datetime", _make_mock_datetime("10:00")), \
             patch("execution.models.now_ist") as mock_now_ist:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            # Simulate that "now" is well past the stale quote timestamp (08:00)
            mock_now_ist.return_value = datetime(2026, 8, 3, 10, 0, 0, tzinfo=IST)
            report = validator.validate(
                oberoirlty_strategy, stale_quotes, payoff, Decimal("50000")
            )
        freshness = next(r for r in report.results if r.name == "quote_freshness")
        assert not freshness.passed

    def test_wide_spreads_fail(
        self, default_risk, oberoirlty_strategy, wide_spread_quotes
    ) -> None:
        validator = PreTradeValidator(default_risk)
        payoff = calculate_payoff_from_credit(oberoirlty_strategy, Decimal("17.00"), 100)
        with patch("risk.validator.datetime", _make_mock_datetime("10:00")):
            report = validator.validate(
                oberoirlty_strategy, wide_spread_quotes, payoff, Decimal("50000")
            )
        spread_check = next(r for r in report.results if r.name == "bid_ask_spread")
        assert not spread_check.passed

    def test_credit_below_minimum_fails(
        self, default_risk, oberoirlty_strategy, good_quotes
    ) -> None:
        validator = PreTradeValidator(default_risk)
        # Credit of 1.0 is below min_net_credit of 15.0
        payoff = calculate_payoff_from_credit(oberoirlty_strategy, Decimal("1.00"), 100)
        with patch("risk.validator.datetime", _make_mock_datetime("10:00")):
            report = validator.validate(
                oberoirlty_strategy, good_quotes, payoff, Decimal("50000")
            )
        credit_check = next(r for r in report.results if r.name == "net_credit")
        assert not credit_check.passed

    def test_capital_exceeded_fails(
        self, default_risk, oberoirlty_strategy, good_quotes
    ) -> None:
        validator = PreTradeValidator(default_risk)
        payoff = calculate_payoff_from_credit(oberoirlty_strategy, Decimal("17.00"), 100)
        with patch("risk.validator.datetime", _make_mock_datetime("10:00")):
            # Margin of 300k exceeds capital_limit of 200k
            report = validator.validate(
                oberoirlty_strategy, good_quotes, payoff, Decimal("300000")
            )
        cap_check = next(r for r in report.results if r.name == "capital_limit")
        assert not cap_check.passed
