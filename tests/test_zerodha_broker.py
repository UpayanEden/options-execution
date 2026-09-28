"""
Tests for ZerodhaBroker.
All Kite API calls are mocked — no real credentials needed.
"""
from __future__ import annotations

import csv
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from brokers.zerodha import ZerodhaBroker
from brokers.base import BrokerError, InstrumentNotFoundError, QuoteError
from execution.models import OptionType, Quote, ResolvedInstrument

IST = ZoneInfo("Asia/Kolkata")


def _make_broker(instruments: list[dict] | None = None, cache_path: str | None = None) -> ZerodhaBroker:
    """Create a ZerodhaBroker with mocked KiteConnect."""
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
        path = f.name

    broker = ZerodhaBroker.__new__(ZerodhaBroker)
    broker._kite = MagicMock()
    broker._cache_path = Path(cache_path or path)
    broker._instruments = instruments  # pre-load to skip API call
    return broker


def _oberoirlty_instrument(
    token: int = 12345,
    symbol: str = "OBEROIRLTY26AUG1700PE",
    strike: float = 1700.0,
    expiry: str = "2026-08-25",
    option_type: str = "PE",
    lot_size: int = 575,
    tick_size: float = 0.05,
) -> dict:
    return {
        "instrument_token": token,
        "tradingsymbol": symbol,
        "name": "OBEROIRLTY",
        "expiry": expiry,
        "strike": strike,
        "lot_size": lot_size,
        "tick_size": tick_size,
        "instrument_type": option_type,
        "exchange": "NFO",
        "segment": "NFO-OPT",
    }


class TestInstrumentResolution:
    def test_resolves_correct_instrument(self) -> None:
        inst = _oberoirlty_instrument()
        broker = _make_broker(instruments=[inst])
        result = broker.resolve_instrument(
            underlying="OBEROIRLTY",
            exchange="NFO",
            expiry=date(2026, 8, 25),
            strike=Decimal("1700"),
            option_type="PE",
        )
        assert result.trading_symbol == "OBEROIRLTY26AUG1700PE"
        assert result.instrument_token == 12345
        assert result.lot_size == 575
        assert result.tick_size == Decimal("0.05")

    def test_raises_when_not_found(self) -> None:
        broker = _make_broker(instruments=[])
        with pytest.raises(InstrumentNotFoundError):
            broker.resolve_instrument(
                underlying="OBEROIRLTY",
                exchange="NFO",
                expiry=date(2026, 8, 25),
                strike=Decimal("9999"),
                option_type="PE",
            )

    def test_ignores_wrong_option_type(self) -> None:
        inst = _oberoirlty_instrument(option_type="CE", symbol="OBEROIRLTY26AUG1700CE")
        broker = _make_broker(instruments=[inst])
        with pytest.raises(InstrumentNotFoundError):
            broker.resolve_instrument(
                underlying="OBEROIRLTY",
                exchange="NFO",
                expiry=date(2026, 8, 25),
                strike=Decimal("1700"),
                option_type="PE",  # looking for PE but only CE exists
            )

    def test_ignores_wrong_expiry(self) -> None:
        inst = _oberoirlty_instrument(expiry="2026-07-31")
        broker = _make_broker(instruments=[inst])
        with pytest.raises(InstrumentNotFoundError):
            broker.resolve_instrument(
                underlying="OBEROIRLTY",
                exchange="NFO",
                expiry=date(2026, 8, 25),  # different expiry
                strike=Decimal("1700"),
                option_type="PE",
            )

    def test_ignores_wrong_strike(self) -> None:
        inst = _oberoirlty_instrument(strike=1720.0, symbol="OBEROIRLTY26AUG1720PE")
        broker = _make_broker(instruments=[inst])
        with pytest.raises(InstrumentNotFoundError):
            broker.resolve_instrument(
                underlying="OBEROIRLTY",
                exchange="NFO",
                expiry=date(2026, 8, 25),
                strike=Decimal("1700"),
                option_type="PE",
            )

    def test_option_type_returned_correctly(self) -> None:
        inst = _oberoirlty_instrument(option_type="CE", symbol="OBEROIRLTY26AUG1700CE")
        broker = _make_broker(instruments=[inst])
        result = broker.resolve_instrument(
            "OBEROIRLTY", "NFO", date(2026, 8, 25), Decimal("1700"), "CE"
        )
        assert result.option_type == OptionType.CE


class TestQuoteParsing:
    def _make_quote_response(self, symbol: str, token: int) -> dict:
        return {
            f"NFO:{symbol}": {
                "instrument_token": token,
                "last_price": 8.5,
                "volume": 12000,
                "oi": 50000,
                "timestamp": "2026-08-03 10:30:00",
                "depth": {
                    "buy": [
                        {"price": 8.25, "quantity": 200, "orders": 3},
                        {"price": 8.00, "quantity": 400, "orders": 5},
                    ],
                    "sell": [
                        {"price": 8.50, "quantity": 150, "orders": 2},
                        {"price": 8.75, "quantity": 300, "orders": 4},
                    ],
                },
            }
        }

    def test_quote_parsed_correctly(self) -> None:
        broker = _make_broker()
        broker._kite.quote.return_value = self._make_quote_response("OBEROIRLTY26AUG1700PE", 12345)

        quotes = broker.get_quotes([(12345, "OBEROIRLTY26AUG1700PE")])
        assert 12345 in quotes
        q = quotes[12345]
        assert q.trading_symbol == "OBEROIRLTY26AUG1700PE"
        assert q.bid == Decimal("8.25")
        assert q.ask == Decimal("8.50")
        assert q.ltp == Decimal("8.5")
        assert q.oi == 50000
        assert len(q.depth_bids) == 2
        assert len(q.depth_asks) == 2

    def test_empty_depth_falls_back_to_ltp(self) -> None:
        broker = _make_broker()
        broker._kite.quote.return_value = {
            "NFO:TEST": {
                "instrument_token": 999,
                "last_price": 10.0,
                "volume": 0,
                "oi": 0,
                "timestamp": None,
                "depth": {"buy": [], "sell": []},
            }
        }
        quotes = broker.get_quotes([(999, "TEST")])
        assert 999 in quotes
        q = quotes[999]
        assert q.bid == Decimal("10.0")
        assert q.ask == Decimal("10.0")

    def test_quote_api_error_raises_quote_error(self) -> None:
        broker = _make_broker()
        broker._kite.quote.side_effect = Exception("Network error")
        with pytest.raises(QuoteError):
            broker.get_quotes([(12345, "OBEROIRLTY26AUG1700PE")])


class TestPositionsAndOrders:
    def test_get_positions_returns_net(self) -> None:
        broker = _make_broker()
        broker._kite.positions.return_value = {
            "net": [{"tradingsymbol": "OBEROIRLTY26AUG1700PE", "net_quantity": -575}],
            "day": [],
        }
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["tradingsymbol"] == "OBEROIRLTY26AUG1700PE"

    def test_get_orders_returns_list(self) -> None:
        broker = _make_broker()
        broker._kite.orders.return_value = [
            {"order_id": "ABC123", "status": "COMPLETE", "tradingsymbol": "TEST"}
        ]
        orders = broker.get_orders()
        assert len(orders) == 1

    def test_get_margins_returns_dict(self) -> None:
        broker = _make_broker()
        broker._kite.margins.return_value = {
            "equity": {"available": {"cash": 150000}, "utilised": {"debits": 50000}}
        }
        margins = broker.get_margins()
        assert "equity" in margins


class TestBrokerErrors:
    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(BrokerError):
            ZerodhaBroker(api_key="", access_token="sometoken")

    def test_missing_access_token_raises(self) -> None:
        with pytest.raises(BrokerError):
            ZerodhaBroker(api_key="somekey", access_token="")

    def test_positions_api_error_raises_broker_error(self) -> None:
        broker = _make_broker()
        broker._kite.positions.side_effect = Exception("timeout")
        with pytest.raises(BrokerError):
            broker.get_positions()
