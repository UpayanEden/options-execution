"""
Shared order fill poller and limit-order price escalation for shift execution.

Instead of one order_history call per order, a single SharedOrderPoller calls
kite.orders() once per tick and dispatches fill status to all waiting orders.

Usage::

    poller = SharedOrderPoller(broker)
    oid, filled_qty = place_and_confirm_shift_leg(
        broker, order, trade_logger, config, poller,
        cutoff_time_str="15:20",
        get_now_str_fn=runner._get_now_str,
    )
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Callable, Dict, Optional, Tuple

from execution.models import Order
from live.trade_logger import EventType, TradeLogger

logger = logging.getLogger(__name__)


class SharedOrderPoller:
    """
    Single-call order status poller shared across all shift legs.

    Rather than each leg making its own broker call, one call to
    broker.get_orders() is made per tick and results dispatched.
    """

    def __init__(self, broker: Any) -> None:
        """
        Args:
            broker: BrokerAdapter with get_orders() method.
        """
        self._broker = broker
        self._pending: Dict[str, dict] = {}
        self._last_poll_time: float = 0.0
        self._last_poll_result: Dict[str, Tuple[str, int, Decimal]] = {}

    def register(self, broker_oid: str, order: Order, initial_price: Decimal) -> None:
        """
        Register an order for tracking.

        Args:
            broker_oid:    Broker-assigned order ID.
            order:         The Order model instance.
            initial_price: The initial limit price used.
        """
        self._pending[broker_oid] = {
            "order": order,
            "initial_price": initial_price,
        }

    def poll_once(self, fill_poll_interval_s: float = 0.5) -> Dict[str, Tuple[str, int, Decimal]]:
        """
        Call broker.get_orders() once and return fill statuses.

        Returns from cache if last poll was < fill_poll_interval_s ago.
        Handles HTTP 429 by sleeping 1s.

        Args:
            fill_poll_interval_s: Minimum seconds between real API calls.

        Returns:
            Dict of broker_oid → (status, filled_qty, avg_price).
        """
        now = time.time()
        if now - self._last_poll_time < fill_poll_interval_s:
            return self._last_poll_result

        result: Dict[str, Tuple[str, int, Decimal]] = {}
        try:
            orders_raw = self._broker.get_orders()  # type: ignore[attr-defined]
            for o in (orders_raw or []):
                oid = str(o.get("order_id", ""))
                if oid not in self._pending:
                    continue
                status = str(o.get("status", "")).upper()
                filled_qty = int(o.get("filled_quantity", 0) or 0)
                avg_price = Decimal(str(o.get("average_price", 0) or 0))
                result[oid] = (status, filled_qty, avg_price)
        except Exception as exc:
            err_str = str(exc)
            if "429" in err_str or "Too Many" in err_str.lower():
                logger.warning("order_poller_rate_limited", extra={"error": err_str})
                time.sleep(1.0)
            else:
                logger.warning("order_poller_get_orders_failed", extra={"error": err_str})

        self._last_poll_time = time.time()
        self._last_poll_result = result
        return result

    def unregister(self, broker_oid: str) -> None:
        """
        Remove a broker order ID from the pending tracking set.

        Args:
            broker_oid: Broker order ID to remove.
        """
        self._pending.pop(broker_oid, None)
        self._last_poll_result.pop(broker_oid, None)


def place_and_confirm_shift_leg(
    broker: Any,
    order: Order,
    trade_logger: TradeLogger,
    config: Any,
    poller: SharedOrderPoller,
    cutoff_time_str: str,
    get_now_str_fn: Callable[[], str],
    price_refresher: Optional[Callable[[], Optional[Decimal]]] = None,
) -> Tuple[Optional[str], int]:
    """
    Place a single shift leg order and confirm its fill using SharedOrderPoller.

    If the order is not filled within shift_order_timeout_s seconds, cancel it and
    re-place at a fresh market price obtained via price_refresher. Loop until filled
    or the time cutoff is reached.

    Args:
        broker:           BrokerAdapter with place_order_from_model() and cancel_order().
        order:            The Order model to place.
        trade_logger:     TradeLogger for structured event logging.
        config:           StrategyParams with fill_poll_interval_s, shift_order_timeout_s.
        poller:           SharedOrderPoller instance (shared across shift legs).
        cutoff_time_str:  HH:MM time cutoff — cancel and return if now >= this.
        get_now_str_fn:   Callable returning current IST time as "HH:MM".
        price_refresher:  Optional callable returning a fresh Decimal limit price.
                          Called on timeout to re-place at market. If None, timeout
                          results in a hard failure.

    Returns:
        Tuple (broker_oid, filled_qty).
        broker_oid is None on full failure.
        filled_qty is 0 on timeout/cancel, or partial fill count on partial.
    """
    fill_poll_interval_s = float(getattr(config, "fill_poll_interval_s", 0.5))
    order_timeout_s = float(getattr(config, "shift_order_timeout_s", 2.0))

    current_order = order

    # Check cutoff before placing
    if get_now_str_fn() >= cutoff_time_str:
        logger.warning("shift_leg_cutoff_before_place", extra={
            "symbol": order.trading_symbol, "cutoff": cutoff_time_str
        })
        return None, 0

    # Place the initial order
    try:
        if hasattr(broker, "place_order_from_model"):
            broker_oid = str(broker.place_order_from_model(current_order))  # type: ignore[attr-defined]
        else:
            broker_oid = str(broker.place_order(  # type: ignore[attr-defined]
                trading_symbol=current_order.trading_symbol,
                exchange="NFO",
                transaction_type=current_order.side.value,
                quantity=current_order.quantity,
                price=current_order.limit_price,
                product="NRML",
            ))
    except Exception as exc:
        logger.error("shift_leg_place_failed", extra={
            "symbol": order.trading_symbol, "error": str(exc)
        })
        trade_logger.log(
            EventType.ORDER_REJECTED,
            order.trading_symbol,
            {"error": str(exc), "phase": "shift_leg_place"},
            level="ERROR",
        )
        return None, 0

    trade_logger.log(
        EventType.ORDER_PLACED,
        order.trading_symbol,
        {
            "broker_order_id": broker_oid,
            "side": order.side.value,
            "quantity": order.quantity,
            "limit_price": str(order.limit_price),
        },
    )
    poller.register(broker_oid, current_order, current_order.limit_price)

    deadline = time.time() + order_timeout_s

    while True:
        # Cutoff check
        if get_now_str_fn() >= cutoff_time_str:
            logger.warning("shift_leg_cutoff_during_poll", extra={
                "symbol": order.trading_symbol, "broker_oid": broker_oid
            })
            _cancel_order(broker, broker_oid, order.trading_symbol)
            poller.unregister(broker_oid)
            return None, 0

        results = poller.poll_once(fill_poll_interval_s)
        status_info = results.get(broker_oid)
        if status_info:
            status, filled_qty, avg_price = status_info

            if status == "COMPLETE":
                trade_logger.log(
                    EventType.ORDER_CONFIRMED,
                    order.trading_symbol,
                    {
                        "broker_order_id": broker_oid,
                        "side": order.side.value,
                        "fill_price": str(avg_price),
                        "quantity": order.quantity,
                        "filled_qty": filled_qty,
                    },
                )
                poller.unregister(broker_oid)
                return broker_oid, filled_qty

            if status in ("CANCELLED", "REJECTED"):
                logger.warning("shift_leg_cancelled_or_rejected", extra={
                    "symbol": order.trading_symbol, "status": status
                })
                trade_logger.log(
                    EventType.ORDER_REJECTED,
                    order.trading_symbol,
                    {"broker_order_id": broker_oid, "status": status},
                    level="WARNING",
                )
                poller.unregister(broker_oid)
                if filled_qty > 0:
                    return broker_oid, filled_qty
                return None, 0

        time.sleep(fill_poll_interval_s)

        # Timeout: cancel and re-place at fresh price
        if time.time() >= deadline:
            _cancel_order(broker, broker_oid, order.trading_symbol)
            poller.unregister(broker_oid)

            if price_refresher is None:
                logger.warning("shift_leg_timeout_no_refresher", extra={
                    "symbol": order.trading_symbol, "broker_oid": broker_oid
                })
                return None, 0

            if get_now_str_fn() >= cutoff_time_str:
                return None, 0

            new_price = price_refresher()
            if new_price is None:
                logger.warning("shift_leg_refresh_price_failed", extra={
                    "symbol": order.trading_symbol
                })
                return None, 0

            logger.info("shift_leg_replace_fresh_price", extra={
                "symbol": order.trading_symbol,
                "old_price": str(current_order.limit_price),
                "new_price": str(new_price),
            })
            current_order = current_order.model_copy(update={"limit_price": new_price})

            # Place new order at fresh price
            try:
                if hasattr(broker, "place_order_from_model"):
                    broker_oid = str(broker.place_order_from_model(current_order))  # type: ignore[attr-defined]
                else:
                    broker_oid = str(broker.place_order(  # type: ignore[attr-defined]
                        trading_symbol=current_order.trading_symbol,
                        exchange="NFO",
                        transaction_type=current_order.side.value,
                        quantity=current_order.quantity,
                        price=current_order.limit_price,
                        product="NRML",
                    ))
            except Exception as exc:
                logger.error("shift_leg_replace_failed", extra={
                    "symbol": order.trading_symbol, "error": str(exc)
                })
                return None, 0

            trade_logger.log(
                EventType.ORDER_PLACED,
                order.trading_symbol,
                {
                    "broker_order_id": broker_oid,
                    "side": order.side.value,
                    "quantity": current_order.quantity,
                    "limit_price": str(new_price),
                    "phase": "replace_fresh_price",
                },
            )
            poller.register(broker_oid, current_order, new_price)
            deadline = time.time() + order_timeout_s


def _cancel_order(broker: Any, broker_oid: str, symbol: str) -> None:
    """Attempt to cancel an order, logging any failure."""
    try:
        if hasattr(broker, "cancel_order"):
            broker.cancel_order(broker_oid)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("shift_leg_cancel_failed", extra={
            "symbol": symbol, "broker_oid": broker_oid, "error": str(exc)
        })
