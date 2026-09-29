"""
Paper trading broker.
Consumes live quotes but simulates fills.
Implements BrokerAdapter so it can be swapped with ZerodhaBroker.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from brokers.base import BrokerAdapter, InstrumentNotFoundError, QuoteError
from execution.models import (
    Fill,
    Order,
    OrderStatus,
    PaperFillMode,
    Quote,
    ResolvedInstrument,
    Side,
)

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


class PaperBroker(BrokerAdapter):
    """
    Simulates broker behavior using live quote data.
    All fill logic is deterministic and configurable.
    """

    def __init__(
        self,
        live_quote_source: BrokerAdapter,
        fill_mode: PaperFillMode = PaperFillMode.CONSERVATIVE,
        fill_delay_seconds: int = 2,
        slippage_per_order: Decimal = Decimal("0.05"),
    ) -> None:
        self._source = live_quote_source
        self.fill_mode = fill_mode
        self.fill_delay_seconds = fill_delay_seconds
        self.slippage_per_order = slippage_per_order
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []

    # ── BrokerAdapter delegates ────────────────────────────────────────────

    def resolve_instrument(
        self,
        underlying: str,
        exchange: str,
        expiry: date,
        strike: Decimal,
        option_type: str,
    ) -> ResolvedInstrument:
        return self._source.resolve_instrument(
            underlying, exchange, expiry, strike, option_type
        )

    def get_quote(self, instrument_token: int, trading_symbol: str) -> Quote:
        return self._source.get_quote(instrument_token, trading_symbol)

    def get_quotes(
        self, instruments: list[tuple[int, str]]
    ) -> dict[int, Quote]:
        return self._source.get_quotes(instruments)

    def get_positions(self) -> list[dict]:
        """Return simulated positions from paper orders."""
        return []

    def get_orders(self) -> list[dict]:
        return [o.model_dump(mode="json") for o in self._orders.values()]

    def get_margins(self) -> dict:
        return {
            "equity": {
                "available": {
                    "live_balance": 999999.0,
                    "collateral": 0.0,
                    "intraday_payin": 0.0,
                },
                "utilised": {"debits": 0.0},
            },
            "note": "paper_mode",
        }

    def get_index_token(self, underlying: str):
        """Delegate to live source broker."""
        if hasattr(self._source, "get_index_token"):
            return self._source.get_index_token(underlying)
        return None

    def _load_instruments(self, force_refresh: bool = False) -> list:
        """Delegate to live source broker."""
        if hasattr(self._source, "_load_instruments"):
            return self._source._load_instruments(force_refresh=force_refresh)
        return []

    # ── Paper order simulation ─────────────────────────────────────────────

    def simulate_order(self, order: Order, quote: Quote) -> tuple[Order, Optional[Fill]]:
        """
        Simulate whether a limit order would fill given the current quote.
        Returns updated order and fill (if any).
        Does NOT modify internal state — caller stores the result.
        """
        if self.fill_delay_seconds > 0:
            time.sleep(self.fill_delay_seconds)

        filled_price = self._check_fill(order, quote)
        if filled_price is None:
            logger.debug(
                "paper_no_fill",
                extra={
                    "order_id": order.order_id,
                    "limit": str(order.limit_price),
                    "bid": str(quote.bid),
                    "ask": str(quote.ask),
                },
            )
            return order, None

        # Apply slippage
        if order.side == Side.BUY:
            filled_price += self.slippage_per_order
        else:
            filled_price -= self.slippage_per_order
        filled_price = max(Decimal("0.05"), filled_price)

        fill = Fill(
            order_id=order.order_id,
            broker_fill_id=f"PAPER_{uuid.uuid4().hex[:8]}",
            quantity=order.quantity,
            price=filled_price,
            timestamp=datetime.now(IST),
        )
        updated = order.model_copy(
            update={
                "status": OrderStatus.COMPLETE,
                "filled_quantity": order.quantity,
                "average_fill_price": filled_price,
                "last_updated_at": datetime.now(IST),
                "broker_order_id": f"PAPER_{uuid.uuid4().hex[:8]}",
            }
        )
        logger.info(
            "paper_fill",
            extra={
                "order_id": order.order_id,
                "side": order.side.value,
                "quantity": order.quantity,
                "fill_price": str(filled_price),
                "fill_mode": self.fill_mode.value,
            },
        )
        return updated, fill

    def _check_fill(self, order: Order, quote: Quote) -> Optional[Decimal]:
        """Return fill price if order would fill, else None."""
        mode = self.fill_mode

        if order.side == Side.BUY:
            if mode == PaperFillMode.CONSERVATIVE:
                # Fills only if limit >= ask
                if order.limit_price >= quote.ask:
                    return quote.ask
            elif mode == PaperFillMode.MIDPOINT:
                if order.limit_price >= quote.midpoint:
                    return quote.midpoint
            elif mode == PaperFillMode.TOUCH:
                if order.limit_price >= quote.bid:
                    return quote.bid
        else:  # SELL
            if mode == PaperFillMode.CONSERVATIVE:
                # Fills only if limit <= bid
                if order.limit_price <= quote.bid:
                    return quote.bid
            elif mode == PaperFillMode.MIDPOINT:
                if order.limit_price <= quote.midpoint:
                    return quote.midpoint
            elif mode == PaperFillMode.TOUCH:
                if order.limit_price <= quote.ask:
                    return quote.ask
        return None
