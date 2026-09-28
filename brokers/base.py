"""
Abstract broker adapter interface.
Paper and live brokers both implement this.
No order-placement methods are exposed in Phase 2.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import Optional

from execution.models import Order, Quote, ResolvedInstrument


class BrokerError(Exception):
    pass


class InstrumentNotFoundError(BrokerError):
    pass


class QuoteError(BrokerError):
    pass


class BrokerAdapter(ABC):
    """All broker adapters must implement this interface."""

    # ── Instruments ────────────────────────────────────────────────────────

    @abstractmethod
    def resolve_instrument(
        self,
        underlying: str,
        exchange: str,
        expiry: date,
        strike: Decimal,
        option_type: str,
    ) -> ResolvedInstrument:
        """
        Look up the exact trading symbol, instrument token, and lot size
        for a given option leg from the broker's instrument master.
        Raises InstrumentNotFoundError if not found.
        """

    # ── Market Data ────────────────────────────────────────────────────────

    @abstractmethod
    def get_quote(self, instrument_token: int, trading_symbol: str) -> Quote:
        """
        Return a fresh Quote for a single instrument.
        Raises QuoteError on failure.
        """

    @abstractmethod
    def get_quotes(
        self, instruments: list[tuple[int, str]]
    ) -> dict[int, Quote]:
        """
        Batch quote fetch for multiple instruments.
        Returns a dict keyed by instrument_token.
        """

    # ── Account ────────────────────────────────────────────────────────────

    @abstractmethod
    def get_positions(self) -> list[dict]:
        """Return open positions from the broker."""

    @abstractmethod
    def get_orders(self) -> list[dict]:
        """Return today's orders from the broker."""

    @abstractmethod
    def get_margins(self) -> dict:
        """Return available margin / funds."""

    # ── Order placement methods are NOT in this base for Phase 2.
    # They will be added in Phase 5 after all paper tests pass.
