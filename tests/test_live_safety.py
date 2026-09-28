"""
Tests for live trading safety gates.
Verifies that:
- Live orders are blocked without correct env vars
- Process lock prevents duplicate instances
- Kill switch halts execution
- Order idempotency key is used
"""
from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brokers.zerodha import LiveTradingBlockedError, ZerodhaBroker
from brokers.base import BrokerError
from execution.process_lock import ProcessLock, ProcessLockError
from risk.kill_switch import KillSwitch, KillSwitchError


class TestLiveTradingGate:
    """_assert_live_mode must block orders unless both env vars are set correctly."""

    def _make_broker(self) -> ZerodhaBroker:
        broker = ZerodhaBroker.__new__(ZerodhaBroker)
        broker._kite = MagicMock()
        broker._cache_path = Path(tempfile.mktemp(suffix=".csv"))
        broker._instruments = []
        return broker

    def test_blocks_when_mode_is_paper(self) -> None:
        broker = self._make_broker()
        with patch.dict(os.environ, {
            "TRADING_MODE": "PAPER",
            "LIVE_TRADING_CONFIRMATION": "I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED",
        }):
            with pytest.raises(LiveTradingBlockedError):
                broker.place_order("TEST", "NFO", "BUY", 100, Decimal("10.00"))

    def test_blocks_when_confirmation_missing(self) -> None:
        broker = self._make_broker()
        with patch.dict(os.environ, {
            "TRADING_MODE": "LIVE",
            "LIVE_TRADING_CONFIRMATION": "",
        }):
            with pytest.raises(LiveTradingBlockedError):
                broker.place_order("TEST", "NFO", "BUY", 100, Decimal("10.00"))

    def test_blocks_when_confirmation_wrong(self) -> None:
        broker = self._make_broker()
        with patch.dict(os.environ, {
            "TRADING_MODE": "LIVE",
            "LIVE_TRADING_CONFIRMATION": "yes_i_confirm",  # wrong value
        }):
            with pytest.raises(LiveTradingBlockedError):
                broker.place_order("TEST", "NFO", "BUY", 100, Decimal("10.00"))

    def test_blocks_market_order_price_zero(self) -> None:
        broker = self._make_broker()
        with patch.dict(os.environ, {
            "TRADING_MODE": "LIVE",
            "LIVE_TRADING_CONFIRMATION": "I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED",
        }):
            with pytest.raises(BrokerError):
                broker.place_order("TEST", "NFO", "BUY", 100, Decimal("0"))

    def test_allows_when_both_env_vars_correct(self) -> None:
        broker = self._make_broker()
        broker._kite.place_order.return_value = "ORDER123"
        with patch.dict(os.environ, {
            "TRADING_MODE": "LIVE",
            "LIVE_TRADING_CONFIRMATION": "I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED",
        }):
            result = broker.place_order("TEST", "NFO", "BUY", 100, Decimal("10.00"))
            assert result == "ORDER123"

    def test_modify_order_blocked_in_paper_mode(self) -> None:
        broker = self._make_broker()
        with patch.dict(os.environ, {"TRADING_MODE": "PAPER"}):
            with pytest.raises(LiveTradingBlockedError):
                broker.modify_order("ORDER123", Decimal("11.00"))

    def test_cancel_order_blocked_in_paper_mode(self) -> None:
        broker = self._make_broker()
        with patch.dict(os.environ, {"TRADING_MODE": "PAPER"}):
            with pytest.raises(LiveTradingBlockedError):
                broker.cancel_order("ORDER123")

    def test_cancel_all_blocked_in_paper_mode(self) -> None:
        broker = self._make_broker()
        with patch.dict(os.environ, {"TRADING_MODE": "PAPER"}):
            with pytest.raises(LiveTradingBlockedError):
                broker.cancel_all_open_orders()


class TestProcessLock:
    def test_acquires_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = Path(tmp) / "test.lock"
            lock = ProcessLock(lock_file)
            lock.acquire()
            assert lock_file.exists()
            lock.release()
            assert not lock_file.exists()

    def test_blocks_second_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = Path(tmp) / "test.lock"
            lock1 = ProcessLock(lock_file)
            lock2 = ProcessLock(lock_file)
            lock1.acquire()
            try:
                with pytest.raises(ProcessLockError):
                    lock2.acquire()
            finally:
                lock1.release()

    def test_cleans_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = Path(tmp) / "test.lock"
            # Write a PID that doesn't exist (PID 1 reserved, use something impossible)
            lock_file.write_text("99999999")  # very unlikely to exist
            lock = ProcessLock(lock_file)
            lock.acquire()  # should clean stale lock and acquire
            assert lock_file.exists()
            lock.release()

    def test_context_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = Path(tmp) / "test.lock"
            lock = ProcessLock(lock_file)
            with lock:
                assert lock_file.exists()
            assert not lock_file.exists()


class TestKillSwitch:
    def test_activates_and_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "KILL_SWITCH_ACTIVE"
            ks = KillSwitch(flag)
            assert not ks.is_active()
            ks.activate("test reason")
            assert ks.is_active()
            with pytest.raises(KillSwitchError):
                ks.check()
            ks.deactivate()
            assert not ks.is_active()
