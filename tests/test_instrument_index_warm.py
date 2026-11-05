"""
Tests for Task 9: warm instrument index at startup.

Spec:
  - ZerodhaBroker.warm_index() eagerly loads the NFO instrument master and
    builds the O(1) index before any entry calls.
  - Returns the count of CE/PE instruments indexed.
  - Logs 'instrument_index_warmed' with size and load_time_s.
  - run_session calls warm_index() when broker has that method.
  - If index is empty after warm, logs an error (non-fatal).
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch, call

import pytest


# ── ZerodhaBroker.warm_index() tests ─────────────────────────────────────────


class TestWarmIndex:

    def _make_broker(self, instruments=None):
        """Return a ZerodhaBroker with pre-loaded instruments (no real API call)."""
        from brokers.zerodha import ZerodhaBroker

        broker = ZerodhaBroker.__new__(ZerodhaBroker)
        broker._instruments = instruments or []
        broker._instrument_index = None
        broker._expiry_index = {}
        broker._strikes_index = {}
        broker._kite = MagicMock()
        from pathlib import Path
        broker._cache_path = Path("/tmp/test_nfo_instruments.csv")
        return broker

    def _sample_instruments(self, n=5):
        """Generate n fake NFO CE/PE instruments."""
        instruments = []
        base_date = date.today() + timedelta(days=30)
        for i in range(n):
            for otype in ("CE", "PE"):
                instruments.append({
                    "instrument_token": 1000 + i * 2 + (0 if otype == "CE" else 1),
                    "tradingsymbol": f"RELIANCE{base_date.strftime('%y%b').upper()}{2800 + i * 50}{otype}",
                    "name": "RELIANCE",
                    "expiry": str(base_date),
                    "strike": str(2800 + i * 50),
                    "instrument_type": otype,
                    "exchange": "NFO",
                    "lot_size": 250,
                })
        return instruments

    def test_warm_index_returns_nonzero_count(self):
        """warm_index() returns the number of indexed instruments."""
        broker = self._make_broker(instruments=self._sample_instruments(5))
        # Pre-load instruments so _load_instruments doesn't try to hit the API
        broker._instruments = self._sample_instruments(5)
        count = broker.warm_index()
        assert count == 10  # 5 strikes × 2 types

    def test_warm_index_builds_instrument_index(self):
        """After warm_index(), _instrument_index is populated."""
        broker = self._make_broker(instruments=self._sample_instruments(3))
        broker.warm_index()
        assert broker._instrument_index is not None
        assert len(broker._instrument_index) > 0

    def test_warm_index_returns_zero_for_empty_instruments(self):
        """Empty instrument list → warm_index returns 0."""
        broker = self._make_broker(instruments=[])
        count = broker.warm_index()
        assert count == 0

    def test_warm_index_logs_size_and_time(self, caplog):
        """warm_index should log 'instrument_index_warmed' with size and load_time_s."""
        import logging
        broker = self._make_broker(instruments=self._sample_instruments(4))
        with caplog.at_level(logging.INFO, logger="brokers.zerodha"):
            broker.warm_index()
        assert any("instrument_index_warmed" in r.message for r in caplog.records)


# ── run_session warm_index integration ───────────────────────────────────────


class TestRunSessionWarmsIndex:

    def _make_runner(self, tmp_path):
        from live.condor_runner import CondorRunner
        from live.trade_logger import TradeLogger
        from live.position_store import PositionStore
        from live.config import LiveConfig, CapitalConfig, StrategyParams, StockConfig

        cfg = LiveConfig(
            capital=CapitalConfig(2_000_000, 0.3, 500_000, 10),
            stocks=[StockConfig("COALINDIA", 3.0, 3.0, 0.4, enabled=True, expiry_type="monthly")],
            strategy=StrategyParams(
                3.0, 2.0, 15, 45, "09:30", "23:59",
            ),
            zerodha_api_key="t", zerodha_api_secret="t", zerodha_access_token="t",
            mode="paper",
        )
        broker = MagicMock()
        broker.get_equity_spots_batch.return_value = {}
        tl = TradeLogger(log_dir=str(tmp_path), symbol="COALINDIA")
        runner = CondorRunner(broker=broker, config=cfg, trade_logger=tl)
        runner._position_store = PositionStore(db_path=tmp_path / "wi_test.db")
        runner._recover_positions = MagicMock(return_value=[])
        runner._get_now_str = MagicMock(return_value="23:59")  # past cutoff → no entries
        return runner

    def test_warm_index_called_when_broker_has_method(self, tmp_path):
        """run_session calls broker.warm_index() when it exists."""
        runner = self._make_runner(tmp_path)
        runner._broker.warm_index = MagicMock(return_value=5000)

        with patch("live.ban_list.refresh_if_stale"):
            with patch("live.condor_runner.ShiftMonitor") as mock_monitor_cls:
                mock_monitor = MagicMock()
                mock_monitor_cls.return_value = mock_monitor
                with patch("live.condor_runner.time.sleep"):
                    runner.run_session()

        runner._broker.warm_index.assert_called_once()

    def test_warm_index_not_called_when_broker_lacks_method(self, tmp_path):
        """run_session skips warm_index when broker doesn't have the method (e.g. PaperBroker)."""
        runner = self._make_runner(tmp_path)
        # Ensure broker has no warm_index attribute (delete it from MagicMock spec)
        del runner._broker.warm_index

        with patch("live.ban_list.refresh_if_stale"):
            with patch("live.condor_runner.ShiftMonitor") as mock_monitor_cls:
                mock_monitor = MagicMock()
                mock_monitor_cls.return_value = mock_monitor
                with patch("live.condor_runner.time.sleep"):
                    # Should not raise even without warm_index
                    runner.run_session()

    def test_empty_index_logs_error_but_continues(self, tmp_path, caplog):
        """If warm_index returns 0, an error is logged but session continues."""
        import logging
        runner = self._make_runner(tmp_path)
        runner._broker.warm_index = MagicMock(return_value=0)

        with patch("live.ban_list.refresh_if_stale"):
            with patch("live.condor_runner.ShiftMonitor") as mock_monitor_cls:
                mock_monitor = MagicMock()
                mock_monitor_cls.return_value = mock_monitor
                with patch("live.condor_runner.time.sleep"):
                    with caplog.at_level(logging.ERROR, logger="live.condor_runner"):
                        runner.run_session()

        assert any("instrument_index_empty" in r.message for r in caplog.records)
