"""Tests for QuoteEngine."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from execution.models import Quote, RiskConfig
from execution.quote_engine import QuoteEngine

IST = ZoneInfo("Asia/Kolkata")


def _fresh_quote(token: int, symbol: str, bid: str, ask: str) -> Quote:
    return Quote(
        instrument_token=token,
        trading_symbol=symbol,
        exchange="NFO",
        timestamp=datetime.now(IST),
        ltp=Decimal(bid),
        bid=Decimal(bid),
        bid_qty=100,
        ask=Decimal(ask),
        ask_qty=100,
    )


class TestQuoteEngineValidation:
    def _make_engine(self, quotes: dict[int, Quote]) -> QuoteEngine:
        mock = MagicMock()
        mock.get_quotes.return_value = quotes
        return QuoteEngine(mock)

    def _make_risk(self, staleness: int = 30, spread_pct: float = 8.0) -> RiskConfig:
        return RiskConfig(
            capital_limit=Decimal("200000"),
            max_strategy_loss=Decimal("5000"),
            max_daily_loss=Decimal("8000"),
            min_net_credit=Decimal("15"),
            target_net_credit=Decimal("20"),
            max_bid_ask_spread_pct=Decimal(str(spread_pct)),
            max_total_slippage=Decimal("5"),
            quote_staleness_seconds=staleness,
            entry_start_time="09:20",
            entry_end_time="14:30",
            exit_start_time="09:20",
            exit_end_time="15:15",
            limit_step=Decimal("0.05"),
            order_wait_seconds=15,
            max_repricing_attempts=5,
        )

    def test_valid_quotes_pass(self) -> None:
        q = _fresh_quote(1, "TEST", "10.00", "10.50")
        engine = self._make_engine({1: q})
        risk = self._make_risk()
        errors = engine.validate_basket({"TEST": q}, risk)
        assert errors == []

    def test_wide_spread_detected(self) -> None:
        q = _fresh_quote(1, "TEST", "5.00", "15.00")
        engine = self._make_engine({1: q})
        risk = self._make_risk(spread_pct=8.0)
        errors = engine.validate_basket({"TEST": q}, risk)
        assert any("spread" in e for e in errors)

    def test_zero_bid_detected(self) -> None:
        q = _fresh_quote(1, "TEST", "0.00", "5.00")
        engine = self._make_engine({1: q})
        risk = self._make_risk()
        errors = engine.validate_basket({"TEST": q}, risk)
        assert any("zero" in e.lower() for e in errors)
