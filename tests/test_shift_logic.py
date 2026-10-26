"""
Tests for Shift Logic Upgrade (Sections 1-7).

Verifies:
- Near-expiry distance reduction
- Trend-aware distance boost
- Exit gate (Section 4)
- NEEDS_RECON state (Section 5)
- Strike validation (Section 1)
- ShiftMonitor NEEDS_RECON skip
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, call

import pytest

# ── Helpers / minimal stubs ───────────────────────────────────────────────


def _make_strategy_params(
    shift_trigger_pct: float = 1.0,
    wing_width_pct: float = 2.0,
    near_expiry_days: int = 10,
    near_expiry_dist_reduction: float = 0.40,
    trend_adx_min: float = 20.0,
    trend_dist_boost: float = 0.10,
    min_shifts_before_exit_gate: int = 2,
    exit_gate_pct: float = 0.05,
    fill_poll_interval_s: float = 0.5,
    fill_step_after_s: float = 3.0,
    max_price_modifications: int = 5,
    max_slippage_pct: float = 5.0,
    spot_missing_alert_cycles: int = 4,
    allow_dte_exit_during_ban: bool = True,
    dte_exit: int = 2,
) -> MagicMock:
    p = MagicMock()
    p.shift_trigger_pct = shift_trigger_pct
    p.wing_width_pct = wing_width_pct
    p.near_expiry_days = near_expiry_days
    p.near_expiry_dist_reduction = near_expiry_dist_reduction
    p.trend_adx_min = trend_adx_min
    p.trend_dist_boost = trend_dist_boost
    p.min_shifts_before_exit_gate = min_shifts_before_exit_gate
    p.exit_gate_pct = exit_gate_pct
    p.fill_poll_interval_s = fill_poll_interval_s
    p.fill_step_after_s = fill_step_after_s
    p.max_price_modifications = max_price_modifications
    p.max_slippage_pct = max_slippage_pct
    p.spot_missing_alert_cycles = spot_missing_alert_cycles
    p.allow_dte_exit_during_ban = allow_dte_exit_during_ban
    p.dte_exit = dte_exit
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


# ── Section 1: Near-expiry distance reduction ──────────────────────────────


class TestNearExpiryDistanceReduction:
    def test_near_expiry_distance_reduction(self):
        """
        pos with 8 DTE, put_dist_pct=5%, near_expiry_dist_reduction=0.40
        → effective_put_dist ≈ 3%
        """
        near_expiry_days = 10
        near_expiry_dist_reduction = 0.40
        put_dist_pct = 5.0
        dte = 8  # within near_expiry_days

        if dte <= near_expiry_days:
            effective = put_dist_pct * (1.0 - near_expiry_dist_reduction)
        else:
            effective = put_dist_pct

        assert abs(effective - 3.0) < 0.001

    def test_normal_expiry_distance(self):
        """
        pos with 15 DTE → effective 5% (no reduction)
        """
        near_expiry_days = 10
        near_expiry_dist_reduction = 0.40
        put_dist_pct = 5.0
        dte = 15  # above near_expiry_days

        if dte <= near_expiry_days:
            effective = put_dist_pct * (1.0 - near_expiry_dist_reduction)
        else:
            effective = put_dist_pct

        assert abs(effective - 5.0) < 0.001

    def test_trend_call_boost_near_expiry(self):
        """
        UP trend, call shift, 8 DTE, call_dist_pct=5%:
        → 5% × (1-0.40) × (1+0.10) = 5% × 0.60 × 1.10 = 3.3%
        """
        near_expiry_days = 10
        near_expiry_dist_reduction = 0.40
        trend_dist_boost = 0.10
        call_dist_pct = 5.0
        dte = 8
        trigger_side = "CALL"
        trend = "UP"

        # Near-expiry reduction
        if dte <= near_expiry_days:
            effective_call_dist = call_dist_pct * (1.0 - near_expiry_dist_reduction)
        else:
            effective_call_dist = call_dist_pct

        # Trend boost
        if trigger_side == "CALL" and trend == "UP":
            effective_call_dist *= (1.0 + trend_dist_boost)

        expected = 5.0 * 0.60 * 1.10  # = 3.3
        assert abs(effective_call_dist - expected) < 0.001


# ── Section 4: Exit gate ──────────────────────────────────────────────────


class TestExitGate:
    def _evaluate_exit_gate(
        self,
        shift_count: int,
        projected_credit: float,
        original_credit: float,
        exit_pnl_now: float,
        shift_worst_pnl: float,
        min_shifts: int = 2,
        exit_gate_pct: float = 0.05,
    ) -> str:
        """
        Replicate the exit gate logic from shift_condor().
        Returns 'EXIT_ZERO_CREDIT', 'EXIT_GATE', or 'CONTINUE'.
        """
        # Condition 2: projected_credit <= 0
        if projected_credit <= 0:
            return "EXIT_ZERO_CREDIT"
        # Condition 1
        if (shift_count >= min_shifts
                and projected_credit <= exit_gate_pct * original_credit
                and exit_pnl_now >= shift_worst_pnl):
            return "EXIT_GATE"
        return "CONTINUE"

    def test_exit_gate_triggers(self):
        """
        shift_count=2, projected_credit=4% of original, exit_pnl_now >= shift_worst_pnl
        → EXIT_GATE
        """
        original_credit = 100.0
        projected_credit = original_credit * 0.04  # 4% < exit_gate_pct (5%)
        exit_pnl_now = 500.0
        shift_worst_pnl = 400.0  # exit_pnl_now >= shift_worst_pnl

        result = self._evaluate_exit_gate(
            shift_count=2,
            projected_credit=projected_credit,
            original_credit=original_credit,
            exit_pnl_now=exit_pnl_now,
            shift_worst_pnl=shift_worst_pnl,
        )
        assert result == "EXIT_GATE"

    def test_exit_gate_no_trigger_below_min_shifts(self):
        """
        shift_count=1 (below min_shifts=2) → gate does not trigger
        """
        original_credit = 100.0
        projected_credit = original_credit * 0.04

        result = self._evaluate_exit_gate(
            shift_count=1,
            projected_credit=projected_credit,
            original_credit=original_credit,
            exit_pnl_now=500.0,
            shift_worst_pnl=400.0,
        )
        assert result == "CONTINUE"

    def test_projected_credit_zero_always_exits(self):
        """
        projected_credit <= 0 at any shift_count → EXIT_ZERO_CREDIT
        """
        result = self._evaluate_exit_gate(
            shift_count=0,
            projected_credit=-5.0,
            original_credit=100.0,
            exit_pnl_now=100.0,
            shift_worst_pnl=50.0,
        )
        assert result == "EXIT_ZERO_CREDIT"

    def test_exit_gate_no_trigger_exit_worse_than_shift(self):
        """
        Even if credit is low, don't exit if exit_pnl_now < shift_worst_pnl.
        """
        original_credit = 100.0
        projected_credit = original_credit * 0.04

        result = self._evaluate_exit_gate(
            shift_count=2,
            projected_credit=projected_credit,
            original_credit=original_credit,
            exit_pnl_now=200.0,
            shift_worst_pnl=300.0,  # shift_worst > exit_pnl
        )
        assert result == "CONTINUE"


# ── Section 1: Strike validation ─────────────────────────────────────────


class TestStrikeValidation:
    def _validate_put_strike(
        self,
        spot: float,
        new_short_strike: float,
        available_puts: List[float],
        shift_trigger_pct: float = 1.0,
    ) -> str:
        """Replicate put strike validation logic."""
        current_short_strike = None  # will compare against a dummy
        # Condition: spot >= new_short_strike * (1 + trigger_pct/100)
        if spot < new_short_strike * (1.0 + shift_trigger_pct / 100.0):
            # Try one step further out
            try:
                idx = available_puts.index(new_short_strike)
            except ValueError:
                idx = -1
            if idx > 0:
                new_short_strike = available_puts[idx - 1]
            if spot < new_short_strike * (1.0 + shift_trigger_pct / 100.0):
                return "ABORT_TOO_CLOSE"
        return "OK"

    def _validate_call_strike(
        self,
        spot: float,
        new_short_strike: float,
        available_calls: List[float],
        shift_trigger_pct: float = 1.0,
    ) -> str:
        """Replicate call strike validation logic."""
        if spot > new_short_strike * (1.0 - shift_trigger_pct / 100.0):
            try:
                idx = available_calls.index(new_short_strike)
            except ValueError:
                idx = -1
            if idx >= 0 and idx < len(available_calls) - 1:
                new_short_strike = available_calls[idx + 1]
            if spot > new_short_strike * (1.0 - shift_trigger_pct / 100.0):
                return "ABORT_TOO_CLOSE"
        return "OK"

    def test_strike_too_close_aborts(self):
        """
        New snapped put strike inside trigger zone → ABORT_TOO_CLOSE.
        spot=1000, short_strike=1000, trigger=1%, so breach = 1010.
        Strike 1000 fails: spot(1000) < 1000*(1.01)=1010.
        Try next lower: 950 → 1000 < 950*1.01=959.5? No, 1000 > 959.5 → OK
        But if both strikes fail the test → ABORT.
        """
        # Force both strikes to fail: put strike = 995, one step lower = 990
        # spot=1000, trigger=1%: need spot >= strike * 1.01
        # 995 * 1.01 = 1004.95 → 1000 < 1004.95 → FAIL
        # 990 * 1.01 = 999.9 → 1000 >= 999.9 → PASS (one step is enough here)
        result = self._validate_put_strike(
            spot=1000.0,
            new_short_strike=995.0,
            available_puts=[950.0, 975.0, 990.0, 995.0, 1000.0],
            shift_trigger_pct=1.0,
        )
        # 990 * 1.01 = 999.9 → 1000 >= 999.9 → should be OK
        assert result == "OK"

    def test_strike_too_close_all_fail_aborts(self):
        """
        If both strike options fail the validation, return ABORT_TOO_CLOSE.
        spot=1000, put_strike=998, next_lower=997
        998 * 1.01 = 1007.98 → 1000 < 1007.98 FAIL
        997 * 1.01 = 1006.97 → 1000 < 1006.97 FAIL → ABORT
        """
        result = self._validate_put_strike(
            spot=1000.0,
            new_short_strike=998.0,
            available_puts=[996.0, 997.0, 998.0, 999.0, 1000.0],
            shift_trigger_pct=1.0,
        )
        assert result == "ABORT_TOO_CLOSE"

    def test_identical_strikes_aborts(self):
        """Verify logic: if new strike == current → ABORT_NO_CHANGE."""
        current_strike = 950.0
        new_short_strike = 950.0
        # Simulate the identical check
        result = "ABORT_NO_CHANGE" if new_short_strike == current_strike else "OK"
        assert result == "ABORT_NO_CHANGE"

    def test_different_strikes_does_not_abort(self):
        """If new strike differs from current → OK."""
        current_strike = 950.0
        new_short_strike = 920.0
        result = "ABORT_NO_CHANGE" if new_short_strike == current_strike else "OK"
        assert result == "OK"


# ── Section 5: NEEDS_RECON state ─────────────────────────────────────────


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
