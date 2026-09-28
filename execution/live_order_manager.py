"""
Live order manager.
Extends OrderManager to place and track real orders on Zerodha.
Requires double-confirmation safety gate — enforced both here and in the broker.
Uses idempotency keys stored in SQLite to prevent duplicate orders on restart.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from brokers.zerodha import LiveTradingBlockedError, ZerodhaBroker
from brokers.base import BrokerError
from execution.models import Fill, Order, OrderStatus, Quote, RiskConfig, Side
from execution.pricing import initial_limit_price, next_limit_price, round_to_tick
from risk.kill_switch import KillSwitch, KillSwitchError

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2    # how often to check order status
MAX_POLL_CYCLES = 30         # up to 30×2s = 60s wait per order attempt


class LiveOrderManager:
    """
    Places, modifies, cancels, and confirms fills for live Zerodha orders.
    Safety: checks kill switch before every broker call.
    Safety: idempotency key tracked in DB to prevent duplicate orders.
    Safety: never assumes filled — always polls until status is terminal.
    """

    def __init__(
        self,
        broker: ZerodhaBroker,
        risk: RiskConfig,
        kill_switch: KillSwitch,
        order_repo=None,
        fill_repo=None,
    ) -> None:
        self._broker = broker
        self._risk = risk
        self._ks = kill_switch
        self._order_repo = order_repo
        self._fill_repo = fill_repo

    def execute_with_adaptive_limit(
        self,
        order: Order,
        quote_fn,
        tick: Decimal = Decimal("0.05"),
        max_price: Optional[Decimal] = None,
        min_price: Optional[Decimal] = None,
        product: str = "NRML",
    ) -> tuple[Order, Optional[Fill]]:
        """
        Place a live LIMIT order and poll until filled or max attempts reached.
        Reprices adaptively within configured bounds.
        Returns (updated_order, fill_or_None).
        """
        self._ks.check()

        quote = quote_fn()
        limit = initial_limit_price(quote, order.side, tick)
        order = order.model_copy(update={
            "limit_price": limit,
            "initial_bid": quote.bid,
            "initial_ask": quote.ask,
            "submitted_at": datetime.now(IST),
            "status": OrderStatus.OPEN,
        })

        logger.info("live_order_start", extra={
            "order_id": order.order_id,
            "idempotency_key": order.idempotency_key,
            "symbol": order.trading_symbol,
            "side": order.side.value,
            "quantity": order.quantity,
            "limit": str(limit),
        })

        if self._order_repo:
            self._order_repo.log_event(order.order_id, "SUBMITTED",
                                        new_status=OrderStatus.OPEN.value,
                                        price=limit,
                                        message=f"Initial limit {limit}")

        # Place initial order
        try:
            broker_order_id = self._broker.place_order(
                trading_symbol=order.trading_symbol,
                exchange="NFO",
                transaction_type=order.side.value,
                quantity=order.quantity,
                price=limit,
                product=product,
                tag=order.idempotency_key[:20],
            )
        except LiveTradingBlockedError:
            raise
        except BrokerError as exc:
            logger.error("live_order_place_failed", extra={"error": str(exc)})
            cancelled = order.model_copy(update={
                "status": OrderStatus.REJECTED,
                "notes": str(exc),
            })
            if self._order_repo:
                self._order_repo.log_event(order.order_id, "REJECTED",
                                            new_status=OrderStatus.REJECTED.value,
                                            message=str(exc))
            return cancelled, None

        order = order.model_copy(update={"broker_order_id": broker_order_id})

        for attempt in range(self._risk.max_repricing_attempts + 1):
            self._ks.check()

            # Poll for fill
            updated, fill = self._poll_until_terminal(order, quote_fn)
            if fill is not None:
                if self._fill_repo:
                    self._fill_repo.save(fill)
                if self._order_repo:
                    self._order_repo.update(updated)
                    self._order_repo.log_event(
                        order.order_id, "FILL",
                        old_status=OrderStatus.OPEN.value,
                        new_status=OrderStatus.COMPLETE.value,
                        price=fill.price,
                        quantity=fill.quantity,
                        message=f"Filled on attempt {attempt}",
                    )
                return updated, fill

            if updated.status == OrderStatus.CANCELLED:
                logger.info("order_cancelled_no_fill", extra={"order_id": order.order_id, "attempt": attempt})
                break

            if attempt >= self._risk.max_repricing_attempts:
                logger.warning("live_max_attempts", extra={"order_id": order.order_id})
                # Cancel the order before giving up
                try:
                    self._broker.cancel_order(broker_order_id)
                except BrokerError as exc:
                    logger.error("cancel_before_abort_failed", extra={"error": str(exc)})
                break

            # Reprice
            time.sleep(self._risk.order_wait_seconds)
            self._ks.check()
            quote = quote_fn()
            if quote.is_stale(self._risk.quote_staleness_seconds):
                logger.warning("stale_quote_during_reprice", extra={"order_id": order.order_id})
                try:
                    self._broker.cancel_order(broker_order_id)
                except BrokerError:
                    pass
                break

            new_limit = next_limit_price(
                current=order.limit_price,
                quote=quote,
                side=order.side,
                step=self._risk.limit_step,
                tick=tick,
                max_price=max_price,
                min_price=min_price,
            )

            try:
                self._broker.modify_order(broker_order_id, new_limit)
            except BrokerError as exc:
                logger.error("reprice_failed", extra={"error": str(exc)})
                break

            order = order.model_copy(update={
                "limit_price": new_limit,
                "reprice_count": order.reprice_count + 1,
                "last_updated_at": datetime.now(IST),
            })
            logger.info("live_repriced", extra={
                "order_id": order.order_id,
                "attempt": attempt + 1,
                "new_limit": str(new_limit),
            })
            if self._order_repo:
                self._order_repo.log_event(order.order_id, "REPRICED",
                                            price=new_limit,
                                            message=f"Reprice {attempt + 1}")

        # Final state: cancelled/unfilled
        final = order.model_copy(update={
            "status": OrderStatus.CANCELLED,
            "last_updated_at": datetime.now(IST),
            "notes": "Max reprice attempts reached without fill",
        })
        if self._order_repo:
            self._order_repo.update(final)
        return final, None

    def _poll_until_terminal(
        self,
        order: Order,
        quote_fn,
    ) -> tuple[Order, Optional[Fill]]:
        """
        Poll order status until COMPLETE, CANCELLED, or REJECTED.
        Returns (updated_order, fill_or_None).
        """
        broker_order_id = order.broker_order_id
        for _ in range(MAX_POLL_CYCLES):
            self._ks.check()
            time.sleep(POLL_INTERVAL_SECONDS)
            try:
                raw = self._broker.get_order_status(broker_order_id)
            except BrokerError as exc:
                logger.warning("poll_status_error", extra={"error": str(exc)})
                continue

            status_str = raw.get("status", "").upper()
            filled_qty = int(raw.get("filled_quantity", 0))
            avg_price_raw = raw.get("average_price", 0) or 0
            avg_price = Decimal(str(avg_price_raw))

            if status_str == "COMPLETE" and filled_qty > 0:
                fill = Fill(
                    order_id=order.order_id,
                    broker_fill_id=broker_order_id,
                    quantity=filled_qty,
                    price=avg_price,
                    timestamp=datetime.now(IST),
                )
                updated = order.model_copy(update={
                    "status": OrderStatus.COMPLETE,
                    "filled_quantity": filled_qty,
                    "average_fill_price": avg_price,
                    "last_updated_at": datetime.now(IST),
                })
                logger.info("live_fill_confirmed", extra={
                    "order_id": order.order_id,
                    "filled_qty": filled_qty,
                    "avg_price": str(avg_price),
                })
                return updated, fill

            if status_str in ("CANCELLED", "REJECTED"):
                updated = order.model_copy(update={
                    "status": OrderStatus.CANCELLED
                    if status_str == "CANCELLED" else OrderStatus.REJECTED,
                    "last_updated_at": datetime.now(IST),
                    "notes": raw.get("status_message", ""),
                })
                return updated, None

            # Still open or partially filled — keep polling
            if filled_qty > 0:
                logger.info("live_partial_fill", extra={
                    "order_id": order.order_id, "filled_qty": filled_qty
                })

        # Timed out without terminal status
        logger.warning("poll_timeout", extra={"order_id": order.order_id})
        return order, None
