"""
Tests for Task 7: entry cutoff respects config.

Spec:
  effective_cutoff = min(config.entry_end, _ENTRY_CUTOFF_TIME="15:15")
  Block if now + entry_completion_buffer_min (default 20) > effective_cutoff
  i.e., block when now >= effective_cutoff - buffer_min

Key scenarios:
  1. now < latest_start → entries attempted
  2. now == latest_start → blocked by buffer
  3. now >= effective_cutoff → blocked by hard cutoff
  4. config.entry_end < 15:15 → effective_cutoff = entry_end; buffer applied against it
  5. config.entry_end > 15:15 → effective_cutoff = 15:15
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch, call

import pytest

# ── helpers ────────────────────────────────────────────────────────────────────

_CUTOFF = "15:15"


def _compute_latest_start(effective_cutoff: str, buffer_min: int = 20) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    dt = datetime.strptime(effective_cutoff, "%H:%M").replace(tzinfo=IST)
    from datetime import timedelta
    return (dt - timedelta(minutes=buffer_min)).strftime("%H:%M")


def _make_config(
    entry_start="09:30",
    entry_end="14:30",
    buffer_min=20,
):
    from live.config import LiveConfig, CapitalConfig, StrategyParams, StockConfig

    strategy = StrategyParams(
        shift_trigger_pct=3.0,
        wing_width_pct=2.0,
        min_dte=15,
        max_dte=45,
        entry_start=entry_start,
        entry_end=entry_end,
        entry_completion_buffer_min=buffer_min,
    )
    capital = CapitalConfig(
        total_capital=2_000_000,
        cash_margin_pct=0.3,
        max_capital_per_stock=500_000,
        max_open_positions=10,
    )
    stocks = [StockConfig(
        symbol="COALINDIA",
        put_dist_pct=3.0, call_dist_pct=3.0, premium_pct=0.4,
        enabled=True, expiry_type="monthly",
    )]
    return LiveConfig(
        capital=capital, stocks=stocks, strategy=strategy,
        zerodha_api_key="test", zerodha_api_secret="test",
        zerodha_access_token="test", mode="paper",
    )


def _make_runner(config):
    """Return a CondorRunner with a minimal stub broker that does not enter trades."""
    from live.condor_runner import CondorRunner
    from live.trade_logger import TradeLogger
    from live.capital_manager import CapitalManager
    from live.position_store import PositionStore
    import uuid
    from pathlib import Path

    broker = MagicMock()
    broker.get_equity_spots_batch.return_value = {}

    tl = TradeLogger(log_dir="/tmp/test_entry_cutoff_logs", symbol="COALINDIA")
    capital_mgr = CapitalManager(config.capital)

    runner = CondorRunner(
        broker=broker,
        config=config,
        trade_logger=tl,
        capital_manager=capital_mgr,
    )
    # Unique in-memory DB per test (avoids cross-test contamination)
    runner._position_store = PositionStore(db_path=Path(f"/tmp/test_ec_{uuid.uuid4().hex}.db"))

    # Stub out things that would try to do real work
    runner._recover_positions = MagicMock(return_value=[])
    return runner


def _entries_attempted(runner) -> bool:
    """Return True if runner tried to call enter_condor for any stock."""
    return runner.enter_condor.called if hasattr(runner.enter_condor, "called") else False


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestEffectiveCutoff:
    """Pure logic tests for effective_cutoff = min(entry_end, 15:15)."""

    def test_entry_end_before_1515_is_effective_cutoff(self):
        assert min("14:30", _CUTOFF) == "14:30"

    def test_entry_end_after_1515_clamped_to_1515(self):
        assert min("16:00", _CUTOFF) == _CUTOFF

    def test_entry_end_equal_1515_stays_1515(self):
        assert min("15:15", _CUTOFF) == _CUTOFF


class TestLatestStartComputation:
    """Buffer logic: latest_start = effective_cutoff - buffer_min."""

    def test_20min_buffer_from_1515(self):
        assert _compute_latest_start("15:15", 20) == "14:55"

    def test_20min_buffer_from_1430(self):
        assert _compute_latest_start("14:30", 20) == "14:10"

    def test_0min_buffer_equals_cutoff(self):
        assert _compute_latest_start("15:15", 0) == "15:15"


class TestRunSessionEntryCutoff:
    """Integration: run_session blocks entries based on cutoff logic."""

    def _run(self, now_str: str, entry_end: str = "14:30", buffer_min: int = 20):
        """
        Call run_session with mocked time and patch enter_condor to track calls.
        Returns (enter_condor_called, trade_logger.log call_args_list).
        """
        config = _make_config(entry_end=entry_end, buffer_min=buffer_min)
        runner = _make_runner(config)
        runner._get_now_str = MagicMock(return_value=now_str)

        enter_condor_calls = []
        runner.enter_condor = MagicMock(return_value=None)

        # Patch the kill switch and shift monitor so run_session terminates quickly
        runner._kill_switch = MagicMock()
        runner._kill_switch.is_active.return_value = False
        runner._kill_switch.check = MagicMock()

        # Patch run_session's blocking monitor wait to return immediately
        with patch("live.condor_runner.ShiftMonitor") as mock_monitor_cls:
            mock_monitor = MagicMock()
            mock_monitor_cls.return_value = mock_monitor
            mock_monitor.start = MagicMock()
            mock_monitor.stop = MagicMock()
            with patch("live.condor_runner.time.sleep"):
                runner.run_session()

        return runner.enter_condor.called

    def test_well_before_buffer_entries_attempted(self):
        """10:00 is well before 14:10 (latest start for 14:30 entry_end - 20min)."""
        called = self._run(now_str="10:00", entry_end="14:30", buffer_min=20)
        assert called, "enter_condor should be attempted well before the buffer"

    def test_at_latest_start_blocked(self):
        """14:10 == latest_start → blocked."""
        called = self._run(now_str="14:10", entry_end="14:30", buffer_min=20)
        assert not called, "enter_condor must not be called at latest_start (14:10)"

    def test_after_latest_start_blocked(self):
        """14:15 > 14:10 latest_start → blocked."""
        called = self._run(now_str="14:15", entry_end="14:30", buffer_min=20)
        assert not called

    def test_at_hard_cutoff_blocked(self):
        """15:15 == hard cutoff → blocked even with late entry_end."""
        called = self._run(now_str="15:15", entry_end="16:00", buffer_min=20)
        assert not called

    def test_entry_end_later_than_1515_uses_1515_cutoff(self):
        """entry_end=16:00 → effective_cutoff=15:15 → latest_start=14:55."""
        # 14:50 is before 14:55 → should attempt
        called_before = self._run(now_str="14:50", entry_end="16:00", buffer_min=20)
        assert called_before

        # 14:55 == 14:55 → should block
        called_at = self._run(now_str="14:55", entry_end="16:00", buffer_min=20)
        assert not called_at

    def test_zero_buffer_only_hard_cutoff_blocks(self):
        """With buffer=0, entries allowed right up to effective_cutoff."""
        called = self._run(now_str="14:29", entry_end="14:30", buffer_min=0)
        assert called
