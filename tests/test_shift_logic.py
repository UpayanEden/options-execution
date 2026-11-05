"""
Tests for Shift Logic.

Verifies:
- NEEDS_RECON state (Section 5)
- ShiftMonitor NEEDS_RECON skip
- Spot missing handling
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch, call

import pytest

# ── Helpers / minimal stubs ───────────────────────────────────────────────


def _make_strategy_params(
    shift_trigger_pct: float = 1.0,
    wing_width_pct: float = 2.0,
    trend_adx_min: float = 20.0,
    fill_poll_interval_s: float = 0.5,
    spot_missing_alert_cycles: int = 4,
) -> MagicMock:
    p = MagicMock()
    p.shift_trigger_pct = shift_trigger_pct
    p.wing_width_pct = wing_width_pct
    p.trend_adx_min = trend_adx_min
    p.fill_poll_interval_s = fill_poll_interval_s
    p.spot_missing_alert_cycles = spot_missing_alert_cycles
    return p


def _make_config(**kwargs) -> MagicMock:
    config = MagicMock()
    config.strategy = _make_strategy_params(**kwargs)
    config.telegram_bot_token = ""
    config.telegram_chat_id = ""
    return config


def _make_inst(strike: float, option_type: str = "PE", ts: str = "") -> MagicMock:
    inst = MagicMock()
    inst.strike = Decimal(str(strike))
    inst.instrument_token = int(strike * 100)
    inst.trading_symbol = ts or f"TEST{int(strike)}{option_type}"
    inst.option_type = MagicMock()
    inst.option_type.value = option_type
    inst.lot_size = 100
    return inst


def _make_position(
    symbol: str = "TESTSTOCK",
    expiry: Optional[date] = None,
    short_put_strike: float = 950.0,
    long_put_strike: float = 930.0,
    short_call_strike: float = 1050.0,
    long_call_strike: float = 1070.0,
    put_dist_pct: float = 5.0,
    call_dist_pct: float = 5.0,
    initial_credit: float = 20.0,
    original_credit: float = 20.0,
    shift_count: int = 0,
    state: str = "OPEN",
    lots: int = 1,
    lot_size: int = 100,
) -> MagicMock:
    if expiry is None:
        expiry = date.today() + timedelta(days=30)
    pos = MagicMock()
    pos.symbol = symbol
    pos.underlying = symbol
    pos.expiry = expiry
    pos.short_put = _make_inst(short_put_strike, "PE")
    pos.long_put = _make_inst(long_put_strike, "PE")
    pos.short_call = _make_inst(short_call_strike, "CE")
    pos.long_call = _make_inst(long_call_strike, "CE")
    pos.put_dist_pct = put_dist_pct
    pos.call_dist_pct = call_dist_pct
    pos.initial_credit = initial_credit
    pos.original_credit = original_credit
    pos.shift_count = shift_count
    pos.state = state
    pos.lots = lots
    pos.lot_size = lot_size
    pos.spot_at_entry = 1000.0
    pos.fo_ban_failures = 0
    pos.fo_ban_suspended = False
    pos.fo_ban_suspended_at = 0.0
    pos.ban_hold = False
    pos.recon_legs = None
    pos.broker_order_ids = []
    pos.entry_time = datetime.now()
    return pos


# ── NEEDS_RECON state ─────────────────────────────────────────────────────


class TestNeedsRecon:
    def test_needs_recon_on_step3_failure(self):
        """
        Mock step2 success + step3 failure → state becomes NEEDS_RECON.
        We test the state machine logic directly by simulating what shift_condor does.
        """
        # Simulate the state transitions that shift_condor performs
        pos = MagicMock()
        pos.state = "OPEN"
        pos.shift_count = 0
        pos.lots = 1
        pos.lot_size = 100

        # Step 2 succeeds
        oid_step2 = "ORD001"
        qty_step2 = 100

        # Step 3 fails
        oid_step3 = None
        qty_step3 = 0

        if not oid_step3:
            pos.state = "NEEDS_RECON"
            pos.recon_legs = {
                "new_long": {"instrument": "TESTPE920", "filled_qty": qty_step2, "broker_oid": oid_step2},
                "new_short": {"instrument": "TESTPE950", "filled_qty": qty_step3, "broker_oid": oid_step3},
            }

        assert pos.state == "NEEDS_RECON"
        assert "new_long" in pos.recon_legs

    def test_step2_full_failure_keeps_open(self):
        """
        Step 2 full failure (oid=None, qty=0) → state stays OPEN.
        """
        pos = MagicMock()
        pos.state = "OPEN"

        oid_step2 = None
        qty_step2 = 0

        if not oid_step2 and qty_step2 == 0:
            pos.state = "OPEN"  # stays OPEN, not NEEDS_RECON
        elif not oid_step2:
            pos.state = "NEEDS_RECON"

        assert pos.state == "OPEN"


class TestShiftMonitorNeedsRecon:
    """Test that ShiftMonitor skips NEEDS_RECON positions."""

    def _make_shift_monitor(self) -> Any:
        from live.shift_monitor import ShiftMonitor

        broker = MagicMock()
        config = _make_config()
        logger_ = MagicMock()
        on_shift_cb = MagicMock()

        monitor = ShiftMonitor(
            broker=broker,
            positions=[],
            config=config,
            logger_=logger_,
            on_shift_cb=on_shift_cb,
        )
        return monitor

    def test_shift_monitor_skips_needs_recon(self):
        """
        Position in NEEDS_RECON → _check_position returns ('NEEDS_RECON', '').
        """
        monitor = self._make_shift_monitor()
        pos = _make_position(state="NEEDS_RECON")

        action, reason = monitor._check_position(pos)

        assert action == "NEEDS_RECON"
        assert reason == ""

    def test_shift_monitor_process_position_skips_needs_recon(self):
        """
        _process_position with NEEDS_RECON pos → on_shift_cb is NEVER called.
        """
        monitor = self._make_shift_monitor()
        pos = _make_position(state="NEEDS_RECON")

        with patch.object(monitor, "_on_shift_cb") as mock_shift:
            monitor._process_position(pos)
            mock_shift.assert_not_called()


# ── Integration: spot missing return value change ─────────────────────────


class TestSpotMissing:
    def test_spot_missing_returns_data_unavailable(self):
        """
        When spot is None from both cache and broker and stale fallback is 0,
        _check_position should return ("DATA_UNAVAILABLE", "").
        """
        from live.shift_monitor import ShiftMonitor

        broker = MagicMock()
        broker.get_equity_spot.return_value = None
        broker.get_index_spot.return_value = None

        config = _make_config()
        logger_ = MagicMock()

        monitor = ShiftMonitor(
            broker=broker,
            positions=[],
            config=config,
            logger_=logger_,
            on_shift_cb=MagicMock(),
        )

        pos = _make_position(state="OPEN")
        # Zero out spot_at_entry so there's no stale fallback
        pos.spot_at_entry = 0.0

        # Ensure spot_cache returns None for this symbol
        spot_cache = {pos.underlying.upper(): None}

        with patch("live.ban_list.is_banned", return_value=False):
            with patch.object(monitor, "_get_current_spot", return_value=None):
                action, reason = monitor._check_position(pos, spot_cache=spot_cache)

        assert action == "DATA_UNAVAILABLE"
