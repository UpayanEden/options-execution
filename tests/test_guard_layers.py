"""
Pytest tests for all five guard layers in live/guard_layers.py.

Tests cover:
- Happy path (all guards pass)
- Each individual failure mode per layer

Uses unittest.mock — no real broker or API calls.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from execution.models import OptionType, Quote, ResolvedInstrument, Side
from live.config import CapitalConfig, LiveConfig, StrategyParams, StockConfig
from live.guard_layers import (
    GuardContext,
    GuardReport,
    _capital_guard,
    _instrument_guard,
    _order_guard,
    _position_guard,
    _quote_guard,
    run_all_guards,
)

IST = ZoneInfo("Asia/Kolkata")

# ── Fixtures ───────────────────────────────────────────────────────────────


def _make_config(
    total_capital: float = 2_000_000,
    max_capital_per_stock: float = 150_000,
    cash_margin_pct: float = 0.5,
    max_open_positions: int = 10,
    shift_trigger_pct: float = 1.5,
    wing_width_pct: float = 2.0,
    min_dte: int = 20,
    max_dte: int = 45,
) -> LiveConfig:
    """Build a minimal LiveConfig for tests."""
    return LiveConfig(
        capital=CapitalConfig(
            total_capital=total_capital,
            cash_margin_pct=cash_margin_pct,
            max_capital_per_stock=max_capital_per_stock,
            max_open_positions=max_open_positions,
        ),
        stocks=[
            StockConfig(symbol="RELIANCE", put_dist_pct=4.0, call_dist_pct=4.0, premium_pct=0.4)
        ],
        strategy=StrategyParams(
            shift_trigger_pct=shift_trigger_pct,
            wing_width_pct=wing_width_pct,
            min_dte=min_dte,
            max_dte=max_dte,
            entry_start="09:30",
            entry_end="14:00",
        ),
        zerodha_api_key="test",
        zerodha_api_secret="test",
        zerodha_access_token="test",
        mode="paper",
    )


def _make_instrument(
    token: int = 12345,
    symbol: str = "RELIANCE2650PE",
    strike: float = 2650.0,
    option_type: str = "PE",
    expiry_offset_days: int = 30,
) -> ResolvedInstrument:
    """Build a ResolvedInstrument for tests."""
    expiry = date.today() + timedelta(days=expiry_offset_days)
    return ResolvedInstrument(
        instrument_token=token,
        trading_symbol=symbol,
        exchange="NFO",
        underlying="RELIANCE",
        expiry=expiry,
        strike=Decimal(str(strike)),
        option_type=OptionType(option_type),
        lot_size=250,
        tick_size=Decimal("0.05"),
    )


def _make_quote(
    token: int = 12345,
    symbol: str = "RELIANCE2650PE",
    bid: float = 45.0,
    ask: float = 46.0,
    ltp: float = 45.5,
    age_seconds: float = 1.0,
) -> Quote:
    """Build a fresh Quote for tests."""
    ts = datetime.now(IST) - timedelta(seconds=age_seconds)
    return Quote(
        instrument_token=token,
        trading_symbol=symbol,
        exchange="NFO",
        timestamp=ts,
        ltp=Decimal(str(ltp)),
        bid=Decimal(str(bid)),
        bid_qty=500,
        ask=Decimal(str(ask)),
        ask_qty=500,
        volume=10000,
        oi=50000,
    )


def _make_context(
    spot: float = 2800.0,
    short_put_strike: float = 2650.0,
    long_put_strike: float = 2594.0,
    short_call_strike: float = 2950.0,
    long_call_strike: float = 3006.0,
    lots: int = 1,
    lot_size: int = 250,
    expiry_offset_days: int = 30,
    available_margin: float = 200_000,
    total_allocated: float = 0.0,
    daily_pnl: float = 0.0,
    open_condors_count: int = 0,
    config: LiveConfig = None,
    quote_age_s: float = 1.0,
    pending_orders: list = None,
    broker_positions: list = None,
    expected_broker_quantities: dict = None,
) -> GuardContext:
    """Build a default GuardContext suitable for happy-path testing."""
    if config is None:
        config = _make_config()

    expiry = date.today() + timedelta(days=expiry_offset_days)

    sp_inst = _make_instrument(12340, "RELIANCE2650PE", short_put_strike, "PE", expiry_offset_days)
    lp_inst = _make_instrument(12341, "RELIANCE2594PE", long_put_strike, "PE", expiry_offset_days)
    sc_inst = _make_instrument(12342, "RELIANCE2950CE", short_call_strike, "CE", expiry_offset_days)
    lc_inst = _make_instrument(12343, "RELIANCE3006CE", long_call_strike, "CE", expiry_offset_days)

    sp_quote = _make_quote(12340, "RELIANCE2650PE", 45.0, 46.0, 45.5, quote_age_s)
    lp_quote = _make_quote(12341, "RELIANCE2594PE", 10.0, 11.0, 10.5, quote_age_s)
    sc_quote = _make_quote(12342, "RELIANCE2950CE", 38.0, 39.0, 38.5, quote_age_s)
    lc_quote = _make_quote(12343, "RELIANCE3006CE", 12.0, 13.0, 12.5, quote_age_s)

    return GuardContext(
        underlying="RELIANCE",
        spot=spot,
        expiry=expiry,
        short_put_strike=short_put_strike,
        short_call_strike=short_call_strike,
        long_put_strike=long_put_strike,
        long_call_strike=long_call_strike,
        short_otm_pct=4.0,
        wing_width_pct=2.0,
        lots=lots,
        lot_size=lot_size,
        quotes={
            "RELIANCE2650PE": sp_quote,
            "RELIANCE2594PE": lp_quote,
            "RELIANCE2950CE": sc_quote,
            "RELIANCE3006CE": lc_quote,
        },
        instruments={
            "RELIANCE2650PE": sp_inst,
            "RELIANCE2594PE": lp_inst,
            "RELIANCE2950CE": sc_inst,
            "RELIANCE3006CE": lc_inst,
        },
        pending_orders=pending_orders or [],
        broker_positions=broker_positions or [],
        available_margin=available_margin,
        daily_pnl=daily_pnl,
        total_allocated_margin=total_allocated,
        config=config,
        phase="pre_entry",
        expected_sides={
            "RELIANCE2650PE": Side.SELL,
            "RELIANCE2594PE": Side.BUY,
            "RELIANCE2950CE": Side.SELL,
            "RELIANCE3006CE": Side.BUY,
        },
        expected_quantities={
            "RELIANCE2650PE": lots * lot_size,
            "RELIANCE2594PE": lots * lot_size,
            "RELIANCE2950CE": lots * lot_size,
            "RELIANCE3006CE": lots * lot_size,
        },
        expected_prices={
            "RELIANCE2650PE": Decimal("45.50"),
            "RELIANCE2594PE": Decimal("10.50"),
            "RELIANCE2950CE": Decimal("38.50"),
            "RELIANCE3006CE": Decimal("12.50"),
        },
        expected_broker_quantities=expected_broker_quantities or {},
        open_condors_count=open_condors_count,
    )


# ── run_all_guards happy path ───────────────────────────────────────────────


class TestRunAllGuardsHappyPath:
    """All five guard layers pass for a valid context."""

    def test_all_guards_pass(self):
        """run_all_guards returns a passing report when all conditions are met."""
        ctx = _make_context()
        report = run_all_guards(ctx)
        assert isinstance(report, GuardReport)
        assert len(report.results) == 5, "All five layers should produce a result"
        assert report.passed, (
            f"Expected all guards to pass. Failures: "
            f"{[(r.layer, r.reason) for r in report.results if not r.passed]}"
        )

    def test_all_guards_run_even_on_failure(self):
        """All 5 layers execute even if the first one fails."""
        ctx = _make_context()
        ctx.instruments = {}  # Trigger InstrumentGuard failure
        report = run_all_guards(ctx)
        assert len(report.results) == 5, "All five layers must run regardless of early failures"
        assert not report.passed
        assert report.first_failure is not None

    def test_first_failure_property(self):
        """first_failure returns the first failing layer."""
        ctx = _make_context()
        ctx.instruments = {}  # InstrumentGuard fails
        report = run_all_guards(ctx)
        assert report.first_failure.layer == "InstrumentGuard"

    def test_to_dict_structure(self):
        """to_dict returns the expected keys."""
        ctx = _make_context()
        report = run_all_guards(ctx)
        d = report.to_dict()
        assert "passed" in d
        assert "first_failure" in d
        assert "layers" in d
        assert len(d["layers"]) == 5


# ── Layer 1: InstrumentGuard ───────────────────────────────────────────────


class TestInstrumentGuard:
    """Tests for _instrument_guard."""

    def test_pass_valid_instruments(self):
        ctx = _make_context()
        result = _instrument_guard(ctx)
        assert result.passed, result.reason

    def test_fail_zero_token(self):
        """Instrument token of 0 should fail."""
        ctx = _make_context()
        bad_inst = _make_instrument(token=0, symbol="RELIANCE2650PE")
        ctx.instruments["RELIANCE2650PE"] = bad_inst
        result = _instrument_guard(ctx)
        assert not result.passed
        assert "instrument_token=0" in result.reason

    def test_fail_expiry_in_past(self):
        """Expiry in the past should fail."""
        ctx = _make_context()
        # Override one instrument with past expiry
        from dataclasses import replace
        past_inst = ResolvedInstrument(
            instrument_token=12340,
            trading_symbol="RELIANCE2650PE",
            exchange="NFO",
            underlying="RELIANCE",
            expiry=date.today() - timedelta(days=1),
            strike=Decimal("2650"),
            option_type=OptionType.PE,
            lot_size=250,
            tick_size=Decimal("0.05"),
        )
        ctx.instruments["RELIANCE2650PE"] = past_inst
        result = _instrument_guard(ctx)
        assert not result.passed
        assert "not in the future" in result.reason

    def test_fail_dte_too_low(self):
        """DTE below min_dte should fail."""
        ctx = _make_context(expiry_offset_days=5)  # 5 DTE < min_dte=20
        result = _instrument_guard(ctx)
        assert not result.passed
        assert "DTE=" in result.reason

    def test_fail_dte_too_high(self):
        """DTE above max_dte should fail."""
        ctx = _make_context(expiry_offset_days=60)  # 60 DTE > max_dte=45
        result = _instrument_guard(ctx)
        assert not result.passed
        assert "DTE=" in result.reason

    def test_fail_empty_instruments(self):
        """Empty instruments dict should fail."""
        ctx = _make_context()
        ctx.instruments = {}
        result = _instrument_guard(ctx)
        assert not result.passed
        assert "No instruments" in result.reason

    def test_layer_name(self):
        ctx = _make_context()
        result = _instrument_guard(ctx)
        assert result.layer == "InstrumentGuard"


# ── Layer 2: QuoteGuard ────────────────────────────────────────────────────


class TestQuoteGuard:
    """Tests for _quote_guard."""

    def test_pass_fresh_quotes(self):
        ctx = _make_context(quote_age_s=1.0)
        result = _quote_guard(ctx)
        assert result.passed, result.reason

    def test_fail_stale_quote(self):
        """Quote older than 10 seconds should fail."""
        ctx = _make_context(quote_age_s=15.0)
        result = _quote_guard(ctx)
        assert not result.passed
        assert "age" in result.reason

    def test_fail_zero_bid(self):
        """Zero bid on any leg should fail."""
        ctx = _make_context()
        stale_q = _make_quote(12340, "RELIANCE2650PE", bid=0.0, ask=46.0, ltp=23.0)
        ctx.quotes["RELIANCE2650PE"] = stale_q
        result = _quote_guard(ctx)
        assert not result.passed
        assert "bid=0" in result.reason

    def test_fail_zero_ask(self):
        """Zero ask should fail."""
        ctx = _make_context()
        bad_q = _make_quote(12340, "RELIANCE2650PE", bid=45.0, ask=0.0, ltp=22.5)
        ctx.quotes["RELIANCE2650PE"] = bad_q
        result = _quote_guard(ctx)
        assert not result.passed
        assert "ask=0" in result.reason

    def test_fail_wide_spread_short_leg(self):
        """Spread > 25% of mid for a short leg should fail."""
        ctx = _make_context()
        # bid=10, ask=90 → spread=80, mid=50, spread_pct=160%
        wide_q = _make_quote(12340, "RELIANCE2650PE", bid=10.0, ask=90.0, ltp=50.0)
        ctx.quotes["RELIANCE2650PE"] = wide_q
        result = _quote_guard(ctx)
        assert not result.passed
        assert "spread" in result.reason.lower()

    def test_fail_ltp_far_from_mid(self):
        """LTP more than 5% from mid should fail."""
        ctx = _make_context()
        # bid=45, ask=46 → mid=45.5, but ltp=50 → deviation ≈ 9.9%
        far_q = _make_quote(12340, "RELIANCE2650PE", bid=45.0, ask=46.0, ltp=50.0)
        ctx.quotes["RELIANCE2650PE"] = far_q
        result = _quote_guard(ctx)
        assert not result.passed
        assert "LTP" in result.reason

    def test_fail_short_leg_low_premium(self):
        """Short leg LTP below 0.1% of spot should fail."""
        ctx = _make_context(spot=2800.0)
        # 0.1% of 2800 = 2.8; set LTP=1.0
        low_q = _make_quote(12340, "RELIANCE2650PE", bid=0.95, ask=1.05, ltp=1.0)
        ctx.quotes["RELIANCE2650PE"] = low_q
        result = _quote_guard(ctx)
        assert not result.passed
        assert "minimum premium" in result.reason

    def test_fail_empty_quotes(self):
        ctx = _make_context()
        ctx.quotes = {}
        result = _quote_guard(ctx)
        assert not result.passed

    def test_layer_name(self):
        ctx = _make_context()
        result = _quote_guard(ctx)
        assert result.layer == "QuoteGuard"


# ── Layer 3: OrderGuard ────────────────────────────────────────────────────


class TestOrderGuard:
    """Tests for _order_guard."""

    def test_pass_valid_orders(self):
        ctx = _make_context()
        result = _order_guard(ctx)
        assert result.passed, result.reason

    def test_fail_price_outside_spread(self):
        """Limit price above ask for a buy order should fail."""
        ctx = _make_context()
        # Set proposed price for the long put (BUY) above ask
        ctx.expected_prices["RELIANCE2594PE"] = Decimal("15.00")  # ask is 11.0
        result = _order_guard(ctx)
        assert not result.passed
        assert "RELIANCE2594PE" in result.reason

    def test_fail_wrong_quantity(self):
        """Quantity mismatch should fail."""
        ctx = _make_context(lots=1, lot_size=250)
        ctx.expected_quantities["RELIANCE2650PE"] = 500  # should be 250
        result = _order_guard(ctx)
        assert not result.passed
        assert "qty" in result.reason

    def test_fail_duplicate_order_recent(self):
        """A pending order placed < 60s ago for the same symbol should fail."""
        ts_recent = datetime.now(IST) - timedelta(seconds=30)
        pending = {
            "tradingsymbol": "RELIANCE2650PE",
            "order_id": "ORD123",
            "order_timestamp": ts_recent.isoformat(),
        }
        ctx = _make_context(pending_orders=[pending])
        result = _order_guard(ctx)
        assert not result.passed
        assert "duplicate" in result.reason.lower()

    def test_pass_old_order_not_duplicate(self):
        """A pending order placed > 60s ago should NOT trigger duplicate check."""
        ts_old = datetime.now(IST) - timedelta(seconds=120)
        pending = {
            "tradingsymbol": "RELIANCE2650PE",
            "order_id": "ORD456",
            "order_timestamp": ts_old.isoformat(),
        }
        ctx = _make_context(pending_orders=[pending])
        result = _order_guard(ctx)
        assert result.passed, result.reason

    def test_layer_name(self):
        ctx = _make_context()
        result = _order_guard(ctx)
        assert result.layer == "OrderGuard"


# ── Layer 4: PositionGuard ─────────────────────────────────────────────────


class TestPositionGuard:
    """Tests for _position_guard."""

    def test_pass_clean_state(self):
        ctx = _make_context()
        result = _position_guard(ctx)
        assert result.passed, result.reason

    def test_fail_max_positions_reached(self):
        """Exceeding max_open_positions should fail."""
        config = _make_config(max_open_positions=3)
        ctx = _make_context(config=config, open_condors_count=3)
        result = _position_guard(ctx)
        assert not result.passed
        assert "max_open_positions" in result.reason

    def test_fail_broker_qty_mismatch(self):
        """If expected broker qty differs from actual, should fail."""
        ctx = _make_context(expected_broker_quantities={"RELIANCE2650PE": -250})
        # Broker has 0 for this symbol
        ctx.broker_positions = []
        result = _position_guard(ctx)
        assert not result.passed
        assert "RELIANCE2650PE" in result.reason

    def test_fail_naked_short_in_pre_entry(self):
        """Existing naked short position should fail in pre_entry phase."""
        ctx = _make_context()
        ctx.broker_positions = [
            {"tradingsymbol": "RELIANCE2650PE", "quantity": -250}
        ]
        result = _position_guard(ctx)
        assert not result.passed
        assert "naked short" in result.reason

    def test_pass_max_positions_not_reached(self):
        config = _make_config(max_open_positions=10)
        ctx = _make_context(config=config, open_condors_count=5)
        result = _position_guard(ctx)
        assert result.passed, result.reason

    def test_layer_name(self):
        ctx = _make_context()
        result = _position_guard(ctx)
        assert result.layer == "PositionGuard"


# ── Layer 5: CapitalGuard ──────────────────────────────────────────────────


class TestCapitalGuard:
    """Tests for _capital_guard."""

    def test_pass_sufficient_capital(self):
        ctx = _make_context(available_margin=200_000, total_allocated=0.0, daily_pnl=0.0)
        result = _capital_guard(ctx)
        assert result.passed, result.reason

    def test_fail_margin_exceeds_per_stock_limit(self):
        """Required margin > max_capital_per_stock should fail."""
        # spot=2800, wing=2%, lot_size=250, lots=1 → margin ≈ 2800*2%*250*1.2 = 16800
        # Set max_capital_per_stock very low
        config = _make_config(max_capital_per_stock=1000)
        ctx = _make_context(config=config, available_margin=200_000)
        result = _capital_guard(ctx)
        assert not result.passed
        assert "max_capital_per_stock" in result.reason

    def test_fail_insufficient_cash(self):
        """Available margin < cash_margin_pct × required should fail."""
        ctx = _make_context(available_margin=100)  # very low
        result = _capital_guard(ctx)
        assert not result.passed
        assert "cash" in result.reason.lower() or "margin" in result.reason.lower()

    def test_fail_daily_loss_limit(self):
        """Daily P&L below -20% of total capital should fail."""
        config = _make_config(total_capital=2_000_000)
        # -20% of 2M = -400k; set to -500k
        ctx = _make_context(config=config, available_margin=200_000, daily_pnl=-500_000)
        result = _capital_guard(ctx)
        assert not result.passed
        assert "Daily P&L" in result.reason

    def test_fail_total_allocation_exceeded(self):
        """Adding new margin that pushes total > total_capital should fail."""
        config = _make_config(total_capital=2_000_000)
        # Already allocated 1,999,000; new trade would exceed total
        ctx = _make_context(
            config=config,
            available_margin=200_000,
            total_allocated=1_999_000,
        )
        result = _capital_guard(ctx)
        assert not result.passed
        assert "total_capital" in result.reason

    def test_layer_name(self):
        ctx = _make_context()
        result = _capital_guard(ctx)
        assert result.layer == "CapitalGuard"


# ── Guard report structural tests ──────────────────────────────────────────


class TestGuardReport:
    """Tests for GuardReport properties."""

    def test_passed_true_when_all_pass(self):
        ctx = _make_context()
        report = run_all_guards(ctx)
        if report.passed:
            assert report.first_failure is None

    def test_passed_false_on_any_fail(self):
        ctx = _make_context()
        ctx.instruments = {}  # Force failure
        report = run_all_guards(ctx)
        assert not report.passed

    def test_to_dict_serialisable(self):
        """to_dict() output should be JSON-serialisable."""
        import json
        ctx = _make_context()
        report = run_all_guards(ctx)
        d = report.to_dict()
        # Should not raise
        json_str = json.dumps(d)
        assert len(json_str) > 0
