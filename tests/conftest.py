"""Shared fixtures for all tests."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from execution.models import (
    LegConfig,
    OptionType,
    PaperFillMode,
    Quote,
    ResolvedInstrument,
    RiskConfig,
    Side,
    StrategyConfig,
)
from brokers.base import BrokerAdapter
from brokers.paper import PaperBroker

IST = ZoneInfo("Asia/Kolkata")


def make_ist(hour: int = 10, minute: int = 30) -> datetime:
    return datetime(2026, 8, 3, hour, minute, 0, tzinfo=IST)


@pytest.fixture
def oberoirlty_strategy() -> StrategyConfig:
    return StrategyConfig(
        name="expected_move_iron_condor",
        underlying="OBEROIRLTY",
        exchange="NFO",
        expiry=date(2026, 8, 25),
        lots=1,
        legs=[
            LegConfig(side=Side.BUY, option_type=OptionType.PE, strike=Decimal("1700")),
            LegConfig(side=Side.SELL, option_type=OptionType.PE, strike=Decimal("1720")),
            LegConfig(side=Side.SELL, option_type=OptionType.CE, strike=Decimal("1920")),
            LegConfig(side=Side.BUY, option_type=OptionType.CE, strike=Decimal("1940")),
        ],
    )


@pytest.fixture
def default_risk() -> RiskConfig:
    return RiskConfig(
        capital_limit=Decimal("200000"),
        max_strategy_loss=Decimal("5000"),
        max_daily_loss=Decimal("8000"),
        min_net_credit=Decimal("15.0"),
        target_net_credit=Decimal("20.0"),
        max_bid_ask_spread_pct=Decimal("8.0"),
        max_total_slippage=Decimal("5.0"),
        quote_staleness_seconds=30,
        entry_start_time="09:20",
        entry_end_time="14:30",
        exit_start_time="09:20",
        exit_end_time="15:15",
        limit_step=Decimal("0.05"),
        order_wait_seconds=15,
        max_repricing_attempts=5,
    )


def _make_quote(
    token: int,
    symbol: str,
    bid: str,
    ask: str,
    ltp: Optional[str] = None,
    stale: bool = False,
) -> Quote:
    ts = make_ist(9, 30) if not stale else make_ist(8, 0)
    return Quote(
        instrument_token=token,
        trading_symbol=symbol,
        exchange="NFO",
        timestamp=ts,
        ltp=Decimal(ltp or bid),
        bid=Decimal(bid),
        bid_qty=100,
        ask=Decimal(ask),
        ask_qty=100,
        volume=5000,
        oi=50000,
    )


@pytest.fixture
def good_quotes() -> dict[str, Quote]:
    """Quotes that pass all validations. Net credit: 25-18=7 per share (both spreads)."""
    return {
        "OBEROIRLTY26AUG1700PE": _make_quote(1001, "OBEROIRLTY26AUG1700PE", "7.50", "8.00"),
        "OBEROIRLTY26AUG1720PE": _make_quote(1002, "OBEROIRLTY26AUG1720PE", "12.00", "12.50"),
        "OBEROIRLTY26AUG1920CE": _make_quote(1003, "OBEROIRLTY26AUG1920CE", "13.00", "13.50"),
        "OBEROIRLTY26AUG1940CE": _make_quote(1004, "OBEROIRLTY26AUG1940CE", "10.00", "10.50"),
    }


@pytest.fixture
def stale_quotes() -> dict[str, Quote]:
    return {
        "OBEROIRLTY26AUG1700PE": _make_quote(1001, "OBEROIRLTY26AUG1700PE", "7.50", "8.00", stale=True),
        "OBEROIRLTY26AUG1720PE": _make_quote(1002, "OBEROIRLTY26AUG1720PE", "12.00", "12.50"),
        "OBEROIRLTY26AUG1920CE": _make_quote(1003, "OBEROIRLTY26AUG1920CE", "13.00", "13.50"),
        "OBEROIRLTY26AUG1940CE": _make_quote(1004, "OBEROIRLTY26AUG1940CE", "10.00", "10.50"),
    }


@pytest.fixture
def wide_spread_quotes() -> dict[str, Quote]:
    return {
        "OBEROIRLTY26AUG1700PE": _make_quote(1001, "OBEROIRLTY26AUG1700PE", "5.00", "12.00"),  # 82% spread
        "OBEROIRLTY26AUG1720PE": _make_quote(1002, "OBEROIRLTY26AUG1720PE", "12.00", "12.50"),
        "OBEROIRLTY26AUG1920CE": _make_quote(1003, "OBEROIRLTY26AUG1920CE", "13.00", "13.50"),
        "OBEROIRLTY26AUG1940CE": _make_quote(1004, "OBEROIRLTY26AUG1940CE", "10.00", "10.50"),
    }


@pytest.fixture
def mock_live_source() -> BrokerAdapter:
    mock = MagicMock(spec=BrokerAdapter)
    return mock


@pytest.fixture
def paper_broker(mock_live_source: BrokerAdapter) -> PaperBroker:
    return PaperBroker(
        live_quote_source=mock_live_source,
        fill_mode=PaperFillMode.CONSERVATIVE,
        fill_delay_seconds=0,
        slippage_per_order=Decimal("0.05"),
    )
