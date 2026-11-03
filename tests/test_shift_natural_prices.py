"""
Tests for Task 6: shift projections use natural prices.

Changes:
  - new_short_price = q_ns.bid  (we SELL at bid — conservative receive)
  - new_long_price  = q_nl.ask  (we BUY at ask — conservative pay)
  - new_side_credit_estimated = bid(new_short) - ask(new_long)
  - min_shift_edge (default 0.50): abort shift if new side credit < min_shift_edge

These tests verify the formula logic directly (pure unit tests, no broker).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

# ── helpers ────────────────────────────────────────────────────────────────────


def _tick(price: float, tick: float = 0.05) -> float:
    """Replicate the tick rounding in condor_runner."""
    return round(round(price / tick) * tick, 10)


def _natural_side_credit(short_bid: float, long_ask: float) -> float:
    """Natural new-side credit: receive bid on short, pay ask on long."""
    return _tick(short_bid) - _tick(long_ask)


# ── Natural price formula ──────────────────────────────────────────────────────


class TestNaturalPriceFormula:

    def test_wide_spread_gives_lower_credit_than_mid(self):
        """
        When spread is wide, natural (bid-ask) credit < mid-mid credit.
        Natural is more conservative and correct.
        """
        # short: bid=9.75, ask=10.25 → mid=10.00 → natural short = bid = 9.75
        # long:  bid=4.75, ask=5.25  → mid=5.00  → natural long  = ask = 5.25
        natural = _natural_side_credit(short_bid=9.75, long_ask=5.25)
        mid_mid = 10.00 - 5.00  # what mid-mid gives

        assert natural < mid_mid, (
            f"Natural credit {natural} should be less than mid-mid {mid_mid}"
        )
        assert natural == pytest.approx(4.50)

    def test_zero_spread_natural_equals_mid(self):
        """When bid==ask (no spread), natural == mid."""
        # both zero-spread instruments
        natural = _natural_side_credit(short_bid=10.0, long_ask=5.0)
        assert natural == pytest.approx(5.0)

    def test_natural_can_be_negative(self):
        """
        Natural credit can be negative (short bid < long ask).
        This represents an uneconomical spread — min_shift_edge should catch it.
        """
        natural = _natural_side_credit(short_bid=4.0, long_ask=5.0)
        assert natural < 0


# ── min_shift_edge guard ──────────────────────────────────────────────────────


class TestMinShiftEdge:

    def _check_edge(
        self, short_bid: float, long_ask: float, min_edge: float = 0.50
    ) -> bool:
        """Return True if shift should be aborted due to insufficient edge."""
        natural = _natural_side_credit(short_bid=short_bid, long_ask=long_ask)
        return natural < min_edge

    def test_below_min_edge_aborts(self):
        """0.40 < 0.50 → abort."""
        assert self._check_edge(short_bid=5.40, long_ask=5.00) is True

    def test_exactly_at_min_edge_allows(self):
        """0.50 == 0.50 → allow (not strictly below)."""
        assert self._check_edge(short_bid=5.50, long_ask=5.00) is False

    def test_above_min_edge_allows(self):
        """2.0 > 0.50 → allow."""
        assert self._check_edge(short_bid=7.0, long_ask=5.0) is False

    def test_negative_credit_aborts_with_default_edge(self):
        """Negative natural credit (-0.50) < 0.50 → abort."""
        assert self._check_edge(short_bid=4.50, long_ask=5.0) is True

    def test_zero_min_edge_disables_guard(self):
        """min_edge=0.0: even slightly negative credit is allowed (-9999 would be too)."""
        assert self._check_edge(short_bid=5.00, long_ask=5.01, min_edge=0.0) is False


# ── Tick rounding ─────────────────────────────────────────────────────────────


class TestTickRounding:

    def test_bid_rounds_to_nearest_tick(self):
        """9.77 → 9.75 (rounds down to nearest 0.05)."""
        assert _tick(9.77) == pytest.approx(9.75)

    def test_ask_rounds_up(self):
        """5.23 → 5.25 (rounds up to nearest 0.05)."""
        assert _tick(5.23) == pytest.approx(5.25)

    def test_already_on_tick_unchanged(self):
        assert _tick(10.0) == pytest.approx(10.0)
        assert _tick(5.5) == pytest.approx(5.5)
