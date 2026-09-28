from __future__ import annotations
import logging
import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from brokers.paper import PaperBroker
from execution.models import Fill, Order, OrderStatus, Quote, RiskConfig, Side
from execution.pricing import (
    depth_aware_initial_price,
    next_limit_price,
    queue_qty_at_price,
    round_to_tick,
)
from risk.kill_switch import KillSwitch, KillSwitchError

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# If qty at our price drops by this fraction between polls → queue is thinning → hold
THINNING_RATIO = 0.70


class OrderManager:
    """
    Manages the lifecycle of a single limit order in paper mode.

    Depth-aware repricing algorithm:
    1. Fetch initial quote + depth.
    2. If queue is THIN  → place AT best bid/ask (aggressive top-of-book).
       If queue is THICK → start at midpoint (passive, fewer people ahead).
    3. After each wait cycle, compare qty at our price vs previous poll:
       - Queue THINNING (qty fell >30%) → fills are happening, hold position.
       - Queue STATIC / GROWING      → we're stuck, reprice toward market now.
    4. Give up after max_repricing_attempts and return unfilled order.
    """

    def __init__(
        self,
        broker: PaperBroker,
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

    # ── helpers ────────────────────────────────────────────────────────────────

    def _qty_at_our_price(self, quote: Quote, side: Side, price: Decimal) -> int:
        """
        Return the total qty sitting at `price` on OUR side of the book.
        BUY order → we're a bidder → look at depth_bids.
        SELL order → we're a seller → look at depth_asks.
        """
        levels = quote.depth_bids if side == Side.BUY else quote.depth_asks
        qty, _ = queue_qty_at_price(levels, price)
        return qty

    def _log_depth_snapshot(self, order_id: str, quote: Quote, side: Side, label: str) -> None:
        levels = quote.depth_bids if side == Side.BUY else quote.depth_asks
        top5 = [(str(l.price), l.quantity, l.orders) for l in levels[:5]]
        logger.debug("depth_snapshot", extra={
            "order_id": order_id,
            "label": label,
            "side": side.value,
            "top5": top5,
        })

    # ── main execution ─────────────────────────────────────────────────────────

    def execute_with_adaptive_limit(
        self,
        order: Order,
        quote_fn,              # Callable[[], Quote]
        tick: Decimal = Decimal("0.05"),
        max_price: Optional[Decimal] = None,
        min_price: Optional[Decimal] = None,
    ) -> tuple[Order, Optional[Fill]]:
        """
        Attempt to fill a single order using depth-aware adaptive limit repricing.
        Returns (updated_order, fill) — fill is None if max attempts exhausted.
        """
        self._ks.check()

        quote = quote_fn()
        thin_threshold = self._risk.thin_queue_threshold

        # ── Initial price: depth-aware ─────────────────────────────────────────
        limit, depth_info = depth_aware_initial_price(
            quote, order.side, tick, thin_threshold
        )
        self._log_depth_snapshot(order.order_id, quote, order.side, "initial")

        order = order.model_copy(update={
            "limit_price":  limit,
            "initial_bid":  quote.bid,
            "initial_ask":  quote.ask,
            "submitted_at": datetime.now(IST),
            "status":       OrderStatus.OPEN,
        })

        logger.info("order_start", extra={
            "order_id":       order.order_id,
            "side":           order.side.value,
            "symbol":         order.trading_symbol,
            "quantity":       order.quantity,
            "limit":          str(limit),
            "bid":            str(quote.bid),
            "ask":            str(quote.ask),
            "depth_strategy": depth_info["strategy"],
            "queue_qty":      depth_info["our_best_qty"],
            "thin_threshold": thin_threshold,
        })

        if self._order_repo:
            self._order_repo.log_event(
                order.order_id, "SUBMITTED",
                new_status=OrderStatus.OPEN.value,
                price=limit,
                message=(
                    f"Depth-aware start: {depth_info['strategy']} "
                    f"(queue={depth_info['our_best_qty']} qty)"
                ),
            )

        # Track qty at our price so we know whether the queue is thinning
        prev_qty = self._qty_at_our_price(quote, order.side, limit)

        # ── Repricing loop ─────────────────────────────────────────────────────
        for attempt in range(self._risk.max_repricing_attempts + 1):
            self._ks.check()

            updated, fill = self._broker.simulate_order(order, quote)

            if fill is not None:
                logger.info("order_filled", extra={
                    "order_id":   order.order_id,
                    "fill_price": str(fill.price),
                    "attempt":    attempt,
                })
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

            if attempt >= self._risk.max_repricing_attempts:
                logger.warning("order_max_attempts_reached", extra={
                    "order_id": order.order_id,
                    "attempts": attempt,
                })
                break

            # ── Wait, then reassess depth ──────────────────────────────────────
            time.sleep(self._risk.order_wait_seconds)
            quote = quote_fn()

            if quote.is_stale(self._risk.quote_staleness_seconds):
                logger.warning("order_stale_quote_during_reprice", extra={
                    "order_id": order.order_id
                })
                break

            self._log_depth_snapshot(order.order_id, quote, order.side, f"reprice_{attempt+1}")
            curr_qty = self._qty_at_our_price(quote, order.side, order.limit_price)

            # Decide: hold position or reprice?
            # curr_qty == 0 means our price LEFT the book entirely (market shifted) → reprice
            # curr_qty dropped >30% from prev → fills happening at our level → hold
            queue_thinning = (
                prev_qty > 0 and
                curr_qty > 0 and
                curr_qty < prev_qty * THINNING_RATIO
            )

            if queue_thinning:
                # Fills are happening at our price — stay put, we're moving up the queue
                logger.info("order_queue_thinning_hold", extra={
                    "order_id":   order.order_id,
                    "prev_qty":   prev_qty,
                    "curr_qty":   curr_qty,
                    "limit":      str(order.limit_price),
                })
                prev_qty = curr_qty
                if self._order_repo:
                    self._order_repo.log_event(
                        order.order_id, "HOLD",
                        price=order.limit_price,
                        message=(
                            f"Queue thinning at {order.limit_price}: "
                            f"{prev_qty}→{curr_qty} qty. Holding."
                        ),
                    )
                continue   # skip repricing this cycle

            # Queue is static or thickening → reprice toward market
            new_limit = next_limit_price(
                current=order.limit_price,
                quote=quote,
                side=order.side,
                step=self._risk.limit_step,
                tick=tick,
                max_price=max_price,
                min_price=min_price,
            )

            order = order.model_copy(update={
                "limit_price":   new_limit,
                "reprice_count": order.reprice_count + 1,
                "last_updated_at": datetime.now(IST),
            })

            prev_qty = self._qty_at_our_price(quote, order.side, new_limit)

            logger.info("order_repriced", extra={
                "order_id":  order.order_id,
                "attempt":   attempt + 1,
                "new_limit": str(new_limit),
                "prev_qty":  prev_qty,
                "bid":       str(quote.bid),
                "ask":       str(quote.ask),
                "reason":    "queue_static",
            })

            if self._order_repo:
                self._order_repo.log_event(
                    order.order_id, "REPRICED",
                    price=new_limit,
                    message=(
                        f"Reprice #{attempt+1}: queue static "
                        f"({curr_qty} qty). New limit: {new_limit}"
                    ),
                )

        # ── Failed to fill ─────────────────────────────────────────────────────
        cancelled = order.model_copy(update={
            "status":          OrderStatus.CANCELLED,
            "last_updated_at": datetime.now(IST),
            "notes":           "Max reprice attempts reached without fill",
        })
        if self._order_repo:
            self._order_repo.update(cancelled)
            self._order_repo.log_event(
                cancelled.order_id, "CANCELLED",
                old_status=OrderStatus.OPEN.value,
                new_status=OrderStatus.CANCELLED.value,
                message="Not filled after max attempts",
            )
        logger.warning("order_cancelled_unfilled", extra={"order_id": order.order_id})
        return cancelled, None
