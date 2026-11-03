"""
Tests for per-position stop-loss check in ShiftMonitor (Task 4).

Spec:
  - Wire stop_loss_credit_multiple and stop_loss_inr from StrategyParams into
    the monitor loop.
  - Require stop_loss_confirm_checks (default 2) consecutive breaches before
    returning STOP_LOSS_EXIT. Resets counter on a clean cycle.
  - Guard: skip check when close_debit < 0 (stale data).
  - close_debit uses natural sides: shorts at ASK, longs at BID.
  - loss = close_debit − original_credit (per unit).

Key test cases:
  - Multiple breaches required: single breach must not trigger, 2nd does.
  - Counter resets when stop-loss is not breached.
  - INR stop-loss: loss_inr = per_unit_loss × lots × lot_size.
  - Negative close_debit guard never fires.
  - Disabled (None) stop-loss never fires.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

# ── helpers ────────────────────────────────────────────────────────────────────


def _make_config(
    stop_loss_credit_multiple=None,
    stop_loss_inr=None,
    stop_loss_confirm_checks=2,
    profit_target_pct=85.0,
):
    from live.config import LiveConfig, CapitalConfig, StrategyParams, StockConfig

    strategy = StrategyParams(
        shift_trigger_pct=3.0,
        wing_width_pct=2.0,
        min_dte=15,
        max_dte=45,
        entry_start="09:30",
        entry_end="14:00",
        profit_target_pct=profit_target_pct,
        stop_loss_credit_multiple=stop_loss_credit_multiple,
        stop_loss_inr=stop_loss_inr,
        stop_loss_confirm_checks=stop_loss_confirm_checks,
    )
    return MagicMock(strategy=strategy)


def _make_pos(
    symbol="RELIANCE",
    original_credit=2.0,
    lots=1,
    lot_size=250,
):
    pos = MagicMock()
    pos.symbol = symbol
    pos.underlying = symbol
    pos.expiry = date.today() + timedelta(days=25)
    pos.original_credit = original_credit
    pos.initial_credit = original_credit
    pos.shift_count = 0
    pos.state = "OPEN"
    pos.ban_hold = False
    pos.recon_legs = None
    pos.lots = lots
    pos.lot_size = lot_size

    sp = MagicMock(); sp.trading_symbol = "RELI26NOV2200PE"; sp.strike = 2200.0
    lp = MagicMock(); lp.trading_symbol = "RELI26NOV2100PE"; lp.strike = 2100.0
    sc = MagicMock(); sc.trading_symbol = "RELI26NOV2600CE"; sc.strike = 2600.0
    lc = MagicMock(); lc.trading_symbol = "RELI26NOV2700CE"; lc.strike = 2700.0
    pos.short_put  = sp
    pos.long_put   = lp
    pos.short_call = sc
    pos.long_call  = lc
    return pos


def _make_cache(sp_ask, sc_ask, lp_bid, lc_bid, sp_bid=None, sc_bid=None, lp_ask=None, lc_ask=None):
    """Build options_cache with natural close keys for RELI symbol."""
    return {
        "RELI26NOV2200PE:ask": sp_ask,
        "RELI26NOV2600CE:ask": sc_ask,
        "RELI26NOV2100PE:bid": lp_bid,
        "RELI26NOV2700CE:bid": lc_bid,
        "RELI26NOV2200PE:bid": sp_bid if sp_bid is not None else max(0.0, sp_ask - 0.05),
        "RELI26NOV2600CE:bid": sc_bid if sc_bid is not None else max(0.0, sc_ask - 0.05),
        "RELI26NOV2100PE:ask": lp_ask if lp_ask is not None else lp_bid + 0.05,
        "RELI26NOV2700CE:ask": lc_ask if lc_ask is not None else lc_bid + 0.05,
        "RELI26NOV2200PE:ltp": sp_ask,
        "RELI26NOV2600CE:ltp": sc_ask,
        "RELI26NOV2100PE:ltp": lp_bid,
        "RELI26NOV2700CE:ltp": lc_bid,
    }


def _make_monitor(config):
    from live.shift_monitor import ShiftMonitor

    broker = MagicMock()
    broker.get_equity_spots_batch = MagicMock(return_value={})
    trade_logger = MagicMock()
    ks = MagicMock()
    ks.is_active.return_value = False

    monitor = ShiftMonitor(
        broker=broker,
        positions=[],
        config=config,
        logger_=trade_logger,
        on_shift_cb=MagicMock(),
        kill_switch=ks,
        on_exit_cb=MagicMock(),
    )
    return monitor


def _call_check(monitor, pos, cache):
    pos.expiry = date.today() + timedelta(days=25)
    monitor._get_current_spot = MagicMock(return_value=2400.0)
    with patch("live.ban_list.is_banned", return_value=False):
        return monitor._check_position(
            pos,
            spot_cache={"RELIANCE": 2400.0},
            all_spots_null=False,
            options_cache=cache,
        )


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestStopLossCreditMultiple:

    def test_single_breach_does_not_exit(self):
        """First breach (confirm_checks=2): must not exit yet."""
        cfg = _make_config(stop_loss_credit_multiple=2.0, stop_loss_confirm_checks=2)
        monitor = _make_monitor(cfg)
        pos = _make_pos(original_credit=2.0)

        # close_debit = (3.0+3.0) - 0 = 6.0, per_unit_loss = 4.0 = 2× credit → breaches
        cache = _make_cache(sp_ask=3.0, sc_ask=3.0, lp_bid=0.0, lc_bid=0.0)
        action, _ = _call_check(monitor, pos, cache)
        assert action != "STOP_LOSS_EXIT", (
            "First breach should not exit yet (need 2 consecutive)"
        )
        assert monitor._sl_breach_count.get(id(pos), 0) == 1

    def test_second_breach_triggers_exit(self):
        """Second consecutive breach fires STOP_LOSS_EXIT."""
        cfg = _make_config(stop_loss_credit_multiple=2.0, stop_loss_confirm_checks=2)
        monitor = _make_monitor(cfg)
        pos = _make_pos(original_credit=2.0)

        # close_debit=6.0, loss=4.0 = 2× → breach
        cache = _make_cache(sp_ask=3.0, sc_ask=3.0, lp_bid=0.0, lc_bid=0.0)
        _call_check(monitor, pos, cache)  # breach 1
        action, reason = _call_check(monitor, pos, cache)  # breach 2
        assert action == "STOP_LOSS_EXIT"
        assert "loss=" in reason

    def test_counter_resets_on_clean_cycle(self):
        """A non-breaching cycle resets the consecutive counter."""
        cfg = _make_config(stop_loss_credit_multiple=2.0, stop_loss_confirm_checks=2)
        monitor = _make_monitor(cfg)
        pos = _make_pos(original_credit=2.0)

        breach_cache = _make_cache(sp_ask=3.0, sc_ask=3.0, lp_bid=0.0, lc_bid=0.0)
        clean_cache  = _make_cache(sp_ask=0.5, sc_ask=0.5, lp_bid=0.0, lc_bid=0.0)

        _call_check(monitor, pos, breach_cache)  # breach 1
        assert monitor._sl_breach_count.get(id(pos), 0) == 1
        _call_check(monitor, pos, clean_cache)   # clean → reset
        assert monitor._sl_breach_count.get(id(pos), 0) == 0

        # Now would need 2 more consecutive breaches to exit
        _call_check(monitor, pos, breach_cache)  # breach 1 again
        action, _ = _call_check(monitor, pos, clean_cache)  # clean → reset
        assert action != "STOP_LOSS_EXIT"
        assert monitor._sl_breach_count.get(id(pos), 0) == 0

    def test_confirm_checks_1_fires_immediately(self):
        """With confirm_checks=1, a single breach triggers exit."""
        cfg = _make_config(stop_loss_credit_multiple=2.0, stop_loss_confirm_checks=1)
        monitor = _make_monitor(cfg)
        pos = _make_pos(original_credit=2.0)

        cache = _make_cache(sp_ask=3.0, sc_ask=3.0, lp_bid=0.0, lc_bid=0.0)
        action, _ = _call_check(monitor, pos, cache)
        assert action == "STOP_LOSS_EXIT"

    def test_below_multiple_does_not_trigger(self):
        """Loss below the multiple threshold does not breach."""
        cfg = _make_config(stop_loss_credit_multiple=2.0, stop_loss_confirm_checks=1)
        monitor = _make_monitor(cfg)
        pos = _make_pos(original_credit=2.0)

        # close_debit=3.0, loss=1.0 = 0.5× < 2× → no breach
        cache = _make_cache(sp_ask=1.5, sc_ask=1.5, lp_bid=0.0, lc_bid=0.0)
        action, _ = _call_check(monitor, pos, cache)
        assert action != "STOP_LOSS_EXIT"

    def test_disabled_multiple_never_fires(self):
        """stop_loss_credit_multiple=None → stop-loss never fires."""
        cfg = _make_config(stop_loss_credit_multiple=None, stop_loss_inr=None)
        monitor = _make_monitor(cfg)
        pos = _make_pos(original_credit=2.0)

        # Massive loss — still no exit
        cache = _make_cache(sp_ask=20.0, sc_ask=20.0, lp_bid=0.0, lc_bid=0.0)
        action, _ = _call_check(monitor, pos, cache)
        assert action != "STOP_LOSS_EXIT"


class TestStopLossInr:

    def test_inr_stop_loss_fires_after_confirms(self):
        """
        lots=1, lot_size=250, original_credit=2.0, close_debit=4.0
        per_unit_loss=2.0, loss_inr = 2.0 × 1 × 250 = 500
        stop_loss_inr=500 → breach, 1 confirm needed → fires
        """
        cfg = _make_config(stop_loss_inr=500, stop_loss_confirm_checks=1)
        monitor = _make_monitor(cfg)
        pos = _make_pos(original_credit=2.0, lots=1, lot_size=250)

        # close_debit = 4.0+0.0-0.0 = 4.0
        cache = _make_cache(sp_ask=2.0, sc_ask=2.0, lp_bid=0.0, lc_bid=0.0)
        action, reason = _call_check(monitor, pos, cache)
        assert action == "STOP_LOSS_EXIT"
        assert "loss_inr" in reason

    def test_inr_below_threshold_no_fire(self):
        """
        loss_inr = 1.0 × 1 × 250 = 250 < stop_loss_inr=500 → no breach
        """
        cfg = _make_config(stop_loss_inr=500, stop_loss_confirm_checks=1)
        monitor = _make_monitor(cfg)
        pos = _make_pos(original_credit=2.0, lots=1, lot_size=250)

        # close_debit = 3.0, loss = 1.0, loss_inr = 250
        cache = _make_cache(sp_ask=1.5, sc_ask=1.5, lp_bid=0.0, lc_bid=0.0)
        action, _ = _call_check(monitor, pos, cache)
        assert action != "STOP_LOSS_EXIT"


class TestStopLossGuards:

    def test_negative_close_debit_skipped(self):
        """close_debit < 0 (stale data) — stop-loss must not fire."""
        cfg = _make_config(stop_loss_credit_multiple=0.01, stop_loss_confirm_checks=1)
        monitor = _make_monitor(cfg)
        pos = _make_pos(original_credit=2.0)

        # sp_ask=sc_ask=0, lp_bid=lc_bid=0.5 → close_debit = -1.0
        cache = _make_cache(sp_ask=0.0, sc_ask=0.0, lp_bid=0.5, lc_bid=0.5)
        action, _ = _call_check(monitor, pos, cache)
        assert action != "STOP_LOSS_EXIT"

    def test_no_options_cache_no_fire(self):
        """When options_cache is None, no stop-loss check runs."""
        cfg = _make_config(stop_loss_credit_multiple=0.01, stop_loss_confirm_checks=1)
        monitor = _make_monitor(cfg)
        pos = _make_pos(original_credit=2.0)

        pos.expiry = date.today() + timedelta(days=25)
        monitor._get_current_spot = MagicMock(return_value=2400.0)
        with patch("live.ban_list.is_banned", return_value=False):
            action, _ = monitor._check_position(
                pos,
                spot_cache={"RELIANCE": 2400.0},
                all_spots_null=False,
                options_cache=None,
            )
        assert action != "STOP_LOSS_EXIT"
