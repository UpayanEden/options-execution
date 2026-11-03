"""
Tests for Task 8: F&O ban state persistence + startup ban list pre-check.

Spec:
  - Persist fo_ban_failures, fo_ban_suspended, fo_ban_suspended_at to DB
  - Migration: add columns if missing from older DBs
  - _recover_positions restores these fields (not reset to 0 on restart)
  - run_session calls ban_list.refresh_if_stale() before the entry phase
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import tempfile
import time
import uuid

import pytest
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


# ── helpers ────────────────────────────────────────────────────────────────────


def _unique_db() -> Path:
    return Path(f"/tmp/test_fo_ban_{uuid.uuid4().hex}.db")


def _make_position(
    symbol="COALINDIA",
    fo_ban_failures=0,
    fo_ban_suspended=False,
    fo_ban_suspended_at=0.0,
):
    """Return a minimal CondorPosition-like MagicMock with fo_ban fields."""
    from live.condor_runner import CondorPosition

    sp = MagicMock()
    sp.instrument_token = 1001
    sp.trading_symbol = "COAL26NOV370PE"
    sp.strike = 370.0
    sp.option_type = "PE"

    lp = MagicMock()
    lp.instrument_token = 1002
    lp.trading_symbol = "COAL26NOV360PE"
    lp.strike = 360.0
    lp.option_type = "PE"

    sc = MagicMock()
    sc.instrument_token = 1003
    sc.trading_symbol = "COAL26NOV410CE"
    sc.strike = 410.0
    sc.option_type = "CE"

    lc = MagicMock()
    lc.instrument_token = 1004
    lc.trading_symbol = "COAL26NOV420CE"
    lc.strike = 420.0
    lc.option_type = "CE"

    pos = CondorPosition(
        symbol=symbol,
        underlying=symbol,
        expiry=date.today() + timedelta(days=25),
        spot_at_entry=390.0,
        short_put=sp,
        long_put=lp,
        short_call=sc,
        long_call=lc,
        lots=1,
        lot_size=1200,
        initial_credit=2.0,
        state="OPEN",
    )
    pos.fo_ban_failures = fo_ban_failures
    pos.fo_ban_suspended = fo_ban_suspended
    pos.fo_ban_suspended_at = fo_ban_suspended_at
    return pos


# ── DB migration tests ────────────────────────────────────────────────────────


class TestPositionStoreMigration:

    def test_new_db_has_fo_ban_columns(self):
        """A freshly created DB should have all fo_ban columns."""
        from live.position_store import PositionStore
        import sqlite3

        db_path = _unique_db()
        store = PositionStore(db_path=db_path)

        conn = sqlite3.connect(str(db_path))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
        conn.close()

        assert "fo_ban_failures" in cols
        assert "fo_ban_suspended" in cols
        assert "fo_ban_suspended_at" in cols

    def test_fo_ban_fields_persisted_and_restored(self):
        """Round-trip: upsert a position with ban state, then load it back."""
        from live.position_store import PositionStore

        db_path = _unique_db()
        store = PositionStore(db_path=db_path)

        pos = _make_position(
            fo_ban_failures=3,
            fo_ban_suspended=True,
            fo_ban_suspended_at=1234567890.0,
        )
        pos.entry_time = datetime.now(IST)
        store.upsert(pos)

        # Read back raw row
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT fo_ban_failures, fo_ban_suspended, fo_ban_suspended_at "
            "FROM positions WHERE symbol = ?", (pos.symbol,)
        ).fetchone()
        conn.close()

        assert row is not None
        assert int(row["fo_ban_failures"]) == 3
        assert int(row["fo_ban_suspended"]) == 1
        assert float(row["fo_ban_suspended_at"]) == pytest.approx(1234567890.0)

    def test_fo_ban_defaults_zero_on_fresh_position(self):
        """A position with no ban state stores defaults of 0."""
        from live.position_store import PositionStore
        import sqlite3

        db_path = _unique_db()
        store = PositionStore(db_path=db_path)
        pos = _make_position()
        pos.entry_time = datetime.now(IST)
        store.upsert(pos)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT fo_ban_failures, fo_ban_suspended, fo_ban_suspended_at "
            "FROM positions WHERE symbol = ?", (pos.symbol,)
        ).fetchone()
        conn.close()

        assert int(row["fo_ban_failures"]) == 0
        assert int(row["fo_ban_suspended"]) == 0
        assert float(row["fo_ban_suspended_at"]) == pytest.approx(0.0)


# ── Recovery test ─────────────────────────────────────────────────────────────


class TestRecoverFoBanState:
    """_recover_positions must restore fo_ban fields from the DB row."""

    def test_recovered_position_has_fo_ban_state(self, tmp_path):
        """After upsert+recover, fo_ban_failures and fo_ban_suspended are restored."""
        from live.condor_runner import CondorRunner
        from live.trade_logger import TradeLogger
        from live.capital_manager import CapitalManager
        from live.position_store import PositionStore
        from live.config import LiveConfig, CapitalConfig, StrategyParams, StockConfig

        cfg = LiveConfig(
            capital=CapitalConfig(2_000_000, 0.3, 500_000, 10),
            stocks=[StockConfig("COALINDIA", 3.0, 3.0, 0.4, enabled=True, expiry_type="monthly")],
            strategy=StrategyParams(3.0, 2.0, 15, 45, "09:30", "14:00"),
            zerodha_api_key="t", zerodha_api_secret="t", zerodha_access_token="t",
            mode="paper",
        )
        broker = MagicMock()
        tl = TradeLogger(log_dir=str(tmp_path), symbol="COALINDIA")
        runner = CondorRunner(broker=broker, config=cfg, trade_logger=tl)

        db_path = tmp_path / "recover_test.db"
        store = PositionStore(db_path=db_path)
        runner._position_store = store

        # Store a position with non-default ban state
        pos = _make_position(fo_ban_failures=4, fo_ban_suspended=True, fo_ban_suspended_at=999.0)
        pos.entry_time = datetime.now(IST)
        store.upsert(pos)

        # Now recover
        recovered = runner._recover_positions()
        assert len(recovered) == 1
        rec_pos = recovered[0]

        assert rec_pos.fo_ban_failures == 4
        assert rec_pos.fo_ban_suspended is True
        assert rec_pos.fo_ban_suspended_at == pytest.approx(999.0)


# ── Startup ban list pre-check ────────────────────────────────────────────────


class TestStartupBanListRefresh:
    """run_session must call ban_list.refresh_if_stale() before entering positions."""

    def test_refresh_if_stale_called_at_startup(self, tmp_path):
        from live.condor_runner import CondorRunner
        from live.trade_logger import TradeLogger
        from live.capital_manager import CapitalManager
        from live.position_store import PositionStore
        from live.config import LiveConfig, CapitalConfig, StrategyParams, StockConfig

        cfg = LiveConfig(
            capital=CapitalConfig(2_000_000, 0.3, 500_000, 10),
            stocks=[StockConfig("COALINDIA", 3.0, 3.0, 0.4, enabled=True, expiry_type="monthly")],
            strategy=StrategyParams(
                3.0, 2.0, 15, 45, "09:30", "23:59",
                min_entry_premium_pct=0.0, min_shift_edge=-9999.0,
            ),
            zerodha_api_key="t", zerodha_api_secret="t", zerodha_access_token="t",
            mode="paper",
        )
        broker = MagicMock()
        broker.get_equity_spots_batch.return_value = {}
        tl = TradeLogger(log_dir=str(tmp_path), symbol="COALINDIA")
        runner = CondorRunner(broker=broker, config=cfg, trade_logger=tl)
        runner._position_store = PositionStore(db_path=tmp_path / "bl_test.db")
        runner._recover_positions = MagicMock(return_value=[])
        runner._get_now_str = MagicMock(return_value="23:59")  # past entry cutoff → no entries

        refresh_calls = []
        with patch("live.ban_list.refresh_if_stale", side_effect=lambda: refresh_calls.append(1)) as mock_refresh:
            with patch("live.condor_runner.ShiftMonitor") as mock_monitor_cls:
                mock_monitor = MagicMock()
                mock_monitor_cls.return_value = mock_monitor
                with patch("live.condor_runner.time.sleep"):
                    runner.run_session()

        # The module-level ban_list.refresh_if_stale is patched; the runner imports it
        # fresh each call so we need to count via the side_effect list.
        # If the runner calls it, refresh_calls will be non-empty.
        assert len(refresh_calls) > 0, "ban_list.refresh_if_stale() must be called during run_session"
