from __future__ import annotations
import logging
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from execution.models import DepthLevel, Order, Quote, RiskConfig, Side

logger = logging.getLogger(__name__)

TICK = Decimal("0.05")  # NSE options minimum tick


def round_to_tick(price: Decimal, tick: Decimal = TICK) -> Decimal:
    """Round price to the nearest tick, always conservatively (never past market)."""
    return (price / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick


def initial_limit_price(quote: Quote, side: Side, tick: Decimal = TICK) -> Decimal:
    """
    Starting limit price — passive, near midpoint.
    BUY: start at midpoint rounded DOWN to tick.
    SELL: start at midpoint rounded UP to tick.
    """
    mid = quote.midpoint
    if side == Side.BUY:
        price = (mid / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick
    else:
        price = (mid / tick).quantize(Decimal("1"), rounding=ROUND_UP) * tick
    logger.debug("initial_limit_price", extra={
        "side": side.value, "mid": str(mid), "limit": str(price)
    })
    return price


def next_limit_price(
    current: Decimal,
    quote: Quote,
    side: Side,
    step: Decimal,
    tick: Decimal = TICK,
    max_price: Decimal | None = None,
    min_price: Decimal | None = None,
) -> Decimal:
    """
    Move one step toward the market.
    BUY: increase price toward ask (never above ask).
    SELL: decrease price toward bid (never below bid).
    """
    if side == Side.BUY:
        candidate = current + step
        ceiling = min(quote.ask, max_price) if max_price else quote.ask
        price = min(candidate, ceiling)
    else:
        candidate = current - step
        floor = max(quote.bid, min_price) if min_price else quote.bid
        price = max(candidate, floor)

    price = round_to_tick(price, tick)
    logger.debug("next_limit_price", extra={
        "side": side.value,
        "prev": str(current),
        "next": str(price),
        "bid": str(quote.bid),
        "ask": str(quote.ask),
    })
    return price


def executable_price_for_leg(quote: Quote, side: Side) -> Decimal:
    """
    Conservative price at which this leg can definitely be executed:
    BUY: ask (worst case); SELL: bid (worst case).
    """
    return quote.ask if side == Side.BUY else quote.bid


# ── Depth-aware pricing ──────────────────────────────────────────────────────

def queue_qty_at_price(depth_levels: list[DepthLevel], price: Decimal) -> tuple[int, int]:
    """
    Return (total_quantity, num_orders) at the given price in a depth book.
    Returns (0, 0) if that price level is not present.
    """
    for level in depth_levels:
        if level.price == price:
            return level.quantity, level.orders
    return 0, 0


def analyse_depth(quote: Quote, side: Side, thin_threshold: int = 500) -> dict:
    """
    Summarise order book conditions relevant to our order side.

    For a BUY order we sit in the BID book — competing with other buyers.
    The relevant depth is depth_bids at our price level.

    For a SELL order we sit in the ASK book — competing with other sellers.
    The relevant depth is depth_asks at our price level.

    We also look at the opposing side to gauge likely fill speed:
    - BUY: thick asks → sellers plentiful → can be patient
    - SELL: thick bids → buyers plentiful → can be patient

    Returns dict:
        our_best_price   — best bid (SELL) or best ask (BUY)
        our_best_qty     — total qty at that best level
        our_best_orders  — number of orders at best level
        opp_best_qty     — qty at best opposing level (fill speed indicator)
        is_thin          — True if our_best_qty < thin_threshold
        queue_thinning   — always False here; set by order_manager after polling
        levels           — list of (price, qty, orders) for our side
    """
    if side == Side.BUY:
        our_levels  = quote.depth_bids   # we join the bid book
        opp_levels  = quote.depth_asks   # sellers we need to match us
        our_best    = quote.bid
    else:
        our_levels  = quote.depth_asks   # we join the ask book
        opp_levels  = quote.depth_bids   # buyers who need to match us
        our_best    = quote.ask

    our_best_qty    = our_levels[0].quantity if our_levels else 0
    our_best_orders = our_levels[0].orders   if our_levels else 0
    opp_best_qty    = opp_levels[0].quantity if opp_levels else 0

    return {
        "our_best_price":   our_best,
        "our_best_qty":     our_best_qty,
        "our_best_orders":  our_best_orders,
        "opp_best_qty":     opp_best_qty,
        "is_thin":          our_best_qty < thin_threshold,
        "queue_thinning":   False,
        "levels":           [(l.price, l.quantity, l.orders) for l in our_levels],
    }


def depth_aware_initial_price(
    quote: Quote,
    side: Side,
    tick: Decimal = TICK,
    thin_threshold: int = 500,
) -> tuple[Decimal, dict]:
    """
    Always join at the TOP of the queue:
        BUY  → place at best bid  (highest existing bid = top of bid book)
        SELL → place at best ask  (lowest existing ask = top of ask book)

    This ensures we are at the most competitive price level from the start.
    The queue depth is still analysed and logged so the repricing loop can
    decide whether to hold (queue thinning) or reprice (queue static).

    Returns (limit_price, depth_analysis_dict).
    """
    depth = analyse_depth(quote, side, thin_threshold)

    # Always start at the best existing price on our side of the book
    price = round_to_tick(depth["our_best_price"], tick)
    depth["strategy"] = "top_of_queue"

    logger.info("depth_aware_initial_price", extra={
        "side":        side.value,
        "strategy":    "top_of_queue",
        "queue_qty":   depth["our_best_qty"],
        "is_thin":     depth["is_thin"],
        "limit_price": str(price),
        "bid":         str(quote.bid),
        "ask":         str(quote.ask),
    })

    return price, depth
