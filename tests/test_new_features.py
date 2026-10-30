"""
Tests for the 6 new features:
  Feature 1: 85% net-premium profit target exit
  Feature 3: 25% minimum premium-to-width filter at entry
  Feature 6: No re-entry after exit for same expiry cycle
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_config(profit_target_pct: float = 85.0, min_entry_premium_pct: float = 25.0):
    """Return a minimal LiveConfig mock with the strategy fields we test."""
    cfg = MagicMock()
    cfg.strategy.profit_target_pct = profit_target_pct
    cfg.strategy.min_entry_premium_pct = min_entry_premium_pct
    cfg.strategy.shift_trigger_pct = 1.0
    cfg.strategy.dte_exit = 2
    cfg.strategy.near_expiry_days = 10
    cfg.strategy.near_expiry_dist_reduction = 0.40
    cfg.strategy.spot_missing_alert_cycles = 4
    cfg.strategy.allow_dte_exit_during_ban = True
    return cfg


def _make_pos(
    symbol: str = "RELIANCE",
    original_credit: float = 10.0,
    short_put_ts: str = "RELIANCE2610PE2800",
    long_put_ts: str  = "RELIANCE2610PE2600",
    short_call_ts: str = "RELIANCE2610CE3100",
    long_call_ts: str  = "RELIANCE2610CE3300",
    short_put_strike: float = 2800.0,
    long_put_strike: float  = 2600.0,
    expiry: date = date(2026, 10, 30),
):
    """Return a minimal CondorPosition-like mock."""
    pos = MagicMock()
    pos.symbol = symbol
    pos.original_credit = original_credit
    pos.state = "OPEN"
    pos.expiry = expiry

    def _inst(ts, strike):
        inst = MagicMock()
        inst.trading_symbol = ts
        inst.strike = Decimal(str(strike))
        return inst

    pos.short_put  = _inst(short_put_ts, short_put_strike)
    pos.long_put   = _inst(long_put_ts,  long_put_strike)
    pos.short_call = _inst(short_call_ts, 3100.0)
    pos.long_call  = _inst(long_call_ts,  3300.0)
    pos.underlying = symbol
    pos.ban_hold = False
    return pos


def _make_monitor(config=None):
    """Return a ShiftMonitor with broker/logger stubs."""
    from live.shift_monitor import ShiftMonitor

    broker = MagicMock()
    broker.get_equity_spots_batch = MagicMock(return_value={})
    # Simulate no position on ban list
    trade_logger = MagicMock()
    ks = MagicMock()
    ks.is_active.return_value = False

    cfg = config or _make_config()

    monitor = ShiftMonitor(
        broker=broker,
        positions=[],
        config=cfg,
        logger_=trade_logger,
        on_shift_cb=MagicMock(),
        kill_switch=ks,
        on_exit_cb=MagicMock(),
    )
    return monitor


# ──────────────────────────────────────────────────────────────────────────────
# Feature 1: Profit target exit
# ──────────────────────────────────────────────────────────────────────────────


def test_profit_target_exit_fires_at_85_pct():
    """
    original_credit=10.0; current short bid=0.8+0.7=1.5; long ask=0+0 → 0;
    current_credit=1.5; profit_captured=10.0-1.5=8.5 ≥ 85% of 10.0=8.5 → fires
    """
    monitor = _make_monitor(_make_config(profit_target_pct=85.0))
    pos = _make_pos(original_credit=10.0)

    sp_ts  = pos.short_put.trading_symbol
    sc_ts  = pos.short_call.trading_symbol
    lp_ts  = pos.long_put.trading_symbol
    lc_ts  = pos.long_call.trading_symbol

    options_cache = {
        f"{sp_ts}:bid": 0.8,
        f"{sp_ts}:ask": 0.9,
        f"{sp_ts}:ltp": 0.85,
        f"{sc_ts}:bid": 0.7,
        f"{sc_ts}:ask": 0.75,
        f"{sc_ts}:ltp": 0.72,
        f"{lp_ts}:bid": 0.0,
        f"{lp_ts}:ask": 0.0,
        f"{lp_ts}:ltp": 0.0,
        f"{lc_ts}:bid": 0.0,
        f"{lc_ts}:ask": 0.0,
        f"{lc_ts}:ltp": 0.0,
    }

    # Patch ban list to return not banned
    with patch("live.shift_monitor.ShiftMonitor._check_position",
               wraps=monitor._check_position):
        # Provide a far-future expiry so DTE check doesn't fire
        pos.expiry = date(2027, 3, 31)
        # Patch get_current_spot to return a valid spot far from strikes
        monitor._get_current_spot = MagicMock(return_value=2900.0)
        # Patch ban list
        with patch("live.ban_list.is_banned", return_value=False):
            action, reason = monitor._check_position(
                pos,
                spot_cache={"RELIANCE": 2900.0},
                all_spots_null=False,
                options_cache=options_cache,
            )

    assert action == "PROFIT_TARGET_EXIT", f"Expected PROFIT_TARGET_EXIT, got {action!r}"
    assert "profit=" in reason


def test_profit_target_does_not_fire_below_threshold():
    """
    original_credit=10.0; current_credit=3.0; profit_captured=7.0 < 85% (8.5) → no fire
    """
    monitor = _make_monitor(_make_config(profit_target_pct=85.0))
    pos = _make_pos(original_credit=10.0)
    pos.expiry = date(2027, 3, 31)

    sp_ts  = pos.short_put.trading_symbol
    sc_ts  = pos.short_call.trading_symbol
    lp_ts  = pos.long_put.trading_symbol
    lc_ts  = pos.long_call.trading_symbol

    options_cache = {
        f"{sp_ts}:bid": 1.5,
        f"{sp_ts}:ask": 1.6,
        f"{sp_ts}:ltp": 1.55,
        f"{sc_ts}:bid": 1.5,
        f"{sc_ts}:ask": 1.6,
        f"{sc_ts}:ltp": 1.55,
        f"{lp_ts}:bid": 0.0,
        f"{lp_ts}:ask": 0.0,
        f"{lp_ts}:ltp": 0.0,
        f"{lc_ts}:bid": 0.0,
        f"{lc_ts}:ask": 0.0,
        f"{lc_ts}:ltp": 0.0,
    }

    monitor._get_current_spot = MagicMock(return_value=2900.0)
    with patch("live.ban_list.is_banned", return_value=False):
        action, reason = monitor._check_position(
            pos,
            spot_cache={"RELIANCE": 2900.0},
            all_spots_null=False,
            options_cache=options_cache,
        )

    # Should not fire profit target (current_credit=3.0, profit=7.0 < 8.5)
    assert action != "PROFIT_TARGET_EXIT", f"Should not fire, got {action!r}"


def test_profit_target_skipped_when_options_cache_none():
    """When options_cache is None the profit target check is silently skipped."""
    monitor = _make_monitor(_make_config(profit_target_pct=85.0))
    pos = _make_pos(original_credit=10.0)
    pos.expiry = date(2027, 3, 31)
    monitor._get_current_spot = MagicMock(return_value=2900.0)
    with patch("live.ban_list.is_banned", return_value=False):
        action, _ = monitor._check_position(
            pos,
            spot_cache={"RELIANCE": 2900.0},
            all_spots_null=False,
            options_cache=None,
        )
    assert action != "PROFIT_TARGET_EXIT"


# ──────────────────────────────────────────────────────────────────────────────
# Feature 3: Minimum premium-to-width filter
# ──────────────────────────────────────────────────────────────────────────────


def test_min_premium_pct_blocks_entry_when_too_low():
    """
    Test the premium filter logic directly: net_credit=2.0, spread_width=20 → 10% < 25% → blocks.
    We test the filter condition, not the full condor_runner flow.
    """
    min_prem_pct_threshold = 25.0
    spread_width = 20.0
    net_credit = 2.0

    premium_pct = net_credit / spread_width * 100  # = 10%
    min_prem_pct = min_prem_pct_threshold / 100.0

    # The condition that triggers the block:
    should_block = spread_width > 0 and (net_credit / spread_width) < min_prem_pct
    assert should_block, f"Expected block: {premium_pct:.1f}% < {min_prem_pct_threshold}%"


def test_min_premium_pct_allows_entry_when_sufficient():
    """
    Test the premium filter logic directly: net_credit=5.5, spread_width=20 → 27.5% ≥ 25% → allows.
    """
    min_prem_pct_threshold = 25.0
    spread_width = 20.0
    net_credit = 5.5

    premium_pct = net_credit / spread_width * 100  # = 27.5%
    min_prem_pct = min_prem_pct_threshold / 100.0

    # The condition that triggers the block should be False:
    should_block = spread_width > 0 and (net_credit / spread_width) < min_prem_pct
    assert not should_block, f"Expected no block: {premium_pct:.1f}% ≥ {min_prem_pct_threshold}%"


def test_min_premium_filter_in_condor_runner(tmp_path):
    """
    Integration test: verify the premium filter inside enter_condor by patching
    the quotes to produce a low-premium scenario and confirming ENTRY_FAILED is logged.
    We patch at the level right before the filter runs.
    """
    from decimal import Decimal
    from datetime import date, timedelta
    from unittest.mock import MagicMock, patch, call

    from live.condor_runner import CondorRunner
    from live.trade_logger import EventType

    cfg = MagicMock()
    cfg.mode = "paper"
    cfg.strategy.min_dte = 20
    cfg.strategy.max_dte = 45
    cfg.strategy.entry_start = "09:00"
    cfg.strategy.entry_end   = "15:00"
    cfg.strategy.wing_width_pct = 2.0
    cfg.strategy.min_entry_premium_pct = 25.0
    cfg.strategy.exit_gate_pct = 0.05
    cfg.strategy.min_shifts_before_exit_gate = 2
    cfg.strategy.default_lots = 0
    cfg.capital.max_open_positions = 10
    cfg.enabled_stocks = []

    broker = MagicMock()
    future_expiry = date.today() + timedelta(days=30)

    # 4 distinct instruments with distinct tokens
    def _make_inst(token, ts, strike):
        inst = MagicMock()
        inst.instrument_token = token
        inst.trading_symbol = ts
        inst.lot_size = 100
        inst.tick_size = Decimal("0.05")
        inst.strike = Decimal(str(strike))
        inst.option_type = MagicMock(value="PE")
        return inst

    insts = [
        _make_inst(1, "SP", 950.0),   # short_put
        _make_inst(2, "LP", 930.0),   # long_put
        _make_inst(3, "SC", 1050.0),  # short_call
        _make_inst(4, "LC", 1070.0),  # long_call
    ]
    broker.resolve_instrument.side_effect = insts

    # net credit = 1.0+1.0-0.0-0.0 = 2.0; spread_width = 950-930=20 → 10% < 25%
    from datetime import datetime
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")

    def _quotes(pairs):
        _now = datetime.now(_IST)
        q_short = MagicMock(); q_short.bid = Decimal("1.0"); q_short.ask = Decimal("1.1"); q_short.timestamp = _now
        q_long  = MagicMock(); q_long.bid  = Decimal("0.0"); q_long.ask  = Decimal("0.0"); q_long.timestamp = _now
        result = {}
        for tok, _ in pairs:
            if tok in (1, 3):
                result[tok] = q_short
            else:
                result[tok] = q_long
        return result

    broker.get_quotes = _quotes
    broker.get_margins = MagicMock(return_value={"equity": {"available": {"live_balance": 100000}}})
    broker.get_basket_margin = MagicMock(return_value={})

    trade_logger = MagicMock()
    ks = MagicMock(); ks.check = MagicMock(); ks.is_active = MagicMock(return_value=False)
    capital_mgr = MagicMock()
    capital_mgr.get_lots_for_capital = MagicMock(return_value=1)
    capital_mgr.estimate_margin = MagicMock(return_value=5000.0)
    capital_mgr.get_total_allocated = MagicMock(return_value=0.0)

    runner = CondorRunner(broker=broker, config=cfg, trade_logger=trade_logger,
                          capital_manager=capital_mgr, kill_switch=ks)
    runner._position_store = MagicMock()
    runner._position_store.is_expiry_exited = MagicMock(return_value=False)

    stock = MagicMock()
    stock.symbol = "TEST"
    stock.put_dist_pct = 5.0
    stock.call_dist_pct = 5.0
    stock.lots = 0
    stock.expiry_type = "monthly"

    with (
        patch.object(runner, "_get_spot", return_value=1000.0),
        patch.object(runner, "_find_expiry", return_value=future_expiry),
        patch.object(runner, "_get_available_strikes",
                     return_value=([930.0, 950.0, 970.0], [1030.0, 1050.0, 1070.0])),
        patch.object(runner, "_snap_to_strike", side_effect=lambda t, av: min(av, key=lambda x: abs(x-t))),
        patch.object(runner, "_get_broker_positions", return_value=[]),
        patch.object(runner, "_get_recent_orders", return_value=[]),
    ):
        result = runner.enter_condor(stock)

    assert result is None, "Expected None (blocked by premium filter)"
    # Verify ENTRY_FAILED with insufficient_premium was logged
    logged_data = [str(c) for c in trade_logger.log.call_args_list]
    assert any("insufficient_premium" in c for c in logged_data), \
        "Expected insufficient_premium in ENTRY_FAILED log"


# ──────────────────────────────────────────────────────────────────────────────
# Feature 6: No re-entry after exit for same expiry cycle
# ──────────────────────────────────────────────────────────────────────────────


def test_no_reentry_after_exit_same_expiry(tmp_path):
    """PositionStore correctly records exited expiries and prevents re-entry."""
    from live.position_store import PositionStore

    store = PositionStore(db_path=tmp_path / "test.db")

    # Initially not exited
    assert store.is_expiry_exited("RELIANCE", "2026-10-30") is False

    # Mark as exited
    store.mark_exited_expiry("RELIANCE", "2026-10-30", "DTE_EXIT")

    # Now flagged
    assert store.is_expiry_exited("RELIANCE", "2026-10-30") is True

    # Different expiry same symbol → not flagged
    assert store.is_expiry_exited("RELIANCE", "2026-11-27") is False

    # Same expiry different symbol → not flagged
    assert store.is_expiry_exited("INFY", "2026-10-30") is False

    store.close()


def test_no_reentry_idempotent(tmp_path):
    """Calling mark_exited_expiry twice (INSERT OR REPLACE) doesn't crash."""
    from live.position_store import PositionStore

    store = PositionStore(db_path=tmp_path / "test2.db")
    store.mark_exited_expiry("SBIN", "2026-10-30", "KILL_SWITCH")
    store.mark_exited_expiry("SBIN", "2026-10-30", "DTE_EXIT")  # replace
    assert store.is_expiry_exited("SBIN", "2026-10-30") is True
    store.close()
