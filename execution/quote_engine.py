from __future__ import annotations
import logging
from decimal import Decimal
from typing import Optional
from brokers.base import BrokerAdapter
from execution.models import LegConfig, Quote, RiskConfig, Side
from risk.payoff import ExecutionCredit

logger = logging.getLogger(__name__)

class QuoteEngine:
    def __init__(self, broker: BrokerAdapter) -> None:
        self._broker = broker

    def fetch_all(self, instruments: list[tuple[int, str]]) -> dict[str, Quote]:
        """Fetch quotes for all instruments. Returns dict keyed by trading_symbol."""
        raw = self._broker.get_quotes(instruments)
        result = {}
        for token, quote in raw.items():
            result[quote.trading_symbol] = quote
            logger.debug("quote_fetched", extra={
                "symbol": quote.trading_symbol,
                "bid": str(quote.bid),
                "ask": str(quote.ask),
                "spread_pct": str(quote.spread_pct),
            })
        return result

    def validate_basket(self, quotes: dict[str, Quote], risk: RiskConfig) -> list[str]:
        """Return list of error strings. Empty list means all quotes are valid."""
        errors = []
        for symbol, q in quotes.items():
            if q.is_stale(risk.quote_staleness_seconds):
                errors.append(f"{symbol}: quote is stale")
            if q.bid <= 0 or q.ask <= 0:
                errors.append(f"{symbol}: zero/negative bid or ask")
            if q.spread_pct > risk.max_bid_ask_spread_pct:
                errors.append(
                    f"{symbol}: spread {q.spread_pct:.2f}% > max {risk.max_bid_ask_spread_pct}%"
                )
        if errors:
            logger.warning("basket_validation_failed", extra={"errors": errors})
        return errors

    def calculate_executable_basket_credit(
        self,
        legs: list[LegConfig],
        quotes: dict[str, Quote],  # keyed by trading_symbol
        lot_size: int,
        lots: int,
        resolved_symbols: dict[int, str],  # leg_index -> trading_symbol
    ) -> ExecutionCredit:
        """
        Conservative executable credit:
        BUY legs pay ask; SELL legs receive bid.
        """
        buy_cost = Decimal("0")
        sell_credit = Decimal("0")
        quantity = lot_size * lots

        for i, leg in enumerate(legs):
            symbol = resolved_symbols[i]
            q = quotes[symbol]
            if leg.side == Side.BUY:
                buy_cost += q.ask
                logger.debug("leg_buy_cost", extra={"symbol": symbol, "ask": str(q.ask)})
            else:
                sell_credit += q.bid
                logger.debug("leg_sell_credit", extra={"symbol": symbol, "bid": str(q.bid)})

        net = sell_credit - buy_cost
        credit = ExecutionCredit(
            buy_cost=buy_cost,
            sell_credit=sell_credit,
            net_credit=net,
            quantity=quantity,
            net_credit_total=net * quantity,
        )
        logger.info("basket_credit_calculated", extra={
            "buy_cost": str(buy_cost),
            "sell_credit": str(sell_credit),
            "net_credit": str(net),
            "net_credit_total": str(net * quantity),
        })
        return credit
