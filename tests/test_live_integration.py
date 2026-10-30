"""
Integration tests for CondorRunner using a PaperBroker mock.

No real API calls are made. The PaperBroker implements BrokerAdapter with
pre-loaded instrument data and deterministic quotes so tests are repeatable.

Tests cover:
- Full condor entry happy path
- Entry failure modes (no expiry, no quotes, guard failures)
- Shift execution
- Exit execution
- Capital manager integration
- ShiftMonitor trigger logic
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from brokers.base import BrokerAdapter, InstrumentNotFoundError, QuoteError
from execution.models import OptionType, Quote, ResolvedInstrument
from live.capital_manager import CapitalManager
from live.config import CapitalConfig, LiveConfig, StrategyParams, StockConfig
from live.condor_runner import CondorPosition, CondorRunner
from live.guard_layers import run_all_guards
from live.shift_monitor import ShiftMonitor
from live.trade_logger import TradeLogger
from risk.kill_switch import KillSwitch

IST = ZoneInfo("Asia/Kolkata")


def _test_expiry() -> date:
    """Return a Thursday ~30 days from today so weekly expiry filter passes."""
    base = date.today() + timedelta(days=30)
    return base + timedelta(days=(3 - base.weekday()) % 7)


# ── PaperBroker fixture ────────────────────────────────────────────────────


class PaperBroker(BrokerAdapter):
    """
    In-memory broker adapter for integration tests.

    Pre-loads a small instrument universe for RELIANCE options.
    Returns deterministic quotes. Simulates instant order fills.
    """

    def __init__(self, spot: float = 2800.0) -> None:
        """
        Initialise the PaperBroker.

        Args:
            spot: Simulated spot price for RELIANCE.
        """
        self._spot = spot
        self._orders: List[Dict[str, Any]] = []
        self._next_order_id = 1
        self._positions: List[Dict[str, Any]] = []
        self._margins = {"equity": {"available": {"live_balance": 500_000}}}
        self._raise_quote_error = False
        self._raise_instrument_error = False
        self._instruments = self._build_instruments()

    # ── Instrument universe ────────────────────────────────────────────────

    def _build_instruments(self) -> List[Dict[str, Any]]:
        """Build a synthetic instrument list for RELIANCE options."""
        instruments = []
        expiry_date = _test_expiry()
        base_strikes = [2500, 2550, 2594, 2600, 2650, 2700, 2750, 2800,
                        2850, 2900, 2950, 3000, 3006, 3050, 3100]
        token_base = 10000
        for strike in base_strikes:
            for otype in ("PE", "CE"):
                sym = f"RELIANCE{strike}{otype}"
                instruments.append({
                    "instrument_token": token_base,
                    "tradingsymbol": sym,
                    "name": "RELIANCE",
                    "exchange": "NFO",
                    "expiry": expiry_date.isoformat(),
                    "strike": float(strike),
                    "instrument_type": otype,
                    "lot_size": 250,
                    "tick_size": 0.05,
                })
                token_base += 1
        return instruments

    def _load_instruments(self) -> List[Dict[str, Any]]:
        """Return pre-built instruments (no download)."""
        return self._instruments

    # ── BrokerAdapter interface ────────────────────────────────────────────

    def resolve_instrument(
        self,
        underlying: str,
        exchange: str,
        expiry: date,
        strike: Decimal,
        option_type: str,
    ) -> ResolvedInstrument:
        """Resolve an instrument from the pre-built instrument list."""
        if self._raise_instrument_error:
            raise InstrumentNotFoundError(f"Simulated not found: {underlying} {strike} {option_type}")
        target = float(strike)
        otype_upper = option_type.upper()
        for inst in self._instruments:
            if (
                inst["name"].upper() == underlying.upper()
                and inst["instrument_type"] == otype_upper
                and date.fromisoformat(inst["expiry"]) == expiry
                and abs(inst["strike"] - target) < 0.01
            ):
                return ResolvedInstrument(
                    instrument_token=inst["instrument_token"],
                    trading_symbol=inst["tradingsymbol"],
                    exchange="NFO",
                    underlying=underlying,
                    expiry=expiry,
                    strike=Decimal(str(inst["strike"])),
                    option_type=OptionType(otype_upper),
                    lot_size=inst["lot_size"],
                    tick_size=Decimal(str(inst["tick_size"])),
                )
        raise InstrumentNotFoundError(f"Not found: {underlying} {expiry} {strike} {option_type}")

    def get_quote(self, instrument_token: int, trading_symbol: str) -> Quote:
        """Return a deterministic quote."""
        quotes = self.get_quotes([(instrument_token, trading_symbol)])
        if instrument_token not in quotes:
            raise QuoteError(f"No quote for {trading_symbol}")
        return quotes[instrument_token]

    def get_quotes(self, instruments: List[Tuple[int, str]]) -> Dict[int, Quote]:
        """Return quotes for all requested instruments."""
        if self._raise_quote_error:
            raise QuoteError("Simulated quote error")
        now = datetime.now(IST)
        result = {}
        for token, sym in instruments:
            # Determine a plausible price based on option type and distance from spot
            if "PE" in sym:
                distance = max(0, self._spot - self._extract_strike(sym))
                intrinsic = max(0, distance)
                price = max(5.0, intrinsic * 0.05 + 15.0)
            else:
                distance = max(0, self._extract_strike(sym) - self._spot)
                intrinsic = max(0, distance)
                price = max(5.0, intrinsic * 0.05 + 15.0)

            result[token] = Quote(
                instrument_token=token,
                trading_symbol=sym,
                exchange="NFO",
                timestamp=now,
                ltp=Decimal(str(round(price, 2))),
                bid=Decimal(str(round(price * 0.98, 2))),
                bid_qty=500,
                ask=Decimal(str(round(price * 1.02, 2))),
                ask_qty=500,
                volume=10000,
                oi=50000,
            )
        return result

    def _extract_strike(self, sym: str) -> float:
        """Extract numeric strike from a symbol like RELIANCE2650PE."""
        digits = "".join(c for c in sym if c.isdigit() or c == ".")
        try:
            return float(digits)
        except ValueError:
            return self._spot

    def get_positions(self) -> List[Dict[str, Any]]:
        """Return simulated open positions."""
        return list(self._positions)

    def get_orders(self) -> List[Dict[str, Any]]:
        """Return all simulated orders."""
        return list(self._orders)

    def get_margins(self) -> Dict[str, Any]:
        """Return simulated margins."""
        return self._margins

    def place_order_from_model(self, order: Any) -> str:
        """Simulate instant order fill, return broker order ID."""
        oid = f"PAPER{self._next_order_id:06d}"
        self._next_order_id += 1
        self._orders.append({
            "order_id": oid,
            "tradingsymbol": order.trading_symbol,
            "transaction_type": order.side.value,
            "quantity": order.quantity,
            "price": float(order.limit_price),
            "status": "COMPLETE",
            "order_timestamp": datetime.now(IST).isoformat(),
        })
        # Update simulated positions
        qty_signed = order.quantity if order.side.value == "BUY" else -order.quantity
        self._positions.append({
            "tradingsymbol": order.trading_symbol,
            "quantity": qty_signed,
            "average_price": float(order.limit_price),
        })
        return oid

    # ── Test helpers ───────────────────────────────────────────────────────

    def set_spot(self, spot: float) -> None:
        """Update the simulated spot price."""
        self._spot = spot

    def get_index_token(self, underlying: str) -> Optional[int]:
        """Return None — spot is injected via _get_spot mock."""
        return None

    def get_basket_margin(self, orders: list) -> dict:
        """Return empty dict — broker_required_margin falls back to estimate in tests."""
        return {}


# ── Config fixture ─────────────────────────────────────────────────────────


def _make_live_config() -> LiveConfig:
    """Build a LiveConfig suitable for integration tests."""
    return LiveConfig(
        capital=CapitalConfig(
            total_capital=2_000_000,
            cash_margin_pct=0.3,
            max_capital_per_stock=500_000,
            max_open_positions=10,
        ),
        stocks=[
            StockConfig(
                symbol="RELIANCE",
                put_dist_pct=4.0,
                call_dist_pct=4.0,
                premium_pct=0.4,
                enabled=True,
                expiry_type="weekly",  # test instruments use today+30d which isn't a monthly expiry
            )
        ],
        strategy=StrategyParams(
            shift_trigger_pct=1.5,
            wing_width_pct=2.0,
            min_dte=20,
            max_dte=45,
            entry_start="00:00",
            entry_end="23:59",
            min_entry_premium_pct=0.0,  # disable premium filter in integration tests
        ),
        zerodha_api_key="test",
        zerodha_api_secret="test",
        zerodha_access_token="test",
        mode="paper",
    )


def _make_runner(
    broker: Optional[PaperBroker] = None,
    spot: float = 2800.0,
    tmp_path: Optional[Any] = None,
) -> Tuple[CondorRunner, PaperBroker, TradeLogger]:
    """Create a CondorRunner with a PaperBroker and TradeLogger."""
    from live.position_store import PositionStore
    import uuid

    if broker is None:
        broker = PaperBroker(spot=spot)
    config = _make_live_config()
    log_dir = str(tmp_path) if tmp_path else "/tmp/test_condor_logs"
    tl = TradeLogger(log_dir=log_dir, symbol="RELIANCE")
    capital_mgr = CapitalManager(config.capital)
    kill_switch = KillSwitch(flag_file=_tmp_kill_path())
    runner = CondorRunner(
        broker=broker,
        config=config,
        trade_logger=tl,
        capital_manager=capital_mgr,
        kill_switch=kill_switch,
    )
    # Use a unique in-memory DB for each test to avoid cross-test contamination
    from pathlib import Path
    unique_db = Path(f"/tmp/test_condor_{uuid.uuid4().hex}.db")
    runner._position_store = PositionStore(db_path=unique_db)
    return runner, broker, tl


def _tmp_kill_path():
    """Return a temp kill switch path that doesn't conflict with production."""
    from pathlib import Path
    path = Path("/tmp/test_kill_switch_flag")
    if path.exists():
        path.unlink()
    return path


# ── Entry tests ────────────────────────────────────────────────────────────


class TestCondorEntry:
    """Integration tests for CondorRunner.enter_condor()."""

    def test_happy_path_entry(self, tmp_path):
        """A valid entry should return a CondorPosition with all four legs filled."""
        runner, broker, _ = _make_runner(spot=2800.0, tmp_path=tmp_path)

        # Patch _get_spot and fix time so test passes at any hour
        runner._get_spot = MagicMock(return_value=2800.0)
        runner._get_now_str = MagicMock(return_value="10:00")

        stock_cfg = runner._config.stocks[0]
        pos = runner.enter_condor(stock_cfg)

        assert pos is not None, "Entry should succeed"
        assert isinstance(pos, CondorPosition)
        assert pos.symbol == "RELIANCE"
        assert pos.lots >= 1
        assert pos.lot_size == 250
        assert pos.state == "OPEN"
        assert pos.short_put.option_type.value == "PE"
        assert pos.short_call.option_type.value == "CE"
        assert pos.long_put.option_type.value == "PE"
        assert pos.long_call.option_type.value == "CE"
        assert float(pos.short_put.strike) < float(pos.short_call.strike)
        assert float(pos.long_put.strike) < float(pos.short_put.strike)
        assert float(pos.long_call.strike) > float(pos.short_call.strike)

    def test_entry_sets_initial_credit(self, tmp_path):
        """Initial credit should be set as net premium received."""
        runner, broker, _ = _make_runner(spot=2800.0, tmp_path=tmp_path)
        runner._get_spot = MagicMock(return_value=2800.0)
        runner._get_now_str = MagicMock(return_value="10:00")
        pos = runner.enter_condor(runner._config.stocks[0])
        assert pos is not None
        # Credit = short_premiums - long_premiums (can be negative in some market conditions)
        assert isinstance(pos.initial_credit, float)

    def test_entry_allocates_capital(self, tmp_path):
        """Entering a condor should allocate capital via CapitalManager."""
        runner, broker, _ = _make_runner(spot=2800.0, tmp_path=tmp_path)
        runner._get_spot = MagicMock(return_value=2800.0)
        runner._get_now_str = MagicMock(return_value="10:00")
        pos = runner.enter_condor(runner._config.stocks[0])
        assert pos is not None
        allocated = runner._capital_manager.get_allocated()
        assert "RELIANCE" in allocated
        assert allocated["RELIANCE"] > 0

    def test_entry_fails_no_spot(self, tmp_path):
        """Entry should fail gracefully if spot price is unavailable."""
        runner, broker, _ = _make_runner(spot=2800.0, tmp_path=tmp_path)
        runner._get_spot = MagicMock(return_value=None)
        pos = runner.enter_condor(runner._config.stocks[0])
        assert pos is None

    def test_entry_fails_no_expiry(self, tmp_path):
        """Entry should fail if no suitable expiry is found."""
        runner, broker, _ = _make_runner(spot=2800.0, tmp_path=tmp_path)
        runner._get_spot = MagicMock(return_value=2800.0)
        # Override config to have impossible DTE range
        runner._config.strategy.min_dte = 100
        runner._config.strategy.max_dte = 150
        pos = runner.enter_condor(runner._config.stocks[0])
        assert pos is None

    def test_entry_fails_quote_error(self, tmp_path):
        """Entry should fail gracefully if quotes cannot be fetched."""
        runner, broker, _ = _make_runner(spot=2800.0, tmp_path=tmp_path)
        runner._get_spot = MagicMock(return_value=2800.0)
        broker._raise_quote_error = True
        pos = runner.enter_condor(runner._config.stocks[0])
        assert pos is None

    def test_entry_places_four_orders(self, tmp_path):
        """A successful entry should place exactly 4 orders (2 hedges + 2 shorts)."""
        runner, broker, _ = _make_runner(spot=2800.0, tmp_path=tmp_path)
        runner._get_spot = MagicMock(return_value=2800.0)
        pos = runner.enter_condor(runner._config.stocks[0])
        assert pos is not None
        assert len(pos.broker_order_ids) == 4

    def test_entry_kills_switch_blocks(self, tmp_path):
        """Kill switch active should prevent entry."""
        runner, broker, _ = _make_runner(spot=2800.0, tmp_path=tmp_path)
        runner._get_spot = MagicMock(return_value=2800.0)
        runner._kill_switch.activate("test")
        pos = runner.enter_condor(runner._config.stocks[0])
        assert pos is None
        runner._kill_switch.deactivate()


# ── Exit tests ─────────────────────────────────────────────────────────────


class TestCondorExit:
    """Integration tests for CondorRunner.exit_condor()."""

    def _build_position(self, broker: PaperBroker) -> CondorPosition:
        """Helper to build a CondorPosition for exit tests."""
        expiry = _test_expiry()
        sp = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("2650"), "PE")
        lp = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("2594"), "PE")
        sc = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("2950"), "CE")
        lc = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("3006"), "CE")
        return CondorPosition(
            symbol="RELIANCE",
            underlying="RELIANCE",
            expiry=expiry,
            spot_at_entry=2800.0,
            short_put=sp,
            long_put=lp,
            short_call=sc,
            long_call=lc,
            lots=1,
            lot_size=250,
            initial_credit=15.0,
            state="OPEN",
        )

    def test_exit_changes_state_to_closed(self, tmp_path):
        """exit_condor should set position state to CLOSED."""
        runner, broker, _ = _make_runner(tmp_path=tmp_path)
        pos = self._build_position(broker)
        runner._capital_manager.allocate("RELIANCE", 50_000)
        runner.exit_condor(pos, "TEST_EXIT")
        assert pos.state == "CLOSED"

    def test_exit_releases_capital(self, tmp_path):
        """exit_condor should release capital allocation."""
        runner, broker, _ = _make_runner(tmp_path=tmp_path)
        pos = self._build_position(broker)
        runner._capital_manager.allocate("RELIANCE", 50_000)
        assert runner._capital_manager.get_total_allocated() > 0
        runner.exit_condor(pos, "TEST_EXIT")
        assert runner._capital_manager.get_total_allocated() == 0

    def test_exit_skips_non_open_position(self, tmp_path):
        """exit_condor should not place orders for already-closed positions."""
        runner, broker, _ = _make_runner(tmp_path=tmp_path)
        pos = self._build_position(broker)
        pos.state = "CLOSED"
        initial_order_count = len(broker.get_orders())
        runner.exit_condor(pos, "ALREADY_CLOSED")
        # No new orders should have been placed
        assert len(broker.get_orders()) == initial_order_count

    def test_exit_removes_from_positions_list(self, tmp_path):
        """exit_condor should remove the position from the runner's list."""
        runner, broker, _ = _make_runner(tmp_path=tmp_path)
        pos = self._build_position(broker)
        runner._positions.append(pos)
        runner._capital_manager.allocate("RELIANCE", 50_000)
        runner.exit_condor(pos, "TEST_EXIT")
        assert pos not in runner._positions


# ── Shift tests ────────────────────────────────────────────────────────────


class TestCondorShift:
    """Integration tests for CondorRunner.shift_condor()."""

    def test_shift_creates_new_position(self, tmp_path):
        """shift_condor should mutate the position in-place and set shift_count=1."""
        runner, broker, _ = _make_runner(spot=2800.0, tmp_path=tmp_path)
        # Use 2860 so new call target = 2860*1.05=3003 → snaps to 3000 (≠ current 2950)
        runner._get_spot = MagicMock(return_value=2860.0)
        runner._get_now_str = MagicMock(return_value="10:00")

        # Create initial position
        expiry = _test_expiry()
        sp = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("2650"), "PE")
        lp = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("2594"), "PE")
        sc = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("2950"), "CE")
        lc = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("3006"), "CE")
        old_pos = CondorPosition(
            symbol="RELIANCE",
            underlying="RELIANCE",
            expiry=expiry,
            spot_at_entry=2800.0,
            short_put=sp,
            long_put=lp,
            short_call=sc,
            long_call=lc,
            lots=1,
            lot_size=250,
            initial_credit=15.0,
            state="OPEN",
        )
        runner._positions.append(old_pos)
        runner._capital_manager.allocate("RELIANCE", 50_000)

        with patch("live.condor_runner.time.sleep"):
            runner.shift_condor(old_pos, "CALL")

        # shift_condor mutates pos in-place and returns None; state stays OPEN
        assert old_pos.shift_count == 1
        assert old_pos.state == "OPEN"

    def test_shift_increments_shift_count(self, tmp_path):
        """shift_condor should increment the shift_count by 1."""
        runner, broker, _ = _make_runner(spot=2800.0, tmp_path=tmp_path)
        # Use 2860 so new call target = 2860*1.05=3003 → snaps to 3000 (≠ current 2950)
        runner._get_spot = MagicMock(return_value=2860.0)
        runner._get_now_str = MagicMock(return_value="10:00")

        expiry = _test_expiry()
        sp = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("2650"), "PE")
        lp = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("2594"), "PE")
        sc = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("2950"), "CE")
        lc = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("3006"), "CE")
        old_pos = CondorPosition(
            symbol="RELIANCE", underlying="RELIANCE", expiry=expiry, spot_at_entry=2800.0,
            short_put=sp, long_put=lp, short_call=sc, long_call=lc,
            lots=1, lot_size=250, initial_credit=15.0, state="OPEN", shift_count=2,
        )
        runner._positions.append(old_pos)
        runner._capital_manager.allocate("RELIANCE", 50_000)

        with patch("live.condor_runner.time.sleep"):
            runner.shift_condor(old_pos, "CALL")

        assert old_pos.shift_count == 3


# ── ShiftMonitor trigger tests ─────────────────────────────────────────────


class TestShiftMonitorTriggers:
    """Tests for ShiftMonitor._check_position() trigger logic."""

    def _make_position(self, spot: float = 2800.0) -> CondorPosition:
        """Create a dummy CondorPosition for shift checks."""
        expiry = _test_expiry()
        broker = PaperBroker(spot=spot)
        sp = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("2650"), "PE")
        lp = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("2594"), "PE")
        sc = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("2950"), "CE")
        lc = broker.resolve_instrument("RELIANCE", "NFO", expiry, Decimal("3006"), "CE")
        return CondorPosition(
            symbol="RELIANCE", underlying="RELIANCE", expiry=expiry, spot_at_entry=spot,
            short_put=sp, long_put=lp, short_call=sc, long_call=lc,
            lots=1, lot_size=250, initial_credit=15.0, state="OPEN",
        )

    def test_no_trigger_within_bounds(self, tmp_path):
        """No shift or TP should trigger when spot is within condor bounds."""
        config = _make_live_config()
        broker = PaperBroker(spot=2800.0)
        tl = TradeLogger(log_dir=str(tmp_path), symbol="RELIANCE")
        monitor = ShiftMonitor(
            broker=broker,
            positions=[],
            config=config,
            logger_=tl,
            on_shift_cb=MagicMock(),
        )
        pos = self._make_position(2800.0)
        should_act, reason = monitor._check_position(pos)
        assert not should_act, f"Should not trigger at spot=2800 within [2650, 2950]. Got: {reason}"

    def test_shift_put_triggers_below_threshold(self, tmp_path):
        """Spot below short_put_strike × (1 - shift_trigger_pct/100) should trigger PUT shift."""
        config = _make_live_config()
        # short_put=2650, shift_trigger=1.5% → breach at 2650 * (1 - 0.015) = 2610.25
        spot = 2600.0
        broker = PaperBroker(spot=spot)
        tl = TradeLogger(log_dir=str(tmp_path), symbol="RELIANCE")
        monitor = ShiftMonitor(
            broker=broker,
            positions=[],
            config=config,
            logger_=tl,
            on_shift_cb=MagicMock(),
        )
        pos = self._make_position(2800.0)
        # Mock _get_current_spot to return the low spot so the shift triggers
        monitor._get_current_spot = MagicMock(return_value=spot)
        should_act, reason = monitor._check_position(pos)
        assert should_act, f"Should trigger SHIFT_PUT at spot={spot}"
        assert "SHIFT_PUT" in reason

    def test_shift_call_triggers_above_threshold(self, tmp_path):
        """Spot above short_call_strike × (1 + shift_trigger_pct/100) should trigger CALL shift."""
        config = _make_live_config()
        # short_call=2950, shift_trigger=1.5% → breach at 2950 * 1.015 = 2994.25
        spot = 3000.0
        broker = PaperBroker(spot=spot)
        tl = TradeLogger(log_dir=str(tmp_path), symbol="RELIANCE")
        monitor = ShiftMonitor(
            broker=broker,
            positions=[],
            config=config,
            logger_=tl,
            on_shift_cb=MagicMock(),
        )
        pos = self._make_position(2800.0)
        # Mock _get_current_spot to return the high spot so the shift triggers
        monitor._get_current_spot = MagicMock(return_value=spot)
        should_act, reason = monitor._check_position(pos)
        assert should_act, f"Should trigger SHIFT_CALL at spot={spot}"
        assert "SHIFT_CALL" in reason

    def test_dte_exit_triggers_near_expiry(self, tmp_path):
        """DTE_EXIT should fire when position is within dte_exit days of expiry."""
        config = _make_live_config()
        broker = PaperBroker(spot=2800.0)
        tl = TradeLogger(log_dir=str(tmp_path), symbol="RELIANCE")
        monitor = ShiftMonitor(
            broker=broker,
            positions=[],
            config=config,
            logger_=tl,
            on_shift_cb=MagicMock(),
        )
        # Build a position expiring in 1 day (below dte_exit threshold of 2)
        pos = self._make_position(2800.0)
        # Override expiry to be imminent
        pos.expiry = date.today() + timedelta(days=1)

        should_act, reason = monitor._check_position(pos)
        assert should_act, f"DTE_EXIT should fire for expiry in 1 day. Reason: {reason}"
        assert "DTE_EXIT" in reason


# ── Capital manager tests ──────────────────────────────────────────────────


class TestCapitalManagerIntegration:
    """Integration tests for CapitalManager used within CondorRunner."""

    def test_allocate_and_release_cycle(self):
        """Full allocate → release cycle should return capital correctly."""
        config = CapitalConfig(
            total_capital=1_000_000,
            cash_margin_pct=0.5,
            max_capital_per_stock=200_000,
            max_open_positions=5,
        )
        mgr = CapitalManager(config)

        assert mgr.get_available_capital() == 1_000_000
        assert mgr.allocate("RELIANCE", 100_000)
        assert mgr.get_available_capital() == 900_000
        assert mgr.get_total_allocated() == 100_000

        released = mgr.release("RELIANCE")
        assert released == 100_000
        assert mgr.get_available_capital() == 1_000_000

    def test_allocate_respects_per_stock_limit(self):
        """Allocating above max_capital_per_stock should fail."""
        config = CapitalConfig(
            total_capital=1_000_000,
            cash_margin_pct=0.5,
            max_capital_per_stock=50_000,
            max_open_positions=5,
        )
        mgr = CapitalManager(config)
        assert not mgr.allocate("RELIANCE", 100_000)  # exceeds 50k limit

    def test_allocate_respects_total_capital(self):
        """Allocating above total_capital should fail."""
        config = CapitalConfig(
            total_capital=100_000,
            cash_margin_pct=0.5,
            max_capital_per_stock=200_000,
            max_open_positions=5,
        )
        mgr = CapitalManager(config)
        assert not mgr.allocate("RELIANCE", 150_000)  # exceeds total

    def test_daily_loss_check(self):
        """Daily loss below -20% should return False."""
        config = CapitalConfig(
            total_capital=1_000_000,
            cash_margin_pct=0.5,
            max_capital_per_stock=200_000,
            max_open_positions=5,
        )
        mgr = CapitalManager(config)
        assert mgr.check_daily_loss(-100_000)   # -10% → within limit
        assert not mgr.check_daily_loss(-300_000)  # -30% → breach

    def test_lots_calculation(self):
        """get_lots_for_capital should return a positive integer."""
        config = CapitalConfig(
            total_capital=2_000_000,
            cash_margin_pct=0.5,
            max_capital_per_stock=500_000,
            max_open_positions=10,
        )
        mgr = CapitalManager(config)
        lots = mgr.get_lots_for_capital("RELIANCE", spot=2800.0, lot_size=250, wing_width_pct=2.0)
        assert lots >= 1


# ── Snap-to-strike tests ───────────────────────────────────────────────────


class TestSnapToStrike:
    """Tests for CondorRunner._snap_to_strike()."""

    def test_exact_match(self, tmp_path):
        runner, _, _ = _make_runner(tmp_path=tmp_path)
        available = [2600.0, 2650.0, 2700.0]
        assert runner._snap_to_strike(2650.0, available) == 2650.0

    def test_rounds_down(self, tmp_path):
        runner, _, _ = _make_runner(tmp_path=tmp_path)
        available = [2600.0, 2650.0, 2700.0]
        # 2620 is closer to 2600 than 2650
        assert runner._snap_to_strike(2620.0, available) == 2600.0

    def test_rounds_up(self, tmp_path):
        runner, _, _ = _make_runner(tmp_path=tmp_path)
        available = [2600.0, 2650.0, 2700.0]
        # 2680 is closer to 2700 than 2650
        assert runner._snap_to_strike(2680.0, available) == 2700.0

    def test_empty_list_returns_target(self, tmp_path):
        runner, _, _ = _make_runner(tmp_path=tmp_path)
        assert runner._snap_to_strike(2650.0, []) == 2650.0
